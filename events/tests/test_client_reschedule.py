"""Client "Change date" (rescheduleEvent) + client date edits on updateRequest.

Run through the real clients schema (the endpoint the web app uses):
  * a member of the event's brand can move it; a client of another brand,
    and a brand ambassador, get "Event not found.";
  * cancelled / clocked-in / recapped / past moves are refused;
  * the event AND its request move together, through the venue's zone;
  * a client move emails the request CC list with old → new and a booked-BA
    count (never names); an admin move sends nothing;
  * updateRequest: a client date change on a pending request emails Ignite,
    and is refused when the request already has live events.
"""

from __future__ import annotations

from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

import pytest

from ambassadors.models import AmbassadorEvent, Attendance
from ambassadors.tests.base import AmbassadorsGraphQLTestCase
from events import models as em
from events.live_board import _ops_today

CC = ["ops@igniteproductions.co", "kyle@igniteproductions.co"]
LA = ZoneInfo("America/Los_Angeles")

RESCHEDULE = """
mutation R($input: RescheduleEventInput!) {
  rescheduleEvent(input: $input) {
    success message requestMoved
    event { uuid startTime endTime }
  }
}
"""

UPDATE_REQUEST = """
mutation U($input: UpdateRequestInput!) {
  updateRequest(input: $input) { success message }
}
"""


