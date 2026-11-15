"""What a brand user can reach through the AI features.

Runs against the real ``schema_clients`` surface with the model call
mocked, and asserts on the prompt that would have been sent:

  * a brand user (or any non-staff role) asking about another brand gets
    nothing from it: a campaign that is not theirs is "not found", and a
    foreign ``tenantId`` is replaced by their own;
  * a brand user's prompt holds approved recaps only: no needs-review
    recap, no archived recap, nothing under a deleted request;
  * a brand user's prompt carries no ambassador name;
  * Ignite staff see what they saw before.
"""

from datetime import timedelta
from unittest.mock import patch

import pytest
from asgiref.sync import sync_to_async
from django.utils import timezone

from ambassadors.tests.base import AmbassadorsGraphQLTestCase
from events import models as event_models
from recaps import models as recap_models


ASK = """
query Ask($tenantId: ID!, $question: String!, $recapId: ID, $eventId: ID) {
  tenantAiAnswer(
    tenantId: $tenantId
    question: $question
    recapId: $recapId
    eventId: $eventId
  ) { ok answer reason }
}
"""

SUMMARY = """
query Summary($requestId: ID!) {
  campaignReportAiSummary(requestId: $requestId) { ok summary reason }
}
"""

ANSWER = """
query Answer($requestId: ID!, $question: String!) {
  campaignReportAiAnswer(requestId: $requestId, question: $question) {
    ok answer reason
  }
}
"""

REPORT = """
query Report($requestId: ID!) {
  campaignReport(requestId: $requestId) { brandName title }
}
"""

NOT_FOUND = "Report not found or not accessible."


