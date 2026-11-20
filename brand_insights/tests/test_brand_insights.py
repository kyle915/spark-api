"""Store recommendations and alerts.

The schema tests run against the real ``schema_clients`` surface with the
web client's own queries (copied from spark-front-client). The OpenAI
transport (``utils.ai_text._post_json``) is replaced in every test, so
nothing reaches the network; the prompt it would have sent is checked for
ambassador names. What is asserted:

* findings are worked out in code, with minimum sample sizes, and visits
  without product are left out of every rate;
* a reworded sentence holding a number that is not in its facts is dropped
  and the sentence built in code is used;
* a brand user sees only published findings of a tenant they belong to,
  never another brand's, and cannot review or generate;
* staff publish with one click; nothing is sent to anyone.
"""

from __future__ import annotations

import json
import re
from datetime import date, datetime, time, timedelta
from unittest.mock import patch

import pytest
from asgiref.sync import sync_to_async
from django.core import mail
from django.test import Client, override_settings
from django.utils import timezone

from ambassadors.tests.base import AmbassadorsGraphQLTestCase
from brand_insights.detectors import alerts, recommendations
from brand_insights.facts import Visit, _answer_says_no_product, store_identity
from events import models as event_models
from recaps import models as recap_models
from utils.ai_text import AiUnavailable

# ── the web client's own documents ─────────────────────────────────────

CARDS = """
query TenantStoreInsightsQuery($tenantId: ID!) {
  tenantStoreInsights(tenantId: $tenantId) {
    recommendations { id kind code subject text periodStart periodEnd isDraft }
    alerts { id kind code subject text periodStart periodEnd isDraft }
  }
}
"""

REVIEW = """
query BrandInsightReviewQuery {
  brandInsightReview {
    tenantId
    tenantName
    drafts
    items {
      id kind code subject text template factsText source status
      periodStart periodEnd generatedAt reviewedByName reviewedAt
    }
  }
}
"""

GENERATE = """
mutation BrandInsightMutationsGenerateMutation($tenantId: ID!) {
  generateBrandInsights(tenantId: $tenantId) { ok reason count }
}
"""

PUBLISH = """
mutation BrandInsightMutationsPublishMutation($tenantId: ID!, $ids: [ID!]) {
  publishBrandInsights(tenantId: $tenantId, ids: $ids) { ok reason count }
}
"""

STATUS = """
mutation BrandInsightMutationsStatusMutation($id: ID!, $status: String!) {
  setBrandInsightStatus(id: $id, status: $status) {
    ok
    reason
    item {
      id kind code subject text template factsText source status
      periodStart periodEnd generatedAt reviewedByName reviewedAt
    }
  }
}
"""

CRON_SECRET = "test-cron-secret-value-only-for-tests"
CRON_PATH = "/internal/cron/generate-brand-insights"


def _reply(content: str) -> dict:
    return {"choices": [{"message": {"content": content}}]}


def _items_from_prompt(prompt: str) -> list[tuple[str, str]]:
    """``[(key, draft)]`` from an explain_text prompt."""
    keys = re.findall(r"^ITEM (.+)$", prompt, re.M)
    drafts = re.findall(r"^DRAFT: (.+)$", prompt, re.M)
    return list(zip(keys, drafts))


def _model_rewords(payload: dict) -> dict:
    """Rewords every item; a low-sales item comes back with a number the
    facts do not hold, which must be dropped."""
    prompt = payload["messages"][1]["content"]
    items = []
    for key, draft in _items_from_prompt(prompt):
        text = f"Reworded: {draft}"
        if key.startswith("low_sales"):
            text += " Sales could rise 35% with a better shelf."
        items.append({"key": key, "text": text})
    return _reply(json.dumps({"items": items}))


# ── pure: identity, flags, detectors ───────────────────────────────────


