"""Deleting, restoring, and editing Torch field marketing plans."""

from datetime import date, datetime, time, timedelta
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pytest
from django.utils import timezone

from ambassadors.models import Attendance
from events import models as em
from events.field_marketing import (
    DELETE_CANCEL_REQUEST,
    DELETE_KEEP_REQUEST,
    DELETE_REMOVE,
    FIELD_MARKETING_SKU_NAMES,
    FieldMarketingError,
    build_board,
    delete_event,
    plan_event,
    restore_event,
    submit_event,
    update_event,
)
from events.field_marketing_mail import (
    TORCH_PLAN_SUBMIT_INTERNAL_EMAILS,
    PlanDeletedInternalMailer,
    notify_plan_deleted,
)
from events.tests.base import EventsGraphQLTestCase
from recaps.models import Recap
from utils.mailer import Mailer

TODAY = timezone.now().astimezone(ZoneInfo("America/New_York")).date()
FUTURE = (TODAY + timedelta(days=20)).isoformat()
PAST = (TODAY - timedelta(days=10)).isoformat()


@pytest.fixture
def sent():
    envelopes = []

    def _capture(self, delay_seconds=None):
        envelopes.append((type(self), self.envelope()))

    with patch.object(Mailer, "send", autospec=True, side_effect=_capture):
        yield envelopes


def _of(sent, mailer_cls):
    return [envelope for cls, envelope in sent if cls is mailer_cls]


