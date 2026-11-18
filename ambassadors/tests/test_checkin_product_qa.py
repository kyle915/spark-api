"""Product questions on the public check-in answer from the brand's own
material only.

``POST /checkin/<code>/product-question`` goes through the real view. These
pin that:

* a grounded answer comes back short, with a "From <brand> notes" source;
* when the model says the material doesn't cover it, the BA gets the fixed
  "check with your lead" line, never the model's own words;
* a brand with no notes, catalog or training gets "no notes yet" and the
  model is never called; no OpenAI key gives the "unavailable" line;
* the prompt carries this brand's material and nothing from another brand;
* a session for brand A cannot ask on brand B's link;
* the per-shift limit holds, and every question is logged;
* staff read/write the notes through GraphQL; non-staff can't.

The OpenAI call is always mocked at ``utils.ai_text._post_json``.
"""

from __future__ import annotations

import json
import uuid

import pytest
from asgiref.sync import sync_to_async
from django.core.cache import cache
from django.test import Client as DjangoClient
from django.urls import reverse
from django.utils import timezone

from ambassadors import product_qa
from ambassadors.tests.base import AmbassadorsGraphQLTestCase
from config.schema_client import schema_clients
from events.checkin_tokens import make_checkin_session_token
from events import checkin_views
from tenants.models import BaProductQuestion


def _completion(payload: dict) -> dict:
    return {"choices": [{"message": {"content": json.dumps(payload)}}]}


def test_tidy_answer_keeps_three_sentences_and_drops_markdown():
    text = "**One.** Two! Three? Four. Five."
    assert product_qa.tidy_answer(text) == "One. Two! Three?"
    assert product_qa.tidy_answer("- Bullet answer") == "Bullet answer"
    assert len(product_qa.tidy_answer("word " * 400)) <= product_qa.ANSWER_MAX_CHARS


