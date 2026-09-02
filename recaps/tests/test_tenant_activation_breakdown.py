"""Server-side Insights activation-type counts.

Counts must come from a DB GROUP BY over all matching Requests — never a
first-N client page — and empty / out-of-scope tenants must degrade to
zeros rather than GraphQL errors.
"""

from datetime import timedelta

import pytest
from django.utils import timezone

from ambassadors.tests.base import AmbassadorsGraphQLTestCase
from recaps.tenant_overview import tenant_activation_breakdown


ACTIVATION_QUERY = """
query($tenantId: ID!, $start: String, $end: String) {
  tenantActivationBreakdown(
    tenantId: $tenantId
    startDate: $start
    endDate: $end
  ) {
    startDate
    endDate
    buckets { key label count }
    byType { name count }
  }
}
"""


@pytest.mark.django_db(transaction=True)
class TestTenantActivationBreakdown(AmbassadorsGraphQLTestCase):
    @pytest.fixture(autouse=True)
    def setup(self, db):
        from config.schema_client import schema_clients

        self.roles = self.setup_default_roles()
        self.schema = schema_clients
        self.endpoint_path = "/api/v1/graphql/clients"
        self.sys = self.get_system_user()

        self.tenant = self.create_tenant(name="Activation Tenant")
        self.location = self.create_location(
            name="HQ", code="HQ", zip_code="94105", tenant=self.tenant
        )
        self.client_row = self.create_client(
            name="Brand", email="brand@example.com", tenant=self.tenant
        )
        self.distributor = self.create_distributor(
            name="Distro",
            email="distro@example.com",
            location=self.location,
            tenant=self.tenant,
        )
        self.retailer = self.create_retailer(
            name="Retailer",
            address="1 Main",
            store_contact="mgr",
            location=self.location,
            tenant=self.tenant,
        )
        self.status_done = self.create_request_status(
            name="Done", tenant=self.tenant, create_event=True
        )
        self.status_pending = self.create_request_status(
            name="Pending", tenant=self.tenant
        )
        self.type_retail = self.create_request_type(
            "Retail Sampling", self.tenant
        )
        self.type_bar = self.create_request_type("On-Prem Bar Night", self.tenant)
        self.type_event = self.create_request_type(
            "Festival Activation", self.tenant
        )
        self.type_other = self.create_request_type("Misc Ops", self.tenant)
        self.today = timezone.now().date()

        self.spark_admin = self.create_user(
            username="act-admin",
            email="act-admin@igniteproductions.co",
            role=self.roles["spark_admin"],
        )

        # Seed one confirmed retail request so the GraphQL test can stay
        # async-only (Django forbids ORM writes inside the async test body).
        self._req(self.type_retail, self.status_done)

    def _req(self, request_type, status, days_ago=0, name=None):
        when = self.today - timedelta(days=days_ago)
        return self.create_request(
            name=name or f"{request_type.name} {days_ago}",
            date=when,
            address="1 Main",
            client=self.client_row,
            distributor=self.distributor,
            retailer=self.retailer,
            request_type=request_type,
            tenant=self.tenant,
            status=status,
        )

    def test_buckets_confirmed_requests_by_type_name(self):
        self._req(self.type_retail, self.status_done, days_ago=1)
        self._req(self.type_bar, self.status_done)
        self._req(self.type_event, self.status_done)
        self._req(self.type_other, self.status_done)
        # Pending must not count.
        self._req(self.type_retail, self.status_pending, name="skip-me")

        data = tenant_activation_breakdown(self.tenant.id)
        by_key = {b["key"]: b["count"] for b in data["buckets"]}
        # setup already seeded one retail Done request.
        assert by_key["retail"] == 2
        assert by_key["onprem"] == 1
        assert by_key["event"] == 1
        assert by_key["other"] == 1
        assert sum(row["count"] for row in data["by_type"]) == 5

    def test_date_window_is_inclusive_on_request_date(self):
        self._req(self.type_retail, self.status_done, days_ago=10)
        self._req(self.type_retail, self.status_done, days_ago=40)

        start = self.today - timedelta(days=14)
        end = self.today
        data = tenant_activation_breakdown(self.tenant.id, start=start, end=end)
        by_key = {b["key"]: b["count"] for b in data["buckets"]}
        # setup's today request + the 10-day-ago one; 40-day-ago excluded.
        assert by_key["retail"] == 2

    @pytest.mark.asyncio
    async def test_graphql_returns_buckets(self):
        start = (self.today - timedelta(days=7)).isoformat()
        end = self.today.isoformat()
        result = await self._execute_query_authenticated(
            ACTIVATION_QUERY,
            {
                "tenantId": str(self.tenant.id),
                "start": start,
                "end": end,
            },
            self.spark_admin,
        )
        assert result.errors is None
        payload = result.data["tenantActivationBreakdown"]
        assert payload["startDate"] == start
        assert payload["endDate"] == end
        retail = next(b for b in payload["buckets"] if b["key"] == "retail")
        assert retail["count"] == 1
        assert retail["label"] == "Retail samplings"
