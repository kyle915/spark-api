from datetime import datetime, timezone as dt_timezone
from unittest.mock import patch

import pytest

from ambassadors.models import Attendance, AttendanceType
from events.models import TimeZone
from jobs.tasks import (
    schedule_ambassador_job_end_15m_reminder,
    schedule_ambassador_job_15m_reminder,
    schedule_ambassador_job_24h_reminder,
    schedule_ambassador_job_3h_reminder,
    send_ambassador_job_end_15m_reminder_push,
    send_ambassador_job_15m_reminder_push,
    send_ambassador_job_24h_reminder,
    send_ambassador_job_3h_reminder,
)
from jobs.tests.base import JobsGraphQLTestCase


@pytest.mark.django_db(transaction=True)
class BaseReminderTestCase(JobsGraphQLTestCase):
    @pytest.fixture(autouse=True)
    def setup(self):
        self.roles = self.setup_default_roles()
        self.tenant = self.create_tenant(name="Reminder Tenant")
        self.system_user = self.get_system_user()

        self.job_title = self.create_job_title(name="Brand Ambassador", tenant=self.tenant)
        self.rate_type = self.create_rate_type(name="Hourly", tenant=self.tenant)
        self.rate = self.create_rate(amount=35.0, rate_type=self.rate_type, tenant=self.tenant)
        self.status = self.create_status(name="Approved", slug="approved", tenant=self.tenant)

        self.ambassador_user = self.create_user(
            username="reminder_ambassador@test.com",
            email="reminder_ambassador@test.com",
            role=self.roles["ambassador"],
            password="testpass123",
        )
        self.ambassador = self.create_ambassador(user=self.ambassador_user)
        self.create_tenanted_user(user=self.ambassador_user, tenant=self.tenant)

        self.tz_minus_five = TimeZone.objects.create(
            name="EST",
            code="EST",
            offset=-5,
            created_by=self.system_user,
        )

    def _build_ambassador_job(self, *, event_start_time: datetime):
        event = self.create_event(
            name="Reminder Event",
            tenant=self.tenant,
            address="123 Main St",
            start_time=event_start_time,
            timezone=self.tz_minus_five,
        )
        job = self.create_job(
            name="Reminder Job",
            code="REM-001",
            address="123 Main St",
            event=event,
            job_title=self.job_title,
            tenant=self.tenant,
            rate=self.rate,
            start_date=event_start_time,
        )
        return self.create_ambassador_job(
            ambassador=self.ambassador,
            job=job,
            status=self.status,
            rate=self.rate,
            tenant=self.tenant,
        )

    def _build_ambassador_job_with_event_date(
        self,
        *,
        event_start_time: datetime,
        event_date: datetime,
    ):
        ambassador_job = self._build_ambassador_job(event_start_time=event_start_time)
        event = ambassador_job.job.event
        event.date = event_date
        event.save(update_fields=["date"])
        return ambassador_job


