"""
Tests for the `send_activation_reminders` management command + the
`/internal/cron/activation-reminders` endpoint.

The command is the cron-driven replacement for the dead django-rq
activation reminder. It pushes "your shift starts soon" to BAs with an
approved shift starting in the near-future window, once per shift
(deduped via AmbassadorEvent.activation_reminder_sent_at).

The push sender is stubbed for the whole test (via an autouse fixture)
so nothing hits the Expo relay — and so the AmbassadorEvent post_save
signal can't make a real send that deactivates the test PushDevice with
its fake token. The mock is reset right before each command run so we
only count the command's own sends, not the signal's incidental ones.
"""

import io
import pytest
from datetime import datetime, timedelta, timezone as _tz
from unittest.mock import patch
from django.core.management import call_command
from django.test import Client, override_settings

from ambassadors.models import AmbassadorEvent, PushDevice
from ambassadors.tests.base import AmbassadorsGraphQLTestCase


ACTIVATION_CRON_URL = "/internal/cron/activation-reminders"
VALID_SECRET = "test-cron-secret-value-only-for-tests"


@pytest.mark.django_db(transaction=True)
class TestSendActivationReminders(AmbassadorsGraphQLTestCase):
    """Command-level coverage: window, reachability, dedup."""

    @pytest.fixture(autouse=True)
    def setup(self, db):
        self.roles = self.setup_default_roles()
        self.tenant = self.create_tenant(name="Activation Tenant")
        self.admin = self.create_user(
            username="admin-act",
            email="admin-act@test.com",
            role=self.roles["spark_admin"],
        )
        self.ba_user = self.create_user(
            username="ba-act",
            email="ba-act@test.com",
            role=self.roles["ambassador"],
        )
        self.ambassador = self.create_ambassador(self.ba_user)
        # A registered, active device so the BA is reachable.
        PushDevice.objects.create(
            user=self.ba_user,
            token="ExponentPushToken[act-aaa]",
            platform="ios",
        )
        # Stub the inline sender for the whole test. The AmbassadorEvent
        # post_save signal also calls it (shift-offer / pre-shift checklist)
        # via its inline fallback when Redis is down; without this stub that
        # would hit the real Expo relay and — with a fake token — deactivate
        # our test device, making the BA "unreachable" for the command.
        with patch(
            "ambassadors.push._send_push_to_user_sync", return_value=1
        ) as mock_send:
            self.mock_send = mock_send
            yield

    def _shift_starting_in(self, minutes: int, *, approved=True, name="Soon shift"):
        start = datetime.now(_tz.utc) + timedelta(minutes=minutes)
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
        """Reset the sender mock (drop any signal-side sends from row
        creation), run the command, return its stdout."""
        self.mock_send.reset_mock()
        out = io.StringIO()
        call_command("send_activation_reminders", *args, stdout=out)
        return out.getvalue()

    def test_shift_in_15_min_is_reminded_and_stamped(self):
        ae = self._shift_starting_in(15)
        self._run()
        self.mock_send.assert_called_once()
        # Reminder routes to the Shifts tab (no ambassadorEventUuid → no
        # re-opening the offer screen).
        _args, kwargs = self.mock_send.call_args
        assert kwargs["title"] == "Your shift starts soon"
        assert kwargs["data"]["screen"] == "shifts"
        assert "ambassadorEventUuid" not in kwargs["data"]
        ae.refresh_from_db()
        assert ae.activation_reminder_sent_at is not None

    def test_shift_in_3_hours_is_not_reminded(self):
        ae = self._shift_starting_in(180)
        self._run()
        self.mock_send.assert_not_called()
        ae.refresh_from_db()
        assert ae.activation_reminder_sent_at is None

    def test_already_reminded_shift_is_not_resent(self):
        self._shift_starting_in(15)
        AmbassadorEvent.objects.update(
            activation_reminder_sent_at=datetime.now(_tz.utc) - timedelta(minutes=5)
        )
        self._run()
        self.mock_send.assert_not_called()

    def test_second_run_does_not_double_send(self):
        self._shift_starting_in(15)
        self._run()
        first = self.mock_send.call_count
        self._run()
        second = self.mock_send.call_count
        # First run sent once + stamped; second run sees the stamp → 0 sends.
        assert first == 1
        assert second == 0

    def test_unapproved_shift_is_not_reminded(self):
        self._shift_starting_in(15, approved=False)
        self._run()
        self.mock_send.assert_not_called()

    def test_already_started_shift_is_not_reminded(self):
        # start_time in the past → "starts soon" no longer applies.
        self._shift_starting_in(-5)
        self._run()
        self.mock_send.assert_not_called()

    def test_unreachable_ba_is_not_stamped(self):
        # No active device → skip without stamping, so a later run (after
        # the BA registers) still catches them.
        ae = self._shift_starting_in(15)
        PushDevice.objects.filter(user=self.ba_user).update(is_active=False)
        self._run()
        self.mock_send.assert_not_called()
        ae.refresh_from_db()
        assert ae.activation_reminder_sent_at is None

    def test_dry_run_sends_nothing_and_stamps_nothing(self):
        ae = self._shift_starting_in(15)
        self._run("--dry-run")
        self.mock_send.assert_not_called()
        ae.refresh_from_db()
        assert ae.activation_reminder_sent_at is None


@pytest.mark.django_db
class TestActivationRemindersCronView:
    """Endpoint security boundary + dry-run pass-through."""

    @pytest.fixture(autouse=True)
    def setup(self):
        self.client = Client()

    @override_settings(INTERNAL_CRON_SECRET=VALID_SECRET)
    @patch("digest.cron_views.call_command")
    def test_valid_secret_fires_command(self, mock_call):
        mock_call.return_value = None
        resp = self.client.post(
            ACTIVATION_CRON_URL, HTTP_X_CRON_SECRET=VALID_SECRET
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["ok"] is True
        args, _kwargs = mock_call.call_args
        assert args[0] == "send_activation_reminders"

    @override_settings(INTERNAL_CRON_SECRET=VALID_SECRET)
    @patch("digest.cron_views.call_command")
    def test_bad_secret_returns_401(self, mock_call):
        resp = self.client.post(ACTIVATION_CRON_URL, HTTP_X_CRON_SECRET="nope")
        assert resp.status_code == 401
        mock_call.assert_not_called()

    @override_settings(INTERNAL_CRON_SECRET="")
    @patch("digest.cron_views.call_command")
    def test_unconfigured_secret_fails_closed_503(self, mock_call):
        resp = self.client.post(
            ACTIVATION_CRON_URL, HTTP_X_CRON_SECRET=VALID_SECRET
        )
        assert resp.status_code == 503
        mock_call.assert_not_called()

    @override_settings(INTERNAL_CRON_SECRET=VALID_SECRET)
    @patch("digest.cron_views.call_command")
    def test_dry_run_flag_plumbed(self, mock_call):
        mock_call.return_value = None
        resp = self.client.post(
            f"{ACTIVATION_CRON_URL}?dry_run=true",
            HTTP_X_CRON_SECRET=VALID_SECRET,
        )
        assert resp.status_code == 200
        assert resp.json()["dry_run"] is True
        args, _kwargs = mock_call.call_args
        assert "--dry-run" in args
