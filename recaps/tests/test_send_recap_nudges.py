"""
Tests for the `send_recap_nudges` management command + the
`/internal/cron/recap-nudges` endpoint.

The command is the cron-driven replacement for the dead django-rq recap
nudge. It pushes a single, timely "don't forget your recap" to BAs whose
approved shift ended a few hours ago with NO recap on file (legacy Recap
OR CustomRecap), once per shift (deduped via
AmbassadorEvent.recap_nudge_sent_at).

The push sender is stubbed for the whole test (via an autouse fixture) so
nothing hits the Expo relay — and so the AmbassadorEvent post_save signal
can't make a real send that deactivates the test PushDevice with its fake
token. The mock is reset right before each command run so we only count
the command's own sends, not the signal's incidental ones.
"""

import io
import pytest
from datetime import datetime, timedelta, timezone as _tz
from unittest.mock import patch
from django.core.management import call_command
from django.test import Client, override_settings

from ambassadors.models import AmbassadorEvent, PushDevice
from recaps.models import CustomRecap, CustomRecapTemplate, Recap
from ambassadors.tests.base import AmbassadorsGraphQLTestCase


RECAP_NUDGE_CRON_URL = "/internal/cron/recap-nudges"
VALID_SECRET = "test-cron-secret-value-only-for-tests"


@pytest.mark.django_db(transaction=True)
class TestSendRecapNudges(AmbassadorsGraphQLTestCase):
    """Command-level coverage: window, recap state, dedup."""

    @pytest.fixture(autouse=True)
    def setup(self, db):
        self.roles = self.setup_default_roles()
        self.tenant = self.create_tenant(name="Nudge Cron Tenant")
        self.admin = self.create_user(
            username="admin-nudgecron",
            email="admin-nudgecron@test.com",
            role=self.roles["spark_admin"],
        )
        self.ba_user = self.create_user(
            username="ba-nudgecron",
            email="ba-nudgecron@test.com",
            role=self.roles["ambassador"],
        )
        self.ambassador = self.create_ambassador(self.ba_user)
        PushDevice.objects.create(
            user=self.ba_user,
            token="ExponentPushToken[nudge-aaa]",
            platform="android",
        )
        # See module docstring: stub the inline sender so the post_save
        # signal can't deactivate our fake-token device behind the test's back.
        with patch(
            "ambassadors.push._send_push_to_user_sync", return_value=1
        ) as mock_send:
            self.mock_send = mock_send
            yield

    def _shift_ended_hours_ago(self, hours: int, *, approved=True, name="Ended shift"):
        end = datetime.now(_tz.utc) - timedelta(hours=hours)
        event = self.create_event(
            name=name,
            tenant=self.tenant,
            date=end,
            start_time=end - timedelta(hours=4),
            end_time=end,
        )
        ae = AmbassadorEvent.objects.create(
            ambassador=self.ambassador,
            event=event,
            tenant=self.tenant,
            is_approved=approved,
            created_by=self.admin,
        )
        return event, ae

    def _run(self, *args):
        """Reset the sender mock (drop any signal-side sends from row
        creation), run the command, return its stdout."""
        self.mock_send.reset_mock()
        out = io.StringIO()
        call_command("send_recap_nudges", *args, stdout=out)
        return out.getvalue()

    def test_shift_ended_2h_ago_no_recap_is_nudged_and_stamped(self):
        _event, ae = self._shift_ended_hours_ago(2)
        self._run()
        self.mock_send.assert_called_once()
        _args, kwargs = self.mock_send.call_args
        assert kwargs["title"] == "Recap due"
        assert kwargs["data"]["screen"] == "recap"
        ae.refresh_from_db()
        assert ae.recap_nudge_sent_at is not None

    def test_shift_with_legacy_recap_is_not_nudged(self):
        event, ae = self._shift_ended_hours_ago(2, name="Filed legacy")
        Recap.objects.create(
            name="filed",
            event=event,
            ambassador=self.ambassador,
            created_by=self.admin,
        )
        self._run()
        self.mock_send.assert_not_called()
        ae.refresh_from_db()
        assert ae.recap_nudge_sent_at is None

    def test_shift_with_custom_recap_is_not_nudged(self):
        event, ae = self._shift_ended_hours_ago(2, name="Filed custom")
        event_type = self.create_event_type("Sampling", self.tenant)
        template = CustomRecapTemplate.objects.create(
            name="Tmpl",
            event_type=event_type,
            tenant=self.tenant,
            created_by=self.admin,
        )
        CustomRecap.objects.create(
            name="custom filed",
            event=event,
            tenant=self.tenant,
            custom_recap_template=template,
            created_by=self.admin,
        )
        self._run()
        self.mock_send.assert_not_called()
        ae.refresh_from_db()
        assert ae.recap_nudge_sent_at is None

    def test_already_nudged_shift_is_not_renudged(self):
        _event, ae = self._shift_ended_hours_ago(2)
        AmbassadorEvent.objects.update(
            recap_nudge_sent_at=datetime.now(_tz.utc) - timedelta(hours=1)
        )
        self._run()
        self.mock_send.assert_not_called()

    def test_just_ended_shift_within_grace_is_not_nudged(self):
        # Ended 10 min ago — inside the 1h grace lower-bound.
        _event, ae = self._shift_ended_hours_ago(0)  # end == now
        ae.event.end_time = datetime.now(_tz.utc) - timedelta(minutes=10)
        ae.event.save(update_fields=["end_time"])
        self._run()
        self.mock_send.assert_not_called()

    def test_long_overdue_shift_is_left_to_daily_sweep(self):
        # Ended 48h ago — past the 24h timely-nudge window; the daily
        # recap-reminders sweep covers it instead.
        self._shift_ended_hours_ago(48)
        self._run()
        self.mock_send.assert_not_called()

    def test_second_run_does_not_double_nudge(self):
        self._shift_ended_hours_ago(2)
        self._run()
        first = self.mock_send.call_count
        self._run()
        second = self.mock_send.call_count
        assert first == 1
        assert second == 0

    def test_dry_run_sends_nothing_and_stamps_nothing(self):
        _event, ae = self._shift_ended_hours_ago(2)
        self._run("--dry-run")
        self.mock_send.assert_not_called()
        ae.refresh_from_db()
        assert ae.recap_nudge_sent_at is None


