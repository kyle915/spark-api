""""Continue with Google" on the AI connection page (handoff to the Spark web
app), plus the two access rules that ride with it: every sign-in path needs a
verified account, and Ignite staff only connect brands they're members of.

Everything goes through real endpoints: the consent page views, the web
app's real ``socialAuthGoogle`` mutation (Google's token check mocked), and
``approveAiSignIn`` over HTTP with the JWT the web app would hold.
"""

from __future__ import annotations

import json
import re
from datetime import timedelta
from urllib.parse import parse_qs, urlsplit

import pytest
from django.test import Client
from django.utils import timezone
from gqlauth.models import UserStatus

from spark_mcp.models import McpConnection, McpSignInHandoff
from spark_mcp.tests.test_mcp import (  # noqa: F401 — fixtures
    BA_EMAIL,
    BASE,
    REDIRECT,
    RESOURCE,
    _pkce,
    register,
)
from tenants.oauth import OAuthIdentity

GQL = "/api/v615204/graphql/clients"
FRONTEND = "https://client.spark.test"

pytestmark = [
    pytest.mark.django_db(transaction=True),
    pytest.mark.usefixtures("_mcp_settings", "_frontend"),
]


@pytest.fixture
def _frontend(settings):
    settings.CLIENT_FRONTEND_URL = FRONTEND
    settings.MCP_SIGNIN_FRONTEND_URL = ""
    yield


@pytest.fixture
def google(monkeypatch):
    """Google's id_token check, mocked: the 'token' is the email it vouches for."""
    from tenants import oauth as tenant_oauth

    def fake_verify(id_token_str, *, audiences=None):
        return OAuthIdentity(provider="google", subject="g-" + id_token_str, email=id_token_str.lower(),
                             email_verified=True)

    monkeypatch.setattr(tenant_oauth, "verify_google_id_token", fake_verify)


# ─── Helpers ───────────────────────────────────────────────────────


def gql(query: str, variables: dict, jwt: str | None = None) -> dict:
    extra = {"HTTP_AUTHORIZATION": f"JWT {jwt}"} if jwt else {}
    r = Client().post(GQL, data=json.dumps({"query": query, "variables": variables}),
                      content_type="application/json", **extra)
    assert r.status_code == 200, r.content
    return r.json()


def web_google_login(email: str) -> dict:
    """What the web app's Google button does: GIS credential → socialAuthGoogle."""
    q = ("mutation G($input: GoogleSocialAuthInput!) { socialAuthGoogle(input: $input) "
         "{ success message token } }")
    return gql(q, {"input": {"idToken": email}})["data"]["socialAuthGoogle"]


def approve(jwt: str | None, handoff: str) -> dict:
    q = "mutation A($h: String!) { approveAiSignIn(handoff: $h) { ok message returnUrl } }"
    return gql(q, {"h": handoff}, jwt)


def start(c: Client, challenge: str, *, extra: dict | None = None) -> str:
    """Open the consent page and press Continue with Google. Returns the
    start nonce from the link to the web app."""
    reg = register(c)
    r = c.get("/oauth/authorize", {
        "response_type": "code", "client_id": reg["client_id"], "redirect_uri": REDIRECT,
        "code_challenge": challenge, "code_challenge_method": "S256", "state": "xyz",
        "scope": "spark.read", "resource": RESOURCE,
    })
    html = r.content.decode()
    # Google first on the sign-in page.
    assert html.index("Continue with Google") < html.index('name="password"')
    req = re.search(r'name="req" value="([^"]+)"', html).group(1)
    form = re.search(r'name="form_token" value="([^"]+)"', html).group(1)
    r = c.post("/oauth/authorize", {"req": req, "form_token": form, "action": "google", **(extra or {})})
    assert r.status_code == 302, r.content
    loc = urlsplit(r["Location"])
    assert f"{loc.scheme}://{loc.netloc}{loc.path}" == f"{FRONTEND}/connect-ai"
    c.client_id = reg["client_id"]  # type: ignore[attr-defined]
    return parse_qs(loc.query)["h"][0]


