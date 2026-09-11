"""
Coverage for the month-end expense receipts export
(recaps/receipts_export.py — the exportExpenseReceipts mutation's core).

Pins: money parsing, the per-recap collection rules (receipt-category
files OR a spend field; legacy account_spend_amount rows; event-date
windowing with created_at fallback; tenant scoping), the CSV shape, and
a PDF smoke test (bytes render even with zero images).
"""

from datetime import date, datetime, timedelta, timezone as _tz

import pytest

from ambassadors.tests.base import AmbassadorsGraphQLTestCase
from recaps import models as recap_models
from recaps.receipts_export import (
    _parse_money,
    _recorded_total,
    apply_amount_overrides,
    build_expense_rows_csv,
    build_receipts_bundle_pdf,
    collect_expense_rows,
)


# ---------------------------------------------------------------------------
# _parse_money
# ---------------------------------------------------------------------------


def test_parse_money_plain_and_decimal():
    assert _parse_money("43.20") == 43.20
    assert _parse_money("60") == 60.0


def test_parse_money_currency_and_commas():
    assert _parse_money("$1,234.50") == 1234.50


def test_parse_money_embedded_words():
    assert _parse_money("spent about 87 dollars") == 87.0


def test_parse_money_garbage_is_none():
    assert _parse_money("n/a") is None
    assert _parse_money("") is None
    assert _parse_money(None) is None


# ---------------------------------------------------------------------------
# collect_expense_rows
# ---------------------------------------------------------------------------


