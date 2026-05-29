"""
Regression coverage for the "Your recaps" LIST resolvers (`recaps` and
`customRecaps` on the clients schema) returning the FULL tenant set.

The web admin loads the whole tenant in one page and does date-range /
search / status / retailer / state filtering CLIENT-SIDE over the rows it
received. The connection previously capped at max_limit=50, so on a tenant
with hundreds of recaps (Liquid Death: 830) only the newest ~50 were ever
reachable: paging couldn't pass the cap and a date filter set to an old
window found nothing, even though totalCount correctly reported the full
count. The fix lifts the page ceiling on these two resolvers
(RECAPS_LIST_MAX_LIMIT = 2000) so one large `first` pages through the
complete tenant set (same class of fix as the Master Tracker `requests`
resolver, #633).

These tests assert:
- a single `first` larger than the OLD cap returns ALL of a tenant's
  recaps (legacy + custom), reconciling edges with totalCount;
- a date-range filter set to an OLD window surfaces the matching older
  recaps (which live beyond the newest-50 the old cap returned), proving
  date-search reaches any date once the full set is returnable.
"""

import pytest
from datetime import datetime, timedelta, timezone as _tz

from ambassadors.tests.base import AmbassadorsGraphQLTestCase
from recaps import models as recap_models


# Bigger than the OLD cap (50) so a pre-fix resolver would clamp and fail.
RECAP_COUNT = 60

RECAPS_QUERY = """
query Recaps($tenantId: ID, $first: Int, $startDate: String, $endDate: String) {
  recaps(
    filters: { tenantId: $tenantId, startDate: $startDate, endDate: $endDate }
    first: $first
  ) {
    totalCount
    edges { node { uuid name event { name date } } }
  }
}
"""

CUSTOM_RECAPS_QUERY = """
query CustomRecaps($tenantId: ID, $first: Int, $startDate: String, $endDate: String) {
  customRecaps(
    filters: { tenantId: $tenantId, startDate: $startDate, endDate: $endDate }
    first: $first
  ) {
    totalCount
    edges { node { uuid name event { name date } } }
  }
}
"""


