"""
Coverage for QueryClients.tenants — the company switcher on the admin web app
(which talks to the clients GraphQL schema).

Admins see EVERY active tenant; everyone else is scoped to their TenantedUser
memberships. "Admin" is resolved authoritatively and matches the data-layer
gate (_is_admin_access): staff, superuser, the spark-admin role, OR an
@igniteproductions.co email.

Regression guard for the fix that made the switcher honor the Ignite email
domain + spark-admin role — not just is_staff/is_superuser. Before it, an
Ignite-team member (e.g. madison@igniteproductions.co) without the staff flag
and without an explicit membership row saw "No companies associated with this
account" even though the data layer already let them act in any tenant.
"""

from __future__ import annotations

import pytest
from asgiref.sync import sync_to_async

from tenants.tests.base import BaseGraphQLTestCase

TENANTS_QUERY = """
query {
  tenants {
    edges { node { name } }
  }
}
"""


@pytest.mark.django_db(transaction=True)
class TestClientsTenantsSwitcher(BaseGraphQLTestCase):
    @pytest.fixture(autouse=True)
    def setup(self, db):
        from config.schema_client import schema_clients

        self.roles = self.setup_default_roles()
        self.tenant_a = self.create_tenant(name="Alpha Co")
        self.tenant_b = self.create_tenant(name="Bravo Co")
        self.schema = schema_clients
        self.endpoint_path = "/api/v1/graphql/clients"

    async def _tenant_names(self, user) -> set[str]:
        result = await self._execute_mutation(
            TENANTS_QUERY, {}, self.endpoint_path, user=user
        )
        assert result.errors is None, result.errors
        return {edge["node"]["name"] for edge in result.data["tenants"]["edges"]}

    @pytest.mark.asyncio
    async def test_ignite_email_sees_all_tenants_without_staff_or_membership(self):
        # @igniteproductions.co, NOT is_staff, NO membership row — must still
        # see every client (the Madison case). The role is intentionally
        # "client" to prove the EMAIL DOMAIN is what grants the full list.
        # NOTE: don't use madison@ itself — that address now sits on
        # IGNITE_ADMIN_EXCLUDE (demoted 2026-06-26), which strips exactly the
        # domain-admin grant this test asserts.
        teammate = await sync_to_async(self.create_user)(
            username="fieldops@igniteproductions.co",
            email="fieldops@igniteproductions.co",
            role=self.roles["client"],
            is_staff=False,
        )
        assert await self._tenant_names(teammate) == {"Alpha Co", "Bravo Co"}

    @pytest.mark.asyncio
    async def test_spark_admin_role_sees_all_tenants(self):
        # spark-admin role on a non-Ignite email, no staff flag — also sees all.
        admin = await sync_to_async(self.create_user)(
            username="ops@external.com",
            email="ops@external.com",
            role=self.roles["spark_admin"],
            is_staff=False,
        )
        assert await self._tenant_names(admin) == {"Alpha Co", "Bravo Co"}

    @pytest.mark.asyncio
    async def test_non_ignite_client_sees_only_their_membership(self):
        # A real client (non-Ignite, non-staff, non-admin) stays scoped to the
        # tenants they belong to — the isolation this resolver must preserve.
        client = await sync_to_async(self.create_user)(
            username="client@brand.com",
            email="client@brand.com",
            role=self.roles["client"],
            is_staff=False,
        )
        await sync_to_async(self.create_tenanted_user)(client, self.tenant_a)
        assert await self._tenant_names(client) == {"Alpha Co"}
