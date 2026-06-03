"""
Tests for ManageAmbassadorJob mutations in the jobs app.

This module tests:
- manage_ambassador_job_assignment (Client and Spark schemas)
  - ACCEPT action
  - REJECT action
  - BLACKLIST action
  - WHITELIST action
"""
import base64

import pytest
import strawberry_django  # noqa: F401
from asgiref.sync import sync_to_async
from django.contrib.auth import get_user_model
from jobs.tests.base import JobsGraphQLTestCase
from jobs import models, inputs
from utils.utils import ROLE_ID

User = get_user_model()


@pytest.mark.django_db(transaction=True)
class TestClientManageAmbassadorJobMutations(JobsGraphQLTestCase):
    """Tests for ManageAmbassadorJob mutations (Client schema)."""

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

        # Create prerequisite data
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
        self.job = self.create_job(
            name="Test Job",
            code="JOB-001",
            address="123 Main St",
            event=self.event,
            job_title=self.job_title,
            tenant=self.tenant
        )

        # Create ambassador and ambassador job
        self.ambassador_user = self.create_user(
            username="ambassador@test.com",
            email="ambassador@test.com",
            role=self.roles['ambassador'],
            password="testpass123"
        )
        self.ambassador = self.create_ambassador(user=self.ambassador_user)
        self.create_tenanted_user(
            user=self.ambassador_user, tenant=self.tenant)

        # Create status and rate for ambassador job
        self.initial_status = self.create_status(
            name="Pending", tenant=self.tenant)
        self.accept_status = self.create_status(
            name="Accepted", tenant=self.tenant)
        self.reject_status = self.create_status(
            name="Rejected", tenant=self.tenant)
        self.blacklist_status = self.create_status(
            name="Blacklisted", tenant=self.tenant)
        self.whitelist_status = self.create_status(
            name="Whitelisted", tenant=self.tenant)

        self.rate_type = self.create_rate_type(
            name="Hourly", tenant=self.tenant)
        self.rate = self.create_rate(
            amount=50.0,
            rate_type=self.rate_type,
            tenant=self.tenant
        )

        self.ambassador_job = self.create_ambassador_job(
            ambassador=self.ambassador,
            job=self.job,
            status=self.initial_status,
            rate=self.rate,
            tenant=self.tenant
        )

        self.schema = schema_clients
        self.endpoint_path = "/api/v1/graphql/clients"

    @pytest.mark.asyncio
    async def test_manage_assignment_accept_with_status_id(self):
        """Test accepting ambassador job assignment with explicit status_id."""
        mutation = """
        mutation ManageAssignment($input: ManageAmbassadorJobAssignmentInput!) {
            manageAmbassadorJobAssignment(input: $input) {
                success
                message
                ambassadorJob {
                    id
                    status {
                        id
                        name
                    }
                }
            }
        }
        """

        variables = {
            "input": {
                "ambassadorJobId": str(self.ambassador_job.id),
                "action": "ACCEPT",
                "statusId": str(self.accept_status.id),
            }
        }

        result = await self._execute_mutation_authenticated(
            mutation, variables, self.client_user, self.endpoint_path)

        assert result.data is not None, f"Result data is None. Errors: {result.errors}"
        assert result.data["manageAmbassadorJobAssignment"]["success"] is True
        assert "accepted" in result.data["manageAmbassadorJobAssignment"]["message"].lower(
        )
        assert int(base64.b64decode(
            result.data["manageAmbassadorJobAssignment"]["ambassadorJob"]["status"]["id"]
        ).decode("utf-8").split(":")[1]) == self.accept_status.id

        # Verify status was updated
        updated_job = await sync_to_async(models.AmbassadorJob.objects.get)(pk=self.ambassador_job.id)
        assert updated_job.status_id == self.accept_status.id

    @pytest.mark.asyncio
    async def test_manage_assignment_reject_with_status_id(self):
        """Test rejecting ambassador job assignment with explicit status_id."""
        mutation = """
        mutation ManageAssignment($input: ManageAmbassadorJobAssignmentInput!) {
            manageAmbassadorJobAssignment(input: $input) {
                success
                message
                ambassadorJob {
                    id
                    status {
                        id
                        name
                    }
                }
            }
        }
        """

        variables = {
            "input": {
                "ambassadorJobId": str(self.ambassador_job.id),
                "action": "REJECT",
                "statusId": str(self.reject_status.id),
            }
        }

        result = await self._execute_mutation_authenticated(
            mutation, variables, self.client_user, self.endpoint_path)

        assert result.data is not None
        assert result.data["manageAmbassadorJobAssignment"]["success"] is True
        assert "rejected" in result.data["manageAmbassadorJobAssignment"]["message"].lower(
        )
        assert int(base64.b64decode(
            result.data["manageAmbassadorJobAssignment"]["ambassadorJob"]["status"]["id"]
        ).decode("utf-8").split(":")[1]) == self.reject_status.id

        # Verify status was updated
        updated_job = await sync_to_async(models.AmbassadorJob.objects.get)(pk=self.ambassador_job.id)
        assert updated_job.status_id == self.reject_status.id

    @pytest.mark.asyncio
    async def test_manage_assignment_blacklist_with_status_id(self):
        """Test blacklisting ambassador job assignment with explicit status_id."""
        mutation = """
        mutation ManageAssignment($input: ManageAmbassadorJobAssignmentInput!) {
            manageAmbassadorJobAssignment(input: $input) {
                success
                message
                ambassadorJob {
                    id
                    status {
                        id
                        name
                    }
                }
            }
        }
        """

        variables = {
            "input": {
                "ambassadorJobId": str(self.ambassador_job.id),
                "action": "BLACKLIST",
                "statusId": str(self.blacklist_status.id),
            }
        }

        result = await self._execute_mutation_authenticated(
            mutation, variables, self.client_user, self.endpoint_path)

        assert result.data is not None
        assert result.data["manageAmbassadorJobAssignment"]["success"] is True
        assert "blacklisted" in result.data["manageAmbassadorJobAssignment"]["message"].lower(
        )
        assert int(base64.b64decode(
            result.data["manageAmbassadorJobAssignment"]["ambassadorJob"]["status"]["id"]
        ).decode("utf-8").split(":")[1]) == self.blacklist_status.id

    @pytest.mark.asyncio
    async def test_manage_assignment_whitelist_with_status_id(self):
        """Test whitelisting ambassador job assignment with explicit status_id."""
        mutation = """
        mutation ManageAssignment($input: ManageAmbassadorJobAssignmentInput!) {
            manageAmbassadorJobAssignment(input: $input) {
                success
                message
                ambassadorJob {
                    id
                    status {
                        id
                        name
                    }
                }
            }
        }
        """

        variables = {
            "input": {
                "ambassadorJobId": str(self.ambassador_job.id),
                "action": "WHITELIST",
                "statusId": str(self.whitelist_status.id),
            }
        }

        result = await self._execute_mutation_authenticated(
            mutation, variables, self.client_user, self.endpoint_path)

        assert result.data is not None
        assert result.data["manageAmbassadorJobAssignment"]["success"] is True
        assert "whitelisted" in result.data["manageAmbassadorJobAssignment"]["message"].lower(
        )
        assert int(base64.b64decode(
            result.data["manageAmbassadorJobAssignment"]["ambassadorJob"]["status"]["id"]
        ).decode("utf-8").split(":")[1]) == self.whitelist_status.id

    @pytest.mark.asyncio
    async def test_manage_assignment_accept_without_status_id(self):
        """Test accepting ambassador job assignment without status_id (uses name pattern)."""
        mutation = """
        mutation ManageAssignment($input: ManageAmbassadorJobAssignmentInput!) {
            manageAmbassadorJobAssignment(input: $input) {
                success
                message
                ambassadorJob {
                    id
                    status {
                        id
                        name
                    }
                }
            }
        }
        """

        variables = {
            "input": {
                "ambassadorJobId": str(self.ambassador_job.id),
                "action": "ACCEPT",
            }
        }

        result = await self._execute_mutation_authenticated(
            mutation, variables, self.client_user, self.endpoint_path)

        assert result.data is not None
        assert result.data["manageAmbassadorJobAssignment"]["success"] is True
        assert "accepted" in result.data["manageAmbassadorJobAssignment"]["message"].lower(
        )
        # Should find a status with "accept" in the name (case-insensitive)
        # The "Accepted" status created in setup should match
        status_name = result.data["manageAmbassadorJobAssignment"]["ambassadorJob"]["status"]["name"]
        assert "accept" in status_name.lower()

    @pytest.mark.asyncio
    async def test_manage_assignment_ambassador_job_not_found(self):
        """Test error when ambassador job is not found."""
        mutation = """
        mutation ManageAssignment($input: ManageAmbassadorJobAssignmentInput!) {
            manageAmbassadorJobAssignment(input: $input) {
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
                "ambassadorJobId": "999999",  # Non-existent ID
                "action": "ACCEPT",
                "statusId": str(self.accept_status.id),
            }
        }

        result = await self._execute_mutation_authenticated(
            mutation, variables, self.client_user, self.endpoint_path)

        assert result.data is not None
        assert result.data["manageAmbassadorJobAssignment"]["success"] is False
        assert "not found" in result.data["manageAmbassadorJobAssignment"]["message"].lower(
        )

    @pytest.mark.asyncio
    async def test_manage_assignment_status_not_found(self):
        """Test error when status is not found."""
        mutation = """
        mutation ManageAssignment($input: ManageAmbassadorJobAssignmentInput!) {
            manageAmbassadorJobAssignment(input: $input) {
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
                "ambassadorJobId": str(self.ambassador_job.id),
                "action": "ACCEPT",
                "statusId": "999999",  # Non-existent status ID
            }
        }

        result = await self._execute_mutation_authenticated(
            mutation, variables, self.client_user, self.endpoint_path)

        assert result.data is not None
        assert result.data["manageAmbassadorJobAssignment"]["success"] is False
        assert "not found" in result.data["manageAmbassadorJobAssignment"]["message"].lower(
        )