@pytest.mark.django_db(transaction=True)
class TestRecapsListReachAll(AmbassadorsGraphQLTestCase):
    """Every recap for the active tenant must be reachable in one page."""

    @pytest.fixture(autouse=True)
    def setup(self, db):
        from config.schema_client import schema_clients

        self.roles = self.setup_default_roles()
        self.schema = schema_clients
        self.endpoint_path = "/api/v1/graphql/clients"
        self.system_user = self.get_system_user()

        self.tenant = self.create_tenant(name="Liquid Death")
        # A second tenant whose rows must never bleed into the counts —
        # guards that lifting the cap didn't regress tenant scoping.
        self.other_tenant = self.create_tenant(name="Girl Beer")

        self.spark_admin = self.create_user(
            username="admin-reach-all",
            email="admin-reach-all@test.com",
            role=self.roles["spark_admin"],
        )

        self.now = datetime.now(_tz.utc)

        # Anchor dates well in the past so the date-window assertion below
        # exercises rows the OLD newest-50 cap would never have returned.
        # Spread one recap per day going backwards from ~120 days ago.
        base = self.now - timedelta(days=120)

        self.event_type = self.create_event_type(
            name="Sampling", tenant=self.tenant
        )
        self.template = recap_models.CustomRecapTemplate.objects.create(
            name="LD Template",
            event_type=self.event_type,
            tenant=self.tenant,
            created_by=self.system_user,
        )

        self.legacy_recaps = []
        self.custom_recaps = []
        self.legacy_dates = []
        for i in range(RECAP_COUNT):
            event_dt = base + timedelta(days=i)
            self.legacy_dates.append(event_dt)
            event = self.create_event(
                name=f"LD Event {i:03d}",
                tenant=self.tenant,
                date=event_dt,
                start_time=event_dt,
                end_time=event_dt + timedelta(hours=4),
            )
            self.legacy_recaps.append(
                recap_models.Recap.objects.create(
                    name=f"LD recap {i:03d}",
                    approved=True,
                    event=event,
                    created_by=self.system_user,
                    updated_by=self.system_user,
                )
            )
            self.custom_recaps.append(
                recap_models.CustomRecap.objects.create(
                    name=f"LD custom recap {i:03d}",
                    approved=True,
                    event=event,
                    tenant=self.tenant,
                    custom_recap_template=self.template,
                    created_by=self.system_user,
                    updated_by=self.system_user,
                )
            )

        # Other-tenant noise (one of each) — must stay out of LD's counts.
        other_event = self.create_event(
            name="GB Event",
            tenant=self.other_tenant,
            date=self.now,
            start_time=self.now,
            end_time=self.now + timedelta(hours=4),
        )
        other_event_type = self.create_event_type(
            name="Sampling", tenant=self.other_tenant
        )
        other_template = recap_models.CustomRecapTemplate.objects.create(
            name="GB Template",
            event_type=other_event_type,
            tenant=self.other_tenant,
            created_by=self.system_user,
        )
        recap_models.Recap.objects.create(
            name="GB recap",
            approved=True,
            event=other_event,
            created_by=self.system_user,
            updated_by=self.system_user,
        )
        recap_models.CustomRecap.objects.create(
            name="GB custom recap",
            approved=True,
            event=other_event,
            tenant=self.other_tenant,
            custom_recap_template=other_template,
            created_by=self.system_user,
            updated_by=self.system_user,
        )

    @pytest.mark.asyncio
    async def test_legacy_recaps_all_reachable_in_one_page(self):
        # A single `first` larger than the OLD cap must return EVERY recap
        # for the tenant — not just the newest 50.
        result = await self._execute_query_authenticated(
            RECAPS_QUERY,
            {"tenantId": str(self.tenant.id), "first": 1000},
            self.spark_admin,
            self.endpoint_path,
        )
        assert result.errors is None, f"errored: {result.errors}"
        conn = result.data["recaps"]
        assert conn["totalCount"] == RECAP_COUNT, conn["totalCount"]
        # edges reconcile with totalCount — the whole set came back.
        assert len(conn["edges"]) == RECAP_COUNT, len(conn["edges"])
        names = {e["node"]["name"] for e in conn["edges"]}
        assert len(names) == RECAP_COUNT
        assert "GB recap" not in names  # tenant scoping preserved

    @pytest.mark.asyncio
    async def test_custom_recaps_all_reachable_in_one_page(self):
        result = await self._execute_query_authenticated(
            CUSTOM_RECAPS_QUERY,
            {"tenantId": str(self.tenant.id), "first": 1000},
            self.spark_admin,
            self.endpoint_path,
        )
        assert result.errors is None, f"errored: {result.errors}"
        conn = result.data["customRecaps"]
        assert conn["totalCount"] == RECAP_COUNT, conn["totalCount"]
        assert len(conn["edges"]) == RECAP_COUNT, len(conn["edges"])
        names = {e["node"]["name"] for e in conn["edges"]}
        assert "GB custom recap" not in names  # tenant scoping preserved

    @pytest.mark.asyncio
    async def test_date_window_surfaces_old_recaps(self):
        # Window the OLDEST 10 days — these rows sit far beyond the newest
        # 50, so the pre-fix cap could never have returned them. With the
        # cap lifted the server-side date filter finds exactly them.
        start = self.legacy_dates[0].date().isoformat()
        end = self.legacy_dates[9].date().isoformat()
        result = await self._execute_query_authenticated(
            RECAPS_QUERY,
            {
                "tenantId": str(self.tenant.id),
                "first": 1000,
                "startDate": start,
                "endDate": end,
            },
            self.spark_admin,
            self.endpoint_path,
        )
        assert result.errors is None, f"errored: {result.errors}"
        conn = result.data["recaps"]
        assert conn["totalCount"] == 10, conn["totalCount"]
        names = {e["node"]["name"] for e in conn["edges"]}
        expected = {f"LD recap {i:03d}" for i in range(10)}
        assert names == expected, names

    @pytest.mark.asyncio
    async def test_custom_date_window_surfaces_old_recaps(self):
        start = self.legacy_dates[0].date().isoformat()
        end = self.legacy_dates[9].date().isoformat()
        result = await self._execute_query_authenticated(
            CUSTOM_RECAPS_QUERY,
            {
                "tenantId": str(self.tenant.id),
                "first": 1000,
                "startDate": start,
                "endDate": end,
            },
            self.spark_admin,
            self.endpoint_path,
        )
        assert result.errors is None, f"errored: {result.errors}"
        conn = result.data["customRecaps"]
        assert conn["totalCount"] == 10, conn["totalCount"]
        names = {e["node"]["name"] for e in conn["edges"]}
        expected = {f"LD custom recap {i:03d}" for i in range(10)}
        assert names == expected, names
