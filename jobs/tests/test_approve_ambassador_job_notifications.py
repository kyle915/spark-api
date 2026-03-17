from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import pytest
from asgiref.sync import sync_to_async

from jobs.tests.base import JobsGraphQLTestCase
from jobs import models
from jobs.envelopes import (
    AmbassadorApprovedForJobMailer,
    AmbassadorJobApprovedNotificationMailer,
)


@pytest.mark.django_db(transaction=True)
class TestApproveAmbassadorJobNotifications(JobsGraphQLTestCase):
    @pytest.fixture(autouse=True)
    def setup(self, db):
        from config.schema_spark import schema_spark

        self.roles = self.setup_default_roles()
        self.tenant = self.create_tenant(name="Test Company")

        self.spark_user = self.create_user(
            username="spark_approve@test.com",
            email="spark_approve@test.com",
            role=self.roles["spark_admin"],
            password="testpass123",
        )
        self.create_tenanted_user(user=self.spark_user, tenant=self.tenant)

        self.rmm_user = self.create_user(
            username="rmm@test.com",
            email="rmm@test.com",
            first_name="Rosa",
            role=self.roles["client"],
            password="testpass123",
        )
        self.create_tenanted_user(user=self.rmm_user, tenant=self.tenant)

        self.event = self.create_event(
            name="Activation Campaign",
            tenant=self.tenant,
            address="123 Test St",
            rmm_asigned=self.rmm_user,
            notes="Wear black pants and arrive 15 minutes early.",
        )
        self.job_title = self.create_job_title(name="Brand Ambassador", tenant=self.tenant)
        self.job = self.create_job(
            name="In-Store Sampling",
            code="JOB-APV-001",
            address="456 Market Ave",
            event=self.event,
            job_title=self.job_title,
            tenant=self.tenant,
            start_date=datetime(2026, 3, 20, 18, 0, tzinfo=timezone.utc),
            end_date=datetime(2026, 3, 20, 22, 0, tzinfo=timezone.utc),
        )

        self.ambassador_user = self.create_user(
            username="amb_notification@test.com",
            email="amb_notification@test.com",
            role=self.roles["ambassador"],
            password="testpass123",
        )
        self.ambassador = self.create_ambassador(user=self.ambassador_user)
        self.create_tenanted_user(user=self.ambassador_user, tenant=self.tenant)

        self.pending_status = self.create_status(name="Pending", tenant=self.tenant)
        self.approved_status = self.create_status(name="Approved", tenant=self.tenant)
        self.rate_type = self.create_rate_type(name="Hourly", tenant=self.tenant)
        self.rate = self.create_rate(amount=55.0, rate_type=self.rate_type, tenant=self.tenant)

        self.ambassador_job = self.create_ambassador_job(
            ambassador=self.ambassador,
            job=self.job,
            status=self.pending_status,
            rate=self.rate,
            tenant=self.tenant,
        )

        self.schema = schema_spark
        self.endpoint_path = "/api/v1/graphql/spark"

    @pytest.mark.asyncio
    async def test_approve_ambassador_job_sends_notification_email(self):
        mutation = """
        mutation ApproveAmbassadorJob($input: ApproveAmbassadorJobInput!) {
            approveAmbassadorJob(input: $input) {
                success
                message
                ambassadorJob {
                    id
                    status {
                        name
                    }
                }
            }
        }
        """
        variables = {
            "input": {
                "tenantId": str(self.tenant.id),
                "ambassadorJobId": str(self.ambassador_job.id),
            }
        }

        with patch(
            "jobs.mutations.AmbassadorJobApprovedNotificationMailer.send"
        ) as mock_client_send, patch(
            "jobs.mutations.AmbassadorApprovedForJobMailer.send"
        ) as mock_send, patch(
            "jobs.mutations.one_signal_client.send_push",
            new=AsyncMock(return_value={"id": "push-123"}),
        ) as mock_push:
            result = await self._execute_mutation_authenticated(
                mutation,
                variables,
                self.spark_user,
                self.endpoint_path,
            )

        assert result.data is not None
        assert result.data["approveAmbassadorJob"]["success"] is True
        assert result.data["approveAmbassadorJob"]["ambassadorJob"]["status"]["name"].lower() == "approved"
        assert mock_client_send.called
        assert mock_send.called
        mock_push.assert_awaited_once()
        kwargs = mock_push.await_args.kwargs
        assert kwargs["external_ids"] == [str(self.ambassador_user.uuid)]
        assert kwargs["data"]["type"] == "job_application_accepted"

        updated_job = await sync_to_async(models.AmbassadorJob.objects.get)(pk=self.ambassador_job.id)
        assert updated_job.status_id == self.approved_status.id

    @pytest.mark.asyncio
    async def test_approved_mailer_template_renders(self):
        self.ambassador_job.status = self.approved_status
        await sync_to_async(self.ambassador_job.save)()
        ambassador_job = await sync_to_async(
            models.AmbassadorJob.objects.select_related(
                "job",
                "job__event",
                "job__event__timezone",
                "tenant",
            ).get
        )(pk=self.ambassador_job.id)

        mailer = AmbassadorJobApprovedNotificationMailer(
            ambassador_job=ambassador_job,
            to_emails=[self.rmm_user.email],
            recipient_first_name=self.rmm_user.first_name,
            reply_to_email=self.rmm_user.email,
        )
        envelope = mailer.envelope()
        rendered_html = envelope.render_template()

        assert envelope.template == "jobs.templates.emails.ambassador_job_approved_notification"
        assert envelope.to_emails == [self.rmm_user.email]
        assert "activation is fully staffed and confirmed" in rendered_html

    @pytest.mark.asyncio
    async def test_approved_ambassador_mailer_template_renders(self):
        self.ambassador_job.status = self.approved_status
        await sync_to_async(self.ambassador_job.save)()
        ambassador_job = await sync_to_async(
            models.AmbassadorJob.objects.select_related(
                "job",
                "job__event",
                "job__event__timezone",
                "ambassador",
                "ambassador__user",
                "tenant",
            ).get
        )(pk=self.ambassador_job.id)

        mailer = AmbassadorApprovedForJobMailer(
            ambassador_job=ambassador_job,
            to_emails=[self.ambassador_user.email],
            recipient_first_name=self.ambassador_user.first_name,
        )
        envelope = mailer.envelope()
        rendered_html = envelope.render_template()

        assert envelope.template == "jobs.templates.emails.ambassador_approved_for_job"
        assert envelope.to_emails == [self.ambassador_user.email]
        assert "You have been approved for this job" in rendered_html
        assert "Wear black pants and arrive 15 minutes early." in rendered_html
