"""Per-recap feedback tags and the ops reviewer summary.

The model is always mocked. These tests never call OpenAI. They pin:

* thin notes and a missing API key store nothing
* invented competitors and quotes are dropped; verbatim ones are kept
* the BA's original feedback text and ``approved`` are left alone
* the summary uses filed numbers and live rule checks, never a score
* a cached summary is not regenerated
* Needs-review detail may generate once; an approved recap does not
* clients do not receive the ops summary
"""

from __future__ import annotations

from unittest.mock import patch

import pytest
from asgiref.sync import sync_to_async
from django.test import override_settings

from ambassadors.tests.base import AmbassadorsGraphQLTestCase
from recaps import models as recap_models
from recaps.recap_ai import (
    extract_recap_feedback,
    kick_recap_feedback_extract,
    reviewer_summary,
)

NOTES = (
    "Consumers loved the mango flavor but said it was too expensive "
    "next to White Claw and they would not buy it today."
)

EXTRACT_OK = {
    "themes": ["mango flavor"],
    "competitor_products": ["White Claw", "Pepsi"],
    "objections": ["too expensive"],
    "purchase_intent": "unlikely",
    "quotes": [
        "too expensive next to White Claw",
        "everyone bought ten cases",
    ],
}


def _feedback(recap, user, text=NOTES):
    return recap_models.ConsumerFeedback.objects.create(
        recap=recap,
        created_by=user,
        updated_by=user,
        feedback=text,
        quotes="Loved the mango.",
    )


@pytest.mark.django_db(transaction=True)
class TestRecapFeedbackExtract(AmbassadorsGraphQLTestCase):
    @pytest.fixture(autouse=True)
    def setup(self, db):
        self.roles = self.setup_default_roles()
        self.system_user = self.get_system_user()
        self.tenant = self.create_tenant(name="Girl Beer")
        self.event = self.create_event(name="Demo @ Store", tenant=self.tenant)

    def _recap(self, **kwargs):
        defaults = dict(
            name="legacy recap",
            event=self.event,
            created_by=self.system_user,
            updated_by=self.system_user,
            total_engagements=12,
            approved=False,
        )
        defaults.update(kwargs)
        return recap_models.Recap.objects.create(**defaults)

    def test_short_text_stores_nothing_and_skips_the_model(self):
        recap = self._recap()
        _feedback(recap, self.system_user, text="nice")
        with override_settings(OPENAI_API_KEY="test-key"):
            with patch("recaps.recap_ai.generate_json") as mock_ai:
                extract_recap_feedback(recap.id, False)
        mock_ai.assert_not_called()
        assert not recap_models.RecapAiReview.objects.filter(recap_id=recap.id).exists()

    def test_missing_key_skips_quietly(self):
        recap = self._recap()
        _feedback(recap, self.system_user)
        with override_settings(OPENAI_API_KEY=""):
            with patch("recaps.recap_ai.generate_json") as mock_ai:
                extract_recap_feedback(recap.id, False)
        mock_ai.assert_not_called()
        assert not recap_models.RecapAiReview.objects.filter(recap_id=recap.id).exists()

    def test_keeps_verbatim_tags_and_drops_inventions(self):
        recap = self._recap()
        row = _feedback(recap, self.system_user)
        with override_settings(OPENAI_API_KEY="test-key"):
            with patch("recaps.recap_ai.generate_json", return_value=EXTRACT_OK):
                extract_recap_feedback(recap.id, False)
        saved = recap_models.RecapAiReview.objects.get(recap_id=recap.id, is_custom=False)
        extract = saved.feedback_extract
        assert "Pepsi" not in extract["competitor_products"]
        assert "White Claw" in extract["competitor_products"]
        assert "everyone bought ten cases" not in extract["quotes"]
        assert any("White Claw" in quote for quote in extract["quotes"])
        assert extract["purchase_intent"] == "unlikely"
        assert extract["themes"]
        row.refresh_from_db()
        recap.refresh_from_db()
        assert row.feedback == NOTES
        assert recap.approved is False
        assert recap.total_engagements == 12

    def test_empty_after_cleaning_does_not_invent_themes(self):
        recap = self._recap()
        _feedback(recap, self.system_user)
        invented = {
            "themes": ["loyalty program"],
            "competitor_products": ["Pepsi"],
            "objections": ["shipping delay"],
            "purchase_intent": "not_stated",
            "quotes": ["a quote that was never said"],
        }
        with override_settings(OPENAI_API_KEY="test-key"):
            with patch("recaps.recap_ai.generate_json", return_value=invented) as mock_ai:
                extract_recap_feedback(recap.id, False)
                extract_recap_feedback(recap.id, False)
        assert mock_ai.call_count == 1
        saved = recap_models.RecapAiReview.objects.get(recap_id=recap.id)
        assert saved.feedback_extract is None

    def test_custom_recap_notes_extract(self):
        event_type = self.create_event_type(name="Sampling", tenant=self.tenant)
        template = recap_models.CustomRecapTemplate.objects.create(
            name="Template",
            event_type=event_type,
            tenant=self.tenant,
            created_by=self.system_user,
        )
        recap = recap_models.CustomRecap.objects.create(
            name="custom",
            event=self.event,
            tenant=self.tenant,
            custom_recap_template=template,
            total_engagements=8,
            created_by=self.system_user,
            approved=False,
        )
        field_type = recap_models.CustomRecapFieldType.objects.create(
            name="text", created_by=self.system_user
        )
        section = recap_models.RecapSection.objects.create(
            name="Notes", tenant=self.tenant, created_by=self.system_user
        )
        field = recap_models.CustomField.objects.create(
            name="Consumer feedback",
            custom_recap_template=template,
            custom_field_type=field_type,
            recap_section=section,
            created_by=self.system_user,
        )
        value = recap_models.CustomFieldValue.objects.create(
            custom_recap=recap,
            custom_field=field,
            value=NOTES,
            created_by=self.system_user,
        )
        with override_settings(OPENAI_API_KEY="test-key"):
            with patch("recaps.recap_ai.generate_json", return_value=EXTRACT_OK):
                extract_recap_feedback(recap.id, True)
        saved = recap_models.RecapAiReview.objects.get(recap_id=recap.id, is_custom=True)
        assert "White Claw" in saved.feedback_extract["competitor_products"]
        value.refresh_from_db()
        recap.refresh_from_db()
        assert value.value == NOTES
        assert recap.approved is False

    def test_kick_swallows_failures(self):
        with patch(
            "recaps.recap_ai.schedule_recap_feedback_extract",
            side_effect=RuntimeError("queue down"),
        ):
            kick_recap_feedback_extract(1, False)