def come_back(c: Client, return_url: str):
    parts = urlsplit(return_url)
    assert f"{parts.scheme}://{parts.netloc}" == BASE and parts.path == "/oauth/authorize/google"
    return c.get(parts.path + "?" + parts.query)


def jwt_for(email: str) -> str:
    res = web_google_login(email)
    assert res["success"], res
    return res["token"]


# ─── Happy path ────────────────────────────────────────────────────


def test_continue_with_google_full_flow_issues_code(world, google):
    c = Client()
    verifier, challenge = _pkce()
    nonce = start(c, challenge)
    res = approve(jwt_for("maya@alphabrew.test"), nonce)["data"]["approveAiSignIn"]
    assert res["ok"] is True, res
    r = come_back(c, res["returnUrl"])
    assert r.status_code == 302 and r["Location"].startswith("/oauth/authorize?req=")
    r = c.get(r["Location"])
    html = r.content.decode()
    assert r.status_code == 200 and "Allow" in html and "maya@alphabrew.test" in html
    assert "Alpha Brew" in html and "Omega Soda" not in html
    req = re.search(r'name="req" value="([^"]+)"', html).group(1)
    form = re.search(r'name="form_token" value="([^"]+)"', html).group(1)
    r = c.post("/oauth/authorize", {"req": req, "form_token": form, "action": "approve",
                                     "tenant": str(world.tenant.uuid)})
    assert r.status_code == 302
    q = parse_qs(urlsplit(r["Location"]).query)
    assert q["state"] == ["xyz"] and urlsplit(r["Location"]).netloc == "claude.ai"
    tok = c.post("/oauth/token", {"grant_type": "authorization_code", "code": q["code"][0],
                                  "redirect_uri": REDIRECT, "client_id": c.client_id,
                                  "code_verifier": verifier, "resource": RESOURCE})
    assert tok.status_code == 200 and tok.json()["access_token"].startswith("spk_at_")
    conn = McpConnection.objects.get()
    assert conn.user_id == world.brand_user.pk and conn.tenant_id == world.tenant.pk
    # Only digests are stored.
    row = McpSignInHandoff.objects.get()
    assert nonce not in (row.start_hash, row.return_hash) and row.used_at is not None


# ─── Single use, expiry, browser binding ───────────────────────────


def test_start_and_return_nonces_are_single_use(world, google):
    c = Client()
    _, challenge = _pkce()
    nonce = start(c, challenge)
    jwt = jwt_for("maya@alphabrew.test")
    first = approve(jwt, nonce)["data"]["approveAiSignIn"]
    assert first["ok"]
    again = approve(jwt, nonce)["data"]["approveAiSignIn"]
    assert again["ok"] is False and again["returnUrl"] is None
    assert come_back(c, first["returnUrl"]).status_code == 302
    r = come_back(c, first["returnUrl"])
    assert r.status_code == 400 and "finish" in r.content.decode()


def test_handoff_expires(world, google):
    c = Client()
    _, challenge = _pkce()
    nonce = start(c, challenge)
    McpSignInHandoff.objects.update(expires_at=timezone.now() - timedelta(seconds=1))
    res = approve(jwt_for("maya@alphabrew.test"), nonce)["data"]["approveAiSignIn"]
    assert res["ok"] is False and "expired" in res["message"]

    # Approved in time, but came back too late.
    c2 = Client()
    nonce2 = start(c2, challenge)
    res = approve(jwt_for("maya@alphabrew.test"), nonce2)["data"]["approveAiSignIn"]
    assert res["ok"]
    McpSignInHandoff.objects.filter(approved_at__isnull=False).update(
        expires_at=timezone.now() - timedelta(seconds=1)
    )
    r = come_back(c2, res["returnUrl"])
    assert r.status_code == 400
    assert "spark_mcp_uid" not in c2.session


