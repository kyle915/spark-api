"""
Tests for the day-before shift confirmation flow:

- `send_shift_confirmations` management command
    Phase A: T-24h "Confirm your shift" push (window, dedup via
    confirmation_requested_at, reachability, skip-if-confirmed, dry-run)
    Phase B: morning-of unconfirmed alert email to the Ignite team
    (window, grace period, attendance exclusion, dedup via
    unconfirmed_alerted_at, dry-run)
- `confirmShift` mobile mutation (stamps confirmed_at, idempotent,
  self-scoped)
- `_auto_confirm_on_attendance` (arrive/clock-in flips the stamp,
  clock-out doesn't, existing stamp preserved)
- `/internal/cron/shift-confirmations` endpoint security boundary

The push sender is stubbed for the whole test (autouse fixture) so
nothing hits the Expo relay — same setup as the activation-reminder
tests. Alert emails land in django's locmem outbox; the Ignite
recipient resolution is patched to a fixed address.
"""

import io
from datetime import datetime, timedelta, timezone as _tz
from unittest.mock import patch

import pytest
from django.core import mail
from django.core.management import call_command
from django.test import Client, override_settings

from ambassadors.models import AmbassadorEvent, Attendance, PushDevice, Source
from ambassadors.mutations import _auto_confirm_on_attendance
from ambassadors.tests.base import AmbassadorsGraphQLTestCase


CONFIRMATIONS_CRON_URL = "/internal/cron/shift-confirmations"
VALID_SECRET = "test-cron-secret-value-only-for-tests"

CONFIRM_MUTATION = """
mutation Confirm($input: ConfirmShiftInput!) {
  confirmShift(input: $input) {
    success
    message
    confirmedAt
  }
}
"""