def test_store_identity_keeps_numbered_stores_apart_and_banners_out():
    assert store_identity("Kroger #412", "", "", "TX")[:2] == ("kroger#412", "Kroger #412")
    assert store_identity("HEB 614", "", "", "TX")[1] == "HEB #614"
    # A banner with a city is that banner in that city.
    assert store_identity("Kroger", "", "Austin", "TX")[1] == "Kroger (Austin, TX)"
    # A bare banner is a chain, never a store.
    key, label, chain, chain_label = store_identity("Kroger", "", "", "")
    assert (key, label, chain_label) == ("", "", "Kroger")
    # "Forever 21" keeps its 21.
    assert store_identity("Forever 21", "", "", "")[1] == ""
    assert store_identity("n/a", "", "", "")[2] == ""


@pytest.mark.parametrize(
    ("name", "value", "flag"),
    [
        ("Was product on shelf?", "No", True),
        ("Product in stock?", "no", True),
        ("Was product on shelf?", "Yes", False),
        ("Additional notes", "Store was out of stock, demo called off", True),
        ("Additional notes", "No product issues today", False),
        ("Notes", "Product not carried at this location", True),
        # Consumer comment fields are never read for this.
        ("List common questions asked by consumers", "Is it out of stock everywhere?", False),
        ("Consumer feedback", "They said no product like this exists", False),
        ("Total number of consumers sampled", "0", False),
    ],
)
def test_no_product_answers(name, value, flag):
    assert _answer_says_no_product(name, value) is flag


TODAY = date(2026, 11, 18)


def _v(store, day_ago, base, units, *, chain=None, no_product=False, price_tag=""):
    chain_key = (chain or store.split(" #")[0]).lower()
    return Visit(
        recap_kind="legacy",
        recap_id=hash((store, day_ago, base, units)) & 0xFFFF,
        day=TODAY - timedelta(days=day_ago),
        store_key=store.lower(),
        store_label=store,
        chain_key=chain_key,
        chain_label=chain or store.split(" #")[0],
        state="TX",
        consumers=base,
        base=base if not no_product else None,
        units=units if not no_product else None,
        no_product=no_product,
        price_tag=price_tag,
    )


def _program():
    """12 visits with product: average 132 / 790 = 16.7%."""
    visits = [_v("Kroger #412", d, 50, 20) for d in (5, 20, 40)]
    visits += [_v("Kroger #500", d, 150, 3, price_tag="unclear") for d in (8, 30)]
    visits += [_v(f"Target #{100 + i}", 10 + i, 50, 6) for i in range(6)]
    visits += [_v("Whole Foods #9", 12, 40, 30)]
    return visits


def test_top_store_needs_two_visits_and_beats_the_average():
    found = {f.code: f for f in recommendations(_program(), TODAY) if f.code == "top_store"}
    tops = [f for f in recommendations(_program(), TODAY) if f.code == "top_store"]
    assert [f.subject for f in tops] == ["Kroger #412"]  # Whole Foods had one visit
    top = found["top_store"]
    assert "40%" in top.template and "16.7%" in top.template and "2.4x" in top.template
    assert top.facts["visits"] == 3 and top.facts["sampled"] == 150


def test_no_brand_average_until_ten_visits():
    assert [f for f in recommendations(_program()[:9], TODAY) if f.code != "repeat_no_product"] == []


def test_low_sales_store_is_told_to_check_price_and_placement():
    low = [f for f in recommendations(_program(), TODAY) if f.code == "low_sales"]
    assert [f.subject for f in low] == ["Kroger #500"]
    assert "Check price and placement" in low[0].template
    assert "A price tag was seen on 0 of 2 shelf-checked visits." in low[0].template


def test_no_product_visits_stay_out_of_rates_and_repeat_is_a_fix():
    visits = _program() + [
        _v("Safeway #77", 3, 0, 0, no_product=True),
        _v("Safeway #77", 25, 0, 0, no_product=True),
        _v("Safeway #77", 60, 0, 0, no_product=True),  # outside 30 days
        # Product missing at the top store once does not drag its rate.
        _v("Kroger #412", 2, 0, 0, no_product=True),
    ]
    found = recommendations(visits, TODAY)
    top = next(f for f in found if f.code == "top_store")
    assert top.facts["conversion_percent"] == 40.0
    repeat = [f for f in found if f.code == "repeat_no_product"]
    assert [f.subject for f in repeat] == ["Safeway #77"]
    assert "2 visits in the last 30 days" in repeat[0].template