@pytest.mark.django_db(transaction=True)
class TestRecapReviewerSummary(AmbassadorsGraphQLTestCase):
    @pytest.fixture(autouse=True)
    def setup(self, db):
        self.roles = self.setup_default_roles()
        self.system_user = self.get_system_user()
        self.tenant = self.create_tenant(name="Girl Beer")
        self.event = self.create_event(name="Demo @ Store", tenant=self.tenant)
        self.recap = recap_models.Recap.objects.create(
            name="Store demo",
            event=self.event,
            created_by=self.system_user,
            updated_by=self.system_user,
            total_engagements=12,
            approved=False,
        )
        _feedback(self.recap, self.system_user)

    def test_rules_summary_uses_filed_numbers_and_no_score(self):
        with override_settings(OPENAI_API_KEY=""):
            result = reviewer_summary(self.recap.id, False, generate=False)
        assert result is not None
        assert "score" not in result
        assert "12" in result["what_happened"]
        assert "9999" not in result["what_happened"]
        assert result["source"] == "rules"
        assert result["cached"] is False
        assert any(flag["code"] == "no_photos" for flag in result["looks_off"])
        assert result["follow_up_question"]
        self.recap.refresh_from_db()
        assert self.recap.approved is False

    def test_invented_numbers_fall_back_and_cache(self):
        with override_settings(OPENAI_API_KEY="test-key"):
            with patch(
                "recaps.recap_ai.generate_json",
                return_value={
                    "what_happened": "They reached 9999 people and sold 4000 cases.",
                    "follow_up_question": "Can you confirm the 9999 reach?",
                },
            ) as mock_ai:
                first = reviewer_summary(self.recap.id, False, generate=True)
                second = reviewer_summary(self.recap.id, False, generate=True)
        assert mock_ai.call_count == 1
        assert "9999" not in first["what_happened"]
        assert "12" in first["what_happened"]
        assert first["cached"] is True
        assert second["what_happened"] == first["what_happened"]
        assert "score" not in first

    def test_model_summary_caches_and_does_not_rebill(self):
        with override_settings(OPENAI_API_KEY="test-key"):
            with patch(
                "recaps.recap_ai.generate_json",
                return_value={
                    "what_happened": "Store demo filed 12 engagements and the BA notes are on the recap.",
                    "follow_up_question": "Can you add the missing photos?",
                },
            ) as mock_ai:
                first = reviewer_summary(self.recap.id, False, generate=True)
                second = reviewer_summary(self.recap.id, False, generate=False)
        assert mock_ai.call_count == 1
        assert first["source"] == "model"
        assert first["cached"] is True
        assert second["source"] == "model"
        assert "12" in first["what_happened"]
        self.recap.refresh_from_db()
        assert self.recap.approved is False
        assert self.recap.total_engagements == 12


SUMMARY_QUERY = """
query Summary($recapId: ID!, $isCustom: Boolean!, $generateIfMissing: Boolean!) {
  recapReviewerSummary(
    recapId: $recapId
    isCustom: $isCustom
    generateIfMissing: $generateIfMissing
  ) {
    whatHappened
    followUpQuestion
    cached
    source
    looksOff { code label severity }
    feedback { themes competitorProducts quotes purchaseIntent }
  }
}
"""

