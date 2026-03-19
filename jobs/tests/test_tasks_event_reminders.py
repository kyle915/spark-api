from datetime import datetime, timedelta, timezone as dt_timezone
from unittest.mock import patch

import pytest

from events.models import TimeZone
from jobs.tasks import (
    send_upcoming_ambassador_event_3h_reminders,
    send_upcoming_ambassador_event_reminders,
)
from jobs.tests.base import JobsGraphQLTestCase


@pytest.mark.django_db(transaction=True)
class TestSendUpcomingAmbassadorEventReminders(JobsGraphQLTestCase):
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

    @patch("jobs.tasks.AmbassadorEventReminderMailer.send")
    @patch("jobs.tasks.timezone.now")
    def test_sends_email_when_event_is_within_24_hours(self, mock_now, mock_send):
        now_utc = datetime(2026, 3, 18, 12, 0, tzinfo=dt_timezone.utc)
        mock_now.return_value = now_utc
        ambassador_job = self._build_ambassador_job(
            event_start_time=now_utc + timedelta(hours=23, minutes=30)
        )

        sent = send_upcoming_ambassador_event_reminders()

        assert sent == 1
        mock_send.assert_called_once()
        ambassador_job.refresh_from_db()
        assert ambassador_job.reminder_sent_at is not None

    @patch("jobs.tasks.AmbassadorEventReminderMailer.send")
    @patch("jobs.tasks.timezone.now")
    def test_does_not_send_when_event_is_outside_24_hours(self, mock_now, mock_send):
        now_utc = datetime(2026, 3, 18, 12, 0, tzinfo=dt_timezone.utc)
        mock_now.return_value = now_utc
        ambassador_job = self._build_ambassador_job(
            event_start_time=now_utc + timedelta(hours=25)
        )

        sent = send_upcoming_ambassador_event_reminders()

        assert sent == 0
        mock_send.assert_not_called()


