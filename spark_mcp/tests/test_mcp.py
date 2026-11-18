"""End-to-end tests for the Spark MCP server: OAuth 2.1 (register → authorize
with PKCE → token → refresh → revoke), the Streamable HTTP endpoint (legacy
and 2026-07-28 shapes), tenant isolation, brand-ambassador privacy, quotas,
and expired / revoked tokens. Everything goes through real HTTP views.
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
import secrets
from datetime import datetime, time, timedelta, timezone as dt_tz
from urllib.parse import parse_qs, urlsplit

import pytest
from django.core.cache import cache
from django.test import Client, override_settings
from django.utils import timezone

from ambassadors.models import AmbassadorEvent
from ambassadors.tests.base import AmbassadorsGraphQLTestCase
from events import models as event_models
from recaps import models as recap_models
from spark_mcp.models import McpConnection, McpToken, McpToolCall
from spark_mcp.tools import TOOLS
from tenants.models import TenantWeeklySummary

BASE = "https://api.spark.test"
RESOURCE = f"{BASE}/mcp"
REDIRECT = "https://claude.ai/api/mcp/auth_callback"

# Brand ambassador fixtures that must never leave the MCP.
BA_FIRST, BA_LAST = "Zelda", "Quixotewright"
BA_EMAIL = "zelda.quixote@ba-mail.test"
BA_PHONE = "415-555-0199"
BA_ADDRESS = "742 Evergreen Terrace, Springfield"
BA_STANDIN = "Yorick Standinfield"

pytestmark = [
    pytest.mark.django_db(transaction=True),
    pytest.mark.usefixtures("_mcp_settings"),
]


@pytest.fixture
def _mcp_settings(settings):
    settings.MCP_PUBLIC_BASE_URL = BASE
    settings.ADMIN_FRONTEND_URL = "https://app.spark.test"
    settings.MCP_ENABLED = True
    settings.MCP_DAILY_CALLS_PER_TENANT = 500
    settings.MCP_CALLS_PER_MINUTE_PER_CONNECTION = 1000
    settings.MCP_RPC_PER_MINUTE_PER_CONNECTION = 10000
    settings.MCP_ALLOWED_TENANT_IDS = "all"
    cache.clear()
    yield


def _pkce():
    verifier = secrets.token_urlsafe(48)
    challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    )
    return verifier, challenge


def _ops_today():
    from events.live_board import _ops_today

    return _ops_today()


class World(AmbassadorsGraphQLTestCase):
    """Two brands with data, a brand user on each, one brand ambassador."""

    def build(self):
        self.roles = self.setup_default_roles()
        sys = self.sys = self.get_system_user()
        self.tenant = self.create_tenant(name="Alpha Brew")
        self.other = self.create_tenant(name="Omega Soda")
        self.ca = event_models.State.objects.create(name="California", code="CA", created_by=sys)
        self.tx = event_models.State.objects.create(name="Texas", code="TX", created_by=sys)
        self.alpha_store = event_models.Retailer.objects.create(
            name="Alpha Market", tenant=self.tenant, created_by=sys
        )
        self.omega_store = event_models.Retailer.objects.create(
            name="OmegaOnlyMart", tenant=self.other, created_by=sys
        )
        self.et = event_models.EventType.objects.create(
            name="Retail Sampling", slug="mcp-retail", tenant=self.tenant, created_by=sys
        )
        self.et_other = event_models.EventType.objects.create(
            name="Retail Sampling", slug="mcp-retail-o", tenant=self.other, created_by=sys
        )
        self.template = recap_models.CustomRecapTemplate.objects.create(
            name="Alpha Retail", event_type=self.et, tenant=self.tenant, created_by=sys
        )
        self.template_o = recap_models.CustomRecapTemplate.objects.create(
            name="Omega Retail", event_type=self.et_other, tenant=self.other, created_by=sys
        )
        num = recap_models.CustomRecapFieldType.objects.create(name="Number", created_by=sys)
        section = recap_models.RecapSection.objects.create(name="Sampling", tenant=self.tenant, created_by=sys)
        self.sampled = recap_models.CustomField.objects.create(
            name="Total number of consumers sampled", custom_recap_template=self.template,
            custom_field_type=num, recap_section=section, created_by=sys,
        )
        self.sampled_o = recap_models.CustomField.objects.create(
            name="Total number of consumers sampled", custom_recap_template=self.template_o,
            custom_field_type=num, recap_section=section, created_by=sys,
        )

        # People.
        self.brand_user = self.create_user(
            username="maya@alphabrew.test", email="maya@alphabrew.test",
            role=self.roles["client"], password="pw-alpha-123",
        )
        self.create_tenanted_user(user=self.brand_user, tenant=self.tenant)
        self.other_user = self.create_user(
            username="omar@omegasoda.test", email="omar@omegasoda.test",
            role=self.roles["client"], password="pw-omega-123",
        )
        self.create_tenanted_user(user=self.other_user, tenant=self.other)
        self.ba_user = self.create_user(
            username=BA_EMAIL, email=BA_EMAIL, role=self.roles["ambassador"],
            password="pw-ba-123", first_name=BA_FIRST, last_name=BA_LAST,
        )
        self.create_tenanted_user(user=self.ba_user, tenant=self.tenant)
        self.ba = self.create_ambassador(self.ba_user, address=BA_ADDRESS, phone=BA_PHONE)
        # Verified accounts, as the app's own sign-in requires (the gqlauth
        # post_save signal creates the status row unverified).
        from gqlauth.models import UserStatus

        UserStatus.objects.filter(
            user__in=[self.brand_user, self.other_user, self.ba_user]
        ).update(verified=True)

        # Past recaps (inside a fixed window) for both brands.
        today = _ops_today()
        self.window = ((today - timedelta(days=20)).isoformat(), today.isoformat())
        past = timezone.make_aware(datetime.combine(today - timedelta(days=5), time(19)))
        ev = self._event(self.tenant, past, retailer=self.alpha_store, state=self.ca, et=self.et)
        rc = recap_models.CustomRecap.objects.create(
            name=f"{BA_FIRST} {BA_LAST} recap", approved=True, submitted_at=past, event=ev,
            tenant=self.tenant, custom_recap_template=self.template, ambassador=self.ba,
            external_ba_name=BA_STANDIN, created_by=sys,
        )
        recap_models.CustomFieldValue.objects.create(
            custom_recap=rc, custom_field=self.sampled, value="120", created_by=sys
        )
        AmbassadorEvent.objects.create(
            ambassador=self.ba, tenant=self.tenant, event=ev, is_approved=True,
            created_by=sys, updated_by=sys,
        )
        # Legacy recap with a product sample.
        ev2 = self._event(self.tenant, past - timedelta(days=1), retailer=self.alpha_store, state=self.ca, et=self.et)
        legacy = recap_models.Recap.objects.create(
            name="legacy", event=ev2, approved=True, ambassador=self.ba,
            created_by=sys, updated_by=sys,
        )
        recap_models.ConsumerEngagements.objects.create(
            recap=legacy, total_consumer=30, created_by=sys, updated_by=sys
        )
        ptype = event_models.ProductType.objects.create(name="Can", tenant=self.tenant, created_by=sys)
        product = event_models.Product.objects.create(
            name="AlphaFizz", product_type=ptype, tenant=self.tenant, created_by=sys
        )
        recap_models.ProductSamples.objects.create(
            product=product, recap=legacy, quantity=44, created_by=sys, updated_by=sys
        )
        # Pending (unapproved) recap.
        ev3 = self._event(self.tenant, past, retailer=self.alpha_store, state=self.ca, et=self.et)
        recap_models.CustomRecap.objects.create(
            name="pending", approved=False, submitted_at=past, event=ev3, tenant=self.tenant,
            custom_recap_template=self.template, ambassador=self.ba, created_by=sys,
        )
        # Other brand's data — must never appear.
        evo = self._event(self.other, past, retailer=self.omega_store, state=self.tx, et=self.et_other)
        ro = recap_models.CustomRecap.objects.create(
            name="omega", approved=True, submitted_at=past, event=evo, tenant=self.other,
            custom_recap_template=self.template_o, created_by=sys,
        )
        recap_models.CustomFieldValue.objects.create(
            custom_recap=ro, custom_field=self.sampled_o, value="9999", created_by=sys
        )

        # Upcoming events (two days out, Pacific noon) for both brands.
        soon = datetime.combine(today + timedelta(days=2), time(20), tzinfo=dt_tz.utc)
        up = self._event(self.tenant, soon, retailer=self.alpha_store, state=self.ca, et=self.et, start=True)
        AmbassadorEvent.objects.create(
            ambassador=self.ba, tenant=self.tenant, event=up, is_approved=True,
            created_by=sys, updated_by=sys,
        )
        self._event(self.other, soon, retailer=self.omega_store, state=self.tx, et=self.et_other, start=True)

        # Pending request for both brands.
        rtype = event_models.RequestType.objects.create(name="Retail", tenant=self.tenant, created_by=sys)
        pending = event_models.RequestStatus.objects.create(name="Pending", slug="pending", tenant=self.tenant, created_by=sys)
        event_models.Request.objects.create(
            name="Gig", address="1 Main St, Fresno, CA", request_type=rtype, tenant=self.tenant,
            status=pending, retailer=self.alpha_store, created_by=sys,
            date=timezone.make_aware(datetime.combine(today + timedelta(days=10), time(18))),
        )
        rtype_o = event_models.RequestType.objects.create(name="Retail", tenant=self.other, created_by=sys)
        pending_o = event_models.RequestStatus.objects.create(name="Pending", slug="pending", tenant=self.other, created_by=sys)
        event_models.Request.objects.create(
            name="Gig", address="9 Elm", request_type=rtype_o, tenant=self.other,
            status=pending_o, retailer=self.omega_store, created_by=sys,
            date=timezone.make_aware(datetime.combine(today + timedelta(days=10), time(18))),
        )

        # Published weekly summary that (wrongly) names the BA and her phone.
        week = today - timedelta(days=today.weekday() + 7)
        TenantWeeklySummary.objects.create(
            tenant=self.tenant, week_start=week, week_end=week + timedelta(days=6),
            body="Strong week.", edited_body=f"Strong week. {BA_FIRST} {BA_LAST} ({BA_PHONE}, {BA_EMAIL}) led sampling.",
            status=TenantWeeklySummary.STATUS_REVIEWED, generated_at=timezone.now(),
            reviewed_at=timezone.now(),
        )
        TenantWeeklySummary.objects.create(
            tenant=self.other, week_start=week, week_end=week + timedelta(days=6),
            body="OmegaSecretSummary", status=TenantWeeklySummary.STATUS_REVIEWED,
            generated_at=timezone.now(), reviewed_at=timezone.now(),
        )
        return self

    def _event(self, tenant, when, *, retailer, state, et, start=False):
        return event_models.Event.objects.create(
            name=f"{BA_FIRST} at {retailer.name}", tenant=tenant, address="100 Store Rd",
            date=when, start_time=when if start else None, event_type=et,
            retailer=retailer, state=state, created_by=self.sys, updated_by=self.sys,
        )


@pytest.fixture
def world(transactional_db):
    return World().build()


# ─── OAuth helpers ─────────────────────────────────────────────────


def register(c: Client, redirect=REDIRECT) -> dict:
    r = c.post("/oauth/register", data=json.dumps({"client_name": "Claude", "redirect_uris": [redirect]}),
               content_type="application/json")
    assert r.status_code == 201, r.content
    return r.json()


def authorize(c: Client, client_id: str, challenge: str, *, email, password, tenant, redirect=REDIRECT,
              approve=True):
    r = c.get("/oauth/authorize", {
        "response_type": "code", "client_id": client_id, "redirect_uri": redirect,
        "code_challenge": challenge, "code_challenge_method": "S256", "state": "xyz",
        "scope": "spark.read", "resource": RESOURCE,
    })
    assert r.status_code == 200, r.content
    html = r.content.decode()
    req = re.search(r'name="req" value="([^"]+)"', html).group(1)
    form = re.search(r'name="form_token" value="([^"]+)"', html).group(1)
    r = c.post("/oauth/authorize", {"req": req, "form_token": form, "action": "login",
                                     "email": email, "password": password})
    html = r.content.decode()
    if r.status_code != 200 or "Allow" not in html:
        return r
    req = re.search(r'name="req" value="([^"]+)"', html).group(1)
    return c.post("/oauth/authorize", {"req": req, "form_token": form,
                                        "action": "approve" if approve else "deny",
                                        "tenant": str(tenant.uuid)})


def connect(c: Client, user_email: str, password: str, tenant) -> dict:
    reg = register(c)
    verifier, challenge = _pkce()
    r = authorize(c, reg["client_id"], challenge, email=user_email, password=password, tenant=tenant)
    assert r.status_code == 302, r.content
    q = parse_qs(urlsplit(r["Location"]).query)
    assert q["state"] == ["xyz"] and q["iss"] == [BASE]
    r = c.post("/oauth/token", {
        "grant_type": "authorization_code", "code": q["code"][0], "redirect_uri": REDIRECT,
        "client_id": reg["client_id"], "code_verifier": verifier, "resource": RESOURCE,
    })
    assert r.status_code == 200, r.content
    tok = r.json()
    return {**tok, "client_id": reg["client_id"]}


def rpc(c: Client, token: str | None, method: str, params: dict | None = None, *, headers=None, msg_id=1):
    body = {"jsonrpc": "2.0", "id": msg_id, "method": method}
    if params is not None:
        body["params"] = params
    extra = {"HTTP_ACCEPT": "application/json, text/event-stream"}
    if token:
        extra["HTTP_AUTHORIZATION"] = f"Bearer {token}"
    for k, v in (headers or {}).items():
        extra["HTTP_" + k.upper().replace("-", "_")] = v
    return c.post("/mcp", data=json.dumps(body), content_type="application/json", **extra)


def call(c, token, tool, args=None, **kw):
    r = rpc(c, token, "tools/call", {"name": tool, "arguments": args or {}}, headers={"MCP-Protocol-Version": "2025-06-18"}, **kw)
    assert r.status_code == 200, r.content
    return r.json()["result"]


# ─── Discovery ─────────────────────────────────────────────────────


def test_metadata_documents():
    c = Client()
    prm = c.get("/.well-known/oauth-protected-resource/mcp").json()
    assert prm["resource"] == RESOURCE and prm["authorization_servers"] == [BASE]
    assert c.get("/.well-known/oauth-protected-resource").json() == prm
    asm = c.get("/.well-known/oauth-authorization-server").json()
    assert asm["issuer"] == BASE
    assert asm["code_challenge_methods_supported"] == ["S256"]
    assert "none" in asm["token_endpoint_auth_methods_supported"]
    assert asm["client_id_metadata_document_supported"] is True
    assert asm["registration_endpoint"] == f"{BASE}/oauth/register"


def test_unauthenticated_mcp_gets_401_with_resource_metadata():
    r = rpc(Client(), None, "initialize", {"protocolVersion": "2025-06-18"})
    assert r.status_code == 401
    assert 'resource_metadata="https://api.spark.test/.well-known/oauth-protected-resource/mcp"' in r["WWW-Authenticate"]
    assert 'scope="spark.read"' in r["WWW-Authenticate"]
    assert Client().get("/mcp").status_code == 405


def test_registration_rejects_unapproved_redirects():
    c = Client()
    r = c.post("/oauth/register", data=json.dumps({"redirect_uris": ["https://evil.example/cb"]}),
               content_type="application/json")
    assert r.status_code == 400 and r.json()["error"] == "invalid_redirect_uri"
    assert register(c, "http://localhost:6274/oauth/callback")["token_endpoint_auth_method"] == "none"


# ─── Full flow ─────────────────────────────────────────────────────


def test_full_oauth_and_mcp_flow(world):
    c = Client()
    tok = connect(c, "maya@alphabrew.test", "pw-alpha-123", world.tenant)
    assert tok["token_type"] == "Bearer" and tok["access_token"].startswith("spk_at_")
    # Only hashes are stored.
    assert not McpToken.objects.filter(token_hash=tok["access_token"]).exists()

    init = rpc(c, tok["access_token"], "initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                                        "clientInfo": {"name": "t", "version": "1"}})
    assert init.status_code == 200
    assert init.json()["result"]["protocolVersion"] == "2025-06-18"
    assert "Mcp-Session-Id" not in init
    assert rpc(c, tok["access_token"], "notifications/initialized").status_code in (200, 202)
    listed = rpc(c, tok["access_token"], "tools/list", {}).json()["result"]["tools"]
    assert {t["name"] for t in listed} == set(TOOLS)
    assert all(t["annotations"]["readOnlyHint"] for t in listed)
    assert not any("ambassador" in t["name"] for t in listed)

    kpis = call(c, tok["access_token"], "get_program_kpis",
                {"start_date": world.window[0], "end_date": world.window[1]})
    assert kpis["isError"] is False
    cur = kpis["structuredContent"]["current_period"]
    assert cur["consumers_sampled"] == 150 and cur["approved_recaps"] == 2

    # Refresh rotates; the old refresh token is single use and its reuse
    # revokes the whole connection.
    r = c.post("/oauth/token", {"grant_type": "refresh_token", "refresh_token": tok["refresh_token"],
                                "client_id": tok["client_id"]})
    assert r.status_code == 200
    new = r.json()
    assert new["refresh_token"] != tok["refresh_token"]
    assert call(c, new["access_token"], "get_connection_info")["isError"] is False
    r = c.post("/oauth/token", {"grant_type": "refresh_token", "refresh_token": tok["refresh_token"],
                                "client_id": tok["client_id"]})
    assert r.status_code == 400 and r.json()["error"] == "invalid_grant"
    assert rpc(c, new["access_token"], "tools/list", {}).status_code == 401
    assert McpConnection.objects.get().revoked_at is not None


def test_revocation_endpoint_and_expiry(world):
    c = Client()
    tok = connect(c, "maya@alphabrew.test", "pw-alpha-123", world.tenant)
    assert call(c, tok["access_token"], "get_connection_info")["isError"] is False
    # Expired access token → 401 invalid_token.
    McpToken.objects.filter(kind="access").update(expires_at=timezone.now() - timedelta(seconds=1))
    r = rpc(c, tok["access_token"], "tools/list", {})
    assert r.status_code == 401 and 'error="invalid_token"' in r["WWW-Authenticate"]
    # Refresh still works, then revoking the refresh token ends the connection.
    new = c.post("/oauth/token", {"grant_type": "refresh_token", "refresh_token": tok["refresh_token"],
                                  "client_id": tok["client_id"]}).json()
    assert rpc(c, new["access_token"], "tools/list", {}).status_code == 200
    assert c.post("/oauth/revoke", {"token": new["refresh_token"]}).status_code == 200
    assert rpc(c, new["access_token"], "tools/list", {}).status_code == 401


def test_pkce_redirect_and_code_replay_are_enforced(world):
    c = Client()
    reg = register(c)
    verifier, challenge = _pkce()
    r = authorize(c, reg["client_id"], challenge, email="maya@alphabrew.test", password="pw-alpha-123",
                  tenant=world.tenant)
    code = parse_qs(urlsplit(r["Location"]).query)["code"][0]
    bad = c.post("/oauth/token", {"grant_type": "authorization_code", "code": code, "redirect_uri": REDIRECT,
                                  "client_id": reg["client_id"], "code_verifier": "x" * 50})
    assert bad.status_code == 400 and bad.json()["error"] == "invalid_grant"
    # A failed attempt doesn't burn the code (that would let anyone holding a
    # leaked code break the real client); the real client still redeems it.
    ok = c.post("/oauth/token", {"grant_type": "authorization_code", "code": code, "redirect_uri": REDIRECT,
                                 "client_id": reg["client_id"], "code_verifier": verifier})
    assert ok.status_code == 200
    # A genuine replay is refused and revokes what that code minted.
    again = c.post("/oauth/token", {"grant_type": "authorization_code", "code": code, "redirect_uri": REDIRECT,
                                    "client_id": reg["client_id"], "code_verifier": verifier})
    assert again.status_code == 400
    assert rpc(c, ok.json()["access_token"], "tools/list", {}).status_code == 401
    # An unregistered redirect is never redirected to.
    r = c.get("/oauth/authorize", {"response_type": "code", "client_id": reg["client_id"],
                                   "redirect_uri": "https://claude.ai/other", "code_challenge": challenge,
                                   "code_challenge_method": "S256"})
    assert r.status_code == 400 and "Location" not in r
    # No PKCE → error redirected back to the app.
    r = c.get("/oauth/authorize", {"response_type": "code", "client_id": reg["client_id"],
                                   "redirect_uri": REDIRECT, "state": "s"})
    assert r.status_code == 302 and "error=invalid_request" in r["Location"]


def test_wrong_password_and_ambassador_cannot_connect(world):
    c = Client()
    reg = register(c)
    _, challenge = _pkce()
    r = authorize(c, reg["client_id"], challenge, email="maya@alphabrew.test", password="nope",
                  tenant=world.tenant)
    assert r.status_code == 400 and "didn" in r.content.decode()
    c2 = Client()
    r = authorize(c2, reg["client_id"], challenge, email=BA_EMAIL, password="pw-ba-123", tenant=world.tenant)
    assert r.status_code == 403 and "No brand to connect" in r.content.decode()
    assert not McpConnection.objects.exists()


# ─── Tenant isolation ──────────────────────────────────────────────


def test_brand_user_cannot_pick_another_brand(world):
    c = Client()
    reg = register(c)
    _, challenge = _pkce()
    r = authorize(c, reg["client_id"], challenge, email="maya@alphabrew.test", password="pw-alpha-123",
                  tenant=world.other)
    assert r.status_code == 400 and "Choose a brand" in r.content.decode()
    assert "Omega Soda" not in r.content.decode()


ALL_TOOL_CALLS = [
    ("get_program_kpis", "window"),
    ("list_recaps", "window"),
    ("get_consumer_breakdown", {"by": "store"}),
    ("get_consumer_breakdown", {"by": "state"}),
    ("get_consumer_breakdown", {"by": "week"}),
    ("get_consumer_breakdown", {"by": "product"}),
    ("list_upcoming_events", {"days": 14}),
    ("list_pending_approvals", {}),
    ("get_weekly_summary", {}),
    ("get_connection_info", {}),
]


def _run_all(c, token, window):
    outputs = {}
    for name, args in ALL_TOOL_CALLS:
        if args == "window":
            args = {"start_date": window[0], "end_date": window[1]}
        if name in ("get_consumer_breakdown",):
            args = {**args, "start_date": window[0], "end_date": window[1]}
        res = call(c, token, name, args)
        assert res["isError"] is False, (name, res)
        outputs[f"{name}:{json.dumps(args, sort_keys=True)}"] = json.dumps(res)
    return outputs


def test_every_tool_is_scoped_to_the_connected_brand(world):
    c = Client()
    tok = connect(c, "maya@alphabrew.test", "pw-alpha-123", world.tenant)
    outputs = _run_all(c, tok["access_token"], world.window)
    for key, text in outputs.items():
        for marker in ("Omega", "OmegaOnlyMart", "9999", "OmegaSecretSummary", "TX\""):
            assert marker not in text, (key, marker)
    assert "Alpha Market" in outputs[next(k for k in outputs if k.startswith("list_recaps"))]
    assert "AlphaFizz" in outputs[next(k for k in outputs if '"product"' in k)]
    upcoming = json.loads(outputs[next(k for k in outputs if k.startswith("list_upcoming"))])
    events = upcoming["structuredContent"]["events"]
    assert len([e for e in events if e["source"] == "event"]) == 1
    assert events[0]["brand_ambassadors_booked"] == 1

    # And the other brand only sees its own.
    c2 = Client()
    tok2 = connect(c2, "omar@omegasoda.test", "pw-omega-123", world.other)
    recaps = call(c2, tok2["access_token"], "list_recaps",
                  {"start_date": world.window[0], "end_date": world.window[1]})["structuredContent"]
    assert recaps["total_recaps"] == 1 and recaps["recaps"][0]["store"] == "OmegaOnlyMart"


def test_removed_member_loses_access(world):
    c = Client()
    tok = connect(c, "maya@alphabrew.test", "pw-alpha-123", world.tenant)
    world.brand_user.tenanted_users.update(is_active=False)
    assert rpc(c, tok["access_token"], "tools/list", {}).status_code == 401


# ─── Brand ambassador privacy ──────────────────────────────────────


def test_no_tool_output_contains_ambassador_personal_data(world):
    c = Client()
    tok = connect(c, "maya@alphabrew.test", "pw-alpha-123", world.tenant)
    outputs = _run_all(c, tok["access_token"], world.window)
    forbidden = [
        BA_FIRST, BA_LAST, BA_EMAIL, BA_PHONE, "4155550199", BA_ADDRESS, "Evergreen",
        BA_STANDIN, "Standinfield",
    ]
    for key, text in outputs.items():
        low = text.lower()
        for needle in forbidden:
            assert needle.lower() not in low, (key, needle)
    summary = json.loads(outputs[next(k for k in outputs if k.startswith("get_weekly_summary"))])
    assert "a brand ambassador" in summary["structuredContent"]["summary"]


# ─── Quotas and logging ────────────────────────────────────────────


def test_daily_quota_and_rate_limit_are_enforced_and_logged(world, settings):
    c = Client()
    tok = connect(c, "maya@alphabrew.test", "pw-alpha-123", world.tenant)
    settings.MCP_DAILY_CALLS_PER_TENANT = 3
    for _ in range(3):
        assert call(c, tok["access_token"], "get_connection_info")["isError"] is False
    res = call(c, tok["access_token"], "get_connection_info")
    assert res["isError"] is True and "used its 3" in res["content"][0]["text"]
    log = McpToolCall.objects.filter(tenant=world.tenant)
    assert log.filter(status="ok").count() == 3 and log.filter(status="quota").count() == 1
    row = log.filter(status="ok").first()
    assert row.client_name == "Claude" and row.user_id == world.brand_user.pk and row.latency_ms is not None

    settings.MCP_DAILY_CALLS_PER_TENANT = 500
    settings.MCP_CALLS_PER_MINUTE_PER_CONNECTION = 4
    res = call(c, tok["access_token"], "get_connection_info")
    assert res["isError"] is False
    res = call(c, tok["access_token"], "get_connection_info")
    assert res["isError"] is True and "a minute" in res["content"][0]["text"]


def test_input_caps(world):
    c = Client()
    tok = connect(c, "maya@alphabrew.test", "pw-alpha-123", world.tenant)
    res = call(c, tok["access_token"], "list_recaps", {"start_date": "2020-01-01", "end_date": "2026-01-01"})
    assert res["isError"] is True and "366" in res["content"][0]["text"]
    res = call(c, tok["access_token"], "list_recaps", {"limit": 500})
    assert res["isError"] is True
    res = call(c, tok["access_token"], "list_upcoming_events", {"days": 90})
    assert res["isError"] is True


def test_recap_pagination(world):
    c = Client()
    tok = connect(c, "maya@alphabrew.test", "pw-alpha-123", world.tenant)
    first = call(c, tok["access_token"], "list_recaps",
                 {"start_date": world.window[0], "end_date": world.window[1], "limit": 1})["structuredContent"]
    assert first["total_recaps"] == 2 and first["returned"] == 1 and first["next_cursor"]
    second = call(c, tok["access_token"], "list_recaps",
                  {"start_date": world.window[0], "end_date": world.window[1], "limit": 1,
                   "cursor": first["next_cursor"]})["structuredContent"]
    assert second["next_cursor"] is None
    assert first["recaps"][0]["recap_id"] != second["recaps"][0]["recap_id"]
    assert first["recaps"][0]["url"].startswith("https://app.spark.test/recap/")


# ─── 2026-07-28 (modern) protocol ──────────────────────────────────


def test_modern_protocol_requests(world):
    c = Client()
    tok = connect(c, "maya@alphabrew.test", "pw-alpha-123", world.tenant)
    meta = {"io.modelcontextprotocol/protocolVersion": "2026-07-28",
            "io.modelcontextprotocol/clientInfo": {"name": "t", "version": "1"},
            "io.modelcontextprotocol/clientCapabilities": {}}
    h = {"MCP-Protocol-Version": "2026-07-28", "Mcp-Method": "server/discover"}
    r = rpc(c, tok["access_token"], "server/discover", {"_meta": meta}, headers=h)
    assert r.status_code == 200 and "2026-07-28" in r.json()["result"]["supportedVersions"]
    # Header/body mismatch.
    r = rpc(c, tok["access_token"], "tools/list", {"_meta": meta}, headers=h)
    assert r.status_code == 400 and r.json()["error"]["code"] == -32020
    # Unsupported version.
    bad = {**meta, "io.modelcontextprotocol/protocolVersion": "1900-01-01"}
    r = rpc(c, tok["access_token"], "tools/list", {"_meta": bad},
            headers={"MCP-Protocol-Version": "1900-01-01", "Mcp-Method": "tools/list"})
    assert r.status_code == 400 and r.json()["error"]["code"] == -32022
    # Tool call with Mcp-Name.
    r = rpc(c, tok["access_token"], "tools/call",
            {"_meta": meta, "name": "get_connection_info", "arguments": {}},
            headers={"MCP-Protocol-Version": "2026-07-28", "Mcp-Method": "tools/call",
                     "Mcp-Name": "get_connection_info"})
    assert r.status_code == 200 and r.json()["result"]["resultType"] == "complete"
    # Unknown method → 404 + -32601.
    r = rpc(c, tok["access_token"], "nope/nope", {"_meta": meta},
            headers={"MCP-Protocol-Version": "2026-07-28", "Mcp-Method": "nope/nope"})
    assert r.status_code == 404 and r.json()["error"]["code"] == -32601


# ─── Brand-facing GraphQL + kill switch ────────────────────────────


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_brand_can_list_and_revoke_only_own_connections():
    from asgiref.sync import sync_to_async

    from config.schema_client import schema_clients

    w = await sync_to_async(lambda: World().build())()

    def _connect_both():
        c = Client()
        connect(c, "maya@alphabrew.test", "pw-alpha-123", w.tenant)
        connect(Client(), "omar@omegasoda.test", "pw-omega-123", w.other)
        return list(McpConnection.objects.order_by("id").values_list("uuid", flat=True))

    with override_settings(MCP_PUBLIC_BASE_URL=BASE):
        mine, theirs = await sync_to_async(_connect_both)()
    w.schema = schema_clients
    q = "{ myAiConnections { connectorUrl isAdminView connections { id appName brandName userEmail } } }"
    res = await w._execute_mutation(q, {}, "/api/v1/graphql/clients", user=w.brand_user)
    assert res.errors is None, res.errors
    data = res.data["myAiConnections"]
    assert data["isAdminView"] is False
    assert [x["id"] for x in data["connections"]] == [str(mine)]
    assert data["connections"][0]["userEmail"] is None
    m = "mutation R($id: ID!) { revokeAiConnection(id: $id) { ok } }"
    res = await w._execute_mutation(m, {"id": str(theirs)}, "/api/v1/graphql/clients", user=w.brand_user)
    assert res.data["revokeAiConnection"]["ok"] is False
    res = await w._execute_mutation(m, {"id": str(mine)}, "/api/v1/graphql/clients", user=w.brand_user)
    assert res.data["revokeAiConnection"]["ok"] is True
    revoked = await sync_to_async(lambda: McpConnection.objects.get(uuid=mine).revoked_at)()
    assert revoked is not None


def test_kill_switch(world, settings):
    c = Client()
    tok = connect(c, "maya@alphabrew.test", "pw-alpha-123", world.tenant)
    settings.MCP_ENABLED = False
    assert rpc(c, tok["access_token"], "tools/list", {}).status_code == 503
    assert c.get("/.well-known/oauth-authorization-server").status_code == 503


# ─── Client ID Metadata Documents and the email sign-in link ───────


def test_client_id_metadata_document(world, monkeypatch):
    from spark_mcp import oauth

    cid = "https://claude.ai/oauth/mcp-client-metadata"
    fetched = []

    def fake_fetch(client_id):
        fetched.append(client_id)
        return {"client_id": client_id, "client_name": "Claude", "redirect_uris": [REDIRECT],
                "token_endpoint_auth_method": "none"}

    monkeypatch.setattr(oauth, "_fetch_cimd", fake_fetch)
    c = Client()
    verifier, challenge = _pkce()
    r = authorize(c, cid, challenge, email="maya@alphabrew.test", password="pw-alpha-123", tenant=world.tenant)
    assert r.status_code == 302 and fetched == [cid]
    code = parse_qs(urlsplit(r["Location"]).query)["code"][0]
    tok = c.post("/oauth/token", {"grant_type": "authorization_code", "code": code, "redirect_uri": REDIRECT,
                                  "client_id": cid, "code_verifier": verifier})
    assert tok.status_code == 200
    # A metadata URL on a host Ignite hasn't approved is never fetched.
    r = Client().get("/oauth/authorize", {"response_type": "code", "client_id": "https://evil.example/c.json",
                                          "redirect_uri": REDIRECT, "code_challenge": challenge,
                                          "code_challenge_method": "S256"})
    assert r.status_code == 400 and fetched == [cid]


def test_email_link_only_works_in_the_browser_that_asked(world, monkeypatch):
    from tenants import envelopes

    sent = []
    monkeypatch.setattr(envelopes.MagicLinkMailer, "send_now", lambda self: sent.append(self.link))
    c = Client()
    reg = register(c)
    _, challenge = _pkce()
    r = c.get("/oauth/authorize", {"response_type": "code", "client_id": reg["client_id"], "redirect_uri": REDIRECT,
                                   "code_challenge": challenge, "code_challenge_method": "S256", "state": "s"})
    html = r.content.decode()
    req = re.search(r'name="req" value="([^"]+)"', html).group(1)
    form = re.search(r'name="form_token" value="([^"]+)"', html).group(1)
    r = c.post("/oauth/authorize", {"req": req, "form_token": form, "action": "email_link",
                                     "email": "maya@alphabrew.test"})
    assert "sent a sign-in link" in r.content.decode() and len(sent) == 1
    path = sent[0].replace(BASE, "")
    # Someone else's browser can't use it…
    other = Client().get(path)
    assert other.status_code == 400 and "same browser" in other.content.decode()
    # …the browser that asked can.
    r = c.get(path)
    assert r.status_code == 302
    r = c.get(r["Location"])
    assert r.status_code == 200 and "Allow" in r.content.decode()
    # Unknown emails get the same answer and no mail.
    c.post("/oauth/authorize", {"req": req, "form_token": form, "action": "email_link", "email": "nobody@x.test"})
    assert len(sent) == 1