def test_alerts_product_missing_last_week_and_conversion_drop():
    week_start, week_end = date(2026, 11, 9), date(2026, 11, 15)

    def day_v(store, day, base, units, **kw):
        v = _v(store, 0, base, units, **kw)
        v.day = day
        return v

    visits = [day_v("Safeway #77", date(2026, 11, 10), 0, 0, no_product=True)]
    visits += [day_v("Kroger #1", date(2026, 11, 12), 0, 0, no_product=True)]
    # Before: 12 visits at 20%. Last four weeks: 12 visits at 5%.
    visits += [day_v(f"Kroger #{i}", date(2026, 9, 25) + timedelta(days=i), 100, 20) for i in range(12)]
    visits += [day_v(f"Kroger #{i}", date(2026, 10, 20) + timedelta(days=i), 100, 5) for i in range(12)]
    found = alerts(visits, week_start, week_end)
    codes = [f.code for f in found]
    missing = found[codes.index("no_product_week")]
    assert missing.template == "Product not found at 2 stores last week: Kroger #1; Safeway #77."
    drop = found[codes.index("conversion_drop")]
    assert "from 20% to 5%" in drop.template
    # One retailer: the brand's drop is not said twice.
    assert "chain_conversion_drop" not in codes


def test_small_or_mild_drops_are_not_alerts():
    week_end = date(2026, 11, 15)

    def day_v(day, base, units):
        v = _v("Kroger #1", 0, base, units)
        v.day = day
        return v

    mild = [day_v(date(2026, 9, 25) + timedelta(days=i), 100, 20) for i in range(12)]
    mild += [day_v(date(2026, 10, 20) + timedelta(days=i), 100, 16) for i in range(12)]
    assert alerts(mild, date(2026, 11, 9), week_end) == []
    few = [day_v(date(2026, 9, 25), 100, 20), day_v(date(2026, 10, 20), 100, 1)]
    assert alerts(few, date(2026, 11, 9), week_end) == []


# ── through the schema ─────────────────────────────────────────────────


