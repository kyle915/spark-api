"""Program executive brief: prose from a fact packet, staff gate, no email.

The model is never called. ``generate_json`` is patched, and the fact packet
is supplied directly except where a test checks that an empty tenant does
not invent a brief.
"""

from __future__ import annotations

import json
from datetime import date
from unittest.mock import patch

import pytest

from ambassadors.tests.base import AmbassadorsGraphQLTestCase
from recaps.program_brief import (
    REASON_EDIT,
    REASON_NO_RECAPS,
    REASON_QUOTE,
    REASON_UNGROUNDED,
    auto_review,
    brief_facts,
    brief_is_grounded,
    compose_brief,
    normalize_preset,
    parse_window,
    quote_in_facts,
    window_reason,
)
from tenants.models import TenantProgramBrief

START_30 = "2026-10-24"
END_30 = "2026-11-23"
START_LONG = "2026-05-27"
END_LONG = "2026-11-23"

QUERY = """
query Brief($tenantId: ID!, $startDate: String!, $endDate: String!) {
  programBrief(tenantId: $tenantId, startDate: $startDate, endDate: $endDate) {
    enough
    approvedRecaps
    brief {
      id
      preset
      paragraph
      bullets
      quote
      status
      isDraft
      stale
      autoReviewed
      reviewedByName
    }
  }
}
"""

GENERATE = """
mutation Generate($tenantId: ID!, $startDate: String!, $endDate: String!, $preset: String!) {
  generateProgramBrief(
    tenantId: $tenantId
    startDate: $startDate
    endDate: $endDate
    preset: $preset
  ) {
    ok
    reason
    brief { id preset paragraph bullets quote status isDraft autoReviewed }
  }
}
"""

SAVE = """
mutation Save($id: ID!, $paragraph: String!, $bullets: [String!]!, $quote: String!) {
  saveProgramBrief(id: $id, paragraph: $paragraph, bullets: $bullets, quote: $quote) {
    ok
    reason
    brief { paragraph bullets quote status autoReviewed reviewedByName }
  }
}
"""

REVIEW = """
mutation Review($id: ID!, $reviewed: Boolean!) {
  setProgramBriefReviewed(id: $id, reviewed: $reviewed) {
    ok
    reason
    brief { status isDraft reviewedByName }
  }
}
"""


def _facts():
    return {
        "brand": "Pivot",
        "period": f"{START_30} to {END_30}",
        "events_run": 6,
        "approved_recaps": 6,
        "consumers_sampled": 40,
        "labels": "consumers_sampled counts people who were sampled.",
        "consumer_comments": [
            {"type": "general", "text": "Loved the citrus flavor at the store."}
        ],
        "feedback_themes": [
            {
                "section": "what people liked",
                "theme": "Citrus flavor",
                "recaps_mentioning_it": 3,
                "quote": "Loved the citrus flavor at the store.",
            }
        ],
    }


def _good_draft():
    return {
        "paragraph": "6 events ran and 40 consumers were sampled.",
        "bullets": ["Citrus flavor came up on 3 recaps."],
        "quote": "Loved the citrus flavor at the store.",
    }


def test_window_and_preset_rules():
    assert parse_window(START_30, END_30) == (date(2026, 10, 24), date(2026, 11, 23))
    assert parse_window(END_30, START_30) is None
    assert parse_window("nope", END_30) is None
    assert "shorter" in window_reason("2020-01-01", "2026-01-01").lower()
    assert normalize_preset("YTD") == "ytd"
    assert normalize_preset("nope") == "custom"
    assert auto_review(date(2026, 10, 24), date(2026, 11, 23), "30") is True
    assert auto_review(date(2026, 8, 25), date(2026, 11, 23), "90") is True
    assert auto_review(date(2026, 10, 24), date(2026, 11, 23), "custom") is False
    assert auto_review(date(2026, 5, 27), date(2026, 11, 23), "180") is False
    assert auto_review(date(2026, 1, 1), date(2026, 11, 23), "ytd") is False
    assert auto_review(date(2026, 10, 23), date(2026, 11, 23), "30") is False


def test_grounding_rejects_invented_figures_percents_and_reach():
    facts = _facts()
    assert brief_is_grounded("6 events ran and 40 consumers were sampled.", facts)
    assert not brief_is_grounded("We sampled 999 consumers.", facts)
    assert not brief_is_grounded("Purchase intent was 12%.", facts)
    assert not brief_is_grounded("Reach was strong across 6 events.", facts)
    assert not brief_is_grounded("Conversion was 12% on 40 consumers sampled.", facts)
    assert not brief_is_grounded("Samples handed out were 40.", facts)
    with_conversion = {
        **facts,
        "conversion": {
            "conversion_percent": "12%",
            "units_sold": 5,
            "note": "Conversion counts retail and on-premise events only.",
        },
    }
    assert brief_is_grounded("Conversion was 12% with 5 units sold.", with_conversion)


