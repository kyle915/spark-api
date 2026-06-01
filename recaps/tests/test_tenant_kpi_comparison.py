"""Coverage for the period-over-period KPI comparison backend:

* :func:`recaps.tenant_overview.tenant_kpi_comparison` — "this period vs last"
  for the FULL nine-KPI set (+ event/recap counts), which picks the most
  recent COMPLETE period of a granularity (month / quarter / year) and the
  complete period immediately before it, and
* the ``tenantKpiComparison(tenantId, period)`` GraphQL query on the clients
  schema — the tenant-scoped, never-raises shell around it.

The headline guarantees these tests lock in:

* **Complete periods only.** ``current`` is always the most recent FULLY
  ELAPSED period; the in-progress current month/quarter/year is NEVER used as
  ``current`` (that would recreate the "-100% halted" partial-period
  distortion the Momentum bucket already had to fix). A recap dated into the
  in-progress current month must NOT appear in ``current``.
* **Right window selection** for each granularity, with the correct human
  labels ("May 2026" vs "Apr 2026"; "Q1 2026" vs "Q4 2025"; "2025" vs "2024").
* **Reconciliation** — a period's totals equal the source-of-truth
  :func:`recaps.tenant_overview.tenant_kpi_totals` for a matching span.
* Tenant scoping (client → own tenant, admin → any) and the degrade-to-null
  posture (missing / out-of-scope tenant, never an error).

Wall-clock independence: every test pins ``timezone.now()`` inside the
``tenant_overview`` module to a FIXED anchor so the "complete period"
selection is deterministic, and dates each fixture into a specific window via
``.update(created_at=...)`` (``created_at`` is ``auto_now_add``, so it must be
back-dated after creation — the same trick the existing insights /
market-performance tests use). Fixtures follow the style of
test_tenant_market_performance.py.
"""

from unittest import mock

import pytest
from django.utils import timezone

from ambassadors.tests.base import AmbassadorsGraphQLTestCase
from events import models as event_models
from recaps import models as recap_models
from recaps import tenant_overview
from recaps.tenant_overview import tenant_kpi_comparison, tenant_kpi_totals


COMPARISON_QUERY = """
query Comparison($tenantId: ID!, $period: String) {
  tenantKpiComparison(tenantId: $tenantId, period: $period) {
    period
    currentLabel
    previousLabel
    current {
      events
      recaps
      consumersReached
      samplesDistributed
      productsSold
      cansSold
      packsSold
      totalEngagements
      firstTimeConsumers
      brandAwareConsumers
      willingToPurchase
    }
    previous {
      events
      recaps
      consumersReached
      samplesDistributed
      productsSold
      cansSold
      packsSold
      totalEngagements
      firstTimeConsumers
      brandAwareConsumers
      willingToPurchase
    }
  }
}
"""

# Fixed "now": mid-June 2026. With this anchor the COMPLETE-period selection
# is fully determined:
#   month   -> current May 2026,  previous Apr 2026
#   quarter -> current Q1 2026,   previous Q4 2025
#   year    -> current 2025,      previous 2024
# and the in-progress June 2026 / Q2 2026 / 2026 are partial -> never used.
_FAKE_NOW = timezone.make_aware(
    __import__("datetime").datetime(2026, 6, 15, 10, 30, 0)
)


def _at(year: int, month: int, day: int = 10):
    """A tz-aware datetime inside ``year``-``month`` (mid-month by default)."""
    return timezone.make_aware(
        __import__("datetime").datetime(year, month, day, 12, 0, 0)
    )