@pytest.mark.django_db(transaction=True)
class TestFieldMarketingDelete(EventsGraphQLTestCase):
    @pytest.fixture(autouse=True)
    def setup(self, db):
        self.roles = self.setup_default_roles()
        self.sys = self.get_system_user()
        self.tenant = self.create_tenant(
            name="Torch THC", slug="torch-thc", request_url_name="keee-torch-thc"
        )
        self.other = self.create_tenant(name="Other Brand", slug="other-brand")
        self.marketer = self.create_user(
            username="alec", email="alec@torchdrinks.com", role=self.roles["client"]
        )
        self.create_tenanted_user(self.marketer, self.tenant)
        self.teammate = self.create_user(
            username="octavius", email="octavius@torchdrinks.com", role=self.roles["client"]
        )
        self.create_tenanted_user(self.teammate, self.tenant)
        self.outsider = self.create_user(
            username="other", email="other@example.com", role=self.roles["client"]
        )
        self.create_tenanted_user(self.outsider, self.other)
        self.ba = self.create_user(
            username="ba", email="ba@example.com", role=self.roles["ambassador"]
        )
        self.create_tenanted_user(self.ba, self.tenant)
        self.admin = self.create_user(
            username="ops", email="ops@igniteproductions.co", role=self.roles["spark_admin"]
        )
        line = em.ProductType.objects.create(
            tenant=self.tenant, name="Field marketing", created_by=self.sys
        )
        for name in FIELD_MARKETING_SKU_NAMES:
            em.Product.objects.create(
                tenant=self.tenant, product_type=line, name=name, created_by=self.sys
            )
        em.RequestType.objects.create(
            tenant=self.tenant, name="Event Activation", created_by=self.sys
        )

    def _draft(self, **overrides):
        payload = {
            "market": "miami",
            "activity": "product_seeding",
            "name": "Wynwood case drop",
            "starts_on": FUTURE,
            "sku_names": ["Black Cherry 10mg"],
            "planned_emails": 40,
        }
        payload.update(overrides)
        return plan_event(
            user=self.marketer, payload=payload, submit=False, tenant_id=self.tenant.id
        )

    def _booked(self, starts_on=FUTURE, **overrides):
        payload = {
            "market": "houston",
            "activity": "event_activation",
            "name": "Montrose block party",
            "starts_on": starts_on,
            "address": "1500 Westheimer Rd, Houston, TX 77006",
            "sku_names": ["Black Cherry 10mg"],
            "needs_field_support": True,
            "ambassador_count": 2,
        }
        payload.update(overrides)
        event = plan_event(
            user=self.marketer, payload=payload, submit=True, tenant_id=self.tenant.id
        )
        event.refresh_from_db()
        assert event.request_id
        return event

    def _delete(self, event, user=None):
        return delete_event(
            user=user or self.marketer, event_id=str(event.uuid), tenant_id=self.tenant.id
        )

    def test_deleted_draft_leaves_board_kpis_and_sends_nothing(self, sent):
        keep = self._draft(name="Keep me", planned_emails=10)
        gone = self._draft(name="Delete me", planned_emails=40)
        before = build_board(self.tenant)
        emails = next(k for k in before["kpis"] if k["key"] == "emails")
        assert emails["planned"] == 50
        assert {row["delete_effect"] for row in before["events"]} == {DELETE_REMOVE}

        sent.clear()
        outcome = self._delete(gone)

        assert outcome.effect == DELETE_REMOVE
        gone.refresh_from_db()
        assert gone.deleted_at is not None
        assert gone.deleted_by_id == self.marketer.id
        board = build_board(self.tenant)
        assert [row["id"] for row in board["events"]] == [str(keep.uuid)]
        emails = next(k for k in board["kpis"] if k["key"] == "emails")
        assert emails["planned"] == 10
        assert board["deleted_events"] == []
        assert sent == []

    def test_deleted_plan_cannot_be_submitted_logged_or_deleted_again(self):
        event = self._draft()
        self._delete(event)
        with pytest.raises(FieldMarketingError):
            submit_event(user=self.marketer, event_id=str(event.uuid), tenant_id=self.tenant.id)
        with pytest.raises(FieldMarketingError):
            self._delete(event)

    def test_permissions(self):
        event = self._draft()
        with pytest.raises(FieldMarketingError):
            self._delete(event, user=self.outsider)
        with pytest.raises(FieldMarketingError):
            self._delete(event, user=self.ba)
        # Any Torch teammate can delete, same as submit / log.
        assert self._delete(event, user=self.teammate).effect == DELETE_REMOVE

    def test_admin_can_delete_any_torch_plan(self):
        event = self._draft()
        assert self._delete(event, user=self.admin).effect == DELETE_REMOVE

    def test_future_booked_plan_cancels_request_and_notifies_internal_only(self, sent):
        event = self._booked()
        request = event.request
        board_row = build_board(self.tenant)["events"][0]
        assert board_row["delete_effect"] == DELETE_CANCEL_REQUEST
        assert board_row["request_uuid"] == str(request.uuid)
        sent.clear()

        outcome = self._delete(event)

        assert outcome.effect == DELETE_CANCEL_REQUEST
        request.refresh_from_db()
        assert request.deleted_at is not None
        event.refresh_from_db()
        assert event.delete_cancelled_request is True
        assert em.RequestActivityLog.objects.filter(
            request=request, metadata__deleted=True
        ).exists()
        assert [cls for cls, _ in sent] == [PlanDeletedInternalMailer]
        envelope = _of(sent, PlanDeletedInternalMailer)[0]
        assert set(envelope.to_emails) == set(TORCH_PLAN_SUBMIT_INTERNAL_EMAILS)
        assert "Montrose block party" in envelope.subject
        assert outcome.request_code in envelope.subject
        assert "cancelled" in envelope.subject
        assert not any("torchdrinks.com" in e for e in envelope.to_emails)

    def test_cancelled_request_closes_open_jobs(self):
        event = self._booked()
        shift = self.create_event("Montrose shift", self.tenant, request=event.request)
        title = self.create_job_title("Brand Ambassador", self.tenant)
        job = self.create_job("Montrose", "J-1", "Houston", shift, title, self.tenant)
        self._delete(event)
        job.refresh_from_db()
        assert job.closed is True

    def test_past_booked_plan_keeps_request(self, sent):
        event = self._booked(starts_on=PAST)
        sent.clear()
        outcome = self._delete(event)
        assert outcome.effect == DELETE_KEEP_REQUEST
        event.request.refresh_from_db()
        assert event.request.deleted_at is None
        event.refresh_from_db()
        assert event.delete_cancelled_request is False
        envelope = _of(sent, PlanDeletedInternalMailer)[0]
        assert "kept" in envelope.subject

    def test_recapped_future_request_is_kept(self):
        event = self._booked()
        shift = self.create_event("Montrose shift", self.tenant, request=event.request)
        Recap.objects.create(name="Montrose recap", event=shift, created_by=self.sys)
        assert build_board(self.tenant)["events"][0]["delete_effect"] == DELETE_KEEP_REQUEST
        assert self._delete(event).effect == DELETE_KEEP_REQUEST
        event.request.refresh_from_db()
        assert event.request.deleted_at is None

    def test_clocked_in_request_is_kept(self):
        event = self._booked()
        shift = self.create_event("Montrose shift", self.tenant, request=event.request)
        Attendance.objects.create(clock_time=timezone.now(), event=shift)
        assert self._delete(event).effect == DELETE_KEEP_REQUEST
        event.request.refresh_from_db()
        assert event.request.deleted_at is None

    def test_started_request_today_is_kept(self):
        event = self._booked(starts_on=TODAY.isoformat())
        event.request.start_time = timezone.now() - timedelta(minutes=5)
        event.request.save(update_fields=["start_time"])
        assert self._delete(event).effect == DELETE_KEEP_REQUEST

    def test_plan_only_submitted_delete_notifies_without_request(self, sent):
        event = plan_event(
            user=self.marketer,
            payload={
                "market": "miami",
                "activity": "product_seeding",
                "name": "Seeding run",
                "starts_on": FUTURE,
                "sku_names": ["Black Cherry 10mg"],
            },
            submit=True,
            tenant_id=self.tenant.id,
        )
        sent.clear()
        assert self._delete(event).effect == DELETE_REMOVE
        envelope = _of(sent, PlanDeletedInternalMailer)[0]
        assert "linked" not in envelope.subject

    def test_non_opted_in_brand_gets_no_delete_mail(self, sent):
        event = self._draft()
        event.tenant = self.other
        notify_plan_deleted(event, self.marketer, [], None, False)
        assert sent == []

    def test_admin_restores_and_request_stays_cancelled(self):
        event = self._booked()
        self._delete(event)
        board = build_board(self.tenant, include_deleted=True)
        assert [row["id"] for row in board["deleted_events"]] == [str(event.uuid)]
        assert board["deleted_events"][0]["delete_cancelled_request"] is True

        with pytest.raises(FieldMarketingError):
            restore_event(user=self.teammate, event_id=str(event.uuid), tenant_id=self.tenant.id)

        restored = restore_event(user=self.admin, event_id=str(event.uuid), tenant_id=self.tenant.id)
        assert restored.deleted_at is None
        event.request.refresh_from_db()
        assert event.request.deleted_at is not None
        row = build_board(self.tenant)["events"][0]
        assert row["request_cancelled"] is True
        assert row["can_book"] is True

        # Booking again makes a fresh request.
        old_request_id = event.request_id
        rebooked = submit_event(user=self.marketer, event_id=str(event.uuid), tenant_id=self.tenant.id)
        rebooked.refresh_from_db()
        assert rebooked.request_id != old_request_id
        assert rebooked.request.deleted_at is None

    def test_deleter_can_undo_within_window_only(self):
        event = self._draft()
        self._delete(event)
        restore_event(user=self.marketer, event_id=str(event.uuid), tenant_id=self.tenant.id)

        self._delete(event)
        em.FieldMarketingEvent.objects.filter(id=event.id).update(
            deleted_at=timezone.now() - timedelta(hours=1)
        )
        with pytest.raises(FieldMarketingError):
            restore_event(user=self.marketer, event_id=str(event.uuid), tenant_id=self.tenant.id)