@pytest.mark.django_db(transaction=True)
class TestClientReschedule(AmbassadorsGraphQLTestCase):
    @pytest.fixture(autouse=True)
    def setup(self, db, monkeypatch):
        from config.schema_client import schema_clients

        self.schema = schema_clients
        self.endpoint = "/api/v1/graphql/clients"
        self.roles = self.setup_default_roles()
        self.sys = self.get_system_user()

        self.sent = []
        monkeypatch.setattr(
            "utils.mailer.Mailer.send_now",
            lambda mailer: self.sent.append(mailer.envelope()),
        )
        monkeypatch.setattr(
            "events.mutations._get_request_cc_emails",
            lambda exclude_email=None, tenant=None: list(CC),
        )
        # Admin updateEvent/BA paths are not under test; keep pushes quiet.
        monkeypatch.setattr(
            "ambassadors.push._send_push_to_user_sync", lambda *a, **k: 0, raising=False
        )

        self.brand = self.create_tenant(name="Feel Free")
        self.other = self.create_tenant(name="Other Brand")
        self.pacific = em.TimeZone.objects.create(name="Pacific", code="PST", offset=-480)

        self.client_user = self.create_user(
            username="lee@brand.test", email="lee@brand.test",
            role=self.roles["client"], first_name="Lee", last_name="K",
        )
        self.create_tenanted_user(self.client_user, self.brand)
        self.outsider = self.create_user(
            username="sam@other.test", email="sam@other.test", role=self.roles["client"],
        )
        self.create_tenanted_user(self.outsider, self.other)
        self.ba_user = self.create_user(
            username="ba@brand.test", email="ba@brand.test", role=self.roles["ambassador"],
        )
        self.create_tenanted_user(self.ba_user, self.brand)
        self.admin = self.create_user(
            username="admin@test.com", email="admin@test.com", role=self.roles["spark_admin"],
        )

        self.approved_ev = self.create_event_status("Approved", self.brand, slug="approved")
        self.approved_req = self.create_request_status("Approved", self.brand, slug="approved")
        self.pending_req = self.create_request_status("Pending", self.brand, slug="pending")
        self.req_type = self.create_request_type(name="Demo", tenant=self.brand)
        location = em.Location.objects.create(
            name="Austin", code="AUS", zip="78701", created_by=self.sys,
        )
        self.retailer = self.create_retailer(
            name="Whole Foods Lamar", address="525 N Lamar", store_contact="x",
            location=location, tenant=self.brand,
        )
        self.client_row = self.create_client(name="FF", email="ff@client.test", tenant=self.brand)
        self.distributor = self.create_distributor(
            name="Dist", email="d@dist.test", location=location, tenant=self.brand,
        )

        self.day = _ops_today() + timedelta(days=10)
        self.new_day = _ops_today() + timedelta(days=12)
        self.request = self._make_request(self.approved_req)
        self.event = self.create_event(
            name="Feel Free demo — Whole Foods Lamar",
            tenant=self.brand,
            address="525 N Lamar",
            request=self.request,
            retailer=self.retailer,
            status=self.approved_ev,
            timezone=self.pacific,
            date=self._at(self.day, time(0, 0)),
            start_time=self._at(self.day, time(10, 0)),
            end_time=self._at(self.day, time(14, 0)),
        )
        ba = self.create_ambassador(self.ba_user)
        AmbassadorEvent.objects.create(
            ambassador=ba, event=self.event, tenant=self.brand, is_approved=True,
            created_by=self.sys,
        )

    # ── helpers ────────────────────────────────────────────────────────

    def _at(self, day, clock):
        return datetime.combine(day, clock, tzinfo=LA)

    def _make_request(self, status):
        return self.create_request(
            name="FF request", date=self._at(self.day, time(0, 0)), address="525 N Lamar",
            client=self.client_row, distributor=self.distributor, retailer=self.retailer,
            request_type=self.req_type, tenant=self.brand,
            start_time=self._at(self.day, time(10, 0)),
            end_time=self._at(self.day, time(14, 0)),
            status=status, timezone=self.pacific,
        )

    async def _move(self, user, day=None, start="11:00", end="15:00", event=None):
        result = await self._execute_mutation(
            RESCHEDULE,
            {"input": {
                "eventId": str((event or self.event).uuid),
                "date": (day or self.new_day).isoformat(),
                "startTime": start,
                "endTime": end,
            }},
            self.endpoint,
            user=user,
        )
        assert result.errors is None, result.errors
        return result.data["rescheduleEvent"]

    async def _reload(self, model, pk):
        from asgiref.sync import sync_to_async

        return await sync_to_async(model.objects.get)(pk=pk)

    # ── permission ─────────────────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_member_client_moves_event_and_request(self):
        res = await self._move(self.client_user)
        assert res["success"] is True, res["message"]
        assert res["requestMoved"] is True

        ev = await self._reload(em.Event, self.event.pk)
        req = await self._reload(em.Request, self.request.pk)
        want_start = self._at(self.new_day, time(11, 0))
        want_end = self._at(self.new_day, time(15, 0))
        assert ev.start_time == want_start and ev.end_time == want_end
        assert req.start_time == want_start and req.end_time == want_end
        assert ev.date == self._at(self.new_day, time(0, 0))
        assert req.date == ev.date

        log = await self._latest_log()
        assert log.kind == em.RequestActivityLog.KIND_UPDATED
        assert "→" in log.summary
        assert log.metadata["by_client"] is True

    @pytest.mark.asyncio
    async def test_non_member_client_refused(self):
        res = await self._move(self.outsider)
        assert res["success"] is False
        assert res["message"] == "Event not found."
        assert self.sent == []

    @pytest.mark.asyncio
    async def test_ambassador_refused_even_as_member(self):
        res = await self._move(self.ba_user)
        assert res["success"] is False
        assert res["message"] == "Event not found."
        ev = await self._reload(em.Event, self.event.pk)
        assert ev.start_time == self._at(self.day, time(10, 0))

    # ── refusals ───────────────────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_cancelled_event_refused(self):
        from asgiref.sync import sync_to_async

        suspended = await sync_to_async(self.create_event_status)(
            "Suspended", self.brand, slug="suspended"
        )
        await sync_to_async(em.Event.objects.filter(pk=self.event.pk).update)(status=suspended)
        res = await self._move(self.client_user)
        assert res["success"] is False
        assert "cancelled" in res["message"]
        assert self.sent == []

    @pytest.mark.asyncio
    async def test_clocked_in_event_refused(self):
        from asgiref.sync import sync_to_async

        await sync_to_async(Attendance.objects.create)(
            clock_time=self._at(self.day, time(10, 0)), event=self.event,
        )
        res = await self._move(self.client_user)
        assert res["success"] is False
        assert "clocked in" in res["message"]

    @pytest.mark.asyncio
    async def test_recapped_event_refused(self):
        from asgiref.sync import sync_to_async

        from recaps.models import CustomRecap, CustomRecapTemplate

        def _recap():
            template = CustomRecapTemplate.objects.create(
                name="T", event_type=self.create_event_type(name="Demo", tenant=self.brand),
                tenant=self.brand, created_by=self.sys,
            )
            CustomRecap.objects.create(
                name="R", event=self.event, tenant=self.brand,
                custom_recap_template=template, created_by=self.sys,
                updated_by=self.sys,
            )

        await sync_to_async(_recap)()
        res = await self._move(self.client_user)
        assert res["success"] is False
        assert "recap" in res["message"]

    @pytest.mark.asyncio
    async def test_past_date_refused(self):
        res = await self._move(self.client_user, day=_ops_today() - timedelta(days=1))
        assert res["success"] is False
        assert "later date" in res["message"]
        assert self.sent == []

    @pytest.mark.asyncio
    async def test_end_before_start_refused(self):
        res = await self._move(self.client_user, start="15:00", end="11:00")
        assert res["success"] is False
        assert "after the start" in res["message"]

    # ── email ──────────────────────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_client_move_emails_cc_list_with_old_and_new(self):
        res = await self._move(self.client_user)
        assert res["success"] is True
        assert len(self.sent) == 1
        env = self.sent[0]
        assert env.to_emails == CC
        old = self._at(self.day, time(10, 0))
        new = self._at(self.new_day, time(11, 0))
        assert env.subject.startswith("Feel Free moved Feel Free demo — Whole Foods Lamar: ")
        assert f"{old.strftime('%a %b %-d')}, 10:00 AM–2:00 PM" in env.subject
        assert f"{new.strftime('%a %b %-d')}, 11:00 AM–3:00 PM" in env.subject
        assert "→" in env.subject
        assert "1 ambassador booked — confirm they can make the new time" in env.html
        assert f"/event/view/{self.event.uuid}" in env.html
        assert "Whole Foods Lamar" in env.html
        assert "lee@brand.test" in env.html
        # Booked BA is counted, never named.
        assert "ba@brand.test" not in env.html

    @pytest.mark.asyncio
    async def test_admin_move_sends_no_email(self):
        res = await self._move(self.admin)
        assert res["success"] is True, res["message"]
        assert self.sent == []
        ev = await self._reload(em.Event, self.event.pk)
        assert ev.start_time == self._at(self.new_day, time(11, 0))

    # ── updateRequest ──────────────────────────────────────────────────

    def _update_input(self, request, day, start, end):
        from strawberry.relay import to_base64

        return {"input": {
            "id": to_base64("Request", request.pk),
            "name": request.name,
            "date": self._at(day, time(0, 0)).isoformat(),
            "startTime": self._at(day, start).isoformat(),
            "endTime": self._at(day, end).isoformat(),
            "address": request.address,
            "notes": None,
            "coordinates": [0.0, 0.0],
            "timezoneId": str(self.pacific.pk),
            "requestTypeId": str(self.req_type.pk),
            "retailerId": str(self.retailer.pk),
            "distributorId": str(self.distributor.pk),
            "details": [],
            "products": [],
        }}

    async def _update(self, user, request, day, start=time(11, 0), end=time(15, 0)):
        result = await self._execute_mutation(
            UPDATE_REQUEST, self._update_input(request, day, start, end),
            self.endpoint, user=user,
        )
        assert result.errors is None, result.errors
        return result.data["updateRequest"]

    @pytest.mark.asyncio
    async def test_client_request_date_change_emails(self):
        from asgiref.sync import sync_to_async

        pending = await sync_to_async(self._make_request)(self.pending_req)
        res = await self._update(self.client_user, pending, self.new_day)
        assert res["success"] is True, res["message"]
        req = await self._reload(em.Request, pending.pk)
        assert req.start_time == self._at(self.new_day, time(11, 0))
        assert len(self.sent) == 1
        env = self.sent[0]
        assert env.to_emails == CC
        assert env.subject.startswith("Feel Free moved FF request: ")
        assert "request edit form" in env.html

    @pytest.mark.asyncio
    async def test_client_request_save_without_date_change_sends_nothing(self):
        from asgiref.sync import sync_to_async

        pending = await sync_to_async(self._make_request)(self.pending_req)
        res = await self._update(
            self.client_user, pending, self.day, start=time(10, 0), end=time(14, 0)
        )
        assert res["success"] is True, res["message"]
        assert self.sent == []

    @pytest.mark.asyncio
    async def test_client_request_date_change_refused_when_event_exists(self):
        res = await self._update(self.client_user, self.request, self.new_day)
        assert res["success"] is False
        assert "Change date" in res["message"]
        req = await self._reload(em.Request, self.request.pk)
        assert req.start_time == self._at(self.day, time(10, 0))
        assert self.sent == []

    @pytest.mark.asyncio
    async def test_admin_request_date_change_sends_nothing(self):
        res = await self._update(self.admin, self.request, self.new_day)
        assert res["success"] is True, res["message"]
        assert self.sent == []

    async def _latest_log(self):
        from asgiref.sync import sync_to_async

        return await sync_to_async(
            lambda: em.RequestActivityLog.objects.filter(request_id=self.request.pk)
            .order_by("-id").first()
        )()
