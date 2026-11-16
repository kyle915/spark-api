"""Best photos on a recap: what is scored, what is never scored, who may
see and change the picks.

The model is never called: ``generate_vision_json`` or
``utils.ai_text._post_json`` is patched in every test that reaches it.
"""

from __future__ import annotations

import json
from datetime import datetime
from io import BytesIO
from unittest.mock import patch

import pytest
from django.test import Client
from django.utils import timezone
from PIL import Image, ImageDraw

from ambassadors.tests.base import AmbassadorsGraphQLTestCase
from events import models as event_models
from recaps import models as recap_models
from recaps import photo_picks
from recaps.photo_picks import (
    MAX_PHOTOS_PER_RECAP,
    choose_picks,
    clean_caption,
    difference_hash,
    hash_distance,
    interpret_score,
    score_pending_photos,
    spread_across_buckets,
)
from utils.ai_text import AiUnavailable


def _jpeg(seed: int, size=(1200, 900)) -> bytes:
    """A distinct picture per seed, so difference hashes differ."""
    image = Image.new("RGB", size, (seed * 37 % 255, seed * 91 % 255, seed * 53 % 255))
    draw = ImageDraw.Draw(image)
    for i in range(8):
        x = (seed * (i + 3) * 97) % size[0]
        y = (seed * (i + 5) * 61) % size[1]
        draw.rectangle([x, y, x + 260, y + 200], fill=((i * 60 + seed * 11) % 255, 20 * i, 255 - 25 * i))
    out = BytesIO()
    image.save(out, format="JPEG", quality=85)
    return out.getvalue()


def _good(caption="Sampling table at the store entrance", kind="table_setup", usefulness=85):
    return {
        "kind": kind,
        "sharp": True,
        "wellLit": True,
        "productVisible": True,
        "brandingVisible": True,
        "usefulness": usefulness,
        "caption": caption,
    }


# ─── Pure ───────────────────────────────────────────────────────────


def test_a_caption_about_a_person_is_replaced():
    assert clean_caption("Sampling table at the store entrance.", "table_setup") == (
        "Sampling table at the store entrance"
    )
    for bad in (
        "A smiling woman holding a can",
        "Brand ambassador at the table",
        "Shoppers trying the product",
        "Young man wearing a branded shirt",
        "She pours a sample",
    ):
        assert clean_caption(bad, "sampling") == "Sampling in progress", bad
    # A known ambassador's name never survives, even without a person word.
    assert clean_caption("Table by Jordan Rivera", "table_setup", ["Jordan Rivera"]) == (
        "Sampling table set up"
    )
    assert clean_caption("", "shelf") == "Product on the shelf"


def test_score_has_ceilings_the_model_cannot_lift():
    assert interpret_score(_good())["score"] == 85
    assert interpret_score({**_good(usefulness=95), "sharp": False})["score"] == 30
    assert interpret_score({**_good(usefulness=95), "wellLit": False})["score"] == 50
    assert (
        interpret_score({**_good(usefulness=95), "productVisible": False, "brandingVisible": False})[
            "score"
        ]
        == 60
    )
    assert interpret_score({**_good(usefulness=500)})["score"] == 100
    assert interpret_score(None) is None
    assert interpret_score({**_good(), "kind": "a person"}) is None


def test_a_receipt_or_screenshot_seen_by_the_model_is_never_scored():
    receipt = interpret_score(_good(kind="receipt", usefulness=99))
    assert receipt["status"] == "skipped" and receipt["skip_reason"] == "receipt"
    assert receipt["score"] == 0
    shot = interpret_score(_good(kind="screenshot", usefulness=99))
    assert shot["status"] == "skipped" and shot["skip_reason"] == "screenshot"


def test_picks_are_the_top_few_spread_across_kinds():
    rows = [
        {"id": 1, "score": 95, "kind": "table_setup"},
        {"id": 2, "score": 93, "kind": "table_setup"},
        {"id": 3, "score": 92, "kind": "table_setup"},
        {"id": 4, "score": 91, "kind": "table_setup"},
        {"id": 5, "score": 70, "kind": "shelf"},
        {"id": 6, "score": 40, "kind": "signage"},  # under the bar
        {"id": 7, "score": 99, "kind": "receipt"},  # never
    ]
    assert choose_picks(rows) == [1, 5, 2, 3]
    assert choose_picks([{"id": 9, "score": 30, "kind": "shelf"}]) == []


