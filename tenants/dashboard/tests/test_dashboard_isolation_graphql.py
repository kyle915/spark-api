"""Cross-tenant isolation tests for the clients-schema dashboard reads.

Round-2 of the clients-schema tenant-isolation sweep. Covers:

  * goalsList(tenantId)      — PII (users' goals + names) must not leak across
    tenants (a client passing another tenant's id gets ``[]``).
  * latestInsights(tenantId) — a client passing another tenant's id gets
    ``null``.
  * eventDashboard / recapDashboard / eventDashboardFilterOptions /
    recapDashboardFilterOptions — these aggregate across all tenants and only
    narrow on an OPTIONAL tenantId. A client/non-admin is now constrained to
    their own tenant(s); an admin keeps the global cross-tenant view.

Builds a SECOND tenant with its own distributor / RMM / event / recap / goal /
insights, then asserts a client bound to ``self.tenant`` can't see the second
tenant's data while a spark-admin can.

Mirrors ``academy/tests/test_academy_isolation_graphql.py``.
"""

import pytest
from asgiref.sync import sync_to_async
from datetime import date, time, datetime, timedelta
from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.utils import timezone

from tenants.dashboard.tests.base import DashboardGraphQLTestCase

User = get_user_model()


GOALS_LIST_QUERY = """
query GoalsList($tenantId: ID!, $year: Int) {
  goalsList(tenantId: $tenantId, year: $year) {
    user { id email }
    year
    eventTargetGoal
  }
}
"""

LATEST_INSIGHTS_QUERY = """
query LatestInsights($tenantId: ID) {
  latestInsights(tenantId: $tenantId) { id tenantId }
}
"""

EVENT_DASHBOARD_QUERY = """
query EventDashboard {
  eventDashboard { metrics { totalEvents } }
}
"""

RECAP_DASHBOARD_QUERY = """
query RecapDashboard {
  recapDashboard { metrics { totalConsumersSampled } }
}
"""

EVENT_FILTER_OPTIONS_QUERY = """
query EventFilterOptions {
  eventDashboardFilterOptions {
    distributors { id name }
    rmms { id name }
  }
}
"""

RECAP_FILTER_OPTIONS_QUERY = """
query RecapFilterOptions {
  recapDashboardFilterOptions {
    distributors { id name }
    rmms { id name }
  }
}
"""


