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
from recaps.recap_sheet_export import (
    META_HEADER,
    build_export_grid,
    rows_for_header,
)


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

    def test_rows_for_header_maps_into_existing_layout(self):
        # Mimic the client's existing "Demo Recaps" header (meta columns +
        # field-name columns + an unmatched "Note" column). rows_for_header
        # must map values into those exact positions, leaving row 1 alone.
        self._recap(
            7,
            [
                (self.samples, "120"),
                (self.market, '["Detroit", "Lansing"]'),
            ],
        )
        header = [
            "Brand Ambassador",
            "Date",
            "Retailer",
            "Total Samples Given Out",
            "What market is this?",
            "Note",
        ]
        rows = rows_for_header(self.tenant, header)
        assert len(rows) == 1
        row = rows[0]
        assert len(row) == len(header)
        # Date column rendered MM/DD/YYYY.
        assert row[1].count("/") == 2
        # Field columns match by name (multiselect → comma list).
        assert row[3] == "120"
        assert row[4] == "Detroit, Lansing"
        # Unmatched column stays blank — we never invent data.
        assert row[5] == ""

    # ── Retailer inference (fallback when the FK chain is unset) ─────────

    def _retailer(self, name):
        from events.models import Retailer

        return Retailer.objects.create(
            tenant=self.tenant, name=name, created_by=self.system_user
        )

    def test_retailer_inferred_from_event_name_when_fk_unset(self):
        # Girl Beer events created off the schedule sheet carry the store in
        # the event NAME ("Whole Foods Los Angeles · 2026-06-21") but no
        # Retailer FK anywhere — the column must still populate by matching
        # the tenant's known Retailer names against that text.
        self._retailer("Whole Foods")
        event = self.create_event(
            name="Whole Foods Los Angeles · 2026-06-21",
            tenant=self.tenant,
            date=self.now,
        )
        recap_models.CustomRecap.objects.create(
            name="wf recap", approved=True, event=event, tenant=self.tenant,
            custom_recap_template=self.template,
            created_by=self.system_user, updated_by=self.system_user,
            submitted_at=self.now,
        )
        rows = rows_for_header(self.tenant, ["Retailer", "Store/Location"])
        assert rows[0][0] == "Whole Foods"

    def test_retailer_fk_wins_over_inference(self):
        vons = self._retailer("Vons")
        self._retailer("Whole Foods")
        event = self.create_event(
            name="Whole Foods Fullerton · 2026-06-23",
            tenant=self.tenant,
            date=self.now,
        )
        recap = recap_models.CustomRecap.objects.create(
            name="fk recap", approved=True, event=event, tenant=self.tenant,
            custom_recap_template=self.template,
            created_by=self.system_user, updated_by=self.system_user,
            submitted_at=self.now,
        )
        recap.retailer = vons
        recap.save(update_fields=["retailer"])
        rows = rows_for_header(self.tenant, ["Retailer"])
        assert rows[0][0] == "Vons"

    def test_retailer_blank_when_nothing_matches(self):
        # No known retailer appears in the text (a bare city), and partial
        # word overlaps must not match ("Vons" inside "Avonsdale").
        self._retailer("Vons")
        event = self.create_event(
            name="Avonsdale Market · 2026-06-29",
            tenant=self.tenant,
            date=self.now,
        )
        recap_models.CustomRecap.objects.create(
            name="city recap", approved=True, event=event, tenant=self.tenant,
            custom_recap_template=self.template,
            created_by=self.system_user, updated_by=self.system_user,
            submitted_at=self.now,
        )
        rows = rows_for_header(self.tenant, ["Retailer"])
        assert rows[0][0] == ""

    # ── Which recaps belong in the client's sheet ───────────────────────

    def _names(self):
        header, rows = build_export_grid(self.tenant)
        col = header.index("Event")
        grid = sorted(row[col] for row in rows)
        mapped = sorted(row[0] for row in rows_for_header(self.tenant, ["Store/Location"]))
        assert len(mapped) == len(grid)  # both builders read the same recaps
        return grid

    def test_archived_recaps_are_left_out(self):
        self._recap(1, [(self.samples, "10")])
        hidden = self._recap(2, [(self.samples, "20")])
        hidden.archived_at = self.now
        hidden.save(update_fields=["archived_at"])
        assert self._names() == ["Event 001"]

    def test_empty_clock_out_stub_is_left_out(self):
        self._recap(1, [(self.samples, "10")])
        self._recap(2, [])  # the blank row a clock-out creates
        assert self._names() == ["Event 001"]

    def test_recap_on_a_deleted_request_is_left_out_but_live_and_standalone_stay(self):
        from events import models as event_models

        status = self.create_request_status(name="Approved", tenant=self.tenant)
        request_type = self.create_request_type(name="Retail", tenant=self.tenant)

        def _request(name, deleted):
            return event_models.Request.objects.create(
                name=name,
                address="1 Main St",
                tenant=self.tenant,
                status=status,
                request_type=request_type,
                deleted_at=self.now if deleted else None,
                created_by=None,
            )

        standalone = self._recap(1, [(self.samples, "10")])
        assert standalone.event.request_id is None
        live = self._recap(2, [(self.samples, "20")])
        gone = self._recap(3, [(self.samples, "30")])
        event_models.Event.objects.filter(id=live.event_id).update(
            request=_request("Live demo", deleted=False)
        )
        event_models.Event.objects.filter(id=gone.event_id).update(
            request=_request("Deleted demo", deleted=True)
        )
        assert self._names() == ["Event 001", "Event 002"]

    def test_unapproved_and_excluded_from_aggregates_recaps_stay(self):
        # The sheet has a Status column, so a submitted-but-unapproved recap
        # is meant to be there; exclude_from_aggregates only affects KPIs.
        pending = self._recap(1, [(self.samples, "10")])
        recap_models.CustomRecap.objects.filter(id=pending.id).update(approved=False)
        flagged = self._recap(2, [(self.samples, "20")])
        recap_models.CustomRecap.objects.filter(id=flagged.id).update(
            exclude_from_aggregates=True
        )
        header, rows = build_export_grid(self.tenant)
        by_event = {row[header.index("Event")]: row[header.index("Status")] for row in rows}
        assert by_event == {"Event 001": "Submitted", "Event 002": "Approved"}
