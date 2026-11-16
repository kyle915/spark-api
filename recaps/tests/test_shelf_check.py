"""The on-shelf check: reading shelf photos, staff corrections, brand summary.

Runs against the real ``schema_clients`` surface with the web client's own
documents (same field set), the real cron view and the real reader. The
OpenAI call is always mocked at ``utils.ai_text._post_json``; storage is
mocked at ``utils.gcs.download_blob_bytes``.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from decimal import Decimal
from io import BytesIO
from unittest.mock import patch

import pytest
from asgiref.sync import sync_to_async
from django.test import Client
from django.utils import timezone
from PIL import Image

from ambassadors.tests.base import AmbassadorsGraphQLTestCase
from events import models as event_models
from recaps import models as recap_models
from recaps import shelf_check
from recaps.shelf_check import (
    effective_answers,
    interpret_shelf,
    read_pending_shelf_photos,
    visit_answer,
)
from utils.ai_text import AiUnavailable

# ── the web client's documents, field for field ─────────────────────

PHOTO_FIELDS = """
    fileId
    fileUrl
    bucketName
    status
    reading { productOnShelf facings priceTagVisible price outOfStockGap }
    correction {
      answers { productOnShelf facings priceTagVisible price outOfStockGap }
      note
      correctedByName
      correctedAt
    }
"""

READINGS = (
    """
query RecapShelfReadingsQuery($recapId: ID!) {
  recapShelfReadings(recapId: $recapId) {
    allowed
    message
    photos { %s }
  }
}
"""
    % PHOTO_FIELDS
)

READ_NOW = (
    """
mutation ReadRecapShelfPhotosMutation($recapId: ID!) {
  readRecapShelfPhotos(recapId: $recapId) {
    allowed
    message
    photos { %s }
  }
}
"""
    % PHOTO_FIELDS
)

CORRECT = (
    """
