"""
Tests for Status mutations in the jobs app.

This module tests:
- create_ambassador_job_status
- update_ambassador_job_status
"""

import pytest
import strawberry_django  # noqa: F401
from asgiref.sync import sync_to_async
from django.contrib.auth import get_user_model
from django.utils.text import slugify
from jobs.tests.base import JobsGraphQLTestCase
from jobs import models

User = get_user_model()


@pytest.mark.django_db(transaction=True)
class TestClientStatusMutations(JobsGraphQLTestCase):
    """Tests for Status mutations (Client schema)."""

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
            role=self.roles["client"],
            password="testpass123",
        )
        # Create tenanted user relationship
        self.create_tenanted_user(user=self.client_user, tenant=self.tenant)
        self.schema = schema_clients
        self.endpoint_path = "/api/v1/graphql/clients"

    @pytest.mark.asyncio
    async def test_create_status_success(self):
        """Test successful status creation."""
        mutation = """
        mutation CreateStatus($input: CreateStatusInput!) {
            createAmbassadorJobStatus(input: $input) {
                success
                message
                status {
                    id
                    uuid
                    name
                    slug
                }
            }
        }
        """

        variables = {
            "input": {
                "name": "Test Status",
            }
        }

        result = await self._execute_mutation_authenticated(
            mutation, variables, self.client_user, self.endpoint_path
        )

        assert result.data is not None, f"Result data is None. Errors: {result.errors}"
        assert result.data["createAmbassadorJobStatus"]["success"] is True
        assert result.data["createAmbassadorJobStatus"]["status"] is not None
        assert (
            result.data["createAmbassadorJobStatus"]["status"]["name"] == "Test Status"
        )
        assert result.data["createAmbassadorJobStatus"]["status"]["slug"] == slugify(
            "Test Status"
        )

        # Verify status was created
        status_id = result.data["createAmbassadorJobStatus"]["status"]["id"]
        status = await sync_to_async(models.Status.objects.get)(pk=status_id)
        assert status.name == "Test Status"
        # Compare tenant IDs to avoid async database access
        tenant_id = await sync_to_async(lambda: self.tenant.id)()
        assert status.tenant_id == tenant_id

    @pytest.mark.asyncio
    async def test_update_status_success(self):
        """Test successful status update."""
        # Create a status first
        status = await sync_to_async(self.create_status)(
            name="Original Status", tenant=self.tenant
        )

        mutation = """
        mutation UpdateStatus($input: UpdateStatusInput!) {
            updateAmbassadorJobStatus(input: $input) {
                success
                message
                status {
                    id
                    name
                    slug
                }
            }
        }
        """

        variables = {
            "input": {
                "id": str(status.id),
                "name": "Updated Status",
            }
        }

        result = await self._execute_mutation_authenticated(
            mutation, variables, self.client_user, self.endpoint_path
        )

        assert result.data is not None
        assert result.data["updateAmbassadorJobStatus"]["success"] is True
        assert (
            result.data["updateAmbassadorJobStatus"]["status"]["name"]
            == "Updated Status"
        )
        assert result.data["updateAmbassadorJobStatus"]["status"]["slug"] == slugify(
            "Original Status"
        )

        # Verify status was updated
        updated_status = await sync_to_async(models.Status.objects.get)(pk=status.id)
        assert updated_status.name == "Updated Status"
        assert updated_status.slug == slugify("Original Status")
