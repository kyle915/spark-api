"""The photo library: tagging once, search scope, and typed searches.

Storage and the model are replaced in every test: ``utils.gcs`` downloads
come from an in-memory dict and the OpenAI transport
(``utils.ai_text._post_json``) or the vision helper is patched. Asserted:

  * a photo is tagged once, and again only when its file changes; dry run
    calls nothing and stores nothing; the call cap holds;
  * a receipt is set aside without a call and never shows in a search;
  * the best-photo scorer and the library share one call per photo;
  * a brand sees only its own approved, unarchived recaps; staff may widen
    to unapproved, never to archived; no other tenant's photo ever shows;
  * typed searches become only known filters, with the model mocked;
  * no file name or ambassador name in anything but the image URL.
"""

from __future__ import annotations

import json
from datetime import datetime
from io import BytesIO
from unittest.mock import patch

import pytest
from django.utils import timezone
from PIL import Image, ImageDraw

from ambassadors.tests.base import AmbassadorsGraphQLTestCase
from events import models as event_models
from photo_library.interpret import reply_to_filters
from photo_library.models import RecapPhotoTag
from photo_library.search import clean_filters
from photo_library.tagging import tag_pending_photos
from photo_library.vision import library_tags
from recaps import models as recap_models
from recaps.photo_picks import score_pending_photos
from utils.ai_text import AiUnavailable


def _jpeg(seed: int, size=(1200, 900)) -> bytes:
    image = Image.new("RGB", size, (seed * 37 % 255, seed * 91 % 255, seed * 53 % 255))
    draw = ImageDraw.Draw(image)
    for i in range(8):
        x = (seed * (i + 3) * 97) % size[0]
        y = (seed * (i + 5) * 61) % size[1]
        draw.rectangle([x, y, x + 260, y + 200], fill=((i * 60 + seed * 11) % 255, 20 * i, 255 - 25 * i))
    out = BytesIO()
    image.save(out, format="JPEG", quality=85)
    return out.getvalue()


def _reply(**over):
    base = {
        "kind": "sampling",
        "sharp": True,
        "wellLit": True,
        "productVisible": True,
        "brandingVisible": True,
        "usefulness": 82,
        "caption": "Sampling table by the cooler aisle",
        "shows": ["consumers_engaging", "product_on_table"],
        "products": [],
        "peoplePresent": True,
        "crowdSize": "group",
        "setting": "indoor_store",
        "issues": [],
    }
    base.update(over)
    return base


CATALOG = [(11, "Seltzer Lime"), (12, "Seltzer Mango")]


# ─── Pure ───────────────────────────────────────────────────────────


def test_tags_keep_only_known_words_and_the_brands_own_products():
    values = library_tags(
        _reply(
            shows=["consumers_engaging", "faces", "signage"],
            products=["Seltzer Mango", "Another Brand Cola"],
            issues=["blurry", "ugly"],
            crowdSize="enormous",
            setting="moon",
        ),
        CATALOG,
    )
    assert values["shows"] == ["consumers_engaging", "signage"]
    assert values["product_ids"] == [12] and values["product_names"] == ["Seltzer Mango"]
    assert values["issues"] == ["blurry"]
    assert values["crowd_size"] == "none" and values["setting"] == "other"
    assert values["excluded"] is False


def test_a_caption_about_a_person_or_a_name_is_never_stored():
    values = library_tags(_reply(caption="Jordan Rivera smiling at shoppers"), CATALOG, names=["Jordan Rivera"])
    assert "Jordan" not in values["caption"] and "shopper" not in values["caption"].lower()
    values = library_tags(_reply(caption="A woman pouring samples"), CATALOG)
    assert values["caption"] == "Sampling in progress"


def test_a_receipt_seen_by_the_model_is_excluded_and_unscored():
    values = library_tags(_reply(kind="receipt", shows=["other"]), CATALOG)
    assert values["excluded"] and values["excluded_reason"] == "receipt"
    assert values["quality_score"] is None and values["caption"] == ""