@pytest.mark.django_db(transaction=True)
class TestSendUpcomingAmbassadorEventReminders(BaseReminderTestCase):
    @patch("jobs.tasks.django_rq.get_scheduler")
    @patch("jobs.tasks.timezone.now")
    def test_schedules_exact_24h_reminder_at_event_start_minus_24_hours(
        self,
        mock_now,
        mock_get_scheduler,
    ):
        now_utc = datetime(2026, 3, 18, 12, 0, tzinfo=dt_timezone.utc)
        mock_now.return_value = now_utc
        ambassador_job = self._build_ambassador_job(
            event_start_time=datetime(2026, 3, 19, 19, 30, tzinfo=dt_timezone.utc)
        )

        scheduler = mock_get_scheduler.return_value
        scheduler.get_jobs.return_value = []
        scheduler.schedule.return_value = type("ScheduledJob", (), {"id": "scheduled-24h"})()

        job_id = schedule_ambassador_job_24h_reminder(ambassador_job.id)

        assert job_id == "scheduled-24h"
        kwargs = scheduler.schedule.call_args.kwargs
        assert kwargs["scheduled_time"] == datetime(2026, 3, 18, 19, 30)
        assert kwargs["args"] == [ambassador_job.id, "2026-03-18T19:30:00+00:00"]

    @patch("jobs.tasks.AmbassadorEventReminderMailer.send")
    @patch("jobs.tasks.timezone.now")
    def test_sends_24h_reminder_when_trigger_matches(self, mock_now, mock_send):
        trigger_at = datetime(2026, 3, 18, 19, 0, tzinfo=dt_timezone.utc)
        mock_now.return_value = trigger_at
        ambassador_job = self._build_ambassador_job(
            event_start_time=datetime(2026, 3, 19, 19, 0, tzinfo=dt_timezone.utc)
        )

        sent = send_ambassador_job_24h_reminder(ambassador_job.id, trigger_at.isoformat())

        assert sent == 1
        mock_send.assert_called_once()
        ambassador_job.refresh_from_db()
        assert ambassador_job.reminder_sent_at == trigger_at

    @patch("jobs.tasks.AmbassadorEventReminderMailer.send")
    def test_skips_stale_24h_reminder_after_event_time_changes(self, mock_send):
        ambassador_job = self._build_ambassador_job(
            event_start_time=datetime(2026, 3, 19, 19, 0, tzinfo=dt_timezone.utc)
        )
        expected_trigger = datetime(2026, 3, 18, 19, 0, tzinfo=dt_timezone.utc)

        ambassador_job.job.event.start_time = datetime(
            2026, 3, 19, 21, 0, tzinfo=dt_timezone.utc
        )
        ambassador_job.job.event.save(update_fields=["start_time"])

        sent = send_ambassador_job_24h_reminder(
            ambassador_job.id,
            expected_trigger.isoformat(),
        )

        assert sent == 0
        mock_send.assert_not_called()
        ambassador_job.refresh_from_db()
        assert ambassador_job.reminder_sent_at is None

    @patch("jobs.tasks.django_rq.get_scheduler")
    @patch("jobs.tasks.timezone.now")
    def test_event_schedule_change_resets_24h_sent_marker_and_reschedules(
        self,
        mock_now,
        mock_get_scheduler,
    ):
        mock_now.return_value = datetime(2026, 3, 18, 12, 0, tzinfo=dt_timezone.utc)
        ambassador_job = self._build_ambassador_job(
            event_start_time=datetime(2026, 3, 19, 19, 0, tzinfo=dt_timezone.utc)
        )
        ambassador_job.reminder_sent_at = datetime(
            2026, 3, 18, 19, 0, tzinfo=dt_timezone.utc
        )
        ambassador_job.save(update_fields=["reminder_sent_at"])

        scheduler = mock_get_scheduler.return_value
        scheduler.get_jobs.return_value = []
        scheduler.schedule.return_value = type("ScheduledJob", (), {"id": "rescheduled-24h"})()

        event = ambassador_job.job.event
        event.start_time = datetime(2026, 3, 19, 21, 0, tzinfo=dt_timezone.utc)
        event.save(update_fields=["start_time"])

        ambassador_job.refresh_from_db()
        assert ambassador_job.reminder_sent_at is None
        scheduler.schedule.assert_called()


