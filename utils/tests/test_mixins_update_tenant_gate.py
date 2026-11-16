"""
Cross-tenant write gate on the SHARED mutation service
(`utils.graphql.mixins.BaseMutationService.set_user_and_tenant`) — the
second copy of the pattern #1424 fixed in events/mutations.py. Used by the
jobs app, ambassador groups, attendance types/statuses/sources, etc.

Before: a non-admin could pass ANOTHER brand's row id to e.g. updateJobTitle;
save() loaded the row by raw id, edited it and wrote the caller's own
tenant_id onto it (edit + steal). Delete had the same hole.

Now, for non-admins the row's own tenant decides access (active
membership); a row outside it reads as "<Model> not found." and is left
untouched; an in-tenant update keeps the row's tenant. Admins are classified
from the DB and behave as before. Run through the real clients schema.
"""

import pytest
from asgiref.sync import sync_to_async

from jobs.models import JobTitle
from tenants.tests.base import BaseGraphQLTestCase

UPDATE_TITLE = """
mutation UpdateTitle($input: UpdateJobTitleInput!) {
  updateJobTitle(input: $input) { success message }
}
"""


@pytest.mark.django_db(transaction=True)
class TestMixinsUpdateTenantGate(BaseGraphQLTestCase):
    @pytest.fixture(autouse=True)
    def setup(self, db):
        from config.schema_client import schema_clients

        self.schema = schema_clients
        self.endpoint_path = "/api/v1/graphql/clients"
        self.roles = self.setup_default_roles()
        self.sys = self.get_system_user()
        self.tenant_a = self.create_tenant(name="Mixins Brand A")
        self.tenant_b = self.create_tenant(name="Mixins Brand B")

        self.admin = self.create_user(
            username="mx-admin", email="mx-admin@test.com",
            role=self.roles["spark_admin"],
        )
        self.client_a = self.create_user(
            username="mx-client-a", email="mx-client-a@test.com",
            role=self.roles["client"],
        )
        self.create_tenanted_user(self.client_a, self.tenant_a)
        self.client_b = self.create_user(
            username="mx-client-b", email="mx-client-b@test.com",
            role=self.roles["client"],
        )
        self.create_tenanted_user(self.client_b, self.tenant_b)

        self.title_b = JobTitle.objects.create(
            name="Brand B Lead", tenant=self.tenant_b, created_by=self.sys,
        )

    async def _update(self, user, name, tenant_id=None):
        data = {"id": str(self.title_b.id), "name": name}
        if tenant_id is not None:
            data["tenantId"] = str(tenant_id)
        return await self._execute_mutation(
            UPDATE_TITLE, {"input": data}, self.endpoint_path, user=user
        )

    async def _reload(self):
        return await sync_to_async(JobTitle.objects.get)(pk=self.title_b.pk)

    @pytest.mark.asyncio
    async def test_anonymous_refused(self):
        result = await self._update(None, "Hijacked")
        assert result.errors
        row = await self._reload()
        assert row.name == "Brand B Lead"

    @pytest.mark.asyncio
    async def test_other_tenant_client_cannot_edit_or_rehome(self):
        result = await self._update(self.client_a, "Hijacked")
        assert result.errors is None, result.errors
        payload = result.data["updateJobTitle"]
        assert payload["success"] is False
        assert payload["message"].endswith("not found.")
        row = await self._reload()
        assert row.name == "Brand B Lead"
        assert row.tenant_id == self.tenant_b.id

    @pytest.mark.asyncio
    async def test_other_tenant_client_naming_own_tenant_still_refused(self):
        result = await self._update(self.client_a, "Hijacked", self.tenant_a.id)
        assert result.data["updateJobTitle"]["success"] is False
        row = await self._reload()
        assert (row.name, row.tenant_id) == ("Brand B Lead", self.tenant_b.id)

    @pytest.mark.asyncio
    async def test_same_tenant_client_can_edit_and_tenant_is_kept(self):
        result = await self._update(self.client_b, "Renamed")
        assert result.data["updateJobTitle"]["success"] is True, result.data
        row = await self._reload()
        assert (row.name, row.tenant_id) == ("Renamed", self.tenant_b.id)

    @pytest.mark.asyncio
    async def test_inactive_membership_is_refused(self):
        from tenants.models import TenantedUser

        await sync_to_async(
            TenantedUser.objects.filter(user=self.client_b).update
        )(is_active=False)
        # Re-activate a membership elsewhere so the default-tenant lookup works.
        await sync_to_async(self.create_tenanted_user)(self.client_b, self.tenant_a)
        result = await self._update(self.client_b, "Renamed")
        assert result.data["updateJobTitle"]["success"] is False
        row = await self._reload()
        assert row.name == "Brand B Lead"

    @pytest.mark.asyncio
    async def test_admin_can_edit_any_tenant(self):
        result = await self._update(self.admin, "Admin Edit", self.tenant_b.id)
        assert result.data["updateJobTitle"]["success"] is True, result.data
        row = await self._reload()
        assert (row.name, row.tenant_id) == ("Admin Edit", self.tenant_b.id)

    @pytest.mark.asyncio
    async def test_staff_admin_without_spark_admin_role_keeps_row_tenant(self):
        """An is_staff user is an admin by the DB-backed check even though
        their role slug isn't spark-admin (so is_spark_schema_request says
        no). They edit any row, and with no tenantId the row keeps its own
        tenant instead of being re-homed into their default tenant."""
        staff = await sync_to_async(self.create_user)(
            username="mx-staff", email="mx-staff@test.com",
            role=self.roles["client"], is_staff=True,
        )
        await sync_to_async(self.create_tenanted_user)(staff, self.tenant_a)
        result = await self._update(staff, "Staff Edit")
        assert result.data["updateJobTitle"]["success"] is True, result.data
        row = await self._reload()
        assert (row.name, row.tenant_id) == ("Staff Edit", self.tenant_b.id)
