"""
Tests for the `send_pre_shift_checklists` management command.

The push sender is stubbed (autouse fixture) so nothing hits the Expo
relay — same setup as the activation-reminder / shift-confirmation tests.
"""

import io
from datetime import datetime, timedelta, timezone as _tz
from unittest.mock import patch

import pytest
from django.core.management import call_command

from ambassadors.models import AmbassadorEvent, PushDevice
from ambassadors.tests.base import AmbassadorsGraphQLTestCase


@pytest.mark.django_db(transaction=True)
class TestSendPreShiftChecklists(AmbassadorsGraphQLTestCase):
    """Window, dedup stamp, and quiet-failure posture."""

    @pytest.fixture(autouse=True)
    def setup(self, db):
        self.roles = self.setup_default_roles()
        self.tenant = self.create_tenant(name="Checklist Tenant")
        self.admin = self.create_user(
            username="admin-check",
            email="admin-check@test.com",
            role=self.roles["spark_admin"],
        )
        self.ba_user = self.create_user(
            username="ba-check",
            email="ba-check@test.com",
            role=self.roles["ambassador"],
        )
        self.ambassador = self.create_ambassador(self.ba_user)
        PushDevice.objects.create(
            user=self.ba_user,
            token="ExponentPushToken[check-aaa]",
            platform="ios",
        )
        # Stub the inline sender for the whole test (the AmbassadorEvent
        # post_save signal also pushes; without the stub a fake token would
        # deactivate the test device). Reset before each command run.
        with patch(
            "ambassadors.push._send_push_to_user_sync", return_value=1
        ) as mock_send:
            self.mock_send = mock_send
            yield

    def _shift_starting_in(self, minutes: int, *, approved=True, name="Checklist shift"):
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
        self.mock_send.reset_mock()
        out = io.StringIO()
        call_command("send_pre_shift_checklists", *args, stdout=out)
        return out.getvalue()

    def test_shift_in_60_min_gets_checklist_and_stamp(self):
        ae = self._shift_starting_in(60)
        self._run()
        self.mock_send.assert_called_once()
        _args, kwargs = self.mock_send.call_args
        assert kwargs["title"] == "Pre-shift checklist"
        assert kwargs["data"]["kind"] == "pre_shift_checklist"
        ae.refresh_from_db()
        assert ae.pre_shift_checklist_sent_at is not None

    def test_push_failure_is_quiet_and_left_unstamped(self):
        """Same pager class as the shift-confirmation ×215 incident: a failed
        send must log WARNING (ERROR-level logs become BackendErrorEvent
        alerts) and leave the row unstamped so the next run retries."""
        from django.db.utils import OperationalError

        from digest.models import BackendErrorEvent

        ae = self._shift_starting_in(60)
        self.mock_send.side_effect = OperationalError("the connection is closed")
        out = self._run()
        ae.refresh_from_db()
        assert ae.pre_shift_checklist_sent_at is None  # unstamped → retried
        assert "1 failed" in out
        assert not BackendErrorEvent.objects.filter(
            signature__contains="send_pre_shift_checklists"
        ).exists()