@pytest.mark.django_db(transaction=True)
class TestCheckinProductQuestion(AmbassadorsGraphQLTestCase):
    @pytest.fixture(autouse=True)
    def setup(self, db, settings):
        from academy.models import TrainingHub, TrainingResource
        from events.models import Product, ProductType

        cache.clear()
        settings.OPENAI_API_KEY = "test-key"
        settings.OPENAI_MODEL = "gpt-4o-mini"
        settings.OPENAI_MODEL_SMALL = "gpt-4o-mini"

        self.roles = self.setup_default_roles()
        self.tenant = self.create_tenant(name="Torch")
        self.other = self.create_tenant(name="Rival Seltzer")
        self.actor = self.create_user(
            username="actor-qa@test.com",
            email="actor-qa@test.com",
            role=self.roles["spark_admin"],
        )
        ba_user = self.create_user(
            username="ba-qa@test.com",
            email="ba-qa@test.com",
            role=self.roles["ambassador"],
            first_name="Dana",
        )
        self.ba = self.create_ambassador(ba_user, phone="555-222-1111")

        self.tenant.ba_product_notes = "OG Luxe: 100mg caffeine per 12oz can. Made with real lime."
        self.tenant.save(update_fields=["ba_product_notes"])
        ptype = ProductType.objects.create(
            name="Beverage", tenant=self.tenant, created_by=self.actor
        )
        Product.objects.create(
            name="OG Luxe",
            description="Sparkling lime energy drink.",
            product_type=ptype,
            tenant=self.tenant,
            created_by=self.actor,
        )
        hub = TrainingHub.objects.create(
            tenant=self.tenant, code="TO-QA1", title="Torch BA training", intro="Smile and pour."
        )
        TrainingResource.objects.create(
            hub=hub, title="Pour size", description="Pour 2oz samples."
        )

        # Brand B has its own, distinct material that must never leak.
        self.other.ba_product_notes = "SECRET-RIVAL-FACT: 999mg of something."
        self.other.save(update_fields=["ba_product_notes"])
        optype = ProductType.objects.create(
            name="Seltzer", tenant=self.other, created_by=self.actor
        )
        Product.objects.create(
            name="Rival Mango",
            description="RIVAL-ONLY-DESCRIPTION",
            product_type=optype,
            tenant=self.other,
            created_by=self.actor,
        )

        self.event = self.create_event(
            name="Kroger Houston", tenant=self.tenant, address="1 Main St", date=timezone.now()
        )
        self.event.walkup_code = "CK-QA1"
        self.event.save(update_fields=["walkup_code"])
        self.other_event = self.create_event(
            name="HEB Austin", tenant=self.other, address="2 Main St", date=timezone.now()
        )
        self.other_event.walkup_code = "CK-QA2"
        self.other_event.save(update_fields=["walkup_code"])

        self.http = DjangoClient()
        self.token = make_checkin_session_token(self.event.id, self.ba.id)

    # ---------- helpers ----------

    def _ask(self, question="How much caffeine is in OG Luxe?", code=None, token=None):
        return self.http.post(
            reverse(
                "events.public_checkin_product_question",
                kwargs={"code": code or self.event.walkup_code},
            ),
            data={"session": token or self.token, "question": question},
            content_type="application/json",
        )

    def _model(self, monkeypatch, reply: dict):
        calls = []

        def post(url, payload, headers, timeout):
            calls.append(payload)
            return _completion(reply)

        monkeypatch.setattr("utils.ai_text._post_json", post)
        return calls

    # ---------- answers ----------

    def test_grounded_answer_with_source_line(self, monkeypatch):
        calls = self._model(
            monkeypatch,
            {"answered": True, "answer": "OG Luxe has 100mg of caffeine per 12oz can."},
        )
        res = self._ask()
        assert res.status_code == 200
        body = res.json()
        assert body == {
            "answer": "OG Luxe has 100mg of caffeine per 12oz can.",
            "answered": True,
            "outcome": "answered",
            "source": "From Torch notes",
        }
        assert len(calls) == 1
        row = BaProductQuestion.objects.get()
        assert row.tenant_id == self.tenant.id
        assert row.event_id == self.event.id
        assert row.ambassador_id == self.ba.id
        assert row.question == "How much caffeine is in OG Luxe?"
        assert row.outcome == "answered"

    def test_prompt_holds_only_this_brands_material(self, monkeypatch):
        calls = self._model(monkeypatch, {"answered": True, "answer": "Yes."})
        self._ask()
        payload = calls[0]
        system = payload["messages"][0]["content"]
        user = payload["messages"][1]["content"]
        assert "ONLY from the BRAND MATERIAL" in system
        assert "health, medical" in system
        assert "another brand" in system
        assert "BRAND: Torch" in user
        assert "100mg caffeine per 12oz can" in user  # notes
        assert "Sparkling lime energy drink." in user  # catalog
        assert "Pour 2oz samples." in user  # training hub
        assert "SECRET-RIVAL-FACT" not in user
        assert "Rival Mango" not in user
        assert "RIVAL-ONLY-DESCRIPTION" not in user
        assert "Rival Seltzer" not in json.dumps(payload)
        assert payload["response_format"]["type"] == "json_schema"

    def test_not_in_material_gets_the_fixed_line(self, monkeypatch):
        self._model(
            monkeypatch,
            {"answered": False, "answer": "Probably about 5% sugar, I'd guess."},
        )
        body = self._ask("Is it keto?").json()
        assert body["answered"] is False
        assert body["answer"] == product_qa.NOT_IN_NOTES
        assert body["source"] == ""
        assert "guess" not in body["answer"]
        assert BaProductQuestion.objects.get().outcome == "not_in_notes"

    def test_long_model_answer_is_trimmed(self, monkeypatch):
        self._model(
            monkeypatch,
            {"answered": True, "answer": "One. Two. Three. Four. Five."},
        )
        assert self._ask().json()["answer"] == "One. Two. Three."

    def test_no_material_refuses_without_calling_the_model(self, monkeypatch):
        calls = self._model(monkeypatch, {"answered": True, "answer": "x"})
        empty = self.create_tenant(name="Blank Brand")
        event = self.create_event(
            name="Blank store", tenant=empty, address="3 Main St", date=timezone.now()
        )
        event.walkup_code = "CK-QA3"
        event.save(update_fields=["walkup_code"])
        token = make_checkin_session_token(event.id, self.ba.id)
        body = self._ask(code="CK-QA3", token=token).json()
        assert body["answered"] is False
        assert body["outcome"] == "no_material"
        assert body["answer"] == product_qa.NO_NOTES_YET
        assert calls == []
        assert BaProductQuestion.objects.get().tenant_id == empty.id

    def test_no_key_is_unavailable(self, monkeypatch, settings):
        settings.OPENAI_API_KEY = ""
        calls = self._model(monkeypatch, {"answered": True, "answer": "x"})
        body = self._ask().json()
        assert body["outcome"] == "ai_unavailable"
        assert body["answer"] == product_qa.AI_UNAVAILABLE
        assert calls == []

    def test_model_failure_is_unavailable(self, monkeypatch):
        from utils.ai_text import AiUnavailable

        def post(url, payload, headers, timeout):
            raise AiUnavailable("boom")

        monkeypatch.setattr("utils.ai_text._post_json", post)
        body = self._ask().json()
        assert body["outcome"] == "ai_unavailable"
        assert body["answered"] is False

    # ---------- access ----------

    def test_session_for_one_brand_cannot_ask_on_another_brands_link(self, monkeypatch):
        calls = self._model(monkeypatch, {"answered": True, "answer": "x"})
        res = self._ask(code=self.other_event.walkup_code, token=self.token)
        assert res.status_code == 401
        assert calls == []
        assert BaProductQuestion.objects.count() == 0

    def test_requires_a_session(self, monkeypatch):
        self._model(monkeypatch, {"answered": True, "answer": "x"})
        res = self.http.post(
            reverse("events.public_checkin_product_question", kwargs={"code": "CK-QA1"}),
            data={"question": "What's in it?"},
            content_type="application/json",
        )
        assert res.status_code == 401

    def test_empty_and_overlong_questions_are_refused(self, monkeypatch):
        calls = self._model(monkeypatch, {"answered": True, "answer": "x"})
        assert self._ask("  ").status_code == 400
        assert self._ask("x" * 301).status_code == 400
        assert calls == []

    def test_per_shift_rate_limit(self, monkeypatch):
        self._model(monkeypatch, {"answered": True, "answer": "Yes."})
        monkeypatch.setattr(checkin_views, "PRODUCT_QA_SESSION_LIMIT", 2)
        assert self._ask().status_code == 200
        assert self._ask().status_code == 200
        res = self._ask()
        assert res.status_code == 429
        assert res.json()["error"] == "rate_limited"
        assert BaProductQuestion.objects.count() == 2

    def test_per_brand_rate_limit(self, monkeypatch):
        self._model(monkeypatch, {"answered": True, "answer": "Yes."})
        monkeypatch.setattr(checkin_views, "PRODUCT_QA_TENANT_LIMIT", 1)
        assert self._ask().status_code == 200
        assert self._ask().status_code == 429

    def test_context_carries_box_state_only_after_identify(self):
        url = reverse("events.public_checkin_context", kwargs={"code": "CK-QA1"})
        before = self.http.get(url).json()
        assert "productQa" not in before
        after = self.http.get(url, HTTP_X_CHECKIN_SESSION=self.token).json()
        assert after["productQa"] == {
            "hasMaterial": True,
            "hasNotes": True,
            "aiAvailable": True,
        }


