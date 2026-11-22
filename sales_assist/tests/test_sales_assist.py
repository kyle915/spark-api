"""Sales assist: renewal notes for clients, anonymized proposals for new leads.

Nothing here reaches the network. Close is the real ``CloseClient`` over a
fake HTTP session (so the exact payloads are asserted), the model is
``utils.ai_text._post_json`` patched, and brand websites are served by an
``httpx.MockTransport``. What is asserted:

* every number in a draft is in its facts, or the text is dropped;
* a prospect draft never names another client and never carries an exact
  figure tied to one — and the model is never even shown them;
* Close only ever gets a note and an email with status "draft" (the client
  refuses anything else), and only when apply + SALES_ASSIST_ENABLED + key;
* a second run makes no duplicate drafts;
* with no CLOSE_API_KEY everything no-ops cleanly.
"""

from __future__ import annotations

import json
import re
from datetime import date, datetime
from unittest.mock import patch

import httpx
import pytest
from django.test import Client
from django.utils import timezone

from ambassadors.tests.base import AmbassadorsGraphQLTestCase
from events import models as event_models
from product_knowledge.fetch import Fetcher
from recaps import models as recap_models
from sales_assist import close_client, comparables, guards, runner
from sales_assist.models import (
    CloseLink,
    SalesAssistCursor,
    SalesAssistDraft,
    SalesProfile,
)

TODAY = date(2026, 11, 18)
Q3 = (date(2026, 7, 1), date(2026, 9, 30))


# ── fakes ───────────────────────────────────────────────────────────────


class FakeResponse:
    def __init__(self, status=200, body=None):
        self.status_code = status
        self._body = body if body is not None else {}
        self.content = json.dumps(self._body).encode()
        self.headers = {}

    def json(self):
        return self._body


class FakeCloseSession:
    """Stands in for requests.Session behind the real CloseClient."""

    def __init__(self, leads=None, notes=None):
        self.auth = None
        self.leads = {lead["id"]: lead for lead in (leads or [])}
        self.notes = notes or {}
        self.calls: list[tuple[str, str, dict | None]] = []

    def request(self, method, url, params=None, json=None, timeout=None):
        path = url.replace(close_client.CLOSE_API_BASE, "")
        self.calls.append((method, path, json))
        if method == "GET" and path.startswith("/lead/") and path != "/lead/":
            lead_id = path.split("/")[2]
            return FakeResponse(200, self.leads.get(lead_id, {}))
        if method == "GET" and path == "/lead/":
            return FakeResponse(200, {"data": list(self.leads.values())})
        if method == "GET" and path == "/activity/note/":
            return FakeResponse(200, {"data": self.notes.get(params.get("lead_id"), [])})
        if method == "GET" and path == "/opportunity/":
            return FakeResponse(200, {"data": []})
        if method == "POST" and path == "/lead/":
            lead_id = f"lead_new{len(self.leads) + 1}"
            lead = {
                "id": lead_id,
                "name": (json or {}).get("name"),
                "display_name": (json or {}).get("name"),
                "description": (json or {}).get("description", ""),
                "contacts": (json or {}).get("contacts") or [],
            }
            self.leads[lead_id] = lead
            return FakeResponse(200, lead)
        if method == "POST" and path == "/activity/note/":
            self.notes.setdefault(json["lead_id"], []).append({"id": "acti_n", "note": json["note"]})
            return FakeResponse(200, {"id": f"acti_note{len(self.writes)}"})
        if method == "POST" and path == "/activity/email/":
            return FakeResponse(200, {"id": f"acti_email{len(self.writes)}", "status": json["status"]})
        return FakeResponse(404, {})

    @property
    def writes(self):
        return [c for c in self.calls if c[0] != "GET"]


def _ai(content):
    return {"choices": [{"message": {"content": content if isinstance(content, str) else json.dumps(content)}}]}


