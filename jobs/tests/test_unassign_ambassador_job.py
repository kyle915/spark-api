"""
Tests for unassign_ambassador_job mutation in the jobs app.
"""
import pytest
from asgiref.sync import sync_to_async
from django.contrib.auth.models import AnonymousUser
from strawberry.relay import to_base64
from unittest.mock import patch

from jobs import models
from jobs.tests.base import JobsGraphQLTestCase


@pytest.mark.django_db(transaction=True)
class TestClientUnassignAmbassadorJob(JobsGraphQLTestCase):
    @pytest.fixture(autouse=True)
    def setup(self, db):
        from config.schema_client import schema_clients

        self.roles = self.setup_default_roles()
        self.tenant = self.create_tenant(name="Unassign Tenant")

        self.client_user = self.create_user(
            username="client_unassign@test.com",
            email="client_unassign@test.com",
            role=self.roles["client"],
            password="testpass123",
        )
        self.create_tenanted_user(user=self.client_user, tenant=self.tenant)

        self.ambassador_user = self.create_user(
            username="ambassador_unassign@test.com",
            email="ambassador_unassign@test.com",
            role=self.roles["ambassador"],
            password="testpass123",
        )
        self.create_tenanted_user(user=self.ambassador_user, tenant=self.tenant)
        self.ambassador = self.create_ambassador(user=self.ambassador_user)

        self.event = self.create_event(
            name="Test Event",
            tenant=self.tenant,
            address="123 Test St",
        )
        self.job_title = self.create_job_title(
            name="Brand Ambassador",
            tenant=self.tenant,
        )
        self.rate_type = self.create_rate_type(
            name="Hourly",
            tenant=self.tenant,
        )
        self.rate = self.create_rate(
            amount=25.0,
            rate_type=self.rate_type,
            tenant=self.tenant,
        )
        self.status = self.create_status(
            name="Assigned",
            tenant=self.tenant,
            slug="assigned",
        )
        self.job = self.create_job(
            name="Sampling Job",
            code="JOB-UNASSIGN",
            address="456 Main St",
            event=self.event,
            job_title=self.job_title,
            tenant=self.tenant,
            rate=self.rate,
        )
        self.ambassador_job = self.create_ambassador_job(
            ambassador=self.ambassador,
            job=self.job,
            status=self.status,
            rate=self.rate,
            tenant=self.tenant,
        )

        self.schema = schema_clients
        self.endpoint_path = "/api/v1/graphql/clients"
        self.mutation = """
            mutation UnassignAmbassadorJob($input: UnassignAmbassadorJobInput!) {
                unassignAmbassadorJob(input: $input) {
                    success
                    message
                    clientMutationId
                }
            }
        """

    @pytest.mark.asyncio
    async def test_unassign_ambassador_job_success(self):
        variables = {
            "input": {
                "ambassadorJobId": to_base64("AmbassadorJob", self.ambassador_job.id),
                "clientMutationId": "unassign-1",
            }
        }

        with patch("jobs.mutations.AmbassadorUnassignedFromJobMailer.send") as mock_send:
            result = await self._execute_mutation_authenticated(
                self.mutation,
                variables,
                self.client_user,
                self.endpoint_path,
            )

        assert result.errors is None
        assert result.data is not None
        assert result.data["unassignAmbassadorJob"]["success"] is True
        assert result.data["unassignAmbassadorJob"]["message"] == "Ambassador unassigned from job."
        assert result.data["unassignAmbassadorJob"]["clientMutationId"] == "unassign-1"
        assert mock_send.called

        exists = await sync_to_async(models.AmbassadorJob.objects.filter(
            id=self.ambassador_job.id
        ).exists)()
        assert exists is False

    @pytest.mark.asyncio
    async def test_unassign_ambassador_job_not_found(self):
        variables = {
            "input": {
                "ambassadorJobId": to_base64("AmbassadorJob", 999999),
            }
        }

        result = await self._execute_mutation_authenticated(
            self.mutation,
            variables,
            self.client_user,
            self.endpoint_path,
        )

        assert result.errors is None
        assert result.data is not None
        assert result.data["unassignAmbassadorJob"]["success"] is False
        assert result.data["unassignAmbassadorJob"]["message"] == "Ambassador job not found."

    @pytest.mark.asyncio
    async def test_unassign_ambassador_job_unauthorized_ambassador(self):
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

        assert result.data is None
        assert result.errors is not None

    @pytest.mark.asyncio
    async def test_unassign_ambassador_job_unauthorized_anonymous(self):
        variables = {
            "input": {
                "ambassadorJobId": to_base64("AmbassadorJob", self.ambassador_job.id),
            }
        }

        result = await self._execute_mutation_authenticated(
            self.mutation,
            variables,
            AnonymousUser(),
            self.endpoint_path,
        )

        assert result.data is None
        assert result.errors is not None