def test_near_identical_shots_hash_close_and_different_shots_do_not():
    a = Image.open(BytesIO(_jpeg(3)))
    again = Image.open(BytesIO(_jpeg(3))).resize((900, 675))
    other = Image.open(BytesIO(_jpeg(8)))
    assert hash_distance(difference_hash(a), difference_hash(again)) <= 5
    assert hash_distance(difference_hash(a), difference_hash(other)) > 5


def test_the_per_recap_limit_takes_turns_across_buckets():
    class Cat:
        def __init__(self, name):
            self.name = name

    class F:
        def __init__(self, i, cat):
            self.id = i
            self.file_recap_category = Cat(cat)

    files = [F(i, "Table Set Up") for i in range(10)] + [F(100 + i, "Sampling photos") for i in range(2)]
    keep, rest = spread_across_buckets(files, 4)
    assert [f.id for f in keep] == [100, 0, 101, 1]
    assert len(rest) == 8
    assert spread_across_buckets(files, 0) == ([], files)


# ─── With the database ──────────────────────────────────────────────

PICKS = """
query Picks($recapId: ID!) {
  recapPhotoPicks(recapId: $recapId) {
    ok reason
    items { fileId url category status note score caption modelPick staffChoice isPick canPick }
  }
}
"""

SET_PICK = """
mutation SetPick($fileId: ID!, $choice: String!) {
  setRecapPhotoPick(fileId: $fileId, choice: $choice) {
    ok reason
    items { fileId modelPick staffChoice isPick canPick }
  }
}
"""

# The Photos page's exact field set (TenantPhotoGalleryQuery.ts).
GALLERY = """
query TenantPhotoGalleryQuery(
  $tenantId: ID! $startDate: String $endDate: String $state: String
  $store: String $category: String $picksOnly: Boolean! $first: Int! $offset: Int!
) {
  tenantPhotoGallery(
    tenantId: $tenantId startDate: $startDate endDate: $endDate state: $state
    store: $store category: $category picksOnly: $picksOnly first: $first offset: $offset
  ) {
    total categories states
    photos { id url category recapUuid store state eventDate isPick caption }
  }
}
"""


