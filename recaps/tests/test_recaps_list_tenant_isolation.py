"""
Tenant-isolation coverage for the recaps LIST resolvers (`recaps` and
`customRecaps` on the clients schema) — the "Your recaps" page.

On Girl Beer the list showed recaps that weren't Girl Beer's (an
LD-looking recap, an inflated count). Every web consumer of these
resolvers is a per-tenant surface that passes the active tenant; the only
way no tenant reaches the resolver is an unrestricted role (staff /
spark-admin) that sent none, in which case the old resolver returned
EVERY tenant's recaps. We mirror `recapEventOptions`: no tenant in scope
=> EMPTY page, never all tenants. Clients are already pinned to their own
tenant.

Tests assert:
- a spark-admin acting inside a tenant sees ONLY that tenant's recaps
  (legacy + custom), never another tenant's;
- a client user is locked to their own tenant even with no filter;
- a spark-admin with NO tenant in scope gets an EMPTY page, not all
  tenants' recaps.
"""

import pytest
from datetime import datetime, timedelta, timezone as _tz

from ambassadors.tests.base import AmbassadorsGraphQLTestCase
from recaps import models as recap_models


RECAPS_QUERY = """
query Recaps($tenantId: ID, $first: Int) {
  recaps(filters: { tenantId: $tenantId }, first: $first) {
    totalCount
    edges { node { uuid name approved event { name } } }
  }
}
"""

CUSTOM_RECAPS_QUERY = """
query CustomRecaps($tenantId: ID, $first: Int) {
  customRecaps(filters: { tenantId: $tenantId }, first: $first) {
    totalCount
    edges { node { uuid name approved } }
  }
}
"""


@pytest.mark.django_db(transaction=True)
class TestRecapsListTenantIsolation(AmbassadorsGraphQLTestCase):
    """The recaps list must be STRICTLY scoped to the active tenant."""

    @pytest.fixture(autouse=True)
    def setup(self, db):
        from config.schema_client import schema_clients

        self.roles = self.setup_default_roles()
        self.schema = schema_clients
        self.endpoint_path = "/api/v1/graphql/clients"
        system_user = self.get_system_user()

        self.tenant = self.create_tenant(name="Girl Beer")
        self.other_tenant = self.create_tenant(name="Liquid Death")

        self.spark_admin = self.create_user(
            username="admin-recaps-iso",
            email="admin-recaps-iso@test.com",
            role=self.roles["spark_admin"],
        )
        self.client_user = self.create_user(
            username="client-recaps-iso",
            email="client-recaps-iso@test.com",
            role=self.roles["client"],
        )
        self.create_tenanted_user(self.client_user, self.tenant)

        now = datetime.now(_tz.utc)
        # Events carry the tenant; recap tenant is derived via event.tenant.
        self.our_event = self.create_event(
            name="Whole Foods Burbank",
            tenant=self.tenant,
            date=now,
            start_time=now,
            end_time=now + timedelta(hours=4),
        )
        self.their_event = self.create_event(
            name="Valero Grand Opening",
            tenant=self.other_tenant,
            date=now,
            start_time=now,
            end_time=now + timedelta(hours=4),
        )

        # Legacy recaps: one per tenant. Ours approved, theirs approved
        # (approval is irrelevant to tenant scoping — both should be
        # excluded by tenant, not by status, here).
        self.our_recap = recap_models.Recap.objects.create(
            name="Girl Beer recap",
            approved=True,
            event=self.our_event,
            created_by=system_user,
            updated_by=system_user,
        )
        self.their_recap = recap_models.Recap.objects.create(
            name="Liquid Death recap",
            approved=True,
            event=self.their_event,
            created_by=system_user,
            updated_by=system_user,
        )

        # Custom recaps: one per tenant (require a template).
        self.our_event_type = self.create_event_type(
            name="Sampling", tenant=self.tenant
        )
        self.their_event_type = self.create_event_type(
            name="Sampling", tenant=self.other_tenant
        )
        self.our_template = recap_models.CustomRecapTemplate.objects.create(
            name="GB Template",
            event_type=self.our_event_type,
            tenant=self.tenant,
            created_by=system_user,
        )
        self.their_template = recap_models.CustomRecapTemplate.objects.create(
            name="LD Template",
            event_type=self.their_event_type,
            tenant=self.other_tenant,
            created_by=system_user,
        )
        self.our_custom = recap_models.CustomRecap.objects.create(
            name="Girl Beer custom recap",
            approved=True,
            event=self.our_event,
            tenant=self.tenant,
            custom_recap_template=self.our_template,
            created_by=system_user,
            updated_by=system_user,
        )
        self.their_custom = recap_models.CustomRecap.objects.create(
            name="Liquid Death custom recap",
            approved=True,
            event=self.their_event,
            tenant=self.other_tenant,
            custom_recap_template=self.their_template,
            created_by=system_user,
            updated_by=system_user,
        )

    @pytest.mark.asyncio
    async def test_admin_with_tenant_sees_only_that_tenant_recaps(self):
        result = await self._execute_query_authenticated(
            RECAPS_QUERY,
            {"tenantId": str(self.tenant.id), "first": 50},
            self.spark_admin,
            self.endpoint_path,
        )
        assert result.errors is None, f"errored: {result.errors}"
        conn = result.data["recaps"]
        names = {e["node"]["name"] for e in conn["edges"]}
        assert names == {"Girl Beer recap"}, names
        assert "Liquid Death recap" not in names
        assert conn["totalCount"] == 1

    @pytest.mark.asyncio
    async def test_admin_with_tenant_sees_only_that_tenant_custom_recaps(self):
        result = await self._execute_query_authenticated(
            CUSTOM_RECAPS_QUERY,
            {"tenantId": str(self.tenant.id), "first": 50},
            self.spark_admin,
            self.endpoint_path,
        )
        assert result.errors is None, f"errored: {result.errors}"
        conn = result.data["customRecaps"]
        names = {e["node"]["name"] for e in conn["edges"]}
        assert names == {"Girl Beer custom recap"}, names
        assert "Liquid Death custom recap" not in names
        assert conn["totalCount"] == 1

    @pytest.mark.asyncio
    async def test_client_user_scoped_to_own_tenant_without_filter(self):
        result = await self._execute_query_authenticated(
            RECAPS_QUERY,
            {"first": 50},
            self.client_user,
            self.endpoint_path,
        )
        assert result.errors is None, f"errored: {result.errors}"
        conn = result.data["recaps"]
        names = {e["node"]["name"] for e in conn["edges"]}
        assert names == {"Girl Beer recap"}, names
        assert conn["totalCount"] == 1

    @pytest.mark.asyncio
    async def test_admin_without_tenant_gets_empty_not_all_tenants(self):
        # The critical guard for the list: no tenant in scope => EMPTY,
        # never every tenant's recaps.
        result = await self._execute_query_authenticated(
            RECAPS_QUERY,
            {"first": 50},
            self.spark_admin,
            self.endpoint_path,
        )
        assert result.errors is None, f"errored: {result.errors}"
        conn = result.data["recaps"]
        assert conn["edges"] == []
        assert conn["totalCount"] == 0

    @pytest.mark.asyncio
    async def test_admin_without_tenant_gets_empty_custom_recaps(self):
        result = await self._execute_query_authenticated(
            CUSTOM_RECAPS_QUERY,
            {"first": 50},
            self.spark_admin,
            self.endpoint_path,
        )
        assert result.errors is None, f"errored: {result.errors}"
        conn = result.data["customRecaps"]
        assert conn["edges"] == []
        assert conn["totalCount"] == 0
