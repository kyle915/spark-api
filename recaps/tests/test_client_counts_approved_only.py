"""Client-facing counts use approved recaps on live requests.

The client campaign report, its share link and PDF, and the client Home
headline tiles (eventDashboard) counted recaps still awaiting review, while
the Program-KPIs sections under the tiles did not: one page disagreed with
itself. The campaign report also leaves archived recaps out (its existing
client bar); the Home tiles match the Program-KPIs sections, which count an
approved recap that was later archived. Runs through the real ``schema_clients`` surface with the full
field set the web app selects (outage lesson), plus the public views.

Ignite staff keep every recap, as before.
"""

from datetime import timedelta
from unittest.mock import patch

import pytest
from django.core.cache import cache
from django.test import Client
from django.utils import timezone

from ambassadors.tests.base import AmbassadorsGraphQLTestCase
from events import models as event_models
from recaps import models as recap_models
from recaps.report_tokens import make_report_token

# The web app's CampaignReportQuery, field for field.
REPORT = """
query CampaignReportQuery($requestId: ID!) {
  campaignReport(requestId: $requestId) {
    requestId brandName title dateRange generatedAt shareToken
    kpis {
      events recaps consumersReached samplesDistributed productsSold
      cansSold packsSold totalEngagements firstTimeConsumers
      brandAwareConsumers willingToPurchase
    }
    events { id name date locationName city state coordinates recapCount }
    photos { url caption }
    ambassadors { name isExternal eventCount }
    highlights { text source }
  }
}
"""

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


