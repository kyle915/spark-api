"""Campaign recap deck: no-product visits, photos, quotes, next steps, the
staff review → ready → brand download flow, big programs built off the
request, and tenant isolation.

Runs through the real ``schema_clients`` surface with the front end's field
set. The model is never called: ``utils.ai_text._post_json`` is patched and
what it would have been sent is asserted on.
"""

from __future__ import annotations

import base64
import json
from datetime import date, datetime
from io import BytesIO
from unittest.mock import patch

import pytest
from asgiref.sync import sync_to_async
from django.core import mail
from django.test import Client
from django.utils import timezone
from PIL import Image

from ambassadors.tests.base import AmbassadorsGraphQLTestCase
from events import models as event_models
from recap_decks import builder
from recap_decks.models import ClientRecapDeck
from recap_decks.no_product import (
    REASON_CHECK_NUMBERS,
    REASON_CHECK_ZERO,
    REASON_NO_PRODUCT,
    REASON_NOTHING_RECORDED,
    classify,
)
from recaps import client_recap_draft as drafts
from recaps import models as recap_models

DRAFT_FIELDS = """
  uuid tenantId tenantName startDate endDate period title summary takeaways textSource
  numbers { label value }
  weeks { label value }
  stores { label value }
  states { label value }
  quotes
  photos { id url category caption store state eventDate isPick }
  createdAt updatedAt
  status canEdit buildNote readyAt nextSteps
  deckNumbers { label value }
  noProductNote leftOutCount
  leftOutVisits { key day store state reason label evidence kept }
  visitsToCheck { key day store state reason label evidence kept }
  quoteChoices { text hidden }
"""

CREATE = (
    """
mutation Create($tenantId: ID!, $startDate: String!, $endDate: String!) {
  createClientRecapDraft(tenantId: $tenantId, startDate: $startDate, endDate: $endDate) {
    ok reason draft { %s }
  }
}
"""
    % DRAFT_FIELDS
)

UPDATE = (
    """
mutation Update($uuid: String!, $title: String, $summary: String, $photoIds: [ID!],
                $nextSteps: [String!], $hiddenQuotes: [String!], $keptVisits: [String!]) {
  updateClientRecapDraft(uuid: $uuid, title: $title, summary: $summary, photoIds: $photoIds,
                         nextSteps: $nextSteps, hiddenQuotes: $hiddenQuotes, keptVisits: $keptVisits) {
    ok reason draft { %s }
  }
}
"""
    % DRAFT_FIELDS
)

STATUS = (
    """
mutation Status($uuid: String!, $status: String!) {
  setClientRecapDeckStatus(uuid: $uuid, status: $status) { ok reason draft { %s } }
}
"""
    % DRAFT_FIELDS
)

READ = "query Read($uuid: String!) { clientRecapDraft(uuid: $uuid) { ok reason draft { %s } } }" % DRAFT_FIELDS
DECKS = (
    "query Decks($tenantId: ID!) { clientRecapDecks(tenantId: $tenantId) "
    "{ uuid tenantName title period readyAt } }"
)
DECK_PDF = (
    "query DeckPdf($uuid: String!) { clientRecapDeckPdf(uuid: $uuid) "
    "{ ok reason filename pdfBase64 } }"
)
BREAKDOWN = """
query Breakdown($tenantId: ID!, $startDate: String, $endDate: String) {
  tenantConsumerBreakdown(tenantId: $tenantId, startDate: $startDate, endDate: $endDate) { total recaps }
}
"""
KPIS = "query Kpis($tenantId: ID!, $year: Int) { tenantKpis(tenantId: $tenantId, year: $year) { recaps consumersReached cansSold } }"

START, END = "2026-01-01", "2026-12-31"


def _jpeg(color=(40, 120, 200)) -> bytes:
    out = BytesIO()
    Image.new("RGB", (1000, 750), color).save(out, format="JPEG")
    return out.getvalue()


def _reply(summary, takeaways, next_steps=()):
    content = json.dumps(
        {"summary": summary, "takeaways": list(takeaways), "next_steps": list(next_steps)}
    )
    return {"choices": [{"message": {"content": content}}]}


