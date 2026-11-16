"""Client recap draft: staff only, approved recaps only, no ambassador
names in the prompt, numbers from the dashboard roll-ups, and a manual
path when the model is unavailable.

Runs through the real ``schema_clients`` surface with the front end's
field set. The model is never called: ``utils.ai_text._post_json`` is
patched and the prompt it would have received is asserted on.
"""

from __future__ import annotations

import base64
import json
import sys
from datetime import datetime
from io import BytesIO
from unittest.mock import patch

import pytest
from asgiref.sync import sync_to_async
from django.core import mail
from django.utils import timezone
from PIL import Image

from ambassadors.tests.base import AmbassadorsGraphQLTestCase
from events import models as event_models
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
mutation Update($uuid: String!, $title: String, $summary: String, $takeaways: [String!], $photoIds: [ID!]) {
  updateClientRecapDraft(uuid: $uuid, title: $title, summary: $summary, takeaways: $takeaways, photoIds: $photoIds) {
    ok reason draft { %s }
  }
}
"""
    % DRAFT_FIELDS
)

READ = "query Read($uuid: String!) { clientRecapDraft(uuid: $uuid) { ok reason draft { %s } } }" % DRAFT_FIELDS
LIST = "query List($tenantId: ID!) { clientRecapDrafts(tenantId: $tenantId) { uuid tenantName title period updatedAt } }"
PDF = "query Pdf($uuid: String!) { clientRecapDraftPdf(uuid: $uuid) { ok reason filename pdfBase64 } }"

BREAKDOWN = """
query Breakdown($tenantId: ID!, $startDate: String, $endDate: String) {
  tenantConsumerBreakdown(tenantId: $tenantId, startDate: $startDate, endDate: $endDate) { total recaps }
}
"""

START, END = "2026-10-01", "2026-10-31"


def _jpeg(color=(40, 120, 200)) -> bytes:
    out = BytesIO()
    Image.new("RGB", (1000, 750), color).save(out, format="JPEG")
    return out.getvalue()


def _reply(summary, takeaways):
    content = json.dumps({"summary": summary, "takeaways": takeaways})
    return {"choices": [{"message": {"content": content}}]}


@pytest.mark.django_db(transaction=True)
class TestClientRecapDraft(AmbassadorsGraphQLTestCase):
    @pytest.fixture(autouse=True)
    def setup(self, db, settings):
        from config.schema_client import schema_clients

        settings.OPENAI_API_KEY = "test-key"
        settings.GS_BUCKET_NAME = "spark-test-bucket"
        self.roles = self.setup_default_roles()
        self.schema = schema_clients
        self.endpoint_path = "/api/v1/graphql/clients"
        self.sys = self.get_system_user()
        self.tenant = self.create_tenant(name="Deck Brand")
        self.other = self.create_tenant(name="Deck Other")
        self.admin = self.create_user(
            username="deck-admin", email="deck-admin@test.com", role=self.roles["spark_admin"]
        )
        self.client_user = self.create_user(
            username="deck-client", email="deck-client@test.com", role=self.roles["client"]
        )
        self.create_tenanted_user(self.client_user, self.tenant)
        ba_user = self.create_user(
            username="jordan-rivera-deck", email="jordan.rivera.deck@ba.test",
            role=self.roles["ambassador"],
        )
        ba_user.first_name, ba_user.last_name = "Jordan", "Rivera"
        ba_user.save()
        self.ba = self.create_ambassador(ba_user)

        self.et = event_models.EventType.objects.create(
            name="Retail Sampling", slug="deck-retail", tenant=self.tenant, created_by=self.sys
        )
        self.template = recap_models.CustomRecapTemplate.objects.create(
            name="Deck Retail", event_type=self.et, tenant=self.tenant, created_by=self.sys
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

        self.sampled = _field("Total number of consumers sampled")
        self.feedback = _field("Consumer feedback")
        self.ba_name = _field("BA Name")
        self.ca = event_models.State.objects.create(name="California", code="CA", created_by=self.sys)
        self.costco = event_models.Retailer.objects.create(
            name="Costco", tenant=self.tenant, created_by=self.sys
        )
        self.file_type = recap_models.FileType.objects.create(name="image", created_by=self.sys)
        self.table = self._category("Table Set Up")
        self.spend = self._category("Product Spend")

        self.approved = self._recap(
            day=6, sampled=120, approved=True,
            feedback="APPROVED QUOTE loved the peach flavor. Jordan Rivera ran out of cups.",
        )
        self.approved_two = self._recap(
            day=14, sampled=80, approved=True, feedback="APPROVED SECOND would buy 2 packs",
        )
        self.pending = self._recap(day=15, sampled=999, approved=False, feedback="PENDING QUOTE")
        self.archived = self._recap(
            day=16, sampled=555, approved=True, feedback="ARCHIVED QUOTE",
            archived_at=timezone.now(),
        )
        self.outside = self._recap(day=3, month=9, sampled=333, approved=True, feedback="SEPTEMBER QUOTE")
        self.images = {}

    # ── fixtures ──────────────────────────────────────────────────────

    def _category(self, name, tenant=None):
        return recap_models.FileRecapCategory.objects.create(
            name=name, tenant=tenant or self.tenant, created_by=self.sys
        )

    def _recap(self, *, day, sampled, approved, feedback, month=10, tenant=None, **kwargs):
        when = timezone.make_aware(datetime(2026, month, day, 18))
        event = event_models.Event.objects.create(
            name=f"Sampling-Costco-{month}/{day}/2026-121", tenant=tenant or self.tenant,
            address="1 Main St", date=when, event_type=self.et, retailer=self.costco,
            state=self.ca, created_by=self.sys, updated_by=self.sys,
        )
        recap = recap_models.CustomRecap.objects.create(
            name="r", approved=approved, submitted_at=when, event=event,
            tenant=tenant or self.tenant, custom_recap_template=self.template,
            ambassador=self.ba, created_by=self.sys, **kwargs,
        )
        for field, value in (
            (self.sampled, str(sampled)),
            (self.feedback, feedback),
            (self.ba_name, "Jordan Rivera"),
        ):
            recap_models.CustomFieldValue.objects.create(
                custom_recap=recap, custom_field=field, value=value, created_by=self.sys
            )
        return recap

    def _file(self, name, recap, category=None):
        self.images[f"recap_files/{name}"] = _jpeg()
        return recap_models.CustomRecapFile.objects.create(
            name=name, url=f"recap_files/{name}", custom_recap=recap,
            file_type=self.file_type, file_recap_category=category or self.table,
            created_by=self.sys,
        )

    async def _create(self, user, reply=None, tenant=None, start=START, end=END):
        """Ask for a draft with the model replaced. Returns the payload and
        the prompts the model would have been sent."""
        payloads = []

        def _post(url, payload, headers, *, timeout):
            payloads.append(payload)
            if isinstance(reply, Exception):
                raise reply
            return reply or _reply(
                "Deck Brand sampled 755 consumers across 3 events in Oct 1 – Oct 31, 2026.",
                [
                    "Costco had 755 consumers sampled.",
                    "Consumers liked the peach flavor.",
                    "Every recap in the period was approved.",
                ],
            )

        with patch("utils.ai_text._post_json", side_effect=_post):
            result = await self._execute_mutation_authenticated(
                CREATE,
                {"tenantId": str((tenant or self.tenant).id), "startDate": start, "endDate": end},
                user,
                self.endpoint_path,
            )
        assert result.errors is None, result.errors
        prompts = [
            "\n".join(str(m["content"]) for m in p["messages"]) for p in payloads
        ]
        return result.data["createClientRecapDraft"], prompts

    async def _query(self, query, variables, user):
        result = await self._execute_query_authenticated(query, variables, user, self.endpoint_path)
        assert result.errors is None, result.errors
        return result.data

    # ── staff: numbers, prompt, words ─────────────────────────────────

    @pytest.mark.asyncio
    async def test_staff_draft_uses_dashboard_numbers_and_approved_recaps_only(self):
        payload, prompts = await self._create(self.admin)
        assert payload["ok"] is True, payload
        draft = payload["draft"]
        numbers = {n["label"]: n["value"] for n in draft["numbers"]}

        # Same number the dashboard's breakdown query returns for the window.
        breakdown = await self._query(
            BREAKDOWN,
            {"tenantId": str(self.tenant.id), "startDate": START, "endDate": END},
            self.admin,
        )
        # 120 + 80 approved, plus 555 on an approved recap that was later
        # archived: the dashboard roll-ups count it today, and the draft
        # prints what the dashboard prints. The 999 awaiting review and the
        # 333 from September stay out.
        assert numbers["Consumers sampled"] == breakdown["tenantConsumerBreakdown"]["total"] == 755
        assert numbers["Approved recaps"] == 3 and numbers["Events"] == 3
        assert "Cans sold" not in numbers, "a zero the form does not collect is left out"
        assert draft["stores"] == [{"label": "Costco", "value": 755}]
        assert draft["states"] == [{"label": "CA", "value": 755}]
        assert draft["period"] == "Oct 1 – Oct 31, 2026"

        # One model call, and what it was given.
        assert len(prompts) == 1
        prompt = prompts[0]
        assert "APPROVED QUOTE" in prompt and "APPROVED SECOND" in prompt
        for hidden in ("PENDING QUOTE", "ARCHIVED QUOTE", "SEPTEMBER QUOTE", "999", "555", "333"):
            assert hidden not in prompt, hidden
        assert "Rivera" not in prompt and "Jordan" not in prompt
        assert "the brand ambassador ran out of cups" in prompt
        assert '"value": 755' in prompt, "numbers are handed over, labelled"
        assert "Never add, subtract, divide" in prompt
        assert "Do not name or describe any person" in prompt

        assert draft["textSource"] == "model"
        assert draft["summary"].startswith("Deck Brand sampled 755 consumers")
        assert len(draft["takeaways"]) == 3
        assert all("Rivera" not in q for q in draft["quotes"])
        assert "Rivera" not in json.dumps(draft)
        assert len(mail.outbox) == 0, "a draft sends nothing"

    @pytest.mark.asyncio
    async def test_a_reply_with_a_number_that_is_not_in_the_facts_is_not_kept(self):
        payload, _prompts = await self._create(
            self.admin,
            reply=_reply(
                "Deck Brand reached 4,500 consumers, a 37% lift.",
                ["Costco had 755 consumers sampled.", "Sales rose 12%.", "Peach was popular."],
            ),
        )
        draft = payload["draft"]
        assert draft["textSource"] == "fallback"
        assert "4,500" not in draft["summary"] and "37%" not in draft["summary"]
        assert "3 events" in draft["summary"] and "Consumers sampled: 755." in draft["summary"]

        # A sound summary with one invented takeaway keeps the rest.
        payload, _prompts = await self._create(
            self.admin,
            reply=_reply(
                "Deck Brand sampled 755 consumers in Oct 1 – Oct 31, 2026.",
                ["Costco had 755 consumers sampled.", "Sales rose 12%.", "Peach was popular."],
            ),
        )
        draft = payload["draft"]
        assert draft["textSource"] == "model"
        assert "Sales rose 12%." not in draft["takeaways"]
        assert len(draft["takeaways"]) == 3, "topped up from the facts"

    @pytest.mark.asyncio
    async def test_a_name_in_the_models_reply_is_taken_out(self):
        payload, _prompts = await self._create(
            self.admin,
            reply=_reply(
                "Jordan Rivera sampled 755 consumers at Costco.",
                ["Costco had 755 consumers sampled.", "Peach was popular.", "Cups ran short."],
            ),
        )
        assert "Rivera" not in payload["draft"]["summary"]
        assert "the brand ambassador sampled 755" in payload["draft"]["summary"]

    @pytest.mark.asyncio
    async def test_without_the_model_staff_still_get_a_draft_to_edit(self, settings):
        from utils.ai_text import AiUnavailable

        # No key: the model is not called at all.
        settings.OPENAI_API_KEY = ""
        payload, prompts = await self._create(self.admin)
        assert prompts == []
        assert payload["ok"] is True
        draft = payload["draft"]
        assert draft["textSource"] == "fallback"
        assert "Deck Brand ran 3 events in Oct 1 – Oct 31, 2026" in draft["summary"]
        assert any("Costco had the most consumers sampled: 755" in t for t in draft["takeaways"])

        # The service is down mid-call: same result.
        settings.OPENAI_API_KEY = "test-key"
        payload, _prompts = await self._create(self.admin, reply=AiUnavailable("down"))
        assert payload["ok"] is True and payload["draft"]["textSource"] == "fallback"

    @pytest.mark.asyncio
    async def test_a_window_with_no_approved_recaps_never_calls_the_model(self):
        payload, prompts = await self._create(self.admin, start="2025-01-01", end="2025-01-31")
        assert payload["ok"] is True and prompts == []
        assert payload["draft"]["textSource"] == "fallback"

    def test_a_date_in_the_facts_does_not_make_a_number_true(self):
        facts = {
            "period": "Oct 1 – Oct 31, 2026",
            "numbers": [{"key": "consumers_reached", "label": "Consumers sampled", "value": 1755}],
            "top_stores": [{"label": "Costco", "consumers_sampled": 755, "recaps": 3}],
            "states": [],
            "consumers_sampled_by_week": [{"week_of": "2026-10-12", "consumers_sampled": 635, "recaps": 2}],
            "comments": ["would buy 2 packs", "half off, 50% less sugar"],
        }
        assert drafts.text_is_grounded("Costco had 755 consumers sampled across 3 recaps.", facts)
        assert drafts.text_is_grounded("In Oct 1 – Oct 31, 2026, 1,755 consumers were sampled.", facts)
        assert drafts.text_is_grounded("One shopper said they would buy 2 packs.", facts)
        assert drafts.text_is_grounded("Consumers mentioned 50% less sugar.", facts)
        assert not drafts.text_is_grounded("Sales rose 12%.", facts)
        assert not drafts.text_is_grounded("The week of the 12 was the busiest.", facts)
        assert not drafts.text_is_grounded("Roughly 43% of consumers bought.", facts)
        assert not drafts.text_is_grounded("About 1,800 consumers were sampled.", facts)

    # ── brand users ───────────────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_brand_users_cannot_make_read_edit_or_download_a_draft(self):
        payload, prompts = await self._create(self.client_user)
        assert payload == {"ok": False, "reason": "Not available.", "draft": None}
        assert prompts == [], "the model must not be called"
        assert await sync_to_async(recap_models.ClientRecapDraft.objects.count)() == 0

        staff_payload, _ = await self._create(self.admin)
        uuid = staff_payload["draft"]["uuid"]
        data = await self._query(READ, {"uuid": uuid}, self.client_user)
        assert data["clientRecapDraft"] == {"ok": False, "reason": "Not available.", "draft": None}
        data = await self._query(LIST, {"tenantId": str(self.tenant.id)}, self.client_user)
        assert data["clientRecapDrafts"] == []
        data = await self._query(PDF, {"uuid": uuid}, self.client_user)
        assert data["clientRecapDraftPdf"]["ok"] is False
        assert data["clientRecapDraftPdf"]["pdfBase64"] == ""
        result = await self._execute_mutation_authenticated(
            UPDATE, {"uuid": uuid, "summary": "hacked"}, self.client_user, self.endpoint_path
        )
        assert result.errors is None, result.errors
        assert result.data["updateClientRecapDraft"]["ok"] is False
        row = await sync_to_async(recap_models.ClientRecapDraft.objects.get)(uuid=uuid)
        assert row.summary != "hacked"

    # ── photos ────────────────────────────────────────────────────────

    def test_default_photos_are_staff_pins_then_the_scorers_picks(self):
        scored = self._file("scored.jpg", self.approved)
        pinned = self._file("pinned.jpg", self.approved_two)
        unpinned = self._file("unpinned.jpg", self.approved_two)
        receipt = self._file("receipt.jpg", self.approved, self.spend)
        hidden = self._file("pending.jpg", self.pending)
        for f, score in ((scored, 90), (unpinned, 95), (hidden, 99)):
            recap_models.RecapPhotoScore.objects.create(
                custom_recap_file=f, status="scored", score=score, kind="table_setup",
                caption="Sampling table set up", model_pick=True,
            )
        recap_models.RecapPhotoStaffPick.objects.create(custom_recap_file=pinned, choice="pinned")
        recap_models.RecapPhotoStaffPick.objects.create(custom_recap_file=unpinned, choice="unpinned")
        recap_models.RecapPhotoStaffPick.objects.create(custom_recap_file=receipt, choice="pinned")
        for extra in range(3):  # enough picks that nothing is filled in
            f = self._file(f"more{extra}.jpg", self.approved)
            recap_models.RecapPhotoScore.objects.create(
                custom_recap_file=f, status="scored", score=60 + extra, kind="shelf", model_pick=True
            )

        from datetime import date

        ids = drafts.default_photo_ids(self.tenant.id, date(2026, 10, 1), date(2026, 10, 31))
        assert ids[0] == pinned.id, "staff pins first"
        assert ids[1] == scored.id, "then the best score"
        assert unpinned.id not in ids and receipt.id not in ids and hidden.id not in ids
        assert len([i for i in ids if i != pinned.id]) == 2, "at most two per recap"

    def test_with_no_picks_yet_one_field_photo_per_recap_is_used(self):
        from datetime import date

        a = self._file("a-table.jpg", self.approved)
        self._file("a-receipt.jpg", self.approved, self.spend)
        b = self._file("b-table.jpg", self.approved_two)
        self._file("b-second.jpg", self.approved_two)
        self._file("pending.jpg", self.pending)
        ids = drafts.default_photo_ids(self.tenant.id, date(2026, 10, 1), date(2026, 10, 31))
        assert sorted(ids) == sorted([a.id, b.id])

    @pytest.mark.asyncio
    async def test_staff_edit_the_words_and_swap_photos(self):
        keep = await sync_to_async(self._file)("keep.jpg", self.approved)
        swap_in = await sync_to_async(self._file)("swap.jpg", self.approved_two)
        receipt = await sync_to_async(self._file)("receipt.jpg", self.approved, self.spend)
        hidden = await sync_to_async(self._file)("pending.jpg", self.pending)

        def _foreign():
            recap = self._recap(day=7, sampled=1, approved=True, feedback="x", tenant=self.other)
            return self._file("foreign.jpg", recap, self._category("Table", self.other))

        foreign = await sync_to_async(_foreign)()
        payload, _ = await self._create(self.admin)
        uuid = payload["draft"]["uuid"]
        assert sorted(p["id"] for p in payload["draft"]["photos"]) == sorted(
            [str(keep.id), str(swap_in.id)]
        )

        result = await self._execute_mutation_authenticated(
            UPDATE,
            {
                "uuid": uuid,
                "title": "Deck Brand October recap",
                "summary": "Edited by staff.",
                "takeaways": ["First point.", "  ", "Second point."],
                "photoIds": [str(i) for i in (swap_in.id, receipt.id, hidden.id, foreign.id, swap_in.id)],
            },
            self.admin,
            self.endpoint_path,
        )
        assert result.errors is None, result.errors
        draft = result.data["updateClientRecapDraft"]["draft"]
        assert draft["title"] == "Deck Brand October recap"
        assert draft["summary"] == "Edited by staff."
        assert draft["takeaways"] == ["First point.", "Second point."]
        assert draft["textSource"] == "edited"
        assert [p["id"] for p in draft["photos"]] == [str(swap_in.id)], (
            "a receipt, an unapproved recap's photo and another brand's photo are refused"
        )
        assert draft["photos"][0]["store"] == "Costco"

        listed = await self._query(LIST, {"tenantId": str(self.tenant.id)}, self.admin)
        assert [d["title"] for d in listed["clientRecapDrafts"]] == ["Deck Brand October recap"]
        read = await self._query(READ, {"uuid": uuid}, self.admin)
        assert read["clientRecapDraft"]["draft"]["summary"] == "Edited by staff."

    # ── the PDF ───────────────────────────────────────────────────────

    def _draft(self):
        from datetime import date

        with patch("utils.ai_text._post_json", return_value=_reply(
            "Deck Brand sampled 755 consumers in Oct 1 – Oct 31, 2026.",
            ["Costco had 755 consumers sampled.", "Peach was popular.", "Cups ran short."],
        )):
            return drafts.create_draft(self.tenant, date(2026, 10, 1), date(2026, 10, 31), self.admin)

    def test_deck_html_is_plain_and_names_no_ambassador(self):
        self._file("table.jpg", self.approved)
        draft = self._draft()
        with patch("utils.gcs.download_blob_bytes", side_effect=lambda name: self.images.get(name)):
            embedded = drafts._embed_photos(drafts.draft_photos(draft))
        assert len(embedded) == 1
        html = drafts.build_draft_html(draft, embedded)
        assert "Deck Brand recap" in html and "Oct 1 – Oct 31, 2026" in html
        assert "At a glance" in html and "What consumers said" in html and "From the field" in html
        assert "APPROVED QUOTE loved the peach flavor" in html
        for banned in ("Rivera", "Jordan", "PENDING QUOTE", "ARCHIVED QUOTE"):
            assert banned not in html, banned
        # Store names come from the retailer, never the raw event title.
        assert "Sampling-Costco" not in html and "Costco" in html
        lowered = html.lower()
        assert "ai generated" not in lowered and "ai-generated" not in lowered
        assert "gradient" not in (drafts._DRAFT_CSS + html).lower()

    def test_deck_prints_feedback_themes_when_there_are_any(self):
        calls = []

        def get_feedback_themes(tenant_id, start, end, **kwargs):
            calls.append((tenant_id, start.isoformat(), end.isoformat()))
            return {
                "status": "ready",
                "themes": [
                    {"section": "liked", "label": "Peach flavor", "recap_count": 2,
                     "comment_count": 3, "quote": "Loved the peach"},
                ],
            }

        with patch("recaps.feedback_themes.get_feedback_themes", side_effect=get_feedback_themes):
            draft = self._draft()
        assert calls == [(self.tenant.id, "2026-10-01", "2026-10-31")]
        assert draft.facts["themes"][0]["label"] == "Peach flavor"
        html = drafts.build_draft_html(draft, [])
        assert "Peach flavor" in html and "noted on 2 recaps" in html
        assert "<blockquote>Loved the peach</blockquote>" in html

    def test_a_week_or_month_already_summarised_reuses_that_text(self):
        from datetime import date

        from tenants.models import TenantWeeklySummary

        TenantWeeklySummary.objects.create(
            tenant=self.tenant, period="month", week_start=date(2026, 10, 1),
            week_end=date(2026, 10, 31), body="Generated month text.",
            edited_body="October went well. Jordan Rivera ran three stores.",
            status="reviewed", facts={}, generated_at=timezone.now(),
        )
        with patch("utils.ai_text._post_json") as post:
            draft = drafts.create_draft(
                self.tenant, date(2026, 10, 1), date(2026, 10, 31), self.admin
            )
        post.assert_not_called()
        assert draft.text_source == "summary"
        assert draft.summary == "October went well. the brand ambassador ran three stores."
        assert draft.takeaways, "takeaways come from the numbers"
        # Other dates still get their own draft.
        with patch("utils.ai_text._post_json", return_value=_reply("Fine.", ["A.", "B.", "C."])) as post:
            other = drafts.create_draft(
                self.tenant, date(2026, 10, 2), date(2026, 10, 31), self.admin
            )
        assert post.call_count == 1 and other.text_source == "model"

    @pytest.mark.asyncio
    async def test_staff_download_the_pdf(self):
        pytest.importorskip("weasyprint")
        await sync_to_async(self._file)("table.jpg", self.approved)
        payload, _ = await self._create(self.admin)
        with patch("utils.gcs.download_blob_bytes", side_effect=lambda name: self.images.get(name)):
            data = await self._query(PDF, {"uuid": payload["draft"]["uuid"]}, self.admin)
        pdf = data["clientRecapDraftPdf"]
        assert pdf["ok"] is True, pdf["reason"]
        assert pdf["filename"] == "deck-brand-recap-2026-10-01-to-2026-10-31.pdf"
        assert base64.b64decode(pdf["pdfBase64"]).startswith(b"%PDF")
        assert len(mail.outbox) == 0

    def test_a_missing_renderer_is_a_clear_reason_not_a_crash(self):
        draft = self._draft()
        with patch.dict(sys.modules, {"weasyprint": None}):
            with pytest.raises(drafts.ClientRecapDraftError) as caught:
                drafts.render_draft_pdf(draft)
        assert "unavailable" in str(caught.value)
