"""Feedback themes and the weekly client summary.

Runs against the real ``schema_clients`` surface with the web client's own
field sets. The OpenAI transport (``utils.ai_text._post_json``) is replaced
in every test, so nothing here reaches the network, and the prompt that
would have been sent is what the data rules are asserted on:

  * only approved, unarchived recaps under a live request, in the window by
    event date;
  * no ambassador name, email address or phone number;
  * a brand user is held to their own tenant, staff may choose;
  * theme counts come from Python, not from the model;
  * with the model unavailable each feature says so and stores nothing.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, time, timedelta
from unittest.mock import patch

import pytest
from asgiref.sync import sync_to_async
from django.core import mail
from django.test import Client, override_settings
from django.utils import timezone

from ambassadors.tests.base import AmbassadorsGraphQLTestCase
from events import models as event_models
from recaps import models as recap_models
from recaps.feedback_themes import (
    KIND_DECLINE,
    KIND_GENERAL,
    KIND_QUESTION,
    Comment,
    _field_kind,
    is_comment_field,
    clean_comment,
    count_themes,
)
from recaps.weekly_narration import previous_complete_week
from recaps.weekly_summary import (
    REASON_UNAVAILABLE,
    REASON_UNGROUNDED,
    month_bounds,
    week_bounds,
)
from utils.ai_text import AiUnavailable

# The field sets the web client asks for.
THEMES = """
query Themes($tenantId: ID!, $startDate: String, $endDate: String) {
  tenantFeedbackThemes(tenantId: $tenantId, startDate: $startDate, endDate: $endDate) {
    status
    recapCount
    commentCount
    truncated
    generatedAt
    themes { section label recapCount quote }
  }
}
"""

REFRESH = """
mutation Refresh($tenantId: ID!, $startDate: String, $endDate: String) {
  refreshTenantFeedbackThemes(tenantId: $tenantId, startDate: $startDate, endDate: $endDate) {
    status
    recapCount
    commentCount
    truncated
    generatedAt
    themes { section label recapCount quote }
  }
}
"""

SUMMARY_FIELDS = """
  id period weekStart weekEnd body editedBody text status generatedAt
  reviewedByName reviewedAt sentAt sentByName sentTo
"""

WEEK = (
    """
query Week($weekStart: String, $period: String! = "week") {
  weeklyClientSummaries(weekStart: $weekStart, period: $period) {
    period
    weekStart
    weekEnd
    rows {
      tenantId
      tenantName
      approvedRecaps
      pendingRecaps
      suggestedRecipients
      summary { %s }
    }
  }
}
"""
    % SUMMARY_FIELDS
)

GENERATE = (
    """
mutation Generate($tenantId: ID!, $weekStart: String!, $period: String! = "week") {
  generateWeeklyClientSummary(tenantId: $tenantId, weekStart: $weekStart, period: $period) {
    ok reason summary { %s }
  }
}
"""
    % SUMMARY_FIELDS
)

SAVE = (
    """
mutation Save($id: ID!, $editedBody: String!) {
  saveWeeklyClientSummary(id: $id, editedBody: $editedBody) {
    ok reason summary { %s }
  }
}
"""
    % SUMMARY_FIELDS
)

REVIEWED = (
    """
mutation Reviewed($id: ID!, $reviewed: Boolean!) {
  setWeeklyClientSummaryReviewed(id: $id, reviewed: $reviewed) {
    ok reason summary { %s }
  }
}
"""
    % SUMMARY_FIELDS
)

SEND = (
    """