@pytest.mark.django_db(transaction=True)
class TestSendShiftConfirmations(AmbassadorsGraphQLTestCase):
    """Command-level coverage for both phases."""

    @pytest.fixture(autouse=True)
    def setup(self, db):
        self.roles = self.setup_default_roles()
        self.tenant = self.create_tenant(name="Confirm Tenant")
        self.admin = self.create_user(
            username="admin-conf",
            email="admin-conf@test.com",
            role=self.roles["spark_admin"],
        )
        self.ba_user = self.create_user(
            username="ba-conf",
            email="ba-conf@test.com",
            role=self.roles["ambassador"],
        )
        self.ambassador = self.create_ambassador(self.ba_user)
        PushDevice.objects.create(
            user=self.ba_user,
            token="ExponentPushToken[conf-aaa]",
            platform="ios",
        )
        # Stub the inline sender for the whole test (the AmbassadorEvent
        # post_save signal also pushes; without the stub a fake token would
        # deactivate the test device). Reset before each command run.
        with patch(
            "ambassadors.push._send_push_to_user_sync", return_value=1
        ) as mock_send, patch(
            "tenants.support._resolve_ignite_recipients",
            return_value=["ops@ignite.test"],
        ):
            self.mock_send = mock_send
            yield

    def _shift_starting_in(self, hours: float, *, approved=True, name="Conf shift"):
        start = datetime.now(_tz.utc) + timedelta(hours=hours)
        event = self.create_event(
            name=name,
            tenant=self.tenant,
            date=start,
            start_time=start,
            end_time=start + timedelta(hours=4),
        )
        return AmbassadorEvent.objects.create(
            ambassador=self.ambassador,
            event=event,
            tenant=self.tenant,
            is_approved=approved,
            created_by=self.admin,
        )

    def _run(self, *args):
        self.mock_send.reset_mock()
        mail.outbox.clear()
        out = io.StringIO()
        call_command("send_shift_confirmations", *args, stdout=out)
        return out.getvalue()

    # ---------- Phase A: confirmation request ----------

    def test_shift_in_20h_is_asked_and_stamped(self):
        ae = self._shift_starting_in(20)
        self._run()
        self.mock_send.assert_called_once()
        _args, kwargs = self.mock_send.call_args
        assert kwargs["title"] == "Confirm your shift"
        assert kwargs["data"]["kind"] == "shift_confirmation"
        # Precise deep-link payload for the mobile tap handler.
        assert kwargs["data"]["ambassadorEventUuid"] == str(ae.uuid)
        assert "You're booked at Conf shift" in kwargs["body"]
        ae.refresh_from_db()
        assert ae.confirmation_requested_at is not None

    def test_shift_in_40h_is_not_asked_yet(self):
        ae = self._shift_starting_in(40)
        self._run()
        self.mock_send.assert_not_called()
        ae.refresh_from_db()
        assert ae.confirmation_requested_at is None

    def test_second_run_does_not_re_ask(self):
        self._shift_starting_in(20)
        self._run()
        assert self.mock_send.call_count == 1
        self._run()
        assert self.mock_send.call_count == 0

    def test_already_confirmed_shift_is_not_asked(self):
        ae = self._shift_starting_in(20)
        AmbassadorEvent.objects.filter(id=ae.id).update(
            confirmed_at=datetime.now(_tz.utc)
        )
        self._run()
        self.mock_send.assert_not_called()

    def test_unapproved_shift_is_not_asked(self):
        self._shift_starting_in(20, approved=False)
        self._run()
        self.mock_send.assert_not_called()

    def test_unreachable_ba_is_not_stamped(self):
        ae = self._shift_starting_in(20)
        PushDevice.objects.filter(user=self.ba_user).update(is_active=False)
        self._run()
        self.mock_send.assert_not_called()
        ae.refresh_from_db()
        assert ae.confirmation_requested_at is None

    def test_dry_run_sends_and_stamps_nothing(self):
        ae = self._shift_starting_in(20)
        self._run("--dry-run")
        self.mock_send.assert_not_called()
        ae.refresh_from_db()
        assert ae.confirmation_requested_at is None

    # ---------- Phase B: morning-of unconfirmed alert ----------

    def test_unconfirmed_shift_starting_soon_alerts_ignite(self):
        ae = self._shift_starting_in(2)
        AmbassadorEvent.objects.filter(id=ae.id).update(
            confirmation_requested_at=datetime.now(_tz.utc) - timedelta(hours=20)
        )
        self._run()
        assert len(mail.outbox) == 1
        msg = mail.outbox[0]
        assert msg.to == ["ops@ignite.test"]
        assert "unconfirmed BA" in msg.subject
        assert "ba-conf@test.com" in msg.body
        assert "no reply to the confirmation push" in msg.body
        ae.refresh_from_db()
        assert ae.unconfirmed_alerted_at is not None
        # Dedup: second run sends no second email.
        self._run()
        assert len(mail.outbox) == 0

    def test_never_reached_ba_alerts_immediately(self):
        # No confirmation_requested_at at all (e.g. no push device) — the
        # alert must still fire and say the BA was never reached.
        ae = self._shift_starting_in(2)
        PushDevice.objects.filter(user=self.ba_user).update(is_active=False)
        self._run()
        assert len(mail.outbox) == 1
        assert "was never reached" in mail.outbox[0].body
        ae.refresh_from_db()
        assert ae.unconfirmed_alerted_at is not None

    def test_just_asked_row_gets_grace_before_alert(self):
        # Asked 5 minutes ago (e.g. a late booking Phase A just caught) —
        # give the BA a beat to answer before paging admins.
        ae = self._shift_starting_in(2)
        AmbassadorEvent.objects.filter(id=ae.id).update(
            confirmation_requested_at=datetime.now(_tz.utc) - timedelta(minutes=5)
        )
        self._run()
        assert len(mail.outbox) == 0
        ae.refresh_from_db()
        assert ae.unconfirmed_alerted_at is None

    def test_ba_already_on_site_is_not_alerted(self):
        ae = self._shift_starting_in(2)
        AmbassadorEvent.objects.filter(id=ae.id).update(
            confirmation_requested_at=datetime.now(_tz.utc) - timedelta(hours=20)
        )
        source, _ = Source.objects.get_or_create(name="arrived")
        Attendance.objects.create(
            clock_time=datetime.now(_tz.utc),
            coordinates=None,
            ambassador=self.ambassador,
            job=None,
            event=ae.event,
            source=source,
        )
        self._run()
        assert len(mail.outbox) == 0

    def test_confirmed_shift_is_not_alerted(self):
        ae = self._shift_starting_in(2)
        AmbassadorEvent.objects.filter(id=ae.id).update(
            confirmed_at=datetime.now(_tz.utc)
        )
        self._run()
        assert len(mail.outbox) == 0

    def test_dry_run_alert_sends_no_email_and_stamps_nothing(self):
        ae = self._shift_starting_in(2)
        AmbassadorEvent.objects.filter(id=ae.id).update(
            confirmation_requested_at=datetime.now(_tz.utc) - timedelta(hours=20)
        )
        self._run("--dry-run")
        assert len(mail.outbox) == 0
        ae.refresh_from_db()
        assert ae.unconfirmed_alerted_at is None

    # ---------- auto-confirm on attendance ----------

    def test_arrive_and_clock_in_auto_confirm(self):
        ae = self._shift_starting_in(2)
        _auto_confirm_on_attendance(ae, "arrived")
        ae.refresh_from_db()
        assert ae.confirmed_at is not None

        ae2 = self._shift_starting_in(3, name="Conf shift 2")
        _auto_confirm_on_attendance(ae2, "clock_in")
        ae2.refresh_from_db()
        assert ae2.confirmed_at is not None

    def test_clock_out_does_not_auto_confirm(self):
        ae = self._shift_starting_in(2)
        _auto_confirm_on_attendance(ae, "clock_out")
        ae.refresh_from_db()
        assert ae.confirmed_at is None

    def test_existing_confirmation_stamp_is_preserved(self):
        ae = self._shift_starting_in(2)
        original = datetime.now(_tz.utc) - timedelta(hours=5)
        AmbassadorEvent.objects.filter(id=ae.id).update(confirmed_at=original)
        ae.refresh_from_db()
        _auto_confirm_on_attendance(ae, "clock_in")
        ae.refresh_from_db()
        assert ae.confirmed_at == original


