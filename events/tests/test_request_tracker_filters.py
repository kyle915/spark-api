"""Master Tracker server-side request filters (status slug, scheduling, state)."""

import pytest
from django.utils import timezone

from events import models as em
from events.tests.base import EventsGraphQLTestCase

REQUESTS_FILTER_Q = """
query Reqs($filters: RequestFiltersInput) {
  requests(first: 50, filters: $filters) {
    totalCount
    edges { node { uuid status { slug } schedulingStatus } }
  }
}
"""


@pytest.mark.django_db(transaction=True)
class TestRequestTrackerFilters(EventsGraphQLTestCase):
    @pytest.fixture(autouse=True)
    def setup(self, db):
        from config.schema_client import schema_clients

        self.schema = schema_clients
        self.endpoint_path = "/api/v1/graphql/clients"
        self.roles = self.setup_default_roles()
        self.tenant = self.create_tenant(name="Tracker Filters Tenant")
        self.admin = self.create_user(
            username="tracker-admin",
            email="admin@igniteproductions.co",
            role=self.roles["spark_admin"],
            is_staff=True,
        )
        self.sys = self.get_system_user()
        rt = em.RequestType.objects.create(
            name="Retail Sampling", tenant=self.tenant, created_by=self.sys
        )
        self.pending_status = self.create_request_status(
            name="Pending", tenant=self.tenant, slug="pending"
        )
        self.done_status = self.create_request_status(
            name="Done", tenant=self.tenant, slug="done"
        )
        self.pending_req = em.Request.objects.create(
            name="Pending row",
            address="100 Main St, Austin, TX 78701",
            request_type=rt,
            tenant=self.tenant,
            status=self.pending_status,
            scheduling_status="needs_scheduling",
            created_by=self.sys,
        )
        self.done_req = em.Request.objects.create(
            name="Done row",
            address="200 Oak Ave, Dallas, TX 75201",
            request_type=rt,
            tenant=self.tenant,
            status=self.done_status,
            scheduling_status="already_scheduled",
            created_by=self.sys,
        )

    @pytest.mark.asyncio
    async def test_status_slug_filter(self):
        res = await self._execute_mutation(
            REQUESTS_FILTER_Q,
            {
                "filters": {
                    "tenantId": str(self.tenant.id),
                    "statusSlug": "pending",
                }
            },
            user=self.admin,
        )
        assert res.errors is None, res.errors
        slugs = {e["node"]["status"]["slug"] for e in res.data["requests"]["edges"]}
        assert slugs == {"pending"}

    @pytest.mark.asyncio
    async def test_status_slugs_filter(self):
        res = await self._execute_mutation(
            REQUESTS_FILTER_Q,
            {
                "filters": {
                    "tenantId": str(self.tenant.id),
                    "statusSlugs": ["pending", "done"],
                }
            },
            user=self.admin,
        )
        assert res.errors is None, res.errors
        assert res.data["requests"]["totalCount"] == 2

    @pytest.mark.asyncio
    async def test_scheduling_status_filter(self):
        res = await self._execute_mutation(
            REQUESTS_FILTER_Q,
            {
                "filters": {
                    "tenantId": str(self.tenant.id),
                    "schedulingStatus": "already_scheduled",
                }
            },
            user=self.admin,
        )
        assert res.errors is None, res.errors
        nodes = [e["node"] for e in res.data["requests"]["edges"]]
        assert len(nodes) == 1
        assert nodes[0]["schedulingStatus"] == "already_scheduled"

    @pytest.mark.asyncio
    async def test_state_code_filter(self):
        res = await self._execute_mutation(
            REQUESTS_FILTER_Q,
            {
                "filters": {
                    "tenantId": str(self.tenant.id),
                    "stateCode": "TX",
                }
            },
            user=self.admin,
        )
        assert res.errors is None, res.errors
        assert res.data["requests"]["totalCount"] == 2

        res_tx_only = await self._execute_mutation(
            REQUESTS_FILTER_Q,
            {
                "filters": {
                    "tenantId": str(self.tenant.id),
                    "stateCode": "TX",
                    "statusSlug": "done",
                }
            },
            user=self.admin,
        )
        assert res_tx_only.errors is None, res_tx_only.errors
        assert res_tx_only.data["requests"]["totalCount"] == 1
