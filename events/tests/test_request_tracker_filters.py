"""Master Tracker server-side request filters (status, date, scheduling, pagination)."""

from datetime import timedelta

import pytest
from asgiref.sync import sync_to_async
from django.utils import timezone

from events import models as em
from events.tests.base import EventsGraphQLTestCase

REQUESTS_FILTER_Q = """
query Reqs($filters: RequestFiltersInput, $first: Int, $after: String) {
  requests(first: $first, after: $after, filters: $filters) {
    totalCount
    pageInfo { hasNextPage endCursor }
    edges { cursor node { uuid status { slug } schedulingStatus date } }
  }
}
"""

TRACKER_COUNTS_Q = """
query TrackerCounts($filters: RequestFiltersInput) {
  trackerStatusCounts(filters: $filters) {
    total
    buckets { slug count }
    marketCodes
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
        today = timezone.now().replace(hour=12, minute=0, second=0, microsecond=0)
        self.pending_req = em.Request.objects.create(
            name="Pending row",
            address="100 Main St, Austin, TX 78701",
            request_type=rt,
            tenant=self.tenant,
            status=self.pending_status,
            scheduling_status="needs_scheduling",
            date=today + timedelta(days=3),
            created_by=self.sys,
        )
        self.done_req = em.Request.objects.create(
            name="Done row",
            address="200 Oak Ave, Dallas, TX 75201",
            request_type=rt,
            tenant=self.tenant,
            status=self.done_status,
            scheduling_status="already_scheduled",
            date=today - timedelta(days=10),
            created_by=self.sys,
        )
        self.today = today

    @pytest.mark.asyncio
    async def test_status_slug_filter(self):
        res = await self._execute_mutation(
            REQUESTS_FILTER_Q,
            {
                "first": 50,
                "filters": {
                    "tenantId": str(self.tenant.id),
                    "statusSlug": "pending",
                },
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
                "first": 50,
                "filters": {
                    "tenantId": str(self.tenant.id),
                    "statusSlugs": ["pending", "done"],
                },
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
                "first": 50,
                "filters": {
                    "tenantId": str(self.tenant.id),
                    "schedulingStatus": "already_scheduled",
                },
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
                "first": 50,
                "filters": {
                    "tenantId": str(self.tenant.id),
                    "stateCode": "TX",
                },
            },
            user=self.admin,
        )
        assert res.errors is None, res.errors
        assert res.data["requests"]["totalCount"] == 2

        res_tx_only = await self._execute_mutation(
            REQUESTS_FILTER_Q,
            {
                "first": 50,
                "filters": {
                    "tenantId": str(self.tenant.id),
                    "stateCode": "TX",
                    "statusSlug": "done",
                },
            },
            user=self.admin,
        )
        assert res_tx_only.errors is None, res_tx_only.errors
        assert res_tx_only.data["requests"]["totalCount"] == 1

    @pytest.mark.asyncio
    async def test_date_range_filter(self):
        start = (self.today - timedelta(days=1)).date().isoformat()
        end = (self.today + timedelta(days=7)).date().isoformat()
        res = await self._execute_mutation(
            REQUESTS_FILTER_Q,
            {
                "first": 50,
                "filters": {
                    "tenantId": str(self.tenant.id),
                    "startDate": start,
                    "endDate": end,
                },
            },
            user=self.admin,
        )
        assert res.errors is None, res.errors
        assert res.data["requests"]["totalCount"] == 1
        assert res.data["requests"]["edges"][0]["node"]["status"]["slug"] == "pending"

    @pytest.mark.asyncio
    async def test_cursor_pagination_past_page(self):
        # Seed enough rows that first:2 has a next page.
        rt = await sync_to_async(em.RequestType.objects.get)(tenant=self.tenant)

        def _seed():
            for i in range(3):
                em.Request.objects.create(
                    name=f"Extra {i}",
                    address=f"{300 + i} Elm St, Houston, TX 77001",
                    request_type=rt,
                    tenant=self.tenant,
                    status=self.pending_status,
                    scheduling_status="needs_scheduling",
                    date=self.today + timedelta(days=20 + i),
                    created_by=self.sys,
                )

        await sync_to_async(_seed)()

        page1 = await self._execute_mutation(
            REQUESTS_FILTER_Q,
            {
                "first": 2,
                "filters": {"tenantId": str(self.tenant.id)},
            },
            user=self.admin,
        )
        assert page1.errors is None, page1.errors
        assert page1.data["requests"]["totalCount"] >= 5
        assert page1.data["requests"]["pageInfo"]["hasNextPage"] is True
        assert len(page1.data["requests"]["edges"]) == 2
        cursor = page1.data["requests"]["pageInfo"]["endCursor"]
        assert cursor

        page2 = await self._execute_mutation(
            REQUESTS_FILTER_Q,
            {
                "first": 2,
                "after": cursor,
                "filters": {"tenantId": str(self.tenant.id)},
            },
            user=self.admin,
        )
        assert page2.errors is None, page2.errors
        assert len(page2.data["requests"]["edges"]) == 2
        ids1 = {e["node"]["uuid"] for e in page1.data["requests"]["edges"]}
        ids2 = {e["node"]["uuid"] for e in page2.data["requests"]["edges"]}
        assert ids1.isdisjoint(ids2)

    @pytest.mark.asyncio
    async def test_tracker_status_counts_ignore_status_filter(self):
        res = await self._execute_mutation(
            TRACKER_COUNTS_Q,
            {
                "filters": {
                    "tenantId": str(self.tenant.id),
                    # Status chip must NOT shrink the buckets — chips need
                    # every slug so "Done · n" stays visible while browsing Active.
                    "statusSlug": "pending",
                    "startDate": (self.today - timedelta(days=30)).date().isoformat(),
                    "endDate": (self.today + timedelta(days=30)).date().isoformat(),
                }
            },
            user=self.admin,
        )
        assert res.errors is None, res.errors
        data = res.data["trackerStatusCounts"]
        by_slug = {b["slug"]: b["count"] for b in data["buckets"]}
        assert by_slug.get("pending", 0) >= 1
        assert by_slug.get("done", 0) >= 1
        assert data["total"] == sum(by_slug.values())