@pytest.mark.django_db(transaction=True)
class TestAiBrandScope(AmbassadorsGraphQLTestCase):
    @pytest.fixture(autouse=True)
    def setup(self, db):
        from config.schema_client import schema_clients

        self.roles = self.setup_default_roles()
        self.schema = schema_clients
        self.endpoint_path = "/api/v1/graphql/clients"
        self.system_user = self.get_system_user()

        self.tenant = self.create_tenant(name="Alpha Brand")
        self.other = self.create_tenant(name="Beta Brand")

        self.admin = self.create_user(
            username="ai-scope-admin",
            email="ai-scope-admin@test.com",
            role=self.roles["spark_admin"],
        )
        self.client_user = self.create_user(
            username="ai-scope-client",
            email="ai-scope-client@test.com",
            role=self.roles["client"],
        )
        self.create_tenanted_user(self.client_user, self.tenant)
        # Not staff and not a "client": an ambassador attached to brand A.
        self.ba_user = self.create_user(
            username="ai-scope-ba",
            email="ai-scope-ba@test.com",
            role=self.roles["ambassador"],
        )
        self.create_tenanted_user(self.ba_user, self.tenant)

        credited = self.create_user(
            username="jordan-rivera-ai",
            email="jordan.rivera.ai@ba.test",
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

        self.event_a = self._event(self.tenant, "Alpha Store One", self.request_a)
        self.approved = self._recap(
            self.event_a, "APPROVED ALPHA QUOTE", engagements=10, approved=True
        )
        self.pending = self._recap(
            self.event_a, "PENDING ALPHA QUOTE", engagements=7, approved=False
        )
        self.archived = self._recap(
            self.event_a, "ARCHIVED ALPHA QUOTE", engagements=5, approved=True
        )
        recap_models.Recap.objects.filter(id=self.archived.id).update(
            archived_at=timezone.now()
        )

        deleted_event = self._event(
            self.tenant, "Alpha Deleted Store", self.deleted_request
        )
        self._recap(deleted_event, "DELETED ALPHA QUOTE", engagements=3, approved=True)

        self.event_b = self._event(self.other, "Beta Store One", self.request_b)
        self.recap_b = self._recap(
            self.event_b, "BETA SECRET QUOTE", engagements=99, approved=True
        )

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

    def _event(self, tenant, name, request):
        now = timezone.now()
        event = self.create_event(
            name=name,
            tenant=tenant,
            date=now,
            start_time=now,
            end_time=now + timedelta(hours=4),
        )
        event_models.Event.objects.filter(id=event.id).update(request=request)
        event.refresh_from_db()
        return event

    def _recap(self, event, quote, *, engagements, approved):
        recap = recap_models.Recap.objects.create(
            name=f"recap {quote}",
            event=event,
            total_engagements=engagements,
            approved=approved,
            submited_at=timezone.now(),
            ambassador=self.credited_ba,
            created_by=self.system_user,
            updated_by=self.system_user,
        )
        recap_models.ConsumerFeedback.objects.create(
            recap=recap,
            quotes=quote,
            feedback=f"Jordan Rivera said: {quote}",
        )
        return recap

    # ── calls ─────────────────────────────────────────────────────────

    async def _ask(self, user, tenant, **scope):
        with patch(
            "recaps.report_types.generate_structured_answer",
            return_value=("An answer.", None),
        ) as generate:
            result = await self._execute_query_authenticated(
                ASK,
                {"tenantId": str(tenant.id), "question": "How did it go?", **scope},
                user,
                self.endpoint_path,
            )
        assert result.errors is None, result.errors
        prompt = generate.call_args[0][1] if generate.call_args else None
        return result.data["tenantAiAnswer"], prompt

    async def _summary(self, user, request):
        with patch(
            "recaps.report_types.generate_summary", return_value="A summary."
        ) as generate:
            result = await self._execute_query_authenticated(
                SUMMARY, {"requestId": str(request.uuid)}, user, self.endpoint_path
            )
        assert result.errors is None, result.errors
        prompt = generate.call_args[0][1] if generate.call_args else None
        return result.data["campaignReportAiSummary"], prompt

    async def _answer(self, user, request):
        with patch(
            "recaps.report_types.generate_structured_answer",
            return_value=("An answer.", None),
        ) as generate:
            result = await self._execute_query_authenticated(
                ANSWER,
                {"requestId": str(request.uuid), "question": "How did it go?"},
                user,
                self.endpoint_path,
            )
        assert result.errors is None, result.errors
        prompt = generate.call_args[0][1] if generate.call_args else None
        return result.data["campaignReportAiAnswer"], prompt

    # ── (a) another brand ─────────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_client_cannot_summarize_another_brands_campaign(self):
        payload, prompt = await self._summary(self.client_user, self.request_b)
        assert payload["ok"] is False
        assert payload["reason"] == NOT_FOUND
        assert prompt is None, "the model must not be called"

    @pytest.mark.asyncio
    async def test_non_client_role_cannot_summarize_another_brands_campaign(self):
        payload, prompt = await self._summary(self.ba_user, self.request_b)
        assert payload["ok"] is False
        assert payload["reason"] == NOT_FOUND
        assert prompt is None, "the model must not be called"

    @pytest.mark.asyncio
    async def test_non_client_role_cannot_ask_about_another_brands_campaign(self):
        payload, prompt = await self._answer(self.ba_user, self.request_b)
        assert payload["ok"] is False
        assert payload["reason"] == NOT_FOUND
        assert prompt is None, "the model must not be called"

    @pytest.mark.asyncio
    async def test_non_client_role_cannot_read_another_brands_campaign_report(self):
        result = await self._execute_query_authenticated(
            REPORT,
            {"requestId": str(self.request_b.uuid)},
            self.ba_user,
            self.endpoint_path,
        )
        assert result.errors is None, result.errors
        assert result.data["campaignReport"] is None

    @pytest.mark.asyncio
    async def test_client_asking_about_another_tenant_is_held_to_their_own(self):
        for user in (self.client_user, self.ba_user):
            payload, prompt = await self._ask(user, self.other)
            assert payload["ok"] is True
            assert "Brand: Alpha Brand" in prompt
            assert "Beta" not in prompt
            assert "BETA SECRET QUOTE" not in prompt

    @pytest.mark.asyncio
    async def test_client_cannot_scope_to_another_brands_recap_or_event(self):
        payload, prompt = await self._ask(
            self.client_user, self.other, recapId=str(self.recap_b.id)
        )
        assert payload["ok"] is False
        assert prompt is None
        payload, prompt = await self._ask(
            self.client_user, self.other, eventId=str(self.event_b.id)
        )
        assert payload["ok"] is False
        assert prompt is None

    # ── (b) approved data only, no ambassador names ───────────────────

    @pytest.mark.asyncio
    async def test_client_overview_prompt_holds_approved_recaps_only(self):
        payload, prompt = await self._ask(self.client_user, self.tenant)
        assert payload["ok"] is True
        assert "APPROVED ALPHA QUOTE" in prompt
        assert "PENDING ALPHA QUOTE" not in prompt
        assert "ARCHIVED ALPHA QUOTE" not in prompt
        assert "DELETED ALPHA QUOTE" not in prompt
        assert "Alpha Deleted Store" not in prompt
        # 10 approved + 5 approved-then-archived. The dashboard totals count
        # archived recaps today and the overview reads the same totals; the
        # 7 awaiting review and the 3 under a deleted request stay out.
        assert "Total engagements: 15" in prompt
        assert "Rivera" not in prompt

    @pytest.mark.asyncio
    async def test_client_cannot_ask_about_an_unapproved_recap(self):
        for recap in (self.pending, self.archived):
            payload, prompt = await self._ask(
                self.client_user, self.tenant, recapId=str(recap.id)
            )
            assert payload["ok"] is False
            assert payload["reason"] == "Recap not found or not accessible."
            assert prompt is None, "the model must not be called"

    @pytest.mark.asyncio
    async def test_client_recap_prompt_has_no_ambassador_name(self):
        payload, prompt = await self._ask(
            self.client_user, self.tenant, recapId=str(self.approved.id)
        )
        assert payload["ok"] is True
        assert "APPROVED ALPHA QUOTE" in prompt
        assert "Rivera" not in prompt
        assert "Jordan" not in prompt
        assert "\nBA:" not in prompt

    @pytest.mark.asyncio
    async def test_client_event_prompt_holds_approved_recaps_only(self):
        payload, prompt = await self._ask(
            self.client_user, self.tenant, eventId=str(self.event_a.id)
        )
        assert payload["ok"] is True
        assert "APPROVED ALPHA QUOTE" in prompt
        assert "PENDING ALPHA QUOTE" not in prompt
        assert "ARCHIVED ALPHA QUOTE" not in prompt
        assert "Rivera" not in prompt
        assert "\nBA:" not in prompt

    @pytest.mark.asyncio
    async def test_client_campaign_prompts_hold_approved_recaps_only(self):
        for call in (self._summary, self._answer):
            payload, prompt = await call(self.client_user, self.request_a)
            assert payload["ok"] is True
            assert "Brand: Alpha Brand" in prompt
            assert "APPROVED ALPHA QUOTE" in prompt
            assert "PENDING ALPHA QUOTE" not in prompt
            assert "ARCHIVED ALPHA QUOTE" not in prompt
            assert "Total engagements: 10" in prompt
            assert "Rivera" not in prompt

    @pytest.mark.asyncio
    async def test_client_cannot_summarize_a_deleted_campaign(self):
        payload, prompt = await self._summary(self.client_user, self.deleted_request)
        assert payload["ok"] is False
        assert prompt is None

    # ── (c) staff: unchanged ──────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_staff_can_ask_about_any_tenant(self):
        payload, prompt = await self._ask(self.admin, self.other)
        assert payload["ok"] is True
        assert "Brand: Beta Brand" in prompt
        assert "BETA SECRET QUOTE" in prompt

    @pytest.mark.asyncio
    async def test_staff_overview_prompt_is_unchanged(self):
        payload, prompt = await self._ask(self.admin, self.tenant)
        assert payload["ok"] is True
        assert "APPROVED ALPHA QUOTE" in prompt
        assert "PENDING ALPHA QUOTE" in prompt

    @pytest.mark.asyncio
    async def test_staff_can_ask_about_an_unapproved_recap_with_its_ambassador(self):
        payload, prompt = await self._ask(
            self.admin, self.tenant, recapId=str(self.pending.id)
        )
        assert payload["ok"] is True
        assert "PENDING ALPHA QUOTE" in prompt
        assert "BA: Jordan Rivera" in prompt
        assert "STATUS: needs review" in prompt

    @pytest.mark.asyncio
    async def test_staff_event_prompt_includes_every_recap(self):
        payload, prompt = await self._ask(
            self.admin, self.tenant, eventId=str(self.event_a.id)
        )
        assert payload["ok"] is True
        assert "APPROVED ALPHA QUOTE" in prompt
        assert "PENDING ALPHA QUOTE" in prompt
        assert "BA: Jordan Rivera" in prompt

    @pytest.mark.asyncio
    async def test_staff_campaign_prompt_is_unchanged(self):
        payload, prompt = await self._summary(self.admin, self.request_b)
        assert payload["ok"] is True
        assert "BETA SECRET QUOTE" in prompt
        payload, prompt = await self._summary(self.admin, self.request_a)
        assert payload["ok"] is True
        assert "APPROVED ALPHA QUOTE" in prompt
        assert "PENDING ALPHA QUOTE" in prompt
        assert "Total engagements: 22" in prompt

    # ── helpers kept honest ───────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_inactive_membership_gets_nothing(self):
        await sync_to_async(
            lambda: self.client_user.tenanted_users.update(is_active=False)
        )()
        with patch(
            "recaps.report_types.generate_summary", return_value="A summary."
        ) as generate:
            result = await self._execute_query_authenticated(
                SUMMARY,
                {"requestId": str(self.request_a.uuid)},
                self.client_user,
                self.endpoint_path,
            )
        generate.assert_not_called()
        payload = (result.data or {}).get("campaignReportAiSummary")
        assert result.errors or payload["ok"] is False


def test_ambassador_names_are_replaced_in_free_text():
    from recaps.scoped_ai import _hide_names

    text = "notes: Jordan  Rivera ran the table. jordan rivera restocked. Riveras Market."
    hidden = _hide_names(text, ["Jordan Rivera"])
    assert "Rivera ran" not in hidden
    assert "jordan rivera" not in hidden.lower().replace("riveras market", "")
    assert hidden.count("the brand ambassador") == 2
    assert "Riveras Market" in hidden
    # A one-word name is not replaced: it would hit "Sam's Club" too.
    assert _hide_names("Sam's Club, with Sam", ["Sam"]) == "Sam's Club, with Sam"


@pytest.mark.parametrize(
    "label, hidden",
    [
        ("BA Name", True),
        ("Brand Ambassador Name", True),
        ("Name of ambassador", True),
        ("Your name", True),
        ("Name", True),
        ("Store name", False),
        ("Product name", False),
        ("Samples handed out", False),
        ("Manager name", False),
    ],
)
def test_form_fields_that_hold_the_ambassadors_name(label, hidden):
    from recaps.scoped_ai import _BA_NAME_FIELD_RE

    assert bool(_BA_NAME_FIELD_RE.search(label)) is hidden
