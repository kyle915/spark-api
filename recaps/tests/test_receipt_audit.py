"""Receipt reading v3, spend and staff-only fraud flags.

Fixtures are made up (brand "Fizz Test Energy", stores "Total Wine #903 /
#912"); no real ambassador names, phones or receipts. The OpenAI call is
always mocked."""

from __future__ import annotations

import io
from datetime import datetime, timedelta
from decimal import Decimal
from unittest.mock import patch

import pytest
from django.test import Client, override_settings
from django.utils import timezone

from ambassadors.models import Attendance, Source
from ambassadors.tests.base import AmbassadorsGraphQLTestCase
from events import models as event_models
from recaps import models as recap_models
from recaps.receipt_flags import check_recaps, compute_recap_flags, evaluate_recap
from recaps.receipt_reader import (
    brand_tokens,
    check_arithmetic,
    interpret_detail,
    parse_card_last4,
    parse_receipt_count,
    parse_time,
    queue_recap_receipts,
    read_pending_receipts,
    receipt_keys,
    scrub_card_numbers,
)
from recaps.receipt_spend import program_spend, recap_spend, spend_csv

# ─── A made-up receipt, in the shape the model returns ──────────────


def receipt(
    *, store="903", city="Palm Beach Gardens", ticket="318280", time="2:07 PM",
    day="2026-10-23", total="62.70", items=None, tax="3.87", fees="",
    subtotal="58.83", legible=True, is_receipt=True, last4="6291",
):
    items = items if items is not None else [
        ("Fizz Test Energy Melon-12oz", "4", "3.49", "13.96"),
        ("Fizz Test Energy Grape-12oz", "4", "3.49", "13.96"),
        ("Ice Bag 7 lb", "1", "2.99", "2.99"),
        ("Fizz Test Energy Lemon-12oz", "4", "3.49", "13.96"),
        ("Fizz Test Energy Razz-12oz", "4", "3.49", "13.96"),
    ]
    return {
        "isReceipt": is_receipt, "totalLegible": legible, "total": total,
        "merchant": "Total Wine & More", "storeNumber": store, "date": day,
        "lines": [{"amount": total, "kind": "charge"}] if legible else [],
        "address": "", "city": city, "state": "FL", "ticketNumber": ticket,
        "register": "6", "time": time,
        "items": [
            {"name": n, "qty": q, "unitPrice": u, "amount": a, "isBrandProduct": False}
            for n, q, u, a in items
        ],
        "subtotal": subtotal, "fees": fees, "tax": tax, "cardLast4": last4,
        "confidence": 0.95,
    }


TOKENS = brand_tokens("Fizz Test Energy")


# ─── Pure ───────────────────────────────────────────────────────────


def test_brand_tokens_drop_generic_words():
    assert brand_tokens("Pivot Energy Drink") == {"pivot"}
    assert "fizz" in TOKENS and "energy" not in TOKENS


def test_detail_reads_the_whole_ticket_and_splits_brand_lines():
    out = interpret_detail(receipt(), TOKENS)
    assert out["ticket_number"] == "318280"
    assert out["purchase_time"] == "14:07"
    assert out["card_last4"] == "6291"
    assert out["arithmetic_ok"] is True
    brand = [i for i in out["line_items"] if i["isBrand"]]
    other = [i for i in out["line_items"] if not i["isBrand"]]
    assert sum(Decimal(i["amount"]) for i in brand) == Decimal("55.84")
    assert [i["name"] for i in other] == ["Ice Bag 7 lb"]
    assert out["fingerprint"] and out["ticket_key"] and out["loose_key"]


