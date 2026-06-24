"""Coverage for the Liquid Death Summary rebuild aggregation.

compute_ld_summary() rolls the tenant's CustomRecaps into KPI totals + buckets
by RMM (state→RMM via the territory map), state, month, and BA, reusing
report_service._accumulate_custom for the per-recap numbers. This pins the
aggregation + RMM attribution; the underlying matcher is report_service's job.
"""
from __future__ import annotations

from datetime import timedelta

import pytest
from django.utils import timezone

from ambassadors.tests.base import AmbassadorsGraphQLTestCase
from events.models import State
from recaps import models as recap_models
from recaps.ld_summary_export import build_summary_grid, compute_ld_summary


@pytest.mark.django_db(transaction=True)
class TestLdSummary(AmbassadorsGraphQLTestCase):
    @pytest.fixture(autouse=True)
    def setup(self, db):
        self.system_user = self.get_system_user()
        self.now = timezone.now()
        self.tenant = self.create_tenant(name="Liquid Death")
        self.event_type = self.create_event_type(name="Retail Sampling", tenant=self.tenant)
        self.ftype = recap_models.CustomRecapFieldType.objects.create(
            name="number", created_by=self.system_user
        )
        self.template = recap_models.CustomRecapTemplate.objects.create(
            name="LD Recap", event_type=self.event_type, tenant=self.tenant,
            created_by=self.system_user,
        )
        self.section = recap_models.RecapSection.objects.create(
            name="Numbers", tenant=self.tenant, created_by=self.system_user
        )
        self.f_consumers = self._field("# of Consumers Sampled")
        self.f_cans = self._field("# of Single Cans Sold")
        self.f_packs = self._field("# of Multi Packs Sold")
        self.f_willing = self._field("Willing to purchase")
        self.ca = State.objects.create(code="CA", name="California", created_by=self.system_user)
        self.ny = State.objects.create(code="NY", name="New York", created_by=self.system_user)

    def _field(self, name):
        return recap_models.CustomField.objects.create(
            name=name, custom_recap_template=self.template,
            custom_field_type=self.ftype, recap_section=self.section,
            created_by=self.system_user,
        )

    def _recap(self, idx, state, ba, *, consumers, cans, packs, willing, days=0):
        event = self.create_event(
            name=f"Store {idx}", tenant=self.tenant, date=self.now + timedelta(days=days)
        )
        recap = recap_models.CustomRecap.objects.create(
            name=f"recap {idx}", approved=True, event=event, tenant=self.tenant,
            state=state, external_ba_name=ba,
            custom_recap_template=self.template,
            created_by=self.system_user, updated_by=self.system_user,
        )
        for field, value in (
            (self.f_consumers, consumers), (self.f_cans, cans),
            (self.f_packs, packs), (self.f_willing, willing),
        ):
            recap_models.CustomFieldValue.objects.create(
                value=str(value), custom_recap=recap, custom_field=field,
                created_by=self.system_user,
            )
        return recap

    def test_totals_and_rmm_state_attribution(self):
        # 2 CA recaps (Kristyn) + 1 NY recap (Lauren).
        self._recap(1, self.ca, "Alice", consumers=100, cans=10, packs=5, willing=20)
        self._recap(2, self.ca, "Bob", consumers=50, cans=4, packs=2, willing=10)
        self._recap(3, self.ny, "Alice", consumers=80, cans=8, packs=3, willing=24, days=40)

        s = compute_ld_summary(self.tenant)
        assert s.total_demos == 3
        assert s.consumers == 230
        assert s.cans == 22
        assert s.packs == 10
        assert s.willing == 54
        # Conversion = (cans + packs) sold / consumers sampled.
        assert round(s.conversion_pct, 1) == round((22 + 10) / 230 * 100, 1)

        # RMM attribution by state.
        assert s.by_rmm["Kristyn"].demos == 2
        assert s.by_rmm["Kristyn"].consumers == 150
        assert s.by_rmm["Lauren"].demos == 1
        assert "Unassigned" not in s.by_rmm

        # By state / BA.
        assert s.by_state["CA"] == 2
        assert s.by_state["NY"] == 1
        assert s.by_ba["Alice"] == 2
        assert s.by_ba["Bob"] == 1

    def test_unmapped_state_buckets_unassigned(self):
        wy_unmapped = State.objects.create(code="ZZ", name="Nowhere", created_by=self.system_user)
        self._recap(9, wy_unmapped, "Carol", consumers=10, cans=1, packs=1, willing=2)
        s = compute_ld_summary(self.tenant)
        assert s.by_rmm["Unassigned"].demos == 1

    def test_grid_is_branded_and_has_sections(self):
        self._recap(1, self.ca, "Alice", consumers=100, cans=10, packs=5, willing=20)
        grid, layout = build_summary_grid(compute_ld_summary(self.tenant))
        flat = [str(c) for row in grid for c in row]
        assert any("LIQUID DEATH" in c for c in flat)
        assert "PERFORMANCE BY RMM" in flat
        assert "PERFORMANCE BY STATE" in flat
        assert "PERFORMANCE BY BRAND AMBASSADOR" in flat
        assert "Kristyn" in flat  # the CA recap's RMM row
        # Layout drives formatting: 4 section headers + their table headers.
        assert len(layout["sections"]) == 4
        assert len(layout["table_headers"]) == 4
        # Format requests build without error and reference the sheet id.
        from recaps.ld_summary_export import summary_format_requests

        reqs = summary_format_requests(123, layout)
        assert reqs and all(
            isinstance(r, dict) for r in reqs
        )
