"""Coverage for the Liquid Death raw-recaps export (branded "Spark Recaps").

build_ld_recaps_grid turns the tenant's CustomRecaps into a branded grid: a
title bar, a header row (event/BA metadata + one column per distinct recap
field), and one row per recap. This pins the header shape, the per-recap value
mapping (including duplicate-name dedupe), and the optional year filter.
"""
from __future__ import annotations

from datetime import timedelta

import pytest
from django.utils import timezone

from ambassadors.tests.base import AmbassadorsGraphQLTestCase
from events.models import State
from recaps import models as recap_models
from recaps.ld_recaps_export import (
    META_COLUMNS,
    build_ld_recaps_grid,
    ld_recaps_format_requests,
)


@pytest.mark.django_db(transaction=True)
class TestLdRecapsExport(AmbassadorsGraphQLTestCase):
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
        self.f_consumers = self._field("Total number of consumers sampled")
        self.f_cans = self._field("How many single cans did consumers purchase?")
        self.ca = State.objects.create(code="CA", name="California", created_by=self.system_user)

    def _field(self, name):
        return recap_models.CustomField.objects.create(
            name=name, custom_recap_template=self.template,
            custom_field_type=self.ftype, recap_section=self.section,
            created_by=self.system_user,
        )

    def _recap(self, idx, ba, *, consumers, cans, days=0):
        event = self.create_event(
            name=f"Store {idx}", tenant=self.tenant, date=self.now + timedelta(days=days)
        )
        recap = recap_models.CustomRecap.objects.create(
            name=f"recap {idx}", approved=True, event=event, tenant=self.tenant,
            state=self.ca, external_ba_name=ba,
            custom_recap_template=self.template,
            created_by=self.system_user, updated_by=self.system_user,
        )
        for field, value in ((self.f_consumers, consumers), (self.f_cans, cans)):
            recap_models.CustomFieldValue.objects.create(
                value=str(value), custom_recap=recap, custom_field=field,
                created_by=self.system_user,
            )
        return recap

    def test_grid_header_and_row_mapping(self):
        self._recap(1, "Alice", consumers=100, cans=10)
        grid, layout, ncols = build_ld_recaps_grid(self.tenant)

        # Title + subtitle + header, then one data row.
        assert "LIQUID DEATH" in grid[0][0]
        header = grid[layout["header"]]
        assert header[: len(META_COLUMNS)] == META_COLUMNS
        assert "Total number of consumers sampled" in header
        assert "How many single cans did consumers purchase?" in header
        assert ncols == len(header)

        data = grid[layout["header"] + 1]
        # BA meta column (index 1) + the two field values land in their columns.
        assert data[1] == "Alice"
        ci = header.index("Total number of consumers sampled")
        assert str(data[ci]) == "100"

        # Format requests build and reference the gid.
        reqs = ld_recaps_format_requests(7, layout)
        assert reqs and all(isinstance(r, dict) for r in reqs)

    def test_year_filter(self):
        self._recap(1, "Alice", consumers=10, cans=1, days=0)        # this year
        self._recap(2, "Bob", consumers=20, cans=2, days=400)        # next year
        this_year = self.now.year
        grid, layout, _ = build_ld_recaps_grid(self.tenant, year=this_year)
        data_rows = grid[layout["header"] + 1:]
        assert len(data_rows) == 1
        assert data_rows[0][1] == "Alice"