def test_wrong_browser_is_rejected_and_burns_the_handoff(world, google):
    """Someone starts a connection and sends the web-app link to a victim. The
    victim's approval comes back in the victim's browser, which lacks the
    starter's session binding: refused, and the starter can't reuse it."""
    starter = Client()
    _, challenge = _pkce()
    nonce = start(starter, challenge)
    res = approve(jwt_for("maya@alphabrew.test"), nonce)["data"]["approveAiSignIn"]
    assert res["ok"]
    victim = Client()
    r = come_back(victim, res["returnUrl"])
    assert r.status_code == 400 and "same browser" in r.content.decode()
    assert "spark_mcp_uid" not in victim.session
    r = come_back(starter, res["returnUrl"])
    assert r.status_code == 400
    assert "spark_mcp_uid" not in starter.session
    # And the starter holding only the start nonce has nothing to finish with.
    r = starter.get("/oauth/authorize/google", {"h": nonce})
    assert r.status_code == 400


def test_open_redirect_attempts_are_ignored(world, google):
    c = Client()
    _, challenge = _pkce()
    nonce = start(c, challenge, extra={"next": "https://evil.example/", "return_to": "https://evil.example/"})
    res = approve(jwt_for("maya@alphabrew.test"), nonce)["data"]["approveAiSignIn"]
    assert res["returnUrl"].startswith(f"{BASE}/oauth/authorize/google?h=")
    parts = urlsplit(res["returnUrl"])
    r = c.get(parts.path + "?" + parts.query + "&next=https%3A%2F%2Fevil.example%2F")
    assert r.status_code == 302 and r["Location"].startswith("/oauth/authorize?req=")
    # A URL in place of the nonce goes nowhere.
    r = Client().get("/oauth/authorize/google", {"h": "https://evil.example/"})
    assert r.status_code == 400 and "evil" not in r.get("Location", "")


# ─── Who is refused ────────────────────────────────────────────────


def test_ambassador_refused(world, google):
    c = Client()
    _, challenge = _pkce()
    nonce = start(c, challenge)
    res = approve(jwt_for(BA_EMAIL), nonce)["data"]["approveAiSignIn"]
    assert res["ok"] is False and res["returnUrl"] is None
    assert "ambassador" in res["message"]
    assert McpSignInHandoff.objects.get().refused_reason == "no brand"


def test_unknown_google_email_refused_and_not_created(world, google):
    from django.contrib.auth import get_user_model

    res = web_google_login("stranger@nowhere.test")
    assert res["success"] is False and res["token"] is None
    assert not get_user_model().objects.filter(email="stranger@nowhere.test").exists()


def test_unauthenticated_approve_refused(world, google):
    c = Client()
    _, challenge = _pkce()
    nonce = start(c, challenge)
    body = approve(None, nonce)
    assert body.get("errors") and not (body.get("data") or {}).get("approveAiSignIn")
    assert McpSignInHandoff.objects.get().approved_at is None


# ─── Verified accounts only, on every path ─────────────────────────


def _unverify(user):
    UserStatus.objects.update_or_create(user=user, defaults={"verified": False})


def test_unverified_account_refused_on_google_path(world, google):
    jwt = jwt_for("maya@alphabrew.test")  # the web app's Google login doesn't gate on it
    _unverify(world.brand_user)
    c = Client()
    _, challenge = _pkce()
    nonce = start(c, challenge)
    res = approve(jwt, nonce)["data"]["approveAiSignIn"]
    assert res["ok"] is False and "verify your account" in res["message"]


def test_unverified_account_refused_on_password_path(world):
    from spark_mcp.tests.test_mcp import authorize

    _unverify(world.brand_user)
    _, challenge = _pkce()
    c = Client()
    r = authorize(c, register(c)["client_id"], challenge, email="maya@alphabrew.test",
                  password="pw-alpha-123", tenant=world.tenant)
    assert r.status_code == 400 and "verify your account" in r.content.decode()
    assert "spark_mcp_uid" not in c.session


