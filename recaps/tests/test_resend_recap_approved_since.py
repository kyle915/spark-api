"""Resend command + thread notify must send when Cloud Tasks is unset."""

import io
import threading
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, patch
from zoneinfo import ZoneInfo

import pytest
from django.core.management import call_command
from django.utils import timezone

from jobs.tests.base import JobsGraphQLTestCase
from recaps import models as recap_models
from recaps.management.commands.resend_recap_approved_since import (
    approved_recaps_since,
    parse_since,
)
from recaps.mutation_parts.notify import _thread_recap_approved_notify


@pytest.mark.django_db(transaction=True)
class TestResendRecapApprovedSince(JobsGraphQLTestCase):
    @pytest.fixture(autouse=True)
    def setup(self, db):
        self.roles = self.setup_default_roles()
        self.tenant = self.create_tenant(name="LD Resend Tenant")
        self.spark_user = self.create_user(
            username="spark_resend@test.com",
            email="spark_resend@test.com",
            role=self.roles["spark_admin"],
            password="testpass123",
        )
        self.create_tenanted_user(user=self.spark_user, tenant=self.tenant)
        self.rmm_user = self.create_user(
            username="t.reed@test.com",
            email="t.reed@test.com",
            first_name="Timothy",
            role=self.roles["client"],
            password="testpass123",
        )
        self.create_tenanted_user(user=self.rmm_user, tenant=self.tenant)
        self.event = self.create_event(
            name="WI State Fair",
            tenant=self.tenant,
            address="West Allis, WI",
            rmm_asigned=self.rmm_user,
        )
        self.job_title = self.create_job_title(name="BA", tenant=self.tenant)
        self.job = self.create_job(
            name="Fair Job",
            code="RESEND-JOB-001",
            address="WI State Fair",
            event=self.event,
            job_title=self.job_title,
            tenant=self.tenant,
        )
        self.ambassador_user = self.create_user(
            username="kelsey@test.com",
            email="kelsey@test.com",
            role=self.roles["ambassador"],
            password="testpass123",
        )
        self.ambassador = self.create_ambassador(user=self.ambassador_user)
        self.create_tenanted_user(user=self.ambassador_user, tenant=self.tenant)

    def _make_recap(self, *, approved: bool, updated_at: datetime, name="Recap"):
        recap = recap_models.Recap.objects.create(
            name=name,
            approved=approved,
            event=self.event,
            job=self.job,
            ambassador=self.ambassador,
            created_by=self.spark_user,
            updated_by=self.spark_user,
        )
        recap_models.Recap.objects.filter(id=recap.id).update(updated_at=updated_at)
        recap.refresh_from_db()
        return recap

    def _make_custom(self, *, approved: bool, updated_at: datetime, name="Custom"):
        from events.models import EventType

        system_user = self.get_system_user()
        event_type = EventType.objects.create(
            name=f"Type {name}",
            slug=f"type-{name.lower().replace(' ', '-')}",
            tenant=self.tenant,
            created_by=system_user,
        )
        template = recap_models.CustomRecapTemplate.objects.create(
            name=f"Template {name}",
            event_type=event_type,
            tenant=self.tenant,
            created_by=system_user,
        )
        recap = recap_models.CustomRecap.objects.create(
            name=name,
            approved=approved,
            event=self.event,
            tenant=self.tenant,
            custom_recap_template=template,
            created_by=self.spark_user,
        )
        recap_models.CustomRecap.objects.filter(id=recap.id).update(
            updated_at=updated_at
        )
        recap.refresh_from_db()
        return recap

    def test_parse_since_is_midnight_pacific(self):
        since = parse_since("2026-08-15")
        assert since.tzinfo == ZoneInfo("America/Los_Angeles")
        assert since.isoformat().startswith("2026-08-15T00:00:00")

    def test_window_includes_approved_updated_since(self):
        since = parse_since("2026-08-15")
        in_window = since + timedelta(hours=18)
        too_old = since - timedelta(days=2)
        kept = self._make_recap(approved=True, updated_at=in_window, name="In")
        self._make_recap(approved=True, updated_at=too_old, name="Old")
        self._make_recap(approved=False, updated_at=in_window, name="Draft")
        rows = list(approved_recaps_since(since, kind="legacy"))
        assert [r.id for _k, r in rows] == [kept.id]

    def test_dry_run_counts_and_does_not_send(self):
        since = parse_since("2026-08-15")
        self._make_recap(
            approved=True, updated_at=since + timedelta(hours=1), name="Legacy"
        )
        self._make_custom(
            approved=True, updated_at=since + timedelta(hours=2), name="Custom"
        )
        out = io.StringIO()
        with patch(
            "recaps.mutation_parts.notify._thread_recap_approved_notify"
        ) as send:
            call_command(
                "resend_recap_approved_since",
                "--since=2026-08-15",
                "--dry-run",
                stdout=out,
            )
        send.assert_not_called()
        text = out.getvalue()
        assert "recaps=2" in text
        assert "unique_to=" in text
        assert "dry_run=True" in text

    def test_send_uses_thread_notify_once_per_recap(self):
        since = parse_since("2026-08-15")
        recap = self._make_recap(
            approved=True, updated_at=since + timedelta(hours=1), name="Send me"
        )
        out = io.StringIO()
        with patch(
            "recaps.mutation_parts.notify._thread_recap_approved_notify"
        ) as send:
            call_command(
                "resend_recap_approved_since",
                "--since=2026-08-15",
                stdout=out,
            )
        send.assert_called_once_with(recap.id, "legacy")
        assert "sent=1" in out.getvalue()

    def test_thread_notify_sends_and_does_not_crash(self):
        recap = self._make_recap(
            approved=True, updated_at=timezone.now(), name="Thread recap"
        )
        with (
            patch(
                "recaps.mutation_parts.notify._ensure_recap_pdf_for_notify",
                new_callable=AsyncMock,
            ),
            patch(
                "recaps.mutation_parts.notify.RecapApprovedNotificationMailer.send"
            ) as mock_send,
        ):
            _thread_recap_approved_notify(recap.id, "legacy")
        assert mock_send.called

    def test_thread_notify_from_worker_thread_does_not_raise(self):
        recap = self._make_recap(
            approved=True, updated_at=timezone.now(), name="Worker recap"
        )
        errors: list[BaseException] = []
        done = threading.Event()

        def _run():
            try:
                with (
                    patch(
                        "recaps.mutation_parts.notify._ensure_recap_pdf_for_notify",
                        new_callable=AsyncMock,
                    ),
                    patch(
                        "recaps.mutation_parts.notify.RecapApprovedNotificationMailer.send"
                    ),
                ):
                    _thread_recap_approved_notify(recap.id, "legacy")
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)
            finally:
                done.set()

        threading.Thread(target=_run).start()
        assert done.wait(timeout=10)
        assert errors == []
