"""Coverage for Insights program-health funnel + date-windowed geo/BA."""

from datetime import timedelta

import pytest
from django.utils import timezone

from ambassadors.tests.base import AmbassadorsGraphQLTestCase
from events import models as event_models
from recaps import models as recap_models
from recaps.tenant_ba_leaderboard import tenant_ba_leaderboard
from recaps.tenant_overview import tenant_market_performance, tenant_program_health


PROGRAM_HEALTH_QUERY = """
query($tenantId: ID!, $start: String, $end: String) {
  tenantProgramHealth(
    tenantId: $tenantId
    startDate: $start
    endDate: $end
  ) {
    scheduled
    filed
    approved
    missing
    startDate
    endDate
  }
}
"""

MARKET_WINDOW_QUERY = """
query($tenantId: ID!, $start: String, $end: String) {
  tenantMarketPerformance(
    tenantId: $tenantId
    startDate: $start
    endDate: $end
  ) {
    state
    eventCount
    consumersReached
  }
}
"""

BA_WINDOW_QUERY = """
query($tenantId: ID!, $start: String, $end: String) {
  tenantBaLeaderboard(
    tenantId: $tenantId
    startDate: $start
    endDate: $end
  ) {
    baId
    name
    shiftsWorked
    recapsFiled
    consumersReached
    samplesDistributed
    reliabilityPct
  }
}
"""


@pytest.mark.django_db(transaction=True)
class TestTenantProgramHealth(AmbassadorsGraphQLTestCase):
    @pytest.fixture(autouse=True)
    def setup(self, db):
        from config.schema_client import schema_clients

        self.roles = self.setup_default_roles()
        self.schema = schema_clients
        self.endpoint_path = "/api/v1/graphql/clients"
        self.sys = self.get_system_user()
        self.tenant = self.create_tenant(name="Health Co")
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
        self.status = self.create_request_status(
            name="Scheduled", tenant=self.tenant, create_event=True
        )
        self.rtype = self.create_request_type("Retail Sampling", self.tenant)
        self.today = timezone.now().date()
        self.start = self.today - timedelta(days=7)
        self.end = self.today
        self.spark_admin = self.create_user(
            username="admin-health",
            email="admin-health@igniteproductions.co",
            role=self.roles["spark_admin"],
        )
        ba_user = self.create_user(
            username="health-ba",
            email="health-ba@test.com",
            role=self.roles["ambassador"],
            first_name="Ada",
            last_name="Field",
        )
        self.ambassador = self.create_ambassador(ba_user)

        # Filed + approved
        req = self.create_request(
            name="Filed stop",
            date=self.start,
            address="1 Main",
            client=self.client_row,
            distributor=self.distributor,
            retailer=self.retailer,
            request_type=self.rtype,
            tenant=self.tenant,
            status=self.status,
        )
        ev = req.event_set.first() or self.create_event(
            name="Filed event", tenant=self.tenant
        )
        if ev.request_id != req.id:
            ev.request = req
            ev.save(update_fields=["request"])
        recap_models.Recap.objects.create(
            name="filed",
            event=ev,
            ambassador=self.ambassador,
            total_engagements=12,
            approved=True,
            created_by=self.sys,
            updated_by=self.sys,
        )

        # Empty stub only
        req2 = self.create_request(
            name="Stub stop",
            date=self.end,
            address="1 Main",
            client=self.client_row,
            distributor=self.distributor,
            retailer=self.retailer,
            request_type=self.rtype,
            tenant=self.tenant,
            status=self.status,
        )
        ev2 = req2.event_set.first() or self.create_event(
            name="Stub event", tenant=self.tenant
        )
        if ev2.request_id != req2.id:
            ev2.request = req2
            ev2.save(update_fields=["request"])
        recap_models.Recap.objects.create(
            name="stub",
            event=ev2,
            ambassador=self.ambassador,
            created_by=self.sys,
            updated_by=self.sys,
        )

    def test_helper_funnel_excludes_empty_shell(self):
        data = tenant_program_health(self.tenant.id, start=self.start, end=self.end)
        assert data["scheduled"] == 2
        assert data["filed"] == 1
        assert data["approved"] == 1
        assert data["missing"] == 1

    @pytest.mark.asyncio
    async def test_graphql_program_health(self):
        result = await self._execute_query_authenticated(
            PROGRAM_HEALTH_QUERY,
            {
                "tenantId": str(self.tenant.id),
                "start": self.start.isoformat(),
                "end": self.end.isoformat(),
            },
            self.spark_admin,
        )
        assert result.errors is None
        row = result.data["tenantProgramHealth"]
        assert row["scheduled"] == 2
        assert row["filed"] == 1
        assert row["approved"] == 1
        assert row["missing"] == 1


