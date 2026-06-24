"""Coverage for the recap → Google Sheet export grid builder.

`build_export_grid(tenant)` flattens a tenant's recaps into a header row +
one row per recap (event/BA metadata, then one column per custom-template
field). This pins:
  * column order follows section.order then field.order,
  * image/photo fields are excluded (they hold a blob path, not demo data),
  * a multiselect answer (JSON array) renders as a comma list,
  * each recap's values land under the right columns, blanks where unanswered.

The Sheets I/O (write_grid_to_sheet) is a thin wrapper over the Google client
and isn't exercised here — the grid building is the logic worth pinning.
"""
from __future__ import annotations

from datetime import timedelta

import pytest
from django.utils import timezone

from ambassadors.tests.base import AmbassadorsGraphQLTestCase
from recaps import models as recap_models
from recaps.recap_sheet_export import META_HEADER, build_export_grid


@pytest.mark.django_db(transaction=True)
class TestRecapSheetExportGrid(AmbassadorsGraphQLTestCase):
    @pytest.fixture(autouse=True)
    def setup(self, db):
        self.system_user = self.get_system_user()
        self.now = timezone.now()
        self.tenant = self.create_tenant(name="GB Export")
        self.event_type = self.create_event_type(name="Retail Sampling", tenant=self.tenant)

        self.number_type = recap_models.CustomRecapFieldType.objects.create(
            name="number", created_by=self.system_user
        )
        self.multiselect_type = recap_models.CustomRecapFieldType.objects.create(
            name="multiselect", created_by=self.system_user
        )
        self.image_type = recap_models.CustomRecapFieldType.objects.create(
            name="image", created_by=self.system_user
        )

        self.template = recap_models.CustomRecapTemplate.objects.create(
            name="GB Recap",
            event_type=self.event_type,
            tenant=self.tenant,
            created_by=self.system_user,
        )
        # Two sections, explicit order so the column order is deterministic.
        self.sales = recap_models.RecapSection.objects.create(
            name="Sales Figures", tenant=self.tenant, created_by=self.system_user, order=1
        )
        self.demo = recap_models.RecapSection.objects.create(
            name="Demographics — Sampled", tenant=self.tenant, created_by=self.system_user, order=2
        )

        self.samples = self._field("Total Samples Given Out", self.number_type, self.sales, 1)
        # An image field in the Sales section — must be EXCLUDED from columns.
        self._field("Table setup pictures", self.image_type, self.sales, 2)
        self.men = self._field("Men who sampled (21-29)", self.number_type, self.demo, 1)
        self.market = self._field("What market is this?", self.multiselect_type, self.demo, 2)

    def _field(self, name, field_type, section, order):
        return recap_models.CustomField.objects.create(
            name=name,
            custom_recap_template=self.template,
            custom_field_type=field_type,
            recap_section=section,
            created_by=self.system_user,
            order=order,
        )

    def _recap(self, idx, values):
        event = self.create_event(
            name=f"Event {idx:03d}", tenant=self.tenant, date=self.now + timedelta(days=idx)
        )
        recap = recap_models.CustomRecap.objects.create(
            name=f"recap {idx:03d}",
            approved=True,
            event=event,
            tenant=self.tenant,
            custom_recap_template=self.template,
            created_by=self.system_user,
            updated_by=self.system_user,
        )
        for field, value in values:
            recap_models.CustomFieldValue.objects.create(
                value=value, custom_recap=recap, custom_field=field, created_by=self.system_user
            )
        return recap

    def test_header_orders_columns_and_excludes_images(self):
        header, rows = build_export_grid(self.tenant)
        assert header[: len(META_HEADER)] == META_HEADER
        # Field columns: section.order then field.order, image field dropped.
        assert header[len(META_HEADER):] == [
            "Total Samples Given Out",
            "Men who sampled (21-29)",
            "What market is this?",
        ]
        assert rows == []  # no recaps yet

    def test_recap_row_values_align_and_multiselect_formats(self):
        self._recap(
            1,
            [
                (self.samples, "120"),
                (self.men, "5"),
                (self.market, '["Detroit", "Lansing"]'),
            ],
        )
        header, rows = build_export_grid(self.tenant)
        assert len(rows) == 1
        row = rows[0]
        assert len(row) == len(header)

        col = {name: row[i] for i, name in enumerate(header)}
        assert col["Total Samples Given Out"] == "120"
        assert col["Men who sampled (21-29)"] == "5"
        # Multiselect JSON → readable comma list.
        assert col["What market is this?"] == "Detroit, Lansing"
        # Metadata columns populated.
        assert col["Status"] == "Approved"
        assert col["Event"] == "Event 001"

    def test_unanswered_field_is_blank(self):
        # Only fill samples; the other two columns must be empty strings.
        self._recap(2, [(self.samples, "99")])
        header, rows = build_export_grid(self.tenant)
        col = {name: rows[0][i] for i, name in enumerate(header)}
        assert col["Total Samples Given Out"] == "99"
        assert col["Men who sampled (21-29)"] == ""
        assert col["What market is this?"] == ""
