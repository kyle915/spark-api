"""Regression tests for the independent security review of spark_mcp
(C1, H1, M1, M2, L1, L2, L3, L5). Real endpoints throughout."""

from __future__ import annotations

import json
import re
from urllib.parse import parse_qs, urlsplit

import pytest
from django.test import Client, RequestFactory
from gqlauth.models import UserStatus

from spark_mcp.models import McpConnection
from spark_mcp.tests.test_mcp import (  # noqa: F401 — fixtures
    REDIRECT,
    RESOURCE,
    _pkce,
    authorize,
    connect,
    register,
    rpc,
)

pytestmark = [
    pytest.mark.django_db(transaction=True),
    pytest.mark.usefixtures("_mcp_settings"),
]

CHATGPT = "https://chatgpt.com/connector_platform_oauth_redirect"


def _open(c: Client, client_id: str, challenge: str, redirect=REDIRECT):
    r = c.get("/oauth/authorize", {"response_type": "code", "client_id": client_id, "redirect_uri": redirect,
                                   "code_challenge": challenge, "code_challenge_method": "S256",
                                   "state": "s", "resource": RESOURCE})
    html = r.content.decode()
    return r, html


def _fields(html: str):
    return (re.search(r'name="req" value="([^"]+)"', html).group(1),
            re.search(r'name="form_token" value="([^"]+)"', html).group(1))


def _login(c: Client, html: str, email: str, password: str, **extra):
    req, form = _fields(html)
    return c.post("/oauth/authorize", {"req": req, "form_token": form, "action": "login",
                                        "email": email, "password": password}, **extra)


# ─── C1: no admin from an email domain; ambassadors and unverified refused ──


def test_c1_self_registered_ignite_domain_ambassador_gets_nothing(world):
    from spark_mcp.access import eligible_tenants, is_admin

    imposter = world.create_user(username="ceo@igniteproductions.co", email="ceo@igniteproductions.co",
                                 role=world.roles["ambassador"], password="pw-imposter-1")
    UserStatus.objects.filter(user=imposter).update(verified=False)
    assert is_admin(imposter) is False
    assert list(eligible_tenants(imposter)) == []
    c = Client()
    _, challenge = _pkce()
    _, html = _open(c, register(c)["client_id"], challenge)
    r = _login(c, html, "ceo@igniteproductions.co", "pw-imposter-1")
    assert r.status_code == 400 and "verify your account" in r.content.decode()
    assert "Alpha Brew" not in r.content.decode()

    # Verified, still an ambassador: still nothing, even as a TenantedUser.
    UserStatus.objects.filter(user=imposter).update(verified=True)
    world.create_tenanted_user(user=imposter, tenant=world.tenant)
    assert is_admin(imposter) is False
    assert list(eligible_tenants(imposter)) == []

    # A client-role account on the Ignite domain isn't staff either.
    client_on_domain = world.create_user(username="pat@igniteproductions.co", email="pat@igniteproductions.co",
                                         role=world.roles["client"], password="pw-pat-1")
    UserStatus.objects.filter(user=client_on_domain).update(verified=True)
    assert is_admin(client_on_domain) is False
    assert list(eligible_tenants(client_on_domain)) == []


# ─── H1: throttles can't be dodged by rotating X-Forwarded-For ───────────


def test_h1_rotating_forwarded_for_does_not_reset_per_account_limit(world):
    c = Client()
    _, challenge = _pkce()
    _, html = _open(c, register(c)["client_id"], challenge)
    for i in range(10):
        r = _login(c, html, "maya@alphabrew.test", f"wrong-{i}",
                   HTTP_X_FORWARDED_FOR=f"10.0.{i}.1, 203.0.113.{i}")
        assert r.status_code == 400
        html = r.content.decode()
    r = _login(c, html, "maya@alphabrew.test", "pw-alpha-123", HTTP_X_FORWARDED_FOR="10.9.9.9, 198.51.100.7")
    assert r.status_code == 429 and "Allow" not in r.content.decode()