def _facts_from(payload) -> dict:
    user = payload["messages"][1]["content"]
    return json.loads(user.split("FACTS:\n", 1)[1])


def site_fetcher(pages: dict[str, str]) -> Fetcher:
    def handler(request: httpx.Request):
        path = request.url.path or "/"
        if path in pages:
            ctype = "text/plain" if path.endswith(".txt") else "text/html"
            return httpx.Response(200, text=pages[path], headers={"content-type": ctype})
        return httpx.Response(404, text="nope")

    return Fetcher(transport=httpx.MockTransport(handler), resolve_dns=False)


SITE = {
    "/robots.txt": "User-agent: *\nAllow: /\n",
    "/": "<html><head><title>Zap Fizz</title></head><body><h1>Zap Fizz sparkling energy</h1>"
         "<p>Clean sparkling energy drinks.</p><a href='/about'>About</a><a href='/where-to-buy'>Find us</a></body></html>",
    "/about": "<html><body><p>Founded by two friends. Now at Kroger and Sprouts.</p></body></html>",
    "/where-to-buy": "<html><body><p>Find Zap Fizz at Kroger, Sprouts and Whole Foods.</p></body></html>",
}

PROSPECT_LEAD = {
    "id": "lead_zap",
    "display_name": "Zap Fizz Beverages",
    "url": "https://zapfizz.test",
    "date_created": "2026-11-18T10:00:00+00:00",
    "status_id": "stat_x",
    "description": "",
    "contacts": [{"id": "cont_1", "name": "Sam Lee", "emails": [{"email": "sam@zapfizz.test"}]}],
    "custom": {},
}

CLIENT_LEAD = {
    "id": "lead_volt",
    "display_name": "Volt Energy Co",
    "date_created": "2025-01-01T00:00:00+00:00",
    "contacts": [{"id": "cont_v", "name": "Riley Park", "emails": [{"email": "riley@volt.test"}]}],
    "custom": {},
}


# ── pure guards ─────────────────────────────────────────────────────────


def test_numbers_must_be_in_the_facts():
    facts = {"consumers_sampled": 1445, "conversion": {"conversion_percent": 20.5}}
    assert guards.numbers_grounded("We sampled 1,445 people, 20.5% bought.", facts)[0]
    ok, missing = guards.numbers_grounded("We sampled 1,500 people.", facts)
    assert not ok and missing == ["1500"]


def test_rounded_figures_are_floored_with_a_plus():
    assert guards.rounded(1445) == "1,400+"
    assert guards.rounded(87) == "80+"
    assert guards.rounded(23812) == "23,000+"
    assert guards.rounded(12) == ""


def test_leaks_find_names_cores_and_exact_figures():
    names = ["Volt Energy Co", "Torch THC"]
    assert guards.leaks("Like Volt, you could ...", forbidden_names=names, forbidden_numbers=[]) == ["name:Volt Energy Co"]
    assert guards.leaks("torch brand", forbidden_names=names, forbidden_numbers=[]) == ["name:Torch THC"]
    assert guards.leaks("1,445 consumers", forbidden_names=[], forbidden_numbers=[1445]) == ["number:1445"]
    assert guards.leaks("1,400+ consumers", forbidden_names=names, forbidden_numbers=[1445]) == []
    # Everyday words in a client's name are not the name on their own.
    assert guards.leaks("fresh bread, feel free to call", forbidden_names=["Fresh Vintage Farms", "Feel Free"],
                        forbidden_numbers=[]) == []
    assert guards.leaks("Like Feel Free did", forbidden_names=["Feel Free"], forbidden_numbers=[]) == ["name:Feel Free"]
    assert guards.leaks("Fresh Vintage sampled", forbidden_names=["Fresh Vintage Farms"], forbidden_numbers=[])


def test_banned_content():
    assert guards.banned("Demos start at $250 per demo")
    assert guards.banned("our 42k ambassadors")
    assert guards.banned("Girl Beer loved it")
    assert not guards.banned("257,000+ brand ambassadors")