def test_quote_must_be_copied_from_the_facts():
    facts = _facts()
    assert quote_in_facts("Loved the citrus flavor at the store.", facts)
    assert not quote_in_facts("People were obsessed with the flavor.", facts)
    assert not quote_in_facts("Loved", facts)


def test_compose_keeps_a_grounded_draft_and_drops_a_bad_quote():
    draft, reason = compose_brief(
        _facts(),
        generate=lambda _system, _user: {
            **_good_draft(),
            "quote": "A sentence the recaps never contained at all.",
        },
    )
    assert reason is None
    assert draft is not None
    assert draft["quote"] == ""
    assert "40" in draft["paragraph"]


def test_compose_drops_an_invented_number_and_does_not_invent_a_fallback():
    draft, reason = compose_brief(
        _facts(),
        generate=lambda _system, _user: {
            "paragraph": "999 consumers were sampled across 6 events.",
            "bullets": [],
            "quote": None,
        },
    )
    assert draft is None
    assert reason == REASON_UNGROUNDED


def test_compose_does_not_call_the_model_without_recaps():
    called = {"n": 0}

    def _generate(_system, _user):
        called["n"] += 1
        return _good_draft()

    draft, reason = compose_brief({"approved_recaps": 0}, generate=_generate)
    assert draft is None
    assert reason == REASON_NO_RECAPS
    assert called["n"] == 0


