"""
Coverage for the geographic performance heatmap backend:

* :func:`recaps.tenant_overview.tenant_market_performance` — the per-US-state
  KPI roll-up that groups a tenant's recaps by *the event's* state
  (``events.models.Event.state.code``) across BOTH recap shapes (legacy
  ``Recap`` + custom ``CustomRecap``), and
* the ``tenantMarketPerformance(tenantId, year)`` GraphQL query on the
  clients schema — the tenant-scoped, never-raises shell around it.

The helper tests assert the grouping/merge math directly against the DB;
the GraphQL tests assert the ``MarketPerformance`` shape, tenant scoping
(clients pinned to their own tenant, admin may target any), and the
degrade-to-empty-list posture (missing / out-of-scope tenant, never an
error). Mirrors the fixture style of test_recaps_list_tenant_isolation.py.
"""

import pytest

from ambassadors.tests.base import AmbassadorsGraphQLTestCase
from events import models as event_models
from recaps import models as recap_models
from recaps.tenant_overview import tenant_market_performance


MARKET_QUERY = """
query Market($tenantId: ID!, $year: Int) {
  tenantMarketPerformance(tenantId: $tenantId, year: $year) {
    state
    eventCount
    recapCount
    consumersReached
    samplesDistributed
    productsSold
    totalEngagements
  }
}
"""