@pytest.mark.django_db(transaction=True)
class TestBaProductNotesStaffSchema(AmbassadorsGraphQLTestCase):
    endpoint = "/api/v1/graphql/clients"

    @pytest.fixture(autouse=True)
    def setup(self, db):
        self.roles = self.setup_default_roles()
        self.tenant = self.create_tenant(name="Torch")
        uid = str(uuid.uuid4())[:8]
        self.staff = self.create_user(
            username=f"staff_{uid}@test.com",
            email=f"staff_{uid}@test.com",
            role=self.roles["spark_admin"],
        )
        self.client_user = self.create_user(
            username=f"client_{uid}@test.com",
            email=f"client_{uid}@test.com",
            role=self.roles["client"],
        )
        self.create_tenanted_user(self.client_user, self.tenant)
        self.schema = schema_clients

    SET = """
        mutation Set($tenantId: ID!, $notes: String!) {
            setBaProductNotes(tenantId: $tenantId, notes: $notes) {
                notes hasMaterial questions { question }
            }
        }
    """
    READ = """
        query Read($tenantId: ID!) {
            baProductQa(tenantId: $tenantId) {
                notes hasMaterial aiAvailable
                questions { question answer outcome askedAt eventName }
            }
        }
    """

    @pytest.mark.asyncio
    async def test_staff_set_and_read_notes_and_questions(self):
        res = await self._execute_mutation_authenticated(
            self.SET,
            {"tenantId": str(self.tenant.id), "notes": "  100mg caffeine.\r\n"},
            self.staff,
            self.endpoint,
        )
        assert res.errors is None, res.errors
        assert res.data["setBaProductNotes"]["notes"] == "100mg caffeine."
        assert res.data["setBaProductNotes"]["hasMaterial"] is True

        await sync_to_async(BaProductQuestion.objects.create)(
            tenant=self.tenant, question="Is it vegan?", answer=product_qa.NOT_IN_NOTES,
            outcome="not_in_notes",
        )
        res = await self._execute_query_authenticated(
            self.READ, {"tenantId": str(self.tenant.id)}, self.staff, self.endpoint
        )
        assert res.errors is None, res.errors
        data = res.data["baProductQa"]
        assert data["notes"] == "100mg caffeine."
        assert data["questions"][0]["question"] == "Is it vegan?"
        assert data["questions"][0]["outcome"] == "not_in_notes"

    @pytest.mark.asyncio
    async def test_non_staff_cannot_read_or_write(self):
        res = await self._execute_mutation_authenticated(
            self.SET,
            {"tenantId": str(self.tenant.id), "notes": "made up"},
            self.client_user,
            self.endpoint,
        )
        assert res.errors
        res = await self._execute_query_authenticated(
            self.READ, {"tenantId": str(self.tenant.id)}, self.client_user, self.endpoint
        )
        assert res.data["baProductQa"] is None
        await sync_to_async(self.tenant.refresh_from_db)()
        assert self.tenant.ba_product_notes == ""