GOOD_REPLY = _reply(
    "Deck Two sampled 240 consumers across 4 demos in Jan 1 – Dec 31, 2026.",
    [
        "Costco had 160 consumers sampled.",
        "Consumers asked about the 12 pack.",
        "Mango was the flavor people mentioned.",
    ],
    [
        "Go back to Costco, which had 160 consumers sampled.",
        "Grow sampling by 25% next quarter.",
    ],
)


@pytest.mark.django_db(transaction=True)
class TestRecapDeck(AmbassadorsGraphQLTestCase):
    @pytest.fixture(autouse=True)
    def setup(self, db, settings):
        from config.schema_client import schema_clients

        settings.OPENAI_API_KEY = "test-key"
        settings.GS_BUCKET_NAME = "spark-test-bucket"
        self.roles = self.setup_default_roles()
        self.schema = schema_clients
        self.endpoint_path = "/api/v1/graphql/clients"
        self.sys = self.get_system_user()
        self.tenant = self.create_tenant(name="Deck Two")
        self.other = self.create_tenant(name="Deck Elsewhere")
        self.admin = self.create_user(
            username="deck2-admin", email="deck2-admin@test.com", role=self.roles["spark_admin"]
        )
        self.client_user = self.create_user(
            username="deck2-client", email="deck2-client@test.com", role=self.roles["client"]
        )
        self.create_tenanted_user(self.client_user, self.tenant)
        self.other_client = self.create_user(
            username="deck2-other", email="deck2-other@test.com", role=self.roles["client"]
        )
        self.create_tenanted_user(self.other_client, self.other)
        ba_user = self.create_user(
            username="jordan-rivera-deck2", email="jordan.rivera.deck2@ba.test",
            role=self.roles["ambassador"],
        )
        ba_user.first_name, ba_user.last_name = "Jordan", "Rivera"
        ba_user.save()
        self.ba_user = ba_user
        self.ba = self.create_ambassador(ba_user)
        self.create_tenanted_user(ba_user, self.tenant)

        self.et = event_models.EventType.objects.create(
            name="Retail Sampling", slug="deck2-retail", tenant=self.tenant, created_by=self.sys
        )
        self.template = recap_models.CustomRecapTemplate.objects.create(
            name="Deck2 Retail", event_type=self.et, tenant=self.tenant, created_by=self.sys
        )
        num = recap_models.CustomRecapFieldType.objects.create(name="Number", created_by=self.sys)
        section = recap_models.RecapSection.objects.create(
            name="Sampling", tenant=self.tenant, created_by=self.sys
        )

        def _field(name):
            return recap_models.CustomField.objects.create(
                name=name, custom_recap_template=self.template, custom_field_type=num,
                recap_section=section, created_by=self.sys,
            )

        self.f_sampled = _field("Total number of consumers sampled")
        self.f_cans = _field("Cans sold")
        self.f_feedback = _field("Consumer feedback")
        self.f_notes = _field("Notes about the demo")
        self.f_ba = _field("BA Name")
        self.ca = event_models.State.objects.create(name="California", code="CA", created_by=self.sys)
        self.az = event_models.State.objects.create(name="Arizona", code="AZ", created_by=self.sys)
        self.costco = event_models.Retailer.objects.create(
            name="Costco", tenant=self.tenant, created_by=self.sys
        )
        self.sprouts = event_models.Retailer.objects.create(
            name="Sprouts", tenant=self.tenant, created_by=self.sys
        )
        self.file_type = recap_models.FileType.objects.create(name="image", created_by=self.sys)
        self.table = recap_models.FileRecapCategory.objects.create(
            name="Table Set Up", tenant=self.tenant, created_by=self.sys
        )
        self.images: dict[str, bytes] = {}

        self.a = self._recap(10, 6, sampled="120", cans="30",
                             feedback="Loved the mango flavor. Jordan Rivera handed out cups.",
                             notes="Busy Saturday crowd near the front doors all day")
        self.b = self._recap(10, 14, sampled="80", cans="10", retailer=self.sprouts, state=self.az,
                             feedback="Asked where to buy the 12 pack")
        self.np_note = self._recap(10, 15, sampled="0", cans="0",
                                   feedback="NP QUOTE should never print",
                                   notes="No product on the shelf, the store never received the delivery")
        self.np_blank = self._recap(10, 16, sampled="", cans="", feedback="", notes="")
        self.zero_other = self._recap(10, 17, sampled="0", cans="0",
                                      notes="Manager refused to let us open the sample cans today")
        self.numbers_np = self._recap(10, 18, sampled="40", cans="5",
                                      feedback="Said it tasted like summer",
                                      notes="Product was out of stock by noon so we used our own")
        self.pending = self._recap(10, 19, sampled="999", cans="99", approved=False,
                                   feedback="PENDING QUOTE")

    # ── fixtures ──────────────────────────────────────────────────────

    def _recap(self, month, day, *, sampled, cans, feedback="", notes="", approved=True,
               retailer=None, state=None, tenant=None):
        when = timezone.make_aware(datetime(2026, month, day, 18))
        event = event_models.Event.objects.create(
            name=f"Sampling-Costco-{month}/{day}/2026-121", tenant=tenant or self.tenant,
            address="1 Main St", date=when, event_type=self.et,
            retailer=retailer or self.costco, state=state or self.ca,
            created_by=self.sys, updated_by=self.sys,
        )
        recap = recap_models.CustomRecap.objects.create(
            name="r", approved=approved, submitted_at=when, event=event,
            tenant=tenant or self.tenant, custom_recap_template=self.template,
            ambassador=self.ba, created_by=self.sys,
        )
        for field, value in (
            (self.f_sampled, sampled),
            (self.f_cans, cans),
            (self.f_feedback, feedback),
            (self.f_notes, notes),
            (self.f_ba, "Jordan Rivera"),
        ):
            if value != "":
                recap_models.CustomFieldValue.objects.create(
                    custom_recap=recap, custom_field=field, value=value, created_by=self.sys
                )
        return recap

    def _file(self, name, recap):
        self.images[f"recap_files/{name}"] = _jpeg()
        return recap_models.CustomRecapFile.objects.create(
            name=name, url=f"recap_files/{name}", custom_recap=recap,
            file_type=self.file_type, file_recap_category=self.table, created_by=self.sys,
        )

    async def _run(self, query, variables, user, reply=GOOD_REPLY):
        payloads = []

        def _post(url, payload, headers, *, timeout):
            payloads.append(payload)
            return reply

        with patch("utils.ai_text._post_json", side_effect=_post), patch(
            "recaps.client_recap_draft.feedback_themes", return_value=[]
        ):
            result = await self._execute_mutation_authenticated(
                query, variables, user, self.endpoint_path
            )
        assert result.errors is None, result.errors
        prompts = ["\n".join(str(m["content"]) for m in p["messages"]) for p in payloads]
        return result.data, prompts

    async def _create(self, user=None, tenant=None):
        data, prompts = await self._run(
            CREATE,
            {"tenantId": str((tenant or self.tenant).id), "startDate": START, "endDate": END},
            user or self.admin,
        )
        return data["createClientRecapDraft"], prompts

    async def _query(self, query, variables, user):
        result = await self._execute_query_authenticated(query, variables, user, self.endpoint_path)
        assert result.errors is None, result.errors
        return result.data

    # ── no-product visits ─────────────────────────────────────────────

    def test_classify_reads_the_numbers_first(self):
        assert classify(activity=0, notes="No product on the shelf") == REASON_NO_PRODUCT
        assert classify(activity=0, notes="") == REASON_NOTHING_RECORDED
        assert classify(activity=0, notes="Manager refused the samples") == REASON_CHECK_ZERO
        assert classify(activity=0, notes="Costco", has_sentence=False) == REASON_NOTHING_RECORDED
        # "Out of stock" with numbers usually means it sold out: kept, flagged.
        assert classify(activity=40, notes="Out of stock by noon") == REASON_CHECK_NUMBERS
        assert classify(activity=40, notes="Great day") is None

    @pytest.mark.asyncio
    async def test_numbers_match_the_dashboard_and_no_product_visits_are_left_out(self):
        payload, prompts = await self._create()
        assert payload["ok"] is True, payload
        draft = payload["draft"]
        assert draft["status"] == "draft" and draft["canEdit"] is True

        numbers = {n["label"]: n["value"] for n in draft["numbers"]}
        breakdown = await self._query(
            BREAKDOWN, {"tenantId": str(self.tenant.id), "startDate": START, "endDate": END},
            self.admin,
        )
        kpis = await self._query(KPIS, {"tenantId": str(self.tenant.id), "year": 2026}, self.admin)
        # Same totals as the dashboard: the left-out visits recorded nothing.
        assert numbers["Consumers sampled"] == breakdown["tenantConsumerBreakdown"]["total"] == 240
        assert numbers["Consumers sampled"] == kpis["tenantKpis"]["consumersReached"]
        assert numbers["Cans sold"] == kpis["tenantKpis"]["cansSold"] == 45
        assert numbers["Approved recaps"] == kpis["tenantKpis"]["recaps"] == 6

        tiles = {n["label"]: n["value"] for n in draft["deckNumbers"]}
        assert tiles["Demos"] == 4, "6 approved recaps less 2 no-product visits"
        assert tiles["Consumers per demo"] == 60
        assert tiles["Consumers sampled"] == 240 and tiles["Cans sold"] == 45
        assert tiles["Stores"] == 2 and tiles["States"] == 2
        assert draft["leftOutCount"] == 2
        assert "2 no-product visits" in draft["noProductNote"]

        left_out = {v["key"]: v for v in draft["leftOutVisits"]}
        assert set(left_out) == {f"C{self.np_note.id}", f"C{self.np_blank.id}"}
        assert left_out[f"C{self.np_note.id}"]["reason"] == "no_product"
        assert "No product on the shelf" in left_out[f"C{self.np_note.id}"]["evidence"]
        assert left_out[f"C{self.np_blank.id}"]["reason"] == "nothing_recorded"
        check = {v["key"]: v["reason"] for v in draft["visitsToCheck"]}
        assert check == {
            f"C{self.zero_other.id}": "zero_other_reason",
            f"C{self.numbers_np.id}": "numbers_but_no_product_note",
        }

        # Quotes: nothing from a no-product visit, nothing pending, no names.
        assert all("NP QUOTE" not in q and "PENDING" not in q for q in draft["quotes"])
        assert any("Loved the mango flavor" in q for q in draft["quotes"])
        assert "Rivera" not in json.dumps(draft)

        # The model got the deck's own counts and never the visit list.
        assert len(prompts) == 1
        assert '"demos": 4' in prompts[0] and '"consumers_per_demo": 60' in prompts[0]
        assert "never received the delivery" not in prompts[0]
        assert "Rivera" not in prompts[0]

        # Next steps: the grounded one is kept, the invented 25% is not.
        assert draft["nextSteps"] == ["Go back to Costco, which had 160 consumers sampled."]
        assert len(mail.outbox) == 0

    @pytest.mark.asyncio
    async def test_staff_can_count_a_left_out_visit_back_in(self):
        payload, _ = await self._create()
        uuid = payload["draft"]["uuid"]
        data, _ = await self._run(
            UPDATE, {"uuid": uuid, "keptVisits": [f"C{self.np_blank.id}", "C999999"]}, self.admin
        )
        draft = data["updateClientRecapDraft"]["draft"]
        tiles = {n["label"]: n["value"] for n in draft["deckNumbers"]}
        assert tiles["Demos"] == 5 and tiles["Consumers per demo"] == 48
        assert draft["leftOutCount"] == 1
        kept = {v["key"]: v["kept"] for v in draft["leftOutVisits"]}
        assert kept[f"C{self.np_blank.id}"] is True and kept[f"C{self.np_note.id}"] is False

    # ── photos ────────────────────────────────────────────────────────

    def test_photos_never_include_no_product_or_unusable_shots(self):
        good = self._file("good.jpg", self.a)
        np_photo = self._file("np.jpg", self.np_note)
        small = self._file("small.jpg", self.b)
        unreadable = self._file("bad.jpg", self.b)
        recap_models.RecapPhotoScore.objects.create(
            custom_recap_file=good, status="scored", score=90, kind="table_setup", model_pick=True
        )
        recap_models.RecapPhotoScore.objects.create(
            custom_recap_file=small, status="skipped", skip_reason="low_resolution"
        )
        recap_models.RecapPhotoScore.objects.create(custom_recap_file=unreadable, status="unreadable")
        for f in (np_photo, small, unreadable):
            recap_models.RecapPhotoStaffPick.objects.create(custom_recap_file=f, choice="pinned")

        with patch("utils.ai_text._post_json", return_value=GOOD_REPLY), patch(
            "recaps.client_recap_draft.feedback_themes", return_value=[]
        ):
            draft = drafts.create_draft(self.tenant, date(2026, 1, 1), date(2026, 12, 31), self.admin)
        assert draft.photo_ids == [good.id], "staff pins never beat the no-product / unusable rule"

        # Asking for them by hand is refused too.
        drafts.update_draft(draft, self.admin, photo_ids=[np_photo.id, small.id, unreadable.id, good.id])
        assert draft.photo_ids == [good.id]

        # Counting the visit back in lets its photo in.
        builder.update_deck(draft, kept_visits=[f"C{self.np_note.id}"])
        drafts.update_draft(draft, self.admin, photo_ids=[np_photo.id, good.id])
        assert draft.photo_ids == [np_photo.id, good.id]
        # …and leaving it out again takes the photo back out.
        builder.update_deck(draft, kept_visits=[])
        draft.refresh_from_db()
        assert draft.photo_ids == [good.id]

    # ── quotes and next steps ─────────────────────────────────────────

    def test_quotes_are_trimmed_not_reworded(self):
        assert drafts.trim_quote('  "Loved it."  ') == "Loved it."
        sentences = (
            "The mango was the favorite of the day and people asked for the twelve pack. "
            "They also asked about the lime flavor for a party this weekend."
        )
        assert drafts.trim_quote(sentences + " " + "word " * 40) == sentences
        # A sentence end too early to be worth keeping: cut at a word instead.
        assert drafts.trim_quote("Great. " + "word " * 80).endswith("…")
        no_stop = "word " * 80
        trimmed = drafts.trim_quote(no_stop)
        assert trimmed.endswith("…") and len(trimmed) <= drafts.MAX_QUOTE_CHARS + 1

    @pytest.mark.asyncio
    async def test_staff_hide_quotes_and_edit_next_steps(self):
        payload, _ = await self._create()
        uuid = payload["draft"]["uuid"]
        first = payload["draft"]["quotes"][0]
        data, _ = await self._run(
            UPDATE,
            {"uuid": uuid, "hiddenQuotes": [first, "not a quote"], "nextSteps": ["Book March.", " "]},
            self.admin,
        )
        draft = data["updateClientRecapDraft"]["draft"]
        assert first not in draft["quotes"]
        assert {c["text"]: c["hidden"] for c in draft["quoteChoices"]}[first] is True
        assert draft["nextSteps"] == ["Book March."]

    # ── review, ready, brand download ─────────────────────────────────

    @pytest.mark.asyncio
    async def test_brand_sees_a_deck_only_after_staff_mark_it_ready(self):
        await sync_to_async(self._file)("good.jpg", self.a)
        payload, _ = await self._create()
        uuid = payload["draft"]["uuid"]

        # A staff draft: invisible to the brand.
        assert (await self._query(DECKS, {"tenantId": str(self.tenant.id)}, self.client_user))[
            "clientRecapDecks"
        ] == []
        pdf = (await self._query(DECK_PDF, {"uuid": uuid}, self.client_user))["clientRecapDeckPdf"]
        assert pdf["ok"] is False and pdf["pdfBase64"] == ""

        # Brand users can't mark it ready.
        data, _ = await self._run(STATUS, {"uuid": uuid, "status": "ready"}, self.client_user)
        assert data["setClientRecapDeckStatus"]["ok"] is False

        data, _ = await self._run(STATUS, {"uuid": uuid, "status": "ready"}, self.admin)
        result = data["setClientRecapDeckStatus"]
        assert result["ok"] is True and result["draft"]["status"] == "ready"
        assert result["draft"]["readyAt"] and result["draft"]["canEdit"] is False
        assert len(mail.outbox) == 0, "marking ready sends nothing"

        listed = (await self._query(DECKS, {"tenantId": str(self.tenant.id)}, self.client_user))[
            "clientRecapDecks"
        ]
        assert [d["uuid"] for d in listed] == [uuid]
        assert listed[0]["title"] == "Deck Two recap"

        # Another brand's user, even asking for this brand by id, gets nothing.
        foreign = await self._query(DECKS, {"tenantId": str(self.tenant.id)}, self.other_client)
        assert foreign["clientRecapDecks"] == []
        pdf = (await self._query(DECK_PDF, {"uuid": uuid}, self.other_client))["clientRecapDeckPdf"]
        assert pdf["ok"] is False
        # Ambassadors on the brand get nothing either.
        assert (await self._query(DECKS, {"tenantId": str(self.tenant.id)}, self.ba_user))[
            "clientRecapDecks"
        ] == []
        pdf = (await self._query(DECK_PDF, {"uuid": uuid}, self.ba_user))["clientRecapDeckPdf"]
        assert pdf["ok"] is False

        # A ready deck is read-only until it goes back to draft.
        data, _ = await self._run(UPDATE, {"uuid": uuid, "summary": "changed"}, self.admin)
        assert data["updateClientRecapDraft"]["ok"] is False
        assert "Move it back to draft" in data["updateClientRecapDraft"]["reason"]

        data, _ = await self._run(STATUS, {"uuid": uuid, "status": "draft"}, self.admin)
        assert data["setClientRecapDeckStatus"]["draft"]["status"] == "draft"
        assert (await self._query(DECKS, {"tenantId": str(self.tenant.id)}, self.client_user))[
            "clientRecapDecks"
        ] == []

    @pytest.mark.asyncio
    async def test_the_brand_downloads_the_ready_pdf(self):
        pytest.importorskip("weasyprint")
        try:
            import weasyprint  # noqa: F401
        except OSError:
            pytest.skip("WeasyPrint native libraries are not installed here")
        await sync_to_async(self._file)("good.jpg", self.a)
        payload, _ = await self._create()
        uuid = payload["draft"]["uuid"]
        await self._run(STATUS, {"uuid": uuid, "status": "ready"}, self.admin)
        with patch("utils.gcs.download_blob_bytes", side_effect=lambda n: self.images.get(n)):
            pdf = (await self._query(DECK_PDF, {"uuid": uuid}, self.client_user))[
                "clientRecapDeckPdf"
            ]
        assert pdf["ok"] is True, pdf["reason"]
        assert base64.b64decode(pdf["pdfBase64"]).startswith(b"%PDF")
        assert pdf["filename"] == "deck-two-recap-2026-01-01-to-2026-12-31.pdf"

    # ── the deck itself ───────────────────────────────────────────────

    def test_deck_html_says_what_was_left_out_and_names_no_one(self):
        with patch("utils.ai_text._post_json", return_value=GOOD_REPLY), patch(
            "recaps.client_recap_draft.feedback_themes", return_value=[]
        ):
            draft = drafts.create_draft(self.tenant, date(2026, 1, 1), date(2026, 12, 31), self.admin)
        html = drafts.build_draft_html(draft, [])
        for heading in ("At a glance", "Summary", "Where it ran", "What consumers said",
                        "Next steps", "How we counted", "No-product visits left out"):
            assert heading in html, heading
        assert "Demos" in html and "Consumers per demo" in html
        assert "2 no-product visits" in html
        # The reason prints; the ambassador's notes never do.
        assert "No product on hand" in html and "never received the delivery" not in html
        for banned in ("Rivera", "Jordan", "NP QUOTE", "PENDING QUOTE", "Sampling-Costco"):
            assert banned not in html, banned
        css = drafts._DRAFT_CSS.lower()
        assert "gradient" not in css and "box-shadow" not in css and "text-transform" not in css
        import re

        sizes = [int(px) for px in re.findall(r"font-size:\s*(\d+)px", drafts._DRAFT_CSS)]
        assert sizes and min(sizes) >= 12, "nothing under 12px"

    # ── big programs ──────────────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_a_big_program_is_finished_off_the_request(self, settings, monkeypatch):
        monkeypatch.setattr(builder, "INLINE_MAX_RECAPS", 2)
        payload, prompts = await self._create()
        assert prompts == [], "no model call inside the staff member's request"
        draft = payload["draft"]
        assert draft["status"] == "building" and draft["canEdit"] is False
        assert draft["textSource"] == "fallback"
        assert {n["label"]: n["value"] for n in draft["deckNumbers"]}["Demos"] == 4
        uuid = draft["uuid"]

        # Not editable or shareable while it is being written.
        data, _ = await self._run(UPDATE, {"uuid": uuid, "summary": "x"}, self.admin)
        assert data["updateClientRecapDraft"]["ok"] is False
        data, _ = await self._run(STATUS, {"uuid": uuid, "status": "ready"}, self.admin)
        assert data["setClientRecapDeckStatus"]["ok"] is False

        settings.INTERNAL_CRON_SECRET = "s3cret"
        url = "/internal/cron/build-recap-decks"

        def _cron(**headers):
            with patch("utils.ai_text._post_json", return_value=GOOD_REPLY), patch(
                "recaps.client_recap_draft.feedback_themes", return_value=[]
            ):
                return Client().post(url, **headers)

        assert (await sync_to_async(_cron)()).status_code == 401
        response = await sync_to_async(_cron)(HTTP_X_CRON_SECRET="s3cret")
        assert response.status_code == 200, response.content
        assert response.json()["report"]["done"] == 1

        read = (await self._query(READ, {"uuid": uuid}, self.admin))["clientRecapDraft"]["draft"]
        assert read["status"] == "draft" and read["textSource"] == "model"
        assert read["nextSteps"] == ["Go back to Costco, which had 160 consumers sampled."]

        # A second run finds nothing to do.
        response = await sync_to_async(_cron)(HTTP_X_CRON_SECRET="s3cret")
        assert response.json()["report"]["done"] == 0

    def test_a_build_is_claimed_once_and_given_up_after_three_tries(self, monkeypatch):
        monkeypatch.setattr(builder, "INLINE_MAX_RECAPS", 0)
        with patch("utils.ai_text._post_json", return_value=GOOD_REPLY):
            draft = builder.start_deck(self.tenant, date(2026, 1, 1), date(2026, 12, 31), self.admin)
        deck = builder.deck_for(draft)
        assert deck.status == ClientRecapDeck.STATUS_BUILDING
        # Someone else is on it (started just now): skipped.
        ClientRecapDeck.objects.filter(id=deck.id).update(build_started_at=timezone.now())
        assert builder.finish_build(deck.id) == "skipped"
        # Three earlier attempts that never finished: handed to staff as is.
        ClientRecapDeck.objects.filter(id=deck.id).update(
            build_started_at=None, build_attempts=builder.MAX_BUILD_ATTEMPTS
        )
        assert builder.finish_build(deck.id) == "gave_up"
        deck.refresh_from_db()
        assert deck.status == "draft" and "built from the numbers" in deck.build_note

    def test_the_task_endpoint_needs_its_secret(self, settings):
        settings.CLOUD_TASKS_SECRET = "t0ken"
        url = "/api/tasks/recap-deck-build"
        assert Client().post(url, "{}", content_type="application/json").status_code == 403
        response = Client().post(
            url, json.dumps({"deck": 123456}), content_type="application/json",
            HTTP_X_TASKS_SECRET="t0ken",
        )
        assert response.status_code == 200 and response.json()["outcome"] == "skipped"

    # ── isolation ─────────────────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_another_brands_recaps_never_reach_the_deck(self):
        def _foreign():
            et = event_models.EventType.objects.create(
                name="Other", slug="deck2-other", tenant=self.other, created_by=self.sys
            )
            when = timezone.make_aware(datetime(2026, 10, 9, 18))
            event = event_models.Event.objects.create(
                name="x", tenant=self.other, address="2 Main", date=when, event_type=et,
                created_by=self.sys, updated_by=self.sys,
            )
            return recap_models.CustomRecap.objects.create(
                name="r", approved=True, submitted_at=when, event=event, tenant=self.other,
                custom_recap_template=self.template, ambassador=self.ba, created_by=self.sys,
            )

        await sync_to_async(_foreign)()
        payload, _ = await self._create()
        draft = payload["draft"]
        assert {n["label"]: n["value"] for n in draft["numbers"]}["Approved recaps"] == 6
        assert draft["leftOutCount"] == 2

        # Brand users can't draft.
        denied, prompts = await self._create(user=self.client_user)
        assert denied == {"ok": False, "reason": "Not available.", "draft": None}
        assert prompts == []