@pytest.mark.django_db(transaction=True)
class TestBrandInsightsSchema(AmbassadorsGraphQLTestCase):
    @pytest.fixture(autouse=True)
    def setup(self, db, settings):
        from config.schema_client import schema_clients

        settings.OPENAI_API_KEY = "test-key-not-real"
        settings.OPENAI_MODEL = ""
        # Review-flow tests start from drafts; the auto-publish tests turn
        # it on explicitly.
        settings.BRAND_INSIGHTS_AUTO_PUBLISH_RECOMMENDATIONS = False

        self.roles = self.setup_default_roles()
        self.schema = schema_clients
        self.endpoint_path = "/api/v1/graphql/clients"
        self.system_user = self.get_system_user()

        from brand_insights.service import ops_today

        self.today = ops_today()

        self.tenant = self.create_tenant(name="Alpha Brand")
        self.other = self.create_tenant(name="Beta Brand")

        self.admin = self.create_user(
            username="insights-admin",
            email="insights-admin@test.com",
            role=self.roles["spark_admin"],
        )
        self.client_user = self.create_user(
            username="insights-client",
            email="insights-client@test.com",
            role=self.roles["client"],
        )
        self.create_tenanted_user(self.client_user, self.tenant)
        self.beta_user = self.create_user(
            username="insights-beta",
            email="insights-beta@test.com",
            role=self.roles["client"],
        )
        self.create_tenanted_user(self.beta_user, self.other)

        ba = self.create_user(
            username="jordan-rivera-insights",
            email="jordan.rivera.insights@ba.test",
            role=self.roles["ambassador"],
        )
        ba.first_name, ba.last_name = "Jordan", "Rivera"
        ba.save()
        self.ba = self.create_ambassador(ba)

        self.request = self._request(self.tenant, "Alpha Retail Tour")
        # 12 visits with product, average 16.7%.
        for ago in (5, 20, 40):
            self._legacy(self.tenant, "Kroger #412", ago, 50, 20)
        for ago in (8, 30):
            self._legacy(self.tenant, "Kroger #500", ago, 150, 3)
        for i in range(6):
            self._legacy(self.tenant, f"Target #{100 + i}", 10 + i, 50, 6)
        self._legacy(self.tenant, "Whole Foods #9", 12, 40, 30)
        # No product at Safeway #77 twice: once last week. Its retailer and
        # store number are on the recap itself (the location normalizer's
        # fields), not on the event.
        last_monday = self.today - timedelta(days=self.today.weekday() + 7)
        self.safeway = event_models.Retailer.objects.create(
            name="Safeway", tenant=self.tenant, created_by=self.system_user
        )
        self._no_product(last_monday + timedelta(days=2))
        self._no_product(self.today - timedelta(days=20))
        # Not approved: never counts.
        self._legacy(self.tenant, "Kroger #999", 6, 100, 90, approved=False)

        self.beta_request = self._request(self.other, "Beta Retail Tour")
        for i in range(12):
            self._legacy(self.other, "Betamart #1" if i < 3 else f"Beta Shop #{i}", 4 + i, 50, 25 if i < 3 else 5)

        patcher = patch("utils.ai_text._post_json", side_effect=AiUnavailable("blocked in tests"))
        self.post = patcher.start()
        yield
        patcher.stop()

    # ── fixtures ──────────────────────────────────────────────────────

    def _request(self, tenant, name):
        return event_models.Request.objects.create(
            name=name,
            address="1 Main St",
            tenant=tenant,
            status=self.create_request_status(name="Approved", tenant=tenant, slug="approved"),
            request_type=self.create_request_type(name="Retail", tenant=tenant),
            created_by=self.system_user,
        )

    def _event(self, tenant, day, retailer=None):
        when = timezone.make_aware(datetime.combine(day, time(15)))
        event = self.create_event(
            name=f"Demo {day}", tenant=tenant, date=when, start_time=when,
            end_time=when + timedelta(hours=4),
        )
        request = self.request if tenant == self.tenant else self.beta_request
        event_models.Event.objects.filter(id=event.id).update(request=request, retailer=retailer)
        event.refresh_from_db()
        return event

    def _legacy(self, tenant, store, days_ago, consumers, sold, approved=True):
        retailer = event_models.Retailer.objects.create(
            name=store, tenant=tenant, created_by=self.system_user
        )
        event = self._event(tenant, self.today - timedelta(days=days_ago), retailer)
        recap = recap_models.Recap.objects.create(
            name=f"recap {store}",
            event=event,
            approved=approved,
            submited_at=timezone.now(),
            products_sold=sold,
            ambassador=self.ba,
            created_by=self.system_user,
            updated_by=self.system_user,
        )
        recap_models.ConsumerEngagements.objects.create(
            recap=recap, total_consumer=consumers,
            created_by=self.system_user, updated_by=self.system_user,
        )
        return recap

    def _no_product(self, day):
        if not hasattr(self, "template"):
            event_type = event_models.EventType.objects.create(
                name="Retail Sampling", slug="insights-retail", tenant=self.tenant,
                created_by=self.system_user,
            )
            self.template = recap_models.CustomRecapTemplate.objects.create(
                name="Retail", event_type=event_type, tenant=self.tenant,
                created_by=self.system_user,
            )
            text = recap_models.CustomRecapFieldType.objects.create(
                name="Short text", created_by=self.system_user
            )
            section = recap_models.RecapSection.objects.create(
                name="Visit", tenant=self.tenant, created_by=self.system_user
            )
            self.fields = {
                name: recap_models.CustomField.objects.create(
                    name=name, custom_recap_template=self.template,
                    custom_field_type=text, recap_section=section,
                    created_by=self.system_user,
                )
                for name in ("Was product on shelf?", "Additional notes", "BA Name")
            }
        event = self._event(self.tenant, day)
        recap = recap_models.CustomRecap.objects.create(
            name="no product", approved=True, submitted_at=event.date, event=event,
            tenant=self.tenant, custom_recap_template=self.template,
            ambassador=self.ba, retailer=self.safeway, store_number="77",
            created_by=self.system_user,
        )
        for name, value in (
            ("Was product on shelf?", "No"),
            ("Additional notes", "Jordan Rivera says the manager pulled it"),
            ("BA Name", "Jordan Rivera"),
        ):
            recap_models.CustomFieldValue.objects.create(
                custom_recap=recap, custom_field=self.fields[name], value=value,
                created_by=self.system_user,
            )

    # ── calls ─────────────────────────────────────────────────────────

    async def _run(self, document, variables, user):
        result = await self._execute_query_authenticated(
            document, variables, user, self.endpoint_path
        )
        assert result.errors is None, result.errors
        return result.data

    async def _cards(self, user, tenant):
        data = await self._run(CARDS, {"tenantId": str(tenant.id)}, user)
        return data["tenantStoreInsights"]

    async def _generate(self, tenant=None):
        data = await self._run(GENERATE, {"tenantId": str((tenant or self.tenant).id)}, self.admin)
        return data["generateBrandInsights"]

    async def _rows(self):
        from brand_insights.models import BrandInsight

        return await sync_to_async(lambda: list(BrandInsight.objects.order_by("id")))()

    # ── tests ─────────────────────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_staff_generate_drafts_that_the_brand_does_not_see(self):
        self.post.side_effect = lambda url, payload, headers, timeout: _model_rewords(payload)
        result = await self._generate()
        assert result["ok"] and result["count"] >= 4

        rows = await self._rows()
        by_code = {}
        for r in rows:
            by_code.setdefault(r.code, []).append(r)
        assert {r.subject for r in by_code["top_store"]} == {"Kroger #412"}
        assert {r.subject for r in by_code["low_sales"]} == {"Kroger #500"}
        assert {r.subject for r in by_code["repeat_no_product"]} == {"Safeway #77"}
        assert by_code["no_product_week"][0].body.endswith("Safeway #77.")
        assert all(r.status == "draft" for r in rows)
        assert all(r.tenant_id == self.tenant.id for r in rows)

        # The grounded rewording is kept; the one with an invented number is
        # dropped for the sentence built in code.
        top = by_code["top_store"][0]
        assert top.source == "model" and top.body.startswith("Reworded:")
        low = by_code["low_sales"][0]
        assert low.source == "template" and "35%" not in low.body
        assert low.body == low.template

        # The prompt names no ambassador and holds no recap free text.
        prompt = self.post.call_args.args[1]["messages"][1]["content"]
        assert "Jordan" not in prompt and "Rivera" not in prompt
        assert "manager pulled it" not in prompt
        assert "Beta" not in prompt

        cards = await self._cards(self.client_user, self.tenant)
        assert cards == {"recommendations": [], "alerts": []}
        staff_view = await self._cards(self.admin, self.tenant)
        assert staff_view["recommendations"] and all(r["isDraft"] for r in staff_view["recommendations"])
        assert len(mail.outbox) == 0

    @pytest.mark.asyncio
    async def test_one_click_publish_then_the_brand_sees_them(self):
        await self._generate()
        data = await self._run(PUBLISH, {"tenantId": str(self.tenant.id)}, self.admin)
        assert data["publishBrandInsights"]["ok"]
        cards = await self._cards(self.client_user, self.tenant)
        recs = cards["recommendations"]
        # Go back first, then what to fix.
        assert [r["code"] for r in recs] == ["top_store", "repeat_no_product", "low_sales"]
        assert not any(r["isDraft"] for r in recs)
        assert [a["code"] for a in cards["alerts"]] == ["no_product_week"]
        blob = json.dumps(cards)
        assert "Jordan" not in blob and "Rivera" not in blob
        # The model was unavailable: every sentence is the one built in code.
        rows = await self._rows()
        assert all(r.source == "template" and r.body == r.template for r in rows)
        assert {r.reviewed_by_id for r in rows} == {self.admin.id}

    @pytest.mark.asyncio
    async def test_hidden_findings_stay_hidden_and_a_rerun_keeps_reviewed_ones(self):
        await self._generate()
        rows = await self._rows()
        low = next(r for r in rows if r.code == "low_sales")
        top = next(r for r in rows if r.code == "top_store")
        data = await self._run(STATUS, {"id": f"BrandInsight:{low.id}", "status": "hidden"}, self.admin)
        assert data["setBrandInsightStatus"]["item"]["status"] == "hidden"
        await self._run(STATUS, {"id": f"BrandInsight:{top.id}", "status": "published"}, self.admin)

        # A new run: the model now answers, but reviewed rows are not touched.
        self.post.side_effect = lambda url, payload, headers, timeout: _model_rewords(payload)
        await self._generate()
        after = {r.id: r for r in await self._rows()}
        assert after[low.id].status == "hidden"
        assert after[top.id].status == "published" and after[top.id].body == top.body

        cards = await self._cards(self.client_user, self.tenant)
        assert [r["code"] for r in cards["recommendations"]] == ["top_store"]

    @pytest.mark.asyncio
    async def test_a_brand_user_only_ever_reads_their_own_brand(self):
        await self._generate()
        await self._generate(self.other)
        await self._run(PUBLISH, {"tenantId": str(self.other.id)}, self.admin)
        await self._run(PUBLISH, {"tenantId": str(self.tenant.id)}, self.admin)

        beta_rows = [r for r in await self._rows() if r.tenant_id == self.other.id]
        assert any(r.subject == "Betamart #1" for r in beta_rows)

        # Alpha's user asking for Beta gets Alpha's own findings.
        cards = await self._cards(self.client_user, self.other)
        blob = json.dumps(cards)
        assert "Beta" not in blob
        assert any(r["subject"] == "Kroger #412" for r in cards["recommendations"])
        beta = await self._cards(self.beta_user, self.other)
        assert "Kroger" not in json.dumps(beta)
        assert any(r["subject"] == "Betamart #1" for r in beta["recommendations"])

    @pytest.mark.asyncio
    async def test_brand_users_cannot_review_publish_hide_or_generate(self):
        await self._generate()
        rows = await self._rows()
        data = await self._run(REVIEW, {}, self.client_user)
        assert data["brandInsightReview"] is None
        data = await self._run(GENERATE, {"tenantId": str(self.tenant.id)}, self.client_user)
        assert data["generateBrandInsights"]["ok"] is False
        data = await self._run(PUBLISH, {"tenantId": str(self.tenant.id)}, self.client_user)
        assert data["publishBrandInsights"]["ok"] is False
        data = await self._run(
            STATUS, {"id": f"BrandInsight:{rows[0].id}", "status": "published"}, self.client_user
        )
        assert data["setBrandInsightStatus"]["ok"] is False
        assert all(r.status == "draft" for r in await self._rows())
        cards = await self._cards(self.client_user, self.tenant)
        assert cards == {"recommendations": [], "alerts": []}

    @pytest.mark.asyncio
    async def test_staff_review_lists_each_brand_with_its_drafts_first(self):
        await self._generate()
        await self._generate(self.other)
        await self._run(PUBLISH, {"tenantId": str(self.other.id)}, self.admin)
        data = await self._run(REVIEW, {}, self.admin)
        brands = data["brandInsightReview"]
        assert [b["tenantName"] for b in brands] == ["Alpha Brand", "Beta Brand"]
        alpha = brands[0]
        assert alpha["drafts"] == len(alpha["items"]) > 0
        top = next(i for i in alpha["items"] if i["code"] == "top_store")
        assert "Its conversion rate: 40%" in top["factsText"]
        assert brands[1]["drafts"] == 0
        assert all(i["reviewedByName"] for i in brands[1]["items"])

    @pytest.mark.asyncio
    async def test_a_finding_that_goes_away_goes_stale_unseen(self):
        await self._generate()
        # The Safeway recaps are archived: the repeat finding is gone.
        await sync_to_async(
            lambda: recap_models.CustomRecap.objects.filter(tenant=self.tenant).update(
                archived_at=timezone.now()
            )
        )()
        await self._generate()
        rows = {r.code: r for r in await self._rows()}
        assert rows["repeat_no_product"].status == "stale"
        assert rows["no_product_week"].status == "stale"
        data = await self._run(REVIEW, {}, self.admin)
        codes = {i["code"] for i in data["brandInsightReview"][0]["items"]}
        assert "repeat_no_product" not in codes

    def test_recommendations_publish_on_their_own_by_default(self):
        from django.conf import settings as live

        from brand_insights import service

        del live.BRAND_INSIGHTS_AUTO_PUBLISH_RECOMMENDATIONS
        assert service.auto_publish_recommendations() is True

    @pytest.mark.asyncio
    async def test_recommendations_can_publish_on_their_own_when_turned_on(self):
        with override_settings(BRAND_INSIGHTS_AUTO_PUBLISH_RECOMMENDATIONS=True):
            await self._generate()
        rows = await self._rows()
        assert {r.status for r in rows if r.kind == "recommendation"} == {"published"}
        assert {r.status for r in rows if r.kind == "alert"} == {"draft"}

    @pytest.mark.asyncio
    async def test_existing_recommendation_drafts_publish_once_turned_on(self):
        with override_settings(BRAND_INSIGHTS_AUTO_PUBLISH_RECOMMENDATIONS=False):
            await self._generate()
        rows = await self._rows()
        assert {r.status for r in rows if r.kind == "recommendation"} == {"draft"}
        before = {r.id: r.body for r in rows if r.kind == "recommendation"}
        with override_settings(BRAND_INSIGHTS_AUTO_PUBLISH_RECOMMENDATIONS=True):
            await self._generate()
        rows = await self._rows()
        recs = [r for r in rows if r.kind == "recommendation"]
        assert {r.status for r in recs} == {"published"}
        assert {r.id: r.body for r in recs} == before
        assert {r.status for r in rows if r.kind == "alert"} == {"draft"}

    # ── cron ──────────────────────────────────────────────────────────

    def _cron(self, query=""):
        return Client().post(
            f"{CRON_PATH}{query}", HTTP_X_CRON_SECRET=CRON_SECRET
        )

    @pytest.mark.asyncio
    async def test_cron_needs_the_secret_and_dry_runs_by_default(self):
        with override_settings(INTERNAL_CRON_SECRET=CRON_SECRET):
            denied = await sync_to_async(lambda: Client().post(CRON_PATH))()
            assert denied.status_code in (401, 403)
            response = await sync_to_async(self._cron)()
        assert response.status_code == 200
        body = response.json()
        assert body["dry_run"] is True and body["emails_sent"] == 0
        names = {t["tenant_name"]: t for t in body["tenants"]}
        assert "top_store" in names["Alpha Brand"]["codes"]
        assert await self._rows() == []
        assert self.post.call_count == 0

    @pytest.mark.asyncio
    async def test_cron_writes_drafts_and_sends_nothing(self):
        with override_settings(INTERNAL_CRON_SECRET=CRON_SECRET):
            response = await sync_to_async(self._cron)("?dry_run=false")
            again = await sync_to_async(self._cron)("?dry_run=false")
        body = response.json()
        assert body["ok"] and body["written"] > 0 and body["emails_sent"] == 0
        # Same numbers the second time: nothing rewritten.
        assert again.json()["written"] == 0
        rows = await self._rows()
        assert {r.tenant_id for r in rows} == {self.tenant.id, self.other.id}
        assert all(r.status == "draft" for r in rows)
        assert len(mail.outbox) == 0
