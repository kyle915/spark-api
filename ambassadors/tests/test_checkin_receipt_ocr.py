"""Tests for ``POST /api/public/checkin/<code>/receipt-read`` and
``ambassadors.checkin_receipt_ocr``."""

from __future__ import annotations

import base64
import hashlib
import json
import time
from datetime import date, datetime
from datetime import timezone as dt_timezone
from decimal import Decimal
from io import BytesIO

import pytest
from django.core.cache import cache
from django.test import Client as DjangoClient
from django.urls import reverse
from PIL import Image

from ambassadors import checkin_receipt_ocr, checkin_web
from ambassadors.checkin_receipt_ocr import RECEIPT_UNREADABLE, propose_brand_spend
from ambassadors.tests.base import AmbassadorsGraphQLTestCase
from events import checkin_views
from events.checkin_tokens import make_checkin_session_token
from events.models import Product, ProductType, Retailer
from recaps.models import (
    CustomField,
    CustomFieldValue,
    CustomRecap,
    CustomRecapFieldType,
    CustomRecapFile,
    CustomRecapTemplate,
    FileRecapCategory,
    FileType,
    RecapSection,
    ReceiptReading,
)
from recaps.receipt_reader import receipt_keys
from receipts.ocr import OcrResult


def _completion(payload: dict) -> dict:
    return {"choices": [{"message": {"content": json.dumps(payload)}}]}


def _jpeg_bytes(shade: int = 180) -> bytes:
    buf = BytesIO()
    Image.new("RGB", (64, 48), (shade, (shade + 30) % 255, (shade + 60) % 255)).save(
        buf, "JPEG"
    )
    return buf.getvalue()


def _jpeg_b64(shade: int = 180) -> str:
    return base64.b64encode(_jpeg_bytes(shade)).decode("ascii")


def _vision_receipt(
    *,
    merchant: str = "Sprouts Farmers Market",
    store_number: str = "412",
    receipt_date: str = "2026-10-16",
    total: str = "46.29",
    subtotal: str = "43.45",
    tax: str = "2.84",
    fees: str = "0.00",
    ticket: str = "884120",
    address: str = "",
    city: str = "Austin",
    state: str = "TX",
    items: list[dict] | None = None,
    legible: bool = True,
    is_receipt: bool = True,
) -> dict:
    default_items = [
        {
            "name": "Brew Dr Peach Kombucha",
            "qty": "6",
            "unitPrice": "3.33",
            "amount": "19.98",
            "isBrandProduct": True,
        },
        {
            "name": "Brew Dr Classic Kombucha",
            "qty": "6",
            "unitPrice": "3.33",
            "amount": "19.98",
            "isBrandProduct": True,
        },
        {
            "name": "Personal Protein Bar",
            "qty": "1",
            "unitPrice": "3.49",
            "amount": "3.49",
            "isBrandProduct": False,
        },
    ]
    return {
        "isReceipt": is_receipt,
        "totalLegible": legible,
        "total": total,
        "merchant": merchant,
        "storeNumber": store_number,
        "date": receipt_date,
        "lines": [{"amount": total, "kind": "charge"}] if legible else [],
        "address": address,
        "city": city,
        "state": state,
        "ticketNumber": ticket,
        "register": "4",
        "time": "14:15",
        "items": default_items if items is None else items,
        "subtotal": subtotal,
        "fees": fees,
        "tax": tax,
        "cardLast4": "4242",
        "confidence": 0.96,
    }


def test_propose_brand_spend_excludes_non_brand_items_on_mixed_receipt():
    out = propose_brand_spend(
        {
            "status": "read",
            "total": Decimal("46.29"),
            "fees": Decimal("0.00"),
            "tax": Decimal("2.84"),
            "line_items": [
                {"name": "Brew Dr Peach", "amount": "39.96", "isBrand": True},
                {"name": "Snack Bar", "amount": "3.49", "isBrand": False},
            ],
        }
    )
    assert out["proposed_spend"] == Decimal("42.80")
    assert out["receipt_total"] == Decimal("46.29")
    assert out["brand_product"] == Decimal("39.96")
    assert out["excluded_non_brand_total"] == Decimal("3.49")
    assert out["excluded_non_brand_count"] == 1


