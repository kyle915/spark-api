"""
Tests for Job mutations in the jobs app.

This module tests:
- create_job (Client and Spark schemas)
- update_job (Client and Spark schemas)
"""
import pytest
import strawberry_django  # noqa: F401
from asgiref.sync import sync_to_async
from django.contrib.auth import get_user_model
from jobs.tests.base import JobsGraphQLTestCase
from jobs import models
from utils.utils import ROLE_ID

User = get_user_model()


@pytest.mark.django_db(transaction=True)
class TestClientJobMutations(JobsGraphQLTestCase):
    """Tests for Job mutations (Client schema)."""

    @pytest.fixture(autouse=True)
    def setup(self, db):
        """Set up test data before each test."""
        from config.schema_client import schema_clients
        self.roles = self.setup_default_roles()
        self.tenant = self.create_tenant(name="Test Company")
        # Create a client user for authentication
        self.client_user = self.create_user(
            username="client@test.com",
            email="client@test.com",
            role=self.roles['client'],
            password="testpass123"
        )
        # Create tenanted user relationship
        self.create_tenanted_user(user=self.client_user, tenant=self.tenant)

        # Create prerequisite data for jobs
        self.company = self.create_company(
            name="Test Company",
            email="company@test.com",
            phone="123-456-7890",
            tenant=self.tenant
        )
        self.location = self.create_location(
            name="Test Location",
            code="TEST",
            zip_code="12345",
            tenant=self.tenant
        )
        self.event = self.create_event(
            name="Test Event",
            tenant=self.tenant,
            address="123 Test St"
        )
        self.job_title = self.create_job_title(
            name="Software Engineer",
            tenant=self.tenant
        )

        self.schema = schema_clients
        self.endpoint_path = "/api/v1/graphql/clients"

    @pytest.mark.asyncio
    async def test_create_job_success(self):
        """Test successful job creation."""
        mutation = """
        mutation CreateJob($input: CreateJobInput!) {
            createJob(input: $input) {
                success
                message
                job {
                    id
                    uuid
                    name
                    code
                    address
                }
            }
        }
        """

        variables = {
            "input": {
                "name": "Test Job",
                "description": "A test job description",
                "code": "JOB-001",
                "address": "123 Main St",
                "jobTitleId": str(self.job_title.id),
                "companyId": str(self.company.id),
                "eventId": str(self.event.id),
                "locationId": str(self.location.id),
            }
        }

        result = await self._execute_mutation_authenticated(
            mutation, variables, self.client_user, self.endpoint_path)

        assert result.data is not None, f"Result data is None. Errors: {result.errors}"
        assert result.data["createJob"]["success"] is True
        assert result.data["createJob"]["job"] is not None
        assert result.data["createJob"]["job"]["name"] == "Test Job"
        assert result.data["createJob"]["job"]["code"] == "JOB-001"

        # Verify job was created
        job_id = result.data["createJob"]["job"]["id"]
        job = await sync_to_async(models.Job.objects.get)(pk=job_id)
        assert job.name == "Test Job"
        assert job.code == "JOB-001"
        # Compare IDs to avoid async database access
        assert job.company_id == self.company.id
        assert job.event_id == self.event.id
        assert job.location_id == self.location.id
        assert job.job_title_id == self.job_title.id
        assert job.tenant_id == self.tenant.id

    @pytest.mark.asyncio
    async def test_update_job_success(self):
        """Test successful job update."""
        # Create a job first
        job = await sync_to_async(self.create_job)(
            name="Original Job",
            code="JOB-ORIG",
            address="Original Address",
            company=self.company,
            event=self.event,
            job_title=self.job_title,
            tenant=self.tenant
        )

        mutation = """
        mutation UpdateJob($input: UpdateJobInput!) {
            updateJob(input: $input) {
                success
                message
                job {
                    id
                    name
                    code
                    address
                }
            }
        }
        """

        variables = {
            "input": {
                "id": str(job.id),
                "name": "Updated Job",
                "description": "Updated description",
                "code": "JOB-UPD",
                "address": "Updated Address",
                "jobTitleId": str(self.job_title.id),
                "companyId": str(self.company.id),
                "eventId": str(self.event.id),
                "locationId": str(self.location.id),
            }
        }

        result = await self._execute_mutation_authenticated(
            mutation, variables, self.client_user, self.endpoint_path)

        assert result.data is not None
        assert result.data["updateJob"]["success"] is True
        assert result.data["updateJob"]["job"]["name"] == "Updated Job"
        assert result.data["updateJob"]["job"]["code"] == "JOB-UPD"

        # Verify job was updated
        updated_job = await sync_to_async(models.Job.objects.get)(pk=job.id)
        assert updated_job.name == "Updated Job"
        assert updated_job.code == "JOB-UPD"