def test_chain_names_and_descriptor_safety():
    assert comparables.chain_of("Kroger #412 Ann Arbor") == "Kroger"
    assert comparables.chain_of("HEB Plus Austin") == "H-E-B"
    assert comparables.chain_of("Joe's Corner Market") == ""
    assert comparables.descriptor_is_safe("a national energy-drink brand", "Volt Energy Co", ["Volt Original"])
    assert not comparables.descriptor_is_safe("the Volt energy brand", "Volt Energy Co", [])


# ── Close client: drafts only ───────────────────────────────────────────


def test_close_client_refuses_anything_but_notes_drafts_and_lead_create():
    for method, path, payload in (
        ("PUT", "/activity/email/acti_1/", {"status": "outbox"}),
        ("DELETE", "/lead/lead_1/", None),
        ("POST", "/activity/email/", {"status": "outbox"}),
        ("POST", "/activity/email/", {"status": "draft", "date_scheduled": "2026-11-19"}),
        ("POST", "/activity/sms/", {"status": "draft"}),
        ("POST", "/lead/", {}),  # name required
        ("POST", "/lead/", {"name": "Acme", "opportunities": []}),
    ):
        with pytest.raises(close_client.UnsafeCloseWrite):
            close_client._assert_safe(method, path, payload)
    close_client._assert_safe("POST", "/activity/email/", {"status": "draft"})
    close_client._assert_safe("POST", "/activity/note/", {"note": "x"})
    close_client._assert_safe("POST", "/lead/", {"name": "Acme Spark Brand"})


def test_close_client_needs_a_key(settings):
    settings.CLOSE_API_KEY = ""
    with pytest.raises(close_client.CloseNotConfigured):
        close_client.CloseClient()
    session = FakeCloseSession()
    client = close_client.CloseClient("k_test", session=session)
    client.create_draft_email("lead_1", to=["a@b.test"], subject="s", body_text="b")
    method, path, payload = session.calls[-1]
    assert (method, path, payload["status"]) == ("POST", "/activity/email/", "draft")
    assert session.auth == ("k_test", "")


# ── with a database ─────────────────────────────────────────────────────