def test_propose_brand_spend_nets_negative_non_brand_discounts_and_refunds():
    # $5.00 non-brand item with a -$2.00 coupon nets to $3.00 excluded, not $5.00.
    discounted = propose_brand_spend(
        {
            "status": "read",
            "total": Decimal("44.50"),
            "fees": Decimal("0.00"),
            "tax": Decimal("1.50"),
            "line_items": [
                {"name": "Brew Dr Peach", "amount": "40.00", "isBrand": True},
                {"name": "Snack Pack", "amount": "5.00", "isBrand": False},
                {"name": "Snack Coupon", "amount": "-2.00", "isBrand": False},
            ],
        }
    )
    assert discounted["proposed_spend"] == Decimal("41.50")
    assert discounted["excluded_non_brand_total"] == Decimal("3.00")
    assert discounted["excluded_non_brand_count"] == 1

    # Fully refunded non-brand item ($5.00 - $5.00 = $0.00) subtracts nothing.
    refunded = propose_brand_spend(
        {
            "status": "read",
            "total": Decimal("41.50"),
            "fees": Decimal("0.00"),
            "tax": Decimal("1.50"),
            "line_items": [
                {"name": "Brew Dr Peach", "amount": "40.00", "isBrand": True},
                {"name": "Snack Pack", "amount": "5.00", "isBrand": False},
                {"name": "Snack Refund", "amount": "-5.00", "isBrand": False},
            ],
        }
    )
    assert refunded["proposed_spend"] == Decimal("41.50")
    assert refunded["excluded_non_brand_total"] == Decimal("0.00")
    assert refunded["excluded_non_brand_count"] == 0


def test_propose_brand_spend_keeps_total_when_supply_only_or_all_brand():
    supply_only = propose_brand_spend(
        {
            "status": "read",
            "total": Decimal("14.50"),
            "fees": Decimal("0.00"),
            "tax": Decimal("1.02"),
            "line_items": [
                {"name": "Ice Bag 10lb", "amount": "6.99", "isBrand": False},
                {"name": "Sample Cups", "amount": "6.49", "isBrand": False},
            ],
        }
    )
    assert supply_only["proposed_spend"] == Decimal("14.50")
    assert supply_only["excluded_non_brand_count"] == 0

    all_brand = propose_brand_spend(
        {
            "status": "read",
            "total": Decimal("42.80"),
            "fees": Decimal("0.00"),
            "tax": Decimal("2.84"),
            "line_items": [
                {"name": "Brew Dr Peach", "amount": "39.96", "isBrand": True},
            ],
        }
    )
    assert all_brand["proposed_spend"] == Decimal("42.80")
    assert all_brand["excluded_non_brand_count"] == 0