def test_unverified_account_refused_on_email_link_path(world, monkeypatch):
    from tenants import envelopes

    sent = []
    monkeypatch.setattr(envelopes.MagicLinkMailer, "send_now", lambda self: sent.append(self.link))
    _unverify(world.brand_user)
    c = Client()
    reg = register(c)
    _, challenge = _pkce()
    r = c.get("/oauth/authorize", {"response_type": "code", "client_id": reg["client_id"],
                                   "redirect_uri": REDIRECT, "code_challenge": challenge,
                                   "code_challenge_method": "S256", "state": "s"})
    html = r.content.decode()
    req = re.search(r'name="req" value="([^"]+)"', html).group(1)
    form = re.search(r'name="form_token" value="([^"]+)"', html).group(1)
    r = c.post("/oauth/authorize", {"req": req, "form_token": form, "action": "email_link",
                                     "email": "maya@alphabrew.test"})
    assert r.status_code == 200 and sent == []  # same generic notice, no link sent

    # A link minted while verified stops working once the account isn't.
    UserStatus.objects.filter(user=world.brand_user).update(verified=True)
    c.post("/oauth/authorize", {"req": req, "form_token": form, "action": "email_link",
                                "email": "maya@alphabrew.test"})
    assert len(sent) == 1
    _unverify(world.brand_user)
    parts = urlsplit(sent[0])
    r = c.get(parts.path + "?" + parts.query)
    assert r.status_code == 400 and "verify your account" in r.content.decode()


def test_unverified_session_drops_back_to_sign_in(world):
    """Verified at sign-in, unverified before approving: back to sign-in."""
    c = Client()
    _, challenge = _pkce()
    reg = register(c)
    params = {"response_type": "code", "client_id": reg["client_id"], "redirect_uri": REDIRECT,
              "code_challenge": challenge, "code_challenge_method": "S256", "state": "s"}
    html = c.get("/oauth/authorize", params).content.decode()
    req = re.search(r'name="req" value="([^"]+)"', html).group(1)
    form = re.search(r'name="form_token" value="([^"]+)"', html).group(1)
    r = c.post("/oauth/authorize", {"req": req, "form_token": form, "action": "login",
                                     "email": "maya@alphabrew.test", "password": "pw-alpha-123"})
    assert "Allow" in r.content.decode()
    _unverify(world.brand_user)
    html = c.get("/oauth/authorize", params).content.decode()
    assert 'name="password"' in html and "verify your account" in html and "Allow" not in html


# ─── Ignite staff: member brands only ──────────────────────────────


def test_staff_only_connect_brands_they_are_members_of(world):
    from spark_mcp.access import eligible_tenants
    from spark_mcp.tests.test_mcp import authorize

    staff = world.create_user(username="ops@igniteproductions.co", email="ops@igniteproductions.co",
                              role=world.roles["spark_admin"], password="pw-staff-123", is_staff=True)
    UserStatus.objects.filter(user=staff).update(verified=True)
    # No membership: nothing to connect, even for staff.
    assert list(eligible_tenants(staff)) == []
    c = Client()
    _, challenge = _pkce()
    r = authorize(c, register(c)["client_id"], challenge, email="ops@igniteproductions.co",
                  password="pw-staff-123", tenant=world.tenant)
    assert r.status_code == 403 and "No brand to connect" in r.content.decode()

    # Member of one brand: only that brand is offered or accepted.
    world.create_tenanted_user(user=staff, tenant=world.tenant)
    assert list(eligible_tenants(staff)) == [world.tenant]
    c = Client()
    r = authorize(c, register(c)["client_id"], challenge, email="ops@igniteproductions.co",
                  password="pw-staff-123", tenant=world.other)
    assert r.status_code == 400 and "Choose a brand" in r.content.decode()
    assert "Omega Soda" not in r.content.decode()
    r = authorize(Client(), register(Client())["client_id"], challenge, email="ops@igniteproductions.co",
                  password="pw-staff-123", tenant=world.tenant)
    assert r.status_code == 302 and "code=" in r["Location"]
