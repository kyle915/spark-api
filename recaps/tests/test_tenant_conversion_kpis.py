"""Torch-style conversion: products purchased ÷ consumers sampled / samples given."""

from datetime import datetime, timedelta

import pytest
from django.utils import timezone

from ambassadors.tests.base import AmbassadorsGraphQLTestCase
from events import models as event_models
from recaps import models as recap_models
from recaps.tenant_overview import (
    conversion_window_totals,
    sales_program_metrics,
    tenant_conversion_kpis,
    tenant_monthly_trend,
)


@pytest.mark.django_db(transaction=True)
class TestTenantConversionSampledBase(AmbassadorsGraphQLTestCase):
    @pytest.fixture(autouse=True)
    def setup(self, db):
        self.roles = self.setup_default_roles()
        self.sys = self.get_system_user()
        self.tenant = self.create_tenant(name="Torch Conv Co")
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
            name="Total Wine",
            address="1 Main",
            store_contact="mgr",
            location=self.location,
            tenant=self.tenant,
        )
        self.status = self.create_request_status(
            name="Scheduled", tenant=self.tenant, create_event=True
        )
        self.retail_type = self.create_request_type(
            "Retail Sampling", self.tenant
        )
        self.event_type = self.create_request_type(
            "Event Activation", self.tenant
        )
        event_type = event_models.EventType.objects.create(
            name="et torch",
            slug="et-torch-conv",
            tenant=self.tenant,
            created_by=self.sys,
        )
        self.template = recap_models.CustomRecapTemplate.objects.create(
            name="Torch Retail",
            event_type=event_type,
            tenant=self.tenant,
            created_by=self.sys,
        )
        self.field_type = recap_models.CustomRecapFieldType.objects.create(
            name="Number", created_by=self.sys
        )
        self.section = recap_models.RecapSection.objects.create(
            name="Sampling", tenant=self.tenant, created_by=self.sys
        )
        self.today = timezone.localdate()
        self.start = self.today - timedelta(days=29)
        self.end = self.today

    def _custom_recap(
        self,
        *,
        request_type,
        fields: list[tuple[str, str]],
        engagements: int = 0,
        when=None,
    ):
        when = when or timezone.make_aware(
            datetime(self.today.year, self.today.month, self.today.day, 12, 0)
        )
        req = self.create_request(
            name=f"req {request_type.name}",
            date=when,
            address="1 Main",
            client=self.client_row,
            distributor=self.distributor,
            retailer=self.retailer,
            request_type=request_type,
            tenant=self.tenant,
            status=self.status,
        )
        event = req.event_set.first() or self.create_event(
            name=f"ev {request_type.name}", tenant=self.tenant
        )
        if event.request_id != req.id:
            event.request = req
            event.save(update_fields=["request"])
        event_models.Event.objects.filter(id=event.id).update(date=when)
        recap = recap_models.CustomRecap.objects.create(
            name=f"recap {request_type.name}",
            approved=True,
            event=event,
            tenant=self.tenant,
            custom_recap_template=self.template,
            total_engagements=engagements,
            created_by=self.sys,
        )
        for name, value in fields:
            field = recap_models.CustomField.objects.create(
                name=name,
                required=False,
                custom_recap_template=self.template,
                custom_field_type=self.field_type,
                recap_section=self.section,
                created_by=self.sys,
            )
            recap_models.CustomFieldValue.objects.create(
                custom_recap=recap,
                custom_field=field,
                value=value,
                created_by=self.sys,
            )
        return recap

    def test_torch_style_sold_over_consumers_sampled(self):
        # Torch logs cans/packs purchased + consumers sampled. Typed
        # total_engagements may be 0 — CONV must still resolve.
        self._custom_recap(
            request_type=self.retail_type,
            engagements=0,
            fields=[
                ("Total number of consumers sampled", "87"),
                ("How many single cans did consumers purchase?", "34"),
                ("How many packs did consumers purchase?", "12"),
            ],
        )
        data = tenant_conversion_kpis(
            self.tenant.id, start=self.start, end=self.end
        )
        assert data["sold"] == 46  # 34 cans + 12 packs
        assert data["engagements"] == 87  # sampled base (GraphQL field name)
        assert data["pct"] == round((46 / 87) * 100, 1)

    def test_samples_given_preferred_over_consumers_sampled(self):
        self._custom_recap(
            request_type=self.retail_type,
            fields=[
                ("Total Samples Given Out", "95"),
                ("Consumers Sampled", "90"),
                ("Cans Sold", "20"),
            ],
        )
        data = tenant_conversion_kpis(
            self.tenant.id, start=self.start, end=self.end
        )
        assert data["sold"] == 20
        assert data["engagements"] == 95
        assert data["pct"] == round((20 / 95) * 100, 1)

    def test_event_activation_excluded(self):
        self._custom_recap(
            request_type=self.event_type,
            fields=[
                ("Total number of consumers sampled", "100"),
                ("How many single cans did consumers purchase?", "50"),
            ],
        )
        data = tenant_conversion_kpis(
            self.tenant.id, start=self.start, end=self.end
        )
        assert data["sold"] == 0
        assert data["engagements"] == 0
        assert data["pct"] is None

    def _walkup_recap(self, *, fields, event_type_name="Retail Sampling", approved=True):
        when = timezone.make_aware(
            datetime(self.today.year, self.today.month, self.today.day, 12, 0)
        )
        et = event_models.EventType.objects.create(
            name=event_type_name,
            slug=f"walkup-{event_type_name.lower().replace(' ', '-')}-{len(fields)}-{approved}",
            tenant=self.tenant,
            created_by=self.sys,
        )
        event = event_models.Event.objects.create(
            name="walk-in",
            tenant=self.tenant,
            address="9 Walk St",
            date=when,
            event_type=et,
            created_by=self.sys,
            updated_by=self.sys,
        )
        recap = recap_models.CustomRecap.objects.create(
            name="walk-in recap",
            approved=approved,
            event=event,
            tenant=self.tenant,
            custom_recap_template=self.template,
            created_by=self.sys,
        )
        for name, value in fields:
            field = recap_models.CustomField.objects.create(
                name=name,
                custom_recap_template=self.template,
                custom_field_type=self.field_type,
                recap_section=self.section,
                created_by=self.sys,
            )
            recap_models.CustomFieldValue.objects.create(
                custom_recap=recap, custom_field=field, value=value, created_by=self.sys
            )
        return recap

    def test_walkup_recap_without_request_counts_by_event_type(self):
        # Standing walk-up events have no Request; their Event type says Retail.
        self._walkup_recap(
            fields=[
                ("Total number of consumers sampled", "40"),
                ("How many single cans did consumers purchase?", "6"),
                ("How many packs did consumers purchase?", "2"),
            ]
        )
        data = tenant_conversion_kpis(self.tenant.id, start=self.start, end=self.end)
        assert data["sold"] == 8
        assert data["engagements"] == 40
        assert data["pct"] == 20.0

    def test_walkup_event_activation_still_excluded(self):
        self._walkup_recap(
            event_type_name="Event Activation",
            fields=[
                ("Total number of consumers sampled", "40"),
                ("How many single cans did consumers purchase?", "6"),
            ],
        )
        data = tenant_conversion_kpis(self.tenant.id, start=self.start, end=self.end)
        assert data["sold"] == 0
        assert data["engagements"] == 0

    def test_zero_sampled_recap_adds_no_purchases(self):
        # Dry demo: nobody sampled, a few sales — no base, so no numerator.
        self._custom_recap(
            request_type=self.retail_type,
            fields=[
                ("Total number of consumers sampled", "0"),
                ("How many packs did consumers purchase?", "17"),
            ],
        )
        self._custom_recap(
            request_type=self.retail_type,
            fields=[
                ("Total number of consumers sampled", "50"),
                ("How many packs did consumers purchase?", "10"),
            ],
        )
        data = tenant_conversion_kpis(self.tenant.id, start=self.start, end=self.end)
        assert data["sold"] == 10
        assert data["engagements"] == 50
        assert data["pct"] == 20.0

    def test_dry_demo_counts_against_people_engaged(self):
        self._walkup_recap(
            fields=[
                ("Total number of consumers sampled", "0"),
                ("How many packs did consumers purchase?", "6"),
                ("Dry demo? (no product tasted)", "Yes"),
                ("People engaged", "30"),
            ]
        )
        self._walkup_recap(
            fields=[
                ("Total number of consumers sampled", "50"),
                ("How many packs did consumers purchase?", "10"),
                ("Dry demo? (no product tasted)", "No"),
            ]
        )
        data = tenant_conversion_kpis(self.tenant.id, start=self.start, end=self.end)
        assert data["sold"] == 16
        assert data["engagements"] == 80
        assert data["pct"] == 20.0
        assert data["dry_sold"] == 6
        assert data["dry_engagements"] == 30
        assert data["dry_recaps"] == 1
        assert data["unpaired_dry_recap_ids"] == []

    def test_dry_demo_without_people_engaged_falls_back_to_sampled(self):
        self._walkup_recap(
            fields=[
                ("Total number of consumers sampled", "25"),
                ("How many packs did consumers purchase?", "5"),
                ("Dry demo? (no product tasted)", "Yes"),
            ]
        )
        data = tenant_conversion_kpis(self.tenant.id, start=self.start, end=self.end)
        assert (data["sold"], data["engagements"]) == (5, 25)
        assert data["dry_engagements"] == 25

    def test_dry_demo_with_no_base_is_listed_not_paired(self):
        recap = self._walkup_recap(
            fields=[
                ("Total number of consumers sampled", "0"),
                ("How many packs did consumers purchase?", "17"),
                ("Dry demo? (no product tasted)", "Yes"),
            ]
        )
        self._walkup_recap(
            fields=[
                ("Total number of consumers sampled", "50"),
                ("How many packs did consumers purchase?", "10"),
            ]
        )
        data = tenant_conversion_kpis(self.tenant.id, start=self.start, end=self.end)
        assert (data["sold"], data["engagements"]) == (10, 50)
        assert data["unpaired_dry_recap_ids"] == [recap.id]

    def test_dry_demo_event_activation_still_excluded(self):
        self._walkup_recap(
            event_type_name="Event Activation",
            fields=[
                ("Total number of consumers sampled", "0"),
                ("How many packs did consumers purchase?", "9"),
                ("Dry demo? (no product tasted)", "Yes"),
                ("People engaged", "40"),
            ],
        )
        data = tenant_conversion_kpis(self.tenant.id, start=self.start, end=self.end)
        assert (data["sold"], data["engagements"], data["dry_recaps"]) == (0, 0, 0)

    def test_archived_recap_excluded(self):
        recap = self._custom_recap(
            request_type=self.retail_type,
            fields=[
                ("Total number of consumers sampled", "50"),
                ("How many packs did consumers purchase?", "10"),
            ],
        )
        recap.archived_at = timezone.now()
        recap.save(update_fields=["archived_at"])
        data = tenant_conversion_kpis(self.tenant.id, start=self.start, end=self.end)
        assert data["sold"] == 0
        assert data["engagements"] == 0

    def test_monthly_trend_sales_series_reconciles_with_conversion(self):
        this_month = timezone.make_aware(
            datetime(self.today.year, self.today.month, 1, 12, 0)
        )
        last_month = this_month - timedelta(days=10)
        self._custom_recap(
            request_type=self.retail_type,
            when=last_month,
            fields=[
                ("Total number of consumers sampled", "40"),
                ("How many single cans did consumers purchase?", "6"),
                ("How many packs did consumers purchase?", "2"),
            ],
        )
        self._custom_recap(
            request_type=self.retail_type,
            when=this_month,
            fields=[
                ("Total number of consumers sampled", "0"),
                ("How many packs did consumers purchase?", "3"),
                ("Dry demo? (no product tasted)", "Yes"),
                ("People engaged", "25"),
            ],
        )
        self._custom_recap(
            request_type=self.event_type,
            when=this_month,
            fields=[
                ("Total number of consumers sampled", "500"),
                ("How many packs did consumers purchase?", "90"),
            ],
        )

        trend = tenant_monthly_trend(self.tenant.id, sales=True)
        by_month = {m.month: m for m in trend}
        last_key = f"{last_month.year:04d}-{last_month.month:02d}"
        this_key = f"{this_month.year:04d}-{this_month.month:02d}"
        assert (by_month[last_key].consumers_sampled, by_month[last_key].units_sold) == (40, 8)
        assert (by_month[this_key].consumers_sampled, by_month[this_key].units_sold) == (25, 3)

        totals = conversion_window_totals(self.tenant.id, None, None)
        assert sum(m.consumers_sampled for m in trend) == totals["base"] == 65
        assert sum(m.units_sold for m in trend) == totals["sold"] == 11

        activity = tenant_monthly_trend(self.tenant.id)
        assert all(m.consumers_sampled == 0 and m.units_sold == 0 for m in activity)

    def test_tenant_kpis_trend_series_follow_tenant_setting(self):
        from recaps.report_types import _build_tenant_kpis
        from tenants.models import Tenant

        kpis = _build_tenant_kpis(self.tenant.id)
        assert [s.key for s in kpis.trend_series] == ["engagements", "samples"]
        assert kpis.trend_note is None

        Tenant.objects.filter(id=self.tenant.id).update(
            insights_trend_series=Tenant.TREND_SERIES_SALES
        )
        kpis = _build_tenant_kpis(self.tenant.id)
        assert [(s.key, s.label) for s in kpis.trend_series] == [
            ("consumersSampled", "Consumers sampled"),
            ("unitsSold", "Units sold"),
        ]
        assert "People engaged" in kpis.trend_note

    def _seed_sales_program(self, when=None):
        self._custom_recap(
            request_type=self.retail_type,
            when=when,
            fields=[
                ("Total number of consumers sampled", "40"),
                ("How many single cans did consumers purchase?", "6"),
                ("How many packs did consumers purchase?", "2"),
                ("First Time consumers?", "10"),
                ("How many consumers that were engaged with knew about Torch THC product/brand?", "5"),
                ("How many consumers would be willing to purchase the product after tasing it?", "20"),
                ("How many consumers would NOT be willing to purchase the product after tasing it?", "9"),
            ],
        )
        self._custom_recap(
            request_type=self.retail_type,
            when=when,
            fields=[
                ("Total number of consumers sampled", "0"),
                ("How many packs did consumers purchase?", "3"),
                ("Dry demo? (no product tasted)", "Yes"),
                ("People engaged", "25"),
                ("First Time consumers?", "4"),
            ],
        )
        self._custom_recap(
            request_type=self.event_type,
            when=when,
            fields=[
                ("Total number of consumers sampled", "500"),
                ("How many packs did consumers purchase?", "90"),
                ("First Time consumers?", "300"),
            ],
        )
        self._custom_recap(
            request_type=self.retail_type,
            when=when,
            fields=[("How many single cans did consumers purchase?", "1")],
        )

    def _make_sales_tenant(self):
        from tenants.models import Tenant

        Tenant.objects.filter(id=self.tenant.id).update(
            insights_trend_series=Tenant.TREND_SERIES_SALES
        )

    def test_sales_program_metrics_match_conversion_set(self):
        self._seed_sales_program()
        m = sales_program_metrics(self.tenant.id, self.start, self.end)
        conv = tenant_conversion_kpis(self.tenant.id, start=self.start, end=self.end)
        assert (m["consumers_sampled"], m["units_sold"]) == (65, 11)
        assert (conv["engagements"], conv["sold"]) == (65, 11)
        assert (m["cans_sold"], m["packs_sold"]) == (6, 5)
        assert (m["recaps"], m["demos"], m["dry_demos"]) == (2, 2, 1)
        assert m["dry_people_engaged"] == 25
        assert m["conversion_pct"] == 16.9
        assert m["first_time_consumers"] == 14
        assert m["brand_aware_consumers"] == 5
        assert m["willing_to_purchase"] == 20

    def test_sales_tenant_insight_cards_drop_engagements(self):
        from recaps.tenant_insights import build_insight_buckets_scoped

        self._seed_sales_program()
        self._make_sales_tenant()
        items, label = build_insight_buckets_scoped(
            self.tenant.id, self.start, self.end
        )
        by = {b["key"]: b for b in items}
        assert by["reach"]["metric"] == "65"
        assert "across 2 demos" in by["reach"]["detail"]
        assert "1 dry demo counted by People engaged (25)" in by["reach"]["detail"]
        assert by["sales"]["title"] == "Units sold"
        assert by["sales"]["metric"] == "11"
        assert "6 single cans · 5 packs" in by["sales"]["detail"]
        assert "16.9% conversion" in by["sales"]["detail"]
        assert by["new_audience"]["metric"] == "14"
        blob = " ".join(f"{b['title']} {b['metric']} {b['detail']}" for b in items)
        assert "engagement" not in blob.lower()
        assert label == f"{self.start} → {self.end}"

    def test_activity_tenant_insight_cards_unscoped(self):
        from recaps.tenant_insights import build_insight_buckets_scoped

        self._seed_sales_program()
        items, label = build_insight_buckets_scoped(
            self.tenant.id, self.start, self.end
        )
        assert label is None
        assert next(b for b in items if b["key"] == "sales")["title"] == "Sales"

    def test_program_kpis_and_comparison_use_sales_metrics(self):
        from recaps.report_types import _build_tenant_kpis
        from recaps.tenant_overview import tenant_kpi_comparison

        self._make_sales_tenant()
        last_month_end = self.today.replace(day=1) - timedelta(days=1)
        self._seed_sales_program(
            when=timezone.make_aware(
                datetime(last_month_end.year, last_month_end.month, 10, 12, 0)
            )
        )
        kpis = _build_tenant_kpis(self.tenant.id)
        assert kpis.program is not None
        assert (kpis.program.consumers_sampled, kpis.program.units_sold) == (65, 11)

        data = tenant_kpi_comparison(self.tenant.id, "month")
        assert data["current"]["program"]["consumers_sampled"] == 65
        assert data["previous"]["program"]["recaps"] == 0
        assert data["sparse_note"] == f"{data['previous_label']} had only 0 recaps — % changes hidden."

    def test_activity_comparison_has_no_program(self):
        from recaps.tenant_overview import tenant_kpi_comparison

        data = tenant_kpi_comparison(self.tenant.id, "month")
        assert "program" not in data["current"]
        assert data["sparse_note"] is not None

    def test_sales_momentum_hides_pct_on_thin_base(self):
        from recaps.tenant_insights import _sales_momentum_bucket

        today = self.today.replace(day=2)
        self._custom_recap(
            request_type=self.retail_type,
            when=timezone.make_aware(datetime(today.year, today.month, 1, 12, 0)),
            fields=[
                ("Total number of consumers sampled", "40"),
                ("How many packs did consumers purchase?", "10"),
            ],
        )
        b = _sales_momentum_bucket(self.tenant.id, today=today)
        prev = today.replace(day=1) - timedelta(days=1)
        assert b["metric"].startswith("n/a vs ")
        assert "month to date" in b["detail"]
        assert "% hidden" in b["detail"]
        assert "engagement" not in b["detail"].lower()
        assert prev.strftime("%b") in b["metric"]

    def test_sales_momentum_compares_same_days(self, monkeypatch):
        from recaps import tenant_insights

        monkeypatch.setattr(tenant_insights, "SPARSE_BASE_MIN_RECAPS", 1)
        today = self.today.replace(day=2)
        prev_first = (today.replace(day=1) - timedelta(days=1)).replace(day=1)
        for when, sampled, packs in (
            (datetime(today.year, today.month, 1, 12, 0), "50", "10"),
            (datetime(prev_first.year, prev_first.month, 1, 12, 0), "40", "8"),
            (datetime(prev_first.year, prev_first.month, 20, 12, 0), "900", "300"),
        ):
            self._custom_recap(
                request_type=self.retail_type,
                when=timezone.make_aware(when),
                fields=[
                    ("Total number of consumers sampled", sampled),
                    ("How many packs did consumers purchase?", packs),
                ],
            )
        b = tenant_insights._sales_momentum_bucket(self.tenant.id, today=today)
        assert b["metric"].startswith("▲ 25% vs ")
        assert "10 units sold" in b["detail"]
        assert "vs 8 " in b["detail"]
        assert "conversion 20.0% vs 20.0% (+0.0 pts)" in b["detail"]


def test_sold_units_count_each_can_and_pack_as_one_unit():
    from recaps.types import _sold_unit_lines, _sold_units_from_fields

    pairs = [
        ("How many single cans did consumers purchase?", "3"),
        ("How many packs did consumers purchase?", "2"),
        ("Peach 4-packs sold", "1"),
        ("Account Spend Amount", "120"),
        ("Total number of consumers sampled", "40"),
    ]
    assert _sold_units_from_fields(pairs) == 6
    assert [n for _, n in _sold_unit_lines(pairs)] == [3, 2, 1]
    assert _sold_units_from_fields([("How many packs did consumers purchase?", "2")]) == 2
    assert _sold_units_from_fields([("Account Spend Amount", "120")]) is None