def test_arithmetic_flags_a_total_that_does_not_add_up():
    out = interpret_detail(receipt(total="72.70", subtotal=""), TOKENS)
    assert out["arithmetic_ok"] is False
    assert "off by $10.00" in out["arithmetic_note"]
    assert out["confidence"] <= 0.4
    ok, _ = check_arithmetic([Decimal("10.47")], Decimal("0.06"), Decimal("0.75"), None, Decimal("11.28"))
    assert ok is True  # fees counted
    ok, note = check_arithmetic([Decimal("10.00")], None, Decimal("1.00"), Decimal("12.00"), Decimal("11.00"))
    assert ok is False and "subtotal" in note.lower()
    # The bottom half of a long receipt: one line in view, the printed
    # subtotal and tax still make the total. Not a math problem.
    ok, note = check_arithmetic([Decimal("2.99")], None, Decimal("4.90"), Decimal("65.81"), Decimal("70.71"))
    assert ok is None and "not in this photo" in note
    ok, _ = check_arithmetic([Decimal("80.00")], None, Decimal("4.90"), Decimal("65.81"), Decimal("70.71"))
    assert ok is False


def test_edited_store_header_keeps_the_loose_key():
    real = receipt_keys(store_number="903", merchant="Total Wine", ticket="318280",
                        receipt_date="2026-10-23", total=Decimal("62.70"))
    edited = receipt_keys(store_number="912", merchant="Total Wine", ticket="318280",
                          receipt_date="2026-10-23", total=Decimal("62.70"))
    assert real["fingerprint"] != edited["fingerprint"]
    assert real["ticket_key"] != edited["ticket_key"]
    assert real["loose_key"] == edited["loose_key"]
    assert receipt_keys(store_number="903", merchant="", ticket="", receipt_date="2026-10-23",
                        total=Decimal("1")) == {"fingerprint": "", "ticket_key": "", "loose_key": ""}


def test_card_numbers_are_never_kept():
    assert parse_card_last4("6291") == "6291"
    assert parse_card_last4("**** 6291") == "6291"
    assert parse_card_last4("4111111111111111") == ""
    scrubbed = scrub_card_numbers({"x": "card 4111 1111 1111 1111 ok", "upc": "850060125413"})
    assert scrubbed == {"x": "card [card] ok", "upc": "850060125413"}


def test_receipt_count_defaults_to_one():
    assert parse_receipt_count(None) == 1
    assert parse_receipt_count(receipt()) == 1
    assert parse_receipt_count({**receipt(), "receiptCount": 3}) == 3
    assert parse_receipt_count({**receipt(), "receiptCount": 0}) == 1
    assert parse_receipt_count({**receipt(), "receiptCount": "x"}) == 1
    assert interpret_detail({**receipt(), "receiptCount": 2}, TOKENS)["receipt_count"] == 2


def test_parse_time():
    assert parse_time("2:07 PM") == "14:07"
    assert parse_time("14:07:39") == "14:07"
    assert parse_time("12:05 am") == "00:05"
    assert parse_time("noon") == ""


def test_not_a_receipt_has_no_detail():
    assert interpret_detail(receipt(is_receipt=False))["ticket_number"] == ""


# ─── With the database ──────────────────────────────────────────────


def _jpeg(seed: int) -> bytes:
    from PIL import Image

    img = Image.new("RGB", (72, 96))
    img.putdata([((x * seed) % 255, (y * 3 + seed) % 255, (x + y) % 255)
                 for y in range(96) for x in range(72)])
    buf = io.BytesIO()
    img.save(buf, format="JPEG")
    return buf.getvalue()