@pytest.mark.django_db(transaction=True)
class TestMarketAndBaDateWindow(AmbassadorsGraphQLTestCase):
    @pytest.fixture(autouse=True)
    def setup(self, db):
        from config.schema_client import schema_clients

        self.roles = self.setup_default_roles()
        self.schema = schema_clients
        self.endpoint_path = "/api/v1/graphql/clients"
        self.sys = self.get_system_user()
        self.tenant = self.create_tenant(name="Window Co")
        self.spark_admin = self.create_user(
            username="admin-window",
            email="admin-window@igniteproductions.co",
            role=self.roles["spark_admin"],
        )
        self.ca = event_models.State.objects.create(
            name="California", code="CA", created_by=self.sys
        )
        today = timezone.now().date()
        self.in_start = today - timedelta(days=3)
        self.in_end = today
        old = today - timedelta(days=40)

        ba_user = self.create_user(
            username="window-ba",
            email="window-ba@test.com",
            role=self.roles["ambassador"],
            first_name="Bo",
            last_name="Window",
        )
        self.ambassador = self.create_ambassador(ba_user)

        ev_in = self.create_event(
            name="In window", tenant=self.tenant, state=self.ca
        )
        ev_in.date = timezone.now().replace(
            year=self.in_start.year,
            month=self.in_start.month,
            day=self.in_start.day,
            hour=12,
            minute=0,
            second=0,
            microsecond=0,
        )
        ev_in.save(update_fields=["date"])
        recap = recap_models.Recap.objects.create(
            name="in",
            event=ev_in,
            ambassador=self.ambassador,
            total_engagements=5,
            created_by=self.sys,
            updated_by=self.sys,
        )
        recap_models.ConsumerEngagements.objects.create(
            recap=recap,
            total_consumer=20,
            created_by=self.sys,
            updated_by=self.sys,
        )

        ev_old = self.create_event(
            name="Old", tenant=self.tenant, state=self.ca
        )
        ev_old.date = timezone.now().replace(
            year=old.year,
            month=old.month,
            day=old.day,
            hour=12,
            minute=0,
            second=0,
            microsecond=0,
        )
        ev_old.save(update_fields=["date"])
        recap_models.Recap.objects.create(
            name="old",
            event=ev_old,
            ambassador=self.ambassador,
            total_engagements=99,
            created_by=self.sys,
            updated_by=self.sys,
        )

    def test_market_window_excludes_old(self):
        rows = tenant_market_performance(
            self.tenant.id, start=self.in_start, end=self.in_end
        )
        assert len(rows) == 1
        assert rows[0]["state"] == "CA"
        assert rows[0]["event_count"] == 1
        assert rows[0]["consumers_reached"] == 20

    def test_ba_window_includes_consumers(self):
        rows = tenant_ba_leaderboard(
            self.tenant.id, start=self.in_start, end=self.in_end
        )
        assert len(rows) == 1
        assert rows[0]["consumers_reached"] == 20
        assert rows[0]["recaps_filed"] == 1
        assert rows[0]["reliability_pct"] is None

    @pytest.mark.asyncio
    async def test_graphql_market_and_ba_window(self):
        m = await self._execute_query_authenticated(
            MARKET_WINDOW_QUERY,
            {
                "tenantId": str(self.tenant.id),
                "start": self.in_start.isoformat(),
                "end": self.in_end.isoformat(),
            },
            self.spark_admin,
        )
        assert m.errors is None
        assert m.data["tenantMarketPerformance"][0]["consumersReached"] == 20

        b = await self._execute_query_authenticated(
            BA_WINDOW_QUERY,
            {
                "tenantId": str(self.tenant.id),
                "start": self.in_start.isoformat(),
                "end": self.in_end.isoformat(),
            },
            self.spark_admin,
        )
        assert b.errors is None
        row = b.data["tenantBaLeaderboard"][0]
        assert row["consumersReached"] == 20
        assert row["reliabilityPct"] is None