@pytest.mark.django_db(transaction=True)
class TestTenantKpiComparison(AmbassadorsGraphQLTestCase):
    """Complete-period selection + reconciliation + scoping for the comparison."""

    @pytest.fixture(autouse=True)
    def setup(self, db):
        from config.schema_client import schema_clients

        self.roles = self.setup_default_roles()
        self.schema = schema_clients
        self.endpoint_path = "/api/v1/graphql/clients"
        self.system_user = self.get_system_user()

        self.tenant = self.create_tenant(name="Girl Beer")
        self.other_tenant = self.create_tenant(name="Liquid Death")
        self.empty_tenant = self.create_tenant(name="Brand New")

        self.spark_admin = self.create_user(
            username="admin-cmp",
            email="admin-cmp@test.com",
            role=self.roles["spark_admin"],
        )
        self.client_user = self.create_user(
            username="client-cmp",
            email="client-cmp@test.com",
            role=self.roles["client"],
        )
        self.create_tenanted_user(self.client_user, self.tenant)

        # --- Seed the tenant's activity into specific calendar windows ----
        # Values are distinct per month so a window can never accidentally
        # pick up another month's numbers.
        #
        # May 2026  (the most-recent-COMPLETE month for a mid-June "now"):
        self._recap_in(
            _at(2026, 5),
            engagements=120,
            consumers=300,
            samples=80,
            products_sold=12,
            cans=8,
            packs=4,
            first_time=40,
            brand_aware=110,
            willing=70,
        )
        # April 2026 (the previous complete month):
        self._recap_in(
            _at(2026, 4),
            engagements=90,
            consumers=200,
            samples=50,
            products_sold=7,
            cans=5,
            packs=2,
            first_time=25,
            brand_aware=80,
            willing=45,
        )
        # June 2026 (the IN-PROGRESS current month) — a "partial" period that
        # must NEVER be used as `current`. A big, unmistakable value so a leak
        # would be obvious.
        self._recap_in(
            _at(2026, 6, 5),
            engagements=99999,
            consumers=99999,
            samples=99999,
            products_sold=99999,
        )

        # --- Other tenant in May 2026: must never leak into our roll-up ---
        self._recap_in(
            _at(2026, 5),
            engagements=5000,
            consumers=5000,
            tenant=self.other_tenant,
        )

    # -- fixture helpers ------------------------------------------------

    def _product(self, name: str):
        product_type = event_models.ProductType.objects.create(
            name=f"type {name}", tenant=self.tenant, created_by=self.system_user
        )
        return event_models.Product.objects.create(
            name=name,
            product_type=product_type,
            tenant=self.tenant,
            created_by=self.system_user,
        )

    def _recap_in(
        self,
        when,
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
        tenant=None,
    ) -> recap_models.Recap:
        """Create a legacy recap (+ event + children) dated to ``when``.

        Everything (the event, the recap, and every child row that feeds a
        KPI) is back-dated with ``.update(created_at=...)`` because the column
        is ``auto_now_add`` — each KPI source filters on its OWN ``created_at``,
        so all of them must land in the target window for the figures to
        reconcile.
        """
        tenant = tenant or self.tenant
        label = when.strftime("%Y-%m-%d-%f")

        event = self.create_event(name=f"ev {label}", tenant=tenant)
        event_models.Event.objects.filter(id=event.id).update(created_at=when)

        recap = recap_models.Recap.objects.create(
            name=f"recap {label}",
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
                product=self._product(f"prod {label}"),
                quantity=samples,
                created_by=self.system_user,
                updated_by=self.system_user,
            )
            recap_models.ProductSamples.objects.filter(id=ps.id).update(
                created_at=when
            )
        return recap

    # -- helper-level tests (pinned clock) ------------------------------

    def test_month_selects_two_complete_months_with_labels(self):
        with mock.patch.object(tenant_overview.timezone, "now", return_value=_FAKE_NOW):
            result = tenant_kpi_comparison(self.tenant.id, "month")

        assert result["period"] == "month"
        # Most recent COMPLETE month is May 2026 (June is in progress).
        assert result["current_label"] == "May 2026"
        assert result["previous_label"] == "Apr 2026"

        cur, prev = result["current"], result["previous"]
        # May 2026 numbers exactly (the other tenant's May row is excluded).
        assert cur["total_engagements"] == 120
        assert cur["consumers_reached"] == 300
        assert cur["samples_distributed"] == 80
        assert cur["products_sold"] == 12
        assert cur["cans_sold"] == 8
        assert cur["packs_sold"] == 4
        assert cur["first_time_consumers"] == 40
        assert cur["brand_aware_consumers"] == 110
        assert cur["willing_to_purchase"] == 70
        assert cur["events"] == 1
        assert cur["recaps"] == 1
        # April 2026 numbers exactly.
        assert prev["total_engagements"] == 90
        assert prev["consumers_reached"] == 200
        assert prev["samples_distributed"] == 50
        assert prev["events"] == 1
        assert prev["recaps"] == 1

    def test_partial_current_month_is_not_used(self):
        """The in-progress June 2026 (99999s) must not appear in either side."""
        with mock.patch.object(tenant_overview.timezone, "now", return_value=_FAKE_NOW):
            result = tenant_kpi_comparison(self.tenant.id, "month")

        for side in ("current", "previous"):
            for key, value in result[side].items():
                assert value != 99999, f"partial June leaked into {side}.{key}"
        # And the selected window is May, not June.
        assert result["current_label"] == "May 2026"
        assert "Jun" not in result["current_label"]

    def test_month_reconciles_with_tenant_kpi_totals_for_may(self):
        """`current` (May 2026) equals the source-of-truth window for May.

        Reconciliation is checked against a one-month window built with the
        SAME ``_year_bounds``-style helper the totals use, so the comparison
        figures provably agree with ``tenant_kpi_totals``' aggregation.
        """
        with mock.patch.object(tenant_overview.timezone, "now", return_value=_FAKE_NOW):
            result = tenant_kpi_comparison(self.tenant.id, "month")
            # The May window: [2026-05-01, 2026-06-01).
            may_window = (
                tenant_overview._month_start(2026, 5),
                tenant_overview._month_start(2026, 6),
            )
            totals = tenant_overview._tenant_kpi_totals_window(
                self.tenant.id, may_window
            )

        cur = result["current"]
        assert cur["consumers_reached"] == totals.consumers_reached
        assert cur["samples_distributed"] == totals.samples_distributed
        assert cur["products_sold"] == totals.products_sold
        assert cur["total_engagements"] == totals.total_engagements
        assert cur["first_time_consumers"] == totals.first_time_consumers
        assert cur["brand_aware_consumers"] == totals.brand_aware_consumers
        assert cur["willing_to_purchase"] == totals.willing_to_purchase

    def test_quarter_selects_two_complete_quarters_with_labels(self):
        with mock.patch.object(tenant_overview.timezone, "now", return_value=_FAKE_NOW):
            result = tenant_kpi_comparison(self.tenant.id, "quarter")

        assert result["period"] == "quarter"
        # mid-June 2026 is in Q2 -> most recent complete quarter is Q1 2026,
        # previous is Q4 2025 (rolls across the year boundary).
        assert result["current_label"] == "Q1 2026"
        assert result["previous_label"] == "Q4 2025"

        # Q1 2026 (Jan-Mar) aggregates April+May? No — those are Q2. Q1 has no
        # fixture activity, so it is all zeros; Q4 2025 likewise empty.
        cur, prev = result["current"], result["previous"]
        assert cur["total_engagements"] == 0
        assert cur["consumers_reached"] == 0
        assert prev["total_engagements"] == 0

    def test_quarter_reconciles_for_a_quarter_with_activity(self):
        """Q2 2026 (the in-progress quarter) holds Apr+May+June activity.

        We don't compare against it as a period (it's partial), but we DO use
        it to prove the quarter windowing sums a quarter correctly: a window
        over [Apr 1, Jul 1) must equal Apr+May+June via the source of truth.
        """
        with mock.patch.object(tenant_overview.timezone, "now", return_value=_FAKE_NOW):
            q2_window = (
                tenant_overview._month_start(2026, 4),
                tenant_overview._month_start(2026, 7),
            )
            totals = tenant_overview._tenant_kpi_totals_window(
                self.tenant.id, q2_window
            )
        # Apr (90) + May (120) + June (99999) engagements.
        assert totals.total_engagements == 90 + 120 + 99999
        # Apr (200) + May (300) + June (99999) consumers.
        assert totals.consumers_reached == 200 + 300 + 99999

    def test_year_selects_two_complete_years_with_labels(self):
        with mock.patch.object(tenant_overview.timezone, "now", return_value=_FAKE_NOW):
            result = tenant_kpi_comparison(self.tenant.id, "year")

        assert result["period"] == "year"
        # 2026 is in progress -> current is 2025, previous is 2024.
        assert result["current_label"] == "2025"
        assert result["previous_label"] == "2024"

    def test_year_current_reconciles_with_tenant_kpi_totals_year(self):
        """`current` for `year` (2025) equals tenant_kpi_totals(year=2025)."""
        # Seed one 2025 recap so the current-year side is non-trivial.
        self._recap_in(_at(2025, 7), engagements=33, consumers=44, samples=22)

        with mock.patch.object(tenant_overview.timezone, "now", return_value=_FAKE_NOW):
            result = tenant_kpi_comparison(self.tenant.id, "year")
            totals_2025 = tenant_kpi_totals(self.tenant.id, year=2025)

        cur = result["current"]
        assert cur["total_engagements"] == totals_2025.total_engagements == 33
        assert cur["consumers_reached"] == totals_2025.consumers_reached == 44
        assert cur["samples_distributed"] == totals_2025.samples_distributed == 22
        # 2024 had no activity -> previous is all zeros.
        assert result["previous"]["total_engagements"] == 0

    def test_unknown_period_falls_back_to_month(self):
        with mock.patch.object(tenant_overview.timezone, "now", return_value=_FAKE_NOW):
            result = tenant_kpi_comparison(self.tenant.id, "decade")
        assert result["period"] == "month"
        assert result["current_label"] == "May 2026"

    def test_empty_tenant_is_all_zeros_not_error(self):
        with mock.patch.object(tenant_overview.timezone, "now", return_value=_FAKE_NOW):
            result = tenant_kpi_comparison(self.empty_tenant.id, "month")
        assert result["current_label"] == "May 2026"
        assert all(v == 0 for v in result["current"].values())
        assert all(v == 0 for v in result["previous"].values())

    def test_unknown_tenant_is_all_zeros(self):
        # The helper itself doesn't existence-check (the resolver does); over
        # no rows every aggregate is simply zero.
        with mock.patch.object(tenant_overview.timezone, "now", return_value=_FAKE_NOW):
            result = tenant_kpi_comparison(987654321, "month")
        assert all(v == 0 for v in result["current"].values())

    # -- GraphQL resolver tests -----------------------------------------

    @pytest.mark.asyncio
    async def test_admin_targets_tenant(self):
        with mock.patch.object(tenant_overview.timezone, "now", return_value=_FAKE_NOW):
            result = await self._execute_query_authenticated(
                COMPARISON_QUERY,
                {"tenantId": str(self.tenant.id), "period": "month"},
                self.spark_admin,
                self.endpoint_path,
            )
        assert result.errors is None, f"errored: {result.errors}"
        cmp = result.data["tenantKpiComparison"]
        assert cmp["period"] == "month"
        assert cmp["currentLabel"] == "May 2026"
        assert cmp["previousLabel"] == "Apr 2026"
        assert cmp["current"]["totalEngagements"] == 120
        assert cmp["current"]["consumersReached"] == 300
        assert cmp["previous"]["totalEngagements"] == 90

    @pytest.mark.asyncio
    async def test_period_defaults_to_month_when_omitted(self):
        with mock.patch.object(tenant_overview.timezone, "now", return_value=_FAKE_NOW):
            result = await self._execute_query_authenticated(
                COMPARISON_QUERY,
                {"tenantId": str(self.tenant.id)},  # no period arg
                self.spark_admin,
                self.endpoint_path,
            )
        assert result.errors is None, f"errored: {result.errors}"
        cmp = result.data["tenantKpiComparison"]
        assert cmp["period"] == "month"
        assert cmp["currentLabel"] == "May 2026"

    @pytest.mark.asyncio
    async def test_client_pinned_to_own_tenant(self):
        # Client passes the OTHER tenant's id but is pinned to their own.
        with mock.patch.object(tenant_overview.timezone, "now", return_value=_FAKE_NOW):
            result = await self._execute_query_authenticated(
                COMPARISON_QUERY,
                {"tenantId": str(self.other_tenant.id), "period": "month"},
                self.client_user,
                self.endpoint_path,
            )
        assert result.errors is None, f"errored: {result.errors}"
        cmp = result.data["tenantKpiComparison"]
        # Their own (Girl Beer) May data, NOT Liquid Death's 5000 row.
        assert cmp["current"]["totalEngagements"] == 120
        assert cmp["current"]["consumersReached"] == 300
        assert cmp["current"]["totalEngagements"] != 5000

    @pytest.mark.asyncio
    async def test_admin_unknown_tenant_null_not_error(self):
        with mock.patch.object(tenant_overview.timezone, "now", return_value=_FAKE_NOW):
            result = await self._execute_query_authenticated(
                COMPARISON_QUERY,
                {"tenantId": "987654321", "period": "month"},
                self.spark_admin,
                self.endpoint_path,
            )
        assert result.errors is None, f"errored: {result.errors}"
        assert result.data["tenantKpiComparison"] is None
