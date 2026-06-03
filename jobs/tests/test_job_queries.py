"""
Tests for Job queries in the jobs app.

This module tests:
- jobs query (Client, Spark, Ambassador schemas)
- job query (Client, Spark, Ambassador schemas)
"""
import base64

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
            event=self.event,
            job_title=self.job_title,
            tenant=self.tenant
        )
        self.job2 = self.create_job(
            name="Job 2",
            code="JOB-002",
            address="Address 2",
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
        decoded_id = int(
            base64.b64decode(result.data["job"]["id"]).decode("utf-8").split(":")[1]
        )
        assert decoded_id == self.job1.id
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

    @pytest.mark.asyncio
    async def test_ambassador_jobs_query_with_status_filter(self):
        """Test ambassadorJobs query accepts status filter without resolver errors."""
        def _create_test_data():
            ambassador_user = self.create_user(
                username="spark_query_ambassador@test.com",
                email="spark_query_ambassador@test.com",
                role=self.roles["ambassador"],
                password="testpass123",
            )
            ambassador = self.create_ambassador(user=ambassador_user)
            self.create_tenanted_user(user=ambassador_user, tenant=self.tenant)
            status = self.create_status(
                name="Pending", tenant=self.tenant, slug="pending"
            )
            rate_type = self.create_rate_type(name="Hour", tenant=self.tenant)
            rate = self.create_rate(
                amount=25.0, rate_type=rate_type, tenant=self.tenant
            )
            self.create_ambassador_job(
                ambassador=ambassador,
                job=self.job,
                status=status,
                rate=rate,
                tenant=self.tenant,
            )

        await sync_to_async(_create_test_data)()

        query = """
        query AmbassadorJobsPendingQuery($first: Int) {
            ambassadorJobs(first: $first, filters: { status: PENDING }) {
                totalCount
                edges {
                    node {
                        id
                    }
                }
            }
        }
        """
        variables = {"first": 10}

        result = await self._execute_query_authenticated(
            query, variables, self.spark_user, self.endpoint_path
        )

        assert result.errors is None
        assert result.data is not None
        assert result.data["ambassadorJobs"] is not None
        assert result.data["ambassadorJobs"]["totalCount"] >= 1

    @pytest.mark.asyncio
    async def test_ambassador_jobs_query_with_search_q(self):
        """Test ambassadorJobs query supports q filtering without FieldError."""
        def _create_test_data():
            ambassador_user = self.create_user(
                username="spark_query_ambassador_q@test.com",
                email="spark_query_ambassador_q@test.com",
                role=self.roles["ambassador"],
                password="testpass123",
                first_name="Alex",
                last_name="Morgan",
            )
            ambassador = self.create_ambassador(
                user=ambassador_user,
            )
            self.create_tenanted_user(user=ambassador_user, tenant=self.tenant)
            status = self.create_status(
                name="Pending", tenant=self.tenant, slug="pending"
            )
            rate_type = self.create_rate_type(name="Hour", tenant=self.tenant)
            rate = self.create_rate(
                amount=25.0, rate_type=rate_type, tenant=self.tenant
            )
            self.create_ambassador_job(
                ambassador=ambassador,
                job=self.job,
                status=status,
                rate=rate,
                tenant=self.tenant,
            )

        await sync_to_async(_create_test_data)()

        query = """
        query AmbassadorJobsSearchQuery($first: Int, $q: String) {
            ambassadorJobs(first: $first, q: $q) {
                totalCount
                edges {
                    node {
                        id
                    }
                }
            }
        }
        """
        variables = {"first": 10, "q": "Spark Job"}

        result = await self._execute_query_authenticated(
            query, variables, self.spark_user, self.endpoint_path
        )

        assert result.errors is None
        assert result.data is not None
        assert result.data["ambassadorJobs"] is not None
        assert result.data["ambassadorJobs"]["totalCount"] >= 1


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


@pytest.mark.django_db(transaction=True)
class TestMobileAmbassadorJobQueries(JobsGraphQLTestCase):
    """Tests for mobile ambassador job queries."""

    @pytest.fixture(autouse=True)
    def setup(self, db):
        from config.schema_mobile import schema_mobile
        self.roles = self.setup_default_roles()
        self.tenant = self.create_tenant(name="Test Company")

        self.ambassador_user = self.create_user(
            username="mobile_ambassador@test.com",
            email="mobile_ambassador@test.com",
            role=self.roles["ambassador"],
            password="testpass123",
        )
        self.ambassador = self.create_ambassador(user=self.ambassador_user)
        self.create_tenanted_user(user=self.ambassador_user, tenant=self.tenant)

        self.other_ambassador_user = self.create_user(
            username="other_mobile_ambassador@test.com",
            email="other_mobile_ambassador@test.com",
            role=self.roles["ambassador"],
            password="testpass123",
        )
        self.other_ambassador = self.create_ambassador(user=self.other_ambassador_user)
        self.create_tenanted_user(user=self.other_ambassador_user, tenant=self.tenant)

        self.location = self.create_location(
            name="Test Location",
            code="TEST",
            zip_code="12345",
            tenant=self.tenant,
        )
        self.event = self.create_event(
            name="Test Event",
            tenant=self.tenant,
            address="123 Test St",
        )
        self.job_title = self.create_job_title(
            name="Software Engineer",
            tenant=self.tenant,
        )
        self.job = self.create_job(
            name="Mobile Job",
            code="MOBILE-001",
            address="Mobile Address",
            event=self.event,
            job_title=self.job_title,
            tenant=self.tenant,
            public=True,
            ongoing=True,
            closed=False,
        )
        self.status = self.create_status(name="Pending", tenant=self.tenant, slug="pending")
        self.rate_type = self.create_rate_type(name="Hour", tenant=self.tenant)
        self.rate = self.create_rate(amount=25.0, rate_type=self.rate_type, tenant=self.tenant)

        self.own_ambassador_job = self.create_ambassador_job(
            ambassador=self.ambassador,
            job=self.job,
            status=self.status,
            rate=self.rate,
            tenant=self.tenant,
        )
        self.other_ambassador_job = self.create_ambassador_job(
            ambassador=self.other_ambassador,
            job=self.job,
            status=self.status,
            rate=self.rate,
            tenant=self.tenant,
        )

        self.schema = schema_mobile
        self.endpoint_path = "/api/v270986/graphql/mobile"

    @pytest.mark.asyncio
    async def test_ambassador_jobs_mobile_returns_only_logged_user_records(self):
        query = """
        query AmbassadorJobsMobileQuery($first: Int) {
            ambassadorJobsMobile(first: $first) {
                edges {
                    node {
                        id
                    }
                }
                totalCount
            }
        }
        """
        variables = {"first": 10}

        result = await self._execute_query_authenticated(
            query, variables, self.ambassador_user, self.endpoint_path
        )

        assert result.errors is None
        assert result.data is not None
        assert result.data["ambassadorJobsMobile"] is not None
        assert result.data["ambassadorJobsMobile"]["totalCount"] == 1
        returned_ids = [
            int(base64.b64decode(edge["node"]["id"]).decode("utf-8").split(":")[1])
            for edge in result.data["ambassadorJobsMobile"]["edges"]
        ]
        assert self.own_ambassador_job.id in returned_ids
        assert self.other_ambassador_job.id not in returned_ids

    @pytest.mark.asyncio
    async def test_ambassador_job_mobile_returns_logged_user_record(self):
        query = """
        query AmbassadorJobMobileQuery($id: ID!) {
            ambassadorJobMobile(id: $id) {
                id
            }
        }
        """
        variables = {"id": str(self.own_ambassador_job.id)}

        result = await self._execute_query_authenticated(
            query, variables, self.ambassador_user, self.endpoint_path
        )

        assert result.errors is None
        assert result.data is not None
        assert result.data["ambassadorJobMobile"] is not None
        decoded_id = int(
            base64.b64decode(result.data["ambassadorJobMobile"]["id"])
            .decode("utf-8")
            .split(":")[1]
        )
        assert decoded_id == self.own_ambassador_job.id

    @pytest.mark.asyncio
    async def test_ambassador_job_mobile_hides_other_ambassador_record(self):
        query = """
        query AmbassadorJobMobileQuery($id: ID!) {
            ambassadorJobMobile(id: $id) {
                id
            }
        }
        """
        variables = {"id": str(self.other_ambassador_job.id)}

        result = await self._execute_query_authenticated(
            query, variables, self.ambassador_user, self.endpoint_path
        )

        assert result.errors is None
        assert result.data is not None
        assert result.data["ambassadorJobMobile"] is None