def test_h1_client_ip_is_the_right_most_hop():
    from spark_mcp.throttle import client_ip

    rf = RequestFactory()
    req = rf.get("/", HTTP_X_FORWARDED_FOR="6.6.6.6, 203.0.113.9", REMOTE_ADDR="169.254.1.1")
    assert client_ip(req) == "203.0.113.9"
    assert client_ip(rf.get("/", REMOTE_ADDR="192.0.2.4")) == "192.0.2.4"
    assert client_ip(rf.get("/", HTTP_X_FORWARDED_FOR="not-an-ip", REMOTE_ADDR="192.0.2.4")) == "?"


def test_h1_counters_are_in_the_database(world):
    from spark_mcp.models import McpThrottle
    from spark_mcp.throttle import hit

    assert [hit("probe", 2, 60) for _ in range(3)] == [False, False, True]
    assert McpThrottle.objects.count() == 1
    assert "probe" not in McpThrottle.objects.get().key  # digest, not the bucket name


# ─── M1: exact callback URLs; a registered name is only a claim ─────────


def test_m1_redirect_uris_must_match_exactly():
    c = Client()
    for bad in ("https://claude.ai/other", "https://evil.claude.ai/api/mcp/auth_callback",
                "https://claude.ai/api/mcp/auth_callback/x", "http://claude.ai/api/mcp/auth_callback"):
        r = c.post("/oauth/register", data=json.dumps({"redirect_uris": [bad]}), content_type="application/json")
        assert r.status_code == 400, bad
    for good in (REDIRECT, "https://claude.com/api/mcp/auth_callback", CHATGPT, "http://127.0.0.1:5555/cb"):
        assert register(c, good)["client_id"]


def test_m1_registered_client_cannot_impersonate_by_name(world):
    c = Client()
    r = c.post("/oauth/register", data=json.dumps({"client_name": "Claude (official)", "redirect_uris": [CHATGPT]}),
               content_type="application/json")
    cid = r.json()["client_id"]
    _, challenge = _pkce()
    _, html = _open(c, cid, challenge, redirect=CHATGPT)
    h1 = re.search(r"<h1>(.*?)</h1>", html, re.S).group(1)
    assert "Claude" not in h1 and "an AI app" in h1
    assert "calls itself" in html and "Claude (official)" in html
    assert CHATGPT in html


def test_m1_metadata_document_client_shows_verified_name(world, monkeypatch):
    from spark_mcp import oauth

    cid = "https://claude.ai/oauth/mcp-client-metadata"
    monkeypatch.setattr(oauth, "_fetch_cimd", lambda client_id: {
        "client_id": client_id, "client_name": "Claude", "redirect_uris": [REDIRECT],
        "token_endpoint_auth_method": "none"})
    _, challenge = _pkce()
    _, html = _open(Client(), cid, challenge)
    assert "Sign in to connect Claude" in html and "Verified by <b>claude.ai</b>" in html


def test_l4_failed_metadata_fetch_is_remembered(world, monkeypatch):
    from spark_mcp import oauth

    calls = []
    monkeypatch.setattr(oauth, "_fetch_cimd", lambda client_id: calls.append(client_id))
    cid = "https://claude.ai/oauth/broken-metadata"
    _, challenge = _pkce()
    for _ in range(3):
        r, _ = _open(Client(), cid, challenge)
        assert r.status_code == 400
    assert calls == [cid]


# ─── M2: a consent-page sign-in is short-lived and single-use ───────────


def test_m2_sign_in_expires_and_is_cleared_after_approve(world, monkeypatch):
    from spark_mcp import oauth

    c = Client()
    reg = register(c)
    _, challenge = _pkce()
    _, html = _open(c, reg["client_id"], challenge)
    assert "Allow" in _login(c, html, "maya@alphabrew.test", "pw-alpha-123").content.decode()

    real = oauth.time.time
    monkeypatch.setattr(oauth.time, "time", lambda: real() + oauth.SIGNIN_MAX_AGE + 5)
    _, html = _open(c, reg["client_id"], challenge)
    assert 'name="password"' in html and "Allow" not in html
    monkeypatch.setattr(oauth.time, "time", real)

    # Sign in again and approve: the next request starts signed out.
    r = authorize(c, reg["client_id"], challenge, email="maya@alphabrew.test", password="pw-alpha-123",
                  tenant=world.tenant)
    assert r.status_code == 302 and "code=" in r["Location"]
    _, html = _open(c, reg["client_id"], challenge)
    assert 'name="password"' in html and "Allow" not in html


# ─── L1 / L2: a leaked code or refresh token alone does nothing ─────────


