"""Receipt reading, the spend-vs-receipts review check, and the check-in
field-issue beacon."""

from __future__ import annotations

import json
from datetime import datetime
from decimal import Decimal
from unittest.mock import patch

import pytest
from django.test import Client
from django.utils import timezone

from ambassadors.tests.base import AmbassadorsGraphQLTestCase
from events import models as event_models
from recaps import models as recap_models
from recaps.checkin_issue_schema import summarize_issues
from recaps.receipt_reader import (
    compare_spend_to_receipts,
    interpret_reading,
    parse_total,
    read_pending_receipts,
)


# ─── Pure ───────────────────────────────────────────────────────────


def test_parse_total_accepts_only_plausible_amounts():
    assert parse_total("38.11") == Decimal("38.11")
    assert parse_total("$1,253.42") == Decimal("1253.42")
    assert parse_total("17.9") == Decimal("17.90")
    assert parse_total("0") is None
    assert parse_total("3001217321615444") is None  # a barcode, not a total
    assert parse_total("12.345") is None
    assert parse_total("total") is None
    assert parse_total("9999.00") is None  # above the plausibility cap


def test_interpret_reading_is_unreadable_unless_confident():
    good = {"isReceipt": True, "totalLegible": True, "total": "40.66",
            "merchant": "Smart & Final", "date": "2026-10-10"}
    out = interpret_reading(good)
    assert out == {"status": "read", "total": Decimal("40.66"),
                   "merchant": "Smart & Final", "receipt_date": "2026-10-10"}
    assert interpret_reading({**good, "totalLegible": False})["status"] == "unreadable"
    assert interpret_reading({**good, "isReceipt": False})["status"] == "unreadable"
    assert interpret_reading({**good, "total": "n/a"})["status"] == "unreadable"
    assert interpret_reading(None)["status"] == "unreadable"
    assert interpret_reading({**good, "date": "10/10/26"})["receipt_date"] == ""


def _read(total):
    return {"status": "read", "total": Decimal(str(total))}


def test_compare_flags_entered_spend_far_from_the_receipts():
    # Fresh Vintage, Oct 11: $300 entered, five receipts totalling $138.63.
    out = compare_spend_to_receipts(
        300, [_read(38.11), _read(40.66), _read(23.92), _read(17.97), _read(17.97)], 5
    )
    assert out["status"] == "flag"
    assert "$300.00 entered, receipts total $138.63" == out["label"]
    assert "$161.37 more than 5 receipts" in out["detail"]


def test_compare_passes_within_tolerance():
    assert compare_spend_to_receipts(38.50, [_read(38.11)], 1)["status"] == "pass"
    assert compare_spend_to_receipts(100, [_read(96)], 1)["status"] == "pass"  # 5%
    assert compare_spend_to_receipts(100, [_read(94)], 1)["status"] == "flag"


def test_compare_is_na_when_it_would_be_a_guess():
    assert compare_spend_to_receipts(None, [_read(10)], 1)["status"] == "na"
    assert compare_spend_to_receipts(50, [], 0)["status"] == "na"
    # One of two receipts has not been read yet.
    assert compare_spend_to_receipts(50, [_read(20)], 2)["status"] == "na"
    # Nothing readable at all.
    assert compare_spend_to_receipts(50, [{"status": "unreadable", "total": None}], 1)["status"] == "na"


def test_compare_with_an_unreadable_receipt_only_flags_overage():
    unreadable = {"status": "unreadable", "total": None}
    # Entered more than what could be read: the unread receipt may explain it.
    assert compare_spend_to_receipts(100, [_read(40), unreadable], 2)["status"] == "na"
    # Receipts already exceed what was entered: that is a real mismatch.
    out = compare_spend_to_receipts(20, [_read(60), unreadable], 2)
    assert out["status"] == "flag" and "more than the $20.00 entered" in out["label"]


def test_summarize_issues_groups_by_kind_most_frequent_first():
    t = timezone.now()
    out = summarize_issues(
        [
            ("photo_upload_failed", 1, t, "timeout"),
            ("gps_denied", 2, t, ""),
            ("photo_upload_failed", 1, t, "old"),
            ("photo_upload_failed", 3, t, ""),
        ]
    )
    assert [s["kind"] for s in out] == ["photo_upload_failed", "gps_denied"]
    assert out[0]["count"] == 3 and out[0]["events"] == 2
    assert out[0]["last_detail"] == "timeout"
    assert out[0]["label"] == "Photo uploads failed"


# ─── With the database ──────────────────────────────────────────────