mutation Send($id: ID!, $recipients: [String!]!, $message: String) {
  sendWeeklyClientSummary(id: $id, recipients: $recipients, message: $message) {
    ok reason sentCount summary { %s }
  }
}
"""
    % SUMMARY_FIELDS
)

CARD = """
query Card($tenantId: ID!) {
  tenantWeeklySummary(tenantId: $tenantId) {
    weekStart
    weekEnd
    text
    isDraft
    reviewedAt
  }
}
"""

BREAKDOWN = """
query B($tenantId: ID!, $startDate: String, $endDate: String) {
  tenantConsumerBreakdown(tenantId: $tenantId, startDate: $startDate, endDate: $endDate) {
    total
    recaps
    events
    storeCount
    stateCount
    byWeek { weekStart consumers recaps }
    byStore { label kind consumers recaps }
    byState { label kind consumers recaps }
  }
}
"""

CRON_SECRET = "test-cron-secret-value-only-for-tests"
CRON_PATH = "/internal/cron/generate-weekly-client-summaries"


def _reply(content: str) -> dict:
    return {"choices": [{"message": {"content": content}}]}


def _prompt(call) -> str:
    """The user message of one captured ``_post_json`` call."""
    return call.args[1]["messages"][1]["content"]


def _themes_from_prompt(prompt: str) -> str:
    """A model reply that files each numbered comment by keyword, plus the
    mistakes a model makes: a comment number that does not exist, and a
    decline reason filed under what people liked."""
    liked, passed, asked = [], [], []
    declines = []
    for line in prompt.splitlines():
        match = re.match(r"^(\d+)\.(?: \[(\w+)\])? (.*)$", line)
        if not match:
            continue
        number, kind, text = int(match[1]), match[2], match[3].lower()
        if kind == "decline":
            declines.append(number)
        if "citrus" in text:
            liked.append(number)
        elif "price" in text or "expensive" in text:
            passed.append(number)
        elif "?" in text:
            asked.append(number)
    themes = [
        {
            "section": "liked",
            "label": "Citrus flavor",
            "comments": liked + declines + [999],
            "quote": liked[0] if liked else 1,
        },
        {"section": "passed", "label": "Price", "comments": passed, "quote": 999},
        {
            "section": "asked",
            "label": "Where to buy",
            "comments": asked,
            "quote": asked[0] if asked else 1,
        },
    ]
    return json.dumps({"themes": themes})


@pytest.mark.django_db(transaction=True)
class TestSummaryAndThemes(AmbassadorsGraphQLTestCase):
    @pytest.fixture(autouse=True)
    def setup(self, db, settings):
        from config.schema_client import schema_clients

        # A key that is not real, so the helpers get as far as the transport,
        # which the fixture below replaces.
        settings.OPENAI_API_KEY = "test-key-not-real"
        settings.OPENAI_MODEL = ""

        self.roles = self.setup_default_roles()
        self.schema = schema_clients
        self.endpoint_path = "/api/v1/graphql/clients"
        self.system_user = self.get_system_user()

        self.week_start, self.week_end = previous_complete_week()
        self.start, self.end = self.week_start.isoformat(), self.week_end.isoformat()

        self.tenant = self.create_tenant(name="Alpha Brand")
        self.other = self.create_tenant(name="Beta Brand")

        self.admin = self.create_user(
            username="themes-admin",
            email="themes-admin@test.com",
            role=self.roles["spark_admin"],
        )
        self.admin.first_name = "Sam"
        self.admin.last_name = "Staff"
        self.admin.save()
        self.client_user = self.create_user(
            username="themes-client",
            email="themes-client@test.com",
            role=self.roles["client"],
        )
        self.create_tenanted_user(self.client_user, self.tenant)

        credited = self.create_user(
            username="jordan-rivera-themes",
            email="jordan.rivera.themes@ba.test",
            role=self.roles["ambassador"],
        )
        credited.first_name = "Jordan"
        credited.last_name = "Rivera"
        credited.save()
        self.credited_ba = self.create_ambassador(credited)

        self.request_a = self._request(self.tenant, "Alpha Spring Tour")
        self.request_b = self._request(self.other, "Beta Fall Tour")
        self.deleted_request = self._request(self.tenant, "Alpha Deleted Tour")
        event_models.Request.objects.filter(id=self.deleted_request.id).update(
            deleted_at=timezone.now()
        )

        self.store_one = self._event(self.tenant, "Alpha Store One", self.request_a, 1)
        self.store_two = self._event(self.tenant, "Alpha Store Two", self.request_a, 2)
        self.store_three = self._event(self.tenant, "Alpha Store Three", self.request_a, 3)

        self.r1 = self._recap(
            self.store_one,
            consumers=40,
            quotes="Loved the citrus flavor",
            feedback="Jordan Rivera said the citrus one went fastest",
            reasons_to_decline="Too expensive for a four pack",
        )
        self.r2 = self._recap(
            self.store_two,
            consumers=25,
            quotes="Citrus was a hit with kids",
            reasons_to_decline="Price is high next to the store brand",
        )
        self.r3 = self._recap(
            self.store_three,
            consumers=10,
            feedback="Where can I buy this near me? Call me at 555-123-4567",
        )
        self.pending = self._recap(
            self.store_one, consumers=7, quotes="PENDING ALPHA QUOTE", approved=False
        )
        self.archived = self._recap(
            self.store_one, consumers=5, quotes="ARCHIVED ALPHA QUOTE"
        )
        recap_models.Recap.objects.filter(id=self.archived.id).update(
            archived_at=timezone.now()
        )
        deleted_event = self._event(
            self.tenant, "Alpha Deleted Store", self.deleted_request, 2
        )
        self._recap(deleted_event, consumers=3, quotes="DELETED ALPHA QUOTE")
        # Approved, but the event was three weeks before the window.
        old_event = self._event(self.tenant, "Alpha Old Store", self.request_a, -20)
        self._recap(old_event, consumers=90, quotes="OLD ALPHA QUOTE about citrus")

        self._custom_recap()

        self.event_b = self._event(self.other, "Beta Store One", self.request_b, 1)
        self._recap(self.event_b, consumers=99, quotes="BETA SECRET QUOTE")

        # No test may reach OpenAI: the transport fails unless a test says
        # what it returns.
        patcher = patch(
            "utils.ai_text._post_json", side_effect=AiUnavailable("blocked in tests")
        )
        self.post = patcher.start()
        yield
        patcher.stop()

    # ── fixtures ──────────────────────────────────────────────────────

    def _request(self, tenant, name):
        return event_models.Request.objects.create(
            name=name,
            address="1 Main St",
            tenant=tenant,
            status=self.create_request_status(
                name="Approved", tenant=tenant, slug="approved"
            ),
            request_type=self.create_request_type(name="Retail", tenant=tenant),
            created_by=self.system_user,
        )

    def _event(self, tenant, name, request, day_offset):
        when = timezone.make_aware(
            datetime.combine(self.week_start + timedelta(days=day_offset), time(18))
        )
        event = self.create_event(
            name=name,
            tenant=tenant,
            date=when,
            start_time=when,
            end_time=when + timedelta(hours=4),
        )
        event_models.Event.objects.filter(id=event.id).update(request=request)
        event.refresh_from_db()
        return event

    def _recap(self, event, *, consumers, approved=True, **feedback):
        recap = recap_models.Recap.objects.create(
            name=f"recap {event.name}",
            event=event,
            approved=approved,
            submited_at=timezone.now(),
            ambassador=self.credited_ba,
            created_by=self.system_user,
            updated_by=self.system_user,
        )
        recap_models.ConsumerEngagements.objects.create(
            recap=recap,
            total_consumer=consumers,
            created_by=self.system_user,
            updated_by=self.system_user,
        )
        recap_models.ConsumerFeedback.objects.create(recap=recap, **feedback)
        return recap

    def _custom_recap(self):
        event_type = event_models.EventType.objects.create(
            name="Retail Sampling",
            slug="themes-retail",
            tenant=self.tenant,
            created_by=self.system_user,
        )
        template = recap_models.CustomRecapTemplate.objects.create(
            name="Themes Retail",
            event_type=event_type,
            tenant=self.tenant,
            created_by=self.system_user,
        )
        text = recap_models.CustomRecapFieldType.objects.create(
            name="Long text", created_by=self.system_user
        )
        section = recap_models.RecapSection.objects.create(
            name="Feedback", tenant=self.tenant, created_by=self.system_user
        )
        self.custom = recap_models.CustomRecap.objects.create(
            name="custom",
            approved=True,
            submitted_at=self.store_two.date,
            event=self.store_two,
            tenant=self.tenant,
            custom_recap_template=template,
            ambassador=self.credited_ba,
            external_ba_name="Casey Walkup",
            created_by=self.system_user,
        )
        for name, value in (
            ("List common questions asked by consumers", "Is it gluten free?"),
            ("Reasons consumers declined", "Casey Walkup heard the price is steep"),
            ("BA Name", "Jordan Rivera"),
            ("Additional notes", "AMBASSADOR NOTE ran out of cups at noon"),
            ("Notes", "AMBASSADOR NOTE manager was rude"),
            ("Total number of consumers sampled", "30"),
        ):
            field = recap_models.CustomField.objects.create(
                name=name,
                custom_recap_template=template,
                custom_field_type=text,
                recap_section=section,
                created_by=self.system_user,
            )
            recap_models.CustomFieldValue.objects.create(
                custom_recap=self.custom,
                custom_field=field,
                value=value,
                created_by=self.system_user,
            )

    # ── calls ─────────────────────────────────────────────────────────

    async def _run(self, document, variables, user):
        result = await self._execute_query_authenticated(
            document, variables, user, self.endpoint_path
        )
        assert result.errors is None, result.errors
        return result.data

    def _window(self, tenant):
        return {"tenantId": str(tenant.id), "startDate": self.start, "endDate": self.end}

    async def _themes(self, user, tenant):
        data = await self._run(THEMES, self._window(tenant), user)
        return data["tenantFeedbackThemes"]

    def _model_groups_comments(self):
        self.post.side_effect = lambda url, payload, headers, timeout: _reply(
            _themes_from_prompt(payload["messages"][1]["content"])
        )

    def _model_writes(self, text):
        self.post.side_effect = None
        self.post.return_value = _reply(text)

    async def _generate(self, user, tenant=None):
        data = await self._run(
            GENERATE,
            {"tenantId": str((tenant or self.tenant).id), "weekStart": self.start},
            user,
        )
        return data["generateWeeklyClientSummary"]

    async def _summaries(self):
        from tenants.models import TenantWeeklySummary

        return await sync_to_async(lambda: list(TenantWeeklySummary.objects.all()))()

    # ── feedback themes ───────────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_brand_user_prompt_holds_approved_comments_only_and_no_names(self):
        self._model_groups_comments()
        payload = await self._themes(self.client_user, self.tenant)
        assert payload["status"] == "ready"
        assert self.post.call_count == 1
        prompt = _prompt(self.post.call_args)

        for kept in (
            "Loved the citrus flavor",
            "Citrus was a hit with kids",
            "[decline] Too expensive for a four pack",
            "[question] Is it gluten free?",
        ):
            assert kept in prompt, kept
        for dropped in (
            "PENDING ALPHA QUOTE",
            "ARCHIVED ALPHA QUOTE",
            "DELETED ALPHA QUOTE",
            "OLD ALPHA QUOTE",
            "BETA SECRET QUOTE",
            "AMBASSADOR NOTE",
            "Rivera",
            "Casey Walkup",
            "555-123-4567",
        ):
            assert dropped not in prompt, dropped
        # The ambassador's name is replaced where it was written into a
        # comment; the comment itself is kept.
        assert "the brand ambassador said the citrus one went fastest" in prompt

    @pytest.mark.asyncio
    async def test_counts_are_counted_here_not_by_the_model(self):
        self._model_groups_comments()
        payload = await self._themes(self.client_user, self.tenant)
        themes = {t["label"]: t for t in payload["themes"]}

        # Three citrus comments on two recaps. The decline reasons the model
        # filed under "liked" and the comment number 999 are not counted.
        assert themes["Citrus flavor"]["section"] == "liked"
        assert themes["Citrus flavor"]["recapCount"] == 2
        # Two legacy recaps and the custom recap gave a price reason.
        assert themes["Price"]["section"] == "passed"
        assert themes["Price"]["recapCount"] == 3
        assert themes["Where to buy"]["recapCount"] == 2
        # Most recaps first.
        assert [t["label"] for t in payload["themes"]][0] == "Price"
        # A quote is one of the stored comments, with nothing personal in it.
        prompt = _prompt(self.post.call_args)
        for theme in payload["themes"]:
            assert theme["quote"] in prompt
            assert "Rivera" not in theme["quote"]
            assert "555" not in theme["quote"]
        # The model pointed "Price" at a comment that does not exist.
        assert themes["Price"]["quote"]
        # 4 recaps had a comment: three legacy and the custom one.
        assert payload["recapCount"] == 4

    @pytest.mark.asyncio
    async def test_brand_user_asking_for_another_brand_gets_their_own(self):
        self._model_groups_comments()
        payload = await self._themes(self.client_user, self.other)
        assert payload["status"] == "ready"
        prompt = _prompt(self.post.call_args)
        assert "Loved the citrus flavor" in prompt
        assert "BETA SECRET QUOTE" not in prompt

    @pytest.mark.asyncio
    async def test_staff_may_choose_the_brand(self):
        self._model_groups_comments()
        # Beta has one comment: too few to group, and the model is not asked.
        payload = await self._themes(self.admin, self.other)
        assert payload["status"] == "too_few"
        assert payload["commentCount"] == 1
        assert payload["themes"] == []
        assert self.post.call_count == 0
        # Alpha, for staff: the same approved-only, name-free prompt.
        payload = await self._themes(self.admin, self.tenant)
        assert payload["status"] == "ready"
        prompt = _prompt(self.post.call_args)
        assert "PENDING ALPHA QUOTE" not in prompt
        assert "Rivera" not in prompt

    @pytest.mark.asyncio
    async def test_a_second_load_reads_the_stored_themes(self):
        self._model_groups_comments()
        first = await self._themes(self.client_user, self.tenant)
        again = await self._themes(self.admin, self.tenant)
        assert self.post.call_count == 1
        assert again["themes"] == first["themes"]
        assert again["generatedAt"] == first["generatedAt"]

    @pytest.mark.asyncio
    async def test_built_ahead_of_time_the_page_does_not_call_the_model(self):
        from asgiref.sync import sync_to_async
        from django.core.management import call_command
        import io

        from recaps.management.commands.prebuild_feedback_themes import ranges_for

        # The prebuild works from _ops_today() and builds rolling ranges
        # ending today (7d / 30d / 90d / 182d / YTD); the page then asks for
        # one of those same ranges. Pin "today" to the Monday after the
        # fixture week, so the 7-day range holds exactly the fixture week's
        # comments whatever day the suite runs on. (Before this, the page
        # asked for the fixture week itself, which only matched a prebuilt
        # range by luck on a Monday or Tuesday.)
        today = self.week_end + timedelta(days=1)
        start, end = ranges_for(today)[0]
        assert (start, end) == (self.week_start, today)

        self._model_groups_comments()
        with patch("events.live_board._ops_today", return_value=today):
            out = io.StringIO()
            await sync_to_async(call_command)(
                "prebuild_feedback_themes", "--tenant", str(self.tenant.id), stdout=out
            )
            assert f"built tenant {self.tenant.id} {start} → {end}" in out.getvalue()
            calls = self.post.call_count
            assert calls >= 1

            data = await self._run(
                THEMES,
                {
                    "tenantId": str(self.tenant.id),
                    "startDate": start.isoformat(),
                    "endDate": end.isoformat(),
                },
                self.client_user,
            )
            themes = data["tenantFeedbackThemes"]
            assert themes["status"] == "ready"
            assert self.post.call_count == calls

            # A second run finds the themes up to date and builds none again.
            again = io.StringIO()
            await sync_to_async(call_command)(
                "prebuild_feedback_themes", "--tenant", str(self.tenant.id), stdout=again
            )
            assert "built 0," in again.getvalue()

    @pytest.mark.asyncio
    async def test_a_newly_approved_comment_rebuilds_once(self):
        self._model_groups_comments()
        await self._themes(self.client_user, self.tenant)
        # Still pending: nothing a brand may see has changed.
        await self._themes(self.client_user, self.tenant)
        assert self.post.call_count == 1

        await sync_to_async(
            lambda: recap_models.Recap.objects.filter(id=self.pending.id).update(
                approved=True
            )
        )()
        payload = await self._themes(self.client_user, self.tenant)
        assert self.post.call_count == 2
        assert "PENDING ALPHA QUOTE" in _prompt(self.post.call_args)
        assert payload["recapCount"] == 5
        await self._themes(self.client_user, self.tenant)
        assert self.post.call_count == 2

    @pytest.mark.asyncio
    async def test_refresh_is_for_staff(self):
        self._model_groups_comments()
        await self._themes(self.client_user, self.tenant)
        data = await self._run(REFRESH, self._window(self.tenant), self.client_user)
        assert data["refreshTenantFeedbackThemes"] is None
        assert self.post.call_count == 1

        data = await self._run(REFRESH, self._window(self.tenant), self.admin)
        assert data["refreshTenantFeedbackThemes"]["status"] == "ready"
        assert self.post.call_count == 2

    @pytest.mark.asyncio
    async def test_themes_say_so_when_the_model_is_unavailable(self):
        from tenants.models import TenantFeedbackThemes

        # The transport fails (the fixture default).
        payload = await self._themes(self.client_user, self.tenant)
        assert payload["status"] == "unavailable"
        assert payload["themes"] == []
        # No key at all: the transport is never reached.
        calls = self.post.call_count
        with override_settings(OPENAI_API_KEY=""):
            payload = await self._themes(self.client_user, self.tenant)
        assert payload["status"] == "unavailable"
        assert self.post.call_count == calls
        # A reply that is not the asked-for shape.
        self._model_writes("not json")
        payload = await self._themes(self.client_user, self.tenant)
        assert payload["status"] == "unavailable"
        assert await sync_to_async(TenantFeedbackThemes.objects.count)() == 0

    @pytest.mark.asyncio
    async def test_a_failed_refresh_keeps_the_stored_themes(self):
        self._model_groups_comments()
        first = await self._themes(self.client_user, self.tenant)
        self.post.side_effect = AiUnavailable("down")
        data = await self._run(REFRESH, self._window(self.tenant), self.admin)
        assert data["refreshTenantFeedbackThemes"]["status"] == "ready"
        assert data["refreshTenantFeedbackThemes"]["themes"] == first["themes"]

    # ── consumers per event ───────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_breakdown_counts_the_events_behind_the_total(self):
        data = await self._run(BREAKDOWN, self._window(self.tenant), self.client_user)
        breakdown = data["tenantConsumerBreakdown"]
        # 40 + 25 + 10 legacy, 30 custom, and the archived recap's 5: the
        # dashboard total counts archived recaps today.
        assert breakdown["total"] == 110
        # Store one, two and three. The custom recap shares store two.
        assert breakdown["events"] == 3

    # ── weekly summary ────────────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_summary_prompt_is_the_dashboard_numbers_and_approved_comments(self):
        from recaps.consumer_breakdown import tenant_consumer_breakdown
        from recaps.tenant_overview import (
            _inclusive_dates_to_window,
            _tenant_kpi_totals_window,
        )

        self._model_writes(
            "Three events ran this week.\n\n**Shoppers** liked the citrus flavor."
        )
        payload = await self._generate(self.admin)
        assert payload["ok"] is True, payload
        assert payload["summary"]["status"] == "reviewed"  # weekly: published at once
        assert payload["summary"]["text"] == (
            "Three events ran this week.\n\nShoppers liked the citrus flavor."
        )

        prompt = _prompt(self.post.call_args)
        facts = json.loads(prompt.split("FACTS:\n", 1)[1])

        def _dashboard():
            window = _inclusive_dates_to_window(self.week_start, self.week_end)
            return (
                tenant_consumer_breakdown(
                    self.tenant.id, self.week_start, self.week_end
                )["total"],
                _tenant_kpi_totals_window(self.tenant.id, window).consumers_reached,
            )

        breakdown_total, kpi_total = await sync_to_async(_dashboard)()
        assert facts["consumers_sampled"] == breakdown_total == kpi_total == 110
        assert facts["events_run"] == 3
        assert facts["brand"] == "Alpha Brand"
        texts = [c["text"] for c in facts["consumer_comments"]]
        assert "Loved the citrus flavor" in texts
        assert {"type": "question asked", "text": "Is it gluten free?"} in facts[
            "consumer_comments"
        ]
        for dropped in (
            "PENDING ALPHA QUOTE",
            "ARCHIVED ALPHA QUOTE",
            "DELETED ALPHA QUOTE",
            "OLD ALPHA QUOTE",
            "BETA",
            "AMBASSADOR NOTE",
            "Rivera",
            "Casey Walkup",
            "555-123-4567",
        ):
            assert dropped not in prompt, dropped

        rows = await self._summaries()
        assert len(rows) == 1 and rows[0].tenant_id == self.tenant.id
        assert rows[0].week_start == self.week_start
        assert len(mail.outbox) == 0

    @pytest.mark.asyncio
    async def test_a_draft_with_a_number_that_was_not_handed_over_is_not_kept(self):
        self._model_writes("The team sampled 98765 consumers this week.")
        payload = await self._generate(self.admin)
        assert payload == {"ok": False, "reason": REASON_UNGROUNDED, "summary": None}
        # Asked twice, then given up on.
        assert self.post.call_count == 2
        assert await self._summaries() == []

    @pytest.mark.asyncio
    async def test_summary_says_so_when_the_model_is_unavailable(self):
        payload = await self._generate(self.admin)
        assert payload == {"ok": False, "reason": REASON_UNAVAILABLE, "summary": None}
        with override_settings(OPENAI_API_KEY=""):
            payload = await self._generate(self.admin)
        assert payload == {"ok": False, "reason": REASON_UNAVAILABLE, "summary": None}
        assert await self._summaries() == []

    @pytest.mark.asyncio
    async def test_a_failed_rewrite_leaves_the_stored_draft(self):
        self._model_writes("Three events ran.")
        first = await self._generate(self.admin)
        self.post.side_effect = AiUnavailable("down")
        payload = await self._generate(self.admin)
        assert payload["ok"] is False
        rows = await self._summaries()
        assert [r.body for r in rows] == [first["summary"]["body"]]

    @pytest.mark.asyncio
    async def test_brand_user_cannot_read_or_write_summaries(self):
        self._model_writes("Three events ran.")
        made = await self._generate(self.admin)
        calls = self.post.call_count
        summary_id = made["summary"]["id"]

        data = await self._run(WEEK, {"weekStart": self.start}, self.client_user)
        assert data["weeklyClientSummaries"] is None
        assert (await self._generate(self.client_user))["ok"] is False
        assert self.post.call_count == calls
        data = await self._run(
            SAVE, {"id": summary_id, "editedBody": "Hijacked."}, self.client_user
        )
        assert data["saveWeeklyClientSummary"]["ok"] is False
        data = await self._run(
            REVIEWED, {"id": summary_id, "reviewed": True}, self.client_user
        )
        assert data["setWeeklyClientSummaryReviewed"]["ok"] is False
        row = (await self._summaries())[0]
        # Untouched by the brand user: still the auto-published weekly text.
        assert row.edited_body == "" and row.status == "reviewed"

    @pytest.mark.asyncio
    async def test_staff_see_each_brand_edit_and_mark_reviewed(self):
        data = await self._run(WEEK, {"weekStart": self.start}, self.admin)
        week = data["weeklyClientSummaries"]
        assert (week["weekStart"], week["weekEnd"]) == (self.start, self.end)
        rows = {r["tenantName"]: r for r in week["rows"]}
        # 3 legacy + the archived one + the custom recap; 1 waiting for review.
        assert rows["Alpha Brand"]["approvedRecaps"] == 5
        assert rows["Alpha Brand"]["pendingRecaps"] == 1
        assert rows["Alpha Brand"]["summary"] is None
        assert rows["Beta Brand"]["approvedRecaps"] == 1
        assert self.post.call_count == 0

        self._model_writes("Three events ran.")
        made = await self._generate(self.admin)
        summary_id = made["summary"]["id"]

        data = await self._run(
            SAVE, {"id": summary_id, "editedBody": "  Three stores ran. "}, self.admin
        )
        saved = data["saveWeeklyClientSummary"]["summary"]
        assert saved["body"] == "Three events ran."
        assert saved["editedBody"] == "Three stores ran."
        assert saved["text"] == "Three stores ran."

        data = await self._run(REVIEWED, {"id": summary_id, "reviewed": True}, self.admin)
        reviewed = data["setWeeklyClientSummaryReviewed"]["summary"]
        assert reviewed["status"] == "reviewed"
        assert reviewed["reviewedByName"] == "Sam Staff"
        assert reviewed["reviewedAt"]

        data = await self._run(WEEK, {"weekStart": self.start}, self.admin)
        listed = {r["tenantName"]: r for r in data["weeklyClientSummaries"]["rows"]}
        assert listed["Alpha Brand"]["summary"]["text"] == "Three stores ran."
        assert listed["Alpha Brand"]["summary"]["status"] == "reviewed"

        data = await self._run(REVIEWED, {"id": summary_id, "reviewed": False}, self.admin)
        back = data["setWeeklyClientSummaryReviewed"]["summary"]
        assert back["status"] == "draft" and back["reviewedByName"] is None
        assert len(mail.outbox) == 0

    @pytest.mark.asyncio
    async def test_staff_can_send_branded_summary_and_stamp_sent(self):
        from tenants.models import Tenant

        await sync_to_async(Tenant.objects.filter(id=self.tenant.id).update)(
            recap_recipient_emails="buyer@alpha.test, ops@alpha.test"
        )
        self._model_writes("Three events ran.\n\nShoppers liked the citrus.")
        made = await self._generate(self.admin)
        summary_id = made["summary"]["id"]

        data = await self._run(WEEK, {"weekStart": self.start}, self.admin)
        listed = {r["tenantName"]: r for r in data["weeklyClientSummaries"]["rows"]}
        suggested = listed["Alpha Brand"]["suggestedRecipients"]
        assert "buyer@alpha.test" in suggested
        assert "themes-client@test.com" in suggested

        data = await self._run(
            SEND,
            {
                "id": summary_id,
                "recipients": ["Buyer@Alpha.test", "buyer@alpha.test", "ops@alpha.test"],
                "message": "Quick Monday note.",
            },
            self.admin,
        )
        payload = data["sendWeeklyClientSummary"]
        assert payload["ok"] is True, payload
        assert payload["sentCount"] == 2  # case-insensitive dedup
        sent = payload["summary"]
        assert sent["sentAt"]
        assert sent["sentByName"] == "Sam Staff"
        assert sent["sentTo"] == ["Buyer@Alpha.test", "ops@alpha.test"]
        assert sent["status"] == "reviewed"
        assert len(mail.outbox) == 2
        bodies = " ".join(
            (m.alternatives[0][0] if m.alternatives else m.body) for m in mail.outbox
        )
        assert "Three events ran." in bodies
        assert "Quick Monday note." in bodies
        assert "Spark by Ignite" in bodies or "spark-logo" in bodies.lower()

        row = (await self._summaries())[0]
        assert row.sent_at is not None
        assert row.sent_by_id == self.admin.id
        assert row.sent_to == ["Buyer@Alpha.test", "ops@alpha.test"]

    @pytest.mark.asyncio
    async def test_send_summary_rejects_bad_recipients_and_non_staff(self):
        self._model_writes("Three events ran.")
        made = await self._generate(self.admin)
        summary_id = made["summary"]["id"]

        data = await self._run(
            SEND, {"id": summary_id, "recipients": []}, self.admin
        )
        assert data["sendWeeklyClientSummary"]["ok"] is False
        data = await self._run(
            SEND,
            {"id": summary_id, "recipients": ["not-an-email"]},
            self.admin,
        )
        assert data["sendWeeklyClientSummary"]["ok"] is False
        data = await self._run(
            SEND,
            {"id": summary_id, "recipients": ["ok@brand.test"]},
            self.client_user,
        )
        assert data["sendWeeklyClientSummary"]["ok"] is False
        assert len(mail.outbox) == 0
        row = (await self._summaries())[0]
        assert row.sent_at is None

    @pytest.mark.asyncio
    async def test_dashboard_shows_a_brand_only_a_reviewed_summary(self):
        async def card(user, tenant):
            data = await self._run(CARD, {"tenantId": str(tenant.id)}, user)
            return data["tenantWeeklySummary"]

        assert await card(self.client_user, self.tenant) is None
        assert await card(self.admin, self.tenant) is None

        self._model_writes("Three events ran.")
        made = await self._generate(self.admin)
        await self._generate(self.admin, self.other)
        summary_id = made["summary"]["id"]
        calls = self.post.call_count

        # Weekly summaries are published as soon as they're written.
        live = await card(self.client_user, self.tenant)
        assert live["isDraft"] is False and live["text"] == "Three events ran."

        # Staff can pull one back to draft: staff see it labelled, the brand nothing.
        await self._run(REVIEWED, {"id": summary_id, "reviewed": False}, self.admin)
        assert await card(self.client_user, self.tenant) is None
        draft = await card(self.admin, self.tenant)
        assert draft["isDraft"] is True and draft["text"] == "Three events ran."

        await self._run(
            SAVE, {"id": summary_id, "editedBody": "Three stores ran."}, self.admin
        )
        await self._run(REVIEWED, {"id": summary_id, "reviewed": True}, self.admin)
        seen = await card(self.client_user, self.tenant)
        assert seen["isDraft"] is False
        assert seen["text"] == "Three stores ran."
        assert (seen["weekStart"], seen["weekEnd"]) == (self.start, self.end)
        # Asking for another brand they don't belong to returns their own.
        assert (await card(self.client_user, self.other))["text"] == "Three stores ran."

        # Back to draft: gone for the brand again.
        await self._run(REVIEWED, {"id": summary_id, "reviewed": False}, self.admin)
        assert await card(self.client_user, self.tenant) is None
        # Reading never calls the model.
        assert self.post.call_count == calls

    # ── monthly client email draft ────────────────────────────────────

    @pytest.mark.asyncio
    async def test_monthly_draft_is_its_own_row_built_the_same_way(self):
        def reply(url, payload, headers, timeout):
            if "response_format" in payload:
                return _reply(_themes_from_prompt(payload["messages"][1]["content"]))
            return _reply("Hi team,\n\nEvents ran and shoppers liked the citrus flavor.")

        self.post.side_effect = reply
        day = self.store_one.date.date()
        month_start, month_end = month_bounds(day.isoformat())
        variables = {
            "tenantId": str(self.tenant.id),
            "weekStart": day.isoformat(),
            "period": "month",
        }
        data = await self._run(GENERATE, variables, self.admin)
        made = data["generateWeeklyClientSummary"]
        assert made["ok"] is True, made
        assert made["summary"]["period"] == "month"
        assert made["summary"]["weekStart"] == month_start.isoformat()
        assert made["summary"]["weekEnd"] == month_end.isoformat()
        assert made["summary"]["text"].startswith("Hi team,\n\n")

        # The last call is the email draft; any call before it grouped themes.
        call = self.post.call_args
        assert "monthly email" in call.args[1]["messages"][0]["content"]
        prompt = _prompt(call)
        facts = json.loads(prompt.split("FACTS:\n", 1)[1])
        assert facts["month"] == month_start.strftime("%B %Y")
        assert facts["approved_recaps"] >= 1
        for dropped in (
            "PENDING ALPHA QUOTE",
            "ARCHIVED ALPHA QUOTE",
            "DELETED ALPHA QUOTE",
            "BETA",
            "Rivera",
            "Casey Walkup",
            "555-123-4567",
        ):
            assert dropped not in prompt, dropped
        if self.store_three.date.date() <= month_end:
            # The whole fixture week is inside this month: the themes the
            # report shows are handed over with their counted recaps.
            themes = {t["theme"]: t for t in facts["feedback_themes"]}
            assert themes["Price"]["recaps_mentioning_it"] == 3
            assert themes["Price"]["section"] == "why people passed"

        # A brand user cannot write one.
        calls = self.post.call_count
        data = await self._run(GENERATE, variables, self.client_user)
        assert data["generateWeeklyClientSummary"]["ok"] is False
        assert self.post.call_count == calls

        # Listed under the month, not under the week.
        data = await self._run(
            WEEK, {"weekStart": day.isoformat(), "period": "month"}, self.admin
        )
        month = data["weeklyClientSummaries"]
        assert month["period"] == "month"
        assert month["weekStart"] == month_start.isoformat()
        rows = {r["tenantName"]: r for r in month["rows"]}
        assert rows["Alpha Brand"]["summary"]["id"] == made["summary"]["id"]
        data = await self._run(WEEK, {"weekStart": self.start}, self.admin)
        weekly = {r["tenantName"]: r for r in data["weeklyClientSummaries"]["rows"]}
        assert weekly["Alpha Brand"]["summary"] is None

        # Reviewed or not, a monthly draft is never the dashboard's summary.
        await self._run(
            REVIEWED, {"id": made["summary"]["id"], "reviewed": True}, self.admin
        )
        data = await self._run(CARD, {"tenantId": str(self.tenant.id)}, self.client_user)
        assert data["tenantWeeklySummary"] is None
        assert len(mail.outbox) == 0

    @pytest.mark.asyncio
    async def test_cron_drafts_a_month_when_asked(self):
        day = self.store_one.date.date()
        with override_settings(INTERNAL_CRON_SECRET=CRON_SECRET):
            response = await sync_to_async(Client().post)(
                f"{CRON_PATH}?period=month&week_start={day.isoformat()}",
                HTTP_X_CRON_SECRET=CRON_SECRET,
            )
            bad = await sync_to_async(Client().post)(
                f"{CRON_PATH}?period=year", HTTP_X_CRON_SECRET=CRON_SECRET
            )
        body = response.json()
        assert response.status_code == 200
        assert body["period"] == "month" and body["dry_run"] is True
        assert body["week_start"] == month_bounds(day.isoformat())[0].isoformat()
        assert body["emails_sent"] == 0
        assert {t["result"] for t in body["tenants"]} == {"would_write"}
        assert bad.status_code == 400
        assert self.post.call_count == 0
        assert await self._summaries() == []

    # ── the Monday job ────────────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_cron_needs_the_secret(self):
        with override_settings(INTERNAL_CRON_SECRET=CRON_SECRET):
            response = await sync_to_async(Client().post)(CRON_PATH)
        assert response.status_code == 401
        assert self.post.call_count == 0

    @pytest.mark.asyncio
    async def test_cron_dry_run_is_the_default_and_writes_nothing(self):
        with override_settings(INTERNAL_CRON_SECRET=CRON_SECRET):
            response = await sync_to_async(Client().post)(
                CRON_PATH, HTTP_X_CRON_SECRET=CRON_SECRET
            )
        body = response.json()
        assert response.status_code == 200
        assert body["dry_run"] is True and body["mode"] == "dry_run"
        assert {t["tenant_name"]: t["result"] for t in body["tenants"]} == {
            "Alpha Brand": "would_write",
            "Beta Brand": "would_write",
        }
        assert self.post.call_count == 0
        assert await self._summaries() == []

    @pytest.mark.asyncio
    async def test_cron_drafts_each_brand_once_and_emails_nobody(self):
        self._model_writes("Events ran and shoppers liked it.")
        edited = await self._generate(self.admin)
        await self._run(
            SAVE,
            {"id": edited["summary"]["id"], "editedBody": "Staff wording."},
            self.admin,
        )
        calls = self.post.call_count

        with override_settings(INTERNAL_CRON_SECRET=CRON_SECRET):
            response = await sync_to_async(Client().post)(
                f"{CRON_PATH}?dry_run=false&week_start={self.start}",
                HTTP_X_CRON_SECRET=CRON_SECRET,
            )
        body = response.json()
        assert response.status_code == 200
        assert body["emails_sent"] == 0 and body["written"] == 1
        assert {t["tenant_name"]: t["result"] for t in body["tenants"]} == {
            "Alpha Brand": "exists",
            "Beta Brand": "written",
        }
        # One call, for Beta. Alpha's draft and the staff edit are untouched.
        assert self.post.call_count == calls + 1
        rows = {r.tenant_id: r for r in await self._summaries()}
        assert rows[self.tenant.id].edited_body == "Staff wording."
        # Weekly summaries are published to the brand as soon as they're written.
        assert rows[self.other.id].status == "reviewed"
        assert rows[self.other.id].reviewed_at is not None
        assert rows[self.other.id].reviewed_by_id is None
        assert len(mail.outbox) == 0

    @pytest.mark.asyncio
    async def test_cron_reports_brands_it_could_not_draft(self):
        with override_settings(INTERNAL_CRON_SECRET=CRON_SECRET):
            response = await sync_to_async(Client().post)(
                f"{CRON_PATH}?dry_run=false", HTTP_X_CRON_SECRET=CRON_SECRET
            )
        body = response.json()
        assert response.status_code == 200 and body["written"] == 0
        assert {t["result"] for t in body["tenants"]} == {"not_written"}
        assert await self._summaries() == []


# ─── Pure functions ─────────────────────────────────────────────────


def _c(recap, text, kind=KIND_GENERAL):
    return Comment(recap=recap, kind=kind, text=text)


def test_count_themes_counts_distinct_recaps_and_ignores_bad_assignments():
    comments = [
        _c("L1", "Great taste"),
        _c("L1", "Tastes fresh"),
        _c("L2", "Nice taste"),
        _c("L3", "Too pricey", KIND_DECLINE),
        _c("C4", "Is it vegan?", KIND_QUESTION),
    ]
    themes = count_themes(
        {
            "themes": [
                # Two comments from one recap count that recap once. 4 is a
                # decline reason and 5 a question: neither is "liked". 9 and
                # "x" are not comments.
                {"section": "liked", "label": " Taste. ", "comments": [1, 2, 3, 4, 5, 9, "x"], "quote": 2},
                # Comment 1 already belongs to the theme above.
                {"section": "liked", "label": "Freshness", "comments": [1], "quote": 1},
                {"section": "passed", "label": "Price", "comments": [4, 5], "quote": 5},
                {"section": "asked", "label": "Ingredients", "comments": [5], "quote": 5},
                {"section": "elsewhere", "label": "Nope", "comments": [3], "quote": 3},
                {"section": "liked", "label": "", "comments": [3], "quote": 3},
            ]
        },
        comments,
    )
    assert themes == [
        {"section": "liked", "label": "Taste", "recap_count": 2, "comment_count": 3, "quote": "Tastes fresh"},
        {"section": "asked", "label": "Ingredients", "recap_count": 1, "comment_count": 1, "quote": "Is it vegan?"},
        # The quote the model chose was not in the theme: its first comment.
        {"section": "passed", "label": "Price", "recap_count": 1, "comment_count": 1, "quote": "Too pricey"},
    ]


def test_count_themes_keeps_six_at_most_and_survives_junk():
    comments = [_c(f"L{i}", f"Comment {i}") for i in range(1, 9)]
    raw = {
        "themes": [
            {"section": "liked", "label": f"Theme {i}", "comments": [i], "quote": i}
            for i in range(1, 9)
        ]
    }
    assert len(count_themes(raw, comments)) == 6
    for junk in (None, {}, {"themes": "no"}, {"themes": [None, 3]}):
        assert count_themes(junk, comments) == []


def test_clean_comment_takes_out_names_contacts_and_empty_answers():
    names = ["Jordan Rivera"]
    assert clean_comment("  Jordan  Rivera heard\nit is too sweet ", names) == (
        "the brand ambassador heard it is too sweet"
    )
    assert clean_comment("Email me at pat@example.com or (555) 123-4567", []) == (
        "Email me at [removed] or [removed]"
    )
    assert clean_comment('["Price", "Already have a brand"]', []) == (
        "Price; Already have a brand"
    )
    for empty in (None, "", "  ", "N/A", "none.", "12", "-"):
        assert clean_comment(empty, []) is None
    assert clean_comment("x" * 400, []).endswith("…")
    assert len(clean_comment("x" * 400, [])) == 240


@pytest.mark.parametrize(
    ("name", "kind"),
    [
        ("Consumer feedback", KIND_GENERAL),
        ("Best quote of the day", KIND_GENERAL),
        ("Reasons to decline", KIND_DECLINE),
        ("Why didn't consumers purchase?", KIND_DECLINE),
        ("Common objections", KIND_DECLINE),
        ("List common questions asked by consumers", KIND_QUESTION),
        ("What consumers asked", KIND_QUESTION),
    ],
)
def test_custom_field_names_are_sorted_by_what_they_ask(name, kind):
    assert _field_kind(name) == kind


def test_week_bounds_is_monday_to_sunday():
    from datetime import date

    assert week_bounds("2026-11-11") == (date(2026, 11, 9), date(2026, 11, 15))
    assert week_bounds("2026-11-09") == (date(2026, 11, 9), date(2026, 11, 15))
    assert week_bounds("nonsense") is None
    assert week_bounds(None, date(2026, 11, 18)) == (date(2026, 11, 9), date(2026, 11, 15))


@pytest.mark.parametrize(
    ("name", "read"),
    [
        ("Notes", False),
        ("Additional notes", False),
        ("Field Notes", False),
        ("BA note", False),
        ("Total number of consumers sampled", False),
        ("Notes on consumer feedback", True),
        ("Consumer feedback", True),
        ("Best quote of the day", True),
        ("Positive stories", True),
        ("Reasons to decline", True),
        ("List common questions asked by consumers", True),
    ],
)
def test_notes_fields_are_not_consumer_comments(name, read):
    assert is_comment_field(name) is read


def test_month_bounds_is_first_to_last_day():
    from datetime import date

    assert month_bounds("2026-10-14") == (date(2026, 10, 1), date(2026, 10, 31))
    assert month_bounds("2026-02-28") == (date(2026, 2, 1), date(2026, 2, 28))
    assert month_bounds("2024-02-10") == (date(2024, 2, 1), date(2024, 2, 29))
    assert month_bounds("2026-12-31") == (date(2026, 12, 1), date(2026, 12, 31))
    # With no date: the last finished month, across a year end too.
    assert month_bounds(None, date(2026, 11, 1)) == (date(2026, 10, 1), date(2026, 10, 31))
    assert month_bounds(None, date(2027, 1, 15)) == (date(2026, 12, 1), date(2026, 12, 31))
    assert month_bounds("nonsense") is None