@pytest.mark.django_db(transaction=True)
class TestReceiptAudit(AmbassadorsGraphQLTestCase):
    @pytest.fixture(autouse=True)
    def setup(self, db):
        from config.schema_client import schema_clients

        self.schema = schema_clients
        self.endpoint_path = "/api/v1/graphql/clients"
        self.roles = self.setup_default_roles()
        self.sys = self.get_system_user()
        self.tenant = self.create_tenant(name="Fizz Test Energy")
        self.other_tenant = self.create_tenant(name="Other Brand")
        self.file_type = recap_models.FileType.objects.create(name="image", created_by=self.sys)
        self.admin = self.create_user(
            username="staff-1", email="staff-1@test.com", role=self.roles["spark_admin"])
        self.brand_user = self.create_user(
            username="brand-1", email="brand-1@brand.test", role=self.roles["client"])
        self.create_tenanted_user(self.brand_user, self.tenant)
        self.ctx = {}
        for t in (self.tenant, self.other_tenant):
            et = event_models.EventType.objects.create(
                name="Retail", slug=f"retail-{t.id}", tenant=t, created_by=self.sys)
            self.ctx[t.id] = {
                "et": et,
                "template": recap_models.CustomRecapTemplate.objects.create(
                    name="Retail", event_type=et, tenant=t, created_by=self.sys),
                "receipts": recap_models.FileRecapCategory.objects.create(
                    name="Receipts", tenant=t, created_by=self.sys),
                "shelf": recap_models.FileRecapCategory.objects.create(
                    name="Shelf", tenant=t, created_by=self.sys),
            }
        self.images = {}

    # helpers

    def recap(self, name, *, tenant=None, day=datetime(2026, 10, 23, 14), **extra):
        tenant = tenant or self.tenant
        when = timezone.make_aware(day)
        event = event_models.Event.objects.create(
            name=name, tenant=tenant, address="1 Main St", date=when, start_time=when,
            event_type=self.ctx[tenant.id]["et"], created_by=self.sys, updated_by=self.sys)
        return recap_models.CustomRecap.objects.create(
            name="r", approved=True, submitted_at=when, event=event, tenant=tenant,
            custom_recap_template=self.ctx[tenant.id]["template"], created_by=self.sys,
            **extra)

    def file(self, recap, name, *, kind="receipts", sha=""):
        return recap_models.CustomRecapFile.objects.create(
            name=name, url=f"recap_files/{name}", custom_recap=recap,
            file_type=self.file_type,
            file_recap_category=self.ctx[recap.tenant_id][kind],
            created_by=self.sys, content_sha256=sha)

    def read_all(self, answers: dict, **kw):
        """Run the real reader; the model answers from ``answers`` by file
        name (blob)."""
        current = {}

        def download(blob):
            current["blob"] = blob
            return _jpeg(len(blob))

        def vision(*a, **k):
            k.get("meta", {}).update({"model": "test-model", "input_tokens": 900, "output_tokens": 300})
            return answers[current["blob"].split("/")[-1]]

        with patch("utils.gcs.download_blob_bytes", side_effect=download), patch(
            "utils.gcs.extract_blob_name_from_url", side_effect=lambda u: u
        ), patch("recaps.receipt_reader.generate_vision_json", side_effect=vision):
            kw.setdefault("limit", 50)
            return read_pending_receipts(**kw)

    def flags_of(self, recap):
        return {
            (f.kind, f.severity): f
            for f in recap_models.RecapFlag.objects.filter(custom_recap=recap, resolved_at__isnull=True)
        }

    # the motivating case

    def pivot_pair(self):
        pbg = self.recap("Total Wine #903 Palm Beach Gardens", store_number="903")
        nmb = self.recap("Total Wine #912 North Miami Beach", store_number="912")
        self.file(pbg, "pbg.jpg")
        f = self.file(nmb, "nmb.webp")
        # Uploaded later (staff filed it 16 days after the event).
        recap_models.CustomRecapFile.objects.filter(id=f.id).update(
            created_at=timezone.now() + timedelta(days=16))
        report = self.read_all({
            "pbg.jpg": receipt(),
            "nmb.webp": receipt(store="912", city="North Miami Beach"),
        })
        assert report["read"] == 2
        return pbg, nmb

    def test_copied_receipt_with_edited_header_is_flagged_on_both_recaps(self):
        pbg, nmb = self.pivot_pair()
        evaluate_recap(nmb.id)
        nmb_flags = self.flags_of(nmb)
        dup = nmb_flags[("duplicate_receipt", "high")]
        assert "318280" in dup.explanation
        assert "#912" in dup.explanation and "#903" in dup.explanation
        assert "Total Wine #903 Palm Beach Gardens" in dup.explanation
        assert dup.details["otherRecap"]["recapUuid"] == str(pbg.uuid)
        mismatch = nmb_flags[("store_mismatch", "high")]
        assert "#903" in mismatch.explanation and "#912" in mismatch.explanation
        # The original carries the other side of the duplicate, not a mismatch.
        pbg_flags = self.flags_of(pbg)
        assert ("duplicate_receipt", "medium") in pbg_flags
        assert not any(kind == "store_mismatch" for kind, _ in pbg_flags)
        # Spend counts the ticket once, on the recap that had it first.
        spend = program_spend(tenant_id=self.tenant.id, date_from=None, date_to=None)
        assert spend["totals"]["total"] == Decimal("62.70")
        assert spend["totals"]["product"] == Decimal("55.84")
        assert spend["totals"]["other"] == Decimal("2.99")
        assert spend["totals"]["tax"] == Decimal("3.87")
        assert spend["totals"]["receipts"] == 1
        row = spend["receipts"][0]
        assert row["ownerRecapId"] == pbg.id and row["alsoOnRecapIds"] == [nmb.id]
        assert recap_spend(nmb.id)["totals"]["total"] == Decimal("0.00")
        assert recap_spend(pbg.id)["totals"]["total"] == Decimal("62.70")
        text = spend_csv(spend["receipts"])
        assert "318280" in text and "55.84" in text and "duplicate_receipt" in text

    def test_duplicate_receipt_is_found_across_tenants(self):
        mine = self.recap("Total Wine #903 Palm Beach Gardens", store_number="903")
        theirs = self.recap("Total Wine #903 Palm Beach Gardens", tenant=self.other_tenant,
                            store_number="903")
        self.file(mine, "a.jpg")
        f = self.file(theirs, "b.jpg")
        recap_models.CustomRecapFile.objects.filter(id=f.id).update(
            created_at=timezone.now() + timedelta(hours=1))
        self.read_all({"a.jpg": receipt(), "b.jpg": receipt()})
        evaluate_recap(theirs.id)
        assert ("duplicate_receipt", "high") in self.flags_of(theirs)
        assert ("duplicate_receipt", "medium") in self.flags_of(mine)

    def test_rephotographed_and_split_receipts_count_once_and_are_not_flagged(self):
        r = self.recap("Total Wine #914 Sarasota", store_number="914")
        self.file(r, "s1.jpg")
        self.file(r, "s2.jpg")
        self.file(r, "top.jpg")
        self.file(r, "bottom.jpg")
        same = receipt(store="914", ticket="457444", total="18.12", tax="1.17", subtotal="16.95",
                       items=[("Fizz Test Energy Melon", "4", "3.49", "13.96"),
                              ("Ice Bag 7 lb", "1", "2.99", "2.99")])
        top = receipt(store="929", ticket="578076", total="", legible=False, subtotal="", tax="",
                      items=[("Fizz Test Energy Melon", "18", "3.49", "62.82")])
        bottom = receipt(store="", ticket="", total="70.71", tax="4.90", subtotal="",
                         items=[("Ice Bag 7 lb", "1", "2.99", "2.99")])
        self.read_all({"s1.jpg": same, "s2.jpg": same, "top.jpg": top, "bottom.jpg": bottom})
        evaluate_recap(r.id)
        assert not any(k == "duplicate_receipt" for k, _ in self.flags_of(r))
        spend = recap_spend(r.id)
        assert spend["totals"]["total"] == Decimal("88.83")  # 18.12 + 70.71
        assert spend["totals"]["receipts"] == 2
        # The halves' lines are combined, the re-photo's are not doubled.
        assert spend["totals"]["product"] == Decimal("76.78")  # 13.96 + 62.82
        assert spend["totals"]["other"] == Decimal("5.98")
        assert spend["totals"]["unitemized"] == Decimal("0.00")

    def test_store_mismatch_from_the_receipt_header(self):
        r = self.recap("Total Wine #925 Altamonte Springs", store_number="925")
        self.file(r, "x.jpg")
        self.read_all({"x.jpg": receipt(store="934", city="Wesley Chapel")})
        flags, _ = compute_recap_flags(r.id)
        mismatch = [f for f in flags if f["kind"] == "store_mismatch"]
        assert mismatch and mismatch[0]["severity"] == "high"
        assert "#934" in mismatch[0]["explanation"] and "#925" in mismatch[0]["explanation"]

    def test_receipt_math_flag(self):
        r = self.recap("Total Wine #903 Palm Beach Gardens", store_number="903")
        self.file(r, "m.jpg")
        self.read_all({"m.jpg": receipt(total="72.70", subtotal="")})
        flags, _ = compute_recap_flags(r.id)
        assert [f["severity"] for f in flags if f["kind"] == "receipt_math"] == ["low"]

    def test_file_ids_rereads_a_photo_that_was_already_read(self):
        from recaps.receipt_reader import find_read_receipts

        r = self.recap("Total Wine #903 Palm Beach Gardens", store_number="903")
        f = self.file(r, "misread.jpg")
        other = self.file(r, "other.jpg")
        self.read_all({"misread.jpg": receipt(total="3.48"), "other.jpg": receipt(ticket="9")})
        assert recap_models.ReceiptReading.objects.get(custom_recap_file=f).total == Decimal("3.48")

        found = find_read_receipts(merchant="total wine")
        assert {row["fileId"] for row in found} == {f.id, other.id}

        # Nothing is unread, so a normal run reads nothing.
        assert self.read_all({})["read"] == 0
        dry = self.read_all({}, file_ids=[f.id], dry_run=True)
        assert [row["fileId"] for row in dry["files"]] == [f.id]
        assert dry["files"][0]["total"] == "3.48"

        report = self.read_all({"misread.jpg": receipt(total="62.70")}, file_ids=[f.id])
        assert report["read"] == 1
        assert recap_models.ReceiptReading.objects.get(custom_recap_file=f).total == Decimal("62.70")

    def test_multiple_receipts_flag(self):
        r = self.recap("Total Wine #903 Palm Beach Gardens", store_number="903")
        self.file(r, "two.jpg")
        self.file(r, "one.jpg")
        self.read_all({
            "two.jpg": {**receipt(), "receiptCount": 2},
            "one.jpg": {**receipt(ticket="318281"), "receiptCount": 1},
        })
        flags, _ = compute_recap_flags(r.id)
        multi = [f for f in flags if f["kind"] == "multiple_receipts"]
        assert len(multi) == 1
        assert multi[0]["severity"] == "low"
        assert "2 receipts" in multi[0]["explanation"]

    def test_short_clock_only_when_the_recap_reports_activity(self):
        user = self.create_user(username="ba-1", email="ba-1@test.com", role=self.roles["ambassador"])
        ba = self.create_ambassador(user)
        r = self.recap("Total Wine #912 North Miami Beach", ambassador=ba, total_engagements=40)
        cin, _ = Source.objects.get_or_create(name="clock_in", defaults={"created_by": self.sys})
        cout, _ = Source.objects.get_or_create(name="clock_out", defaults={"created_by": self.sys})
        start = r.event.start_time
        Attendance.objects.create(clock_time=start, ambassador=ba, event=r.event, source=cin, created_by=self.sys)
        Attendance.objects.create(clock_time=start + timedelta(seconds=5), ambassador=ba,
                                  event=r.event, source=cout, created_by=self.sys)
        flags, _ = compute_recap_flags(r.id)
        short = [f for f in flags if f["kind"] == "short_clock"]
        assert short and "5 seconds" in short[0]["explanation"]
        assert "40 engagements" in short[0]["explanation"]
        recap_models.CustomRecap.objects.filter(id=r.id).update(total_engagements=0)
        assert not [f for f in compute_recap_flags(r.id)[0] if f["kind"] == "short_clock"]

    def test_late_staff_filing(self):
        r = self.recap("Total Wine #912 North Miami Beach", filling_for_ambassador=True)
        recap_models.CustomRecap.objects.filter(id=r.id).update(
            created_at=r.event.date + timedelta(days=16))
        flags, _ = compute_recap_flags(r.id)
        late = [f for f in flags if f["kind"] == "late_staff_filing"]
        assert late and "16 days" in late[0]["explanation"]
        recap_models.CustomRecap.objects.filter(id=r.id).update(
            created_at=r.event.date + timedelta(days=2))
        assert not [f for f in compute_recap_flags(r.id)[0] if f["kind"] == "late_staff_filing"]

    def test_duplicate_photos_across_recaps_and_within_one(self):
        sha = "a" * 64
        first = self.recap("Total Wine #903 Palm Beach Gardens")
        second = self.recap("Total Wine #912 North Miami Beach", tenant=self.other_tenant)
        self.file(first, "shelf.jpg", kind="shelf", sha=sha)
        f2 = self.file(second, "shelf2.jpg", kind="shelf", sha=sha)
        recap_models.CustomRecapFile.objects.filter(id=f2.id).update(
            created_at=timezone.now() + timedelta(hours=1))
        self.file(second, "table.jpg", kind="shelf", sha="b" * 64)
        self.file(second, "table-again.jpg", kind="shelf", sha="b" * 64)
        flags, _ = compute_recap_flags(second.id)
        photo = {(f["severity"], f["details"].get("within", False)) for f in flags if f["kind"] == "duplicate_photo"}
        assert ("medium", False) in photo  # same file as the earlier recap
        assert ("low", True) in photo  # attached twice in this recap

    def test_near_identical_photo_by_difference_hash(self):
        a = self.recap("Total Wine #903 Palm Beach Gardens")
        b = self.recap("Total Wine #912 North Miami Beach")
        fa = self.file(a, "p1.jpg", kind="shelf")
        fb = self.file(b, "p2.jpg", kind="shelf")
        recap_models.RecapPhotoScore.objects.create(custom_recap_file=fa, dhash="f0f0f0f0f0f0f0f0")
        recap_models.RecapPhotoScore.objects.create(custom_recap_file=fb, dhash="f0f0f0f0f0f0f0f1")
        flags, _ = compute_recap_flags(b.id)
        assert any(f["kind"] == "duplicate_photo" and not f["details"]["exact"] for f in flags)

    def test_resolution_sticks_and_stale_flags_close_themselves(self):
        pbg, nmb = self.pivot_pair()
        evaluate_recap(nmb.id)
        dup = recap_models.RecapFlag.objects.get(custom_recap=nmb, kind="duplicate_receipt")
        dup.resolved_at = timezone.now()
        dup.resolved_by = self.admin
        dup.resolution_note = "Checked with the store"
        dup.save()
        evaluate_recap(nmb.id)
        dup.refresh_from_db()
        assert dup.resolved_by_id == self.admin.id  # a person's decision stays
        # The copy is removed from the PBG recap's readings → the PBG flag closes.
        recap_models.ReceiptReading.objects.filter(custom_recap_file__custom_recap=nmb).update(
            fingerprint="", ticket_key="", loose_key="")
        evaluate_recap(pbg.id)
        row = recap_models.RecapFlag.objects.get(custom_recap=pbg, kind="duplicate_receipt")
        assert row.resolved_at is not None and row.resolved_by_id is None
        assert row.resolution_note == recap_models.RecapFlag.AUTO_CLEARED_NOTE

    # reader mechanics

    def test_statuses_cap_and_dry_run(self):
        r = self.recap("Total Wine #903 Palm Beach Gardens")
        for n in ("a.jpg", "b.jpg", "c.jpg"):
            self.file(r, n)
        dry = self.read_all({}, dry_run=True)
        assert dry["would_read"] == 3 and not recap_models.ReceiptReading.objects.exists()
        with override_settings(RECEIPT_READER_DAILY_MAX_READS=2):
            first = self.read_all({"c.jpg": receipt(), "b.jpg": receipt(is_receipt=False), "a.jpg": None})
            assert first["read"] == 1 and first["not_a_receipt"] == 1 and first["capped"] is True
            second = self.read_all({"a.jpg": None})
            assert second["capped"] is True and second["read"] == 0
        third = self.read_all({"a.jpg": None})
        assert third["failed"] == 1
        row = recap_models.ReceiptReading.objects.get(custom_recap_file__name="a.jpg")
        assert row.status == "failed" and row.attempts == 1
        good = recap_models.ReceiptReading.objects.get(custom_recap_file__name="c.jpg")
        assert good.model == "test-model" and good.input_tokens == 900
        assert good.card_last4 == "6291" and len(good.image_dhash) == 16
        assert "6291" not in str(good.raw.get("items"))

    def test_approval_queues_receipts_as_pending(self):
        r = self.recap("Total Wine #903 Palm Beach Gardens")
        self.file(r, "q.jpg")
        self.file(r, "shelf.jpg", kind="shelf")
        assert queue_recap_receipts(r.id) == 1
        row = recap_models.ReceiptReading.objects.get()
        assert row.status == "pending"
        self.read_all({"q.jpg": receipt()})
        row.refresh_from_db()
        assert row.status == "read"

    def test_spend_check_waits_for_queued_receipts(self):
        from recaps.recap_review_checks import _Facts, _check_receipt_total

        r = self.recap("Total Wine #903 Palm Beach Gardens")
        self.file(r, "one.jpg")
        self.file(r, "two.jpg")
        self.read_all({"two.jpg": receipt()}, limit=1)  # newest first: two.jpg only
        queue_recap_receipts(r.id)  # one.jpg now pending
        facts = _Facts()
        facts.account_spend = 200.0
        assert _check_receipt_total(r.id, True, facts)["status"] == "na"
        self.read_all({"one.jpg": receipt(ticket="999", total="137.30", tax="", subtotal="",
                                          items=[])})
        assert _check_receipt_total(r.id, True, facts)["status"] == "pass"

    def test_check_recaps_dry_run_writes_nothing(self):
        pbg, nmb = self.pivot_pair()
        out = check_recaps(recap_ids=[nmb.id], dry_run=True, tenant_id=self.tenant.id)
        assert out["recaps_by_kind"].get("duplicate_receipt")
        assert not recap_models.RecapFlag.objects.exists()

    def test_cron_endpoint_dry_run(self):
        r = self.recap("Total Wine #903 Palm Beach Gardens")
        self.file(r, "a.jpg")
        with override_settings(INTERNAL_CRON_SECRET="s3cret"):
            res = Client().post(
                "/internal/cron/read-recap-receipts",
                {"dry_run": "true", "tenant_id": str(self.tenant.id),
                 "date_from": "2026-10-01", "date_to": "2026-10-31", "flags": "force"},
                HTTP_X_CRON_SECRET="s3cret",
            )
            bad = Client().post("/internal/cron/read-recap-receipts", {"date_from": "10/1"},
                                HTTP_X_CRON_SECRET="s3cret")
        assert res.status_code == 200, res.content
        body = res.json()
        assert body["report"]["dry_run"] is True and body["report"]["would_read"] == 1
        assert body["flags"]["dry_run"] is True
        assert bad.status_code == 400
        assert not recap_models.ReceiptReading.objects.exists()

    # GraphQL, through the real schema, with the field set the web app uses

    AUDIT_QUERY = """
    query RecapReceiptsPanelQuery($recapId: ID!) {
      recapReceiptAudit(recapId: $recapId) {
        waitingCount
        flagsEnabled
        totals { product other unitemized fees tax total receipts }
        readings {
          id fileId fileUrl status merchant storeNumber address city stateCode
          ticketNumber register receiptDate purchaseTime subtotal fees tax total
          cardLast4 confidence arithmeticOk arithmeticNote model readAt error
          countedHere alsoOnRecapUuids
          lineItems { name qty unitPrice amount isBrand }
        }
        flags {
          id recapId recapUuid tenantId tenantName eventName eventDate kind kindLabel
          severity explanation otherRecapUuid createdAt resolvedAt resolvedByName
          resolutionNote autoCleared
        }
      }
    }
    """
    LIST_QUERY = """
    query ReceiptChecksPageQuery($tenantId: ID, $dateFrom: String, $dateTo: String,
                                 $kind: String, $unresolvedOnly: Boolean!, $includeCsv: Boolean!) {
      receiptSpendSummary(tenantId: $tenantId, dateFrom: $dateFrom, dateTo: $dateTo,
                           includeCsv: $includeCsv) {
        receiptRows
        csv
        totals { product other unitemized fees tax total receipts }
        recaps {
          recapId recapUuid eventName eventDate openFlags
          totals { product other unitemized fees tax total receipts }
        }
      }
      recapStaffFlags(tenantId: $tenantId, dateFrom: $dateFrom, dateTo: $dateTo, kind: $kind,
                      unresolvedOnly: $unresolvedOnly, first: 200) {
        id recapId recapUuid tenantId tenantName eventName eventDate kind kindLabel
        severity explanation otherRecapUuid createdAt resolvedAt resolvedByName
        resolutionNote autoCleared
      }
    }
    """
    RESOLVE = """
    mutation ResolveRecapFlagMutation($input: ResolveRecapFlagInput!) {
      resolveRecapFlag(input: $input) {
        success message
        flag { id resolvedAt resolvedByName resolutionNote autoCleared }
      }
    }
    """

    @pytest.mark.asyncio
    async def test_graphql_staff_sees_everything_brand_users_see_nothing(self):
        from asgiref.sync import sync_to_async

        pbg, nmb = await sync_to_async(self.pivot_pair, thread_sensitive=True)()
        await sync_to_async(evaluate_recap, thread_sensitive=True)(nmb.id)

        staff = await self._execute_query_authenticated(
            self.AUDIT_QUERY, {"recapId": str(nmb.id)}, self.admin, self.endpoint_path)
        assert staff.errors is None, staff.errors
        audit = staff.data["recapReceiptAudit"]
        assert {f["kind"] for f in audit["flags"]} >= {"duplicate_receipt", "store_mismatch"}
        assert audit["readings"][0]["ticketNumber"] == "318280"
        assert audit["readings"][0]["countedHere"] is False
        assert audit["readings"][0]["alsoOnRecapUuids"] == [str(pbg.uuid)]
        assert audit["totals"]["total"] == "0.00"

        listing = await self._execute_query_authenticated(
            self.LIST_QUERY,
            {"tenantId": str(self.tenant.id), "dateFrom": "2026-10-01", "dateTo": "2026-10-31",
             "kind": None, "unresolvedOnly": True, "includeCsv": True},
            self.admin, self.endpoint_path)
        assert listing.errors is None, listing.errors
        assert listing.data["receiptSpendSummary"]["totals"]["total"] == "62.70"
        assert "318280" in listing.data["receiptSpendSummary"]["csv"]
        flags = listing.data["recapStaffFlags"]
        assert flags[0]["severity"] == "high"

        # A brand user of the same tenant: nothing, for their own recaps.
        brand = await self._execute_query_authenticated(
            self.AUDIT_QUERY, {"recapId": str(nmb.id)}, self.brand_user, self.endpoint_path)
        assert brand.errors is None and brand.data["recapReceiptAudit"] is None
        brand_list = await self._execute_query_authenticated(
            self.LIST_QUERY,
            {"tenantId": str(self.tenant.id), "dateFrom": "2026-10-01", "dateTo": "2026-10-31",
             "kind": None, "unresolvedOnly": True, "includeCsv": True},
            self.brand_user, self.endpoint_path)
        assert brand_list.errors is None
        assert brand_list.data["receiptSpendSummary"] is None
        assert brand_list.data["recapStaffFlags"] == []

        flag_id = flags[0]["id"]
        denied = await self._execute_mutation(
            self.RESOLVE, {"input": {"flagId": flag_id, "note": "fine"}},
            self.endpoint_path, user=self.brand_user)
        assert denied.errors is None and denied.data["resolveRecapFlag"]["success"] is False
        needs_note = await self._execute_mutation(
            self.RESOLVE, {"input": {"flagId": flag_id, "note": " "}},
            self.endpoint_path, user=self.admin)
        assert needs_note.data["resolveRecapFlag"]["success"] is False
        done = await self._execute_mutation(
            self.RESOLVE, {"input": {"flagId": flag_id, "note": "Store confirmed the copy"}},
            self.endpoint_path, user=self.admin)
        assert done.errors is None, done.errors
        payload = done.data["resolveRecapFlag"]
        assert payload["success"] is True and payload["flag"]["resolvedAt"]
        assert payload["flag"]["resolutionNote"] == "Store confirmed the copy"