def test_quality_has_the_scorers_ceilings_and_a_small_photo_is_flagged():
    values = library_tags(_reply(usefulness=95, sharp=False), CATALOG, width=500, height=400)
    assert values["quality_score"] == 30
    assert "blurry" in values["issues"] and "low_resolution" in values["issues"]


def test_a_reply_without_library_fields_stores_nothing():
    assert library_tags({"kind": "shelf", "usefulness": 80}, CATALOG) is None


def test_typed_search_reply_maps_only_onto_known_filters():
    raw = reply_to_filters(
        {
            "text": "cooler",
            "shows": ["consumers_engaging", "faces"],
            "crowdSizes": ["group", "crowd"],
            "settings": [],
            "products": ["Seltzer Lime", "Not Ours"],
            "store": "Kroger",
            "states": ["TX", "XX"],
            "startDate": "2026-09-01",
            "endDate": "not a date",
            "minQuality": 70,
            "hideIssues": True,
            "peoplePresent": None,
            "sort": "quality",
        },
        CATALOG,
    )
    f = clean_filters(raw, is_staff=False)
    assert f.shows == ["consumers_engaging"]
    assert f.crowd_sizes == ["group", "crowd"]
    assert f.product_ids == [11]
    assert f.store == "Kroger" and f.states == ["TX"]
    assert f.start.isoformat() == "2026-09-01" and f.end is None
    assert f.min_quality == 70 and f.hide_issues and f.sort == "quality"
    assert f.approved_only is True, "a brand user is always held to approved recaps"
    assert clean_filters({"approved_only": False}, is_staff=True).approved_only is False
    assert clean_filters({"approved_only": False}, is_staff=False).approved_only is True
    assert clean_filters({"sort": "DROP TABLE"}, is_staff=True).sort == ""


# ─── With the database ──────────────────────────────────────────────

LIBRARY = """
query PhotoLibraryQuery($tenantId: ID!, $filters: PhotoLibraryFilterInput, $first: Int!, $offset: Int!) {
  photoLibrary(tenantId: $tenantId, filters: $filters, first: $first, offset: $offset) {
    total inScope tagged states approvedOnly
    products { key label }
    showOptions { key label }
    photos {
      id url caption category shows showLabels crowdSize crowdLabel setting settingLabel
      peoplePresent qualityScore issues products store city state eventDate recapUuid
      isPick approved tagged
    }
  }
}
"""

INTERPRET = """
query InterpretPhotoSearchQuery($tenantId: ID!, $text: String!) {
  interpretPhotoSearch(tenantId: $tenantId, text: $text) {
    ok reason chips
    filters {
      text shows crowdSizes settings productIds store states startDate endDate
      minQuality peoplePresent hideIssues sort
    }
  }
}
"""


