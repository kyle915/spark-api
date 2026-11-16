"""Other brands people mentioned, and what sells where.

Runs against the real ``schema_clients`` surface with the web client's own
field sets. The OpenAI transport (``utils.ai_text._post_json``) is replaced
in every test, so nothing here reaches the network, and the prompts that
would have been sent are what the data rules are asserted on:

  * only approved, unarchived recaps under a live request, in the window by
    event date, and no ambassador name;
  * a brand user is held to their own tenant, staff may choose;
  * a brand or a product the comment does not name is dropped;
  * every count comes from Python, not from the model;
  * floors: too few mentions is one sentence, one event cannot name a
    state's leading product;
  * a second load reads the stored row; with the model unavailable each
    section says so and stores nothing.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, time, timedelta
from unittest.mock import patch

import pytest
from asgiref.sync import sync_to_async
from django.utils import timezone

from ambassadors.tests.base import AmbassadorsGraphQLTestCase
from events import models as event_models
from recaps import models as recap_models
from recaps.comment_insights import (
    LEAD_NO_LEADER,
    LEAD_READY,
    LEAD_TOO_FEW,
    count_mentions,
    is_own_name,
    keep_feelings,
    names_product,
    normalise,
    summarize_regions,
    written_in,
)
from recaps.feedback_themes import KIND_GENERAL, Comment
from recaps.weekly_narration import previous_complete_week
from utils.ai_text import AiUnavailable

# The field sets the web client asks for.
MENTIONS = """
query Mentions($tenantId: ID!, $startDate: String, $endDate: String) {
  tenantCompetitorMentions(tenantId: $tenantId, startDate: $startDate, endDate: $endDate) {
    status
    mentionCommentCount
    recapCount
    commentCount
    truncated
    brands { brand recapCount preferOurs preferTheirs neutral quote }
  }
}
"""

REGIONS = """
query Regions($tenantId: ID!, $startDate: String, $endDate: String) {
  tenantProductRegions(tenantId: $tenantId, startDate: $startDate, endDate: $endDate) {
    status
    commentsStatus
    hasSold
    hasSampled
    recapCount
    minStateRecaps
    states {
      state
      recaps
      soldStatus soldProduct soldUnits soldRecaps
      sampledStatus sampledProduct sampledUnits sampledRecaps
      spokenStatus spokenProduct spokenPositive spokenNegative spokenRecaps
    }
  }
}
"""


def _reply(content: str) -> dict:
    return {"choices": [{"message": {"content": content}}]}


def _numbered(prompt: str):
    for line in prompt.splitlines():
        match = re.match(r"^(\d+)\.(?: \[\w+\])? (.*)$", line)
        if match:
            yield int(match[1]), match[2].lower()


def _fake_model(url, payload, headers, timeout):
    """Tags comments by keyword, and makes the mistakes a model makes: a
    brand the comment never names, the tenant's own product called another
    brand, a product the comment does not name, a comment that does not
    exist, and totals nobody asked for."""
    system = payload["messages"][0]["content"]
    user = payload["messages"][1]["content"]
    mentions = []
    if "ANOTHER brand" in system:
        for number, text in _numbered(user):
            if "white claw" in text or "whiteclaw" in text:
                stance = (
                    "prefers_ours"
                    if "better than" in text
                    else "prefers_theirs"
                    if "prefer white claw" in text
                    else "neutral"
                )
                mentions.append({"comment": number, "brand": "White Claw", "stance": stance})
            if "truly" in text:
                mentions.append(
                    {"comment": number, "brand": "truly", "stance": "prefers_theirs"}
                )
            if "citrus splash" in text:
                mentions.append(
                    {"comment": number, "brand": "Citrus Splash", "stance": "neutral"}
                )
        mentions.append({"comment": 1, "brand": "Topo Chico", "stance": "prefers_ours"})
        mentions.append({"comment": 999, "brand": "White Claw", "stance": "neutral"})
        return _reply(json.dumps({"mentions": mentions, "total": 50}))
    products = dict(
        (name.lower(), int(number))
        for number, name in re.findall(r"^P(\d+): (.+)$", system, flags=re.M)
    )
    for number, text in _numbered(user):
        for name, product in products.items():
            if name in text:
                feeling = "negative" if "too sweet" in text else "positive"
                mentions.append({"comment": number, "product": product, "feeling": feeling})
    # The first comment is about Citrus Splash only.
    mentions.append({"comment": 1, "product": products["berry blast"], "feeling": "positive"})
    mentions.append({"comment": 999, "product": 1, "feeling": "positive"})
    return _reply(json.dumps({"mentions": mentions}))


def _calls(post, marker: str) -> list[str]:
    """User prompts of the captured calls whose system prompt has ``marker``."""
    return [
        call.args[1]["messages"][1]["content"]
        for call in post.call_args_list
        if marker in call.args[1]["messages"][0]["content"]
    ]


@pytest.mark.django_db(transaction=True)
class TestCommentInsights(AmbassadorsGraphQLTestCase):
    @pytest.fixture(autouse=True)
    def setup(self, db, settings):
        from config.schema_client import schema_clients

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
            username="insight-admin",
            email="insight-admin@test.com",
            role=self.roles["spark_admin"],
        )
        self.client_user = self.create_user(
            username="insight-client",
            email="insight-client@test.com",
            role=self.roles["client"],
        )
        self.create_tenanted_user(self.client_user, self.tenant)
        credited = self.create_user(
            username="jordan-rivera-insight",
            email="jordan.rivera.insight@ba.test",
            role=self.roles["ambassador"],
        )
        credited.first_name, credited.last_name = "Jordan", "Rivera"
        credited.save()
        self.ba = self.create_ambassador(credited)

        product_type = event_models.ProductType.objects.create(
            name="Seltzer", tenant=self.tenant, created_by=self.system_user
        )
        self.citrus, self.berry = (
            event_models.Product.objects.create(
                name=name,
                product_type=product_type,
                tenant=self.tenant,
                created_by=self.system_user,
            )
            for name in ("Citrus Splash", "Berry Blast")
        )

        self.request_a = self._request(self.tenant, "Alpha Tour")
        self.request_b = self._request(self.other, "Beta Tour")
        deleted_request = self._request(self.tenant, "Alpha Deleted Tour")
        event_models.Request.objects.filter(id=deleted_request.id).update(
            deleted_at=timezone.now()
        )

        texas = event_models.State.objects.create(
            name="Texas", code="TX", created_by=self.system_user
        )
        # One event carries the state itself, the others only an address.
        self.tx1 = self._event(self.tenant, "TX One", self.request_a, 1, state=texas)
        self.tx2 = self._event(
            self.tenant, "TX Two", self.request_a, 2, address="5 Elm St, Dallas, TX 75201"
        )
        self.tx3 = self._event(
            self.tenant, "TX Three", self.request_a, 3, address="9 Oak Ave, Austin, TX"
        )
        self.ca1 = self._event(
            self.tenant, "CA One", self.request_a, 2, address="2 Bay St, Oakland, CA 94607"
        )

        self.r1 = self._recap(
            self.tx1,
            quotes="Tastes better than White Claw",
            feedback="Jordan Rivera said Citrus Splash was the favorite",
        )
        self.r2 = self._recap(
            self.tx2,
            quotes="I usually buy whiteclaw but Citrus Splash is great",
            reasons_to_decline="Cheaper to get Truly",
        )
        self.r3 = self._recap(
            self.tx3,
            quotes="Loved Citrus Splash",
            feedback="I prefer White Claw's taste honestly",
        )
        self.r4 = self._recap(self.ca1, quotes="Berry Blast was too sweet")
        self._recap(self.tx1, quotes="PENDING likes Topo Chico", approved=False)
        archived = self._recap(self.tx1, quotes="ARCHIVED likes White Claw")
        recap_models.Recap.objects.filter(id=archived.id).update(
            archived_at=timezone.now()
        )
        deleted_event = self._event(
            self.tenant, "Deleted", deleted_request, 2, address="1 A St, Waco, TX"
        )
        self._recap(deleted_event, quotes="DELETED likes White Claw")
        old_event = self._event(
            self.tenant, "Old", self.request_a, -20, address="1 B St, Waco, TX"
        )
        self._recap(old_event, quotes="OLD likes White Claw and Citrus Splash")
        beta_event = self._event(
            self.other, "Beta One", self.request_b, 1, address="1 C St, Waco, TX"
        )
        self._recap(beta_event, quotes="BETA SECRET prefers Corona")

        self._custom_template()
        # Texas: three approved recaps on three events.
        self._custom(self.tx1, sold={"Citrus Splash": 10, "Berry Blast": 2}, sampled=30)
        self._custom(self.tx2, sold={"Citrus Splash": 5, "Berry Blast": 6}, sampled=20)
        self._custom(self.tx3, sold={"Citrus Splash": 4}, sampled=10)
        # Not approved: must not turn Texas over to Berry Blast.
        self._custom(self.tx3, sold={"Berry Blast": 500}, sampled=900, approved=False)
        # California: one big event, which is not enough to name a leader.
        self._custom(self.ca1, sold={"Berry Blast": 50}, sampled=80)

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

    def _event(self, tenant, name, request, day_offset, *, state=None, address="1 Main St"):
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
        event_models.Event.objects.filter(id=event.id).update(
            request=request, state=state, address=address
        )
        event.refresh_from_db()
        return event

    def _recap(self, event, *, approved=True, **feedback):
        recap = recap_models.Recap.objects.create(
            name=f"recap {event.name}",
            event=event,
            approved=approved,
            submited_at=timezone.now(),
            ambassador=self.ba,
            created_by=self.system_user,
            updated_by=self.system_user,
        )
        recap_models.ConsumerFeedback.objects.create(recap=recap, **feedback)
        return recap

    def _custom_template(self):
        event_type = event_models.EventType.objects.create(
            name="Retail Sampling",
            slug="insight-retail",
            tenant=self.tenant,
            created_by=self.system_user,
        )
        self.template = recap_models.CustomRecapTemplate.objects.create(
            name="Insight Retail",
            event_type=event_type,
            tenant=self.tenant,
            created_by=self.system_user,
        )
        number = recap_models.CustomRecapFieldType.objects.create(
            name="Number", created_by=self.system_user
        )
        section = recap_models.RecapSection.objects.create(
            name="Sales", tenant=self.tenant, created_by=self.system_user
        )
        self.fields = {
            name: recap_models.CustomField.objects.create(
                name=name,
                custom_recap_template=self.template,
                custom_field_type=number,
                recap_section=section,
                created_by=self.system_user,
            )
            for name in (
                "Units sold: Citrus Splash",
                "Units sold: berry blast",
                "Total units sold today",
            )
        }

    def _custom(self, event, *, sold, sampled, approved=True):
        recap = recap_models.CustomRecap.objects.create(
            name=f"custom {event.name}",
            approved=approved,
            submitted_at=event.date,
            event=event,
            tenant=self.tenant,
            custom_recap_template=self.template,
            ambassador=self.ba,
            created_by=self.system_user,
        )
        values = {
            "Units sold: Citrus Splash": sold.get("Citrus Splash"),
            "Units sold: berry blast": sold.get("Berry Blast"),
            # A rolled-up total is never a product.
            "Total units sold today": sum(sold.values()),
        }
        for name, value in values.items():
            if value is not None:
                recap_models.CustomFieldValue.objects.create(
                    custom_recap=recap,
                    custom_field=self.fields[name],
                    value=str(value),
                    created_by=self.system_user,
                )
        recap_models.CustomRecapProductSample.objects.create(
            custom_recap=recap,
            product=self.berry,
            quantity=sampled,
            created_by=self.system_user,
        )
        return recap

    # ── calls ─────────────────────────────────────────────────────────

    async def _run(self, document, user, tenant=None):
        result = await self._execute_query_authenticated(
            document,
            {
                "tenantId": str((tenant or self.tenant).id),
                "startDate": self.start,
                "endDate": self.end,
            },
            user,
            self.endpoint_path,
        )
        assert result.errors is None, result.errors
        return next(iter(result.data.values()))

    def _model_tags(self):
        self.post.side_effect = _fake_model

    async def _rows(self):
        from tenants.models import TenantCommentInsight

        return await sync_to_async(
            lambda: list(TenantCommentInsight.objects.values_list("tenant_id", "kind"))
        )()

    # ── other brands people mentioned ─────────────────────────────────

    @pytest.mark.asyncio
    async def test_brand_user_prompt_is_approved_only_with_no_names(self):
        self._model_tags()
        payload = await self._run(MENTIONS, self.client_user)
        assert payload["status"] == "ready"
        assert self.post.call_count == 1
        # The sorting job runs on the small model.
        from utils import ai_text

        assert self.post.call_args.args[1]["model"] == ai_text._small_model()
        system = self.post.call_args.args[1]["messages"][0]["content"]
        prompt = self.post.call_args.args[1]["messages"][1]["content"]
        for kept in (
            "Tastes better than White Claw",
            "[decline] Cheaper to get Truly",
            "the brand ambassador said Citrus Splash was the favorite",
        ):
            assert kept in prompt, kept
        for dropped in ("PENDING", "ARCHIVED", "DELETED", "OLD", "BETA", "Rivera"):
            assert dropped not in prompt, dropped
            assert dropped not in system, dropped
        # The model is told which names are the tenant's own.
        assert "Alpha Brand" in system
        assert "Citrus Splash" in system and "Berry Blast" in system

    @pytest.mark.asyncio
    async def test_counts_are_python_and_an_unwritten_brand_is_dropped(self):
        self._model_tags()
        payload = await self._run(MENTIONS, self.client_user)
        brands = {b["brand"]: b for b in payload["brands"]}
        # Topo Chico is in no approved comment; Citrus Splash is the
        # tenant's own product; comment 999 does not exist.
        assert set(brands) == {"White Claw", "truly"}
        claw = brands["White Claw"]
        # Three recaps: "White Claw", "whiteclaw" and "White Claw's".
        assert claw["recapCount"] == 3
        assert (claw["preferOurs"], claw["preferTheirs"], claw["neutral"]) == (1, 1, 1)
        assert brands["truly"]["recapCount"] == 1
        assert brands["truly"]["preferTheirs"] == 1
        assert [b["brand"] for b in payload["brands"]][0] == "White Claw"
        assert payload["mentionCommentCount"] == 4
        # Every quote is a stored comment, with no ambassador in it.
        prompt = self.post.call_args.args[1]["messages"][1]["content"]
        for brand in payload["brands"]:
            assert brand["quote"] in prompt
            assert "Rivera" not in brand["quote"]

    @pytest.mark.asyncio
    async def test_brand_user_asking_for_another_brand_gets_their_own(self):
        self._model_tags()
        payload = await self._run(MENTIONS, self.client_user, self.other)
        assert payload["status"] == "ready"
        prompt = self.post.call_args.args[1]["messages"][1]["content"]
        assert "Tastes better than White Claw" in prompt
        assert "BETA SECRET" not in prompt
        assert {tenant for tenant, _kind in await self._rows()} == {self.tenant.id}

    @pytest.mark.asyncio
    async def test_staff_may_choose_and_too_few_comments_asks_nothing(self):
        self._model_tags()
        # Beta has one comment: the model is not asked.
        payload = await self._run(MENTIONS, self.admin, self.other)
        assert payload["status"] == "too_few"
        assert payload["brands"] == []
        assert self.post.call_count == 0
        payload = await self._run(MENTIONS, self.admin)
        assert payload["status"] == "ready"
        prompt = self.post.call_args.args[1]["messages"][1]["content"]
        assert "PENDING" not in prompt and "Rivera" not in prompt

    @pytest.mark.asyncio
    async def test_under_three_mentions_is_too_few_and_is_still_stored(self):
        def _one(url, payload, headers, timeout):
            (number,) = [
                n
                for n, text in _numbered(payload["messages"][1]["content"])
                if "better than white claw" in text
            ]
            mention = {"comment": number, "brand": "White Claw", "stance": "neutral"}
            return _reply(json.dumps({"mentions": [mention]}))

        self.post.side_effect = _one
        payload = await self._run(MENTIONS, self.client_user)
        assert payload["status"] == "too_few"
        assert payload["brands"] == []
        assert payload["mentionCommentCount"] == 1
        await self._run(MENTIONS, self.client_user)
        assert self.post.call_count == 1

    @pytest.mark.asyncio
    async def test_a_second_load_reads_the_stored_row_until_a_comment_is_approved(self):
        self._model_tags()
        first = await self._run(MENTIONS, self.client_user)
        again = await self._run(MENTIONS, self.admin)
        assert self.post.call_count == 1
        assert again == first

        await sync_to_async(
            lambda: recap_models.Recap.objects.filter(
                consumer_feedback__quotes__startswith="PENDING"
            ).update(approved=True)
        )()
        await self._run(MENTIONS, self.client_user)
        assert self.post.call_count == 2

    @pytest.mark.asyncio
    async def test_model_unavailable_says_so_and_stores_nothing(self):
        for user in (self.client_user, self.admin):
            payload = await self._run(MENTIONS, user)
            assert payload["status"] == "unavailable"
            assert payload["brands"] == []
        assert await self._rows() == []
        # A reply that is not the shape asked for is not stored either.
        self.post.side_effect = lambda *a, **k: _reply("not json")
        payload = await self._run(MENTIONS, self.client_user)
        assert payload["status"] == "unavailable"
        assert await self._rows() == []

    # ── what sells where ──────────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_regions_numbers_come_from_approved_recaps_per_state(self):
        self._model_tags()
        payload = await self._run(REGIONS, self.client_user)
        assert payload["status"] == "ready"
        assert payload["hasSold"] is True and payload["hasSampled"] is True
        assert payload["minStateRecaps"] == 3
        states = {s["state"]: s for s in payload["states"]}
        assert set(states) == {"TX", "CA"}

        texas = states["TX"]
        # 10 + 5 + 4 on three recaps; the pending recap's 500 is not counted
        # and the "Total units sold" roll-up is not a product.
        assert texas["soldStatus"] == "ready"
        assert texas["soldProduct"] == "Citrus Splash"
        assert texas["soldUnits"] == 19
        assert texas["soldRecaps"] == 3
        # Handed out is its own column: Berry Blast, 30 + 20 + 10.
        assert texas["sampledProduct"] == "Berry Blast"
        assert texas["sampledUnits"] == 60

        # One event cannot name a leader, however many units it moved.
        california = states["CA"]
        assert california["soldStatus"] == "too_few"
        assert california["soldProduct"] is None
        assert california["soldUnits"] == 0
        assert california["soldRecaps"] == 1
        assert california["sampledStatus"] == "too_few"
        assert california["spokenStatus"] == "too_few"
        assert california["spokenProduct"] is None

    @pytest.mark.asyncio
    async def test_regions_comments_are_counted_here_and_only_naming_comments_are_sent(self):
        self._model_tags()
        payload = await self._run(REGIONS, self.client_user)
        assert payload["commentsStatus"] == "ready"
        prompts = _calls(self.post, "products are")
        assert len(prompts) == 1
        prompt = prompts[0]
        assert "Loved Citrus Splash" in prompt
        assert "Berry Blast was too sweet" in prompt
        # Comments that name no product are not sent at all.
        assert "Tastes better than White Claw" not in prompt
        assert "Truly" not in prompt
        for dropped in ("PENDING", "ARCHIVED", "DELETED", "OLD", "BETA", "Rivera"):
            assert dropped not in prompt, dropped

        texas = {s["state"]: s for s in payload["states"]}["TX"]
        assert texas["spokenStatus"] == "ready"
        assert texas["spokenProduct"] == "Citrus Splash"
        # Three Texas recaps spoke well of it. The model also tagged the
        # first comment as Berry Blast, which that comment never names.
        assert texas["spokenPositive"] == 3
        assert texas["spokenNegative"] == 0
        assert texas["spokenRecaps"] == 3

    @pytest.mark.asyncio
    async def test_regions_scope_cache_and_staff(self):
        self._model_tags()
        mine = await self._run(REGIONS, self.client_user, self.other)
        assert {s["state"] for s in mine["states"]} == {"TX", "CA"}
        again = await self._run(REGIONS, self.admin)
        assert again == mine
        assert self.post.call_count == 1
        # Beta has no products: nothing to show and the model is not asked.
        beta = await self._run(REGIONS, self.admin, self.other)
        assert beta["status"] == "too_few"
        assert beta["commentsStatus"] == "none"
        assert beta["states"] == []
        assert self.post.call_count == 1

    @pytest.mark.asyncio
    async def test_regions_without_the_model_keep_the_numbers_and_store_nothing(self):
        payload = await self._run(REGIONS, self.client_user)
        assert payload["status"] == "ready"
        assert payload["commentsStatus"] == "unavailable"
        texas = {s["state"]: s for s in payload["states"]}["TX"]
        assert texas["soldProduct"] == "Citrus Splash"
        assert texas["spokenStatus"] == "too_few"
        assert await self._rows() == []


# ── the counting, without a database ──────────────────────────────────


def _c(recap, text):
    return Comment(recap=recap, kind=KIND_GENERAL, text=text)


def test_normalise_and_written_in():
    assert normalise("Dr. Pepper’s") == "dr pepper"
    assert normalise("  WHITE-claw ") == "white claw"
    assert written_in(normalise("I like whiteclaw more"), normalise("White Claw"))
    assert written_in(normalise("La Croix's is fine"), normalise("la croix"))
    assert not written_in(normalise("that claw machine"), normalise("White Claw"))
    assert not written_in(normalise("tastes like a seltzer"), normalise("Truly"))


def test_own_names_are_not_other_brands():
    products = ["Citrus Splash", "Berry Blast 12oz"]
    assert is_own_name("alpha", "Alpha Brand", products)
    assert is_own_name("alpha brand seltzer", "Alpha Brand", products)
    assert is_own_name("berry blast", "Alpha Brand", products)
    assert not is_own_name("white claw", "Alpha Brand", products)


def test_count_mentions_settles_each_recap_once_and_survives_junk():
    comments = [
        _c("L1", "Better than Truly"),
        _c("L1", "But Truly is cheaper so I buy Truly"),
        _c("L2", "Way better than Truly"),
        _c("L3", "no brands here"),
    ]
    raw = {
        "mentions": [
            {"comment": 1, "brand": "Truly", "stance": "prefers_ours"},
            {"comment": 2, "brand": "TRULY", "stance": "prefers_theirs"},
            {"comment": 2, "brand": "Truly", "stance": "prefers_theirs"},
            {"comment": 3, "brand": "Truly", "stance": "prefers_ours"},
            {"comment": 4, "brand": "Truly", "stance": "prefers_ours"},
            {"comment": True, "brand": "Truly", "stance": "prefers_ours"},
            {"comment": 3, "brand": "Truly", "stance": "adores"},
            {"comment": 3, "brand": "42", "stance": "neutral"},
            "junk",
        ]
    }
    out = count_mentions(raw, comments, "Alpha Brand", [])
    assert out["mention_comment_count"] == 3
    (truly,) = out["brands"]
    assert truly["brand"] == "Truly"
    # L1 pulled both ways, so it is neither; L2 preferred ours.
    assert (truly["recap_count"], truly["prefer_ours"], truly["neutral"]) == (2, 1, 1)
    assert truly["prefer_theirs"] == 0
    assert truly["quote"] in {c.text for c in comments}
    assert count_mentions(None, comments, "Alpha", []) == {
        "brands": [],
        "mention_comment_count": 0,
    }
    assert count_mentions({"mentions": "x"}, comments, "Alpha", [])["brands"] == []


def test_a_product_is_named_by_its_distinctive_words_only():
    assert names_product(normalise("the blue razz was great"), "Torch Blue Razz 10mg", "Torch")
    assert not names_product(normalise("love torch"), "Torch Blue Razz 10mg", "Torch")
    assert not names_product(normalise("want the 10mg pack"), "Torch Blue Razz 10mg Pack", "Torch")
    products = [(1, "Blue Razz"), (2, "Mango")]
    comments = [_c("C1", "blue razz was amazing"), _c("C2", "not sure")]
    raw = {
        "mentions": [
            {"comment": 1, "product": 1, "feeling": "positive"},
            {"comment": 1, "product": 1, "feeling": "negative"},
            {"comment": 1, "product": 2, "feeling": "positive"},
            {"comment": 2, "product": 1, "feeling": "positive"},
            {"comment": 1, "product": 9, "feeling": "positive"},
            {"comment": 1, "product": 1, "feeling": "great"},
        ]
    }
    assert keep_feelings(raw, comments, products, "Torch") == [["C1", "Blue Razz", "positive"]]


def test_summarize_regions_floors_and_ties():
    places = {
        f"C{n}": {"event": event, "state": state}
        for n, event, state in (
            (1, 1, "TX"), (2, 1, "TX"), (3, 1, "TX"),  # three recaps, one event
            (4, 2, "GA"), (5, 3, "GA"), (6, 4, "GA"),
            (7, 5, ""),
        )
    }  # fmt: skip

    def line(recap, product, units, measure="sold"):
        return {"recap": recap, "product": product, "measure": measure, "units": units}

    lines = [
        line("C1", "A", 90), line("C2", "A", 90), line("C3", "A", 90),
        line("C4", "A", 5), line("C5", "B", 5), line("C6", "A", 1), line("C6", "B", 1),
        line("C7", "A", 999),
    ]  # fmt: skip
    tags = [
        ["C4", "A", "positive"],
        ["C4", "A", "negative"],  # the same recap pulled both ways
        ["C5", "B", "positive"],
        ["C6", "B", "positive"],
        ["C6", "A", "negative"],
    ]
    rows = {row["state"]: row for row in summarize_regions(lines, tags, places)}
    # A recap with no state is nowhere.
    assert set(rows) == {"TX", "GA"}
    # Three recaps but one event: not enough.
    assert rows["TX"]["sold"]["status"] == LEAD_TOO_FEW
    assert rows["TX"]["sold"]["product"] is None
    # Level at six units each: enough data, no leader.
    assert rows["GA"]["sold"]["status"] == LEAD_NO_LEADER
    assert rows["GA"]["sold"]["recaps"] == 3
    assert rows["GA"]["sampled"]["status"] == LEAD_TOO_FEW
    spoken = rows["GA"]["spoken"]
    assert spoken["status"] == LEAD_READY
    assert (spoken["product"], spoken["positive"], spoken["negative"]) == ("B", 2, 0)
    assert spoken["recaps"] == 3

    # One recap speaking well of a product is not a leader.
    thin = summarize_regions([], [["C4", "A", "positive"], ["C5", "B", "neutral"],
                                  ["C6", "B", "neutral"]], places)  # fmt: skip
    assert thin[0]["spoken"]["status"] == LEAD_NO_LEADER


def test_plural_and_joined_spellings_are_one_brand():
    """"Red Bull", "Red Bulls" and "redbull" are one brand, not three."""
    from recaps.comment_insights import brand_key, normalise

    assert brand_key(normalise("Red Bulls")) == brand_key(normalise("Red Bull"))
    assert brand_key(normalise("redbull")) == brand_key(normalise("Red Bull"))
    assert brand_key(normalise("Monsters")) == brand_key(normalise("Monster"))
    # Names that end in s themselves are left alone.
    assert brand_key(normalise("Celsius")) != brand_key(normalise("Celsiu"))
    assert brand_key(normalise("Bliss")) != brand_key(normalise("Blis"))


def test_spellings_of_one_brand_are_counted_together_under_its_proper_name():
    comments = [
        Comment(recap="r1", kind="feedback", text="I dislike Red Bull but this is good."),
        Comment(recap="r2", kind="feedback", text="People came in for Monsters or Red Bulls."),
        Comment(recap="r3", kind="feedback", text="Better than redbull."),
    ]
    raw = {
        "mentions": [
            {"comment": 1, "brand": "Red Bull", "name": "Red Bull", "stance": "prefers_ours"},
            {"comment": 2, "brand": "Red Bulls", "name": "Red Bull", "stance": "prefers_theirs"},
            {"comment": 2, "brand": "Monsters", "name": "Monster", "stance": "prefers_theirs"},
            {"comment": 3, "brand": "redbull", "name": "Red Bull", "stance": "prefers_ours"},
        ]
    }
    out = count_mentions(raw, comments, "Alpha Brand", [])
    brands = {b["brand"]: b for b in out["brands"]}
    assert set(brands) == {"Red Bull", "Monster"}
    assert brands["Red Bull"]["recap_count"] == 3
    assert brands["Monster"]["recap_count"] == 1


def test_a_proper_name_that_is_a_different_brand_is_ignored():
    comments = [Comment(recap="r1", kind="feedback", text="Tastes like Monsters to me.")]
    raw = {"mentions": [
        {"comment": 1, "brand": "Monsters", "name": "Rockstar", "stance": "neutral"},
    ]}
    out = count_mentions(raw, comments, "Alpha Brand", [])
    assert [b["brand"] for b in out["brands"]] == ["Monsters"]