@pytest.mark.django_db(transaction=True)
class TestReceiptReadingDb(AmbassadorsGraphQLTestCase):
    @pytest.fixture(autouse=True)
    def setup(self, db):
        self.roles = self.setup_default_roles()
        self.sys = self.get_system_user()
        self.tenant = self.create_tenant(name="Receipt Brand")
        self.et = event_models.EventType.objects.create(
            name="Retail Sampling", slug="receipt-retail", tenant=self.tenant,
            created_by=self.sys,
        )
        self.template = recap_models.CustomRecapTemplate.objects.create(
            name="Receipt Retail", event_type=self.et, tenant=self.tenant,
            created_by=self.sys,
        )
        self.file_type = recap_models.FileType.objects.create(
            name="image", created_by=self.sys
        )
        self.receipts = recap_models.FileRecapCategory.objects.create(
            name="Expense Receipts", tenant=self.tenant, created_by=self.sys
        )
        self.booth = recap_models.FileRecapCategory.objects.create(
            name="Booth setup", tenant=self.tenant, created_by=self.sys
        )
        when = timezone.make_aware(datetime(2026, 10, 11, 12))
        self.event = event_models.Event.objects.create(
            name="Costco", tenant=self.tenant, address="1 Main St", date=when,
            event_type=self.et, created_by=self.sys, updated_by=self.sys,
        )
        self.recap = recap_models.CustomRecap.objects.create(
            name="r", approved=True, submitted_at=when, event=self.event,
            tenant=self.tenant, custom_recap_template=self.template,
            created_by=self.sys,
        )

    def _file(self, name, category):
        return recap_models.CustomRecapFile.objects.create(
            name=name, url=f"recap_files/{name}", custom_recap=self.recap,
            file_type=self.file_type, file_recap_category=category,
            created_by=self.sys,
        )

    def test_reads_each_receipt_once_and_ignores_other_photos(self):
        r1 = self._file("r1.jpg", self.receipts)
        r2 = self._file("r2.jpg", self.receipts)
        self._file("booth.jpg", self.booth)
        self._file("scan.pdf", self.receipts)  # not an image

        answers = iter(
            [
                {"status": "read", "total": Decimal("38.11"), "merchant": "Walmart", "receipt_date": ""},
                {"status": "unreadable", "total": None, "merchant": "", "receipt_date": ""},
            ]
        )
        with patch("utils.gcs.download_blob_bytes", return_value=b"img"), patch(
            "utils.gcs.extract_blob_name_from_url", side_effect=lambda u: u
        ), patch("recaps.pdf.downscale_image_bytes", side_effect=lambda b: b), patch(
            "recaps.receipt_reader.read_receipt_image", side_effect=lambda *a, **k: next(answers)
        ) as reader:
            first = read_pending_receipts(limit=10)
            second = read_pending_receipts(limit=10)

        assert reader.call_count == 2  # only the two receipt images, once each
        assert first["read"] == 1 and first["unreadable"] == 1 and first["skipped"] == 0
        assert second["read"] == 0 and second["unreadable"] == 0
        rows = dict(
            recap_models.ReceiptReading.objects.values_list("custom_recap_file_id", "status")
        )
        assert set(rows) == {r1.id, r2.id}
        assert sorted(rows.values()) == ["read", "unreadable"]

    def test_field_issue_beacon_stores_known_kinds_only(self):
        self.tenant.checkin_code = "RB-TEST12"
        self.tenant.save(update_fields=["checkin_code"])
        client = Client()

        def post(body):
            return client.post(
                "/api/public/checkin/RB-TEST12/client-event",
                data=json.dumps(body),
                content_type="application/json",
            )

        assert post({"kind": "photo_upload_failed", "detail": "PUT   timed out\n after 3 tries"}).status_code == 204
        assert post({"kind": "not_a_kind", "detail": "x"}).status_code == 204
        assert Client().post(
            "/api/public/checkin/NOPE-000000/client-event",
            data=json.dumps({"kind": "gps_denied"}),
            content_type="application/json",
        ).status_code == 204

        rows = list(
            recap_models.CheckinFieldIssue.objects.values_list("kind", "detail", "tenant_id")
        )
        assert rows == [("photo_upload_failed", "PUT timed out after 3 tries", self.tenant.id)]

    def test_a_clock_button_press_is_recorded_but_is_not_an_issue(self):
        from recaps.checkin_issue_schema import summarize_issues

        self.tenant.checkin_code = "RB-TEST12"
        self.tenant.save(update_fields=["checkin_code"])
        res = Client().post(
            "/api/public/checkin/RB-TEST12/client-event",
            data=json.dumps({"kind": "punch_tapped", "detail": "clock in 2026-11-14T16:45:00Z"}),
            content_type="application/json",
        )
        assert res.status_code == 204
        row = recap_models.CheckinFieldIssue.objects.get()
        assert (row.kind, row.detail) == ("punch_tapped", "clock in 2026-11-14T16:45:00Z")
        # The staff issue summary reads only real problems.
        issues = (
            recap_models.CheckinFieldIssue.objects.exclude(
                kind__in=recap_models.CheckinFieldIssue.NON_ISSUE_KINDS
            ).values_list("kind", "event_id", "created_at", "detail")
        )
        assert summarize_issues(issues) == []
