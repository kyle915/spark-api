from datetime import datetime, timezone
from unittest.mock import patch

import pytest
from asgiref.sync import sync_to_async

from jobs.tests.base import JobsGraphQLTestCase


@pytest.mark.django_db(transaction=True)
class TestCreateAmbassadorJobNotifications(JobsGraphQLTestCase):
    @pytest.fixture(autouse=True)
    def setup(self, db):
        from config.schema_spark import schema_spark

        self.roles = self.setup_default_roles()
        self.tenant = self.create_tenant(name="Test Company")

        self.spark_user = self.create_user(
            username="spark_create_aj@test.com",
            email="spark_create_aj@test.com",
            role=self.roles["spark_admin"],
            password="testpass123",
        )
        self.create_tenanted_user(user=self.spark_user, tenant=self.tenant)

        self.event = self.create_event(
            name="Create Ambassador Job Event",
            tenant=self.tenant,
            address="123 Test St",
            notes="Bring branded shirt.",
        )
        self.job_title = self.create_job_title(name="Brand Ambassador", tenant=self.tenant)
        self.rate_type = self.create_rate_type(name="Hourly", tenant=self.tenant)
        self.rate = self.create_rate(amount=55.0, rate_type=self.rate_type, tenant=self.tenant)
        self.job = self.create_job(
            name="In-Store Sampling",
            code="JOB-CREATE-001",
            address="456 Market Ave",
            event=self.event,
            job_title=self.job_title,
            tenant=self.tenant,
            rate=self.rate,
            start_date=datetime(2026, 3, 20, 18, 0, tzinfo=timezone.utc),
            end_date=datetime(2026, 3, 20, 22, 0, tzinfo=timezone.utc),
        )

        self.ambassador_user = self.create_user(
            username="amb_create_notification@test.com",
            email="amb_create_notification@test.com",
            role=self.roles["ambassador"],
            password="testpass123",
        )
        self.ambassador = self.create_ambassador(user=self.ambassador_user)
        self.create_tenanted_user(user=self.ambassador_user, tenant=self.tenant)

        self.pending_status = self.create_status(name="Pending", tenant=self.tenant)
        self.approved_status = self.create_status(
            name="Approved", tenant=self.tenant, slug="approved"
        )

        self.schema = schema_spark
        self.endpoint_path = "/api/v1/graphql/spark"

    @pytest.mark.asyncio
    async def test_create_ambassador_job_sends_ambassador_email(self):
        mutation = """
        mutation CreateAmbassadorJob($input: CreateAmbassadorJobInput!) {
            createAmbassadorJob(input: $input) {
                success
                message
                ambassadorJob {
                    id
                }
            }
        }
        """
        variables = {
            "input": {
                "tenantId": str(self.tenant.id),
                "ambassadorId": str(self.ambassador.id),
                "jobId": str(self.job.id),
                "statusId": str(self.pending_status.id),
                "rateId": str(self.rate.id),
                "appearAsRfp": True,
            }
        }

        with patch(
            "jobs.notification_rules.timezone.now",
            return_value=datetime(2026, 3, 20, 12, 0, tzinfo=timezone.utc),
        ), patch("jobs.mutations.AmbassadorAssignedToJobMailer.send") as mock_send, patch(
            "ambassadors.push._send_push_to_user_sync"
        ):
            result = await self._execute_mutation_authenticated(
                mutation,
                variables,
                self.spark_user,
                self.endpoint_path,
            )

        assert result.errors is None
        assert result.data is not None
        assert result.data["createAmbassadorJob"]["success"] is True
        assert mock_send.called

    @pytest.mark.asyncio
    async def test_create_ambassador_job_does_not_send_email_for_past_event(self):
        mutation = """
        mutation CreateAmbassadorJob($input: CreateAmbassadorJobInput!) {
            createAmbassadorJob(input: $input) {
                success
                message
                ambassadorJob {
                    id
                }
            }
        }
        """
        variables = {
            "input": {
                "tenantId": str(self.tenant.id),
                "ambassadorId": str(self.ambassador.id),
                "jobId": str(self.job.id),
                "statusId": str(self.pending_status.id),
                "rateId": str(self.rate.id),
                "appearAsRfp": True,
            }
        }

        with patch(
            "jobs.notification_rules.timezone.now",
            return_value=datetime(2026, 3, 21, 12, 0, tzinfo=timezone.utc),
        ), patch("jobs.mutations.AmbassadorAssignedToJobMailer.send") as mock_send, patch(
            "ambassadors.push._send_push_to_user_sync"
        ):
            result = await self._execute_mutation_authenticated(
                mutation,
                variables,
                self.spark_user,
                self.endpoint_path,
            )

        assert result.errors is None
        assert result.data is not None
        assert result.data["createAmbassadorJob"]["success"] is True
        mock_send.assert_not_called()

    @pytest.mark.asyncio
    async def test_create_ambassador_job_duplicate_assignment_fails(self):
        mutation = """
        mutation CreateAmbassadorJob($input: CreateAmbassadorJobInput!) {
            createAmbassadorJob(input: $input) {
                success
                message
                ambassadorJob {
                    id
                }
            }
        }
        """
        variables = {
            "input": {
                "tenantId": str(self.tenant.id),
                "ambassadorId": str(self.ambassador.id),
                "jobId": str(self.job.id),
                "statusId": str(self.pending_status.id),
                "rateId": str(self.rate.id),
                "appearAsRfp": True,
            }
        }

        with patch("jobs.mutations.AmbassadorAssignedToJobMailer.send"), patch(
            "ambassadors.push._send_push_to_user_sync"
        ):
            first_result = await self._execute_mutation_authenticated(
                mutation,
                variables,
                self.spark_user,
                self.endpoint_path,
            )
            assert first_result.errors is None
            assert first_result.data["createAmbassadorJob"]["success"] is True

            second_result = await self._execute_mutation_authenticated(
                mutation,
                variables,
                self.spark_user,
                self.endpoint_path,
            )
        assert second_result.errors is None
        assert second_result.data["createAmbassadorJob"]["success"] is False
        assert "already assigned to this job" in second_result.data["createAmbassadorJob"][
            "message"
        ].lower()

    @pytest.mark.asyncio
    async def test_create_ambassador_job_creates_approved_booking(self):
        """Regression: admin direct-assign (createAmbassadorJob) IS the hire,
        so it must create an ``is_approved=True`` AmbassadorEvent. The BA's
        upcoming-shifts + clock-in both gate on AmbassadorEvent.is_approved=True
        — before the fix the booking was created is_approved=False, so a BA who
        was hired this way never saw the gig and couldn't clock in.

        (The is_approved=True → surfaces-in-myUpcomingShifts + clock-in mechanic
        is covered end-to-end by test_assign_creates_booking; here we assert the
        booking row this entry point produces is approved.)
        """
        from ambassadors.models import AmbassadorEvent

        mutation = """
        mutation CreateAmbassadorJob($input: CreateAmbassadorJobInput!) {
            createAmbassadorJob(input: $input) {
                success
                message
            }
        }
        """
        variables = {
            "input": {
                "tenantId": str(self.tenant.id),
                "ambassadorId": str(self.ambassador.id),
                "jobId": str(self.job.id),
                "statusId": str(self.pending_status.id),
                "rateId": str(self.rate.id),
                "appearAsRfp": True,
            }
        }

        # Email + push are best-effort side-effects; stub them so the test
        # doesn't reach the mail driver / the push's asyncio.run path.
        with patch("jobs.mutations.AmbassadorAssignedToJobMailer.send"), patch(
            "ambassadors.push._send_push_to_user_sync"
        ):
            result = await self._execute_mutation_authenticated(
                mutation,
                variables,
                self.spark_user,
                self.endpoint_path,
            )

        assert result.errors is None, f"errors: {result.errors}"
        assert result.data["createAmbassadorJob"]["success"] is True

        booking = await sync_to_async(
            lambda: AmbassadorEvent.objects.filter(
                ambassador=self.ambassador, event=self.event
            ).first()
        )()
        assert booking is not None, "expected an AmbassadorEvent booking row"
        assert await sync_to_async(lambda: booking.is_approved)() is True, (
            "the hire booking must be is_approved=True or the BA can't see / "
            "clock into the shift"
        )
        assert await sync_to_async(lambda: booking.tenant_id)() == self.tenant.id