@pytest.mark.django_db(transaction=True)
class TestClientCountsApprovedOnly(AmbassadorsGraphQLTestCase):
    @pytest.fixture(autouse=True)
    def setup(self, db, settings):
        from config.schema_client import schema_clients

        settings.GS_BUCKET_NAME = "spark-test-bucket"
        cache.clear()
        self.roles = self.setup_default_roles()
        self.schema = schema_clients
        self.endpoint_path = "/api/v1/graphql/clients"
        self.system_user = self.get_system_user()
        self.tenant = self.create_tenant(name="Alpha Brand")

        self.admin = self.create_user(
            username="counts-admin", email="counts-admin@test.com",
            role=self.roles["spark_admin"],
        )
        self.client_user = self.create_user(
            username="counts-client", email="counts-client@test.com",
            role=self.roles["client"],
        )
        self.create_tenanted_user(self.client_user, self.tenant)

        self.request = event_models.Request.objects.create(
            name="Spring Tour",
            address="1 Main St",
            tenant=self.tenant,
            status=self.create_request_status(name="Approved", tenant=self.tenant, slug="approved"),
            request_type=self.create_request_type(name="Retail", tenant=self.tenant),
            created_by=self.system_user,
        )
        now = timezone.now() - timedelta(days=2)
        self.event = self.create_event(
            name="Store One", tenant=self.tenant, date=now, start_time=now,
            end_time=now + timedelta(hours=4),
        )
        event_models.Event.objects.filter(id=self.event.id).update(request=self.request)

        # Legacy: approved 100 consumers, needs-review 40, approved-then-archived 7.
        # The campaign report leaves the archived one out; the Home tiles
        # count it, like the Program-KPIs sections under them.
        self._legacy(consumers=100, approved=True)
        self._legacy(consumers=40, approved=False)
        archived = self._legacy(consumers=7, approved=True)
        recap_models.Recap.objects.filter(id=archived.id).update(archived_at=timezone.now())

        # Custom: approved 30 consumers, needs-review 500.
        event_type = self.create_event_type(name="Sampling", tenant=self.tenant)
        self.template = recap_models.CustomRecapTemplate.objects.create(
            name="Sampling", event_type=event_type, tenant=self.tenant,
            created_by=self.system_user,
        )
        self.field_type = recap_models.CustomRecapFieldType.objects.create(
            name="Number", created_by=self.system_user
        )
        self.section = recap_models.RecapSection.objects.create(
            name="Numbers", tenant=self.tenant, created_by=self.system_user
        )
        self.sampled_field = self._field("Total number of consumers sampled")
        self._custom(consumers="30", approved=True)
        self._custom(consumers="500", approved=False)
        cache.clear()

    def _legacy(self, *, consumers, approved):
        recap = recap_models.Recap.objects.create(
            name=f"legacy {consumers}", event=self.event, total_engagements=consumers,
            approved=approved, submited_at=timezone.now(),
            created_by=self.system_user, updated_by=self.system_user,
        )
        recap_models.ConsumerEngagements.objects.create(
            recap=recap, total_consumer=consumers, first_time_consumers=0,
            brand_aware_consumers=0, willing_to_purchase_consumers=0,
            not_willing_consumers=0, created_by=self.system_user,
        )
        return recap

    def _field(self, name):
        return recap_models.CustomField.objects.create(
            name=name, custom_recap_template=self.template,
            custom_field_type=self.field_type, recap_section=self.section,
            created_by=self.system_user,
        )

    def _custom(self, *, consumers, approved):
        recap = recap_models.CustomRecap.objects.create(
            name=f"custom {consumers}", approved=approved, event=self.event,
            tenant=self.tenant, custom_recap_template=self.template,
            created_by=self.system_user,
        )
        recap_models.CustomFieldValue.objects.create(
            custom_recap=recap, custom_field=self.sampled_field, value=consumers,
            created_by=self.system_user,
        )
        return recap

    async def _query(self, document, variables, user):
        result = await self._execute_query_authenticated(
            document, variables, user, self.endpoint_path
        )
        assert result.errors is None, result.errors
        return result.data

    # ── campaign report ───────────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_client_campaign_report_counts_approved_only(self):
        data = await self._query(REPORT, {"requestId": str(self.request.uuid)}, self.client_user)
        kpis = data["campaignReport"]["kpis"]
        assert kpis["recaps"] == 2  # approved legacy + approved custom
        assert kpis["consumersReached"] == 130
        assert data["campaignReport"]["events"][0]["recapCount"] == 2

    @pytest.mark.asyncio
    async def test_staff_campaign_report_is_unchanged(self):
        data = await self._query(REPORT, {"requestId": str(self.request.uuid)}, self.admin)
        kpis = data["campaignReport"]["kpis"]
        assert kpis["recaps"] == 5
        assert kpis["consumersReached"] == 100 + 40 + 7 + 30 + 500

    def test_share_link_counts_approved_only(self):
        token = make_report_token(self.request.id)
        resp = Client().get(f"/api/public/report/{token}")
        assert resp.status_code == 200
        kpis = resp.json()["report"]["kpis"]
        assert kpis["recaps"] == 2
        assert kpis["consumersReached"] == 130

    def test_share_link_pdf_counts_approved_only(self):
        from recaps import report_pdf

        seen = {}

        def _capture(data):
            seen["kpis"] = data.kpis
            return "<html></html>"

        with patch.object(report_pdf, "_build_report_html", side_effect=_capture):
            try:
                report_pdf.generate_campaign_report_pdf(self.request.id)
            except report_pdf.CampaignReportPdfError:
                pass  # no WeasyPrint here; the numbers were already built
        assert seen["kpis"].recaps == 2
        assert seen["kpis"].consumers_reached == 130

    # ── client Home tiles (eventDashboard) ────────────────────────────

    def _window(self):
        today = timezone.now().date()
        return {"filters": {
            "tenantId": str(self.tenant.id),
            "startDate": (today - timedelta(days=30)).isoformat(),
            "endDate": today.isoformat(),
        }}

    @pytest.mark.asyncio
    async def test_client_home_tiles_count_approved_only(self):
        data = await self._query(EVENT_DASHBOARD, self._window(), self.client_user)
        metrics = data["eventDashboard"]["metrics"]
        assert metrics["consumersSampled"] == 100 + 7 + 30
        assert metrics["totalEvents"] == 1

    @pytest.mark.asyncio
    async def test_staff_dashboard_still_counts_every_recap(self):
        data = await self._query(EVENT_DASHBOARD, self._window(), self.admin)
        assert data["eventDashboard"]["metrics"]["consumersSampled"] == 677

    @pytest.mark.asyncio
    async def test_needs_review_only_event_is_not_an_event_run_for_clients(self):
        now = timezone.now() - timedelta(days=1)
        lone = await self._make_event_with_pending_recap(now)
        assert lone
        data = await self._query(EVENT_DASHBOARD, self._window(), self.client_user)
        assert data["eventDashboard"]["metrics"]["totalEvents"] == 1
        cache.clear()
        data = await self._query(EVENT_DASHBOARD, self._window(), self.admin)
        assert data["eventDashboard"]["metrics"]["totalEvents"] == 2

    async def _make_event_with_pending_recap(self, when):
        from asgiref.sync import sync_to_async

        def _make():
            event = self.create_event(
                name="Store Two", tenant=self.tenant, date=when, start_time=when,
                end_time=when + timedelta(hours=4),
            )
            recap_models.Recap.objects.create(
                name="pending only", event=event, total_engagements=9,
                approved=False, created_by=self.system_user,
                updated_by=self.system_user,
            )
            cache.clear()
            return event.id

        return await sync_to_async(_make)()



@pytest.mark.django_db
def test_home_tiles_use_the_same_bar_as_program_kpis():
    """The hero and the sections under it filter recaps the same way."""
    from recaps.aggregates import client_counted_q
    from recaps.tenant_overview import _approved_only

    qs = recap_models.Recap.objects.all()
    assert str(_approved_only(qs, "").query) == str(qs.filter(client_counted_q("")).query)
