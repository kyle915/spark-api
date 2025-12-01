import pytest
from asgiref.sync import sync_to_async
from django.contrib.auth import get_user_model
from tenants.models import Tenant
from tenants.tests.base import BaseGraphQLTestCase
from utils.utils import ROLE_ID

User = get_user_model()

@pytest.mark.django_db(transaction=True)
class TestTenantMutations(BaseGraphQLTestCase):
    """Tests for SparkTenantMutations.create_tenant mutation."""

    @pytest.fixture(autouse=True)
    def setup(self, db):
        """Set up test data before each test."""
        from config.schema_spark import schema_spark
        self.roles = self.setup_default_roles()
        self.schema = schema_spark
        self.endpoint_path = "/api/v1/graphql/spark"

    @pytest.mark.asyncio
    async def test_create_tenant_success(self):
        """Test successful tenant creation by spark admin."""
        # Create spark admin user
        user = await self.create_user_async(
            username="sparkadmin",
            email="sparkadmin@test.com",
            role=self.roles['spark_admin'],
            password="password123"
        )
        
        mutation = """
        mutation CreateTenant($input: CreateTenantInput!) {
            createTenant(input: $input) {
                success
                message
                tenant {
                    id
                    name
                    uuid
                }
                clientMutationId
            }
        }
        """

        variables = {
            "input": {
                "name": "New Tenant",
                "clientMutationId": "test-123"
            }
        }
        
        result = await self._execute_mutation(
            mutation, variables, self.endpoint_path, user=user)

        assert result.data is not None
        assert result.data["createTenant"]["success"] is True
        assert result.data["createTenant"]["tenant"]["name"] == "New Tenant"
        assert result.data["createTenant"]["clientMutationId"] == "test-123"

        # Verify database
        tenant = await sync_to_async(Tenant.objects.get)(name="New Tenant")
        assert tenant.name == "New Tenant"
        # Verify request_url_name format: 4 chars + - + slugified name
        parts = tenant.request_url_name.split('-')
        assert len(parts) >= 2
        assert len(parts[0]) == 4
        assert tenant.request_url_name.endswith("new-tenant")

    @pytest.mark.asyncio
    async def test_create_tenant_not_authenticated(self):
        """Test tenant creation by unauthenticated user."""
        mutation = """
        mutation CreateTenant($input: CreateTenantInput!) {
            createTenant(input: $input) {
                success
                message
            }
        }
        """

        variables = {
            "input": {
                "name": "Unauthorized Tenant"
            }
        }

        result = await self._execute_mutation(
            mutation, variables, self.endpoint_path)

        assert result.data is not None
        assert result.data["createTenant"]["success"] is False
        assert "authenticated" in result.data["createTenant"]["message"].lower()

    @pytest.mark.asyncio
    async def test_create_tenant_not_authorized(self):
        """Test tenant creation by non-admin user."""
        # Create regular user (e.g. ambassador)
        user = await self.create_user_async(
            username="ambassador",
            email="ambassador@test.com",
            role=self.roles['ambassador'],
            password="password123"
        )

        mutation = """
        mutation CreateTenant($input: CreateTenantInput!) {
            createTenant(input: $input) {
                success
                message
            }
        }
        """

        variables = {
            "input": {
                "name": "Unauthorized Tenant"
            }
        }

        result = await self._execute_mutation(
            mutation, variables, self.endpoint_path, user=user)

        assert result.data is not None
        assert result.data["createTenant"]["success"] is False
        assert "permission" in result.data["createTenant"]["message"].lower()

    async def create_user_async(self, **kwargs):
        return await sync_to_async(self.create_user)(**kwargs)