@pytest.mark.django_db(transaction=True)
class TestSendUpcomingAmbassadorEvent3HoursReminders(BaseReminderTestCase):
    @patch("jobs.tasks.django_rq.get_scheduler")
    @patch("jobs.tasks.timezone.now")
    def test_schedules_exact_3h_reminder_at_event_start_minus_3_hours(
        self,
        mock_now,
        mock_get_scheduler,
    ):
        now_utc = datetime(2026, 3, 18, 12, 0, tzinfo=dt_timezone.utc)
        mock_now.return_value = now_utc
        ambassador_job = self._build_ambassador_job(
            event_start_time=datetime(2026, 3, 18, 19, 30, tzinfo=dt_timezone.utc)
        )

        scheduler = mock_get_scheduler.return_value
        scheduler.get_jobs.return_value = []
        scheduler.schedule.return_value = type("ScheduledJob", (), {"id": "scheduled-3h"})()

        job_id = schedule_ambassador_job_3h_reminder(ambassador_job.id)

        assert job_id == "scheduled-3h"
        kwargs = scheduler.schedule.call_args.kwargs
        assert kwargs["scheduled_time"] == datetime(2026, 3, 18, 16, 30)
        assert kwargs["args"] == [ambassador_job.id, "2026-03-18T16:30:00+00:00"]

    @patch("jobs.tasks.django_rq.get_scheduler")
    @patch("jobs.tasks.timezone.now")
    def test_ignores_event_date_time_for_3h_trigger(
        self,
        mock_now,
        mock_get_scheduler,
    ):
        now_utc = datetime(2026, 3, 18, 12, 0, tzinfo=dt_timezone.utc)
        mock_now.return_value = now_utc
        ambassador_job = self._build_ambassador_job_with_event_date(
            event_start_time=datetime(2026, 4, 6, 19, 10, tzinfo=dt_timezone.utc),
            event_date=datetime(2026, 4, 6, 6, 0, tzinfo=dt_timezone.utc),
        )

        scheduler = mock_get_scheduler.return_value
        scheduler.get_jobs.return_value = []
        scheduler.schedule.return_value = type("ScheduledJob", (), {"id": "scheduled-3h"})()

        job_id = schedule_ambassador_job_3h_reminder(ambassador_job.id)

        assert job_id == "scheduled-3h"
        kwargs = scheduler.schedule.call_args.kwargs
        assert kwargs["scheduled_time"] == datetime(2026, 4, 6, 16, 10)
        assert kwargs["args"] == [ambassador_job.id, "2026-04-06T16:10:00+00:00"]

    @patch("jobs.tasks.AmbassadorEventReminder3HoursMailer.send")
    @patch("jobs.tasks.timezone.now")
    def test_sends_3h_reminder_when_trigger_matches(self, mock_now, mock_send):
        trigger_at = datetime(2026, 3, 18, 16, 0, tzinfo=dt_timezone.utc)
        mock_now.return_value = trigger_at
        ambassador_job = self._build_ambassador_job(
            event_start_time=datetime(2026, 3, 18, 19, 0, tzinfo=dt_timezone.utc)
        )

        sent = send_ambassador_job_3h_reminder(ambassador_job.id, trigger_at.isoformat())

        assert sent == 1
        mock_send.assert_called_once()
        ambassador_job.refresh_from_db()
        assert ambassador_job.reminder_3h_sent_at == trigger_at

    @patch("jobs.tasks.AmbassadorEventReminder3HoursMailer.send")
    def test_skips_stale_3h_reminder_after_event_time_changes(self, mock_send):
        ambassador_job = self._build_ambassador_job(
            event_start_time=datetime(2026, 3, 18, 19, 0, tzinfo=dt_timezone.utc)
        )
        expected_trigger = datetime(2026, 3, 18, 16, 0, tzinfo=dt_timezone.utc)

        ambassador_job.job.event.start_time = datetime(
            2026, 3, 18, 21, 0, tzinfo=dt_timezone.utc
        )
        ambassador_job.job.event.save(update_fields=["start_time"])

        sent = send_ambassador_job_3h_reminder(
            ambassador_job.id,
            expected_trigger.isoformat(),
        )

        assert sent == 0
        mock_send.assert_not_called()
        ambassador_job.refresh_from_db()
        assert ambassador_job.reminder_3h_sent_at is None

    @patch("jobs.tasks.django_rq.get_scheduler")
    @patch("jobs.tasks.timezone.now")
    def test_event_schedule_change_resets_sent_marker_and_reschedules(
        self,
        mock_now,
        mock_get_scheduler,
    ):
        mock_now.return_value = datetime(2026, 3, 18, 12, 0, tzinfo=dt_timezone.utc)
        ambassador_job = self._build_ambassador_job(
            event_start_time=datetime(2026, 3, 18, 19, 0, tzinfo=dt_timezone.utc)
        )
        ambassador_job.reminder_3h_sent_at = datetime(
            2026, 3, 18, 16, 0, tzinfo=dt_timezone.utc
        )
        ambassador_job.save(update_fields=["reminder_3h_sent_at"])

        scheduler = mock_get_scheduler.return_value
        scheduler.get_jobs.return_value = []
        scheduler.schedule.return_value = type("ScheduledJob", (), {"id": "rescheduled"})()

        event = ambassador_job.job.event
        event.start_time = datetime(2026, 3, 18, 21, 0, tzinfo=dt_timezone.utc)
        event.save(update_fields=["start_time"])

        ambassador_job.refresh_from_db()
        assert ambassador_job.reminder_3h_sent_at is None
        scheduler.schedule.assert_called()


