"""
Tests for invite_ambassadors_to_job mutation in the jobs app.

This module tests:
- invite_ambassadors_to_job mutation (Client and Spark schemas)
  - Successful invitation of ambassadors
  - Job validation (exists, has rate)
  - Ambassador validation (exists, belongs to tenant)
  - Automatic "invited" status creation
  - AmbassadorJob creation with correct fields
"""
import pytest
import strawberry_django  # noqa: F401
import uuid
from asgiref.sync import sync_to_async
from django.contrib.auth import get_user_model
from graphql import GraphQLError
from jobs.tests.base import JobsGraphQLTestCase
from jobs import models
from ambassadors import models as ambassador_models

User = get_user_model()


@pytest.mark.django_db(transaction=True)
class TestClientInviteAmbassadorsToJob(JobsGraphQLTestCase):
    """Tests for invite_ambassadors_to_job mutation (Client schema)."""

    @pytest.fixture(autouse=True)
    def setup(self, db):
        """Set up test data before each test."""
        from config.schema_client import schema_clients
        self.roles = self.setup_default_roles()
        self.tenant = self.create_tenant(name="Test Company")

        # Create client user
        unique_id = str(uuid.uuid4())[:8]
        self.client_user = self.create_user(
            username=f"client_invite_{unique_id}@test.com",
            email=f"client_invite_{unique_id}@test.com",
            role=self.roles['client'],
            password="testpass123"
        )
        self.create_tenanted_user(user=self.client_user, tenant=self.tenant)

        # Create prerequisite data
        self.event = self.create_event(
            name="Test Event",
            tenant=self.tenant,
            address="123 Test St"
        )
        self.job_title = self.create_job_title(
            name="Software Engineer",
            tenant=self.tenant
        )
        self.rate_type = self.create_rate_type(
            name="Hourly",
            tenant=self.tenant
        )
        self.rate = self.create_rate(
            amount=50.0,
            rate_type=self.rate_type,
            tenant=self.tenant
        )
        self.job = self.create_job(
            name="Test Job",
            code="JOB-001",
            address="123 Main St",
            event=self.event,
            job_title=self.job_title,
            tenant=self.tenant,
            rate=self.rate
        )

        # Create ambassadors
        self.ambassador_user1 = self.create_user(
            username=f"amb1_invite_{unique_id}@test.com",
            email=f"amb1_invite_{unique_id}@test.com",
            role=self.roles['ambassador'],
            password="testpass123"
        )
        self.create_tenanted_user(
            user=self.ambassador_user1, tenant=self.tenant)
        self.ambassador1 = self.create_ambassador(user=self.ambassador_user1)

        self.ambassador_user2 = self.create_user(
            username=f"amb2_invite_{unique_id}@test.com",
            email=f"amb2_invite_{unique_id}@test.com",
            role=self.roles['ambassador'],
            password="testpass123"
        )
        self.create_tenanted_user(
            user=self.ambassador_user2, tenant=self.tenant)
        self.ambassador2 = self.create_ambassador(user=self.ambassador_user2)

        self.schema = schema_clients
        self.endpoint_path = "/api/v1/graphql/clients"

        self.mutation = """
            mutation InviteAmbassadorsToJob($input: InviteAmbassadorsToJobInput!) {
                inviteAmbassadorsToJob(input: $input) {
                    success
                    message
                    clientMutationId
                    ambassadorJobs {
                        id
                        uuid
                        ambassador {
                            id
                            uuid
                            user {
                                email
                            }
                        }
                        job {
                            id
                            name
                        }
                        status {
                            id
                            name
                            slug
                        }
                        rate {
                            id
                            amount
                        }
                        appearAsRfp
                    }
                }
            }
        """

    @pytest.mark.asyncio
    async def test_invite_ambassadors_to_job_success(self):
        """Test successful invitation of ambassadors to job."""
        variables = {
            "input": {
                "tenantId": str(self.tenant.id),
                "jobId": str(self.job.id),
                "ambassadorIds": [str(self.ambassador1.id), str(self.ambassador2.id)],
                "clientMutationId": "invite-1"
            }
        }

        result = await self._execute_mutation_authenticated(
            self.mutation, variables, self.client_user, self.endpoint_path)

        assert result.errors is None
        assert result.data is not None
        assert result.data["inviteAmbassadorsToJob"]["success"] is True
        assert "invited" in result.data["inviteAmbassadorsToJob"]["message"].lower(
        )
        assert result.data["inviteAmbassadorsToJob"]["clientMutationId"] == "invite-1"

        # Verify ambassador jobs were created
        ambassador_jobs = result.data["inviteAmbassadorsToJob"]["ambassadorJobs"]
        assert len(ambassador_jobs) == 2

        # GraphQL returns Relay global IDs
        ambassador_global_ids = {aj["ambassador"]["id"]
                                 for aj in ambassador_jobs}
        # Check that both ambassadors are present (using their global IDs)
        assert len(ambassador_global_ids) == 2

        # Verify each ambassador job has correct fields
        # Job ID is returned as Relay global ID, not plain integer
        for aj in ambassador_jobs:
            assert aj["job"]["id"] is not None  # Job ID (Relay global ID)
            assert aj["status"]["slug"] == "invited"
            assert aj["rate"]["amount"] == 50.0
            assert aj["appearAsRfp"] is True

        # Verify in database
        @sync_to_async
        def check_ambassador_jobs():
            jobs = list(models.AmbassadorJob.objects.filter(
                job=self.job,
                ambassador__in=[self.ambassador1, self.ambassador2]
            ).select_related('status', 'rate', 'created_by', 'updated_by'))

            # Extract fields that might need sync access
            return [{
                'status_slug': aj.status.slug,
                'rate': aj.rate,
                'appear_as_rfp': aj.appear_as_rfp,
                'created_by': aj.created_by,
                'updated_by': aj.updated_by,
            } for aj in jobs]

        db_jobs = await check_ambassador_jobs()
        assert len(db_jobs) == 2
        for aj_data in db_jobs:
            assert aj_data['status_slug'] == "invited"
            assert aj_data['rate'] == self.rate
            assert aj_data['appear_as_rfp'] is True
            assert aj_data['created_by'] == self.client_user
            assert aj_data['updated_by'] == self.client_user

    @pytest.mark.asyncio
    async def test_invite_single_ambassador_to_job(self):
        """Test inviting a single ambassador to job."""
        variables = {
            "input": {
                "tenantId": str(self.tenant.id),
                "jobId": str(self.job.id),
                "ambassadorIds": [str(self.ambassador1.id)],
            }
        }

        result = await self._execute_mutation_authenticated(
            self.mutation, variables, self.client_user, self.endpoint_path)

        assert result.errors is None
        assert result.data is not None
        assert result.data["inviteAmbassadorsToJob"]["success"] is True
        assert len(result.data["inviteAmbassadorsToJob"]
                   ["ambassadorJobs"]) == 1

    @pytest.mark.asyncio
    async def test_invite_ambassadors_job_not_found(self):
        """Test invitation fails when job doesn't exist."""
        variables = {
            "input": {
                "tenantId": str(self.tenant.id),
                "jobId": "999999",
                "ambassadorIds": [str(self.ambassador1.id)],
            }
        }

        result = await self._execute_mutation_authenticated(
            self.mutation, variables, self.client_user, self.endpoint_path)

        assert result.errors is not None
        assert any("Job not found or has no rate" in str(error)
                   for error in result.errors)

    @pytest.mark.asyncio
    async def test_invite_ambassadors_job_without_rate(self):
        """Test invitation fails when job has no rate."""
        # Create job without rate
        @sync_to_async
        def create_job_without_rate():
            return self.create_job(
                name="Job Without Rate",
                code="JOB-NO-RATE",
                address="123 Test St",
                event=self.event,
                job_title=self.job_title,
                tenant=self.tenant,
                rate=None
            )

        job_without_rate = await create_job_without_rate()

        variables = {
            "input": {
                "tenantId": str(self.tenant.id),
                "jobId": str(job_without_rate.id),
                "ambassadorIds": [str(self.ambassador1.id)],
            }
        }

        result = await self._execute_mutation_authenticated(
            self.mutation, variables, self.client_user, self.endpoint_path)

        assert result.errors is not None
        assert any("Job not found or has no rate" in str(error)
                   for error in result.errors)

    @pytest.mark.asyncio
    async def test_invite_ambassadors_empty_list(self):
        """Test invitation with empty ambassador list returns empty result."""
        variables = {
            "input": {
                "tenantId": str(self.tenant.id),
                "jobId": str(self.job.id),
                "ambassadorIds": [],
            }
        }

        result = await self._execute_mutation_authenticated(
            self.mutation, variables, self.client_user, self.endpoint_path)

        assert result.errors is None
        assert result.data is not None
        assert result.data["inviteAmbassadorsToJob"]["success"] is True
        assert result.data["inviteAmbassadorsToJob"]["ambassadorJobs"] == []

    @pytest.mark.asyncio
    async def test_invite_ambassadors_not_found(self):
        """Test invitation with non-existent ambassador IDs returns empty result."""
        variables = {
            "input": {
                "tenantId": str(self.tenant.id),
                "jobId": str(self.job.id),
                "ambassadorIds": ["999999", "999998"],
            }
        }

        result = await self._execute_mutation_authenticated(
            self.mutation, variables, self.client_user, self.endpoint_path)

        assert result.errors is None
        assert result.data is not None
        assert result.data["inviteAmbassadorsToJob"]["success"] is True
        # When no ambassadors are found, returns empty list
        assert result.data["inviteAmbassadorsToJob"]["ambassadorJobs"] == []

    @pytest.mark.asyncio
    async def test_invite_ambassadors_creates_invited_status(self):
        """Test that 'invited' status is created if it doesn't exist."""
        # Ensure no invited status exists
        @sync_to_async
        def remove_invited_status():
            models.Status.objects.filter(
                slug="invited", tenant_id=self.tenant.id).delete()

        await remove_invited_status()

        variables = {
            "input": {
                "tenantId": str(self.tenant.id),
                "jobId": str(self.job.id),
                "ambassadorIds": [str(self.ambassador1.id)],
            }
        }

        result = await self._execute_mutation_authenticated(
            self.mutation, variables, self.client_user, self.endpoint_path)

        assert result.errors is None
        assert result.data is not None
        assert result.data["inviteAmbassadorsToJob"]["success"] is True

        # Verify invited status was created
        @sync_to_async
        def check_status():
            return models.Status.objects.filter(
                slug="invited", tenant_id=self.tenant.id).exists()

        status_exists = await check_status()
        assert status_exists is True

        # Verify ambassador job has invited status
        @sync_to_async
        def check_aj_status():
            aj = models.AmbassadorJob.objects.filter(
                job=self.job,
                ambassador=self.ambassador1
            ).first()
            return aj.status.slug if aj else None

        status_slug = await check_aj_status()
        assert status_slug == "invited"

    @pytest.mark.asyncio
    async def test_invite_ambassadors_unauthorized_ambassador(self):
        """Test ambassador users cannot invite ambassadors."""
        @sync_to_async
        def create_ambassador_user():
            ambassador_user = self.create_user(
                username=f"amb_test_{uuid.uuid4().hex[:8]}@test.com",
                email=f"amb_test_{uuid.uuid4().hex[:8]}@test.com",
                role=self.roles['ambassador'],
                password="testpass123"
            )
            self.create_tenanted_user(user=ambassador_user, tenant=self.tenant)
            return ambassador_user

        ambassador_user = await create_ambassador_user()

        variables = {
            "input": {
                "tenantId": str(self.tenant.id),
                "jobId": str(self.job.id),
                "ambassadorIds": [str(self.ambassador1.id)],
            }
        }

        result = await self._execute_mutation_authenticated(
            self.mutation, variables, ambassador_user, self.endpoint_path)

        assert result.data is None
        assert result.errors is not None
        assert len(result.errors) > 0

    @pytest.mark.asyncio
    async def test_invite_ambassadors_unauthorized_anonymous(self):
        """Test anonymous users cannot invite ambassadors."""
        from django.contrib.auth.models import AnonymousUser

        variables = {
            "input": {
                "tenantId": str(self.tenant.id),
                "jobId": str(self.job.id),
                "ambassadorIds": [str(self.ambassador1.id)],
            }
        }

        result = await self._execute_mutation_authenticated(
            self.mutation, variables, AnonymousUser(), self.endpoint_path)

        assert result.data is None
        assert result.errors is not None

    @pytest.mark.asyncio
    async def test_invite_ambassadors_mixed_valid_invalid(self):
        """Test invitation with mix of valid and invalid ambassador IDs."""
        # Create third ambassador in same tenant
        @sync_to_async
        def create_third_ambassador():
            unique_id = str(uuid.uuid4())[:8]
            ambassador_user3 = self.create_user(
                username=f"amb3_invite_{unique_id}@test.com",
                email=f"amb3_invite_{unique_id}@test.com",
                role=self.roles['ambassador'],
                password="testpass123"
            )
            self.create_tenanted_user(
                user=ambassador_user3, tenant=self.tenant)
            ambassador3 = self.create_ambassador(user=ambassador_user3)
            return ambassador3

        ambassador3 = await create_third_ambassador()

        variables = {
            "input": {
                "tenantId": str(self.tenant.id),
                "jobId": str(self.job.id),
                "ambassadorIds": [
                    str(self.ambassador1.id),
                    "999999",  # Non-existent
                    str(ambassador3.id),
                    "999998",  # Non-existent
                ],
            }
        }

        result = await self._execute_mutation_authenticated(
            self.mutation, variables, self.client_user, self.endpoint_path)

        assert result.errors is None
        assert result.data is not None
        assert result.data["inviteAmbassadorsToJob"]["success"] is True

        # Should only create jobs for valid ambassadors
        ambassador_jobs = result.data["inviteAmbassadorsToJob"]["ambassadorJobs"]
        assert len(ambassador_jobs) == 2

        # GraphQL returns Relay global IDs
        ambassador_global_ids = {aj["ambassador"]["id"]
                                 for aj in ambassador_jobs}
        assert len(ambassador_global_ids) == 2


