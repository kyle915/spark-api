"""Cross-tenant isolation tests for the Academy clients-schema ops.

Proves the tenant-scoping fix on the CLIENTS schema (the same bug class as
the Favorites leak fixed in PR #692): a client-role caller can neither read
nor mutate another tenant's academy modules by supplying that tenant's
``tenantId`` / module ``uuid``, while same-tenant access and admin
cross-tenant access still work.

Mirrors ``jobs/tests/test_favorite_ambassadors_graphql.py``.
"""

import pytest
from asgiref.sync import sync_to_async
from django.contrib.auth import get_user_model

from academy.models import AcademyModule
from config.schema_client import schema_clients
from tenants.tests.base import BaseGraphQLTestCase

User = get_user_model()


ADMIN_LIST_QUERY = """
query AdminAcademy($filters: AcademyModuleFiltersInput) {
  academyModulesAdmin(filters: $filters) {
    uuid
    title
    tenantId
  }
}
"""

MODULE_QUERY = """
query AcademyModule($uuid: ID!) {
  academyModule(uuid: $uuid) {
    uuid
    title
    tenantId
  }
}
"""

CREATE_MUTATION = """
mutation Create($input: CreateAcademyModuleInput!) {
  createAcademyModule(input: $input) {
    success
    message
    academyModule { uuid title tenantId }
  }
}
"""

UPDATE_MUTATION = """
mutation Update($input: UpdateAcademyModuleInput!) {
  updateAcademyModule(input: $input) {
    success
    academyModule { uuid title }
  }
}
"""

DELETE_MUTATION = """
mutation Delete($input: DeleteAcademyModuleInput!) {
  deleteAcademyModule(input: $input) {
    success
  }
}
"""


