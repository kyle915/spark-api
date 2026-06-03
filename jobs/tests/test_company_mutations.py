"""
Tests for Company mutations in the jobs app.

This module tests:
- create_company
- update_company
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
class TestClientCompanyMutations(JobsGraphQLTestCase):
    """Tests for Company mutations (Client schema)."""

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
        self.schema = schema_clients
        self.endpoint_path = "/api/v1/graphql/clients"

    @pytest.mark.asyncio
    async def test_create_company_success(self):
        """Test successful company creation."""
        mutation = """
        mutation CreateCompany($input: CreateCompanyInput!) {
            createCompany(input: $input) {
                success
                message
                company {
                    id
                    uuid
                    name
                    email
                    phone
                }
            }
        }
        """

        variables = {
            "input": {
                "name": "Test Company Inc",
                "email": "company@test.com",
                "phone": "123-456-7890",
            }
        }

        result = await self._execute_mutation_authenticated(
            mutation, variables, self.client_user, self.endpoint_path)

        assert result.data is not None, f"Result data is None. Errors: {result.errors}"
        assert result.data["createCompany"]["success"] is True
        assert result.data["createCompany"]["company"] is not None
        assert result.data["createCompany"]["company"]["name"] == "Test Company Inc"
        assert result.data["createCompany"]["company"]["email"] == "company@test.com"

        # Verify company was created
        company_gid = result.data["createCompany"]["company"]["id"]
        company_id = int(base64.b64decode(company_gid).decode("utf-8").split(":")[1])
        company = await sync_to_async(models.Company.objects.get)(pk=company_id)
        assert company.name == "Test Company Inc"
        assert company.email == "company@test.com"
        # Compare tenant IDs to avoid async database access
        tenant_id = await sync_to_async(lambda: self.tenant.id)()
        assert company.tenant_id == tenant_id

    @pytest.mark.asyncio
    async def test_update_company_success(self):
        """Test successful company update."""
        # Create a company first
        company = await sync_to_async(self.create_company)(
            name="Original Company",
            email="original@test.com",
            phone="111-111-1111",
            tenant=self.tenant
        )

        mutation = """
        mutation UpdateCompany($input: UpdateCompanyInput!) {
            updateCompany(input: $input) {
                success
                message
                company {
                    id
                    name
                    email
                }
            }
        }
        """

        variables = {
            "input": {
                "id": str(company.id),
                "name": "Updated Company",
                "email": "updated@test.com",
                "phone": "222-222-2222",
            }
        }

        result = await self._execute_mutation_authenticated(
            mutation, variables, self.client_user, self.endpoint_path)

        assert result.data is not None
        assert result.data["updateCompany"]["success"] is True
        assert result.data["updateCompany"]["company"]["name"] == "Updated Company"
        assert result.data["updateCompany"]["company"]["email"] == "updated@test.com"

        # Verify company was updated
        updated_company = await sync_to_async(models.Company.objects.get)(pk=company.id)
        assert updated_company.name == "Updated Company"
        assert updated_company.email == "updated@test.com"
