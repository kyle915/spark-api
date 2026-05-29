"""
Tenant-isolation coverage for the recap-form event picker resolver
(`recapEventOptions` on the clients schema).

This is the query behind "NEW RECAP -> Tell us how it went -> search
events". Unlike the general `events` query (an all-tenants admin
surface), this resolver must NEVER surface another client's events:
you can only file a recap against an event in the tenant you're acting
in. A prior fix (#343) scoped the picker on the FRONTEND only by always
passing `filters.tenantId`; this resolver enforces the same rule on the
server so no client mistake can leak cross-tenant rows.

These tests assert:
- a spark-admin acting inside a tenant (passing tenantId) sees ONLY
  that tenant's events, never another tenant's;
- a client user resolves to their own membership tenant even with no
  explicit filter, and never sees another tenant's events;
- a spark-admin with NO tenant in scope gets an EMPTY page (the
  server-side hard stop), not every tenant's events;
- the `q` search stays within the active tenant.
"""

import pytest
from datetime import datetime, timedelta, timezone as _tz
from asgiref.sync import sync_to_async

from ambassadors.tests.base import AmbassadorsGraphQLTestCase


RECAP_EVENT_OPTIONS_QUERY = """
query RecapEventOptions($tenantId: ID, $q: String, $first: Int) {
  recapEventOptions(tenantId: $tenantId, q: $q, first: $first) {
    totalCount
    edges {
      node {
        uuid
        name
        tenantId
        tenant {
          id
          name
        }
      }
    }
  }
}
"""