@pytest.mark.django_db(transaction=True)
class TestSendUpcomingAmbassadorEvent15MinutesReminders(BaseReminderTestCase):
    @patch("jobs.tasks.django_rq.get_scheduler")
    @patch("jobs.tasks.timezone.now")
    def test_schedules_exact_15m_reminder_at_event_start_minus_15_minutes(
        self,
        mock_now,
        mock_get_scheduler,
    ):
        now_utc = datetime(2026, 3, 18, 12, 0, tzinfo=dt_timezone.utc)
        mock_now.return_value = now_utc
        ambassador_job = self._build_ambassador_job(
            event_start_time=datetime(2026, 3, 18, 19, 30, tzinfo=dt_timezone.utc)
        )

        scheduler = mock_get_scheduler.return_value
        scheduler.get_jobs.return_value = []
        scheduler.schedule.return_value = type("ScheduledJob", (), {"id": "scheduled-15m"})()

        job_id = schedule_ambassador_job_15m_reminder(ambassador_job.id)

        assert job_id == "scheduled-15m"
        kwargs = scheduler.schedule.call_args.kwargs
        assert kwargs["scheduled_time"] == datetime(2026, 3, 18, 19, 15)
        assert kwargs["args"] == [ambassador_job.id, "2026-03-18T19:15:00+00:00"]

    @patch("jobs.tasks.async_to_sync")
    @patch("jobs.tasks.timezone.now")
    def test_sends_15m_push_when_trigger_matches(self, mock_now, mock_async_to_sync):
        trigger_at = datetime(2026, 3, 18, 19, 15, tzinfo=dt_timezone.utc)
        mock_now.return_value = trigger_at
        ambassador_job = self._build_ambassador_job(
            event_start_time=datetime(2026, 3, 18, 19, 30, tzinfo=dt_timezone.utc)
        )

        mock_sender = mock_async_to_sync.return_value
        sent = send_ambassador_job_15m_reminder_push(ambassador_job.id, trigger_at.isoformat())

        assert sent == 1
        mock_sender.assert_called_once()
        call_kwargs = mock_sender.call_args.kwargs
        assert call_kwargs["external_ids"] == [str(self.ambassador_user.uuid)]
        assert call_kwargs["data"]["type"] == "event_starting_soon_15m"
        ambassador_job.refresh_from_db()
        assert ambassador_job.reminder_15m_sent_at == trigger_at

    @patch("jobs.tasks.async_to_sync")
    def test_skips_stale_15m_push_after_event_time_changes(self, mock_async_to_sync):
        ambassador_job = self._build_ambassador_job(
            event_start_time=datetime(2026, 3, 18, 19, 30, tzinfo=dt_timezone.utc)
        )
        expected_trigger = datetime(2026, 3, 18, 19, 15, tzinfo=dt_timezone.utc)

        ambassador_job.job.event.start_time = datetime(
            2026, 3, 18, 21, 0, tzinfo=dt_timezone.utc
        )
        ambassador_job.job.event.save(update_fields=["start_time"])

        sent = send_ambassador_job_15m_reminder_push(
            ambassador_job.id,
            expected_trigger.isoformat(),
        )

        assert sent == 0
        mock_async_to_sync.assert_not_called()
        ambassador_job.refresh_from_db()
        assert ambassador_job.reminder_15m_sent_at is None


