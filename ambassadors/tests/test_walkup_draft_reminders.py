"""Walk-up draft-stuck reminder: one email, only for unsubmitted drafts.

A BA who clocked out (or whose shift day passed) with answers still in
`WalkupRecapDraft` gets one email linking the standing check-in. Filed
recaps, empty drafts, and drafts from a shift still in progress get
nothing, and the stamp stops a second send.
"""

from __future__ import annotations

import io
from datetime import timedelta
from unittest.mock import patch

import pytest
from django.core.management import call_command
from django.test import Client, override_settings
from django.utils import timezone

from ambassadors.draft_reminders import send_walkup_draft_reminders
from ambassadors.models import WalkupRecapDraft
from ambassadors.tests.base import AmbassadorsGraphQLTestCase

CRON_URL = "/internal/cron/walkup-draft-reminders"
VALID_SECRET = "test-cron-secret-value-only-for-tests"


@pytest.mark.django_db(transaction=True)
class TestWalkupDraftReminders(AmbassadorsGraphQLTestCase):
    @pytest.fixture(autouse=True)
    def setup(self, db):
        self.roles = self.setup_default_roles()
        self.tenant = self.create_tenant(name="Reminder Tenant")
        self.tenant.checkin_code = "RM-TEST1"
        self.tenant.save(update_fields=["checkin_code"])
        self.actor = self.create_user(
            username="actor-reminder@test.com",
            email="actor-reminder@test.com",
            role=self.roles["spark_admin"],
        )
        ba_user = self.create_user(
            username="ba-reminder@test.com",
            email="ba-reminder@test.com",
            role=self.roles["ambassador"],
        )
        self.ba = self.create_ambassador(ba_user)
        etype = self.create_event_type("Retail Sampling", self.tenant)
        self.event = self.create_event(
            name="Reminder shift",
            tenant=self.tenant,
            address="1 Market St",
            event_type=etype,
            date=timezone.now() - timedelta(days=1),
        )
        self.sent: list[dict] = []

    def _send(self, **kwargs):
        self.sent.append(kwargs)

    def _draft(self, *, hours_ago=5, payload=None, scope="shift"):
        draft = WalkupRecapDraft.objects.create(
            event=self.event,
            ambassador=self.ba,
            scope=scope,
            payload=payload
            if payload is not None
            else {"v": 1, "values": {"notes": "Busy aisle"}},
        )
        WalkupRecapDraft.objects.filter(id=draft.id).update(
            updated_at=timezone.now() - timedelta(hours=hours_ago)
        )
        draft.refresh_from_db()
        return draft

    def _run(self, **kwargs):
        kwargs.setdefault("send_email", self._send)
        return send_walkup_draft_reminders(**kwargs)

    def test_clocked_out_or_past_shift_draft_gets_one_email_with_standing_link(self):
        draft = self._draft()
        counts = self._run()
        assert counts["sent"] == 1
        assert len(self.sent) == 1
        assert self.sent[0]["to_email"] == "ba-reminder@test.com"
        assert "https://client.igniteproductions.co/checkin/RM-TEST1" in self.sent[0]["body_html"]
        draft.refresh_from_db()
        assert draft.reminder_sent_at is not None

        counts = self._run()
        assert counts["sent"] == 0
        assert len(self.sent) == 1

    def test_recent_draft_inside_grace_is_not_reminded(self):
        self._draft(hours_ago=1)
        assert self._run()["sent"] == 0
        assert self.sent == []

    def test_empty_draft_is_not_reminded(self):
        self._draft(payload={"v": 1, "values": {}})
        counts = self._run()
        assert counts["sent"] == 0
        assert counts["skipped_empty"] == 1

    def test_filed_recap_is_not_reminded(self):
        from recaps.models import CustomRecap, CustomRecapTemplate

        etype = self.create_event_type("Retail Sampling 2", self.tenant)
        template = CustomRecapTemplate.objects.create(
            tenant=self.tenant, name="T", event_type=etype, created_by=self.actor
        )
        CustomRecap.objects.create(
            name="Filed",
            event=self.event,
            ambassador=self.ba,
            tenant=self.tenant,
            custom_recap_template=template,
            created_by=self.actor,
            updated_by=self.actor,
            submitted_at=timezone.now(),
        )
        draft = self._draft()
        counts = self._run()
        assert counts["sent"] == 0
        assert counts["skipped_filed"] == 1
        draft.refresh_from_db()
        assert draft.reminder_sent_at is None

    def test_dry_run_sends_and_stamps_nothing(self):
        draft = self._draft()
        out = io.StringIO()
        call_command(
            "send_walkup_draft_reminders", "--dry-run", stdout=out
        )
        draft.refresh_from_db()
        assert draft.reminder_sent_at is None
        assert "sent 1" in out.getvalue()


@pytest.mark.django_db
class TestWalkupDraftRemindersCronView:
    @pytest.fixture(autouse=True)
    def setup(self):
        self.client = Client()

    @override_settings(INTERNAL_CRON_SECRET=VALID_SECRET)
    @patch("digest.cron_views.call_command")
    def test_valid_secret_fires_command(self, mock_call):
        resp = self.client.post(CRON_URL, HTTP_X_CRON_SECRET=VALID_SECRET)
        assert resp.status_code == 200
        assert resp.json()["ok"] is True
        args, _ = mock_call.call_args
        assert args[0] == "send_walkup_draft_reminders"

    @override_settings(INTERNAL_CRON_SECRET=VALID_SECRET)
    @patch("digest.cron_views.call_command")
    def test_bad_secret_returns_401(self, mock_call):
        resp = self.client.post(CRON_URL, HTTP_X_CRON_SECRET="nope")
        assert resp.status_code == 401
        mock_call.assert_not_called()
