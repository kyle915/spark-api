"""Coverage for the Liquid Death Summary rebuild aggregation.

compute_ld_summary() rolls the tenant's CustomRecaps into KPI totals + buckets
by RMM (state→RMM via the territory map), state, month, and BA, reusing
report_service._accumulate_custom for the per-recap numbers. This pins the
aggregation + RMM attribution; the underlying matcher is report_service's job.
"""
from __future__ import annotations

from datetime import timedelta
from unittest.mock import MagicMock

import pytest
from django.utils import timezone

from ambassadors.tests.base import AmbassadorsGraphQLTestCase
from events.models import State
from recaps import models as recap_models
from recaps.ld_summary_export import (
    LdSummary,
    _num,
    build_summary_grid,
    compute_ld_summary,
    read_recaps_tab_by_year,
)


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


def test_num_parses_recap_numeric_cells():
    assert _num("70") == 70
    assert _num("1,234") == 1234
    assert _num("-") == 0
    assert _num("N/A") == 0
    assert _num("") == 0
    assert _num(None) == 0
    assert _num("12 cases") == 12


def _recaps_tab_svc(rows):
    """svc whose values().get() returns the given RECAPS-tab data rows."""
    svc = MagicMock()
    svc.spreadsheets.return_value.values.return_value.get.return_value.execute.return_value = {
        "values": rows
    }
    return svc


def _rc_row(date, consumers, cans, packs):
    """A 17-col RECAPS row: date=col2, consumers=col10, cans=col15, packs=col16."""
    row = [""] * 17
    row[2] = date
    row[10] = str(consumers)
    row[15] = str(cans)
    row[16] = str(packs)
    return row


def test_read_recaps_tab_by_year_aggregates():
    svc = _recaps_tab_svc([
        _rc_row("01/11/2025", 70, 8, 10),
        _rc_row("02/02/2025", 30, 2, 0),
        _rc_row("05/05/2026", 100, 10, 5),
        _rc_row("", 999, 9, 9),          # no date → skipped
    ])
    by_year = read_recaps_tab_by_year(svc, "sid", tab="RECAPS")
    assert set(by_year.keys()) == {"2025", "2026"}
    assert by_year["2025"].demos == 2
    assert by_year["2025"].consumers == 100
    assert by_year["2025"].cans == 10
    assert by_year["2025"].packs == 10
    assert by_year["2026"].demos == 1
    assert by_year["2026"].consumers == 100


def test_build_grid_program_matches_app():
    from recaps.ld_summary_export import ProgramKpis

    program_all = ProgramKpis(
        events_run=866, consumers=148687, brand_aware=71200, willing=63936,
        single_cans=7169, multi_packs=4936, pack_cans_equiv=59232,
    )
    program_years = {
        "2026": ProgramKpis(
            events_run=865, consumers=148617, single_cans=7169,
            multi_packs=4936, pack_cans_equiv=59232,
        ),
        "2025": ProgramKpis(events_run=1, consumers=70),
    }
    grid, layout = build_summary_grid(
        LdSummary(total_demos=47), program_all=program_all, program_years=program_years
    )
    flat = [str(c) for row in grid for c in row]
    assert "EVENTS RUN" in flat
    assert "PERFORMANCE BY YEAR" in flat
    assert any("SPARK APP-RECAP DETAIL" in c for c in flat)
    assert layout["kpi_cols"] == 6
    # KPI value row: events run + cans-sold-total (single + pack-equivalent).
    kpi = grid[layout["kpi_value"]]
    assert kpi[0] == 866
    assert kpi[1] == 148687
    assert kpi[2] == 66401  # 7169 single + 59232 pack-equivalent
    assert kpi[3] == 4936
    # Brand awareness % rendered.
    assert any(c.endswith("%") for c in kpi if isinstance(c, str))


def test_program_kpis_cans_and_pct_properties():
    from recaps.ld_summary_export import ProgramKpis

    p = ProgramKpis(consumers=100, brand_aware=48, willing=43, single_cans=10, pack_cans_equiv=120)
    assert p.cans_sold_total == 130
    assert round(p.brand_awareness_pct, 1) == 48.0
    assert round(p.purchase_intent_pct, 1) == 43.0


def test_build_grid_breakdowns_use_full_dataset_not_47_slice():
    from recaps.ld_summary_export import ProgramKpis

    program_all = ProgramKpis(events_run=866, consumers=148687, single_cans=7169,
                              multi_packs=4936, pack_cans_equiv=59232)
    program_years = {"2026": ProgramKpis(events_run=866, consumers=148687)}
    breakdowns = {
        "by_rmm": {"Kristyn": {"events": 250, "consumers": 40000, "cans": 2000, "packs": 1500}},
        "by_state": {"CA": {"events": 200, "consumers": 35000}},
        "by_ba": {"Alice": {"events": 30, "consumers": 5000}},
    }
    grid, layout = build_summary_grid(
        LdSummary(total_demos=47), program_all=program_all,
        program_years=program_years, breakdowns=breakdowns,
    )
    flat = [str(c) for row in grid for c in row]
    assert "PERFORMANCE BY RMM" in flat
    assert "Kristyn" in flat
    assert "40000" in flat  # full-dataset consumers, not the 2,075 slice
    assert "35000" in flat  # by-state consumers
    # The "47 custom-template recaps" caveat banner is suppressed for full data.
    assert not any("custom-template recaps" in c for c in flat)