@pytest.mark.django_db
class TestRecapNudgesCronView:
    """Endpoint security boundary + dry-run pass-through."""

    @pytest.fixture(autouse=True)
    def setup(self):
        self.client = Client()

    @override_settings(INTERNAL_CRON_SECRET=VALID_SECRET)
    @patch("digest.cron_views.call_command")
    def test_valid_secret_fires_command(self, mock_call):
        mock_call.return_value = None
        resp = self.client.post(
            RECAP_NUDGE_CRON_URL, HTTP_X_CRON_SECRET=VALID_SECRET
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["ok"] is True
        args, _kwargs = mock_call.call_args
        assert args[0] == "send_recap_nudges"

    @override_settings(INTERNAL_CRON_SECRET=VALID_SECRET)
    @patch("digest.cron_views.call_command")
    def test_bad_secret_returns_401(self, mock_call):
        resp = self.client.post(RECAP_NUDGE_CRON_URL, HTTP_X_CRON_SECRET="nope")
        assert resp.status_code == 401
        mock_call.assert_not_called()

    @override_settings(INTERNAL_CRON_SECRET="")
    @patch("digest.cron_views.call_command")
    def test_unconfigured_secret_fails_closed_503(self, mock_call):
        resp = self.client.post(
            RECAP_NUDGE_CRON_URL, HTTP_X_CRON_SECRET=VALID_SECRET
        )
        assert resp.status_code == 503
        mock_call.assert_not_called()

    @override_settings(INTERNAL_CRON_SECRET=VALID_SECRET)
    @patch("digest.cron_views.call_command")
    def test_dry_run_flag_plumbed(self, mock_call):
        mock_call.return_value = None
        resp = self.client.post(
            f"{RECAP_NUDGE_CRON_URL}?dry_run=true",
            HTTP_X_CRON_SECRET=VALID_SECRET,
        )
        assert resp.status_code == 200
        assert resp.json()["dry_run"] is True
        args, _kwargs = mock_call.call_args
        assert "--dry-run" in args
