from unittest.mock import patch

import pytest
from asgiref.sync import sync_to_async
import strawberry_django  # noqa: F401

from jobs.tests.base import JobsGraphQLTestCase
from recaps import models as recap_models
from recaps.envelopes import RecapApprovedNotificationMailer


@pytest.mark.django_db(transaction=True)
class TestApproveRecapNotifications(JobsGraphQLTestCase):
    @pytest.fixture(autouse=True)
    def setup(self, db):
        from config.schema_spark import schema_spark

        self.roles = self.setup_default_roles()
        self.tenant = self.create_tenant(name="Recap Tenant")

        self.spark_user = self.create_user(
            username="spark_recap@test.com",
            email="spark_recap@test.com",
            role=self.roles["spark_admin"],
            password="testpass123",
        )
        self.create_tenanted_user(user=self.spark_user, tenant=self.tenant)

        self.rmm_user = self.create_user(
            username="rmm_recap@test.com",
            email="rmm_recap@test.com",
            first_name="Rosa",
            role=self.roles["client"],
            password="testpass123",
        )
        self.create_tenanted_user(user=self.rmm_user, tenant=self.tenant)

        self.event = self.create_event(
            name="Recap Event",
            tenant=self.tenant,
            address="123 Recap St",
            rmm_asigned=self.rmm_user,
        )
        self.job_title = self.create_job_title(name="BA", tenant=self.tenant)
        self.job = self.create_job(
            name="Recap Job",
            code="RECAP-JOB-001",
            address="456 Activation Ave",
            event=self.event,
            job_title=self.job_title,
            tenant=self.tenant,
        )

        self.ambassador_user = self.create_user(
            username="recap_amb@test.com",
            email="recap_amb@test.com",
            role=self.roles["ambassador"],
            password="testpass123",
        )
        self.ambassador = self.create_ambassador(user=self.ambassador_user)
        self.create_tenanted_user(user=self.ambassador_user, tenant=self.tenant)

        self.recap = recap_models.Recap.objects.create(
            name="Post activation recap",
            approved=False,
            event=self.event,
            job=self.job,
            ambassador=self.ambassador,
            created_by=self.spark_user,
            updated_by=self.spark_user,
        )

        self.schema = schema_spark
        self.endpoint_path = "/api/v1/graphql/spark"

    @pytest.mark.asyncio
    async def test_approve_recap_sends_notification_email(self):
        mutation = """
        mutation ApproveRecap($input: ApproveRecapInput!) {
            approveRecap(input: $input) {
                success
                message
                recap {
                    id
                    approved
                }
            }
        }
        """
        variables = {
            "input": {
                "id": str(self.recap.id),
                "approved": True,
            }
        }

        with patch("recaps.mutations.RecapApprovedNotificationMailer.send") as mock_send:
            result = await self._execute_mutation_authenticated(
                mutation,
                variables,
                self.spark_user,
                self.endpoint_path,
            )

        assert result.data is not None
        assert result.data["approveRecap"]["success"] is True
        assert result.data["approveRecap"]["recap"]["approved"] is True
        assert mock_send.called

    @pytest.mark.asyncio
    async def test_recap_approved_mailer_template_renders(self):
        self.recap.approved = True
        await sync_to_async(self.recap.save)()
        recap = await sync_to_async(
            recap_models.Recap.objects.select_related(
                "event",
                "event__tenant",
                "job",
                "retailer",
                "timezone",
                "ambassador",
            ).get
        )(id=self.recap.id)

        mailer = RecapApprovedNotificationMailer(
            recap=recap,
            to_emails=[self.rmm_user.email],
            recipient_first_name=self.rmm_user.first_name,
            reply_to_email=self.rmm_user.email,
        )
        envelope = mailer.envelope()
        rendered_html = envelope.render_template()

        assert envelope.template == "recaps.templates.emails.recap_approved_notification"
        assert envelope.to_emails == [self.rmm_user.email]
        assert "Activation Summary" in rendered_html
