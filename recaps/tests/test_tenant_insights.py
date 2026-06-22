"""Coverage for the DETERMINISTIC proactive-insight buckets:

* :func:`recaps.tenant_insights.build_insight_buckets` — the five fixed,
  computed-live buckets (reach, sampling, sales, new audience, momentum) that
  replaced the old free-form OpenAI insights, and
* the ``tenantInsights(tenantId)`` GraphQL query on the clients schema — the
  tenant-scoped, never-raises shell that now computes those buckets live (no
  AI call, no snapshot read) so they stay in lockstep with ``tenantKpis``.

The headline regression these tests lock in is the **Momentum fix**: the old
panel compared the CURRENT/empty trailing month (a month that has not started)
against the prior real month and dramatized it as a "-100% — Operations
halted" card. The deterministic Momentum bucket only ever compares months that
actually have activity, so the empty current/future month can never produce a
negative delta or a "halted" card.

Fixtures follow the style of test_tenant_market_performance.py. Because
``Recap.created_at`` is ``auto_now_add``, recaps are placed into specific
trend months by updating ``created_at`` after creation (the same trick the
existing insights tests use). Target months are chosen RELATIVE to the live
trailing window (via :func:`recaps.tenant_overview.tenant_monthly_trend`) so
the tests are independent of the wall clock.
"""

import pytest
from django.utils import timezone

from ambassadors.tests.base import AmbassadorsGraphQLTestCase
from events import models as event_models
from recaps import models as recap_models
from recaps.tenant_insights import build_insight_buckets
from recaps.tenant_overview import tenant_kpi_totals, tenant_monthly_trend


INSIGHTS_QUERY = """
query Insights($tenantId: ID!) {
  tenantInsights(tenantId: $tenantId) {
    generatedAt
    items {
      key
      title
      detail
      sentiment
      metric
    }
  }
}
"""


def _month_first(year: int, month: int):
    """Midnight (tz-aware) on the first of ``year``-``month``."""
    return timezone.now().replace(
        year=year,
        month=month,
        day=10,  # mid-month: comfortably inside the TruncMonth bucket
        hour=12,
        minute=0,
        second=0,
        microsecond=0,
    )