@pytest.mark.django_db(transaction=True)
class TestPhotoLibraryDb(AmbassadorsGraphQLTestCase):
    @pytest.fixture(autouse=True)
    def setup(self, db, settings):
        from config.schema_client import schema_clients

        settings.OPENAI_API_KEY = "test-key"
        settings.GS_BUCKET_NAME = "spark-test-bucket"
        self.roles = self.setup_default_roles()
        self.schema = schema_clients
        self.endpoint_path = "/api/v1/graphql/clients"
        self.sys = self.get_system_user()
        self.tenant = self.create_tenant(name="Library Brand")
        self.other = self.create_tenant(name="Library Other")
        self.admin = self.create_user(
            username="lib-admin", email="lib-admin@test.com", role=self.roles["spark_admin"]
        )
        self.client_user = self.create_user(
            username="lib-client", email="lib-client@test.com", role=self.roles["client"]
        )
        self.create_tenanted_user(self.client_user, self.tenant)
        self.other_client = self.create_user(
            username="lib-other", email="lib-other@test.com", role=self.roles["client"]
        )
        self.create_tenanted_user(self.other_client, self.other)
        ba_user = self.create_user(
            username="jordan-rivera-lib", email="jordan.rivera.lib@ba.test",
            role=self.roles["ambassador"],
        )
        ba_user.first_name, ba_user.last_name = "Jordan", "Rivera"
        ba_user.save()
        self.ba = self.create_ambassador(ba_user)
        self.texas = event_models.State.objects.create(name="Texas", code="TX", created_by=self.sys)
        self.ohio = event_models.State.objects.create(name="Ohio", code="OH", created_by=self.sys)
        self.kroger = event_models.Retailer.objects.create(
            name="Kroger", tenant=self.tenant, created_by=self.sys
        )
        self.heb = event_models.Retailer.objects.create(
            name="H-E-B", tenant=self.tenant, created_by=self.sys
        )
        ptype = event_models.ProductType.objects.create(name="Seltzer", tenant=self.tenant, created_by=self.sys)
        self.lime = event_models.Product.objects.create(
            name="Lime", product_type=ptype, tenant=self.tenant, created_by=self.sys
        )
        self.et = {}
        self.templates = {}
        self.file_type = recap_models.FileType.objects.create(name="image", created_by=self.sys)
        self.categories = {}
        self.images: dict[str, bytes] = {}

    # ── fixtures ──────────────────────────────────────────────────────

    def _template(self, tenant):
        if tenant.id not in self.templates:
            et = event_models.EventType.objects.create(
                name="Retail Sampling", slug=f"lib-retail-{tenant.id}", tenant=tenant, created_by=self.sys
            )
            self.templates[tenant.id] = recap_models.CustomRecapTemplate.objects.create(
                name="Library Retail", event_type=et, tenant=tenant, created_by=self.sys
            )
        return self.templates[tenant.id]

    def _category(self, name, tenant=None):
        tenant = tenant or self.tenant
        key = (tenant.id, name)
        if key not in self.categories:
            self.categories[key] = recap_models.FileRecapCategory.objects.create(
                name=name, tenant=tenant, created_by=self.sys
            )
        return self.categories[key]

    def _recap(self, *, approved=True, day=11, tenant=None, retailer=None, state=None, **kwargs):
        tenant = tenant or self.tenant
        template = self._template(tenant)
        when = timezone.make_aware(datetime(2026, 10, day, 12))
        event = event_models.Event.objects.create(
            name=f"Sampling-{day}", tenant=tenant, address="1 Main St", date=when,
            event_type=template.event_type, created_by=self.sys, updated_by=self.sys,
            retailer=retailer, state=state,
        )
        return recap_models.CustomRecap.objects.create(
            name="r", approved=approved, submitted_at=when, event=event, tenant=tenant,
            custom_recap_template=template, ambassador=self.ba, created_by=self.sys,
            retailer=retailer, state=state, **kwargs,
        )

    def _file(self, name, recap, *, category="Sampling photos", seed=1, sha="", size=(1200, 900)):
        row = recap_models.CustomRecapFile.objects.create(
            name=name, url=f"recap_files/{name}", custom_recap=recap, file_type=self.file_type,
            file_recap_category=self._category(category, recap.tenant), content_sha256=sha,
            created_by=self.sys,
        )
        self.images[f"recap_files/{name}"] = _jpeg(seed, size)
        return row

    def _tag(self, answers=None, **kwargs):
        calls = []

        def _vision(system, user, **kw):
            calls.append({"system": system, "user": user, **kw})
            if answers is None:
                return _reply()
            answer = answers(len(calls)) if callable(answers) else answers
            if isinstance(answer, Exception):
                raise answer
            return answer

        kwargs.setdefault("dry_run", False)
        with patch("utils.gcs.download_blob_bytes", side_effect=lambda n: self.images.get(n)), patch(
            "photo_library.tagging.generate_vision_json", side_effect=_vision
        ):
            report = tag_pending_photos(**kwargs)
        return report, calls

    async def _search(self, user, tenant, filters=None, first=60, offset=0):
        result = await self._execute_query_authenticated(
            LIBRARY,
            {"tenantId": str(tenant.id), "filters": filters, "first": first, "offset": offset},
            user,
            self.endpoint_path,
        )
        assert result.errors is None, result.errors
        return result.data["photoLibrary"]

    # ── tagging ───────────────────────────────────────────────────────

    def test_a_photo_is_tagged_once_and_again_only_when_its_file_changes(self):
        recap = self._recap(retailer=self.kroger, state=self.texas)
        f = self._file("jordan-rivera-table.jpg", recap, seed=3)
        self._file("shelf.jpg", recap, seed=7, category="Before & After Shelf")

        report, calls = self._tag()
        assert len(calls) == 2 and report["tagged"] == 2
        tag = RecapPhotoTag.objects.get(custom_recap_file=f)
        assert tag.status == "tagged" and tag.origin == "vision"
        assert "Kroger" in tag.search_text and "Texas" in tag.search_text
        assert "jordan" not in tag.search_text.lower(), "never a file or ambassador name"

        report, again = self._tag()
        assert again == [] and report["pending"] == 0, "a tagged photo is never looked at again"

        f.url = "recap_files/replaced.jpg"
        f.save()
        self.images["recap_files/replaced.jpg"] = _jpeg(9)
        _report, third = self._tag()
        assert len(third) == 1, "only the changed file is tagged again"
        assert RecapPhotoTag.objects.count() == 2

    def test_dry_run_calls_nothing_and_stores_nothing(self):
        recap = self._recap()
        self._file("a.jpg", recap, seed=3)
        self._file("b.jpg", recap, seed=4)
        with patch("utils.gcs.download_blob_bytes") as download:
            report, calls = self._tag(dry_run=True)
        assert calls == [] and not download.called
        assert report["would_call"] == 2 and RecapPhotoTag.objects.count() == 0

    def test_the_call_cap_holds_and_the_rest_waits(self):
        recap = self._recap()
        for i in range(5):
            self._file(f"p{i}.jpg", recap, seed=20 + i * 7)
        report, calls = self._tag(max_calls=2)
        assert len(calls) == 2 and report["stopped"] == "budget"
        _report, more = self._tag(max_calls=10)
        assert len(more) == 3

    def test_receipts_are_set_aside_without_a_call_and_copies_reuse_tags(self):
        recap = self._recap()
        receipt = self._file("receipt.jpg", recap, category="Product Spend", seed=2)
        first = self._file("one.jpg", recap, seed=5, sha="b" * 64)
        second_recap = self._recap(day=12)
        copy = self._file("one-again.jpg", second_recap, seed=5, sha="b" * 64)
        report, calls = self._tag()
        assert len(calls) == 1, "the receipt and the same file twice need no call"
        assert RecapPhotoTag.objects.get(custom_recap_file=receipt).status == "excluded"
        a = RecapPhotoTag.objects.get(custom_recap_file=first)
        b = RecapPhotoTag.objects.get(custom_recap_file=copy)
        assert sorted([a.origin, b.origin]) == ["copied", "vision"]
        assert a.shows == b.shows and a.caption == b.caption

    def test_without_the_model_nothing_is_stored(self):
        recap = self._recap()
        self._file("a.jpg", recap, seed=3)
        report, _calls = self._tag(answers=AiUnavailable("no key"))
        assert report["stopped"] == "ai_unavailable"
        assert RecapPhotoTag.objects.count() == 0

    def test_the_scorer_and_the_library_share_one_call_per_photo(self):
        recap = self._recap()
        scored_first = self._file("a.jpg", recap, seed=3)
        payloads = []

        def _post(url, payload, headers, *, timeout):
            payloads.append(payload)
            return {"choices": [{"message": {"content": json.dumps(_reply())}}]}

        with patch("utils.gcs.download_blob_bytes", side_effect=lambda n: self.images.get(n)), patch(
            "utils.ai_text._post_json", side_effect=_post
        ):
            score_pending_photos(since_days=3650)
            assert len(payloads) == 1
            schema = payloads[0]["response_format"]["json_schema"]["schema"]
            assert "shows" in schema["properties"] and "crowdSize" in schema["properties"]
            assert schema["properties"]["products"]["items"]["enum"] == ["Seltzer Lime"]
            system = payloads[0]["messages"][0]["content"]
            assert "Do not identify, recognize or describe any person" in system
            # The scorer's call already tagged the photo for the library.
            assert RecapPhotoTag.objects.get(custom_recap_file=scored_first).origin == "vision"
            report = tag_pending_photos(dry_run=False)
            assert len(payloads) == 1 and report["model_calls"] == 0

            # The other way round: tagged by the library first, the scorer
            # reuses the stored answer.
            later = self._file("b.jpg", recap, seed=11)
            tag_pending_photos(dry_run=False)
            assert len(payloads) == 2
            recap_models.RecapPhotoScore.objects.filter(custom_recap_file=later).delete()
            score_pending_photos(since_days=3650)
            assert len(payloads) == 2, "the scorer reused the library's answer"
        score = recap_models.RecapPhotoScore.objects.get(custom_recap_file=later)
        assert score.status == "scored" and score.score == 82

    # ── who sees what ─────────────────────────────────────────────────

    def _scene(self):
        self.approved = self._recap(retailer=self.kroger, state=self.texas)
        self.pending = self._recap(approved=False, day=12, retailer=self.kroger, state=self.texas)
        self.archived = self._recap(day=13, archived_at=timezone.now(), retailer=self.kroger)
        theirs = self._recap(tenant=self.other, day=14)
        self.f_ok = self._file("jordan-rivera-ok.jpg", self.approved, seed=3)
        self.f_receipt = self._file("receipt.jpg", self.approved, category="Product Spend", seed=4)
        self.f_pending = self._file("pending.jpg", self.pending, seed=5)
        self.f_archived = self._file("archived.jpg", self.archived, seed=6)
        self.f_theirs = self._file("theirs.jpg", theirs, seed=7)
        self._tag()

    @pytest.mark.asyncio
    async def test_a_brand_sees_only_its_own_approved_photos_and_never_a_receipt(self):
        from asgiref.sync import sync_to_async

        await sync_to_async(self._scene)()
        page = await self._search(self.client_user, self.tenant, {"approvedOnly": False})
        ids = {p["id"] for p in page["photos"]}
        assert ids == {str(self.f_ok.id)}
        assert page["approvedOnly"] is True
        assert all(p["approved"] for p in page["photos"])
        # Asking for the other brand gives their own photos back, never theirs.
        page = await self._search(self.client_user, self.other)
        assert str(self.f_theirs.id) not in {p["id"] for p in page["photos"]}
        page = await self._search(self.other_client, self.tenant)
        assert {p["id"] for p in page["photos"]} == {str(self.f_theirs.id)}
        # No file name or ambassador name outside the image URL.
        photo = (await self._search(self.client_user, self.tenant))["photos"][0]
        shown = json.dumps({k: v for k, v in photo.items() if k != "url"}).lower()
        assert "jordan" not in shown and "rivera" not in shown and ".jpg" not in shown

    @pytest.mark.asyncio
    async def test_staff_may_widen_to_unapproved_but_never_archived_or_receipts(self):
        from asgiref.sync import sync_to_async

        await sync_to_async(self._scene)()
        page = await self._search(self.admin, self.tenant)
        assert {p["id"] for p in page["photos"]} == {str(self.f_ok.id)}
        page = await self._search(self.admin, self.tenant, {"approvedOnly": False})
        assert {p["id"] for p in page["photos"]} == {str(self.f_ok.id), str(self.f_pending.id)}
        pending = next(p for p in page["photos"] if p["id"] == str(self.f_pending.id))
        assert pending["approved"] is False
        page = await self._search(self.admin, self.other, {"approvedOnly": False})
        assert {p["id"] for p in page["photos"]} == {str(self.f_theirs.id)}

    @pytest.mark.asyncio
    async def test_filters_and_free_text_narrow_the_photos(self):
        from asgiref.sync import sync_to_async

        def _setup():
            a = self._recap(retailer=self.kroger, state=self.texas)
            b = self._recap(day=15, retailer=self.heb, state=self.ohio)
            self.crowd = self._file("crowd.jpg", a, seed=3)
            self.shelf = self._file("shelf.jpg", b, seed=9, category="Shelf")
            self.untagged = self._file("later.jpg", b, seed=13)

            # Newest recap first: the shelf, the later photo, then the crowd.
            def answers(n):
                if n == 3:
                    return _reply(products=["Seltzer Lime"], usefulness=90)
                return _reply(
                    kind="shelf", shows=["shelf_display"], crowdSize="none", peoplePresent=False,
                    caption="Product on the cooler shelf", usefulness=60, issues=["dark"],
                )

            self._tag(answers)
            # One photo the tagger has not reached yet.
            RecapPhotoTag.objects.filter(custom_recap_file=self.untagged).delete()

        await sync_to_async(_setup)()
        everything = await self._search(self.admin, self.tenant)
        assert everything["total"] == 3 and everything["tagged"] == 2
        # Sorted by quality, untagged last.
        assert [p["id"] for p in everything["photos"]][-1] == str(self.untagged.id)
        assert everything["photos"][0]["id"] == str(self.crowd.id)

        crowd = await self._search(
            self.admin, self.tenant,
            {"shows": ["consumers_engaging"], "crowdSizes": ["group", "crowd"], "store": "kroger", "states": ["TX"]},
        )
        assert [p["id"] for p in crowd["photos"]] == [str(self.crowd.id)]
        assert crowd["photos"][0]["products"] == ["Seltzer Lime"]
        assert crowd["photos"][0]["store"] == "Kroger" and crowd["photos"][0]["state"] == "TX"

        product = await self._search(self.admin, self.tenant, {"productIds": [str(self.lime.id)]})
        assert [p["id"] for p in product["photos"]] == [str(self.crowd.id)]

        text = await self._search(self.admin, self.tenant, {"text": "cooler shelf"})
        assert [p["id"] for p in text["photos"]] == [str(self.shelf.id)]
        text = await self._search(self.admin, self.tenant, {"text": "Ohio"})
        assert [p["id"] for p in text["photos"]] == [str(self.shelf.id)]

        clean = await self._search(self.admin, self.tenant, {"hideIssues": True})
        assert [p["id"] for p in clean["photos"]] == [str(self.crowd.id)]
        paged = await self._search(self.admin, self.tenant, first=1, offset=1)
        assert paged["total"] == 3 and len(paged["photos"]) == 1

    # ── typed searches ────────────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_a_typed_search_becomes_filters_with_the_model_mocked(self):
        payloads = []
        reply = {
            "text": "",
            "shows": ["consumers_engaging"],
            "crowdSizes": ["group", "crowd"],
            "settings": [],
            "products": [],
            "store": "Kroger",
            "states": ["TX"],
            "startDate": None,
            "endDate": None,
            "minQuality": 70,
            "hideIssues": False,
            "peoplePresent": None,
            "sort": "quality",
        }

        def _post(url, payload, headers, *, timeout):
            payloads.append(payload)
            return {"choices": [{"message": {"content": json.dumps(reply)}}]}

        with patch("utils.ai_text._post_json", side_effect=_post):
            result = await self._execute_query_authenticated(
                INTERPRET,
                {"tenantId": str(self.tenant.id), "text": "best crowd photos at Kroger in Texas"},
                self.client_user,
                self.endpoint_path,
            )
        assert result.errors is None, result.errors
        data = result.data["interpretPhotoSearch"]
        assert data["ok"] is True
        f = data["filters"]
        assert f["shows"] == ["consumers_engaging"] and f["crowdSizes"] == ["group", "crowd"]
        assert f["store"] == "Kroger" and f["states"] == ["TX"]
        assert f["minQuality"] == 70 and f["sort"] == "quality"
        assert "Consumers engaging" in data["chips"] and "TX" in data["chips"]
        assert len(payloads) == 1
        sent = payloads[0]
        assert sent["response_format"]["json_schema"]["strict"] is True
        user_text = sent["messages"][-1]["content"]
        assert "best crowd photos at Kroger in Texas" in user_text
        assert "Seltzer Lime" in user_text, "the brand's own products only"
        assert "Rivera" not in json.dumps(sent)

    @pytest.mark.asyncio
    async def test_a_bad_reply_never_adds_an_unknown_filter(self):
        def _post(url, payload, headers, *, timeout):
            bad = {
                "text": "tent", "shows": ["faces"], "crowdSizes": [], "settings": ["moon"],
                "products": ["Rival Cola"], "store": "", "states": ["ZZ"], "startDate": "2026-13-45",
                "endDate": None, "minQuality": 500, "hideIssues": False, "peoplePresent": None,
                "sort": "drop",
            }
            return {"choices": [{"message": {"content": json.dumps(bad)}}]}

        with patch("utils.ai_text._post_json", side_effect=_post):
            result = await self._execute_query_authenticated(
                INTERPRET, {"tenantId": str(self.tenant.id), "text": "tent photos"},
                self.admin, self.endpoint_path,
            )
        f = result.data["interpretPhotoSearch"]["filters"]
        assert f["shows"] == [] and f["settings"] == [] and f["productIds"] == []
        assert f["states"] == [] and f["startDate"] is None and f["sort"] == ""
        assert f["minQuality"] == 100 and f["text"] == "tent"

    @pytest.mark.asyncio
    async def test_without_the_model_the_words_are_searched_as_typed(self, settings):
        settings.OPENAI_API_KEY = ""
        result = await self._execute_query_authenticated(
            INTERPRET, {"tenantId": str(self.tenant.id), "text": "kroger end cap"},
            self.admin, self.endpoint_path,
        )
        data = result.data["interpretPhotoSearch"]
        assert data["ok"] is False and data["filters"]["text"] == "kroger end cap"

    # ── the cron endpoint ─────────────────────────────────────────────

    def test_cron_endpoint_needs_the_secret_and_is_a_dry_run_by_default(self, settings):
        from django.test import Client

        settings.INTERNAL_CRON_SECRET = "s3cret"
        self._file("a.jpg", self._recap(), seed=3)
        url = "/internal/cron/tag-recap-photos"
        assert Client().post(url).status_code == 401
        with patch("photo_library.tagging.generate_vision_json", return_value=_reply()) as vision:
            dry = Client().post(url, {"max_calls": "5"}, HTTP_X_CRON_SECRET="s3cret")
            assert dry.status_code == 200 and dry.json()["dry_run"] is True
            assert vision.call_count == 0 and RecapPhotoTag.objects.count() == 0
            with patch("utils.gcs.download_blob_bytes", side_effect=lambda n: self.images.get(n)):
                real = Client().post(
                    url, {"dry_run": "false", "max_calls": "5"}, HTTP_X_CRON_SECRET="s3cret"
                )
        assert real.status_code == 200 and real.json()["dry_run"] is False
        assert vision.call_count == 1 and RecapPhotoTag.objects.count() == 1
