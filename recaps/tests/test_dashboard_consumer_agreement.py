"""A brand's Home headline and the weekly chart under it print one number.

Liquid Death's Client view read 10,765 consumers sampled in the "last 30
days" headline (eventDashboard) over 10,641 in the chart for the same days
(tenantConsumerBreakdown). Two causes, both covered here through the real
client schema with the web app's full EventDashboardQuery field set:

* staff are counted every recap (by design) and the Client view preview is
  staff — 124 consumers on two recaps still awaiting review. ``clientView``
  now counts the way the brand is counted;
* the headline's own sum windowed on any of date / start_time / request date
  and kept events flagged off the dashboard. A single-brand headline now
  reads the chart's rows (``consumers_sampled_total``).
"""

from datetime import timedelta

import pytest
from django.core.cache import cache
from django.utils import timezone

from ambassadors.tests.base import AmbassadorsGraphQLTestCase
from events import models as event_models
from recaps import models as recap_models

# The web app's EventDashboardQuery, field for field.
EVENT_DASHBOARD = """
query EventDashboardQuery($filters: EventDashboardFiltersInput) {
  eventDashboard(filters: $filters) {
    globalKpis {
      singleCansSold multiPacksSold packCansEquivalent productsSold
      byRmm { rmmId rmmName singleCansSold multiPacksSold }
    }
    metrics {
      totalEvents consumersSampled brandAwareness purchaseIntent
      brandAwarenessBase purchaseIntentBase comparisonPeriod
      comparisonValues { totalEvents consumersSampled brandAwareness purchaseIntent }
    }
    monthlyTrends { dataPoints { month consumersSampled willingToPurchase conversionRate eventsCount } }
    performanceInsights {
      knewAboutBrand knewAboutBrandPercentage willingToPurchase
      willingToPurchasePercentage bestMonth { month eventsCount consumersCount } growthRate
    }
    goalsProgress { name target current percentageComplete }
    recentEvents { id name date location consumers intentRate status }
  }
  eventDashboardFilterOptions {
    distributors { id name }
    rmms { id name }
    quarters { value label }
    tenants { id name }
  }
}
"""

BREAKDOWN = """
query ConsumerBreakdownQuery($tenantId: ID!, $startDate: String, $endDate: String) {
  tenantConsumerBreakdown(tenantId: $tenantId, startDate: $startDate, endDate: $endDate) {
    total recaps
  }
}
"""


@pytest.mark.django_db(transaction=True)
class TestDashboardConsumerAgreement(AmbassadorsGraphQLTestCase):
    @pytest.fixture(autouse=True)
    def setup(self, db, settings):
        from config.schema_client import schema_clients

        settings.GS_BUCKET_NAME = "spark-test-bucket"
        cache.clear()
        self.roles = self.setup_default_roles()
        self.schema = schema_clients
        self.endpoint_path = "/api/v1/graphql/clients"
        self.sys = self.get_system_user()
        self.tenant = self.create_tenant(name="Agree Brand")
        self.admin = self.create_user(
            username="agree-admin", email="agree-admin@test.com",
            role=self.roles["spark_admin"],
        )
        self.client_user = self.create_user(
            username="agree-client", email="agree-client@test.com",
            role=self.roles["client"],
        )
        self.create_tenanted_user(self.client_user, self.tenant)
        et = self.create_event_type(name="Sampling", tenant=self.tenant)
        self.template = recap_models.CustomRecapTemplate.objects.create(
            name="Sampling", event_type=et, tenant=self.tenant, created_by=self.sys,
        )
        ft = recap_models.CustomRecapFieldType.objects.create(name="Number", created_by=self.sys)
        section = recap_models.RecapSection.objects.create(
            name="Numbers", tenant=self.tenant, created_by=self.sys
        )
        self.field = recap_models.CustomField.objects.create(
            name="Total number of consumers sampled", custom_recap_template=self.template,
            custom_field_type=ft, recap_section=section, created_by=self.sys,
        )

        live = self._event("Store A", 3)
        self._recap(live, "500", approved=True)
        self._recap(live, "124", approved=False)  # awaiting review
        hidden = self._event("Off-dashboard pop-up", 4)
        event_models.Event.objects.filter(id=hidden.id).update(exclude_from_dashboard=True)
        self._recap(hidden, "77", approved=True)
        cache.clear()

    def _event(self, name, days_ago):
        when = timezone.now() - timedelta(days=days_ago)
        return self.create_event(
            name=name, tenant=self.tenant, date=when, start_time=when,
            end_time=when + timedelta(hours=3),
        )

    def _recap(self, event, consumers, *, approved):
        recap = recap_models.CustomRecap.objects.create(
            name=f"r{consumers}", approved=approved, event=event, tenant=self.tenant,
            custom_recap_template=self.template, created_by=self.sys,
        )
        recap_models.CustomFieldValue.objects.create(
            custom_recap=recap, custom_field=self.field, value=consumers,
            created_by=self.sys,
        )

    def _window(self):
        today = timezone.now().date()
        return (today - timedelta(days=30)).isoformat(), today.isoformat()

    async def _both(self, user, *, client_view=None):
        start, end = self._window()
        filters = {"tenantId": str(self.tenant.id), "startDate": start, "endDate": end,
                   "distributorIds": [], "rmmAsignedId": None, "quarter": None}
        if client_view is not None:
            filters["clientView"] = client_view
        dash = await self._execute_query_authenticated(
            EVENT_DASHBOARD, {"filters": filters}, user, self.endpoint_path
        )
        assert dash.errors is None, dash.errors
        chart = await self._execute_query_authenticated(
            BREAKDOWN, {"tenantId": str(self.tenant.id), "startDate": start, "endDate": end},
            user, self.endpoint_path,
        )
        assert chart.errors is None, chart.errors
        return (
            dash.data["eventDashboard"]["metrics"]["consumersSampled"],
            chart.data["tenantConsumerBreakdown"]["total"],
        )

    @pytest.mark.asyncio
    async def test_brand_headline_equals_the_chart(self):
        headline, chart = await self._both(self.client_user)
        assert headline == chart == 500

    @pytest.mark.asyncio
    async def test_staff_client_view_equals_what_the_brand_sees(self):
        headline, chart = await self._both(self.admin, client_view=True)
        assert headline == chart == 500

    @pytest.mark.asyncio
    async def test_staff_view_still_counts_recaps_awaiting_review(self):
        headline, _ = await self._both(self.admin)
        assert headline == 500 + 124 + 77