@pytest.mark.django_db(transaction=True)
class TestFieldMarketingEditAndTimes(EventsGraphQLTestCase):
    @pytest.fixture(autouse=True)
    def setup(self, db):
        self.roles = self.setup_default_roles()
        self.sys = self.get_system_user()
        self.tenant = self.create_tenant(
            name="Torch THC", slug="torch-thc", request_url_name="keee-torch-thc"
        )
        self.marketer = self.create_user(
            username="alec", email="alec@torchdrinks.com", role=self.roles["client"]
        )
        self.create_tenanted_user(self.marketer, self.tenant)
        line = em.ProductType.objects.create(
            tenant=self.tenant, name="Field marketing", created_by=self.sys
        )
        for name in FIELD_MARKETING_SKU_NAMES:
            em.Product.objects.create(
                tenant=self.tenant, product_type=line, name=name, created_by=self.sys
            )
        em.RequestType.objects.create(
            tenant=self.tenant, name="Event Activation", created_by=self.sys
        )
        self.central_dst = em.TimeZone.objects.create(name="Central", code="CDT", offset=-300)
        em.TimeZone.objects.create(name="Central", code="CST", offset=-360)

    def _payload(self, **overrides):
        payload = {
            "market": "houston",
            "activity": "event_activation",
            "name": "Montrose block party",
            "starts_on": "2026-10-24",
            "address": "1500 Westheimer Rd, Houston, TX 77006",
            "sku_names": ["Black Cherry 10mg"],
            "needs_field_support": True,
            "ambassador_count": 3,
            "start_time": "22:00",
            "end_time": "01:30",
        }
        payload.update(overrides)
        return payload

    def test_booked_request_gets_market_local_times_timezone_and_ba_count(self):
        event = plan_event(
            user=self.marketer, payload=self._payload(), submit=True, tenant_id=self.tenant.id
        )
        event.refresh_from_db()
        assert event.start_time == time(22, 0)
        assert event.end_time == time(1, 30)
        assert event.support_times == "10:00 PM – 1:30 AM"
        request = event.request
        houston = ZoneInfo("America/Chicago")
        assert request.start_time == datetime(2026, 10, 24, 22, 0, tzinfo=houston)
        # Overnight: ends the next morning.
        assert request.end_time == datetime(2026, 10, 25, 1, 30, tzinfo=houston)
        assert request.date == request.start_time
        assert request.timezone_id == self.central_dst.id
        assert "BA count: 3" in request.notes
        assert "Times: 10:00 PM – 1:30 AM" in request.notes
        assert request.load_in_time == "10:00 PM – 1:30 AM"

    def test_end_without_start_is_rejected(self):
        with pytest.raises(FieldMarketingError):
            plan_event(
                user=self.marketer,
                payload=self._payload(start_time="", end_time="20:00"),
                submit=False,
                tenant_id=self.tenant.id,
            )

    def test_times_dropped_for_unstaffed_tactics(self):
        event = plan_event(
            user=self.marketer,
            payload=self._payload(activity="product_seeding", start_time="10:00", end_time=""),
            submit=False,
            tenant_id=self.tenant.id,
        )
        assert event.start_time is None
        assert event.support_times == ""

    def test_edit_draft(self):
        event = plan_event(
            user=self.marketer, payload=self._payload(), submit=False, tenant_id=self.tenant.id
        )
        updated = update_event(
            user=self.marketer,
            event_id=str(event.uuid),
            payload=self._payload(name="Montrose after-party", starts_on="2026-10-25"),
            tenant_id=self.tenant.id,
        )
        assert updated.name == "Montrose after-party"
        assert updated.starts_on == date(2026, 10, 25)
        assert updated.status == em.FieldMarketingEvent.STATUS_PLANNED
        assert updated.request_id is None

    def test_booked_plan_edits_on_the_request(self):
        event = plan_event(
            user=self.marketer, payload=self._payload(), submit=True, tenant_id=self.tenant.id
        )
        with pytest.raises(FieldMarketingError, match="Change it on the request"):
            update_event(
                user=self.marketer,
                event_id=str(event.uuid),
                payload=self._payload(name="Renamed"),
                tenant_id=self.tenant.id,
            )