@pytest.mark.django_db(transaction=True)
class TestCheckinReceiptOcr(AmbassadorsGraphQLTestCase):
    @pytest.fixture(autouse=True)
    def setup(self, db, settings):
        cache.clear()
        settings.OPENAI_API_KEY = "test-key"
        settings.OPENAI_MODEL = "gpt-4o-mini"

        self.roles = self.setup_default_roles()
        self.tenant = self.create_tenant(name="Brew Dr Kombucha")
        self.actor = self.create_user(
            username="actor-receipt-ocr@test.com",
            email="actor-receipt-ocr@test.com",
            role=self.roles["spark_admin"],
        )
        ba_user = self.create_user(
            username="ba-receipt-ocr@test.com",
            email="ba-receipt-ocr@test.com",
            role=self.roles["ambassador"],
            first_name="Elena",
            last_name="Vance",
        )
        self.ba = self.create_ambassador(ba_user, phone="555-301-9988")
        self.retail_type = self.create_event_type("Retail Sampling", self.tenant)
        self.template = CustomRecapTemplate.objects.create(
            tenant=self.tenant,
            name="Retail Recap",
            event_type=self.retail_type,
            product_samples=True,
            created_by=self.actor,
        )
        self.retailer = Retailer.objects.create(
            tenant=self.tenant,
            name="Sprouts Farmers Market",
            created_by=self.actor,
        )
        self.event = self.create_event(
            name="Sprouts Farmers Market #412",
            tenant=self.tenant,
            address="4006 S Lamar Blvd, Austin, TX 78704",
            event_type=self.retail_type,
            date=datetime(2026, 10, 16, 12, 0, tzinfo=dt_timezone.utc),
        )
        self.event.retailer = self.retailer
        self.event.custom_recap_template = self.template
        self.event.walkup_code = "CK-RCPT1"
        self.event.save(
            update_fields=["retailer", "custom_recap_template", "walkup_code"]
        )

        ptype = ProductType.objects.create(
            name="Kombucha", tenant=self.tenant, created_by=self.actor
        )
        Product.objects.create(
            name="Peach", product_type=ptype, tenant=self.tenant, created_by=self.actor
        )
        Product.objects.create(
            name="Classic",
            product_type=ptype,
            tenant=self.tenant,
            created_by=self.actor,
        )

        number, _ = CustomRecapFieldType.objects.get_or_create(
            name="number", defaults={"created_by": self.actor}
        )
        section = RecapSection.objects.create(
            tenant=self.tenant, name="Spend", order=0, created_by=self.actor
        )
        self.spend_field = CustomField.objects.create(
            name="Product Spend",
            custom_recap_template=self.template,
            custom_field_type=number,
            recap_section=section,
            order=0,
            created_by=self.actor,
        )
        self.file_type, _ = FileType.objects.get_or_create(
            name="image", defaults={"created_by": self.actor}
        )
        self.receipt_cat = FileRecapCategory.objects.create(
            name="Product Spend", tenant=self.tenant, created_by=self.actor
        )

        self.http = DjangoClient()
        self.token = make_checkin_session_token(self.event.id, self.ba.id)

    def _post(self, image_b64: str | None = None, **extra):
        return self.http.post(
            reverse(
                "events.public_checkin_receipt_read",
                kwargs={"code": self.event.walkup_code},
            ),
            data={
                "session": self.token,
                "imageBase64": _jpeg_b64() if image_b64 is None else image_b64,
                "contentType": "image/jpeg",
                **extra,
            },
            content_type="application/json",
        )

    def _mock_vision(self, monkeypatch, payload: dict):
        calls = []

        def post(url, body, headers, timeout):
            calls.append(body)
            return _completion(payload)

        monkeypatch.setattr("utils.ai_text._post_json", post)
        return calls

    def test_proposes_brand_spend_excluding_non_brand_item_with_clean_shift_match(
        self, monkeypatch
    ):
        calls = self._mock_vision(monkeypatch, _vision_receipt())
        res = self._post()

        assert res.status_code == 200
        body = res.json()
        assert body["available"] is True
        assert body["proposedSpend"] == "42.80"
        assert body["receiptTotal"] == "46.29"
        assert body["brandProductSubtotal"] == "39.96"
        assert body["excludedNonBrandTotal"] == "3.49"
        assert body["excludedNonBrandCount"] == 1
        assert body["merchant"] == "Sprouts Farmers Market"
        assert body["storeNumber"] == "412"
        assert body["receiptDate"] == "2026-10-16"
        assert body["advisories"] == []
        assert len(calls) == 1
        prompt_text = json.dumps(calls[0]["messages"])
        for pii in ("Elena", "Vance", "9988", self.token):
            assert pii not in prompt_text

    def test_flags_store_and_date_mismatch_advisories_without_blocking(
        self, monkeypatch
    ):
        self._mock_vision(
            monkeypatch,
            _vision_receipt(
                merchant="Target",
                store_number="903",
                receipt_date="2026-10-12",
                total="32.10",
                subtotal="29.72",
                tax="2.38",
                items=[
                    {
                        "name": "Brew Dr Peach",
                        "qty": "8",
                        "unitPrice": "3.715",
                        "amount": "29.72",
                        "isBrandProduct": True,
                    }
                ],
            ),
        )
        res = self._post()

        assert res.status_code == 200
        body = res.json()
        assert body["available"] is True
        assert body["proposedSpend"] == "32.10"
        kinds = [a["kind"] for a in body["advisories"]]
        assert "store_mismatch" in kinds
        assert "date_mismatch" in kinds
        store_msg = next(
            a["message"] for a in body["advisories"] if a["kind"] == "store_mismatch"
        )
        date_msg = next(
            a["message"] for a in body["advisories"] if a["kind"] == "date_mismatch"
        )
        assert "Target #903" in store_msg
        assert "Sprouts Farmers Market #412" in store_msg
        assert "Oct 12, 2026" in date_msg
        assert "Oct 16, 2026" in date_msg

    def test_flags_store_number_mismatch_on_same_chain(self, monkeypatch):
        self._mock_vision(
            monkeypatch,
            _vision_receipt(merchant="Sprouts Farmers Market", store_number="999"),
        )
        body = self._post().json()
        kinds = [a["kind"] for a in body["advisories"]]
        assert kinds == ["store_mismatch"]

    def test_same_street_address_suppresses_false_store_mismatch(self, monkeypatch):
        self._mock_vision(
            monkeypatch,
            _vision_receipt(
                merchant="SFM Grocery Ops LLC",
                store_number="",
                address="4006 South Lamar Boulevard, Austin, TX 78704",
            ),
        )
        body = self._post().json()
        assert body["advisories"] == []

    def test_flags_duplicate_receipt_by_ocr_ticket_hash_across_recaps(
        self, monkeypatch
    ):
        other_event = self.create_event(
            name="Sprouts #412 Prior",
            tenant=self.tenant,
            address="4006 S Lamar Blvd, Austin, TX 78704",
            event_type=self.retail_type,
            date=datetime(2026, 10, 12, 12, 0, tzinfo=dt_timezone.utc),
        )
        other_recap = CustomRecap.objects.create(
            tenant=self.tenant,
            event=other_event,
            custom_recap_template=self.template,
            created_by=self.actor,
        )
        other_file = CustomRecapFile.objects.create(
            name="prior.jpg",
            url="recap_files/prior.jpg",
            custom_recap=other_recap,
            file_type=self.file_type,
            file_recap_category=self.receipt_cat,
            created_by=self.actor,
        )
        keys = receipt_keys(
            store_number="412",
            merchant="Sprouts Farmers Market",
            ticket="884120",
            receipt_date="2026-10-16",
            total=Decimal("46.29"),
        )
        ReceiptReading.objects.create(
            custom_recap_file=other_file,
            status=ReceiptReading.STATUS_READ,
            total=Decimal("46.29"),
            merchant="Sprouts Farmers Market",
            store_number="412",
            receipt_date="2026-10-16",
            ticket_number="884120",
            **keys,
        )

        self._mock_vision(monkeypatch, _vision_receipt())
        body = self._post(image_b64=_jpeg_b64(77)).json()

        dup = [a for a in body["advisories"] if a["kind"] == "duplicate_receipt"]
        assert len(dup) == 1
        assert dup[0]["matchedOn"] == "ocr_key"
        assert "Oct 12, 2026" in dup[0]["message"]

    def test_falls_back_to_google_vision_ocr_when_openai_unavailable(
        self, monkeypatch, settings
    ):
        settings.OPENAI_API_KEY = ""

        def fake_ocr(_bytes: bytes) -> OcrResult:
            return OcrResult(
                ok=True,
                text=(
                    "SPROUTS FARMERS MARKET #412\n"
                    "Ticket #771234\n"
                    "BREW DR PEACH   19.98\n"
                    "ICE BAG 7LB      3.50\n"
                    "TAX              1.90\n"
                    "TOTAL           25.38\n"
                    "10/16/2026\n"
                ),
                store="SPROUTS FARMERS MARKET #412",
                amount=Decimal("25.38"),
                purchase_date=date(2026, 10, 16),
            )

        monkeypatch.setattr(checkin_receipt_ocr, "run_ocr", fake_ocr)
        res = self._post()

        assert res.status_code == 200
        body = res.json()
        assert body["available"] is True
        # 25.38 - 3.50 non-brand ice = 21.88
        assert body["proposedSpend"] == "21.88"
        assert body["receiptTotal"] == "25.38"
        assert body["excludedNonBrandTotal"] == "3.50"
        assert body["excludedNonBrandCount"] == 1
        assert body["advisories"] == []

    def test_missing_all_ocr_credentials_degrades_cleanly_and_still_catches_sha256_duplicate(
        self, monkeypatch, settings
    ):
        settings.OPENAI_API_KEY = ""
        monkeypatch.setattr(
            checkin_receipt_ocr,
            "run_ocr",
            lambda _b: OcrResult(ok=False, reason="Vision disabled"),
        )

        raw = _jpeg_bytes(91)
        sha = hashlib.sha256(raw).hexdigest()
        other_event = self.create_event(
            name="Earlier Shift",
            tenant=self.tenant,
            address="100 Congress Ave, Austin, TX 78701",
            event_type=self.retail_type,
            date=datetime(2026, 10, 9, 12, 0, tzinfo=dt_timezone.utc),
        )
        other_recap = CustomRecap.objects.create(
            tenant=self.tenant,
            event=other_event,
            custom_recap_template=self.template,
            created_by=self.actor,
        )
        CustomRecapFile.objects.create(
            name="dup.jpg",
            url="recap_files/dup.jpg",
            content_sha256=sha,
            custom_recap=other_recap,
            file_type=self.file_type,
            file_recap_category=self.receipt_cat,
            created_by=self.actor,
        )

        res = self._post(image_b64=base64.b64encode(raw).decode("ascii"))
        assert res.status_code == 200
        body = res.json()
        assert body["available"] is False
        assert body["proposedSpend"] is None
        assert body["message"] == RECEIPT_UNREADABLE
        assert len(body["advisories"]) == 1
        assert body["advisories"][0]["kind"] == "duplicate_receipt"
        assert body["advisories"][0]["matchedOn"] == "sha256"

    def test_same_shift_recap_does_not_self_flag_duplicate(self, monkeypatch, settings):
        settings.OPENAI_API_KEY = ""
        monkeypatch.setattr(
            checkin_receipt_ocr,
            "run_ocr",
            lambda _b: OcrResult(ok=False, reason="Vision disabled"),
        )
        raw = _jpeg_bytes(44)
        sha = hashlib.sha256(raw).hexdigest()
        own_recap = CustomRecap.objects.create(
            tenant=self.tenant,
            event=self.event,
            ambassador=self.ba,
            custom_recap_template=self.template,
            created_by=self.actor,
        )
        CustomRecapFile.objects.create(
            name="own.jpg",
            url="recap_files/own.jpg",
            content_sha256=sha,
            custom_recap=own_recap,
            file_type=self.file_type,
            file_recap_category=self.receipt_cat,
            created_by=self.actor,
        )
        body = self._post(image_b64=base64.b64encode(raw).decode("ascii")).json()
        assert body["advisories"] == []

    def test_slow_upstream_times_out_cleanly_and_skips_fallback_after_deadline(
        self, monkeypatch, settings
    ):
        # Use a non-fallback configured model to ensure neither the OpenAI
        # model-fallback retry nor Google Vision run_ocr starts once the
        # outer deadline has expired.
        settings.OPENAI_MODEL = "gpt-5.4"
        monkeypatch.setattr(checkin_receipt_ocr, "MODEL_DEADLINE_SECONDS", 0.25)
        openai_calls: list[float] = []
        ocr_calls: list[float] = []

        def slow(*_a, **kwargs):
            openai_calls.append(float(kwargs.get("timeout") or 0))
            time.sleep(0.35)
            raise RuntimeError("upstream timed out")

        def tracked_ocr(_b, *, timeout=25):
            ocr_calls.append(float(timeout))
            return OcrResult(ok=False, reason="skip")

        monkeypatch.setattr("utils.ai_text._post_json", slow)
        monkeypatch.setattr(checkin_receipt_ocr, "run_ocr", tracked_ocr)
        started = time.monotonic()
        res = self._post()

        assert time.monotonic() - started < 1.0
        assert res.status_code == 200
        assert res.json()["available"] is False
        # Wait briefly so the daemon worker finishes its sleep and verify it
        # did NOT start a second OpenAI fallback attempt or a Vision OCR call.
        time.sleep(0.2)
        assert len(openai_calls) == 1
        assert openai_calls[0] <= 0.25
        assert ocr_calls == []

    def test_requires_valid_session_and_enforces_rate_limits(self, monkeypatch):
        self._mock_vision(monkeypatch, _vision_receipt())
        unauth = self.http.post(
            reverse(
                "events.public_checkin_receipt_read",
                kwargs={"code": self.event.walkup_code},
            ),
            data={"session": "", "imageBase64": _jpeg_b64()},
            content_type="application/json",
        )
        assert unauth.status_code == 401

        monkeypatch.setattr(checkin_views, "RECEIPT_READ_SESSION_LIMIT", 2)
        statuses = [
            self._post(image_b64=_jpeg_b64(10 + i)).status_code for i in range(3)
        ]
        assert statuses == [200, 200, 429]

    def test_saves_nothing_and_recap_still_submits_with_advisories(self, monkeypatch):
        monkeypatch.setattr(checkin_web, "_finalize_recap_offthread", lambda _id: None)
        self._mock_vision(
            monkeypatch,
            _vision_receipt(merchant="Kroger", receipt_date="2026-10-01"),
        )
        body = self._post().json()
        assert body["available"] is True
        assert len(body["advisories"]) == 2
        assert CustomRecap.objects.count() == 0
        assert CustomFieldValue.objects.count() == 0

        blob = f"recap_files/checkin/{self.event.uuid}/receipt.jpg"
        submit_res = self.http.post(
            reverse(
                "events.public_checkin_recap",
                kwargs={"code": self.event.walkup_code},
            ),
            data={
                "session": self.token,
                "fieldValues": [
                    {"customFieldId": str(self.spend_field.id), "value": "42.80"},
                ],
                "files": [{"blobName": blob, "category": str(self.receipt_cat.id)}],
                "usedCorpoCard": True,
            },
            content_type="application/json",
        )
        assert submit_res.status_code == 200, submit_res.content
        assert CustomRecap.objects.count() == 1
