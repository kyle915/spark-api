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
        if result.data["createTenant"]["success"] is False:
            print(f"DEBUG: {result.data['createTenant']['message']}")
        assert result.data["createTenant"]["success"] is True
        assert result.data["createTenant"]["tenant"]["name"] == "New Tenant"
        assert result.data["createTenant"]["clientMutationId"] == "test-123"

        # Verify database
        tenant = await sync_to_async(Tenant.objects.get)(name="New Tenant")
        assert tenant.name == "New Tenant"
        # Verify request_url_name format: 4 random chars + - + slugified name
        parts = tenant.request_url_name.split('-')
        assert len(parts) >= 2
        assert len(parts[0]) == 4
        assert tenant.request_url_name.endswith("new-tenant")

        # Verify automatic status creation
        from events.models import RequestStatus, EventStatus

        request_statuses = await sync_to_async(list)(RequestStatus.objects.filter(tenant=tenant))
        assert len(request_statuses) >= 3
        status_names = [s.name for s in request_statuses]
        assert "Pending" in status_names
        assert "Approved" in status_names
        assert "Decline" in status_names or "Declined" in status_names
        
        # Verify slugs
        for status in request_statuses:
            assert status.slug is not None
            if status.name == "Pending":
                assert status.slug == "pending"

        event_statuses = await sync_to_async(list)(EventStatus.objects.filter(tenant=tenant))
        assert len(event_statuses) >= 2
        event_status_names = [s.name for s in event_statuses]
        assert "Approved" in event_status_names
        assert "Decline" in event_status_names or "Declined" in event_status_names

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


@pytest.mark.django_db(transaction=True)
class TestCreateTenantOnClientsSchema(BaseGraphQLTestCase):
    """Regression guard: the CLIENTS GraphQL schema (the one the admin web app
    actually hits) must expose createTenant.

    It briefly didn't — MutationClients included only LinkedSheetMutations, not
    SparkTenantMutations — so onboarding a new client from the admin app failed
    with "Cannot query field 'createTenant' on type 'Mutation'".
    """

    _MUTATION = """
    mutation CreateTenant($input: CreateTenantInput!) {
        createTenant(input: $input) {
            success
            message
            tenant { id name uuid }
        }
    }
    """

    @pytest.fixture(autouse=True)
    def setup(self, db):
        from config.schema_client import schema_clients

        self.roles = self.setup_default_roles()
        self.schema = schema_clients
        self.endpoint_path = "/api/v1/graphql/clients"

    async def create_user_async(self, **kwargs):
        return await sync_to_async(self.create_user)(**kwargs)

    @pytest.mark.asyncio
    async def test_spark_admin_can_create_tenant_via_clients_schema(self):
        user = await self.create_user_async(
            username="sparkadmin_clients",
            email="sparkadmin_clients@test.com",
            role=self.roles["spark_admin"],
            password="password123",
        )
        result = await self._execute_mutation(
            self._MUTATION,
            {"input": {"name": "Alloy Fitness", "clientMutationId": "c-1"}},
            self.endpoint_path,
            user=user,
        )
        # The headline regression: the field must EXIST on the clients schema
        # (no "Cannot query field 'createTenant'") AND succeed for an admin.
        assert result.errors is None, f"errored: {result.errors}"
        assert result.data is not None
        assert result.data["createTenant"]["success"] is True
        assert result.data["createTenant"]["tenant"]["name"] == "Alloy Fitness"

    @pytest.mark.asyncio
    async def test_clients_schema_create_tenant_blocks_non_admin(self):
        user = await self.create_user_async(
            username="amb_clients",
            email="amb_clients@test.com",
            role=self.roles["ambassador"],
            password="password123",
        )
        result = await self._execute_mutation(
            self._MUTATION,
            {"input": {"name": "Should Not Exist"}},
            self.endpoint_path,
            user=user,
        )
        # Field exists on the clients schema, but the resolver's own
        # spark-admin self-gate still denies non-admins.
        assert result.errors is None, f"errored: {result.errors}"
        assert result.data["createTenant"]["success"] is False
        assert "permission" in result.data["createTenant"]["message"].lower()