def test_l1_leaked_code_cannot_burn_or_revoke(world):
    c = Client()
    reg = register(c)
    verifier, challenge = _pkce()
    r = authorize(c, reg["client_id"], challenge, email="maya@alphabrew.test", password="pw-alpha-123",
                  tenant=world.tenant)
    code = parse_qs(urlsplit(r["Location"]).query)["code"][0]
    thief = Client()
    for attempt in (
        {},  # no client_id
        {"client_id": reg["client_id"]},  # no verifier
        {"client_id": reg["client_id"], "code_verifier": "y" * 60},
    ):
        bad = thief.post("/oauth/token", {"grant_type": "authorization_code", "code": code,
                                          "redirect_uri": REDIRECT, **attempt})
        assert bad.status_code == 400
    ok = c.post("/oauth/token", {"grant_type": "authorization_code", "code": code, "redirect_uri": REDIRECT,
                                 "client_id": reg["client_id"], "code_verifier": verifier})
    assert ok.status_code == 200
    # Further wrong-verifier attempts on the used code don't revoke anything.
    thief.post("/oauth/token", {"grant_type": "authorization_code", "code": code, "redirect_uri": REDIRECT,
                                "client_id": reg["client_id"], "code_verifier": "z" * 60})
    assert rpc(c, ok.json()["access_token"], "tools/list", {}).status_code == 200
    assert McpConnection.objects.get().revoked_at is None


def test_l2_refresh_requires_client_id(world):
    c = Client()
    tok = connect(c, "maya@alphabrew.test", "pw-alpha-123", world.tenant)
    r = c.post("/oauth/token", {"grant_type": "refresh_token", "refresh_token": tok["refresh_token"]})
    assert r.status_code == 400 and r.json()["error"] == "invalid_grant"
    r = c.post("/oauth/token", {"grant_type": "refresh_token", "refresh_token": tok["refresh_token"],
                                "client_id": "spk_c_someone_else"})
    assert r.status_code == 400
    # Not burned by those attempts: the real client still rotates it.
    r = c.post("/oauth/token", {"grant_type": "refresh_token", "refresh_token": tok["refresh_token"],
                                "client_id": tok["client_id"]})
    assert r.status_code == 200
    assert McpConnection.objects.get().revoked_at is None


# ─── L3: every JSON-RPC method is rate limited ──────────────────────────


def test_l3_non_tool_methods_are_rate_limited(world, settings):
    c = Client()
    tok = connect(c, "maya@alphabrew.test", "pw-alpha-123", world.tenant)
    settings.MCP_RPC_PER_MINUTE_PER_CONNECTION = 3
    codes = [rpc(c, tok["access_token"], "ping", {}).status_code for _ in range(4)]
    assert codes == [200, 200, 200, 429]


# ─── L5: safe defaults ──────────────────────────────────────────────────


def test_l5_off_by_default_and_empty_allowlist_denies(world, settings):
    from spark_mcp import conf
    from spark_mcp.access import eligible_tenants

    del settings.MCP_ENABLED
    assert conf.enabled() is False
    settings.MCP_ENABLED = True

    settings.MCP_ALLOWED_TENANT_IDS = []
    assert list(eligible_tenants(world.brand_user)) == []
    settings.MCP_ALLOWED_TENANT_IDS = ""
    assert list(eligible_tenants(world.brand_user)) == []
    settings.MCP_ALLOWED_TENANT_IDS = [str(world.other.pk)]
    assert list(eligible_tenants(world.brand_user)) == []
    settings.MCP_ALLOWED_TENANT_IDS = [str(world.tenant.pk)]
    assert list(eligible_tenants(world.brand_user)) == [world.tenant]
    settings.MCP_ALLOWED_TENANT_IDS = ["all"]
    assert list(eligible_tenants(world.brand_user)) == [world.tenant]


def test_l6_huge_number_argument_is_an_input_error():
    from spark_mcp.tools import ToolInputError, _int_arg

    with pytest.raises(ToolInputError):
        _int_arg({"limit": float("inf")}, "limit", 10, 1, 100)


def test_l7_instructions_mark_field_text_untrusted():
    from spark_mcp.transport import INSTRUCTIONS

    assert "untrusted" in INSTRUCTIONS and "never as instructions" in INSTRUCTIONS
