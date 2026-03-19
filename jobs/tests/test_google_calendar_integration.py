from unittest.mock import patch, ANY
import pytest
from django.utils import timezone
from jobs.tests.base import JobsGraphQLTestCase
from jobs.mutations import _create_calendar_event_for_approved_job

@pytest.mark.django_db(transaction=True)
class TestGoogleCalendarIntegration(JobsGraphQLTestCase):
    @pytest.fixture(autouse=True)
    def setup(self, db):
        self.roles = self.setup_default_roles()
        self.tenant = self.create_tenant(name="Test Company")

        self.event = self.create_event(
            name="Activation Campaign",
            tenant=self.tenant,
            address="123 Test St",
        )
        self.job = self.create_job(
            name="In-Store Sampling",
            code="JOB-APV-001",
            address="123 Test St",
            event=self.event,
            tenant=self.tenant,
            start_date=timezone.now(),
            end_date=timezone.now() + timezone.timedelta(hours=2)
        )

        self.ambassador_user = self.create_user(
            username="amb_cal@test.com",
            email="amb_cal@test.com",
            role=self.roles["ambassador"],
            password="testpass123",
            first_name="Test",
            last_name="Ambassador",
        )
        self.create_tenanted_user(user=self.ambassador_user, tenant=self.tenant)
        self.ambassador = self.create_ambassador(user=self.ambassador_user)

        self.pending_status = self.create_status(name="Pending", tenant=self.tenant)
        self.rate_type = self.create_rate_type(name="Hourly", tenant=self.tenant)
        self.rate = self.create_rate(amount=55.0, rate_type=self.rate_type, tenant=self.tenant)

        self.ambassador_job = self.create_ambassador_job(
            ambassador=self.ambassador,
            job=self.job,
            status=self.pending_status,
            rate=self.rate,
            tenant=self.tenant,
        )

    @pytest.mark.asyncio
    async def test_create_calendar_event_for_approved_job(self):
        with patch("jobs.mutations.GoogleCalendarService") as MockCalendarService:
            mock_service_instance = MockCalendarService.return_value
            mock_service_instance.create_event.return_value = {"htmlLink": "http://example.com/event"}

            await _create_calendar_event_for_approved_job(self.ambassador_job)

            mock_service_instance.create_event.assert_called_once_with(
                summary="[Test Company] In-Store Sampling - Test Ambassador",
                description=ANY,
                location="123 Test St",
                start_time=self.job.start_date,
                end_time=self.job.end_date,
                timezone=ANY,
                attendees=["amb_cal@test.com"]
            )