@pytest.mark.django_db(transaction=True)
class TestProgramBriefGraphQL(AmbassadorsGraphQLTestCase):
    @pytest.fixture(autouse=True)
    def setup(self, db):
        from config.schema_client import schema_clients

        self.roles = self.setup_default_roles()
        self.schema = schema_clients
        self.endpoint_path = "/api/v1/graphql/clients"
        self.tenant = self.create_tenant(name="Brief Brand")
        self.other = self.create_tenant(name="Other Brief Brand")
        self.admin = self.create_user(
            username="brief-admin",
            email="brief-admin@test.com",
            role=self.roles["spark_admin"],
            first_name="Sam",
            last_name="Staff",
        )
        self.client_user = self.create_user(
            username="brief-client",
            email="brief-client@test.com",
            role=self.roles["client"],
        )
        self.create_tenanted_user(self.client_user, self.tenant)

    async def _run(self, document, variables, user):
        result = await self._execute_query_authenticated(
            document, variables, user, self.endpoint_path
        )
        assert result.errors is None, result.errors
        return result.data

    def _vars(self, tenant, start=START_30, end=END_30, preset="30"):
        return {
            "tenantId": str(tenant.id),
            "startDate": start,
            "endDate": end,
            "preset": preset,
        }

    def test_fact_packet_reads_stored_rows_and_does_not_call_the_model(self):
        with (
            patch("recaps.program_brief.generate_json") as brief_model,
            patch("recaps.feedback_themes.generate_json") as theme_model,
            patch("recaps.comment_insights.generate_json") as mention_model,
            patch("utils.ai_text._post_json") as post,
        ):
            facts = brief_facts(
                self.tenant.id, date.fromisoformat(START_30), date.fromisoformat(END_30)
            )
        brief_model.assert_not_called()
        theme_model.assert_not_called()
        mention_model.assert_not_called()
        post.assert_not_called()
        assert facts["approved_recaps"] == 0
        assert "feedback_themes" not in facts
        assert "competitor_mentions" not in facts
        assert "reach" not in json.dumps(facts).lower()

    @pytest.mark.asyncio
    async def test_empty_tenant_does_not_call_the_model(self):
        with patch("recaps.program_brief.generate_json") as generate:
            data = await self._run(GENERATE, self._vars(self.tenant), self.admin)
        generate.assert_not_called()
        assert data["generateProgramBrief"]["ok"] is False
        assert data["generateProgramBrief"]["reason"] == REASON_NO_RECAPS
        assert await self._count() == 0

    @pytest.mark.asyncio
    async def test_client_cannot_generate_or_read_a_draft(self):
        with patch("recaps.program_brief.generate_json") as generate:
            denied = await self._run(GENERATE, self._vars(self.tenant), self.client_user)
        generate.assert_not_called()
        assert denied["generateProgramBrief"]["ok"] is False

        await self._store(
            self.tenant,
            paragraph="6 events ran and 40 consumers were sampled.",
            status=TenantProgramBrief.STATUS_DRAFT,
            preset="custom",
            start=date.fromisoformat(START_LONG),
            end=date.fromisoformat(END_LONG),
        )
        viewed = await self._run(
            QUERY,
            {
                "tenantId": str(self.other.id),
                "startDate": START_LONG,
                "endDate": END_LONG,
            },
            self.client_user,
        )
        assert viewed["programBrief"]["brief"] is None

        staff = await self._run(
            QUERY,
            {
                "tenantId": str(self.tenant.id),
                "startDate": START_LONG,
                "endDate": END_LONG,
            },
            self.admin,
        )
        assert staff["programBrief"]["brief"]["isDraft"] is True
        assert "40" in staff["programBrief"]["brief"]["paragraph"]

    @pytest.mark.asyncio
    async def test_short_preset_publishes_when_numbers_check_and_custom_does_not(self):
        facts = _facts()
        with (
            patch("recaps.program_brief.brief_facts", return_value=facts),
            patch("recaps.program_brief.generate_json", return_value=_good_draft()),
            patch("recaps.program_brief.approved_recap_count", return_value=6),
        ):
            published = await self._run(
                GENERATE, self._vars(self.tenant, preset="30"), self.admin
            )
        assert published["generateProgramBrief"]["ok"] is True
        brief = published["generateProgramBrief"]["brief"]
        assert brief["status"] == "reviewed"
        assert brief["autoReviewed"] is True
        assert brief["isDraft"] is False
        assert brief["quote"] == "Loved the citrus flavor at the store."

        with patch("recaps.program_brief.approved_recap_count", return_value=6):
            client = await self._run(
                QUERY,
                {"tenantId": str(self.tenant.id), "startDate": START_30, "endDate": END_30},
                self.client_user,
            )
        assert client["programBrief"]["brief"]["paragraph"] == brief["paragraph"]

        with (
            patch("recaps.program_brief.brief_facts", return_value=facts),
            patch("recaps.program_brief.generate_json", return_value=_good_draft()),
            patch("recaps.program_brief.approved_recap_count", return_value=6),
        ):
            draft = await self._run(
                GENERATE,
                self._vars(self.tenant, start=START_LONG, end=END_LONG, preset="180"),
                self.admin,
            )
        assert draft["generateProgramBrief"]["brief"]["status"] == "draft"
        assert draft["generateProgramBrief"]["brief"]["autoReviewed"] is False
        hidden = await self._run(
            QUERY,
            {"tenantId": str(self.tenant.id), "startDate": START_LONG, "endDate": END_LONG},
            self.client_user,
        )
        assert hidden["programBrief"]["brief"] is None

    @pytest.mark.asyncio
    async def test_ungrounded_draft_is_not_stored(self):
        with (
            patch("recaps.program_brief.brief_facts", return_value=_facts()),
            patch(
                "recaps.program_brief.generate_json",
                return_value={
                    "paragraph": "999 consumers were sampled.",
                    "bullets": [],
                    "quote": None,
                },
            ),
        ):
            data = await self._run(GENERATE, self._vars(self.tenant), self.admin)
        assert data["generateProgramBrief"]["ok"] is False
        assert data["generateProgramBrief"]["reason"] == REASON_UNGROUNDED
        assert await self._count() == 0

    @pytest.mark.asyncio
    async def test_read_does_not_call_the_model_or_rebuild_facts(self):
        await self._store(self.tenant, paragraph="6 events ran and 40 consumers were sampled.")
        with (
            patch("recaps.program_brief.generate_json") as generate,
            patch("recaps.program_brief.brief_facts") as facts,
        ):
            data = await self._run(
                QUERY,
                {"tenantId": str(self.tenant.id), "startDate": START_30, "endDate": END_30},
                self.admin,
            )
        generate.assert_not_called()
        facts.assert_not_called()
        assert data["programBrief"]["brief"]["paragraph"].startswith("6 events")

    @pytest.mark.asyncio
    async def test_staff_edit_cannot_add_a_number_or_a_new_quote(self):
        row = await self._store(
            self.tenant, paragraph="6 events ran and 40 consumers were sampled."
        )
        brief_id = f"ProgramBrief:{row.id}"
        bad_number = await self._run(
            SAVE,
            {
                "id": brief_id,
                "paragraph": "6 events ran and 999 consumers were sampled.",
                "bullets": [],
                "quote": "",
            },
            self.admin,
        )
        assert bad_number["saveProgramBrief"]["ok"] is False
        assert bad_number["saveProgramBrief"]["reason"] == REASON_EDIT

        bad_quote = await self._run(
            SAVE,
            {
                "id": brief_id,
                "paragraph": "6 events ran and 40 consumers were sampled.",
                "bullets": ["Citrus flavor came up on 3 recaps."],
                "quote": "A brand new sentence that was never said.",
            },
            self.admin,
        )
        assert bad_quote["saveProgramBrief"]["ok"] is False
        assert bad_quote["saveProgramBrief"]["reason"] == REASON_QUOTE

        kept = await self._run(
            SAVE,
            {
                "id": brief_id,
                "paragraph": "6 events ran and 40 consumers were sampled.",
                "bullets": ["Citrus flavor came up on 3 recaps."],
                "quote": "Loved the citrus flavor at the store.",
            },
            self.admin,
        )
        assert kept["saveProgramBrief"]["ok"] is True
        assert kept["saveProgramBrief"]["brief"]["reviewedByName"] == "Sam Staff"

    @pytest.mark.asyncio
    async def test_mark_reviewed_lets_the_brand_read_it_and_a_client_cannot_flip_it(self):
        row = await self._store(
            self.tenant,
            paragraph="6 events ran and 40 consumers were sampled.",
            status=TenantProgramBrief.STATUS_DRAFT,
            preset="custom",
            approved=0,
        )
        brief_id = f"ProgramBrief:{row.id}"
        denied = await self._run(
            REVIEW, {"id": brief_id, "reviewed": True}, self.client_user
        )
        assert denied["setProgramBriefReviewed"]["ok"] is False

        reviewed = await self._run(
            REVIEW, {"id": brief_id, "reviewed": True}, self.admin
        )
        assert reviewed["setProgramBriefReviewed"]["brief"]["status"] == "reviewed"
        assert reviewed["setProgramBriefReviewed"]["brief"]["reviewedByName"] == "Sam Staff"

        client = await self._run(
            QUERY,
            {"tenantId": str(self.tenant.id), "startDate": START_30, "endDate": END_30},
            self.client_user,
        )
        assert client["programBrief"]["brief"]["isDraft"] is False
        assert "40" in client["programBrief"]["brief"]["paragraph"]

    @pytest.mark.asyncio
    async def test_stale_count_hides_the_brief_from_the_brand(self):
        await self._store(
            self.tenant,
            paragraph="6 events ran and 40 consumers were sampled.",
            status=TenantProgramBrief.STATUS_REVIEWED,
            approved=6,
        )
        with patch("recaps.program_brief.approved_recap_count", return_value=9):
            client = await self._run(
                QUERY,
                {"tenantId": str(self.tenant.id), "startDate": START_30, "endDate": END_30},
                self.client_user,
            )
            staff = await self._run(
                QUERY,
                {"tenantId": str(self.tenant.id), "startDate": START_30, "endDate": END_30},
                self.admin,
            )
        assert client["programBrief"]["brief"] is None
        assert staff["programBrief"]["brief"]["stale"] is True

    @pytest.mark.asyncio
    async def test_a_client_cannot_read_another_brands_brief(self):
        await self._store(
            self.other,
            paragraph="Other brand had 8 events and 80 consumers sampled.",
            status=TenantProgramBrief.STATUS_REVIEWED,
            approved=8,
        )
        data = await self._run(
            QUERY,
            {"tenantId": str(self.other.id), "startDate": START_30, "endDate": END_30},
            self.client_user,
        )
        brief = data["programBrief"]["brief"]
        assert brief is None or "Other brand" not in (brief.get("paragraph") or "")

    async def _count(self) -> int:
        from asgiref.sync import sync_to_async

        return await sync_to_async(TenantProgramBrief.objects.count)()

    async def _store(
        self,
        tenant,
        *,
        paragraph: str,
        status: str = TenantProgramBrief.STATUS_REVIEWED,
        preset: str = "30",
        start: date | None = None,
        end: date | None = None,
        approved: int = 6,
    ):
        from asgiref.sync import sync_to_async
        from django.utils import timezone

        start = start or date.fromisoformat(START_30)
        end = end or date.fromisoformat(END_30)

        def _create():
            return TenantProgramBrief.objects.create(
                tenant=tenant,
                start_date=start,
                end_date=end,
                preset=preset,
                paragraph=paragraph,
                bullets=["Citrus flavor came up on 3 recaps."],
                quote="Loved the citrus flavor at the store.",
                status=status,
                facts={
                    **_facts(),
                    "approved_recaps": approved,
                    "events_run": 6,
                    "consumers_sampled": 40,
                },
                generated_at=timezone.now(),
                reviewed_at=timezone.now() if status == TenantProgramBrief.STATUS_REVIEWED else None,
            )

        return await sync_to_async(_create)()
