"""No-show cascade idempotency + fail-open delivery audit."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone as _tz
from unittest.mock import patch

import pytest

from ambassadors.models import AmbassadorEvent
from ambassadors.tests.base import AmbassadorsGraphQLTestCase
from staffing.cascade import evaluate_noshow_cascades, trigger_noshow_cascade_for_event
from staffing.models import NoShowCascadeAlert


@pytest.mark.django_db(transaction=True)
class TestNoShowCascade(AmbassadorsGraphQLTestCase):
    @pytest.fixture(autouse=True)
    def setup(self, db):
        self.roles = self.setup_default_roles()
        self.system_user = self.get_system_user()
        self.tenant = self.create_tenant(name="Cascade Brand")
        self.now = datetime.now(_tz.utc)
        self.event = self.create_event(
            name="Sprouts Cascade Demo",
            tenant=self.tenant,
            date=self.now - timedelta(minutes=20),
            start_time=self.now - timedelta(minutes=20),
            end_time=self.now + timedelta(hours=3),
        )
        ba_user = self.create_user(
            username="cascade-ba",
            email="cascade-ba@example.com",
            role=self.roles["ambassador"],
        )
        self.ba = self.create_ambassador(user=ba_user, phone="5555550100")
        self.create_tenanted_user(ba_user, self.tenant)
        self.ae = AmbassadorEvent.objects.create(
            ambassador=self.ba,
            event=self.event,
            tenant=self.tenant,
            is_approved=True,
            created_by=self.system_user,
            updated_by=self.system_user,
        )

    def test_trigger_is_idempotent(self):
        with patch("staffing.cascade.compute_smart_ba_matches", return_value=[]):
            first, created1, _ = trigger_noshow_cascade_for_event(
                event=self.event,
                ambassador_event=self.ae,
                trigger_reason=NoShowCascadeAlert.REASON_MISSING_CHECKIN_15M,
                now=self.now,
            )
            second, created2, msg = trigger_noshow_cascade_for_event(
                event=self.event,
                ambassador_event=self.ae,
                trigger_reason=NoShowCascadeAlert.REASON_MISSING_CHECKIN_15M,
                now=self.now,
            )
        assert created1 is True
        assert first is not None
        assert created2 is False
        assert second.id == first.id
        assert "already" in msg.lower()
        assert NoShowCascadeAlert.objects.filter(event=self.event).count() == 1

    def test_dry_run_and_fail_open_audit(self):
        fake_match = {
            "ba_id": "Ambassador:1",
            "ba_uuid": "00000000-0000-0000-0000-000000000099",
            "name": "Backup BA",
            "score": 88,
            "reasons": ["12.0 units/hr", "97% reliable"],
            "summary_reason": "12.0 units/hr · 97% reliable",
            "email": "backup@example.com",
            "phone": "5555550199",
        }
        with (
            patch("staffing.cascade.compute_smart_ba_matches", return_value=[fake_match]),
            patch("staffing.cascade._email_configured", return_value=False),
            patch("staffing.cascade.sms_configured", return_value=False),
        ):
            alert, created, message = trigger_noshow_cascade_for_event(
                event=self.event,
                ambassador_event=self.ae,
                trigger_reason=NoShowCascadeAlert.REASON_MANUAL,
                dry_run=True,
                now=self.now,
            )
        assert alert is None
        assert created is False
        assert "Dry run" in message
        assert NoShowCascadeAlert.objects.count() == 0

        with (
            patch("staffing.cascade.compute_smart_ba_matches", return_value=[fake_match]),
            patch("staffing.cascade._email_configured", return_value=False),
            patch("staffing.cascade.sms_configured", return_value=False),
            patch(
                "staffing.cascade._ops_alert_emails",
                return_value=["ops@igniteproductions.co"],
            ),
        ):
            alert, created, _ = trigger_noshow_cascade_for_event(
                event=self.event,
                ambassador_event=self.ae,
                trigger_reason=NoShowCascadeAlert.REASON_MANUAL,
                dry_run=False,
                now=self.now,
            )
        assert created is True
        assert alert is not None
        statuses = {entry["status"] for entry in alert.delivery_audit}
        assert "skipped_not_configured" in statuses

    def test_evaluate_flags_missing_checkin(self):
        with patch(
            "staffing.cascade.trigger_noshow_cascade_for_event",
            return_value=(None, False, "Dry run"),
        ) as mocked:
            summary = evaluate_noshow_cascades(
                now=self.now,
                checkin_grace_minutes=15,
                lookback_hours=6,
                dry_run=True,
                tenant_id=self.tenant.id,
            )
        assert summary["checked_missing_checkin"] >= 1
        assert summary["flagged"] >= 1
        assert mocked.called