@pytest.mark.django_db(transaction=True)
class TestSendUpcomingAmbassadorEventEnd15MinutesReminders(BaseReminderTestCase):
    @patch("jobs.tasks.django_rq.get_scheduler")
    @patch("jobs.tasks.timezone.now")
    def test_schedules_exact_end_plus_15m_reminder(
        self,
        mock_now,
        mock_get_scheduler,
    ):
        now_utc = datetime(2026, 3, 18, 12, 0, tzinfo=dt_timezone.utc)
        mock_now.return_value = now_utc
        ambassador_job = self._build_ambassador_job(
            event_start_time=datetime(2026, 3, 18, 19, 30, tzinfo=dt_timezone.utc)
        )
        ambassador_job.job.event.end_time = datetime(2026, 3, 18, 21, 0, tzinfo=dt_timezone.utc)
        ambassador_job.job.event.save(update_fields=["end_time"])

        scheduler = mock_get_scheduler.return_value
        scheduler.get_jobs.return_value = []
        scheduler.schedule.return_value = type("ScheduledJob", (), {"id": "scheduled-end-15m"})()

        job_id = schedule_ambassador_job_end_15m_reminder(ambassador_job.id)

        assert job_id == "scheduled-end-15m"
        kwargs = scheduler.schedule.call_args.kwargs
        assert kwargs["scheduled_time"] == datetime(2026, 3, 18, 21, 15)
        assert kwargs["args"] == [ambassador_job.id, "2026-03-18T21:15:00+00:00"]

    @patch("jobs.tasks.async_to_sync")
    @patch("jobs.tasks.timezone.now")
    def test_sends_end_plus_15m_push(self, mock_now, mock_async_to_sync):
        trigger_at = datetime(2026, 3, 18, 21, 15, tzinfo=dt_timezone.utc)
        mock_now.return_value = trigger_at
        ambassador_job = self._build_ambassador_job(
            event_start_time=datetime(2026, 3, 18, 19, 30, tzinfo=dt_timezone.utc)
        )
        ambassador_job.job.event.end_time = datetime(2026, 3, 18, 21, 0, tzinfo=dt_timezone.utc)
        ambassador_job.job.event.save(update_fields=["end_time"])

        mock_sender = mock_async_to_sync.return_value
        sent = send_ambassador_job_end_15m_reminder_push(
            ambassador_job.id,
            trigger_at.isoformat(),
        )

        assert sent == 1
        mock_sender.assert_called_once()
        call_kwargs = mock_sender.call_args.kwargs
        assert call_kwargs["external_ids"] == [str(self.ambassador_user.uuid)]
        assert call_kwargs["data"]["type"] == "event_ended_clock_out_recap_15m"
        ambassador_job.refresh_from_db()
        assert ambassador_job.reminder_end_15m_sent_at == trigger_at

    @patch("jobs.tasks.async_to_sync")
    def test_skips_stale_end_plus_15m_push_after_end_time_changes(self, mock_async_to_sync):
        ambassador_job = self._build_ambassador_job(
            event_start_time=datetime(2026, 3, 18, 19, 30, tzinfo=dt_timezone.utc)
        )
        ambassador_job.job.event.end_time = datetime(2026, 3, 18, 21, 0, tzinfo=dt_timezone.utc)
        ambassador_job.job.event.save(update_fields=["end_time"])
        expected_trigger = datetime(2026, 3, 18, 21, 15, tzinfo=dt_timezone.utc)

        ambassador_job.job.event.end_time = datetime(2026, 3, 18, 22, 0, tzinfo=dt_timezone.utc)
        ambassador_job.job.event.save(update_fields=["end_time"])

        sent = send_ambassador_job_end_15m_reminder_push(
            ambassador_job.id,
            expected_trigger.isoformat(),
        )

        assert sent == 0
        mock_async_to_sync.assert_not_called()
        ambassador_job.refresh_from_db()
        assert ambassador_job.reminder_end_15m_sent_at is None

    @patch("jobs.tasks.async_to_sync")
    @patch("jobs.tasks.timezone.now")
    def test_sends_recap_only_when_clock_out_exists(self, mock_now, mock_async_to_sync):
        trigger_at = datetime(2026, 3, 18, 21, 15, tzinfo=dt_timezone.utc)
        mock_now.return_value = trigger_at
        ambassador_job = self._build_ambassador_job(
            event_start_time=datetime(2026, 3, 18, 19, 30, tzinfo=dt_timezone.utc)
        )
        ambassador_job.job.event.end_time = datetime(2026, 3, 18, 21, 0, tzinfo=dt_timezone.utc)
        ambassador_job.job.event.save(update_fields=["end_time"])

        clock_out_type = AttendanceType.objects.create(
            name="Clock Out",
            slug="clock_out",
            created_by=self.system_user,
        )
        Attendance.objects.create(
            clock_time=trigger_at,
            ambassador=ambassador_job.ambassador,
            job=ambassador_job.job,
            event=ambassador_job.job.event,
            attendace_type=clock_out_type,
            created_by=self.system_user,
        )

        mock_sender = mock_async_to_sync.return_value
        sent = send_ambassador_job_end_15m_reminder_push(
            ambassador_job.id,
            trigger_at.isoformat(),
        )

        assert sent == 1
        mock_sender.assert_called_once()
        call_kwargs = mock_sender.call_args.kwargs
        assert call_kwargs["data"]["type"] == "event_ended_recap_15m"
