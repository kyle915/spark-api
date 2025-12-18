import pytest
from asgiref.sync import sync_to_async
from django.contrib.auth import get_user_model

from tenants.models import TenantTheme
from tenants.tests.base import BaseGraphQLTestCase


User = get_user_model()


@pytest.mark.django_db(transaction=True)
class TestTenantThemeGraphQL(BaseGraphQLTestCase):
    """GraphQL tests for TenantTheme queries and mutations."""

    @pytest.fixture(autouse=True)
    def setup(self, db):
        """Set up roles, tenant, and schema."""
        from config.schema_spark import schema_spark

        self.roles = self.setup_default_roles()
        self.schema = schema_spark
        self.endpoint_path = "/api/v1/graphql/spark"

        # Create base tenant
        self.tenant = self.create_tenant(name="Theme Tenant")

    async def _create_spark_admin_user(self) -> User:
        return await sync_to_async(self.create_user)(
            username="spark-admin-theme",
            email="spark-admin-theme@test.com",
            role=self.roles["spark_admin"],
            password="password123",
        )

    @pytest.mark.asyncio
    async def test_upsert_tenant_theme_creates_dark_theme_for_tenant(self):
        """Spark admin can create a dark theme for a tenant."""
        user = await self._create_spark_admin_user()

        mutation = """
        mutation UpsertTenantTheme($input: CreateOrUpdateTenantThemeInput!) {
          upsertTenantTheme(input: $input) {
            success
            message
            theme {
              colorScheme
              name
              cssVariables
            }
            clientMutationId
          }
        }
        """

        variables = {
            "input": {
                "tenantId": str(self.tenant.id),
                "colorScheme": "dark",
                "name": "Custom Dark",
                "cssVariables": {"--color-primary": "oklch(50% 0.2 200)"},
                "clientMutationId": "theme-create-1",
            }
        }

        result = await self._execute_mutation(
            mutation, variables, self.endpoint_path, user=user
        )

        assert result.data is not None
        payload = result.data["upsertTenantTheme"]
        assert payload["success"] is True
        assert payload["clientMutationId"] == "theme-create-1"
        assert payload["theme"]["colorScheme"] == "dark"
        assert payload["theme"]["name"] == "Custom Dark"
        assert payload["theme"]["cssVariables"][
            "--color-primary"] == "oklch(50% 0.2 200)"

        theme = await sync_to_async(
            lambda: TenantTheme.objects.get(
                tenant=self.tenant, color_scheme="dark")
        )()
        assert theme.name == "Custom Dark"
        assert theme.css_variables["--color-primary"] == "oklch(50% 0.2 200)"

    @pytest.mark.asyncio
    async def test_upsert_tenant_theme_updates_existing_theme(self):
        """Upserting with same (tenant, color_scheme) updates the existing theme."""
        user = await self._create_spark_admin_user()

        # Seed an initial theme
        await sync_to_async(TenantTheme.objects.create)(
            tenant=self.tenant,
            color_scheme="dark",
            name="Initial",
            created_by=user,
            updated_by=user,
        )

        mutation = """
        mutation UpsertTenantTheme($input: CreateOrUpdateTenantThemeInput!) {
          upsertTenantTheme(input: $input) {
            success
            message
            theme {
              id
              name
              colorScheme
              cssVariables
            }
          }
        }
        """

        variables = {
            "input": {
                "tenantId": str(self.tenant.id),
                "colorScheme": "dark",
                "name": "Updated Dark",
                "cssVariables": {"--color-primary": "oklch(60% 0.25 210)"},
            }
        }

        result = await self._execute_mutation(
            mutation, variables, self.endpoint_path, user=user
        )

        assert result.data is not None
        payload = result.data["upsertTenantTheme"]
        assert payload["success"] is True
        assert payload["theme"]["name"] == "Updated Dark"
        assert payload["theme"]["colorScheme"] == "dark"
        assert payload["theme"]["cssVariables"][
            "--color-primary"] == "oklch(60% 0.25 210)"

        # Ensure still only one theme for that tenant/scheme
        themes = await sync_to_async(
            lambda: list(
                TenantTheme.objects.filter(
                    tenant=self.tenant, color_scheme="dark")
            )
        )()
        assert len(themes) == 1
        assert themes[0].name == "Updated Dark"

    @pytest.mark.asyncio
    async def test_upsert_tenant_theme_requires_authentication(self):
        """Unauthenticated users cannot call upsertTenantTheme."""
        mutation = """
        mutation UpsertTenantTheme($input: CreateOrUpdateTenantThemeInput!) {
          upsertTenantTheme(input: $input) {
            success
            message
          }
        }
        """

        variables = {
            "input": {
                "tenantId": str(self.tenant.id),
                "colorScheme": "dark",
            }
        }

        result = await self._execute_mutation(
            mutation, variables, self.endpoint_path
        )

        assert result.data is not None
        payload = result.data["upsertTenantTheme"]
        assert payload["success"] is False
        assert "not authenticated" in payload["message"].lower()

    @pytest.mark.asyncio
    async def test_upsert_tenant_theme_requires_spark_admin_role(self):
        """Non-spark users cannot manage tenant themes."""
        # Regular client user
        user = await sync_to_async(self.create_user)(
            username="client-theme",
            email="client-theme@test.com",
            role=self.roles["client"],
            password="password123",
        )

        mutation = """
        mutation UpsertTenantTheme($input: CreateOrUpdateTenantThemeInput!) {
          upsertTenantTheme(input: $input) {
            success
            message
          }
        }
        """

        variables = {
            "input": {
                "tenantId": str(self.tenant.id),
                "colorScheme": "dark",
            }
        }

        result = await self._execute_mutation(
            mutation, variables, self.endpoint_path, user=user
        )

        assert result.data is not None
        payload = result.data["upsertTenantTheme"]
        assert payload["success"] is False
        assert "permission" in payload["message"].lower()

    @pytest.mark.asyncio
    async def test_tenant_theme_public_query_returns_theme_by_tenant_and_scheme(self):
        """Public tenantThemePublic query returns theme without auth."""
        system_user = self.get_system_user()
        dark_theme = await sync_to_async(TenantTheme.objects.create)(
            tenant=self.tenant,
            color_scheme="dark",
            name="Public Dark",
            created_by=system_user,
            updated_by=system_user,
        )

        query = """
        query TenantThemePublic($tenantId: ID!, $scheme: String!) {
          tenantThemePublic(tenantId: $tenantId, colorScheme: $scheme) {
            id
            name
            colorScheme
          }
        }
        """

        variables = {
            "tenantId": str(self.tenant.id),
            "scheme": "dark",
        }

        result = await self._execute_mutation(
            query, variables, self.endpoint_path
        )

        assert result.data is not None
        assert result.errors is None
        theme = result.data["tenantThemePublic"]
        assert theme is not None
        assert theme["id"] == str(dark_theme.id)
        assert theme["name"] == "Public Dark"
        assert theme["colorScheme"] == "dark"

    @pytest.mark.asyncio
    async def test_tenant_theme_public_query_is_accessible_without_auth(self):
        """tenantThemePublic can be accessed without authentication."""
        system_user = self.get_system_user()
        await sync_to_async(TenantTheme.objects.create)(
            tenant=self.tenant,
            color_scheme="dark",
            name="Anonymous Dark",
            created_by=system_user,
            updated_by=system_user,
        )

        query = """
        query TenantThemePublic($tenantId: ID!, $scheme: String!) {
          tenantThemePublic(tenantId: $tenantId, colorScheme: $scheme) {
            id
            name
            colorScheme
          }
        }
        """

        variables = {
            "tenantId": str(self.tenant.id),
            "scheme": "dark",
        }

        # No user passed -> unauthenticated request
        result = await self._execute_mutation(
            query, variables, self.endpoint_path
        )

        assert result.errors is None
        assert result.data is not None
        theme = result.data["tenantThemePublic"]
        assert theme is not None
