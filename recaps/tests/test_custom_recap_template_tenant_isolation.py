"""
Tenant-isolation coverage for the custom recap TEMPLATE resolvers
(`customRecapTemplates` and `customRecapTemplate` on the clients schema).

A CustomRecapTemplate defines which fields the recap-build form renders.
If template resolution leaks across tenants, the "NEW RECAP" form draws
the WRONG client's template — the live bug Kyle hit on Girl Beer, whose
form rendered Liquid Death's fields instead of Girl Beer's own
("Men/Women who sampled", "Most Common Question", etc.).

The old resolver passed `filters.tenant_id` straight through with NO
server-side scoping when absent, so an unrestricted role (staff /
spark-admin) with an empty tenant filter got EVERY tenant's templates and
the frontend was the only guard — the same frontend-only pattern that
re-broke for the events picker (#343).

These tests mirror `test_recap_event_options.py` and assert:
- a spark-admin acting inside a tenant (passing tenantId) sees ONLY that
  tenant's templates, never another tenant's;
- a client user resolves to their own membership tenant even with no
  explicit filter, and never sees another tenant's templates;
- a spark-admin with NO tenant in scope gets an EMPTY page (the
  server-side hard stop), not every tenant's templates;
- the single `customRecapTemplate` lookup won't return a foreign tenant's
  template even when asked for it by raw uuid while a tenant is in scope.
"""

import pytest

from ambassadors.tests.base import AmbassadorsGraphQLTestCase
from recaps import models as recap_models


CUSTOM_RECAP_TEMPLATES_QUERY = """
query CustomRecapTemplates($tenantId: ID, $first: Int) {
  customRecapTemplates(filters: { tenantId: $tenantId }, first: $first) {
    totalCount
    edges {
      node {
        uuid
        name
        tenant {
          id
          name
        }
      }
    }
  }
}
"""

CUSTOM_RECAP_TEMPLATE_QUERY = """
query CustomRecapTemplate($uuid: ID, $tenantId: ID) {
  customRecapTemplate(uuid: $uuid, tenantId: $tenantId) {
    uuid
    name
    tenant {
      id
      name
    }
  }
}
"""


@pytest.mark.django_db(transaction=True)
class TestCustomRecapTemplateTenantIsolation(AmbassadorsGraphQLTestCase):
    """Custom recap template resolution must be STRICTLY tenant-scoped."""

    @pytest.fixture(autouse=True)
    def setup(self, db):
        from config.schema_client import schema_clients

        self.roles = self.setup_default_roles()
        self.schema = schema_clients
        self.endpoint_path = "/api/v1/graphql/clients"
        system_user = self.get_system_user()

        # Two tenants — the one we're acting in (Girl Beer) and a foreign
        # one (Liquid Death) whose template must never appear in our form.
        self.tenant = self.create_tenant(name="Girl Beer")
        self.other_tenant = self.create_tenant(name="Liquid Death")

        # Spark admin (unrestricted role) — the user that triggered the
        # leak. Acts inside a tenant by passing tenantId.
        self.spark_admin = self.create_user(
            username="admin-tpl-iso",
            email="admin-tpl-iso@test.com",
            role=self.roles["spark_admin"],
        )

        # A client user that belongs to `self.tenant` only.
        self.client_user = self.create_user(
            username="client-tpl-iso",
            email="client-tpl-iso@test.com",
            role=self.roles["client"],
        )
        self.create_tenanted_user(self.client_user, self.tenant)

        # An EventType per tenant (CustomRecapTemplate requires one).
        self.event_type = self.create_event_type(
            name="Sampling", tenant=self.tenant
        )
        self.other_event_type = self.create_event_type(
            name="Sampling", tenant=self.other_tenant
        )

        # Girl Beer's own template (the one the form SHOULD load).
        self.ours = recap_models.CustomRecapTemplate.objects.create(
            name="Girl Beer Sampling Recap",
            event_type=self.event_type,
            tenant=self.tenant,
            created_by=system_user,
        )
        # Liquid Death's template — must NEVER surface in Girl Beer's form.
        self.theirs = recap_models.CustomRecapTemplate.objects.create(
            name="Liquid Death Sampling Recap",
            event_type=self.other_event_type,
            tenant=self.other_tenant,
            created_by=system_user,
        )

    @pytest.mark.asyncio
    async def test_spark_admin_with_tenant_sees_only_that_tenant(self):
        result = await self._execute_query_authenticated(
            CUSTOM_RECAP_TEMPLATES_QUERY,
            {"tenantId": str(self.tenant.id), "first": 50},
            self.spark_admin,
            self.endpoint_path,
        )
        assert result.errors is None, f"errored: {result.errors}"
        conn = result.data["customRecapTemplates"]
        names = {e["node"]["name"] for e in conn["edges"]}
        tenant_names = {e["node"]["tenant"]["name"] for e in conn["edges"]}

        assert names == {"Girl Beer Sampling Recap"}, names
        assert tenant_names == {"Girl Beer"}, tenant_names
        assert "Liquid Death Sampling Recap" not in names
        assert conn["totalCount"] == 1

    @pytest.mark.asyncio
    async def test_client_user_scoped_to_own_tenant_without_filter(self):
        # A client passes no tenantId — they must still be locked to their
        # own membership tenant and never see the foreign template.
        result = await self._execute_query_authenticated(
            CUSTOM_RECAP_TEMPLATES_QUERY,
            {"first": 50},
            self.client_user,
            self.endpoint_path,
        )
        assert result.errors is None, f"errored: {result.errors}"
        conn = result.data["customRecapTemplates"]
        names = {e["node"]["name"] for e in conn["edges"]}
        assert names == {"Girl Beer Sampling Recap"}, names
        assert conn["totalCount"] == 1

    @pytest.mark.asyncio
    async def test_admin_without_tenant_gets_empty_not_all_tenants(self):
        # The critical guard: an unrestricted admin with NO tenant in scope
        # must get an EMPTY page, NOT every tenant's templates. This is the
        # exact leak that made Girl Beer's form render Liquid Death fields.
        result = await self._execute_query_authenticated(
            CUSTOM_RECAP_TEMPLATES_QUERY,
            {"first": 50},
            self.spark_admin,
            self.endpoint_path,
        )
        assert result.errors is None, f"errored: {result.errors}"
        conn = result.data["customRecapTemplates"]
        assert conn["edges"] == []
        assert conn["totalCount"] == 0

    @pytest.mark.asyncio
    async def test_single_template_lookup_refuses_foreign_tenant(self):
        # Even asked for the foreign template by its exact uuid while acting
        # in our tenant, the single resolver returns None (not the other
        # client's template) — indistinguishable from "not found".
        result = await self._execute_query_authenticated(
            CUSTOM_RECAP_TEMPLATE_QUERY,
            {"uuid": str(self.theirs.uuid), "tenantId": str(self.tenant.id)},
            self.spark_admin,
            self.endpoint_path,
        )
        assert result.errors is None, f"errored: {result.errors}"
        assert result.data["customRecapTemplate"] is None

    @pytest.mark.asyncio
    async def test_single_template_lookup_returns_own_tenant(self):
        result = await self._execute_query_authenticated(
            CUSTOM_RECAP_TEMPLATE_QUERY,
            {"uuid": str(self.ours.uuid), "tenantId": str(self.tenant.id)},
            self.spark_admin,
            self.endpoint_path,
        )
        assert result.errors is None, f"errored: {result.errors}"
        node = result.data["customRecapTemplate"]
        assert node is not None
        assert node["name"] == "Girl Beer Sampling Recap"
        assert node["tenant"]["name"] == "Girl Beer"