@pytest.mark.django_db(transaction=True)
class TestDashboardTenantIsolationGraphQL(DashboardGraphQLTestCase):
    """Cross-tenant isolation for the dashboard PII + aggregation reads."""

    @pytest.fixture(autouse=True)
    def setup_isolation_data(self, setup_dashboard_data):
        """Add a second tenant (with its own data) + an admin user on top of
        the single-tenant fixtures from DashboardGraphQLTestCase."""
        cache.clear()

        # Admin (spark-admin) — sees everything.
        self.admin_user = self.create_user(
            username="dash-admin@test.com",
            email="dash-admin@test.com",
            role=self.roles["spark_admin"],
            password="testpass123",
        )

        # A SECOND tenant with its own graph + a client bound only to it.
        self.other_tenant = self.create_tenant(name="Other Brand")
        self.other_client = self.create_user(
            username="other-client@test.com",
            email="other-client@test.com",
            role=self.roles["client"],
            password="testpass123",
        )
        self.create_tenanted_user(
            user=self.other_client, tenant=self.other_tenant
        )
        self.other_rmm = self.create_user(
            username="other-rmm@test.com",
            email="other-rmm@test.com",
            role=self.roles["client"],
            password="testpass123",
        )
        self.create_tenanted_user(user=self.other_rmm, tenant=self.other_tenant)

        other_location = self.create_location(
            name="Other Loc", code="OTH", zip_code="99999",
            tenant=self.other_tenant,
        )
        other_client_obj = self.create_client(
            name="Other Co", email="oc@example.com", tenant=self.other_tenant,
        )
        self.other_distributor = self.create_distributor(
            name="OtherDistributorUnique", email="od@example.com",
            location=other_location, tenant=self.other_tenant,
        )
        other_retailer = self.create_retailer(
            name="Other Retailer", address="Addr", store_contact="C",
            location=other_location, tenant=self.other_tenant,
        )
        other_req_type = self.create_request_type(
            name="Other RT", tenant=self.other_tenant,
        )
        other_req_status = self.create_request_status(
            name="Other Approved", tenant=self.other_tenant, create_event=True,
        )
        other_event_status = self.create_event_status(
            name="Other Active", tenant=self.other_tenant,
        )
        other_event_type = self.create_event_type(
            name="Other Promo", tenant=self.other_tenant,
        )

        now = timezone.now()
        other_request = self.create_request(
            name="Other Request", date=now, address="Addr",
            client=other_client_obj, distributor=self.other_distributor,
            retailer=other_retailer, request_type=other_req_type,
            tenant=self.other_tenant, start_time=time(9, 0),
            end_time=time(17, 0), status=other_req_status,
        )
        self.other_event = self.create_event(
            name="Other Event", tenant=self.other_tenant, address="Addr",
            request=other_request, event_type=other_event_type,
            status=other_event_status, rmm_asigned=self.other_rmm,
            date=now, start_time=now,
        )

        from recaps import models as recap_models
        self.other_recap = recap_models.Recap.objects.create(
            name="Other Recap", event=self.other_event,
            total_engagements=500, products_sold=200, approved=True,
            created_by=self.get_system_user(),
        )
        recap_models.ConsumerEngagements.objects.create(
            recap=self.other_recap, total_consumer=500,
            first_time_consumers=100, brand_aware_consumers=200,
            willing_to_purchase_consumers=300, not_willing_consumers=200,
            created_by=self.get_system_user(),
        )

        # Goals + insights for the OTHER tenant (the PII that must not leak).
        from tenants import models as tenant_models
        tenant_models.Goal.objects.create(
            tenant=self.other_tenant, user=self.other_client,
            year=2026, event_target_goal=99,
        )
        self.other_insights = tenant_models.Insights.objects.create(
            tenant=self.other_tenant,
            from_date=date(2026, 1, 1), to_date=date(2026, 12, 31),
            total_feedback_count=7, created_by=self.get_system_user(),
        )

        # A goal + insights for the caller's OWN tenant too.
        tenant_models.Goal.objects.create(
            tenant=self.tenant, user=self.client_user,
            year=2026, event_target_goal=10,
        )
        tenant_models.Insights.objects.create(
            tenant=self.tenant,
            from_date=date(2026, 1, 1), to_date=date(2026, 12, 31),
            total_feedback_count=3, created_by=self.get_system_user(),
        )
        cache.clear()

    # == goalsList (PII) =====================================================

    @pytest.mark.asyncio
    async def test_client_cannot_list_other_tenant_goals(self):
        """A client passing another tenant's id gets [] (no users' goals/names)."""
        result = await self._execute_query_authenticated(
            GOALS_LIST_QUERY,
            {"tenantId": str(self.other_tenant.id), "year": 2026},
            self.client_user,
        )
        assert result.errors is None
        assert result.data["goalsList"] == []

    @pytest.mark.asyncio
    async def test_client_can_list_own_tenant_goals(self):
        result = await self._execute_query_authenticated(
            GOALS_LIST_QUERY,
            {"tenantId": str(self.tenant.id), "year": 2026},
            self.client_user,
        )
        assert result.errors is None
        rows = result.data["goalsList"]
        assert len(rows) == 1
        assert rows[0]["user"]["id"] == str(self.client_user.id)

    @pytest.mark.asyncio
    async def test_admin_can_list_any_tenant_goals(self):
        result = await self._execute_query_authenticated(
            GOALS_LIST_QUERY,
            {"tenantId": str(self.other_tenant.id), "year": 2026},
            self.admin_user,
        )
        assert result.errors is None
        rows = result.data["goalsList"]
        assert len(rows) == 1
        assert rows[0]["eventTargetGoal"] == 99

    # == latestInsights ======================================================

    @pytest.mark.asyncio
    async def test_client_cannot_read_other_tenant_insights(self):
        result = await self._execute_query_authenticated(
            LATEST_INSIGHTS_QUERY,
            {"tenantId": str(self.other_tenant.id)},
            self.client_user,
        )
        assert result.errors is None
        assert result.data["latestInsights"] is None

    @pytest.mark.asyncio
    async def test_client_can_read_own_tenant_insights(self):
        result = await self._execute_query_authenticated(
            LATEST_INSIGHTS_QUERY,
            {"tenantId": str(self.tenant.id)},
            self.client_user,
        )
        assert result.errors is None
        assert result.data["latestInsights"] is not None
        assert result.data["latestInsights"]["tenantId"] == str(self.tenant.id)

    @pytest.mark.asyncio
    async def test_admin_can_read_any_tenant_insights(self):
        result = await self._execute_query_authenticated(
            LATEST_INSIGHTS_QUERY,
            {"tenantId": str(self.other_tenant.id)},
            self.admin_user,
        )
        assert result.errors is None
        assert result.data["latestInsights"] is not None
        assert result.data["latestInsights"]["tenantId"] == str(
            self.other_tenant.id
        )

    # == eventDashboard (aggregate) =========================================

    @pytest.mark.asyncio
    async def test_client_event_dashboard_excludes_other_tenant(self):
        """A client's totalEvents counts only their own tenant's events."""
        cache.clear()
        client_res = await self._execute_query_authenticated(
            EVENT_DASHBOARD_QUERY, {}, self.client_user
        )
        assert client_res.errors is None
        client_total = client_res.data["eventDashboard"]["metrics"]["totalEvents"]

        admin_res = await self._execute_query_authenticated(
            EVENT_DASHBOARD_QUERY, {}, self.admin_user
        )
        assert admin_res.errors is None
        admin_total = admin_res.data["eventDashboard"]["metrics"]["totalEvents"]

        # The admin (global) sees strictly more events than the client (own
        # tenant only), because the other tenant has its own in-window event.
        assert admin_total > client_total
        assert client_total >= 1

    # == recapDashboard (aggregate) =========================================

    @pytest.mark.asyncio
    async def test_client_recap_dashboard_excludes_other_tenant(self):
        cache.clear()
        client_res = await self._execute_query_authenticated(
            RECAP_DASHBOARD_QUERY, {}, self.client_user
        )
        assert client_res.errors is None
        client_total = (
            client_res.data["recapDashboard"]["metrics"]["totalConsumersSampled"]
        )

        admin_res = await self._execute_query_authenticated(
            RECAP_DASHBOARD_QUERY, {}, self.admin_user
        )
        assert admin_res.errors is None
        admin_total = (
            admin_res.data["recapDashboard"]["metrics"]["totalConsumersSampled"]
        )

        # Other tenant contributed 500 consumers; admin must see them, client
        # must not.
        assert admin_total >= client_total + 500

    # == filter options (aggregate) =========================================

    @pytest.mark.asyncio
    async def test_client_event_filter_options_exclude_other_tenant(self):
        cache.clear()
        result = await self._execute_query_authenticated(
            EVENT_FILTER_OPTIONS_QUERY, {}, self.client_user
        )
        assert result.errors is None
        data = result.data["eventDashboardFilterOptions"]
        names = {d["name"] for d in (data["distributors"] or [])}
        assert "OtherDistributorUnique" not in names

    @pytest.mark.asyncio
    async def test_admin_event_filter_options_include_all_tenants(self):
        cache.clear()
        result = await self._execute_query_authenticated(
            EVENT_FILTER_OPTIONS_QUERY, {}, self.admin_user
        )
        assert result.errors is None
        data = result.data["eventDashboardFilterOptions"]
        names = {d["name"] for d in (data["distributors"] or [])}
        assert "OtherDistributorUnique" in names

    @pytest.mark.asyncio
    async def test_client_recap_filter_options_exclude_other_tenant(self):
        cache.clear()
        result = await self._execute_query_authenticated(
            RECAP_FILTER_OPTIONS_QUERY, {}, self.client_user
        )
        assert result.errors is None
        data = result.data["recapDashboardFilterOptions"]
        names = {d["name"] for d in (data["distributors"] or [])}
        assert "OtherDistributorUnique" not in names

    @pytest.mark.asyncio
    async def test_admin_recap_filter_options_include_all_tenants(self):
        cache.clear()
        result = await self._execute_query_authenticated(
            RECAP_FILTER_OPTIONS_QUERY, {}, self.admin_user
        )
        assert result.errors is None
        data = result.data["recapDashboardFilterOptions"]
        names = {d["name"] for d in (data["distributors"] or [])}
        assert "OtherDistributorUnique" in names
