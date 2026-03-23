from datetime import datetime, timezone

import pytest

from events.models import TimeZone
from jobs.notification_rules import should_send_ambassador_event_email
from jobs.tests.base import JobsGraphQLTestCase


@pytest.mark.django_db(transaction=True)
class TestAmbassadorEmailNotificationRules(JobsGraphQLTestCase):
    @pytest.fixture(autouse=True)
    def setup(self):
        self.roles = self.setup_default_roles()
        self.tenant = self.create_tenant(name="Notification Rule Tenant")
        self.system_user = self.get_system_user()

        self.job_title = self.create_job_title(name="Brand Ambassador", tenant=self.tenant)
        self.rate_type = self.create_rate_type(name="Hourly", tenant=self.tenant)
        self.rate = self.create_rate(amount=35.0, rate_type=self.rate_type, tenant=self.tenant)
        self.status = self.create_status(name="Assigned", slug="assigned", tenant=self.tenant)

        self.ambassador_user = self.create_user(
            username="rule_ambassador@test.com",
            email="rule_ambassador@test.com",
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
            name="Rule Event",
            tenant=self.tenant,
            address="123 Main St",
            start_time=event_start_time,
            timezone=self.tz_minus_five,
        )
        job = self.create_job(
            name="Rule Job",
            code="RULE-001",
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

    def test_allows_email_for_event_happening_today(self):
        now_utc = datetime(2026, 3, 21, 12, 0, tzinfo=timezone.utc)
        ambassador_job = self._build_ambassador_job(
            event_start_time=datetime(2026, 3, 21, 23, 0, tzinfo=timezone.utc)
        )

        assert should_send_ambassador_event_email(ambassador_job, now=now_utc) is True

    def test_blocks_email_for_event_before_today(self):
        now_utc = datetime(2026, 3, 21, 12, 0, tzinfo=timezone.utc)
        ambassador_job = self._build_ambassador_job(
            event_start_time=datetime(2026, 3, 20, 23, 0, tzinfo=timezone.utc)
        )

        assert should_send_ambassador_event_email(ambassador_job, now=now_utc) is False