@pytest.mark.django_db(transaction=True)
class TestTenantMarketPerformance(AmbassadorsGraphQLTestCase):
    """Per-state KPI roll-up grouped by the event's US state."""

    @pytest.fixture(autouse=True)
    def setup(self, db):
        from config.schema_client import schema_clients

        self.roles = self.setup_default_roles()
        self.schema = schema_clients
        self.endpoint_path = "/api/v1/graphql/clients"
        self.system_user = self.get_system_user()

        self.tenant = self.create_tenant(name="Girl Beer")
        self.other_tenant = self.create_tenant(name="Liquid Death")

        self.spark_admin = self.create_user(
            username="admin-market",
            email="admin-market@test.com",
            role=self.roles["spark_admin"],
        )
        self.client_user = self.create_user(
            username="client-market",
            email="client-market@test.com",
            role=self.roles["client"],
        )
        self.create_tenanted_user(self.client_user, self.tenant)

        self.ca = self._state("California", "CA")
        self.tx = self._state("Texas", "TX")

        # --- CA: one legacy recap + one custom recap -------------------
        ca_event_legacy = self.create_event(
            name="WF Burbank", tenant=self.tenant, state=self.ca
        )
        ca_legacy = recap_models.Recap.objects.create(
            name="ca legacy",
            event=ca_event_legacy,
            total_engagements=10,
            products_sold=5,
            created_by=self.system_user,
            updated_by=self.system_user,
        )
        recap_models.ConsumerEngagements.objects.create(
            recap=ca_legacy,
            total_consumer=100,
            created_by=self.system_user,
            updated_by=self.system_user,
        )
        recap_models.ProductSamples.objects.create(
            recap=ca_legacy,
            product=self._product("La Croix"),
            quantity=20,
            created_by=self.system_user,
            updated_by=self.system_user,
        )

        ca_event_custom = self.create_event(
            name="WF Pasadena", tenant=self.tenant, state=self.ca
        )
        self.template = self._template("GB Template")
        ca_custom = recap_models.CustomRecap.objects.create(
            name="ca custom",
            event=ca_event_custom,
            tenant=self.tenant,
            custom_recap_template=self.template,
            total_engagements=7,
            created_by=self.system_user,
            updated_by=self.system_user,
        )
        # Free-text KPI fields: "Consumers Sampled" -> consumers_reached,
        # "Cans Sold" matches the cans/packs sold regex.
        self._field_value(ca_custom, "Consumers Sampled", "30")
        self._field_value(ca_custom, "Cans Sold", "4")

        # --- TX: one legacy recap (engagements only) -------------------
        tx_event = self.create_event(
            name="HEB Austin", tenant=self.tenant, state=self.tx
        )
        recap_models.Recap.objects.create(
            name="tx legacy",
            event=tx_event,
            total_engagements=3,
            created_by=self.system_user,
            updated_by=self.system_user,
        )

        # --- Event with NO state: must be skipped (no map bucket) ------
        nostate_event = self.create_event(
            name="No State", tenant=self.tenant, state=None
        )
        recap_models.Recap.objects.create(
            name="nostate legacy",
            event=nostate_event,
            total_engagements=999,
            created_by=self.system_user,
            updated_by=self.system_user,
        )

        # --- Other tenant in CA: must never leak into our roll-up ------
        other_event = self.create_event(
            name="Valero", tenant=self.other_tenant, state=self.ca
        )
        recap_models.Recap.objects.create(
            name="other legacy",
            event=other_event,
            total_engagements=5000,
            created_by=self.system_user,
            updated_by=self.system_user,
        )

    # -- fixture helpers ------------------------------------------------

    def _state(self, name: str, code: str) -> event_models.State:
        return event_models.State.objects.create(
            name=name, code=code, created_by=self.system_user
        )

    def _product(self, name: str) -> event_models.Product:
        product_type = event_models.ProductType.objects.create(
            name="Beverage", tenant=self.tenant, created_by=self.system_user
        )
        return event_models.Product.objects.create(
            name=name,
            product_type=product_type,
            tenant=self.tenant,
            created_by=self.system_user,
        )

    def _template(self, name: str) -> recap_models.CustomRecapTemplate:
        event_type = self.create_event_type(name="Sampling", tenant=self.tenant)
        return recap_models.CustomRecapTemplate.objects.create(
            name=name,
            event_type=event_type,
            tenant=self.tenant,
            created_by=self.system_user,
        )

    def _field_value(self, custom_recap, field_name: str, value: str):
        field_type = recap_models.CustomRecapFieldType.objects.create(
            name="number", created_by=self.system_user
        )
        section = recap_models.RecapSection.objects.create(
            name="KPIs", tenant=self.tenant, created_by=self.system_user
        )
        field = recap_models.CustomField.objects.create(
            name=field_name,
            custom_recap_template=custom_recap.custom_recap_template,
            custom_field_type=field_type,
            recap_section=section,
            created_by=self.system_user,
        )
        return recap_models.CustomFieldValue.objects.create(
            custom_recap=custom_recap,
            custom_field=field,
            value=value,
            created_by=self.system_user,
        )

    # -- helper-level tests ---------------------------------------------

    def test_groups_by_event_state_and_skips_nullstate(self):
        rows = tenant_market_performance(self.tenant.id)
        by = {r["state"]: r for r in rows}
        # Only CA + TX; the no-state event is dropped, other tenant excluded.
        assert set(by) == {"CA", "TX"}
        # Deterministic, sorted-by-code order.
        assert [r["state"] for r in rows] == ["CA", "TX"]

    def test_ca_merges_legacy_and_custom(self):
        by = {r["state"]: r for r in tenant_market_performance(self.tenant.id)}
        ca = by["CA"]
        assert ca["event_count"] == 2  # two CA events
        assert ca["recap_count"] == 2  # one legacy + one custom
        assert ca["total_engagements"] == 17  # 10 legacy + 7 custom
        assert ca["consumers_reached"] == 130  # 100 legacy + 30 custom sampled
        # 5 legacy products_sold + 4 from the custom "Cans Sold" free-text
        # field (cans/packs feed products_sold, mirroring tenant_kpi_totals).
        assert ca["products_sold"] == 9
        # samples: legacy structured 20 + custom fallback to its 30 sampled
        # (custom has no structured CustomRecapProductSample rows).
        assert ca["samples_distributed"] == 50

    def test_tx_legacy_only(self):
        by = {r["state"]: r for r in tenant_market_performance(self.tenant.id)}
        tx = by["TX"]
        assert tx["event_count"] == 1
        assert tx["recap_count"] == 1
        assert tx["total_engagements"] == 3
        assert tx["consumers_reached"] == 0
        assert tx["samples_distributed"] == 0
        assert tx["products_sold"] == 0

    def test_reconciles_with_tenant_kpi_totals(self):
        # Summing each KPI across states must equal the whole-tenant total
        # from the source of truth, MINUS the one no-state recap (999
        # engagements) that has no map bucket and is intentionally excluded.
        from recaps.tenant_overview import tenant_kpi_totals

        totals = tenant_kpi_totals(self.tenant.id)
        rows = tenant_market_performance(self.tenant.id)
        summed_engagements = sum(r["total_engagements"] for r in rows)
        # totals includes the no-state recap's 999; the per-state view omits it.
        assert summed_engagements == totals.total_engagements - 999
        assert sum(r["consumers_reached"] for r in rows) == totals.consumers_reached
        assert sum(r["products_sold"] for r in rows) == totals.products_sold

    def test_year_filter_excludes_out_of_range(self):
        # All fixtures are created "now"; a far-past year yields no rows.
        assert tenant_market_performance(self.tenant.id, year=2000) == []

    def test_year_filter_uses_event_date(self):
        # Windowing is on the EVENT date, not created_at: a recap whose event
        # is dated 2022 lands in the 2022 view even though its row was created
        # "now"; the now-dated setup events do not. Counts + KPIs share this
        # basis, so a state's year numbers agree.
        import datetime

        from django.utils import timezone

        when = timezone.make_aware(datetime.datetime(2022, 5, 1, 12, 0))
        ev = self.create_event(name="WF 2022", tenant=self.tenant, state=self.ca)
        event_models.Event.objects.filter(id=ev.id).update(
            date=when, start_time=when
        )
        recap_models.Recap.objects.create(
            name="2022 legacy", event=ev, total_engagements=42,
            created_by=self.system_user, updated_by=self.system_user,
        )
        by = {r["state"]: r for r in tenant_market_performance(self.tenant.id, year=2022)}
        # Only the 2022-dated event's state appears; the now-dated setup
        # CA/TX events are excluded from the 2022 window.
        assert set(by) == {"CA"}
        assert by["CA"]["total_engagements"] == 42
        assert by["CA"]["recap_count"] == 1
        assert by["CA"]["event_count"] == 1
        # A neighbouring year with no event-dated activity is empty.
        assert tenant_market_performance(self.tenant.id, year=2023) == []

    def test_unknown_tenant_is_empty(self):
        assert tenant_market_performance(987654321) == []

    # -- GraphQL resolver tests -----------------------------------------

    @pytest.mark.asyncio
    async def test_admin_targets_tenant(self):
        result = await self._execute_query_authenticated(
            MARKET_QUERY,
            {"tenantId": str(self.tenant.id)},
            self.spark_admin,
            self.endpoint_path,
        )
        assert result.errors is None, f"errored: {result.errors}"
        rows = result.data["tenantMarketPerformance"]
        by = {r["state"]: r for r in rows}
        assert set(by) == {"CA", "TX"}
        assert by["CA"]["totalEngagements"] == 17
        assert by["CA"]["consumersReached"] == 130
        assert by["TX"]["recapCount"] == 1

    @pytest.mark.asyncio
    async def test_client_pinned_to_own_tenant(self):
        # Client passes the OTHER tenant's id but is pinned to their own.
        result = await self._execute_query_authenticated(
            MARKET_QUERY,
            {"tenantId": str(self.other_tenant.id)},
            self.client_user,
            self.endpoint_path,
        )
        assert result.errors is None, f"errored: {result.errors}"
        rows = result.data["tenantMarketPerformance"]
        # Their own (Girl Beer) data, NOT Liquid Death's 5000-engagement row.
        by = {r["state"]: r for r in rows}
        assert set(by) == {"CA", "TX"}
        assert all(r["totalEngagements"] != 5000 for r in rows)

    @pytest.mark.asyncio
    async def test_admin_unknown_tenant_empty_not_error(self):
        result = await self._execute_query_authenticated(
            MARKET_QUERY,
            {"tenantId": "987654321"},
            self.spark_admin,
            self.endpoint_path,
        )
        assert result.errors is None, f"errored: {result.errors}"
        assert result.data["tenantMarketPerformance"] == []