@pytest.mark.django_db(transaction=True)
class TestAcademyTenantIsolationGraphQL(BaseGraphQLTestCase):
    """Cross-tenant isolation for academyModulesAdmin / academyModule /
    create / update / delete academy module on the clients schema."""

    @pytest.fixture(autouse=True)
    def setup(self, db):
        self.roles = self.setup_default_roles()
        self.schema = schema_clients
        self.endpoint_path = "/api/v1/graphql/clients"
        self.tenant = self.create_tenant(name="Academy Mine")
        self.other_tenant = self.create_tenant(name="Academy Theirs")

    async def _client_user_for(self, username, email, tenant) -> User:
        user = await sync_to_async(self.create_user)(
            username=username,
            email=email,
            role=self.roles["client"],
            password="password123",
        )
        await sync_to_async(self.create_tenanted_user)(user=user, tenant=tenant)
        return user

    async def _admin_user(self, username, email) -> User:
        return await sync_to_async(self.create_user)(
            username=username,
            email=email,
            role=self.roles["spark_admin"],
            password="password123",
        )

    async def _module(self, tenant, title="Mod", **kw) -> AcademyModule:
        return await sync_to_async(AcademyModule.objects.create)(
            tenant=tenant, title=title, **kw
        )

    # -- reads ---------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_client_list_pinned_to_own_tenant(self):
        """A client passing another tenant's id is pinned to their own tenant."""
        user = await self._client_user_for("ac-list", "aclist@test.com", self.tenant)
        await self._module(self.tenant, "Mine A")
        await self._module(self.other_tenant, "Theirs A")

        # Ask for the OTHER tenant's modules -> scoped to caller's own tenant.
        result = await self._execute_mutation(
            ADMIN_LIST_QUERY,
            {"filters": {"tenantId": str(self.other_tenant.id)}},
            self.endpoint_path,
            user=user,
        )
        assert result.errors is None
        rows = result.data["academyModulesAdmin"]
        assert [r["title"] for r in rows] == ["Mine A"]
        assert all(r["tenantId"] == str(self.tenant.id) for r in rows)

    @pytest.mark.asyncio
    async def test_client_cannot_fetch_other_tenant_module_by_uuid(self):
        """academyModule(uuid) returns null for another tenant's module."""
        user = await self._client_user_for("ac-get", "acget@test.com", self.tenant)
        theirs = await self._module(self.other_tenant, "Secret Playbook")

        result = await self._execute_mutation(
            MODULE_QUERY,
            {"uuid": str(theirs.uuid)},
            self.endpoint_path,
            user=user,
        )
        assert result.errors is None
        assert result.data["academyModule"] is None

    @pytest.mark.asyncio
    async def test_client_can_fetch_own_tenant_module(self):
        user = await self._client_user_for("ac-get2", "acget2@test.com", self.tenant)
        mine = await self._module(self.tenant, "My Playbook")

        result = await self._execute_mutation(
            MODULE_QUERY,
            {"uuid": str(mine.uuid)},
            self.endpoint_path,
            user=user,
        )
        assert result.errors is None
        assert result.data["academyModule"]["uuid"] == str(mine.uuid)

    # -- writes --------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_client_create_pinned_to_own_tenant(self):
        """create with another tenant's id writes to the caller's OWN tenant."""
        user = await self._client_user_for("ac-add", "acadd@test.com", self.tenant)

        result = await self._execute_mutation(
            CREATE_MUTATION,
            {
                "input": {
                    "title": "Injected",
                    "tenantId": str(self.other_tenant.id),
                }
            },
            self.endpoint_path,
            user=user,
        )
        assert result.errors is None
        payload = result.data["createAcademyModule"]
        assert payload["success"] is True
        assert payload["academyModule"]["tenantId"] == str(self.tenant.id)

        # Nothing landed on the targeted (other) tenant.
        leaked = await sync_to_async(
            AcademyModule.objects.filter(tenant_id=self.other_tenant.id).exists
        )()
        assert leaked is False

    @pytest.mark.asyncio
    async def test_client_cannot_update_other_tenant_module(self):
        """update of another tenant's module is denied (success=False) and the
        row is unchanged."""
        user = await self._client_user_for("ac-upd", "acupd@test.com", self.tenant)
        theirs = await self._module(self.other_tenant, "Original")

        result = await self._execute_mutation(
            UPDATE_MUTATION,
            {"input": {"uuid": str(theirs.uuid), "title": "Hacked"}},
            self.endpoint_path,
            user=user,
        )
        assert result.errors is None
        assert result.data["updateAcademyModule"]["success"] is False

        refreshed = await sync_to_async(AcademyModule.objects.get)(uuid=theirs.uuid)
        assert refreshed.title == "Original"

    @pytest.mark.asyncio
    async def test_client_cannot_delete_other_tenant_module(self):
        """delete of another tenant's module is denied; the row survives."""
        user = await self._client_user_for("ac-del", "acdel@test.com", self.tenant)
        theirs = await self._module(self.other_tenant, "Keep Me")

        result = await self._execute_mutation(
            DELETE_MUTATION,
            {"input": {"uuid": str(theirs.uuid)}},
            self.endpoint_path,
            user=user,
        )
        assert result.errors is None
        assert result.data["deleteAcademyModule"]["success"] is False

        survived = await sync_to_async(
            AcademyModule.objects.filter(uuid=theirs.uuid).exists
        )()
        assert survived is True

    @pytest.mark.asyncio
    async def test_client_can_update_own_tenant_module(self):
        user = await self._client_user_for("ac-upd2", "acupd2@test.com", self.tenant)
        mine = await self._module(self.tenant, "Mine Original")

        result = await self._execute_mutation(
            UPDATE_MUTATION,
            {"input": {"uuid": str(mine.uuid), "title": "Mine Updated"}},
            self.endpoint_path,
            user=user,
        )
        assert result.errors is None
        assert result.data["updateAcademyModule"]["success"] is True
        refreshed = await sync_to_async(AcademyModule.objects.get)(uuid=mine.uuid)
        assert refreshed.title == "Mine Updated"

    # -- admin ---------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_admin_can_target_any_tenant(self):
        """A spark-admin may list + mutate any tenant's modules via tenantId."""
        admin = await self._admin_user("ac-admin", "acadmin@test.com")
        theirs = await self._module(self.other_tenant, "Their Mod")

        # List the other tenant's modules.
        list_result = await self._execute_mutation(
            ADMIN_LIST_QUERY,
            {"filters": {"tenantId": str(self.other_tenant.id)}},
            self.endpoint_path,
            user=admin,
        )
        assert list_result.errors is None
        assert [r["title"] for r in list_result.data["academyModulesAdmin"]] == [
            "Their Mod"
        ]

        # Create under the other tenant.
        create_result = await self._execute_mutation(
            CREATE_MUTATION,
            {
                "input": {
                    "title": "Admin Made",
                    "tenantId": str(self.other_tenant.id),
                }
            },
            self.endpoint_path,
            user=admin,
        )
        assert create_result.errors is None
        assert create_result.data["createAcademyModule"]["success"] is True
        assert (
            create_result.data["createAcademyModule"]["academyModule"]["tenantId"]
            == str(self.other_tenant.id)
        )

        # Update the other tenant's existing module.
        update_result = await self._execute_mutation(
            UPDATE_MUTATION,
            {"input": {"uuid": str(theirs.uuid), "title": "Admin Edited"}},
            self.endpoint_path,
            user=admin,
        )
        assert update_result.errors is None
        assert update_result.data["updateAcademyModule"]["success"] is True