@pytest.mark.django_db(transaction=True)
class TestSparkManageAmbassadorJobMutations(JobsGraphQLTestCase):
    """Tests for ManageAmbassadorJob mutations (Spark schema)."""

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

        # Create prerequisite data
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
        self.job = self.create_job(
            name="Spark Job",
            code="SPARK-001",
            address="456 Spark St",
            event=self.event,
            job_title=self.job_title,
            tenant=self.tenant
        )

        # Create ambassador and ambassador job
        self.ambassador_user = self.create_user(
            username="ambassador2@test.com",
            email="ambassador2@test.com",
            role=self.roles['ambassador'],
            password="testpass123"
        )
        self.ambassador = self.create_ambassador(user=self.ambassador_user)
        self.create_tenanted_user(
            user=self.ambassador_user, tenant=self.tenant)

        # Create status and rate for ambassador job
        self.initial_status = self.create_status(
            name="Pending", tenant=self.tenant)
        self.accept_status = self.create_status(
            name="Accepted", tenant=self.tenant)

        self.rate_type = self.create_rate_type(
            name="Hourly", tenant=self.tenant)
        self.rate = self.create_rate(
            amount=50.0,
            rate_type=self.rate_type,
            tenant=self.tenant
        )

        self.ambassador_job = self.create_ambassador_job(
            ambassador=self.ambassador,
            job=self.job,
            status=self.initial_status,
            rate=self.rate,
            tenant=self.tenant
        )

        self.schema = schema_spark
        self.endpoint_path = "/api/v1/graphql/spark"

    @pytest.mark.asyncio
    async def test_manage_assignment_accept_spark(self):
        """Test accepting ambassador job assignment (Spark schema)."""
        mutation = """
        mutation ManageAssignment($input: ManageAmbassadorJobAssignmentInput!) {
            manageAmbassadorJobAssignment(input: $input) {
                success
                message
                ambassadorJob {
                    id
                    status {
                        id
                        name
                    }
                }
            }
        }
        """

        variables = {
            "input": {
                "ambassadorJobId": str(self.ambassador_job.id),
                "action": "ACCEPT",
                "statusId": str(self.accept_status.id),
            }
        }

        result = await self._execute_mutation_authenticated(
            mutation, variables, self.spark_user, self.endpoint_path)

        assert result.data is not None, f"Result data is None. Errors: {result.errors}"
        assert result.data["manageAmbassadorJobAssignment"]["success"] is True
        assert "accepted" in result.data["manageAmbassadorJobAssignment"]["message"].lower(
        )
        assert int(base64.b64decode(
            result.data["manageAmbassadorJobAssignment"]["ambassadorJob"]["status"]["id"]
        ).decode("utf-8").split(":")[1]) == self.accept_status.id

        # Verify status was updated
        updated_job = await sync_to_async(models.AmbassadorJob.objects.get)(pk=self.ambassador_job.id)
        assert updated_job.status_id == self.accept_status.id
