import pytest
import strawberry_django  # noqa: F401
from asgiref.sync import sync_to_async
from strawberry.relay import to_base64

from jobs.tests.base import JobsGraphQLTestCase
from jobs import models


@pytest.mark.django_db(transaction=True)
class TestAcceptAmbassadorJobInvitation(JobsGraphQLTestCase):
    @pytest.fixture(autouse=True)
    def setup(self, db):
        from config.schema_ambassador import schema_ambassador

        self.roles = self.setup_default_roles()
        self.tenant = self.create_tenant(name="Accept Invitation Tenant")

        self.ambassador_user = self.create_user(
            username="accept_invite_ambassador@test.com",
            email="accept_invite_ambassador@test.com",
            role=self.roles["ambassador"],
            password="testpass123",
        )
        self.ambassador = self.create_ambassador(user=self.ambassador_user)
        self.create_tenanted_user(user=self.ambassador_user, tenant=self.tenant)

        self.other_ambassador_user = self.create_user(
            username="other_accept_invite_ambassador@test.com",
            email="other_accept_invite_ambassador@test.com",
            role=self.roles["ambassador"],
            password="testpass123",
        )
        self.other_ambassador = self.create_ambassador(user=self.other_ambassador_user)
        self.create_tenanted_user(user=self.other_ambassador_user, tenant=self.tenant)

        self.event = self.create_event(
            name="Accept Invitation Event",
            tenant=self.tenant,
            address="123 Test St",
        )
        self.job_title = self.create_job_title(
            name="Promoter",
            tenant=self.tenant,
        )
        self.rate_type = self.create_rate_type(
            name="Hourly",
            tenant=self.tenant,
        )
        self.rate = self.create_rate(
            amount=75.0,
            rate_type=self.rate_type,
            tenant=self.tenant,
        )
        self.job = self.create_job(
            name="Accept Invitation Job",
            code="JOB-ACCEPT-INVITE",
            address="123 Test St",
            event=self.event,
            job_title=self.job_title,
            tenant=self.tenant,
            rate=self.rate,
        )

        self.invited_status = self.create_status(
            name="Invited",
            tenant=self.tenant,
        )

        self.ambassador_job = self.create_ambassador_job(
            ambassador=self.ambassador,
            job=self.job,
            status=self.invited_status,
            rate=self.rate,
            tenant=self.tenant,
        )
        self.other_ambassador_job = self.create_ambassador_job(
            ambassador=self.other_ambassador,
            job=self.job,
            status=self.invited_status,
            rate=self.rate,
            tenant=self.tenant,
        )

        self.schema = schema_ambassador
        self.endpoint_path = "/api/v1/graphql/ambassadors"
        self.mutation = """
            mutation AcceptAmbassadorJobInvitation($input: AcceptAmbassadorJobInvitationInput!) {
                acceptAmbassadorJobInvitation(input: $input) {
                    success
                    message
                    ambassadorJob {
                        id
                        status {
                            slug
                        }
                    }
                }
            }
        """

    @pytest.mark.asyncio
    async def test_accept_own_invited_job(self):
        variables = {
            "input": {
                "ambassadorJobId": to_base64("AmbassadorJob", self.ambassador_job.id),
            }
        }

        result = await self._execute_mutation_authenticated(
            self.mutation,
            variables,
            self.ambassador_user,
            self.endpoint_path,
        )

        assert result.errors is None
        assert result.data is not None
        assert result.data["acceptAmbassadorJobInvitation"]["success"] is True
        assert result.data["acceptAmbassadorJobInvitation"]["ambassadorJob"]["status"]["slug"] == "accepted"

        @sync_to_async
        def get_status_slug():
            return models.AmbassadorJob.objects.select_related("status").get(
                pk=self.ambassador_job.id
            ).status.slug

        assert await get_status_slug() == "accepted"

    @pytest.mark.asyncio
    async def test_cannot_accept_other_ambassador_invitation(self):
        variables = {
            "input": {
                "ambassadorJobId": to_base64("AmbassadorJob", self.other_ambassador_job.id),
            }
        }

        result = await self._execute_mutation_authenticated(
            self.mutation,
            variables,
            self.ambassador_user,
            self.endpoint_path,
        )

        assert result.errors is None
        assert result.data is not None
        assert result.data["acceptAmbassadorJobInvitation"]["success"] is False
        assert "own job invitations" in result.data["acceptAmbassadorJobInvitation"]["message"].lower()