SUMMARIZE = """
mutation Summarize($recapId: ID!, $isCustom: Boolean!) {
  summarizeRecap(recapId: $recapId, isCustom: $isCustom) {
    whatHappened
    cached
    source
    followUpQuestion
  }
}
"""


@pytest.mark.django_db(transaction=True)
class TestRecapReviewerSummaryApi(AmbassadorsGraphQLTestCase):
    @pytest.fixture(autouse=True)
    def setup(self, db):
        from config.schema_client import schema_clients

        self.roles = self.setup_default_roles()
        self.schema = schema_clients
        self.endpoint_path = "/api/v1/graphql/clients"
        self.system_user = self.get_system_user()
        self.tenant = self.create_tenant(name="Girl Beer")
        self.spark_admin = self.create_user(
            username="admin-ai-review",
            email="admin-ai-review@test.com",
            role=self.roles["spark_admin"],
        )
        self.client_user = self.create_user(
            username="client-ai-review",
            email="client-ai-review@test.com",
            role=self.roles["client"],
        )
        self.create_tenanted_user(self.client_user, self.tenant)
        self.event = self.create_event(name="Demo", tenant=self.tenant)
        self.recap = recap_models.Recap.objects.create(
            name="Needs review recap",
            event=self.event,
            total_engagements=12,
            created_by=self.system_user,
            updated_by=self.system_user,
            approved=False,
        )
        _feedback(self.recap, self.system_user)

    @pytest.mark.asyncio
    async def test_client_gets_nothing(self):
        result = await self._execute_query_authenticated(
            SUMMARY_QUERY,
            {
                "recapId": str(self.recap.id),
                "isCustom": False,
                "generateIfMissing": True,
            },
            self.client_user,
            self.endpoint_path,
        )
        assert result.errors is None, result.errors
        assert result.data["recapReviewerSummary"] is None

        def _saved():
            return recap_models.RecapAiReview.objects.filter(
                recap_id=self.recap.id, summary__isnull=False
            ).exists()

        assert await sync_to_async(_saved)() is False

    @pytest.mark.asyncio
    async def test_needs_review_generates_once(self):
        variables = {
            "recapId": str(self.recap.id),
            "isCustom": False,
            "generateIfMissing": True,
        }
        with override_settings(OPENAI_API_KEY="test-key"):
            with patch(
                "recaps.recap_ai.generate_json",
                return_value={
                    "what_happened": "Needs review recap filed 12 engagements.",
                    "follow_up_question": "Can you add the photos?",
                },
            ) as mock_ai:
                first = await self._execute_query_authenticated(
                    SUMMARY_QUERY, variables, self.spark_admin, self.endpoint_path
                )
                second = await self._execute_query_authenticated(
                    SUMMARY_QUERY, variables, self.spark_admin, self.endpoint_path
                )
        assert first.errors is None, first.errors
        assert second.errors is None, second.errors
        assert mock_ai.call_count == 1
        assert first.data["recapReviewerSummary"]["source"] == "model"
        assert second.data["recapReviewerSummary"]["cached"] is True
        assert "score" not in first.data["recapReviewerSummary"]

        def _approved():
            self.recap.refresh_from_db()
            return self.recap.approved

        assert await sync_to_async(_approved)() is False

    @pytest.mark.asyncio
    async def test_approved_open_does_not_call_the_model(self):
        self.recap.approved = True
        await sync_to_async(self.recap.save)(update_fields=["approved"])
        with override_settings(OPENAI_API_KEY="test-key"):
            with patch("recaps.recap_ai.generate_json") as mock_ai:
                result = await self._execute_query_authenticated(
                    SUMMARY_QUERY,
                    {
                        "recapId": str(self.recap.id),
                        "isCustom": False,
                        "generateIfMissing": True,
                    },
                    self.spark_admin,
                    self.endpoint_path,
                )
        assert result.errors is None, result.errors
        mock_ai.assert_not_called()
        body = result.data["recapReviewerSummary"]
        assert body["source"] == "rules"
        assert body["cached"] is False
        assert "12" in body["whatHappened"]

    @pytest.mark.asyncio
    async def test_summarize_mutation_does_not_approve(self):
        with override_settings(OPENAI_API_KEY="test-key"):
            with patch(
                "recaps.recap_ai.generate_json",
                return_value={
                    "what_happened": "Needs review recap filed 12 engagements.",
                    "follow_up_question": "Can you add the photos?",
                },
            ):
                result = await self._execute_query_authenticated(
                    SUMMARIZE,
                    {"recapId": str(self.recap.id), "isCustom": False},
                    self.spark_admin,
                    self.endpoint_path,
                )
        assert result.errors is None, result.errors
        assert result.data["summarizeRecap"]["source"] == "model"

        def _unchanged():
            self.recap.refresh_from_db()
            feedback = recap_models.ConsumerFeedback.objects.get(recap=self.recap)
            return self.recap.approved, feedback.feedback

        approved, feedback = await sync_to_async(_unchanged)()
        assert approved is False
        assert feedback == NOTES
