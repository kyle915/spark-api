"""Integration coverage for the event-confirmation GraphQL surface.

Exercises the CLIENTS schema — the endpoint spark-front-client actually calls
(VITE_GRAPHQL_ENDPOINT → /graphql/clients) — rather than the resolvers
directly, so this also pins that the fields are mounted where the admin tab
can reach them and that tenant scoping is enforced.

The two things most worth pinning here:

1. **The typed date/time is interpreted in the VENUE's timezone.** The tab
   sends wall-clock strings plus a timezone name; the server owns the
   conversion. A 1pm Chicago shift has to land on 18:00Z, not on 1pm in
   whatever `settings.TIME_ZONE` happens to be (UTC).
2. **Sending creates the confirmation and NOTHING else** — no Event, no
   AmbassadorEvent — because creating a booking would fire the BA a
   "New shift offered" push on top of the email.
"""

from __future__ import annotations

from datetime import timedelta
from unittest.mock import patch

import pytest
from asgiref.sync import sync_to_async
from django.utils import timezone as djtz

from ambassadors.tests.base import AmbassadorsGraphQLTestCase
from events.models import EventConfirmation, EventConfirmationSend

SEND_PATH = "events.event_confirmations.EventConfirmationMailer.send_now"

FORM_QUERY = """
    query FormOptions($tenantId: ID!) {
      eventConfirmationFormOptions(tenantId: $tenantId) {
        productOptions
        recapUrl
        trainingUrl
        fromEmail
        hasRecapLink
        hasTrainingLink
      }
    }
"""

SEND_MUTATION = """
    mutation Send($input: SendEventConfirmationInput!) {
      sendEventConfirmation(input: $input) {
        success
        message
        confirmation {
          uuid
          baName
          baEmail
          dateLabel
          timeLabel
          products
          sendReminders
          sends { stage sentAt }
        }
      }
    }
"""

LIST_QUERY = """
    query List($tenantId: ID!) {
      eventConfirmations(tenantId: $tenantId) { uuid baEmail sendReminders }
    }
"""

CANCEL_MUTATION = """
    mutation Cancel($uuid: ID!) {
      cancelEventConfirmationReminders(uuid: $uuid) {
        success
        confirmation { sendReminders cancelledAt }
      }
    }
"""