@pytest.mark.django_db(transaction=True)
class TestTenantInsightBuckets(AmbassadorsGraphQLTestCase):
    """Deterministic insight buckets + the Momentum empty-month fix."""

    @pytest.fixture(autouse=True)
    def setup(self, db):
        from config.schema_client import schema_clients

        self.roles = self.setup_default_roles()
        self.schema = schema_clients
        self.endpoint_path = "/api/v1/graphql/clients"
        self.system_user = self.get_system_user()

        # Active tenant gets real activity; empty tenant gets nothing.
        self.tenant = self.create_tenant(name="Girl Beer")
        self.empty_tenant = self.create_tenant(name="No Activity Co")

        self.spark_admin = self.create_user(
            username="admin-insights",
            email="admin-insights@test.com",
            role=self.roles["spark_admin"],
        )
        self.client_user = self.create_user(
            username="client-insights",
            email="client-insights@test.com",
            role=self.roles["client"],
        )
        self.create_tenanted_user(self.client_user, self.tenant)

        # Pick three real trend months from the LIVE trailing window so the
        # test never depends on the exact "today": the newest month is the
        # (possibly empty) current/future bucket; we deliberately leave it
        # empty and put activity in two earlier, fully-past months.
        window = [m.month for m in tenant_monthly_trend(self.tenant.id)]
        # window is oldest -> newest, length 12. Use a comfortably-past pair.
        self.older_month = window[-4]  # e.g. three months before current
        self.newer_active_month = window[-3]  # the latest ACTIVE month
        self.current_empty_month = window[-1]  # current/future, kept EMPTY

    # -- fixture helpers ------------------------------------------------

    def _recap_in_month(
        self,
        month_key: str,
        *,
        engagements: int = 0,
        consumers: int = 0,
        samples: int = 0,
        products_sold: int = 0,
        cans: int = 0,
        packs: int = 0,
        first_time: int = 0,
        brand_aware: int = 0,
        willing: int = 0,
    ) -> recap_models.Recap:
        """Create a legacy recap (+ children) dated into ``month_key``.

        ``month_key`` is ``"YYYY-MM"``. The recap and its children are created
        normally then back-dated with ``.update(created_at=...)`` because the
        column is ``auto_now_add`` (mirrors the existing insights tests).
        """
        year, mm = (int(p) for p in month_key.split("-"))
        when = _month_first(year, mm)

        event = self.create_event(name=f"ev {month_key}", tenant=self.tenant)
        # Trend/KPI windows now scope by EVENT date (not created_at), so date
        # the event into the target month too.
        event_models.Event.objects.filter(id=event.id).update(date=when)
        recap = recap_models.Recap.objects.create(
            name=f"recap {month_key}",
            event=event,
            total_engagements=engagements,
            products_sold=products_sold,
            total_cans_sold=cans,
            total_packs_sold=packs,
            created_by=self.system_user,
            updated_by=self.system_user,
        )
        recap_models.Recap.objects.filter(id=recap.id).update(created_at=when)

        if any((consumers, first_time, brand_aware, willing)):
            eng = recap_models.ConsumerEngagements.objects.create(
                recap=recap,
                total_consumer=consumers,
                first_time_consumers=first_time,
                brand_aware_consumers=brand_aware,
                willing_to_purchase_consumers=willing,
                created_by=self.system_user,
                updated_by=self.system_user,
            )
            recap_models.ConsumerEngagements.objects.filter(id=eng.id).update(
                created_at=when
            )

        if samples:
            ps = recap_models.ProductSamples.objects.create(
                recap=recap,
                product=self._product(f"prod {month_key}"),
                quantity=samples,
                created_by=self.system_user,
                updated_by=self.system_user,
            )
            recap_models.ProductSamples.objects.filter(id=ps.id).update(
                created_at=when
            )
        return recap

    def _product(self, name: str):
        from events import models as event_models

        product_type = event_models.ProductType.objects.create(
            name=f"type {name}", tenant=self.tenant, created_by=self.system_user
        )
        return event_models.Product.objects.create(
            name=name,
            product_type=product_type,
            tenant=self.tenant,
            created_by=self.system_user,
        )

    def _seed_two_active_months_newest_empty(self):
        """Older active month + a STRONGER newer active month; newest empty.

        Engagements: older=100, newer=120 (a +20% rise). The newest trend
        bucket (current/future) is deliberately left with NO activity — the
        exact shape that used to yield the bogus "-100% halted" card.
        """
        self._recap_in_month(
            self.older_month,
            engagements=100,
            consumers=200,
            samples=50,
            products_sold=10,
            cans=8,
            packs=2,
            first_time=40,
            brand_aware=120,
            willing=60,
        )
        self._recap_in_month(
            self.newer_active_month,
            engagements=120,
            consumers=150,
            samples=30,
            products_sold=5,
            cans=4,
            packs=1,
            first_time=25,
            brand_aware=90,
            willing=45,
        )

    # -- empty-tenant path ----------------------------------------------

    def test_empty_tenant_returns_no_buckets(self):
        assert build_insight_buckets(self.empty_tenant.id) == []

    def test_unknown_tenant_returns_no_buckets(self):
        assert build_insight_buckets(987654321) == []

    # -- bucket order / shape / formatting ------------------------------

    def test_active_tenant_buckets_present_in_order(self):
        self._seed_two_active_months_newest_empty()
        buckets = build_insight_buckets(self.tenant.id)

        keys = [b["key"] for b in buckets]
        # Two active months -> momentum is emitted; full fixed order.
        assert keys == ["reach", "sampling", "sales", "new_audience", "momentum"]

        # Every bucket has the full dict shape with a known sentiment.
        for b in buckets:
            assert set(b) == {"key", "title", "detail", "sentiment", "metric"}
            assert b["sentiment"] in {"positive", "neutral", "attention"}
            assert isinstance(b["metric"], str)
            assert isinstance(b["detail"], str)

    def test_numbers_reconcile_with_tenant_kpi_totals(self):
        self._seed_two_active_months_newest_empty()
        totals = tenant_kpi_totals(self.tenant.id)
        by = {b["key"]: b for b in build_insight_buckets(self.tenant.id)}

        # Reach / sampling / sales / new-audience metrics are the formatted
        # source-of-truth totals (thousands separators), so they reconcile.
        assert by["reach"]["metric"] == f"{totals.consumers_reached:,}"
        assert by["sampling"]["metric"] == f"{totals.samples_distributed:,}"
        assert by["sales"]["metric"] == f"{totals.products_sold:,}"
        assert by["new_audience"]["metric"] == f"{totals.first_time_consumers:,}"

        # consumers = 200 + 150 = 350 -> formatted with a comma.
        assert totals.consumers_reached == 350
        assert by["reach"]["metric"] == "350"
        # samples = 50 + 30 = 80; 2 events -> ~40/event.
        assert totals.samples_distributed == 80
        assert "~40/event" in by["sampling"]["detail"]
        # sales detail shows the cans/packs breakdown (12 cans · 3 packs).
        assert "12 cans" in by["sales"]["detail"]
        assert "3 packs" in by["sales"]["detail"]

    def test_thousands_separator_formatting(self):
        # One big month so a >=1,000 figure must render with a comma.
        self._recap_in_month(
            self.older_month, engagements=5000, consumers=12400, samples=3400
        )
        by = {b["key"]: b for b in build_insight_buckets(self.tenant.id)}
        assert by["reach"]["metric"] == "12,400"
        assert "12,400 consumers reached" in by["reach"]["detail"]
        assert by["sampling"]["metric"] == "3,400"

    # -- THE Momentum fix ------------------------------------------------

    def test_momentum_ignores_empty_current_month_no_negative_no_halted(self):
        """Regression: newest trend month empty -> Momentum reflects the last
        ACTIVE month, never a negative/'-100%'/'halted' delta."""
        self._seed_two_active_months_newest_empty()
        by = {b["key"]: b for b in build_insight_buckets(self.tenant.id)}
        momentum = by["momentum"]

        # Sanity: the current/newest trend bucket really is empty.
        trend = {m.month: m for m in tenant_monthly_trend(self.tenant.id)}
        cur = trend[self.current_empty_month]
        assert (cur.recaps, cur.engagements, cur.samples) == (0, 0, 0)

        # The comparison is the two ACTIVE months (120 vs 100 = +20% up), so
        # the bucket is an UP card, never the empty-month collapse.
        assert momentum["metric"] == "▲ 20% vs " + self._short(self.older_month)
        assert momentum["sentiment"] == "positive"
        # Detail names the latest ACTIVE month, NOT the empty current month.
        assert self.newer_active_month in momentum["detail"]
        assert self.current_empty_month not in momentum["detail"]

        # The forbidden strings must appear NOWHERE in any bucket.
        blob = " ".join(
            f"{b['metric']} {b['detail']} {b['title']}"
            for b in by.values()
        )
        assert "-100%" not in blob
        assert "halted" not in blob.lower()
        assert "▼" not in blob  # nothing trended down here

    def test_momentum_attention_on_steep_drop(self):
        # Older month strong (100), newer ACTIVE month collapses (10) = -90%,
        # newest bucket empty. A real, meaningful decline -> "attention".
        self._recap_in_month(self.older_month, engagements=100, consumers=50)
        self._recap_in_month(self.newer_active_month, engagements=10, consumers=5)
        by = {b["key"]: b for b in build_insight_buckets(self.tenant.id)}
        momentum = by["momentum"]
        assert momentum["metric"].startswith("▼ 90% vs ")
        assert momentum["sentiment"] == "attention"
        # Still sourced from a REAL prior month, not the empty tail.
        assert "halted" not in momentum["detail"].lower()

    def test_single_active_month_reports_peak_not_delta(self):
        # Exactly one active month -> no comparison; a neutral "Peak" card.
        self._recap_in_month(self.newer_active_month, engagements=77, consumers=88)
        by = {b["key"]: b for b in build_insight_buckets(self.tenant.id)}
        momentum = by["momentum"]
        assert momentum["sentiment"] == "neutral"
        assert momentum["metric"] == "Peak: " + self._short(self.newer_active_month)
        assert "77 engagements" in momentum["detail"]
        assert "%" not in momentum["metric"]

    def test_momentum_absent_when_no_active_months(self):
        """Activity exists only via fields that don't bucket into the trend
        (none here) -> with zero active trend months, the momentum card is
        omitted entirely rather than emitting a misleading card.

        We simulate this by giving the tenant activity ONLY in the current
        empty-by-trend sense: a recap whose trend month is the current bucket
        but with zero engagements/samples (so it has no *active* month) — the
        recap still makes the tenant non-empty (recap_count > 0), so the other
        four buckets appear but momentum does not.
        """
        # A recap with zero engagements/samples in the current month: it
        # counts as a recap (tenant is non-empty) but the trend month has no
        # engagements/samples and recaps>0 makes it "active"... so to get a
        # truly no-active-month case we must avoid the recap landing in the
        # trend window at all. Place it BEFORE the window via a far-past date.
        event = self.create_event(name="ancient", tenant=self.tenant)
        recap = recap_models.Recap.objects.create(
            name="ancient recap",
            event=event,
            total_engagements=0,
            products_sold=3,  # gives the tenant some non-zero KPI total
            created_by=self.system_user,
            updated_by=self.system_user,
        )
        ancient = timezone.now().replace(year=2000, month=1, day=15)
        recap_models.Recap.objects.filter(id=recap.id).update(created_at=ancient)

        buckets = build_insight_buckets(self.tenant.id)
        keys = [b["key"] for b in buckets]
        # Tenant is non-empty (a recap + products_sold) so the four core
        # buckets show, but no trend month is active -> momentum omitted.
        assert keys == ["reach", "sampling", "sales", "new_audience"]
        assert "momentum" not in keys

    @staticmethod
    def _short(month_key: str) -> str:
        from recaps.tenant_insights import _short_month

        return _short_month(month_key)

    # -- GraphQL resolver: live compute, scoping, never-raise -----------

    @pytest.mark.asyncio
    async def test_graphql_admin_gets_live_buckets_with_key_field(self):
        from asgiref.sync import sync_to_async

        await sync_to_async(self._seed_two_active_months_newest_empty)()

        result = await self._execute_query_authenticated(
            INSIGHTS_QUERY,
            {"tenantId": str(self.tenant.id)},
            self.spark_admin,
            self.endpoint_path,
        )
        assert result.errors is None, f"errored: {result.errors}"
        payload = result.data["tenantInsights"]
        # Live compute always stamps generatedAt (never null for an active tenant).
        assert payload["generatedAt"] is not None
        items = payload["items"]
        assert [i["key"] for i in items] == [
            "reach",
            "sampling",
            "sales",
            "new_audience",
            "momentum",
        ]
        momentum = next(i for i in items if i["key"] == "momentum")
        assert momentum["metric"].startswith("▲ 20% vs ")
        assert "-100%" not in momentum["metric"]

    @pytest.mark.asyncio
    async def test_graphql_client_pinned_to_own_tenant(self):
        from asgiref.sync import sync_to_async

        await sync_to_async(self._seed_two_active_months_newest_empty)()

        # Client passes the EMPTY tenant's id but is pinned to their own
        # (the active one), so they get their own non-empty buckets.
        result = await self._execute_query_authenticated(
            INSIGHTS_QUERY,
            {"tenantId": str(self.empty_tenant.id)},
            self.client_user,
            self.endpoint_path,
        )
        assert result.errors is None, f"errored: {result.errors}"
        items = result.data["tenantInsights"]["items"]
        assert [i["key"] for i in items][:4] == [
            "reach",
            "sampling",
            "sales",
            "new_audience",
        ]

    @pytest.mark.asyncio
    async def test_graphql_empty_tenant_degrades_to_empty_items(self):
        result = await self._execute_query_authenticated(
            INSIGHTS_QUERY,
            {"tenantId": str(self.empty_tenant.id)},
            self.spark_admin,
            self.endpoint_path,
        )
        assert result.errors is None, f"errored: {result.errors}"
        assert result.data["tenantInsights"]["items"] == []

    @pytest.mark.asyncio
    async def test_graphql_unknown_tenant_empty_not_error(self):
        result = await self._execute_query_authenticated(
            INSIGHTS_QUERY,
            {"tenantId": "987654321"},
            self.spark_admin,
            self.endpoint_path,
        )
        assert result.errors is None, f"errored: {result.errors}"
        payload = result.data["tenantInsights"]
        assert payload["items"] == []
        assert payload["generatedAt"] is None