@pytest.mark.django_db(transaction=True)
class TestRecapEventOptionsTenantIsolation(AmbassadorsGraphQLTestCase):
    """The recap event picker must be STRICTLY scoped to the active tenant."""

    @pytest.fixture(autouse=True)
    def setup(self, db):
        from config.schema_client import schema_clients

        self.roles = self.setup_default_roles()
        self.schema = schema_clients
        self.endpoint_path = "/api/v1/graphql/clients"

        # Two tenants — the one we're acting in, and a foreign one whose
        # events must never appear in our picker.
        self.tenant = self.create_tenant(name="Girl Beer")
        self.other_tenant = self.create_tenant(name="Liquid Death")

        # Spark admin (unrestricted role) — the user that triggered the
        # leak in the field. Acts inside a tenant by passing tenantId.
        self.spark_admin = self.create_user(
            username="admin-recap-options",
            email="admin-recap-options@test.com",
            role=self.roles["spark_admin"],
        )

        # A client user that belongs to `self.tenant` only.
        self.client_user = self.create_user(
            username="client-recap-options",
            email="client-recap-options@test.com",
            role=self.roles["client"],
        )
        self.create_tenanted_user(self.client_user, self.tenant)

        now = datetime.now(_tz.utc)
        # Events for our tenant.
        self.ours_1 = self.create_event(
            name="Whole Foods Burbank",
            tenant=self.tenant,
            date=now,
            start_time=now,
            end_time=now + timedelta(hours=4),
        )
        self.ours_2 = self.create_event(
            name="H-E-B West Lake Hills",
            tenant=self.tenant,
            date=now,
            start_time=now,
            end_time=now + timedelta(hours=4),
        )
        # Event for the FOREIGN tenant — same venue chain name on purpose
        # ("Albertsons" was the leaking row Kyle saw) to prove the filter
        # is by tenant, not by name.
        self.theirs = self.create_event(
            name="Albertsons",
            tenant=self.other_tenant,
            date=now,
            start_time=now,
            end_time=now + timedelta(hours=4),
        )

    @pytest.mark.asyncio
    async def test_spark_admin_with_tenant_sees_only_that_tenant(self):
        result = await self._execute_query_authenticated(
            RECAP_EVENT_OPTIONS_QUERY,
            {"tenantId": str(self.tenant.id), "first": 50},
            self.spark_admin,
            self.endpoint_path,
        )
        assert result.errors is None, f"errored: {result.errors}"
        conn = result.data["recapEventOptions"]
        tenant_ids = {e["node"]["tenantId"] for e in conn["edges"]}
        names = {e["node"]["name"] for e in conn["edges"]}

        # Only our tenant's events; the foreign tenant's "Albertsons"
        # must be absent.
        assert tenant_ids == {str(self.tenant.id)}, tenant_ids
        assert "Albertsons" not in names
        assert {"Whole Foods Burbank", "H-E-B West Lake Hills"} <= names
        assert conn["totalCount"] == 2

    @pytest.mark.asyncio
    async def test_each_row_carries_a_client_brand_label(self):
        # The UI needs a visible client/brand name per row — make sure the
        # tenant relation is resolvable on every node.
        result = await self._execute_query_authenticated(
            RECAP_EVENT_OPTIONS_QUERY,
            {"tenantId": str(self.tenant.id), "first": 50},
            self.spark_admin,
            self.endpoint_path,
        )
        assert result.errors is None, f"errored: {result.errors}"
        edges = result.data["recapEventOptions"]["edges"]
        assert edges
        for e in edges:
            assert e["node"]["tenant"] is not None
            assert e["node"]["tenant"]["name"] == "Girl Beer"

    @pytest.mark.asyncio
    async def test_client_user_scoped_to_own_tenant_without_filter(self):
        # A client passes no tenantId — they must still be locked to their
        # own membership tenant and never see the foreign tenant's events.
        result = await self._execute_query_authenticated(
            RECAP_EVENT_OPTIONS_QUERY,
            {"first": 50},
            self.client_user,
            self.endpoint_path,
        )
        assert result.errors is None, f"errored: {result.errors}"
        conn = result.data["recapEventOptions"]
        tenant_ids = {e["node"]["tenantId"] for e in conn["edges"]}
        assert tenant_ids == {str(self.tenant.id)}, tenant_ids
        assert conn["totalCount"] == 2

    @pytest.mark.asyncio
    async def test_admin_without_tenant_gets_empty_not_all_tenants(self):
        # The critical guard: an unrestricted admin with NO tenant in
        # scope must get an EMPTY page, NOT every tenant's events. This is
        # what the general `events` resolver gets wrong and why the picker
        # leaked.
        result = await self._execute_query_authenticated(
            RECAP_EVENT_OPTIONS_QUERY,
            {"first": 50},
            self.spark_admin,
            self.endpoint_path,
        )
        assert result.errors is None, f"errored: {result.errors}"
        conn = result.data["recapEventOptions"]
        assert conn["edges"] == []
        assert conn["totalCount"] == 0

    @pytest.mark.asyncio
    async def test_search_stays_within_active_tenant(self):
        # Searching a term that also matches a foreign-tenant event name
        # must not pull that foreign row in. Add an "Albertsons" to OUR
        # tenant and confirm only ours comes back.
        now = datetime.now(_tz.utc)
        await sync_to_async(self.create_event)(
            name="Albertsons Pasadena",
            tenant=self.tenant,
            date=now,
            start_time=now,
            end_time=now + timedelta(hours=4),
        )
        result = await self._execute_query_authenticated(
            RECAP_EVENT_OPTIONS_QUERY,
            {"tenantId": str(self.tenant.id), "q": "Albertsons", "first": 50},
            self.spark_admin,
            self.endpoint_path,
        )
        assert result.errors is None, f"errored: {result.errors}"
        conn = result.data["recapEventOptions"]
        names = {e["node"]["name"] for e in conn["edges"]}
        tenant_ids = {e["node"]["tenantId"] for e in conn["edges"]}
        # Only our Albertsons; the foreign-tenant Albertsons stays out.
        assert names == {"Albertsons Pasadena"}, names
        assert tenant_ids == {str(self.tenant.id)}