@pytest.mark.django_db(transaction=True)
class TestSendUpcomingAmbassadorEvent3HoursReminders(JobsGraphQLTestCase):
    @pytest.fixture(autouse=True)
    def setup(self):
        self.roles = self.setup_default_roles()
        self.tenant = self.create_tenant(name="Reminder 3h Tenant")
        self.system_user = self.get_system_user()

        self.job_title = self.create_job_title(name="Brand Ambassador", tenant=self.tenant)
        self.rate_type = self.create_rate_type(name="Hourly", tenant=self.tenant)
        self.rate = self.create_rate(amount=35.0, rate_type=self.rate_type, tenant=self.tenant)
        self.status = self.create_status(name="Approved", slug="approved", tenant=self.tenant)

        self.ambassador_user = self.create_user(
            username="reminder_3h_ambassador@test.com",
            email="reminder_3h_ambassador@test.com",
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
            name="Reminder 3h Event",
            tenant=self.tenant,
            address="123 Main St",
            start_time=event_start_time,
            timezone=self.tz_minus_five,
        )
        job = self.create_job(
            name="Reminder 3h Job",
            code="REM-3H-001",
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

    @patch("jobs.tasks.AmbassadorEventReminder3HoursMailer.send")
    @patch("jobs.tasks.timezone.now")
    def test_sends_email_when_event_is_within_3_hours(self, mock_now, mock_send):
        now_utc = datetime(2026, 3, 18, 12, 0, tzinfo=dt_timezone.utc)
        mock_now.return_value = now_utc
        ambassador_job = self._build_ambassador_job(
            event_start_time=now_utc + timedelta(hours=2, minutes=45)
        )

        sent = send_upcoming_ambassador_event_3h_reminders()

        assert sent == 1
        mock_send.assert_called_once()
        ambassador_job.refresh_from_db()
        assert ambassador_job.reminder_3h_sent_at is not None

    @patch("jobs.tasks.AmbassadorEventReminder3HoursMailer.send")
    @patch("jobs.tasks.timezone.now")
    def test_does_not_send_when_event_is_outside_3_hours(self, mock_now, mock_send):
        now_utc = datetime(2026, 3, 18, 12, 0, tzinfo=dt_timezone.utc)
        mock_now.return_value = now_utc
        ambassador_job = self._build_ambassador_job(
            event_start_time=now_utc + timedelta(hours=4)
        )

        sent = send_upcoming_ambassador_event_3h_reminders()

        assert sent == 0
        mock_send.assert_not_called()
        ambassador_job.refresh_from_db()
        assert ambassador_job.reminder_3h_sent_at is None

    @patch("jobs.tasks.AmbassadorEventReminder3HoursMailer.send")
    @patch("jobs.tasks.timezone.now")
    def test_does_not_send_twice_if_3h_reminder_was_already_sent(self, mock_now, mock_send):
        now_utc = datetime(2026, 3, 18, 12, 0, tzinfo=dt_timezone.utc)
        mock_now.return_value = now_utc
        ambassador_job = self._build_ambassador_job(
            event_start_time=now_utc + timedelta(hours=1)
        )
        ambassador_job.reminder_3h_sent_at = now_utc - timedelta(minutes=10)
        ambassador_job.save(update_fields=["reminder_3h_sent_at"])

        sent = send_upcoming_ambassador_event_3h_reminders()

        assert sent == 0
        mock_send.assert_not_called()

    @patch("jobs.tasks.AmbassadorEventReminder3HoursMailer.send")
    @patch("jobs.tasks.timezone.now")
    def test_sends_3h_even_when_24h_was_already_sent(self, mock_now, mock_send):
        now_utc = datetime(2026, 3, 18, 12, 0, tzinfo=dt_timezone.utc)
        mock_now.return_value = now_utc
        ambassador_job = self._build_ambassador_job(
            event_start_time=now_utc + timedelta(hours=2)
        )
        ambassador_job.reminder_sent_at = now_utc - timedelta(hours=1)
        ambassador_job.save(update_fields=["reminder_sent_at"])

        sent = send_upcoming_ambassador_event_3h_reminders()

        assert sent == 1
        mock_send.assert_called_once()
        ambassador_job.refresh_from_db()
        assert ambassador_job.reminder_sent_at is None

    @patch("jobs.tasks.AmbassadorEventReminderMailer.send")
    @patch("jobs.tasks.timezone.now")
    def test_does_not_send_24h_when_event_is_within_3_hours(self, mock_now, mock_send):
        now_utc = datetime(2026, 3, 18, 12, 0, tzinfo=dt_timezone.utc)
        mock_now.return_value = now_utc
        ambassador_job = self._build_ambassador_job(
            event_start_time=now_utc + timedelta(hours=2, minutes=30)
        )

        sent = send_upcoming_ambassador_event_reminders()

        assert sent == 0
        mock_send.assert_not_called()
        ambassador_job.refresh_from_db()
        assert ambassador_job.reminder_sent_at is None

    @patch("jobs.tasks.AmbassadorEventReminderMailer.send")
    @patch("jobs.tasks.timezone.now")
    def test_does_not_send_twice_if_reminder_was_already_sent(self, mock_now, mock_send):
        now_utc = datetime(2026, 3, 18, 12, 0, tzinfo=dt_timezone.utc)
        mock_now.return_value = now_utc
        ambassador_job = self._build_ambassador_job(
            event_start_time=now_utc + timedelta(hours=4)
        )
        ambassador_job.reminder_sent_at = now_utc - timedelta(minutes=10)
        ambassador_job.save(update_fields=["reminder_sent_at"])

        sent = send_upcoming_ambassador_event_reminders()

        assert sent == 0
        mock_send.assert_not_called()

    @patch("jobs.tasks.AmbassadorEventReminderMailer.send")
    @patch("jobs.tasks.timezone.now")
    def test_does_not_send_when_status_is_not_approved(self, mock_now, mock_send):
        now_utc = datetime(2026, 3, 18, 12, 0, tzinfo=dt_timezone.utc)
        mock_now.return_value = now_utc
        ambassador_job = self._build_ambassador_job(
            event_start_time=now_utc + timedelta(hours=4)
        )
        pending_status = self.create_status(
            name="Pending",
            slug="pending",
            tenant=self.tenant,
        )
        ambassador_job.status = pending_status
        ambassador_job.save(update_fields=["status"])

        sent = send_upcoming_ambassador_event_reminders()

        assert sent == 0
        mock_send.assert_not_called()
