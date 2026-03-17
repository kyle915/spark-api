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
from unittest.mock import patch
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
        coordinates = [12.34, -56.78]
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
                "eventId": str(self.event.id),
                "coordinates": coordinates,
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
        assert job.event_id == self.event.id
        assert job.coordinates == coordinates
        assert job.job_title_id == self.job_title.id
        assert job.tenant_id == self.tenant.id

    @pytest.mark.asyncio
    async def test_update_job_success(self):
        """Test successful job update."""
        # Create a job first
        original_coordinates = [10.0, 20.0]
        job = await sync_to_async(self.create_job)(
            name="Original Job",
            code="JOB-ORIG",
            address="Original Address",
            event=self.event,
            job_title=self.job_title,
            tenant=self.tenant,
            coordinates=original_coordinates,
        )
        new_coordinates = [30.0, 40.0]

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
                "eventId": str(self.event.id),
                "coordinates": new_coordinates,
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
        assert updated_job.coordinates == new_coordinates

    @pytest.mark.asyncio
    async def test_update_job_sends_email_when_relevant_fields_change(self):
        rate_type = await sync_to_async(self.create_rate_type)(
            name="Hourly",
            tenant=self.tenant,
        )
        original_rate = await sync_to_async(self.create_rate)(
            amount=50.0,
            rate_type=rate_type,
            tenant=self.tenant,
        )
        updated_rate = await sync_to_async(self.create_rate)(
            amount=60.0,
            rate_type=rate_type,
            tenant=self.tenant,
        )
        ambassador_user = await sync_to_async(self.create_user)(
            username="ambassador_update_job@test.com",
            email="ambassador_update_job@test.com",
            role=self.roles["ambassador"],
            password="testpass123",
        )
        await sync_to_async(self.create_tenanted_user)(
            user=ambassador_user,
            tenant=self.tenant,
        )
        ambassador = await sync_to_async(self.create_ambassador)(user=ambassador_user)
        status = await sync_to_async(self.create_status)(name="Assigned", tenant=self.tenant)
        job = await sync_to_async(self.create_job)(
            name="Original Job",
            code="JOB-NOTIFY",
            address="Original Address",
            event=self.event,
            job_title=self.job_title,
            tenant=self.tenant,
            rate=original_rate,
        )
        await sync_to_async(self.create_ambassador_job)(
            ambassador=ambassador,
            job=job,
            status=status,
            rate=original_rate,
            tenant=self.tenant,
        )

        mutation = """
        mutation UpdateJob($input: UpdateJobInput!) {
            updateJob(input: $input) {
                success
                message
                job {
                    id
                }
            }
        }
        """

        variables = {
            "input": {
                "id": str(job.id),
                "name": "Original Job",
                "description": "Updated description",
                "code": "JOB-NOTIFY",
                "address": "Updated Address",
                "jobTitleId": str(self.job_title.id),
                "eventId": str(self.event.id),
                "rateId": str(updated_rate.id),
                "coordinates": [10.0, 20.0],
            }
        }

        with patch("jobs.mutations.AmbassadorJobUpdatedMailer.send") as mock_send:
            result = await self._execute_mutation_authenticated(
                mutation, variables, self.client_user, self.endpoint_path
            )

        assert result.errors is None
        assert result.data is not None
        assert result.data["updateJob"]["success"] is True
        assert mock_send.called


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
        coordinates = [-12.34, 56.78]
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
                "eventId": str(self.event.id),
                "coordinates": coordinates,
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
        assert job.coordinates == coordinates
        # Compare tenant IDs to avoid async database access
        tenant_id = await sync_to_async(lambda: self.tenant.id)()
        assert job.tenant_id == tenant_id

    @pytest.mark.asyncio
    async def test_update_job_success(self):
        """Test successful job update (Spark schema)."""
        # Create a job first
        original_coordinates = [11.11, 22.22]
        job = await sync_to_async(self.create_job)(
            name="Original Spark Job",
            code="SPARK-ORIG",
            address="Original Address",
            event=self.event,
            job_title=self.job_title,
            tenant=self.tenant,
            coordinates=original_coordinates,
        )
        new_coordinates = [33.33, 44.44]

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
                "eventId": str(self.event.id),
                "coordinates": new_coordinates,
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
        assert updated_job.coordinates == new_coordinates