@pytest.mark.django_db(transaction=True)
class TestSparkInviteAmbassadorsToJob(JobsGraphQLTestCase):
    """Tests for invite_ambassadors_to_job mutation (Spark schema)."""

    @pytest.fixture(autouse=True)
    def setup(self, db):
        """Set up test data before each test."""
        from config.schema_spark import schema_spark
        self.roles = self.setup_default_roles()
        self.tenant = self.create_tenant(name="Test Company")

        # Create spark admin user
        unique_id = str(uuid.uuid4())[:8]
        self.spark_admin_user = self.create_user(
            username=f"spark_invite_{unique_id}@test.com",
            email=f"spark_invite_{unique_id}@test.com",
            role=self.roles['spark_admin'],
            password="testpass123"
        )
        self.create_tenanted_user(
            user=self.spark_admin_user, tenant=self.tenant)

        # Create prerequisite data
        self.event = self.create_event(
            name="Test Event",
            tenant=self.tenant,
            address="123 Test St"
        )
        self.job_title = self.create_job_title(
            name="Software Engineer",
            tenant=self.tenant
        )
        self.rate_type = self.create_rate_type(
            name="Hourly",
            tenant=self.tenant
        )
        self.rate = self.create_rate(
            amount=75.0,
            rate_type=self.rate_type,
            tenant=self.tenant
        )
        self.job = self.create_job(
            name="Test Job",
            code="JOB-SPARK-001",
            address="123 Main St",
            event=self.event,
            job_title=self.job_title,
            tenant=self.tenant,
            rate=self.rate
        )

        # Create ambassadors
        self.ambassador_user1 = self.create_user(
            username=f"amb1_spark_{unique_id}@test.com",
            email=f"amb1_spark_{unique_id}@test.com",
            role=self.roles['ambassador'],
            password="testpass123"
        )
        self.create_tenanted_user(
            user=self.ambassador_user1, tenant=self.tenant)
        self.ambassador1 = self.create_ambassador(user=self.ambassador_user1)

        self.schema = schema_spark
        self.endpoint_path = "/api/v1/graphql/spark"

        self.mutation = """
            mutation InviteAmbassadorsToJob($input: InviteAmbassadorsToJobInput!) {
                inviteAmbassadorsToJob(input: $input) {
                    success
                    message
                    clientMutationId
                    ambassadorJobs {
                        id
                        ambassador {
                            id
                            user {
                                email
                            }
                        }
                        job {
                            id
                            name
                        }
                        status {
                            slug
                        }
                        rate {
                            amount
                        }
                    }
                }
            }
        """

    @pytest.mark.asyncio
    async def test_invite_ambassadors_to_job_success_by_spark_admin(self):
        """Test successful invitation by spark admin."""
        variables = {
            "input": {
                "tenantId": str(self.tenant.id),
                "jobId": str(self.job.id),
                "ambassadorIds": [str(self.ambassador1.id)],
                "clientMutationId": "spark-invite-1"
            }
        }

        result = await self._execute_mutation_authenticated(
            self.mutation, variables, self.spark_admin_user, self.endpoint_path)

        assert result.errors is None
        assert result.data is not None
        assert result.data["inviteAmbassadorsToJob"]["success"] is True
        assert result.data["inviteAmbassadorsToJob"]["clientMutationId"] == "spark-invite-1"
        assert len(result.data["inviteAmbassadorsToJob"]
                   ["ambassadorJobs"]) == 1

        # Verify rate is correct
        aj = result.data["inviteAmbassadorsToJob"]["ambassadorJobs"][0]
        assert aj["rate"]["amount"] == 75.0
        assert aj["status"]["slug"] == "invited"