@pytest.mark.django_db(transaction=True)
class TestCollectExpenseRows(AmbassadorsGraphQLTestCase):
    @pytest.fixture(autouse=True)
    def setup(self, db):
        self.roles = self.setup_default_roles()
        self.system_user = self.get_system_user()
        self.tenant = self.create_tenant(name="Girl Beer")
        self.other_tenant = self.create_tenant(name="Liquid Death")

        self.start = date(2026, 5, 1)
        self.end = date(2026, 5, 31)
        self.in_range = datetime(2026, 5, 14, 18, 0, tzinfo=_tz.utc)

        event_type = self.create_event_type(
            name="Sampling", tenant=self.tenant
        )
        self.template = recap_models.CustomRecapTemplate.objects.create(
            name="GB Recap",
            event_type=event_type,
            tenant=self.tenant,
            created_by=self.system_user,
        )
        self.receipt_cat = recap_models.FileRecapCategory.objects.create(
            name="Receipts", tenant=self.tenant, created_by=self.system_user
        )
        self.other_cat = recap_models.FileRecapCategory.objects.create(
            name="Table setup", tenant=self.tenant, created_by=self.system_user
        )
        self.file_type = recap_models.FileType.objects.create(
            name="Image", extension=".jpg", created_by=self.system_user
        )

    def _custom_recap(self, *, name, when, tenant=None):
        tenant = tenant or self.tenant
        event = self.create_event(
            name=name, tenant=tenant, date=when,
            start_time=when, end_time=when + timedelta(hours=4),
        )
        return recap_models.CustomRecap.objects.create(
            name=name,
            event=event,
            tenant=tenant,
            custom_recap_template=self.template,
            created_by=self.system_user,
            updated_by=self.system_user,
        )

    def _field_value(self, custom_recap, field_name, value):
        field_type = recap_models.CustomRecapFieldType.objects.create(
            name="number", created_by=self.system_user
        )
        section = recap_models.RecapSection.objects.create(
            name="Receipts", tenant=self.tenant, created_by=self.system_user
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

    def _file(self, custom_recap, category, blob="recap_files/r1.jpg"):
        return recap_models.CustomRecapFile.objects.create(
            name="receipt.jpg",
            url=blob,
            custom_recap=custom_recap,
            file_recap_category=category,
            file_type=self.file_type,
            created_by=self.system_user,
        )

    def test_collects_custom_with_receipt_file_and_amount(self):
        recap = self._custom_recap(name="Vons Sparks", when=self.in_range)
        self._field_value(recap, "Account Spend Amount", "43.20")
        self._file(recap, self.receipt_cat)

        rows = collect_expense_rows(self.tenant.id, self.start, self.end)
        assert len(rows) == 1
        row = rows[0]
        assert row["kind"] == "custom"
        assert row["amount"] == 43.20
        assert row["event_date"] == "2026-05-14"
        assert len(row["files"]) == 1
        # public_url() needs live GCS settings — pin the blob instead.
        assert row["files"][0]["blob"] == "recap_files/r1.jpg"

    def test_non_receipt_category_and_no_amount_is_excluded(self):
        recap = self._custom_recap(name="No receipts", when=self.in_range)
        self._file(recap, self.other_cat)
        rows = collect_expense_rows(self.tenant.id, self.start, self.end)
        assert rows == []

    def test_amount_only_recap_still_included(self):
        recap = self._custom_recap(name="Spend only", when=self.in_range)
        self._field_value(recap, "Account Spend Amount", "$12")
        rows = collect_expense_rows(self.tenant.id, self.start, self.end)
        assert len(rows) == 1
        assert rows[0]["amount"] == 12.0
        assert rows[0]["files"] == []

    def test_out_of_range_and_foreign_tenant_excluded(self):
        late = datetime(2026, 6, 2, 18, 0, tzinfo=_tz.utc)
        recap = self._custom_recap(name="June event", when=late)
        self._file(recap, self.receipt_cat)
        foreign = self._custom_recap(
            name="LD event", when=self.in_range, tenant=self.other_tenant
        )
        self._field_value(foreign, "Account Spend Amount", "99")

        rows = collect_expense_rows(self.tenant.id, self.start, self.end)
        assert rows == []

    def test_legacy_account_spend_included(self):
        event = self.create_event(
            name="Legacy WF",
            tenant=self.tenant,
            date=self.in_range,
            start_time=self.in_range,
            end_time=self.in_range + timedelta(hours=4),
        )
        recap_models.Recap.objects.create(
            name="Legacy recap",
            event=event,
            account_spend_amount="25.50",
            created_by=self.system_user,
            updated_by=self.system_user,
        )
        rows = collect_expense_rows(self.tenant.id, self.start, self.end)
        assert len(rows) == 1
        assert rows[0]["kind"] == "legacy"
        assert rows[0]["amount"] == 25.50

    def test_csv_has_total_row(self):
        recap = self._custom_recap(name="Vons", when=self.in_range)
        self._field_value(recap, "Account Spend Amount", "10.50")
        rows = collect_expense_rows(self.tenant.id, self.start, self.end)
        csv_text = build_expense_rows_csv(rows)
        assert "Event date" in csv_text.splitlines()[0]
        assert "TOTAL" in csv_text
        assert "10.50" in csv_text

    def test_pdf_smoke_renders_without_images(self):
        recap = self._custom_recap(name="Vons", when=self.in_range)
        self._field_value(recap, "Account Spend Amount", "10.50")
        rows = collect_expense_rows(self.tenant.id, self.start, self.end)
        try:
            pdf = build_receipts_bundle_pdf(
                tenant_name="Girl Beer",
            start=self.start,
            end=self.end,
                rows=rows,
                images_by_blob={},
            )
        except OSError:
            # WeasyPrint's native libs (gobject/pango) aren't installed on
            # dev Macs — they live in the Cloud Run image, where the recap
            # PDF renders daily. Skip locally, render-test in the container.
            pytest.skip("WeasyPrint native libs unavailable on this host")
        assert pdf[:4] == b"%PDF"


# ---------------------------------------------------------------------------
# apply_amount_overrides — billing reconciliation
#
# BAs type the spend by hand, so a billing packet's total can disagree with
# what the attached receipts add up to. These pin that the correction is
# applied, is never silent, and fails loudly on a bad id rather than shipping
# an invoice that is quietly short.
# ---------------------------------------------------------------------------


def _row(recap_id, amount, ba="BA"):
    return {
        "kind": "custom",
        "recap_id": recap_id,
        "recap_uuid": f"uuid-{recap_id}",
        "event_uuid": "",
        "ba_name": ba,
        "ba_email": "",
        "event_name": "Event",
        "event_date": "2026-09-04",
        "address": "",
        "amount": amount,
        "corpo_card": False,
        "files": [],
    }


def test_override_replaces_amount_and_keeps_the_original():
    rows = [_row(1012, 67.00)]
    applied, problems = apply_amount_overrides(rows, {"1012": 66.14})
    assert (applied, problems) == (1, [])
    assert rows[0]["amount"] == 66.14
    assert rows[0]["amount_recorded"] == 67.00
    assert rows[0]["amount_adjusted"] is True


def test_override_accepts_int_keys_and_string_amounts():
    rows = [_row(1012, 67.00)]
    applied, problems = apply_amount_overrides(rows, {1012: "66.14"})
    assert (applied, problems) == (1, [])
    assert rows[0]["amount"] == 66.14


def test_override_matching_recorded_amount_is_not_annotated():
    """A no-op override must not brand a correct line as 'adjusted'."""
    rows = [_row(1015, 61.65)]
    applied, problems = apply_amount_overrides(rows, {"1015": 61.65})
    assert (applied, problems) == (0, [])
    assert "amount_adjusted" not in rows[0]
    assert "amount_recorded" not in rows[0]


def test_unknown_recap_id_is_reported_not_ignored():
    rows = [_row(1012, 67.00)]
    applied, problems = apply_amount_overrides(rows, {"9999": 10.0})
    assert applied == 0
    assert len(problems) == 1 and "9999" in problems[0]
    assert rows[0]["amount"] == 67.00


def test_non_numeric_override_is_reported():
    rows = [_row(1012, 67.00)]
    applied, problems = apply_amount_overrides(rows, {"1012": "abc"})
    assert applied == 0
    assert len(problems) == 1
    assert rows[0]["amount"] == 67.00


def test_untouched_rows_stay_untouched():
    rows = [_row(1012, 67.00), _row(1018, 102.80)]
    apply_amount_overrides(rows, {"1012": 66.14})
    assert rows[1]["amount"] == 102.80
    assert "amount_adjusted" not in rows[1]


def test_note_is_carried_onto_adjusted_rows_only():
    rows = [_row(1012, 67.00), _row(1018, 102.80)]
    apply_amount_overrides(rows, {"1012": 66.14}, note="Tied to receipts.")
    assert rows[0]["adjust_note"] == "Tied to receipts."
    assert "adjust_note" not in rows[1]


def test_csv_shows_both_figures_and_a_reconciled_total():
    rows = [_row(1012, 67.00), _row(1018, 102.80)]
    apply_amount_overrides(rows, {"1012": 66.14})
    csv_text = build_expense_rows_csv(rows)
    header, *body = [ln for ln in csv_text.splitlines() if ln.strip()]
    assert "As entered" in header and "Adjusted" in header
    # billed figure, original figure and the adjusted flag all present
    assert "66.14" in body[0] and "67.00" in body[0] and "yes" in body[0]
    # the untouched row carries no 'as entered' echo
    assert "102.80" in body[1]
    total_line = body[-1]
    assert "168.94" in total_line  # 66.14 + 102.80
    assert "169.80" in total_line  # what the recaps said


def test_overriding_a_row_with_no_recorded_amount_invents_no_baseline():
    """A recap that carried receipts but no typed figure has nothing 'as
    entered' — the reconciliation line must not pretend otherwise."""
    rows = [_row(1200, None)]
    applied, problems = apply_amount_overrides(rows, {"1200": 25.00})
    assert (applied, problems) == (1, [])
    assert rows[0]["amount"] == 25.00
    assert rows[0]["amount_recorded"] is None
    assert _recorded_total(rows) == 0.0


def test_recorded_total_ignores_adjustments():
    rows = [_row(1012, 67.00), _row(1018, 102.80)]
    apply_amount_overrides(rows, {"1012": 66.14})
    assert _recorded_total(rows) == pytest.approx(169.80)