@pytest.mark.django_db(transaction=True)
class TestSparkJobMutations(JobsGraphQLTestCase):
    """Tests for Job mutations (Spark schema)."""

    @pytest.fixture(autouse=True)
    def setup(self, db):
        """Set up test data before each test."""
        from config.schema_spark import schema_spark
        self.roles = self.setup_default_roles()
        self.tenant = self.create_tenant(name="Test Company")
        # Create a spark admin user for authentication
        self.spark_user = self.create_user(
            username="spark@test.com",
            email="spark@test.com",
            role=self.roles['spark_admin'],
            password="testpass123"
        )
        # Create tenanted user relationship
        self.create_tenanted_user(user=self.spark_user, tenant=self.tenant)

        # Create prerequisite data for jobs
        self.company = self.create_company(
            name="Test Company",
            email="company@test.com",
            phone="123-456-7890",
            tenant=self.tenant
        )
        self.location = self.create_location(
            name="Test Location",
            code="TEST",
            zip_code="12345",
            tenant=self.tenant
        )
        self.event = self.create_event(
            name="Test Event",
            tenant=self.tenant,
            address="123 Test St"
        )
        self.job_title = self.create_job_title(
            name="Software Engineer",
            tenant=self.tenant
        )

        self.schema = schema_spark
        self.endpoint_path = "/api/v1/graphql/spark"

    @pytest.mark.asyncio
    async def test_create_job_success(self):
        """Test successful job creation (Spark schema)."""
        mutation = """
        mutation CreateJob($input: CreateJobInput!) {
            createJob(input: $input) {
                success
                message
                job {
                    id
                    uuid
                    name
                    code
                }
            }
        }
        """

        variables = {
            "input": {
                "name": "Spark Job",
                "description": "A spark job description",
                "code": "SPARK-001",
                "address": "456 Spark St",
                "jobTitleId": str(self.job_title.id),
                "companyId": str(self.company.id),
                "eventId": str(self.event.id),
                "locationId": str(self.location.id),
            }
        }

        result = await self._execute_mutation_authenticated(
            mutation, variables, self.spark_user, self.endpoint_path)

        assert result.data is not None, f"Result data is None. Errors: {result.errors}"
        assert result.data["createJob"]["success"] is True
        assert result.data["createJob"]["job"] is not None
        assert result.data["createJob"]["job"]["name"] == "Spark Job"

        # Verify job was created
        job_id = result.data["createJob"]["job"]["id"]
        job = await sync_to_async(models.Job.objects.get)(pk=job_id)
        assert job.name == "Spark Job"
        # Compare tenant IDs to avoid async database access
        tenant_id = await sync_to_async(lambda: self.tenant.id)()
        assert job.tenant_id == tenant_id

    @pytest.mark.asyncio
    async def test_update_job_success(self):
        """Test successful job update (Spark schema)."""
        # Create a job first
        job = await sync_to_async(self.create_job)(
            name="Original Spark Job",
            code="SPARK-ORIG",
            address="Original Address",
            company=self.company,
            event=self.event,
            job_title=self.job_title,
            tenant=self.tenant
        )

        mutation = """
        mutation UpdateJob($input: UpdateJobInput!) {
            updateJob(input: $input) {
                success
                message
                job {
                    id
                    name
                    code
                }
            }
        }
        """

        variables = {
            "input": {
                "id": str(job.id),
                "name": "Updated Spark Job",
                "description": "Updated description",
                "code": "SPARK-UPD",
                "address": "Updated Address",
                "jobTitleId": str(self.job_title.id),
                "companyId": str(self.company.id),
                "eventId": str(self.event.id),
                "locationId": str(self.location.id),
            }
        }

        result = await self._execute_mutation_authenticated(
            mutation, variables, self.spark_user, self.endpoint_path)

        assert result.data is not None
        assert result.data["updateJob"]["success"] is True
        assert result.data["updateJob"]["job"]["name"] == "Updated Spark Job"

        # Verify job was updated
        updated_job = await sync_to_async(models.Job.objects.get)(pk=job.id)
        assert updated_job.name == "Updated Spark Job"