@pytest.mark.django_db(transaction=True)
class TestEventConfirmationGraphQL(AmbassadorsGraphQLTestCase):
    @pytest.fixture(autouse=True)
    def setup(self, db):
        from config.schema_client import schema_clients

        self.roles = self.setup_default_roles()
        self.system_user = self.get_system_user()
        self.schema = schema_clients
        self.endpoint_path = "/api/v1/graphql/clients"

        self.tenant = self.create_tenant(
            name="Liquid Death",
            checkin_code="LD-TNBJ8K",
            checkin_training_url=(
                "https://admin.igniteproductions.co/training/LD-FZUWXT"
            ),
        )
        self.other_tenant = self.create_tenant(name="Feel Free")
        self.admin = self.create_user(
            username="admin-conf@test.com",
            email="admin-conf@test.com",
            role=self.roles["spark_admin"],
        )

    async def _run(self, query, variables):
        return await self._execute_query_authenticated(
            query, variables, self.admin, self.endpoint_path
        )

    def _payload(self, **overrides):
        base = dict(
            tenantId=str(self.tenant.id),
            baName="Deond Thomas",
            baEmail="deond762@example.com",
            date="2026-08-01",
            startTime="13:00",
            endTime="16:00",
            timezoneName="America/Chicago",
            storeName="Jewel Osco",
            address="4042 W Foster Ave, Chicago, IL 60630, USA",
            eventTypeLabel="Retail Sampling",
            products=[
                "Sparkling Water — Squeezed-to-Death",
                "Sparkling Water — Severed Lime",
            ],
            sendReminders=True,
            sendNow=True,
        )
        base.update(overrides)
        return base

    # ---------------------------------------------------------------- reads

    @pytest.mark.asyncio
    async def test_form_options_reads_the_links_off_the_tenant(self):
        result = await self._run(FORM_QUERY, {"tenantId": str(self.tenant.id)})
        assert result.errors is None, result.errors
        data = result.data["eventConfirmationFormOptions"]

        assert len(data["productOptions"]) == 31
        assert data["productOptions"][0].startswith("Sparkling Water — ")
        # Built from Tenant.checkin_code / checkin_training_url, not hardcoded.
        assert data["recapUrl"].endswith("/checkin/LD-TNBJ8K")
        assert data["trainingUrl"].endswith("/training/LD-FZUWXT")
        assert data["hasRecapLink"] is True
        assert data["hasTrainingLink"] is True
        assert "staffing@igniteproductions.co" in data["fromEmail"]

    @pytest.mark.asyncio
    async def test_a_tenant_with_no_links_is_flagged_not_faked(self):
        result = await self._run(
            FORM_QUERY, {"tenantId": str(self.other_tenant.id)}
        )
        assert result.errors is None, result.errors
        data = result.data["eventConfirmationFormOptions"]
        assert data["hasRecapLink"] is False
        assert data["hasTrainingLink"] is False
        assert data["recapUrl"] == ""

    # --------------------------------------------------------------- writes

    @pytest.mark.asyncio
    async def test_send_stores_the_instant_in_the_venues_timezone(self):
        with patch(SEND_PATH):
            result = await self._run(SEND_MUTATION, {"input": self._payload()})
        assert result.errors is None, result.errors
        payload = result.data["sendEventConfirmation"]
        assert payload["success"] is True

        row = await sync_to_async(
            lambda: EventConfirmation.objects.select_related("timezone").get()
        )()
        # 1pm America/Chicago on 2026-08-01 (CDT, UTC-5) == 18:00Z. Storing the
        # naive wall clock would have made this 13:00Z and every reminder five
        # hours early.
        assert row.starts_at.isoformat() == "2026-08-01T18:00:00+00:00"
        assert row.ends_at.isoformat() == "2026-08-01T21:00:00+00:00"
        # …and it renders back as the BA's local wall clock.
        assert payload["confirmation"]["dateLabel"] == "08/01/2026"
        assert payload["confirmation"]["timeLabel"] == "1p - 4p"

    @pytest.mark.asyncio
    async def test_send_creates_no_event_and_no_booking(self):
        from ambassadors.models import AmbassadorEvent
        from events.models import Event

        with patch(SEND_PATH) as send:
            result = await self._run(SEND_MUTATION, {"input": self._payload()})
        assert result.data["sendEventConfirmation"]["success"] is True
        assert send.call_count == 1

        counts = await sync_to_async(
            lambda: (
                Event.objects.count(),
                AmbassadorEvent.objects.count(),
                EventConfirmation.objects.count(),
            )
        )()
        # No Event and no roster row means no "New shift offered" push, no
        # Google Calendar sync and no KPI movement — just the email.
        assert counts == (0, 0, 1)

    @pytest.mark.asyncio
    async def test_booked_stage_is_stamped_so_the_sweep_skips_it(self):
        with patch(SEND_PATH):
            await self._run(SEND_MUTATION, {"input": self._payload()})
        stages = await sync_to_async(
            lambda: list(
                EventConfirmationSend.objects.values_list("stage", flat=True)
            )
        )()
        assert stages == [EventConfirmation.STAGE_BOOKED]

    @pytest.mark.asyncio
    async def test_save_without_emailing_persists_but_sends_nothing(self):
        with patch(SEND_PATH) as send:
            result = await self._run(
                SEND_MUTATION, {"input": self._payload(sendNow=False)}
            )
        assert result.data["sendEventConfirmation"]["success"] is True
        assert send.call_count == 0
        assert await sync_to_async(EventConfirmation.objects.count)() == 1
        assert await sync_to_async(EventConfirmationSend.objects.count)() == 0

    @pytest.mark.asyncio
    async def test_overnight_shift_rolls_the_end_to_the_next_day(self):
        with patch(SEND_PATH):
            await self._run(
                SEND_MUTATION,
                {"input": self._payload(startTime="21:00", endTime="01:00")},
            )
        row = await sync_to_async(EventConfirmation.objects.get)()
        assert row.ends_at - row.starts_at == timedelta(hours=4)

    @pytest.mark.parametrize(
        "bad,expected",
        [
            ({"baName": "  "}, "BA name is required"),
            ({"baEmail": "nope"}, "valid BA email"),
            ({"date": "not-a-date"}, "date/time"),
        ],
    )
    @pytest.mark.asyncio
    async def test_bad_input_is_rejected_without_writing(self, bad, expected):
        with patch(SEND_PATH) as send:
            result = await self._run(
                SEND_MUTATION, {"input": self._payload(**bad)}
            )
        payload = result.data["sendEventConfirmation"]
        assert payload["success"] is False
        assert expected in payload["message"]
        assert send.call_count == 0
        assert await sync_to_async(EventConfirmation.objects.count)() == 0

    @pytest.mark.asyncio
    async def test_cancelling_stops_reminders_but_keeps_the_record(self):
        with patch(SEND_PATH):
            sent = await self._run(SEND_MUTATION, {"input": self._payload()})
        uuid = sent.data["sendEventConfirmation"]["confirmation"]["uuid"]

        result = await self._run(CANCEL_MUTATION, {"uuid": uuid})
        assert result.errors is None, result.errors
        conf = result.data["cancelEventConfirmationReminders"]["confirmation"]
        assert conf["sendReminders"] is False
        assert conf["cancelledAt"] is not None
        # Kept, not deleted — the booked email really did go to the BA.
        assert await sync_to_async(EventConfirmation.objects.count)() == 1

    # -------------------------------------------------------------- scoping

    @pytest.mark.asyncio
    async def test_history_is_scoped_to_the_tenant(self):
        with patch(SEND_PATH):
            await self._run(SEND_MUTATION, {"input": self._payload()})

        mine = await self._run(LIST_QUERY, {"tenantId": str(self.tenant.id)})
        assert len(mine.data["eventConfirmations"]) == 1

        # The other client must not see it — an unscoped read is how Feel Free
        # once leaked onto the Total Wireless board.
        theirs = await self._run(
            LIST_QUERY, {"tenantId": str(self.other_tenant.id)}
        )
        assert theirs.data["eventConfirmations"] == []

    @pytest.mark.asyncio
    async def test_reminders_flag_threads_through_to_the_row(self):
        with patch(SEND_PATH):
            await self._run(
                SEND_MUTATION, {"input": self._payload(sendReminders=False)}
            )
        row = await sync_to_async(EventConfirmation.objects.get)()
        assert row.send_reminders is False

        # And such a row is invisible to the sweep.
        from events.event_confirmations import due_reminders

        future = djtz.now() + timedelta(days=400)
        assert await sync_to_async(due_reminders)(future) == []

    @pytest.mark.asyncio
    async def test_event_type_prefill_comes_from_the_tenants_default(self):
        """The Event Type field arrives filled in from
        Tenant.checkin_event_type — the same column that decides which program
        a check-in stamps — so the tab can't disagree with the check-in link."""
        from events.models import EventType

        def _seed():
            retail = EventType.objects.create(
                name="Retail Sampling",
                tenant=self.tenant,
                created_by=self.system_user,
            )
            activation = EventType.objects.create(
                name="Event Activation",
                tenant=self.tenant,
                created_by=self.system_user,
            )
            self.tenant.checkin_event_type = retail
            self.tenant.save(update_fields=["checkin_event_type"])
            self.tenant.checkin_event_types.set([retail, activation])

        await sync_to_async(_seed)()

        result = await self._run(
            """
            query O($tenantId: ID!) {
              eventConfirmationFormOptions(tenantId: $tenantId) {
                defaultEventTypeLabel
                eventTypeOptions
                brandName
              }
            }
            """,
            {"tenantId": str(self.tenant.id)},
        )
        assert result.errors is None, result.errors
        data = result.data["eventConfirmationFormOptions"]
        assert data["defaultEventTypeLabel"] == "Retail Sampling"
        # Default first, so it's the obvious pick in the datalist.
        assert data["eventTypeOptions"][0] == "Retail Sampling"
        assert set(data["eventTypeOptions"]) == {
            "Retail Sampling",
            "Event Activation",
        }
        assert data["brandName"] == "Liquid Death"

    @pytest.mark.asyncio
    async def test_a_tenant_with_no_pinned_program_still_gets_options(self):
        """One event type and no pin: that type IS the default."""
        from events.models import EventType

        await sync_to_async(EventType.objects.create)(
            name="Field Sampling",
            tenant=self.other_tenant,
            created_by=self.system_user,
        )
        result = await self._run(
            """
            query O($tenantId: ID!) {
              eventConfirmationFormOptions(tenantId: $tenantId) {
                defaultEventTypeLabel
                eventTypeOptions
              }
            }
            """,
            {"tenantId": str(self.other_tenant.id)},
        )
        data = result.data["eventConfirmationFormOptions"]
        assert data["defaultEventTypeLabel"] == "Field Sampling"
        assert data["eventTypeOptions"] == ["Field Sampling"]