@pytest.mark.django_db(transaction=True)
class TestConfirmShiftMutation(AmbassadorsGraphQLTestCase):
    """confirmShift on the mobile schema — stamp, idempotency, self-scope."""

    @pytest.fixture(autouse=True)
    def setup(self, db):
        from config.schema_mobile import schema_mobile

        self.roles = self.setup_default_roles()
        self.schema = schema_mobile
        self.endpoint_path = "/api/v1/graphql/mobile"
        self.tenant = self.create_tenant(name="ConfirmMut Tenant")
        self.ba_user = self.create_user(
            username="ba-cm",
            email="ba-cm@test.com",
            role=self.roles["ambassador"],
        )
        self.ambassador = self.create_ambassador(self.ba_user)
        self.other_user = self.create_user(
            username="ba-cm-other",
            email="ba-cm-other@test.com",
            role=self.roles["ambassador"],
        )
        self.other_ambassador = self.create_ambassador(self.other_user)
        self.event = self.create_event(name="ConfirmMut Shift", tenant=self.tenant)
        # The post_save push signal would hit the relay with fake tokens.
        with patch("ambassadors.push._send_push_to_user_sync", return_value=1):
            yield

    def _book(self, ambassador) -> AmbassadorEvent:
        return AmbassadorEvent.objects.create(
            ambassador=ambassador,
            event=self.event,
            tenant=self.tenant,
            is_approved=True,
            created_by=self.ba_user,
        )

    @pytest.mark.asyncio
    async def test_confirm_own_shift_stamps(self):
        from asgiref.sync import sync_to_async

        ae = await sync_to_async(self._book)(self.ambassador)
        result = await self._execute_mutation(
            CONFIRM_MUTATION,
            {"input": {"ambassadorEventUuid": str(ae.uuid)}},
            user=self.ba_user,
        )
        assert result.errors is None
        payload = result.data["confirmShift"]
        assert payload["success"] is True
        assert payload["confirmedAt"] is not None
        await sync_to_async(ae.refresh_from_db)()
        assert ae.confirmed_at is not None

    @pytest.mark.asyncio
    async def test_confirm_is_idempotent(self):
        from asgiref.sync import sync_to_async

        ae = await sync_to_async(self._book)(self.ambassador)
        first = await self._execute_mutation(
            CONFIRM_MUTATION,
            {"input": {"ambassadorEventUuid": str(ae.uuid)}},
            user=self.ba_user,
        )
        stamp1 = first.data["confirmShift"]["confirmedAt"]
        second = await self._execute_mutation(
            CONFIRM_MUTATION,
            {"input": {"ambassadorEventUuid": str(ae.uuid)}},
            user=self.ba_user,
        )
        assert second.data["confirmShift"]["success"] is True
        assert second.data["confirmShift"]["confirmedAt"] == stamp1

    @pytest.mark.asyncio
    async def test_cannot_confirm_another_bas_shift(self):
        from asgiref.sync import sync_to_async

        ae = await sync_to_async(self._book)(self.other_ambassador)
        result = await self._execute_mutation(
            CONFIRM_MUTATION,
            {"input": {"ambassadorEventUuid": str(ae.uuid)}},
            user=self.ba_user,
        )
        payload = result.data["confirmShift"]
        assert payload["success"] is False
        await sync_to_async(ae.refresh_from_db)()
        assert ae.confirmed_at is None

    @pytest.mark.asyncio
    async def test_confirm_by_event_uuid_resolves_own_row(self):
        from asgiref.sync import sync_to_async

        ae = await sync_to_async(self._book)(self.ambassador)
        result = await self._execute_mutation(
            CONFIRM_MUTATION,
            {"input": {"eventUuid": str(self.event.uuid)}},
            user=self.ba_user,
        )
        assert result.data["confirmShift"]["success"] is True
        await sync_to_async(ae.refresh_from_db)()
        assert ae.confirmed_at is not None