@pytest.mark.django_db(transaction=True)
class TestSalesAssist(AmbassadorsGraphQLTestCase):
    @pytest.fixture(autouse=True)
    def setup(self, db, settings):
        settings.OPENAI_API_KEY = "test-key"
        settings.CLOSE_API_KEY = ""
        settings.SALES_ASSIST_ENABLED = True
        settings.GS_BUCKET_NAME = "spark-test-bucket"
        self.settings = settings
        self.roles = self.setup_default_roles()
        self.sys = self.get_system_user()
        ba_user = self.create_user(username="ba-sa", email="ba-sa@ba.test", role=self.roles["ambassador"])
        self.ba = self.create_ambassador(ba_user)
        self.ca = event_models.State.objects.create(name="California", code="CA", created_by=self.sys)
        self.oh = event_models.State.objects.create(name="Ohio", code="OH", created_by=self.sys)

        # The existing client, used for its own renewal and (anonymized) as a comparable.
        self.volt = self.create_tenant(name="Volt Energy Co")
        self.volt_kit = self._kit(self.volt, "Kroger #412")
        for day, sampled, sold in ((6, "700", "140"), (13, "500", "100"), (20, "245", "49")):
            self._recap(self.volt, self.volt_kit, 8, day, sampled, sold,
                        feedback="Loved the citrus flavor, said it tasted clean and not too sweet.")
        SalesProfile.objects.create(tenant=self.volt, category="energy drink",
                                    descriptor="a national energy-drink brand")
        self.link = CloseLink.objects.create(tenant=self.volt, close_lead_id="lead_volt")

    # fixtures

    def _kit(self, tenant, store):
        et = event_models.EventType.objects.create(
            name="Retail Sampling", slug=f"sa-retail-{tenant.id}", tenant=tenant, created_by=self.sys
        )
        template = recap_models.CustomRecapTemplate.objects.create(
            name="SA Retail", event_type=et, tenant=tenant, created_by=self.sys
        )
        num = recap_models.CustomRecapFieldType.objects.create(name=f"Number{tenant.id}", created_by=self.sys)
        section = recap_models.RecapSection.objects.create(name="Sampling", tenant=tenant, created_by=self.sys)

        def field(name):
            return recap_models.CustomField.objects.create(
                name=name, custom_recap_template=template, custom_field_type=num,
                recap_section=section, created_by=self.sys,
            )

        retailer = event_models.Retailer.objects.create(name=store, tenant=tenant, created_by=self.sys)
        return {
            "et": et, "template": template, "retailer": retailer,
            "sampled": field("Total number of consumers sampled"),
            "sold": field("Cans sold"),
            "feedback": field("Consumer feedback"),
        }

    def _recap(self, tenant, kit, month, day, sampled, sold, feedback=""):
        when = timezone.make_aware(datetime(2026, month, day, 18))
        event = event_models.Event.objects.create(
            name=f"Sampling {month}/{day}", tenant=tenant, address="1 Main St", date=when,
            event_type=kit["et"], retailer=kit["retailer"], state=self.oh,
            created_by=self.sys, updated_by=self.sys,
        )
        recap = recap_models.CustomRecap.objects.create(
            name="r", approved=True, submitted_at=when, event=event, tenant=tenant,
            custom_recap_template=kit["template"], ambassador=self.ba, created_by=self.sys,
        )
        for key, value in (("sampled", sampled), ("sold", sold), ("feedback", feedback)):
            if value:
                recap_models.CustomFieldValue.objects.create(
                    custom_recap=recap, custom_field=kit[key], value=value, created_by=self.sys
                )
        return recap

    def _close(self, leads=(CLIENT_LEAD,), notes=None):
        session = FakeCloseSession(leads=list(leads), notes=notes)
        return close_client.CloseClient("k_test", session=session), session

    @staticmethod
    def _ai_router(renewal=None, research=None, profile=None, draft=None):
        sent: list[dict] = []

        def post(url, payload, headers, *, timeout):
            sent.append(payload)
            system = payload["messages"][0]["content"]
            if "this quarter" in system:
                return _ai(renewal(_facts_from(payload)) if renewal else "")
            if system.startswith("You research a company"):
                return _ai(research or {})
            if system.startswith("You classify a consumer brand"):
                return _ai(profile or {"category": "", "descriptor": ""})
            if system.startswith("You draft outreach"):
                return _ai(draft(_facts_from(payload)))
            raise AssertionError("unexpected model call: " + system[:60])

        return post, sent

    # renewals

    def _grounded_paragraph(self, facts):
        return (
            f"In {facts['period']} the team ran {facts['events_run']} events and sampled "
            f"{facts['consumers_sampled']:,} consumers."
        )

    def test_renewal_creates_one_note_and_one_draft_and_never_sends(self):
        client, session = self._close()
        post, _sent = self._ai_router(renewal=self._grounded_paragraph)
        with patch("utils.ai_text._post_json", side_effect=post):
            out = runner.run_renewals(apply=True, today=TODAY, client=client)
        assert out["applied"] is True
        assert [r["status"] for r in out["rows"]] == ["created"]
        writes = session.writes
        assert [w[1] for w in writes] == ["/activity/note/", "/activity/email/"]
        email = writes[1][2]
        assert email["status"] == "draft"
        assert email["to"] == ["riley@volt.test"]
        assert email["body_text"].startswith("Hi Riley,")
        assert "1,445" in email["body_text"]  # its own numbers, to its own lead
        assert "Q3 2026" in email["subject"]
        note = writes[0][2]["note"]
        assert "DRAFT FOR REVIEW" in note and "[spark-sales-assist:renewal:" in note
        row = SalesAssistDraft.objects.get(status="created")
        assert (row.period_start, row.period_end) == Q3

        # Run again: nothing new is drafted.
        with patch("utils.ai_text._post_json", side_effect=post):
            again = runner.run_renewals(apply=True, today=TODAY, client=client)
        assert again["rows"][0]["status"] == "skipped"
        assert len(session.writes) == 2

    def test_marker_in_close_prevents_a_duplicate_even_without_the_audit_row(self):
        key = f"renewal:{self.volt.id}:2026-07-01:2026-09-30"
        client, session = self._close(notes={"lead_volt": [{"id": "n1", "note": runner.marker(key)}]})
        post, _ = self._ai_router(renewal=self._grounded_paragraph)
        with patch("utils.ai_text._post_json", side_effect=post):
            out = runner.run_renewals(apply=True, today=TODAY, client=client)
        assert out["rows"][0]["reason"] == "marker in Close"
        assert session.writes == []

    def test_ungrounded_paragraph_is_dropped_but_numbers_stay(self):
        client, session = self._close()
        post, sent = self._ai_router(renewal=lambda f: "We sampled 99,999 consumers, a record.")
        with patch("utils.ai_text._post_json", side_effect=post):
            runner.run_renewals(apply=True, today=TODAY, client=client)
        assert len(sent) == 2  # one retry
        note = session.writes[0][2]["note"]
        assert "99,999" not in note and "Suggested summary" not in note
        assert "Consumers sampled: 1,445" in note
        row = SalesAssistDraft.objects.get(status="created")
        assert row.facts["narrative_source"] == "none: ungrounded"

    def test_dry_run_by_default_and_when_disabled(self):
        client, session = self._close()
        post, _ = self._ai_router(renewal=self._grounded_paragraph)
        with patch("utils.ai_text._post_json", side_effect=post):
            out = runner.run_renewals(apply=False, today=TODAY, client=client)
            assert out["applied"] is False and out["rows"][0]["status"] == "dry_run"
            self.settings.SALES_ASSIST_ENABLED = False
            out = runner.run_renewals(apply=True, today=TODAY, client=client)
        assert "SALES_ASSIST_ENABLED" in out["why_dry_run"]
        assert session.writes == []
        assert SalesAssistDraft.objects.filter(status="dry_run").count() == 2
        assert not SalesAssistDraft.objects.filter(status="created").exists()

    def test_no_close_key_is_a_clean_noop(self):
        post, _ = self._ai_router(renewal=self._grounded_paragraph)
        with patch("utils.ai_text._post_json", side_effect=post):
            out = runner.run_renewals(apply=True, today=TODAY)
        assert out["applied"] is False and "CLOSE_API_KEY" in out["why_dry_run"]
        assert runner.run_prospects(apply=True)["skipped"] == "close-not-configured"
        assert runner.suggest_links()["skipped"] == "close-not-configured"

    def test_quarter_with_no_recaps_is_skipped(self):
        client, session = self._close()
        out = runner.run_renewals(apply=True, today=TODAY, client=client,
                                  start=date(2026, 1, 1), end=date(2026, 3, 31))
        assert out["rows"][0]["reason"] == "no approved recaps"
        assert session.writes == []

    # prospects

    RESEARCH = {
        "is_consumer_brand": True, "category": "energy drink",
        "what_they_sell": "Clean sparkling energy drinks.", "products": ["Zap Original"],
        "retailers": ["Kroger", "Sprouts"], "recent_news": [], "regions": [],
    }

    @staticmethod
    def _good_draft(facts):
        ex = facts["anonymized_examples"][0]
        return {
            "subject": "Sampling for Zap Fizz",
            "email_body": (
                f"Hi {facts['first_name']},\n\nSaw Zap Fizz is at Kroger. For {ex['who']} at "
                f"{ex['where'][0]} we sampled {ex['consumers_sampled']} consumers with "
                f"{ex.get('conversion_percent', '')} conversion. Open to a 15-minute call?"
            ),
            "one_pager": f"About Zap Fizz\n- Results from similar programs: {ex['who']}, {ex['consumers_sampled']} sampled.",
        }

    def _run_prospect(self, draft, client=None):
        client, session = client or self._close(leads=[PROSPECT_LEAD, CLIENT_LEAD])
        post, sent = self._ai_router(research=self.RESEARCH, draft=draft)
        with patch("utils.ai_text._post_json", side_effect=post):
            out = runner.run_prospects(apply=True, lead_id="lead_zap", client=client,
                                       fetcher=site_fetcher(SITE), today=TODAY)
        return out, session, sent

    def test_prospect_draft_uses_anonymized_results_only(self):
        out, session, sent = self._run_prospect(self._good_draft)
        assert out["rows"][0]["status"] == "created", out
        note = session.writes[0][2]["note"]
        email = session.writes[1][2]
        assert email["status"] == "draft" and email["to"] == ["sam@zapfizz.test"]
        text = note + email["body_text"] + email["subject"]
        assert "a national energy-drink brand" in text
        assert "1,400+" in text
        for leak in ("Volt", "1,445", "1445"):
            assert leak not in text
        # The model is never shown the client's name or its exact figures.
        prompts = json.dumps([p["messages"] for p in sent])
        assert "Volt" not in prompts and "1445" not in prompts and "1,445" not in prompts
        assert "zapfizz.test/about" in note  # sources listed for the rep

    def test_prospect_draft_naming_a_client_is_never_kept(self):
        def leaky(facts):
            d = self._good_draft(facts)
            d["email_body"] += " Ask Volt Energy Co how it went."
            return d

        out, session, sent = self._run_prospect(leaky)
        assert out["rows"][0]["status"] == "failed"
        assert "leak name:Volt Energy Co" in out["rows"][0]["reason"]
        assert session.writes == []
        draft_calls = [p for p in sent if p["messages"][0]["content"].startswith("You draft outreach")]
        assert len(draft_calls) == 2

    def test_prospect_draft_with_an_exact_client_figure_or_a_price_is_never_kept(self):
        def exact(facts):
            d = self._good_draft(facts)
            d["email_body"] += " That was 1,445 people. Demos are $300."
            return d

        out, session, _ = self._run_prospect(exact)
        assert out["rows"][0]["status"] == "failed"
        assert session.writes == []

    def test_existing_client_lead_is_not_pitched(self):
        client, session = self._close(leads=[CLIENT_LEAD])
        out = runner.run_prospects(apply=True, lead_id="lead_volt", client=client)
        assert out["rows"][0]["reason"] == "existing client"
        assert session.writes == []

    def test_prospect_is_idempotent(self):
        self._run_prospect(self._good_draft)
        out, session, _ = self._run_prospect(self._good_draft)
        assert out["rows"][0]["status"] == "skipped"

    def test_first_poll_sets_the_cursor_and_bulk_imports_are_skipped(self):
        client, session = self._close(leads=[PROSPECT_LEAD])
        out = runner.run_prospects(apply=True, client=client)
        assert out["cursor_initialized"] == PROSPECT_LEAD["date_created"]
        assert SalesAssistCursor.objects.get(name=runner.CURSOR_PROSPECTS).value == PROSPECT_LEAD["date_created"]

        self.settings.SALES_ASSIST_IMPORT_BURST = 3
        burst = [
            {**PROSPECT_LEAD, "id": f"lead_b{i}", "date_created": f"2026-11-18T11:0{i}:00+00:00"}
            for i in range(4)
        ]
        client, session = self._close(leads=burst)
        out = runner.run_prospects(apply=True, client=client)
        assert out["skipped_bulk_import"] == 4
        assert session.writes == []
        assert SalesAssistCursor.objects.get(name=runner.CURSOR_PROSPECTS).value == burst[-1]["date_created"]

    def test_unsafe_descriptor_from_the_model_keeps_a_tenant_out_of_examples(self):
        other = self.create_tenant(name="Brightwater Kombucha")
        kit = self._kit(other, "Whole Foods Market 10")
        self._recap(other, kit, 9, 3, "400", "40")
        post, _ = self._ai_router(profile={"category": "kombucha", "descriptor": "Brightwater's kombucha line"})
        with patch("utils.ai_text._post_json", side_effect=post):
            lib = comparables.build_library(TODAY, budget=guards.AiBudget(max_calls=5), allow_ai=True)
        assert [r.tenant_name for r in lib] == ["Volt Energy Co"]
        assert SalesProfile.objects.get(tenant=other).descriptor == ""

    def test_budget_cap_stops_ai_calls(self):
        self.settings.SALES_ASSIST_MAX_AI_CALLS_PER_RUN = 1
        out, session, sent = self._run_prospect(self._good_draft)
        assert len(sent) == 1  # research only
        assert out["rows"][0]["status"] == "failed"
        assert session.writes == []

    # cron + GraphQL

    def test_cron_requires_the_secret_and_never_shows_the_key(self):
        self.settings.INTERNAL_CRON_SECRET = "s3cret"
        self.settings.CLOSE_API_KEY = "api_supersecretvalue"
        http = Client()
        assert http.post("/internal/cron/sales-assist", {"action": "status"}).status_code == 401
        resp = http.post("/internal/cron/sales-assist", {"action": "status"}, HTTP_X_CRON_SECRET="s3cret")
        assert resp.status_code == 200
        body = resp.content.decode()
        assert json.loads(body)["close_configured"] is True
        assert "supersecret" not in body

    @pytest.mark.asyncio
    async def test_only_staff_can_link_a_tenant(self):
        from asgiref.sync import sync_to_async

        from config.schema_client import schema_clients

        self.schema = schema_clients
        admin = await sync_to_async(self.create_user)(
            username="sa-admin", email="sa-admin@test.com", role=self.roles["spark_admin"]
        )
        brand = await sync_to_async(self.create_user)(
            username="sa-brand", email="sa-brand@test.com", role=self.roles["client"]
        )
        await sync_to_async(self.create_tenanted_user)(brand, self.volt)
        other = await sync_to_async(self.create_tenant)(name="Newco")
        mutation = """
        mutation Link($tenantId: ID!, $lead: String!) {
          linkTenantToClose(tenantId: $tenantId, closeLeadId: $lead) { ok reason link { tenantName closeLeadId } }
        }"""
        variables = {"tenantId": str(other.id), "lead": "lead_newco"}
        denied = await self._execute_mutation_authenticated(mutation, variables, brand, "/api/v1/graphql/clients")
        assert denied.data["linkTenantToClose"]["ok"] is False
        ok = await self._execute_mutation_authenticated(mutation, variables, admin, "/api/v1/graphql/clients")
        assert ok.errors is None, ok.errors
        assert ok.data["linkTenantToClose"]["link"] == {"tenantName": "Newco", "closeLeadId": "lead_newco"}
        bad = await self._execute_mutation_authenticated(
            mutation, {"tenantId": str(other.id), "lead": "Newco"}, admin, "/api/v1/graphql/clients"
        )
        assert bad.data["linkTenantToClose"]["ok"] is False


def test_quarter_math():
    from sales_assist.renewal import last_complete_quarter, quarter_label

    assert last_complete_quarter(date(2026, 11, 18)) == Q3
    assert last_complete_quarter(date(2026, 1, 5)) == (date(2025, 10, 1), date(2025, 12, 31))
    assert quarter_label(*Q3) == "Q3 2026"
    assert re.match(r"Aug 1, 2026 to", quarter_label(date(2026, 8, 1), date(2026, 9, 30)))