mutation CorrectShelfReadingMutation($input: CorrectShelfReadingInput!) {
  correctShelfReading(input: $input) {
    success
    message
    photo { %s }
  }
}
"""
    % PHOTO_FIELDS
)

SUMMARY = """
query ShelfSummaryQuery($tenantId: ID!, $startDate: String, $endDate: String) {
  shelfSummary(tenantId: $tenantId, startDate: $startDate, endDate: $endDate) {
    status
    visits
    onShelf
    notFound
    unclear
    priceTagVisible
    unreadVisits
    notFoundStores { storeName eventDate }
    notFoundMore
  }
}
"""


def _jpeg() -> bytes:
    out = BytesIO()
    Image.new("RGB", (64, 48), (120, 120, 120)).save(out, "JPEG")
    return out.getvalue()


def _answer(**over) -> dict:
    base = {
        "isShelfPhoto": True,
        "productOnShelf": "yes",
        "facings": 6,
        "facingsCountClear": True,
        "priceTagVisible": "yes",
        "price": "3.49",
        "priceLegible": True,
        "outOfStockGap": "no",
    }
    return {**base, **over}


def _reply(payload: dict) -> dict:
    return {"choices": [{"message": {"content": json.dumps(payload)}}]}


# ─── Pure ───────────────────────────────────────────────────────────


def test_interpret_keeps_only_what_the_model_was_sure_of():
    out = interpret_shelf(_answer())
    assert out == {
        "status": "read",
        "product_on_shelf": "yes",
        "facings": 6,
        "price_tag_visible": "yes",
        "price": Decimal("3.49"),
        "out_of_stock_gap": "no",
    }
    # A count that was not clear, and a price that was not legible, are blank.
    assert interpret_shelf(_answer(facingsCountClear=False))["facings"] is None
    assert interpret_shelf(_answer(priceLegible=False))["price"] is None
    assert interpret_shelf(_answer(price="349000"))["price"] is None
    assert interpret_shelf(_answer(facings=400))["facings"] is None
    # No facing count without the product, no price without a tag.
    assert interpret_shelf(_answer(productOnShelf="no"))["facings"] is None
    assert interpret_shelf(_answer(priceTagVisible="unclear"))["price"] is None


def test_unclear_is_an_answer_and_an_odd_value_becomes_unclear():
    out = interpret_shelf(
        _answer(productOnShelf="unclear", priceTagVisible="maybe", outOfStockGap="unclear")
    )
    assert out["status"] == "read"
    assert out["product_on_shelf"] == "unclear"
    assert out["price_tag_visible"] == "unclear"
    assert out["out_of_stock_gap"] == "unclear"


def test_not_a_shelf_photo_and_a_failed_call_are_told_apart():
    assert interpret_shelf(_answer(isShelfPhoto=False))["status"] == "unreadable"
    assert interpret_shelf(None)["status"] == "failed"


def test_a_visit_is_not_found_only_when_every_photo_says_no():
    def photo(on_shelf, tag="unclear"):
        return {"product_on_shelf": on_shelf, "price_tag_visible": tag}

    assert visit_answer([photo("no"), photo("yes")])["product_on_shelf"] == "yes"
    assert visit_answer([photo("no"), photo("no")])["product_on_shelf"] == "no"
    assert visit_answer([photo("no"), photo("unclear")])["product_on_shelf"] == "unclear"
    assert visit_answer([photo("no"), photo("no", "yes")])["price_tag_visible"] == "yes"


# ─── Through the database, the cron view and the schema ─────────────


@pytest.mark.django_db(transaction=True)
class TestShelfCheck(AmbassadorsGraphQLTestCase):
    @pytest.fixture(autouse=True)
    def setup(self, db, settings):
        from config.schema_client import schema_clients

        settings.OPENAI_API_KEY = "test-key-not-real"
        settings.OPENAI_MODEL = "gpt-4o-mini"
        settings.INTERNAL_CRON_SECRET = "cron-secret"

        self.roles = self.setup_default_roles()
        self.schema = schema_clients
        self.endpoint_path = "/api/v1/graphql/clients"
        self.sys = self.get_system_user()

        self.tenant = self.create_tenant(name="Alpha Seltzer")
        self.other = self.create_tenant(name="Beta Brand")
        self.admin = self.create_user(
            username="shelf-admin", email="shelf-admin@test.com", role=self.roles["spark_admin"]
        )
        self.admin.first_name, self.admin.last_name = "Sam", "Staff"
        self.admin.save()
        self.client_user = self.create_user(
            username="shelf-client", email="shelf-client@test.com", role=self.roles["client"]
        )
        self.create_tenanted_user(self.client_user, self.tenant)
        ba_user = self.create_user(
            username="shelf-ba", email="shelf-ba@test.com", role=self.roles["ambassador"]
        )
        ba_user.first_name, ba_user.last_name = "Jordan", "Rivera"
        ba_user.save()
        self.ba = self.create_ambassador(ba_user)

        ptype = event_models.ProductType.objects.create(
            name="Seltzer", tenant=self.tenant, created_by=self.sys
        )
        for name in ("Mango 12oz", "Lime 12oz"):
            event_models.Product.objects.create(
                name=name, product_type=ptype, tenant=self.tenant, created_by=self.sys
            )

        self.file_type = recap_models.FileType.objects.create(name="image", created_by=self.sys)
        self.shelf = recap_models.FileRecapCategory.objects.create(
            name="On Shelf", tenant=self.tenant, created_by=self.sys
        )
        self.table = recap_models.FileRecapCategory.objects.create(
            name="Table Set Up", tenant=self.tenant, created_by=self.sys
        )
        self.retail_type, self.retail_template = self._program(self.tenant, "Retail Sampling")
        self.festival_type, self.festival_template = self._program(self.tenant, "Festival")

        self.today = timezone.now().date()
        self.start = (self.today - timedelta(days=10)).isoformat()
        self.end = self.today.isoformat()

        # No test may reach OpenAI or storage unless it says what they return.
        self.post_patch = patch(
            "utils.ai_text._post_json", side_effect=AiUnavailable("blocked in tests")
        )
        self.post = self.post_patch.start()
        self.download_patch = patch("utils.gcs.download_blob_bytes", return_value=_jpeg())
        self.download = self.download_patch.start()
        yield
        self.post_patch.stop()
        self.download_patch.stop()

    # ── fixtures ──────────────────────────────────────────────────────

    def _program(self, tenant, name):
        etype = event_models.EventType.objects.create(
            name=name, slug=f"shelf-{name.lower().replace(' ', '-')}-{tenant.id}",
            tenant=tenant, created_by=self.sys,
        )
        template = recap_models.CustomRecapTemplate.objects.create(
            name=f"{name} form", event_type=etype, tenant=tenant, created_by=self.sys
        )
        return etype, template

    def _recap(self, store, *, approved=True, days_ago=2, retail=True, tenant=None):
        tenant = tenant or self.tenant
        when = timezone.make_aware(
            datetime.combine(self.today - timedelta(days=days_ago), datetime.min.time())
        ) + timedelta(hours=14)
        etype = self.retail_type if retail else self.festival_type
        template = self.retail_template if retail else self.festival_template
        event = event_models.Event.objects.create(
            name=store, tenant=tenant, address="1 Main St", date=when,
            event_type=etype, created_by=self.sys, updated_by=self.sys,
        )
        return recap_models.CustomRecap.objects.create(
            name=f"recap {store}", approved=approved, submitted_at=when, event=event,
            tenant=tenant, custom_recap_template=template, ambassador=self.ba,
            created_by=self.sys,
        )

    def _photo(self, recap, name, category=None):
        return recap_models.CustomRecapFile.objects.create(
            name=name, url=f"recap_files/{name}", custom_recap=recap,
            file_type=self.file_type, file_recap_category=category or self.shelf,
            created_by=self.sys,
        )

    def _reading(self, recap_file, on_shelf="yes", tag="unclear", **over):
        return recap_models.ShelfReading.objects.create(
            custom_recap_file=recap_file, status="read", product_on_shelf=on_shelf,
            price_tag_visible=tag, **over,
        )

    def _model_says(self, *answers):
        queue = list(answers)
        self.post.side_effect = lambda url, payload, headers, timeout: _reply(
            queue.pop(0) if len(queue) > 1 else queue[0]
        )

    async def _run(self, document, variables, user):
        result = await self._execute_query_authenticated(
            document, variables, user, self.endpoint_path
        )
        assert result.errors is None, result.errors
        return result.data

    async def _summary(self, user, tenant=None):
        data = await self._run(
            SUMMARY,
            {"tenantId": str((tenant or self.tenant).id), "startDate": self.start, "endDate": self.end},
            user,
        )
        return data["shelfSummary"]

    # ── reading ───────────────────────────────────────────────────────

    def test_reads_shelf_photos_on_retail_recaps_once_each(self):
        retail = self._recap("Kroger 12")
        festival = self._recap("Summer Fest", retail=False)
        s1 = self._photo(retail, "shelf1.jpg")
        s2 = self._photo(retail, "shelf2.jpg")
        self._photo(retail, "table.jpg", self.table)   # another bucket
        self._photo(retail, "shelf.pdf")               # not an image
        self._photo(festival, "fest-shelf.jpg")        # not a retail recap
        self._model_says(_answer(), _answer(productOnShelf="unclear", priceTagVisible="no"))

        first = read_pending_shelf_photos(limit=10)
        second = read_pending_shelf_photos(limit=10)

        assert self.post.call_count == 2  # the two shelf images, once each
        assert first["read"] == 2 and first["remaining"] == 0
        assert second["read"] == 0
        rows = {r.custom_recap_file_id: r for r in recap_models.ShelfReading.objects.all()}
        assert set(rows) == {s1.id, s2.id}
        assert {r.product_on_shelf for r in rows.values()} == {"yes", "unclear"}

    def test_model_is_sent_the_brand_and_products_and_told_to_ignore_people(self):
        self._photo(self._recap("Kroger 12"), "shelf1.jpg")
        self._model_says(_answer())
        read_pending_shelf_photos(limit=10)

        payload = self.post.call_args.args[1]
        system = payload["messages"][0]["content"]
        text = payload["messages"][1]["content"][0]["text"]
        assert "Alpha Seltzer" in text and "Mango 12oz" in text and "Lime 12oz" in text
        assert "Do not identify, describe or mention any person" in system
        for private in ("Jordan", "Rivera", "Kroger", "Main St"):
            assert private not in text and private not in system
        assert payload["messages"][1]["content"][1]["type"] == "image_url"

    def test_each_run_stops_at_its_photo_budget(self):
        recap = self._recap("Kroger 12")
        for i in range(5):
            self._photo(recap, f"s{i}.jpg")
        self._model_says(_answer())

        report = read_pending_shelf_photos(limit=2)

        assert self.post.call_count == 2
        assert report["read"] == 2 and report["remaining"] == 3

    def test_each_run_stops_at_its_time_budget(self, monkeypatch):
        recap = self._recap("Kroger 12")
        for i in range(4):
            self._photo(recap, f"s{i}.jpg")
        self._model_says(_answer())
        clock = iter([0.0, 1.0, 999.0, 999.0, 999.0, 999.0])
        monkeypatch.setattr(shelf_check.time, "monotonic", lambda: next(clock))

        report = shelf_check._read_files(
            shelf_check.pending_shelf_files(), limit=10, max_seconds=30
        )

        assert report["stopped"] == "time"
        assert self.post.call_count == 1

    def test_a_failing_model_stops_the_run_and_is_retried_once_more_only(self):
        recap = self._recap("Kroger 12")
        for i in range(6):
            self._photo(recap, f"s{i}.jpg")

        first = read_pending_shelf_photos(limit=10)  # transport raises: 3 failures
        assert first["failed"] == 3 and first["stopped"] == "failures"
        assert self.post.call_count == 3
        assert set(recap_models.ShelfReading.objects.values_list("status", flat=True)) == {"failed"}

        for _ in range(6):
            read_pending_shelf_photos(limit=10)
        # Six photos, two tries each, then they are left alone.
        assert self.post.call_count == 12
        assert read_pending_shelf_photos(limit=10)["remaining"] == 0

    def test_no_key_reads_nothing_and_stores_nothing(self, settings):
        settings.OPENAI_API_KEY = ""
        self._photo(self._recap("Kroger 12"), "shelf1.jpg")

        report = read_pending_shelf_photos(limit=10)

        assert report["ai_unavailable"] is True
        assert self.post.call_count == 0
        assert recap_models.ShelfReading.objects.count() == 0

    def test_cron_view_needs_the_secret_and_caps_the_run(self):
        recap = self._recap("Kroger 12")
        for i in range(3):
            self._photo(recap, f"s{i}.jpg")
        self._model_says(_answer())
        http = Client()

        assert http.post("/internal/cron/read-shelf-photos").status_code in (401, 403)
        res = http.post(
            "/internal/cron/read-shelf-photos",
            data={"limit": "2"},
            HTTP_X_CRON_SECRET="cron-secret",
        )
        assert res.status_code == 200, res.content
        assert res.json()["report"]["read"] == 2
        assert res.json()["report"]["remaining"] == 1
        # An absurd limit is cut to the ceiling, not honoured.
        assert shelf_check.MAX_RUN_LIMIT == 200

    # ── staff review ──────────────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_staff_see_each_photo_and_can_read_one_recap_on_demand(self):
        recap = await sync_to_async(self._recap)("Kroger 12", approved=False)
        photo = await sync_to_async(self._photo)(recap, "shelf1.jpg")
        self._model_says(_answer())

        before = (await self._run(READINGS, {"recapId": str(recap.id)}, self.admin))[
            "recapShelfReadings"
        ]
        assert before["allowed"] is True
        assert [p["status"] for p in before["photos"]] == ["pending"]
        assert self.post.call_count == 0  # looking reads nothing

        after = (await self._run(READ_NOW, {"recapId": str(recap.id)}, self.admin))[
            "readRecapShelfPhotos"
        ]
        assert after["message"] == "Read 1 photo."
        assert after["photos"][0] == {
            "fileId": str(photo.id),
            "fileUrl": after["photos"][0]["fileUrl"],
            "bucketName": "On Shelf",
            "status": "read",
            "reading": {
                "productOnShelf": "yes",
                "facings": 6,
                "priceTagVisible": "yes",
                "price": "3.49",
                "outOfStockGap": "no",
            },
            "correction": None,
        }
        # A second click does not read the photo again.
        again = (await self._run(READ_NOW, {"recapId": str(recap.id)}, self.admin))[
            "readRecapShelfPhotos"
        ]
        assert again["message"] == "No photos left to read."
        assert self.post.call_count == 1

    @pytest.mark.asyncio
    async def test_staff_correction_is_stored_apart_and_wins(self):
        recap = await sync_to_async(self._recap)("Kroger 12")
        photo = await sync_to_async(self._photo)(recap, "shelf1.jpg")
        await sync_to_async(self._reading)(photo, on_shelf="no")

        assert (await self._summary(self.admin))["notFound"] == 1

        data = await self._run(
            CORRECT,
            {
                "input": {
                    "fileId": str(photo.id),
                    "productOnShelf": "yes",
                    "facings": 4,
                    "priceTagVisible": "yes",
                    "price": "2.99",
                    "outOfStockGap": "no",
                    "note": "Bottom shelf, behind the endcap. Jordan confirmed.",
                }
            },
            self.admin,
        )
        payload = data["correctShelfReading"]
        assert payload["success"] is True
        assert payload["photo"]["correction"]["answers"]["productOnShelf"] == "yes"
        assert payload["photo"]["correction"]["correctedByName"] == "Sam Staff"
        # What was read is still on record.
        assert payload["photo"]["reading"]["productOnShelf"] == "no"
        reading = await sync_to_async(recap_models.ShelfReading.objects.get)(
            custom_recap_file=photo
        )
        assert reading.product_on_shelf == "no"

        summary = await self._summary(self.admin)
        assert (summary["onShelf"], summary["notFound"], summary["priceTagVisible"]) == (1, 0, 1)

        # A later run of the reader cannot undo it.
        self._model_says(_answer(productOnShelf="no"))
        await sync_to_async(read_pending_shelf_photos)(limit=10)
        assert self.post.call_count == 0
        assert (await self._summary(self.admin))["onShelf"] == 1

    @pytest.mark.asyncio
    async def test_a_correction_must_be_yes_no_or_unclear(self):
        recap = await sync_to_async(self._recap)("Kroger 12")
        photo = await sync_to_async(self._photo)(recap, "shelf1.jpg")
        data = await self._run(
            CORRECT,
            {
                "input": {
                    "fileId": str(photo.id),
                    "productOnShelf": "probably",
                    "priceTagVisible": "no",
                    "outOfStockGap": "no",
                }
            },
            self.admin,
        )
        assert data["correctShelfReading"]["success"] is False
        assert await sync_to_async(recap_models.ShelfReadingCorrection.objects.count)() == 0

    @pytest.mark.asyncio
    async def test_brand_user_gets_no_per_photo_rows_and_cannot_read_or_correct(self):
        recap = await sync_to_async(self._recap)("Kroger 12")
        photo = await sync_to_async(self._photo)(recap, "shelf1.jpg")
        await sync_to_async(self._reading)(photo)
        await sync_to_async(shelf_check.save_correction)(
            photo, product_on_shelf="yes", facings=3, price_tag_visible="no", price=None,
            out_of_stock_gap="no", note="STAFF ONLY NOTE", user=self.admin,
        )
        self._model_says(_answer())

        seen = (await self._run(READINGS, {"recapId": str(recap.id)}, self.client_user))[
            "recapShelfReadings"
        ]
        assert seen == {"allowed": False, "message": "", "photos": []}
        read = (await self._run(READ_NOW, {"recapId": str(recap.id)}, self.client_user))[
            "readRecapShelfPhotos"
        ]
        assert read["allowed"] is False and read["photos"] == []
        assert self.post.call_count == 0

        result = await self._execute_query_authenticated(
            CORRECT,
            {
                "input": {
                    "fileId": str(photo.id),
                    "productOnShelf": "no",
                    "priceTagVisible": "no",
                    "outOfStockGap": "no",
                }
            },
            self.client_user,
            self.endpoint_path,
        )
        assert result.errors
        correction = await sync_to_async(recap_models.ShelfReadingCorrection.objects.get)(
            custom_recap_file=photo
        )
        assert correction.product_on_shelf == "yes"

        # The summary a brand user does get carries no note and no names.
        summary = await self._summary(self.client_user)
        blob = json.dumps(summary)
        assert "STAFF ONLY NOTE" not in blob
        assert "Jordan" not in blob and "Rivera" not in blob and "Sam" not in blob

    # ── brand summary ─────────────────────────────────────────────────

    def _seed_summary(self):
        found = self._recap("Kroger 12", days_ago=2)
        self._reading(self._photo(found, "a.jpg"), "yes", "yes", facings=5, price=Decimal("3.49"))
        missing = self._recap("Safeway 88", days_ago=3)
        self._reading(self._photo(missing, "b.jpg"), "no", "no")
        self._reading(self._photo(missing, "b2.jpg"), "no", "unclear")
        blurry = self._recap("Target 4", days_ago=4)
        self._reading(self._photo(blurry, "c.jpg"), "unclear")
        # Approved, shelf photo not read yet.
        self._photo(self._recap("Publix 9", days_ago=5), "d.jpg")
        # Not approved: product missing, but a brand must not see it.
        pending = self._recap("PENDING STORE", approved=False, days_ago=2)
        self._reading(self._photo(pending, "e.jpg"), "no")
        # Archived after approval.
        archived = self._recap("ARCHIVED STORE", days_ago=2)
        self._reading(self._photo(archived, "f.jpg"), "no")
        recap_models.CustomRecap.objects.filter(id=archived.id).update(
            archived_at=timezone.now()
        )
        # Before the window.
        old = self._recap("OLD STORE", days_ago=40)
        self._reading(self._photo(old, "g.jpg"), "no")
        # Not a retail visit.
        fest = self._recap("FESTIVAL STORE", retail=False)
        self._reading(self._photo(fest, "h.jpg"), "no")
        # Another brand.
        other_type, other_template = self._program(self.other, "Retail Sampling")
        other_shelf = recap_models.FileRecapCategory.objects.create(
            name="On Shelf", tenant=self.other, created_by=self.sys
        )
        when = timezone.now() - timedelta(days=2)
        other_event = event_models.Event.objects.create(
            name="BETA STORE", tenant=self.other, address="2 Main St", date=when,
            event_type=other_type, created_by=self.sys, updated_by=self.sys,
        )
        other_recap = recap_models.CustomRecap.objects.create(
            name="beta", approved=True, submitted_at=when, event=other_event,
            tenant=self.other, custom_recap_template=other_template, created_by=self.sys,
        )
        self._reading(self._photo(other_recap, "i.jpg", other_shelf), "no")

    @pytest.mark.asyncio
    async def test_brand_summary_counts_approved_retail_visits_only(self):
        await sync_to_async(self._seed_summary)()

        summary = await self._summary(self.client_user)

        assert summary == {
            "status": "ready",
            "visits": 3,
            "onShelf": 1,
            "notFound": 1,
            "unclear": 1,
            "priceTagVisible": 1,
            "unreadVisits": 1,
            "notFoundStores": [
                {
                    "storeName": "Safeway 88",
                    "eventDate": (self.today - timedelta(days=3)).isoformat(),
                }
            ],
            "notFoundMore": 0,
        }
        assert self.post.call_count == 0  # counted from stored answers

    @pytest.mark.asyncio
    async def test_brand_user_cannot_read_another_brands_summary(self):
        await sync_to_async(self._seed_summary)()

        refused = await self._summary(self.client_user, self.other)
        assert refused["status"] == "unavailable" and refused["visits"] == 0
        assert refused["notFoundStores"] == []

        staff = await self._summary(self.admin, self.other)
        assert staff["notFound"] == 1
        assert staff["notFoundStores"][0]["storeName"] == "BETA STORE"

    @pytest.mark.asyncio
    async def test_summary_says_none_when_nothing_has_an_answer(self):
        await sync_to_async(self._photo)(
            await sync_to_async(self._recap)("Publix 9"), "d.jpg"
        )
        summary = await self._summary(self.client_user)
        assert summary["status"] == "none"
        assert summary["visits"] == 0 and summary["unreadVisits"] == 1

    def test_effective_answer_prefers_the_correction(self):
        photo = self._photo(self._recap("Kroger 12"), "a.jpg")
        reading = self._reading(photo, "no")
        assert effective_answers(reading, None)["product_on_shelf"] == "no"
        correction = shelf_check.save_correction(
            photo, product_on_shelf="yes", facings=2, price_tag_visible="unclear",
            price=None, out_of_stock_gap="unclear", user=self.admin,
        )
        assert effective_answers(reading, correction)["product_on_shelf"] == "yes"
        assert effective_answers(None, None) is None