@pytest.mark.django_db
class TestShiftConfirmationsCronView:
    """Endpoint security boundary + param pass-through."""

    @pytest.fixture(autouse=True)
    def setup(self):
        self.client = Client()

    @override_settings(INTERNAL_CRON_SECRET=VALID_SECRET)
    @patch("digest.cron_views.call_command")
    def test_valid_secret_fires_command(self, mock_call):
        mock_call.return_value = None
        resp = self.client.post(
            CONFIRMATIONS_CRON_URL, HTTP_X_CRON_SECRET=VALID_SECRET
        )
        assert resp.status_code == 200
        assert resp.json()["ok"] is True
        args, _kwargs = mock_call.call_args
        assert args[0] == "send_shift_confirmations"

    @override_settings(INTERNAL_CRON_SECRET=VALID_SECRET)
    @patch("digest.cron_views.call_command")
    def test_bad_secret_returns_401(self, mock_call):
        resp = self.client.post(
            CONFIRMATIONS_CRON_URL, HTTP_X_CRON_SECRET="nope"
        )
        assert resp.status_code == 401
        mock_call.assert_not_called()

    @override_settings(INTERNAL_CRON_SECRET="")
    @patch("digest.cron_views.call_command")
    def test_unconfigured_secret_fails_closed_503(self, mock_call):
        resp = self.client.post(
            CONFIRMATIONS_CRON_URL, HTTP_X_CRON_SECRET=VALID_SECRET
        )
        assert resp.status_code == 503
        mock_call.assert_not_called()

    @override_settings(INTERNAL_CRON_SECRET=VALID_SECRET)
    @patch("digest.cron_views.call_command")
    def test_dry_run_and_hours_plumbed(self, mock_call):
        mock_call.return_value = None
        resp = self.client.post(
            f"{CONFIRMATIONS_CRON_URL}?dry_run=true&lead_hours=30&alert_hours=6",
            HTTP_X_CRON_SECRET=VALID_SECRET,
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["dry_run"] is True
        assert body["lead_hours"] == 30
        assert body["alert_hours"] == 6
        args, _kwargs = mock_call.call_args
        assert "--dry-run" in args
        assert "30" in args
        assert "6" in args