@pytest.mark.django_db(transaction=True)
class TestPhotoPicksDb(AmbassadorsGraphQLTestCase):
    @pytest.fixture(autouse=True)
    def setup(self, db, settings):
        from config.schema_client import schema_clients

        settings.OPENAI_API_KEY = "test-key"
        settings.GS_BUCKET_NAME = "spark-test-bucket"
        self.roles = self.setup_default_roles()
        self.schema = schema_clients
        self.endpoint_path = "/api/v1/graphql/clients"
        self.sys = self.get_system_user()
        self.tenant = self.create_tenant(name="Picks Brand")
        self.other = self.create_tenant(name="Picks Other")
        self.admin = self.create_user(
            username="picks-admin", email="picks-admin@test.com", role=self.roles["spark_admin"]
        )
        self.client_user = self.create_user(
            username="picks-client", email="picks-client@test.com", role=self.roles["client"]
        )
        self.create_tenanted_user(self.client_user, self.tenant)
        self.other_client = self.create_user(
            username="picks-other", email="picks-other@test.com", role=self.roles["client"]
        )
        self.create_tenanted_user(self.other_client, self.other)
        ba_user = self.create_user(
            username="jordan-rivera-picks", email="jordan.rivera.picks@ba.test",
            role=self.roles["ambassador"],
        )
        ba_user.first_name, ba_user.last_name = "Jordan", "Rivera"
        ba_user.save()
        self.ba = self.create_ambassador(ba_user)

        self.et = event_models.EventType.objects.create(
            name="Retail Sampling", slug="picks-retail", tenant=self.tenant, created_by=self.sys
        )
        self.template = recap_models.CustomRecapTemplate.objects.create(
            name="Picks Retail", event_type=self.et, tenant=self.tenant, created_by=self.sys
        )
        self.file_type = recap_models.FileType.objects.create(name="image", created_by=self.sys)
        self.receipts = self._category("Product Spend")
        self.table = self._category("Table Set Up")
        self.sampling = self._category("Sampling photos")
        self.shelf = self._category("Before & After Shelf / Stock")
        self.recap = self._recap(approved=True)
        self.images: dict[str, bytes] = {}

    # ── fixtures ──────────────────────────────────────────────────────

    def _category(self, name):
        return recap_models.FileRecapCategory.objects.create(
            name=name, tenant=self.tenant, created_by=self.sys
        )

    def _recap(self, *, approved, day=11, tenant=None, **kwargs):
        when = timezone.make_aware(datetime(2026, 10, day, 12))
        event = event_models.Event.objects.create(
            name=f"Costco {day}", tenant=tenant or self.tenant, address="1 Main St", date=when,
            event_type=self.et, created_by=self.sys, updated_by=self.sys,
        )
        return recap_models.CustomRecap.objects.create(
            name="r", approved=approved, submitted_at=when, event=event,
            tenant=tenant or self.tenant, custom_recap_template=self.template,
            ambassador=self.ba, created_by=self.sys, **kwargs,
        )

    def _file(self, name, category, *, seed=None, recap=None, size=(1200, 900), sha=""):
        row = recap_models.CustomRecapFile.objects.create(
            name=name, url=f"recap_files/{name}", custom_recap=recap or self.recap,
            file_type=self.file_type, file_recap_category=category, content_sha256=sha,
            created_by=self.sys,
        )
        if seed is not None:
            self.images[f"recap_files/{name}"] = _jpeg(seed, size)
        return row

    def _run(self, answers=None, **kwargs):
        """Run the job with storage and the model replaced."""
        calls = []

        def _vision(system, user, **kw):
            calls.append({"system": system, "user": user, **kw})
            if answers is None:
                return _good()
            answer = answers(len(calls)) if callable(answers) else next(answers)
            if isinstance(answer, Exception):
                raise answer
            return answer

        with patch("utils.gcs.download_blob_bytes", side_effect=lambda name: self.images.get(name)), patch(
            "recaps.photo_picks.generate_vision_json", side_effect=_vision
        ):
            report = score_pending_photos(since_days=3650, **kwargs)
        return report, calls

    def _score(self, f):
        return recap_models.RecapPhotoScore.objects.filter(custom_recap_file=f).first()

    async def _picks(self, user, recap=None):
        result = await self._execute_query_authenticated(
            PICKS, {"recapId": str((recap or self.recap).id)}, user, self.endpoint_path
        )
        assert result.errors is None, result.errors
        return result.data["recapPhotoPicks"]

    async def _gallery(self, user, tenant, picks_only):
        result = await self._execute_query_authenticated(
            GALLERY,
            {
                "tenantId": str(tenant.id), "startDate": None, "endDate": None, "state": None,
                "store": None, "category": None, "picksOnly": picks_only, "first": 60, "offset": 0,
            },
            user,
            self.endpoint_path,
        )
        assert result.errors is None, result.errors
        return result.data["tenantPhotoGallery"]

    # ── what reaches the model ────────────────────────────────────────

    def test_cheap_checks_run_first_and_only_the_rest_reach_the_model(self):
        receipt = self._file("receipt.jpg", self.receipts, seed=1)
        table = self._file("table.jpg", self.table, seed=2, sha="a" * 64)
        same_file = self._file("table-again.jpg", self.table, seed=2, sha="a" * 64)
        tiny = self._file("tiny.jpg", self.sampling, seed=4, size=(400, 300))
        shelf = self._file("shelf.jpg", self.shelf, seed=5)
        near_copy = self._file("shelf-burst.jpg", self.shelf, seed=5)
        self._file("notes.pdf", self.table)  # not an image: never considered

        report, calls = self._run()

        assert len(calls) == 2, "only the table and the shelf shot need the model"
        assert report["model_calls"] == 2 and report["scored"] == 2
        assert self._score(receipt).skip_reason == "receipt"
        assert self._score(same_file).skip_reason == "duplicate"
        assert self._score(tiny).skip_reason == "low_resolution"
        assert self._score(near_copy).skip_reason == "near_duplicate"
        assert self._score(table).status == "scored" and self._score(table).model_pick
        assert self._score(shelf).status == "scored" and self._score(shelf).model_pick
        assert self._score(table).caption == "Sampling table at the store entrance"
        for row in recap_models.RecapPhotoScore.objects.exclude(status="scored"):
            assert row.model_pick is False

    def test_the_model_is_told_not_to_describe_people_and_gets_a_small_image(self, settings):
        self._file("table.jpg", self.table, seed=2, size=(4000, 3000))
        payloads = []

        def _post(url, payload, headers, *, timeout):
            payloads.append(payload)
            return {"choices": [{"message": {"content": json.dumps(_good())}}]}

        with patch("utils.gcs.download_blob_bytes", side_effect=lambda name: self.images.get(name)), patch(
            "utils.ai_text._post_json", side_effect=_post
        ):
            report = score_pending_photos(since_days=3650)

        assert report["scored"] == 1 and len(payloads) == 1
        system = payloads[0]["messages"][0]["content"]
        assert "Do not identify, recognize or describe any person" in system
        content = payloads[0]["messages"][1]["content"]
        assert [part["type"] for part in content] == ["text", "image_url"], "one image per call"
        text = json.dumps([payloads[0]["messages"][0], content[0]])
        assert "Rivera" not in text and "Jordan" not in text
        assert payloads[0]["response_format"]["json_schema"]["strict"] is True
        # 4000×3000 was shrunk before it was sent.
        import base64

        sent = base64.b64decode(content[1]["image_url"]["url"].split(",", 1)[1])
        assert max(Image.open(BytesIO(sent)).size) <= photo_picks.VISION_MAX_DIM

    def test_a_caption_naming_a_person_is_not_stored(self):
        f = self._file("table.jpg", self.table, seed=2)
        self._run(answers=lambda n: _good(caption="Jordan Rivera smiling at the table"))
        assert self._score(f).caption == "Sampling table set up"

    def test_a_photo_is_never_scored_twice(self):
        self._file("table.jpg", self.table, seed=2)
        self._file("shelf.jpg", self.shelf, seed=5)
        _report, first = self._run()
        report, second = self._run()
        assert len(first) == 2
        assert second == [] and report["model_calls"] == 0 and report["recaps"] == 0
        # A photo added later is the only one looked at.
        self._file("late.jpg", self.sampling, seed=9)
        _report, third = self._run()
        assert len(third) == 1
        assert recap_models.RecapPhotoScore.objects.count() == 3

    def test_no_more_than_the_per_recap_limit_is_ever_opened(self):
        for i in range(MAX_PHOTOS_PER_RECAP + 5):
            self._file(f"p{i}.jpg", self.sampling, seed=20 + i)
        report, calls = self._run(max_photos=200)
        assert len(calls) == MAX_PHOTOS_PER_RECAP
        assert report["skipped_by_reason"] == {"over_limit": 5}
        # The five left over are remembered, so a second run opens nothing.
        _report, again = self._run(max_photos=200)
        assert again == []
        picks = recap_models.RecapPhotoScore.objects.filter(model_pick=True).count()
        assert picks == photo_picks.MAX_PICKS_PER_RECAP

    def test_the_run_stops_at_its_photo_budget_and_resumes(self):
        second = self._recap(approved=True, day=12)
        for i in range(3):
            self._file(f"a{i}.jpg", self.sampling, seed=40 + i)
            self._file(f"b{i}.jpg", self.sampling, seed=50 + i, recap=second)
        report, calls = self._run(max_photos=4)
        assert len(calls) == 4 and report["model_calls"] == 4
        assert recap_models.RecapPhotoScore.objects.filter(status="scored").count() == 4
        report, calls = self._run(max_photos=4)
        assert len(calls) == 2
        assert recap_models.RecapPhotoScore.objects.filter(status="scored").count() == 6

    def test_the_run_stops_at_its_time_limit(self):
        for i in range(3):
            self._file(f"a{i}.jpg", self.sampling, seed=40 + i)
        ticks = {"now": 0.0}

        def _clock():
            # Every look at the clock is 30 seconds later than the last.
            ticks["now"] += 30.0
            return ticks["now"]

        with patch("recaps.photo_picks.time.monotonic", side_effect=_clock):
            report, calls = self._run(max_seconds=100)
        assert 1 <= len(calls) < 3
        assert report["budget_seconds"] == 100
        assert recap_models.RecapPhotoScore.objects.count() == len(calls)

    def test_only_approved_live_recaps_are_scored(self):
        pending = self._recap(approved=False, day=12)
        archived = self._recap(approved=True, day=13, archived_at=timezone.now())
        self._file("pending.jpg", self.sampling, seed=60, recap=pending)
        self._file("archived.jpg", self.sampling, seed=61, recap=archived)
        report, calls = self._run()
        assert calls == [] and report["recaps"] == 0
        assert recap_models.RecapPhotoScore.objects.count() == 0

    def test_without_the_model_nothing_is_scored_and_pinning_still_works(self):
        receipt = self._file("receipt.jpg", self.receipts, seed=1)
        table = self._file("table.jpg", self.table, seed=2)
        report, calls = self._run(answers=lambda n: AiUnavailable("no key"))
        assert report["ai_unavailable"] is True and report["scored"] == 0
        assert len(calls) == 1
        assert self._score(table) is None, "left for a later run"
        assert self._score(receipt).skip_reason == "receipt"
        # The manual path: staff pin by hand.
        assert photo_picks.set_staff_pick(table.id, "pinned", self.admin) == {"ok": True, "reason": ""}
        items = {i["file_id"]: i for i in photo_picks.recap_photo_picks(self.recap.id)}
        assert items[str(table.id)]["is_pick"] is True
        assert items[str(table.id)]["status"] == "not_scored"
        # When the model is back the photo is scored once.
        _report, calls = self._run()
        assert len(calls) == 1 and self._score(table).status == "scored"

    def test_an_outage_mid_run_leaves_photos_for_the_next_run(self):
        files = [self._file(f"a{i}.jpg", self.sampling, seed=40 + i) for i in range(5)]
        report, calls = self._run(answers=lambda n: None)
        assert report["stopped"] == "model_failing" and len(calls) == 3
        assert recap_models.RecapPhotoScore.objects.count() == 0
        # One bad reply among good ones is that photo's problem: recorded.
        report, calls = self._run(answers=lambda n: None if n == 2 else _good())
        assert report["unreadable"] == 1 and report["scored"] == 4
        assert sorted(self._score(f).status for f in files) == ["scored"] * 4 + ["unreadable"]

    # ── picks: staff wins, receipts never ─────────────────────────────

    def test_a_receipt_can_never_be_pinned_or_picked(self):
        receipt = self._file("receipt.jpg", self.receipts, seed=1)
        looks_like_one = self._file("odd.jpg", self.table, seed=3)
        self._run(answers=lambda n: _good(kind="receipt", usefulness=99))
        assert self._score(looks_like_one).skip_reason == "receipt"
        for f in (receipt, looks_like_one):
            outcome = photo_picks.set_staff_pick(f.id, "pinned", self.admin)
            assert outcome == {"ok": False, "reason": "A receipt can't be a best photo."}
        # Even a pin written straight to the table does not make it a pick.
        recap_models.RecapPhotoStaffPick.objects.create(custom_recap_file=receipt, choice="pinned")
        items = {i["file_id"]: i for i in photo_picks.recap_photo_picks(self.recap.id)}
        assert items[str(receipt.id)]["is_pick"] is False
        assert items[str(receipt.id)]["can_pick"] is False
        assert not recap_models.CustomRecapFile.objects.filter(
            photo_picks.effective_pick_q(), id=receipt.id
        ).exists()

    @pytest.mark.asyncio
    async def test_staff_see_the_picks_and_their_choice_wins(self):
        from asgiref.sync import sync_to_async

        table = await sync_to_async(self._file)("table.jpg", self.table, seed=2)
        shelf = await sync_to_async(self._file)("shelf.jpg", self.shelf, seed=5)
        await sync_to_async(self._run)(
            answers=lambda n: _good(usefulness=90) if n == 1 else _good(kind="shelf", usefulness=20)
        )

        data = await self._picks(self.admin)
        assert data["ok"] is True
        items = {i["fileId"]: i for i in data["items"]}
        assert items[str(table.id)]["isPick"] is True and items[str(table.id)]["modelPick"] is True
        assert items[str(shelf.id)]["isPick"] is False and items[str(shelf.id)]["score"] == 20
        assert items[str(table.id)]["url"].endswith("recap_files/table.jpg")

        async def _set(file_id, choice):
            result = await self._execute_mutation_authenticated(
                SET_PICK, {"fileId": str(file_id), "choice": choice}, self.admin, self.endpoint_path
            )
            assert result.errors is None, result.errors
            return {i["fileId"]: i for i in result.data["setRecapPhotoPick"]["items"]}

        items = await _set(table.id, "unpinned")
        assert items[str(table.id)]["isPick"] is False
        assert items[str(table.id)]["modelPick"] is True, "the scorer's choice is kept as it was"
        items = await _set(shelf.id, "pinned")
        assert items[str(shelf.id)]["isPick"] is True and items[str(shelf.id)]["modelPick"] is False

        # A later run of the scorer does not undo either decision.
        await sync_to_async(self._file)("late.jpg", self.sampling, seed=9)
        await sync_to_async(self._run)()
        items = {i["fileId"]: i for i in (await self._picks(self.admin))["items"]}
        assert items[str(table.id)]["isPick"] is False
        assert items[str(shelf.id)]["isPick"] is True

        # Handing it back restores the scorer's choice.
        items = await _set(table.id, "")
        assert items[str(table.id)]["isPick"] is True

    @pytest.mark.asyncio
    async def test_brand_users_cannot_read_or_change_picks(self):
        from asgiref.sync import sync_to_async

        table = await sync_to_async(self._file)("table.jpg", self.table, seed=2)
        await sync_to_async(self._run)()
        for user in (self.client_user, self.other_client):
            data = await self._picks(user)
            assert data == {"ok": False, "reason": "Not available.", "items": []}
            result = await self._execute_mutation_authenticated(
                SET_PICK, {"fileId": str(table.id), "choice": "unpinned"}, user, self.endpoint_path
            )
            assert result.errors is None, result.errors
            assert result.data["setRecapPhotoPick"]["ok"] is False
        assert await sync_to_async(recap_models.RecapPhotoStaffPick.objects.count)() == 0

    @pytest.mark.asyncio
    async def test_gallery_shows_a_brand_its_own_approved_picks_only(self):
        from asgiref.sync import sync_to_async

        def _seed():
            table = self._file("table.jpg", self.table, seed=2)
            self._file("dull.jpg", self.shelf, seed=5)
            self._file("receipt.jpg", self.receipts, seed=1)
            self._run(answers=lambda n: _good(usefulness=90) if n == 1 else _good(kind="shelf", usefulness=20))
            # A pinned photo on a recap still awaiting review must not show.
            pending = self._recap(approved=False, day=12)
            hidden = self._file("pending.jpg", self.sampling, seed=7, recap=pending)
            recap_models.RecapPhotoStaffPick.objects.create(custom_recap_file=hidden, choice="pinned")
            return table

        table = await sync_to_async(_seed)()

        best = await self._gallery(self.client_user, self.tenant, True)
        assert [p["id"] for p in best["photos"]] == [str(table.id)]
        assert best["photos"][0]["isPick"] is True
        assert best["photos"][0]["caption"] == "Sampling table at the store entrance"

        everything = await self._gallery(self.client_user, self.tenant, False)
        names = sorted(p["url"].rsplit("/", 1)[-1] for p in everything["photos"])
        assert names == ["dull.jpg", "table.jpg"], "no receipt, nothing unapproved"
        assert "Rivera" not in json.dumps(everything) and "Jordan" not in json.dumps(everything)

        # Another brand's user asking for this brand is held to their own.
        foreign = await self._gallery(self.other_client, self.tenant, True)
        assert foreign["photos"] == [] and foreign["total"] == 0

    # ── the cron endpoint ─────────────────────────────────────────────

    def test_cron_endpoint_needs_the_secret_and_reports_the_run(self, settings):
        settings.INTERNAL_CRON_SECRET = "s3cret"
        self._file("table.jpg", self.table, seed=2)
        url = "/internal/cron/score-recap-photos"
        assert Client().post(url).status_code == 401
        with patch("utils.gcs.download_blob_bytes", side_effect=lambda name: self.images.get(name)), patch(
            "recaps.photo_picks.generate_vision_json", return_value=_good()
        ) as vision:
            response = Client().post(
                url, {"since_days": "3650", "max_photos": "5"}, HTTP_X_CRON_SECRET="s3cret"
            )
        assert response.status_code == 200
        body = response.json()
        assert body["ok"] is True and body["report"]["scored"] == 1
        assert body["report"]["budget_photos"] == 5
        assert vision.call_count == 1
