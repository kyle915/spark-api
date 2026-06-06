"""Tests for the send_open_shift_alerts cron command — pushes eligible BAs
when a shift is dropped (an OpenShift opens), once per OpenShift.

Push delivery is mocked (patch ambassadors.push._send_push_to_user_sync) so
no Expo call is made; we assert WHO would be alerted + the notified_at dedup.
"""

import pytest
from unittest.mock import patch
from asgiref.sync import sync_to_async
from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.utils import timezone

from ambassadors.models import AmbassadorEvent, OpenShift, PushDevice
from ambassadors.tests.base import AmbassadorsGraphQLTestCase

User = get_user_model()


@pytest.mark.django_db
class TestOpenShiftAlerts(AmbassadorsGraphQLTestCase):
    @pytest.fixture(autouse=True)
    def setup(self, db):
        self.roles = self.setup_default_roles()
        self.tenant = self.create_tenant(name="Alert Tenant")

        self.dropper_user = self.create_user(
            username="ba-drop-a", email="da@t.com", role=self.roles["ambassador"]
        )
        self.dropper = self.create_ambassador(self.dropper_user)
        self.eligible_user = self.create_user(
            username="ba-elig", email="e@t.com", role=self.roles["ambassador"]
        )
        self.eligible = self.create_ambassador(self.eligible_user)
        self.stranger_user = self.create_user(
            username="ba-strange", email="st@t.com", role=self.roles["ambassador"]
        )
        self.stranger = self.create_ambassador(self.stranger_user)

        self.event = self.create_event(name="Open Alert Shift", tenant=self.tenant)
        self.history_event = self.create_event(name="Past", tenant=self.tenant)

        # Both eligible BA + stranger have reachable devices.
        PushDevice.objects.create(
            user=self.eligible_user, token="ExponentPushToken[e]", platform="ios"
        )
        PushDevice.objects.create(
            user=self.stranger_user, token="ExponentPushToken[s]", platform="ios"
        )

    def _seed(self):
        # eligible BA has brand history; stranger has none.
        AmbassadorEvent.objects.create(
            ambassador=self.eligible,
            event=self.history_event,
            tenant=self.tenant,
            is_approved=True,
            created_by=self.eligible_user,
        )
        self.event.start_time = timezone.now() + timezone.timedelta(days=2)
        self.event.save(update_fields=["start_time"])
        return OpenShift.objects.create(
            event=self.event, released_by=self.dropper_user
        )

    def test_alerts_eligible_only_and_stamps_notified(self):
        row = self._seed()
        with patch(
            "ambassadors.push._send_push_to_user_sync", return_value=1
        ) as mock_push:
            call_command("send_open_shift_alerts")

        pushed_user_ids = {c.args[0] for c in mock_push.call_args_list}
        assert self.eligible_user.id in pushed_user_ids
        assert self.stranger_user.id not in pushed_user_ids  # no brand history
        assert self.dropper_user.id not in pushed_user_ids  # dropped it
        row.refresh_from_db()
        assert row.notified_at is not None

    def test_dry_run_sends_nothing_and_does_not_stamp(self):
        row = self._seed()
        with patch(
            "ambassadors.push._send_push_to_user_sync", return_value=1
        ) as mock_push:
            call_command("send_open_shift_alerts", "--dry-run")
        mock_push.assert_not_called()
        row.refresh_from_db()
        assert row.notified_at is None

    def test_already_notified_is_skipped(self):
        row = self._seed()
        row.notified_at = timezone.now()
        row.save(update_fields=["notified_at"])
        with patch(
            "ambassadors.push._send_push_to_user_sync", return_value=1
        ) as mock_push:
            call_command("send_open_shift_alerts")
        mock_push.assert_not_called()

    def test_claimed_shift_is_skipped(self):
        row = self._seed()
        row.claimed_at = timezone.now()
        row.claimed_by = self.eligible_user
        row.save(update_fields=["claimed_at", "claimed_by"])
        with patch(
            "ambassadors.push._send_push_to_user_sync", return_value=1
        ) as mock_push:
            call_command("send_open_shift_alerts")
        mock_push.assert_not_called()
