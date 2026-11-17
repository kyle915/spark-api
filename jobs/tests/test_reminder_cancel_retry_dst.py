"""Shift reminder emails: cancelled demos, failed sends, and DST.

1. A cancelled demo keeps its approved bookings (events/demo_cancel.py), so the
   24h/3h reminder emails and the 15m pushes must check the event itself.
2. A reminder email that fails must not be stamped as sent; the next cron pass
   retries it, up to REMINDER_EMAIL_MAX_ATTEMPTS.
3. Shift times in these emails are the venue's wall clock at the shift's own
   date, however the TimeZone row stores its offset.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone as dt_timezone
from io import StringIO
from unittest.mock import patch

import pytest
from django.core.management import call_command

from events import models as event_models
from events.models import TimeZone
from jobs.envelopes import _build_ambassador_job_email_context
from jobs.models import AmbassadorJob
from jobs.tasks import (
    REMINDER_EMAIL_MAX_ATTEMPTS,
    send_ambassador_job_15m_reminder_push,
    send_ambassador_job_24h_reminder,
    send_ambassador_job_3h_reminder,
)
from jobs.tests.test_tasks_event_reminders import BaseReminderTestCase

MAILER_24H = "jobs.tasks.AmbassadorEventReminderMailer.send"
MAILER_3H = "jobs.tasks.AmbassadorEventReminder3HoursMailer.send"


@pytest.mark.django_db(transaction=True)
class TestCancelledDemoGetsNoReminder(BaseReminderTestCase):
    def _event_status(self, slug):
        return event_models.EventStatus.objects.create(
            name=slug.title(), slug=slug, tenant=self.tenant, created_by=self.system_user
        )

    def _attach_request(self, event, *, status_slug, deleted=False):
        status = event_models.RequestStatus.objects.create(
            name=status_slug.title(),
            slug=status_slug,
            tenant=self.tenant,
            created_by=self.system_user,
        )
        request = event_models.Request.objects.create(
            name="Demo",
            address="123 Main St",
            tenant=self.tenant,
            status=status,
            request_type=event_models.RequestType.objects.create(
                name="Retail", tenant=self.tenant, created_by=self.system_user
            ),
            created_by=None,
        )
        if deleted:
            event_models.Request.objects.filter(pk=request.pk).update(
                deleted_at=datetime(2026, 3, 1, tzinfo=dt_timezone.utc)
            )
        event_models.Event.objects.filter(pk=event.pk).update(request=request)

    def _due_job(self, now):
        return self._build_ambassador_job(event_start_time=now + timedelta(hours=2))

    @patch(MAILER_3H)
    @patch(MAILER_24H)
    @patch("jobs.tasks.timezone.now")
    def test_suspended_event_sends_no_24h_or_3h_email(self, mock_now, send_24, send_3):
        now = datetime(2026, 3, 18, 12, 0, tzinfo=dt_timezone.utc)
        mock_now.return_value = now
        aj = self._due_job(now)
        event_models.Event.objects.filter(pk=aj.job.event_id).update(
            status=self._event_status("suspended")
        )

        assert send_ambassador_job_24h_reminder(aj.id) == 0
        assert send_ambassador_job_3h_reminder(aj.id) == 0
        send_24.assert_not_called()
        send_3.assert_not_called()
        aj.refresh_from_db()
        assert aj.reminder_sent_at is None and aj.reminder_3h_sent_at is None

    @patch(MAILER_24H)
    @patch("jobs.tasks.timezone.now")
    def test_declined_or_deleted_request_sends_no_email(self, mock_now, send_24):
        now = datetime(2026, 3, 18, 12, 0, tzinfo=dt_timezone.utc)
        mock_now.return_value = now
        declined = self._due_job(now)
        self._attach_request(declined.job.event, status_slug="declined")
        assert send_ambassador_job_24h_reminder(declined.id) == 0

        deleted = self._due_job(now)
        self._attach_request(deleted.job.event, status_slug="approved", deleted=True)
        assert send_ambassador_job_24h_reminder(deleted.id) == 0
        send_24.assert_not_called()

    @patch(MAILER_24H)
    @patch("jobs.tasks.timezone.now")
    def test_an_approved_request_still_gets_its_email(self, mock_now, send_24):
        now = datetime(2026, 3, 18, 12, 0, tzinfo=dt_timezone.utc)
        mock_now.return_value = now
        aj = self._due_job(now)
        self._attach_request(aj.job.event, status_slug="approved")
        assert send_ambassador_job_24h_reminder(aj.id) == 1
        send_24.assert_called_once()

    @patch("jobs.tasks.async_to_sync")
    def test_suspended_event_gets_no_15m_push(self, mock_async_to_sync):
        aj = self._due_job(datetime.now(dt_timezone.utc))
        event_models.Event.objects.filter(pk=aj.job.event_id).update(
            status=self._event_status("cancelled")
        )
        assert send_ambassador_job_15m_reminder_push(aj.id) == 0
        mock_async_to_sync.assert_not_called()

    def test_cron_dry_run_does_not_count_cancelled_demos(self):
        live = self._due_job(datetime.now(dt_timezone.utc))
        cancelled = self._due_job(datetime.now(dt_timezone.utc))
        event_models.Event.objects.filter(pk=cancelled.job.event_id).update(
            status=self._event_status("suspended")
        )
        out = StringIO()
        call_command("send_ambassador_job_reminders", "--dry-run", stdout=out)
        log = out.getvalue()
        assert f"[24h] would fire: 1 (ids=[{live.id}])" in log
        assert f"[3h] would fire: 1 (ids=[{live.id}])" in log


@pytest.mark.django_db(transaction=True)
class TestFailedReminderEmailIsRetried(BaseReminderTestCase):
    @patch(MAILER_24H)
    @patch("jobs.tasks.timezone.now")
    def test_failed_send_is_not_stamped_and_the_next_pass_retries(self, mock_now, send):
        now = datetime(2026, 3, 18, 12, 0, tzinfo=dt_timezone.utc)
        mock_now.return_value = now
        aj = self._build_ambassador_job(event_start_time=now + timedelta(hours=20))

        send.return_value = False  # provider answered without taking it
        assert send_ambassador_job_24h_reminder(aj.id) == 0
        aj.refresh_from_db()
        assert aj.reminder_sent_at is None
        assert aj.reminder_24h_attempts == 1

        send.return_value = True
        assert send_ambassador_job_24h_reminder(aj.id) == 1
        aj.refresh_from_db()
        assert aj.reminder_sent_at == now
        assert send.call_count == 2

    @patch(MAILER_3H, side_effect=RuntimeError("resend down"))
    @patch("jobs.tasks.timezone.now")
    def test_a_raising_send_is_not_stamped(self, mock_now, send):
        now = datetime(2026, 3, 18, 12, 0, tzinfo=dt_timezone.utc)
        mock_now.return_value = now
        aj = self._build_ambassador_job(event_start_time=now + timedelta(hours=2))
        assert send_ambassador_job_3h_reminder(aj.id) == 0
        aj.refresh_from_db()
        assert aj.reminder_3h_sent_at is None
        assert aj.reminder_3h_attempts == 1

    @patch(MAILER_24H, return_value=False)
    def test_retries_stop_at_the_cap(self, send):
        aj = self._build_ambassador_job(
            event_start_time=datetime.now(dt_timezone.utc) + timedelta(hours=20)
        )
        for _ in range(REMINDER_EMAIL_MAX_ATTEMPTS + 2):
            call_command("send_ambassador_job_reminders", stdout=StringIO())

        assert send.call_count == REMINDER_EMAIL_MAX_ATTEMPTS
        aj.refresh_from_db()
        assert aj.reminder_sent_at is None
        assert aj.reminder_24h_attempts == REMINDER_EMAIL_MAX_ATTEMPTS
        out = StringIO()
        call_command("send_ambassador_job_reminders", "--dry-run", stdout=out)
        assert "[24h] would fire: 0" in out.getvalue()

    @patch(MAILER_24H, return_value=None)
    @patch("jobs.tasks.timezone.now")
    def test_nothing_to_send_still_counts_as_done(self, mock_now, send):
        """None = nobody to send to / muted: retrying would not change that."""
        now = datetime(2026, 3, 18, 12, 0, tzinfo=dt_timezone.utc)
        mock_now.return_value = now
        aj = self._build_ambassador_job(event_start_time=now + timedelta(hours=20))
        assert send_ambassador_job_24h_reminder(aj.id) == 1
        aj.refresh_from_db()
        assert aj.reminder_sent_at == now


@pytest.mark.django_db(transaction=True)
class TestShiftEmailTimesAcrossDst(BaseReminderTestCase):
    def _context_for(self, tz_row, start_utc):
        aj = self._build_ambassador_job(event_start_time=start_utc)
        event_models.Event.objects.filter(pk=aj.job.event_id).update(
            timezone=tz_row, end_time=start_utc + timedelta(hours=3)
        )
        aj = AmbassadorJob.objects.select_related("job__event__timezone").get(pk=aj.pk)
        return _build_ambassador_job_email_context(aj)

    def test_pacific_row_stored_with_the_winter_offset_reads_right_in_summer(self):
        # 12:00 PDT on July 10 = 19:00 UTC. The row carries PST's -480.
        pst = TimeZone.objects.create(
            name="Pacific", code="PST", offset=-480, created_by=self.system_user
        )
        ctx = self._context_for(pst, datetime(2026, 7, 10, 19, 0, tzinfo=dt_timezone.utc))
        assert ctx["start_time"] == "12:00 PM"  # was "11:00 AM"
        assert ctx["end_time"] == "3:00 PM"
        assert ctx["activation_date"] == "07/10/2026"

    def test_eastern_row_in_winter_and_summer(self):
        est = TimeZone.objects.create(
            name="Eastern", code="EST", offset=-300, created_by=self.system_user
        )
        winter = self._context_for(est, datetime(2026, 1, 15, 15, 0, tzinfo=dt_timezone.utc))
        summer = self._context_for(est, datetime(2026, 7, 15, 14, 0, tzinfo=dt_timezone.utc))
        assert winter["start_time"] == "10:00 AM"
        assert summer["start_time"] == "10:00 AM"

    def test_offset_stored_in_hours_is_not_read_as_minutes(self):
        # A row with no recognisable name/code and offset -6 (hours). Adding it
        # as minutes put the shift at 5:54 PM UTC-ish instead of noon.
        odd = TimeZone.objects.create(
            name="Venue local", code="", offset=-6, created_by=self.system_user
        )
        ctx = self._context_for(odd, datetime(2026, 1, 15, 18, 0, tzinfo=dt_timezone.utc))
        assert ctx["start_time"] == "12:00 PM"

    def test_evening_shift_keeps_the_venue_date(self):
        # 8pm PDT July 10 is already July 11 in UTC.
        pdt = TimeZone.objects.create(
            name="Pacific", code="PDT", offset=-420, created_by=self.system_user
        )
        ctx = self._context_for(pdt, datetime(2026, 7, 11, 3, 0, tzinfo=dt_timezone.utc))
        assert ctx["activation_date"] == "07/10/2026"
        assert ctx["start_time"] == "8:00 PM"