DELETE_MUTATION = """
mutation Delete($input: DeleteFieldMarketingInput!) {
  deleteFieldMarketing(input: $input) {
    success message requestCode requestCancelled canUndo
    event { id deletedAt deleteEffect }
  }
}
"""

RESTORE_MUTATION = """
mutation Restore($input: RestoreFieldMarketingInput!) {
  restoreFieldMarketing(input: $input) { success message event { id deletedAt } }
}
"""

BOARD_QUERY = """
query Board($tenantId: ID, $includeDeleted: Boolean!) {
  fieldMarketing(tenantId: $tenantId, includeDeleted: $includeDeleted) {
    canRestore
    events { id deleteEffect canBook requestUuid }
    deletedEvents { id deletedByName deleteCancelledRequest }
  }
}
"""


@pytest.mark.django_db(transaction=True)
class TestFieldMarketingDeleteGraphQL(EventsGraphQLTestCase):
    @pytest.fixture(autouse=True)
    def setup(self, db):
        from config.schema_client import schema_clients

        self.schema = schema_clients
        self.endpoint_path = "/api/v1/graphql/clients"
        self.roles = self.setup_default_roles()
        self.tenant = self.create_tenant(
            name="Torch THC", slug="torch-thc", request_url_name="keee-torch-thc"
        )
        self.marketer = self.create_user(
            username="alec",
            email="alec@torchdrinks.com",
            role=self.roles["client"],
            first_name="Alec",
            last_name="Aparicio",
        )
        self.create_tenanted_user(self.marketer, self.tenant)
        self.admin = self.create_user(
            username="ops",
            email="ops@igniteproductions.co",
            role=self.roles["spark_admin"],
            is_staff=True,
        )
        self.plan = plan_event(
            user=self.marketer,
            payload={
                "market": "miami",
                "activity": "sales_support",
                "support_type": "retail_visit",
                "name": "Publix walk",
                "starts_on": FUTURE,
            },
            submit=False,
            tenant_id=self.tenant.id,
        )

    @pytest.mark.asyncio
    async def test_delete_undo_and_admin_board(self):
        variables = {"input": {"eventId": str(self.plan.uuid), "tenantId": str(self.tenant.id)}}
        deleted = await self._execute_mutation(DELETE_MUTATION, variables, user=self.marketer)
        assert deleted.errors is None, deleted.errors
        payload = deleted.data["deleteFieldMarketing"]
        assert payload["success"] is True
        assert payload["canUndo"] is True
        assert payload["requestCancelled"] is False
        assert payload["event"]["deletedAt"]

        client_board = await self._execute_mutation(
            BOARD_QUERY,
            {"tenantId": str(self.tenant.id), "includeDeleted": True},
            user=self.marketer,
        )
        assert client_board.errors is None, client_board.errors
        board = client_board.data["fieldMarketing"]
        assert board["canRestore"] is False
        assert board["events"] == []
        assert board["deletedEvents"] == []

        admin_board = await self._execute_mutation(
            BOARD_QUERY,
            {"tenantId": str(self.tenant.id), "includeDeleted": True},
            user=self.admin,
        )
        assert admin_board.errors is None, admin_board.errors
        board = admin_board.data["fieldMarketing"]
        assert board["canRestore"] is True
        assert board["deletedEvents"][0]["deletedByName"] == "Alec Aparicio"

        restored = await self._execute_mutation(RESTORE_MUTATION, variables, user=self.marketer)
        assert restored.errors is None, restored.errors
        assert restored.data["restoreFieldMarketing"]["event"]["deletedAt"] is None
