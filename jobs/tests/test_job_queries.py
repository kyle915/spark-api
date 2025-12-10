"""
Tests for Job queries in the jobs app.

This module tests:
- jobs query (Client, Spark, Ambassador schemas)
- job query (Client, Spark, Ambassador schemas)
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
class TestClientJobQueries(JobsGraphQLTestCase):
    """Tests for Job queries (Client schema)."""

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

        # Create test jobs
        self.job1 = self.create_job(
            name="Job 1",
            code="JOB-001",
            address="Address 1",
            company=self.company,
            event=self.event,
            job_title=self.job_title,
            tenant=self.tenant
        )
        self.job2 = self.create_job(
            name="Job 2",
            code="JOB-002",
            address="Address 2",
            company=self.company,
            event=self.event,
            job_title=self.job_title,
            tenant=self.tenant
        )

        self.schema = schema_clients
        self.endpoint_path = "/api/v1/graphql/clients"

    @pytest.mark.asyncio
    async def test_jobs_query_success(self):
        """Test successful jobs query."""
        query = """
        query JobsQuery($first: Int) {
            jobs(first: $first) {
                edges {
                    node {
                        id
                        name
                        code
                    }
                    cursor
                }
                pageInfo {
                    hasNextPage
                    hasPreviousPage
                }
                totalCount
            }
        }
        """

        variables = {
            "first": 10
        }

        result = await self._execute_query_authenticated(
            query, variables, self.client_user, self.endpoint_path)

        assert result.data is not None, f"Result data is None. Errors: {result.errors}"
        assert result.data["jobs"] is not None
        assert result.data["jobs"]["totalCount"] >= 2
        assert len(result.data["jobs"]["edges"]) >= 2

        # Verify we can find our test jobs
        job_names = [edge["node"]["name"]
                     for edge in result.data["jobs"]["edges"]]
        assert "Job 1" in job_names or "Job 2" in job_names

    @pytest.mark.asyncio
    async def test_job_query_success(self):
        """Test successful single job query."""
        query = """
        query JobQuery($id: ID!) {
            job(id: $id) {
                id
                name
                code
                address
            }
        }
        """

        variables = {
            "id": str(self.job1.id)
        }

        result = await self._execute_query_authenticated(
            query, variables, self.client_user, self.endpoint_path)

        assert result.data is not None
        assert result.data["job"] is not None
        assert result.data["job"]["id"] == str(self.job1.id)
        assert result.data["job"]["name"] == "Job 1"
        assert result.data["job"]["code"] == "JOB-001"


@pytest.mark.django_db(transaction=True)
class TestSparkJobQueries(JobsGraphQLTestCase):
    """Tests for Job queries (Spark schema)."""

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

        # Create test job
        self.job = self.create_job(
            name="Spark Job",
            code="SPARK-001",
            address="Spark Address",
            company=self.company,
            event=self.event,
            job_title=self.job_title,
            tenant=self.tenant
        )

        self.schema = schema_spark
        self.endpoint_path = "/api/v1/graphql/spark"

    @pytest.mark.asyncio
    async def test_jobs_query_success(self):
        """Test successful jobs query (Spark schema)."""
        query = """
        query JobsQuery($first: Int) {
            jobs(first: $first) {
                edges {
                    node {
                        id
                        name
                        code
                    }
                }
                totalCount
            }
        }
        """

        variables = {
            "first": 10
        }

        result = await self._execute_query_authenticated(
            query, variables, self.spark_user, self.endpoint_path)

        assert result.data is not None, f"Result data is None. Errors: {result.errors}"
        assert result.data["jobs"] is not None
        assert result.data["jobs"]["totalCount"] >= 1

    @pytest.mark.asyncio
    async def test_job_query_success(self):
        """Test successful single job query (Spark schema)."""
        query = """
        query JobQuery($id: ID!) {
            job(id: $id) {
                id
                name
                code
            }
        }
        """

        variables = {
            "id": str(self.job.id)
        }

        result = await self._execute_query_authenticated(
            query, variables, self.spark_user, self.endpoint_path)

        assert result.data is not None
        assert result.data["job"] is not None
        assert result.data["job"]["name"] == "Spark Job"


@pytest.mark.django_db(transaction=True)
class TestAmbassadorJobQueries(JobsGraphQLTestCase):
    """Tests for Job queries (Ambassador schema)."""

    @pytest.fixture(autouse=True)
    def setup(self, db):
        """Set up test data before each test."""
        from config.schema_ambassador import schema_ambassador
        self.roles = self.setup_default_roles()
        self.tenant = self.create_tenant(name="Test Company")
        # Create an ambassador user for authentication
        self.ambassador_user = self.create_user(
            username="ambassador@test.com",
            email="ambassador@test.com",
            role=self.roles['ambassador'],
            password="testpass123"
        )
        # Create ambassador profile
        self.ambassador = self.create_ambassador(user=self.ambassador_user)
        # Create tenanted user relationship
        self.create_tenanted_user(
            user=self.ambassador_user, tenant=self.tenant)

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

        # Create a public, ongoing, not-closed job (available for ambassadors)
        self.available_job = self.create_job(
            name="Available Job",
            code="AVAIL-001",
            address="Available Address",
            company=self.company,
            event=self.event,
            job_title=self.job_title,
            tenant=self.tenant,
            public=True,
            ongoing=True,
            closed=False
        )

        self.schema = schema_ambassador
        self.endpoint_path = "/api/v1/graphql/ambassadors"

    @pytest.mark.asyncio
    async def test_available_jobs_query_success(self):
        """Test successful available jobs query (Ambassador schema)."""
        query = """
        query AvailableJobsQuery($first: Int) {
            availableJobs(first: $first) {
                edges {
                    node {
                        id
                        name
                        code
                    }
                }
                totalCount
            }
        }
        """

        variables = {
            "first": 10
        }

        result = await self._execute_query_authenticated(
            query, variables, self.ambassador_user, self.endpoint_path)

        assert result.data is not None, f"Result data is None. Errors: {result.errors}"
        assert result.data["availableJobs"] is not None
        assert result.data["availableJobs"]["totalCount"] >= 1
