"""Tests for `send_activation_autopilot` — nudge never-signed-in BAs with
an imminent shift (once each) + digest the stragglers to the Ignite team.
"""

import io
from datetime import datetime, timedelta, timezone as _tz
from unittest.mock import patch

import pytest
from django.core.management import call_command

from ambassadors.models import AmbassadorEvent
from ambassadors.tests.base import AmbassadorsGraphQLTestCase


@pytest.mark.django_db(transaction=True)
class TestActivationAutopilot(AmbassadorsGraphQLTestCase):
    @pytest.fixture(autouse=True)
    def setup(self, db):
        self.roles = self.setup_default_roles()
        self.tenant = self.create_tenant(name="Autopilot Tenant")
        self.admin = self.create_user(
            username="admin-ap",
            email="admin-ap@test.com",
            role=self.roles["spark_admin"],
        )
        # Never-signed-in BA (Django leaves last_login NULL until first auth).
        self.dark_user = self.create_user(
            username="dark-ba",
            email="dark-ba@test.com",
            role=self.roles["ambassador"],
        )
        self.dark_amb = self.create_ambassador(self.dark_user)
        # Stub the signal-side inline push + both email paths so nothing
        # leaves the process; patch recipients so the digest has a target.
        with patch(
            "ambassadors.push._send_push_to_user_sync", return_value=1
        ), patch(
            "ambassadors.services.AmbassadorGeneratedPasswordMailer.send"
        ) as mock_welcome, patch(
            "utils.mailer.Mailer.send_now"
        ) as mock_digest, patch(
            "tenants.support._resolve_ignite_recipients",
            return_value=["ops@igniteproductions.co"],
        ):
            self.mock_welcome = mock_welcome
            self.mock_digest = mock_digest
            yield

    def _shift_in_hours(self, hours: float, *, user=None, amb=None, approved=True):
        start = datetime.now(_tz.utc) + timedelta(hours=hours)
        event = self.create_event(
            name="Brickell pop-up",
            tenant=self.tenant,
            date=start,
            start_time=start,
            end_time=start + timedelta(hours=4),
        )
        return AmbassadorEvent.objects.create(
            ambassador=amb or self.dark_amb,
            event=event,
            tenant=self.tenant,
            is_approved=approved,
            created_by=self.admin,
        )

    def _run(self, *args):
        self.mock_welcome.reset_mock()
        self.mock_digest.reset_mock()
        out = io.StringIO()
        call_command("send_activation_autopilot", *args, stdout=out)
        return out.getvalue()

    def test_dark_ba_with_imminent_shift_is_emailed_and_stamped(self):
        ae = self._shift_in_hours(48)
        log = self._run()
        self.mock_welcome.assert_called_once()  # fresh welcome + temp password
        self.mock_digest.assert_called_once()  # admin heads-up
        ae.refresh_from_db()
        assert ae.activation_nudge_stage == 1
        assert "dark BAs   : 1" in log

    def test_signed_in_ba_is_ignored(self):
        from django.contrib.auth import get_user_model

        get_user_model().objects.filter(pk=self.dark_user.pk).update(
            last_login=datetime.now(_tz.utc)
        )
        self._shift_in_hours(48)
        log = self._run()
        self.mock_welcome.assert_not_called()
        self.mock_digest.assert_not_called()
        assert "Nothing to do" in log

    def test_shift_outside_window_is_ignored(self):
        self._shift_in_hours(100)  # beyond the 72h window
        self._run()
        self.mock_welcome.assert_not_called()

    def test_already_stamped_ba_is_not_re_emailed(self):
        self._shift_in_hours(48)
        first = self._run()
        assert self.mock_welcome.call_count == 1
        second = self._run()
        # Second run: still dark + in window (still in the digest), but the
        # stage-1 stamp means no second password reset.
        assert self.mock_welcome.call_count == 0
        assert self.mock_digest.call_count == 1  # digest still fires
        assert "already emailed" in second
        assert "1" in first

    def test_dry_run_sends_nothing_and_does_not_stamp(self):
        ae = self._shift_in_hours(48)
        self._run("--dry-run")
        self.mock_welcome.assert_not_called()
        self.mock_digest.assert_not_called()
        ae.refresh_from_db()
        assert ae.activation_nudge_stage == 0
