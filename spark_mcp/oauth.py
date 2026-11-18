"""OAuth 2.1 authorization server for MCP clients (Claude, ChatGPT, ...).

Endpoints (all under the API's public origin, the issuer):

* ``/.well-known/oauth-protected-resource[/mcp]`` — RFC 9728
* ``/.well-known/oauth-authorization-server`` — RFC 8414
* ``/oauth/register`` — RFC 7591 dynamic client registration (public clients)
* ``/oauth/authorize`` — sign in with a Spark account, pick a brand, approve
* ``/oauth/token`` — authorization_code (PKCE S256 required) and
  refresh_token (single use, rotated; reuse revokes the connection)
* ``/oauth/revoke`` — RFC 7009

Client ID Metadata Documents are supported too: an https ``client_id`` on an
allowed host is fetched and its ``redirect_uris`` are used.

Sign-in reuses Spark accounts: email + password (the same backend as the
app's login) or a one-time email link for people who normally use Google
sign-in or magic links. The sign-in lives only in this page's session key
(``SESSION_USER_KEY``); it is not a Django login and grants nothing else.
"""

from __future__ import annotations

import base64
import hashlib
import ipaddress
import json
import logging
import secrets
from datetime import timedelta
from urllib.parse import urlencode, urlsplit

import httpx
from django.contrib.auth import authenticate, get_user_model
from django.core import signing
from django.core.cache import cache
from django.db import transaction
from django.http import HttpRequest, HttpResponse, HttpResponseRedirect, JsonResponse
from django.shortcuts import render
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_GET, require_http_methods, require_POST

from . import conf
from .access import eligible_tenants
from .models import McpAuthorizationCode, McpClient, McpConnection, McpToken, digest

logger = logging.getLogger(__name__)

SESSION_USER_KEY = "spark_mcp_uid"
SESSION_LINK_NONCE = "spark_mcp_link_nonce"
SESSION_FORM_TOKEN = "spark_mcp_form_token"
REQUEST_SALT = "spark.mcp.authorize.v1"
LINK_SALT = "spark.mcp.signin-link.v1"
REQUEST_MAX_AGE = 30 * 60
LINK_MAX_AGE = 15 * 60
LOOPBACK_HOSTS = {"localhost", "127.0.0.1", "[::1]", "::1"}
CIMD_REFRESH = timedelta(hours=24)
CIMD_MAX_BYTES = 64 * 1024


# ─── Small helpers ─────────────────────────────────────────────────


def _disabled() -> JsonResponse:
    return JsonResponse(
        {"error": "temporarily_unavailable", "error_description": "AI connections are turned off."},
        status=503,
    )


def _client_ip(request: HttpRequest) -> str:
    fwd = request.META.get("HTTP_X_FORWARDED_FOR", "")
    return (fwd.split(",")[0].strip() if fwd else request.META.get("REMOTE_ADDR", "")) or "?"


def _throttled(bucket: str, limit: int, seconds: int) -> bool:
    """Best-effort per-instance throttle on the Django cache."""
    key = f"spark_mcp:throttle:{bucket}"
    try:
        count = cache.get_or_set(key, 0, seconds)
        if count >= limit:
            return True
        try:
            cache.incr(key)
        except ValueError:
            cache.set(key, 1, seconds)
    except Exception:  # noqa: BLE001 — a cache outage must not block sign-in
        return False
    return False


def _oauth_error(error: str, description: str, status: int = 400) -> JsonResponse:
    resp = JsonResponse({"error": error, "error_description": description}, status=status)
    resp["Cache-Control"] = "no-store"
    return resp


def _new_secret(prefix: str) -> str:
    return prefix + secrets.token_urlsafe(32)


def _host_allowed(host: str, allowed: list[str]) -> bool:
    host = (host or "").lower().rstrip(".")
    return any(host == h or host.endswith("." + h) for h in allowed)


def _is_loopback(parts) -> bool:
    return parts.scheme == "http" and (parts.hostname or "").lower() in LOOPBACK_HOSTS


def redirect_uri_allowed_for_registration(uri: str) -> bool:
    try:
        parts = urlsplit(uri)
    except ValueError:
        return False
    if parts.fragment or not parts.hostname:
        return False
    if _is_loopback(parts):
        return True
    return parts.scheme == "https" and _host_allowed(parts.hostname, conf.allowed_redirect_hosts())


def redirect_uri_matches(uri: str, registered: list[str]) -> bool:
    """Exact match, except loopback redirects match on any port (RFC 8252 §7.3;
    Claude Code and the MCP Inspector pick a port per session)."""
    if uri in registered:
        return True
    try:
        parts = urlsplit(uri)
    except ValueError:
        return False
    if not _is_loopback(parts):
        return False
    for reg in registered:
        try:
            rp = urlsplit(reg)
        except ValueError:
            continue
        if (
            _is_loopback(rp)
            and (rp.hostname or "").lower() == (parts.hostname or "").lower()
            and rp.path == parts.path
            and rp.query == parts.query
        ):
            return True
    return False


def _canonical_resource_ok(resource: str | None) -> bool:
    if not resource:
        return True  # older clients omit it; the token is bound to our resource anyway
    want = conf.resource_url().rstrip("/").lower()
    return resource.strip().rstrip("/").lower() == want


def _pkce_ok(verifier: str, challenge: str) -> bool:
    if not verifier or not (43 <= len(verifier) <= 128):
        return False
    computed = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest())
        .decode()
        .rstrip("=")
    )
    return secrets.compare_digest(computed, challenge)


# ─── Metadata ──────────────────────────────────────────────────────


def protected_resource_metadata() -> dict:
    return {
        "resource": conf.resource_url(),
        "authorization_servers": [conf.issuer()],
        "scopes_supported": [conf.SCOPE],
        "bearer_methods_supported": ["header"],
        "resource_name": conf.SERVER_NAME,
        "resource_documentation": f"{conf.frontend_url()}/help/ai-connector",
    }


def authorization_server_metadata() -> dict:
    iss = conf.issuer()
    return {
        "issuer": iss,
        "authorization_endpoint": f"{iss}/oauth/authorize",
        "token_endpoint": f"{iss}/oauth/token",
        "registration_endpoint": f"{iss}/oauth/register",
        "revocation_endpoint": f"{iss}/oauth/revoke",
        "response_types_supported": ["code"],
        "response_modes_supported": ["query"],
        "grant_types_supported": ["authorization_code", "refresh_token"],
        "code_challenge_methods_supported": ["S256"],
        "token_endpoint_auth_methods_supported": ["none"],
        "revocation_endpoint_auth_methods_supported": ["none"],
        "scopes_supported": [conf.SCOPE],
        "client_id_metadata_document_supported": True,
        "authorization_response_iss_parameter_supported": True,
        "service_documentation": f"{conf.frontend_url()}/help/ai-connector",
    }


def _json(data: dict, status: int = 200) -> JsonResponse:
    resp = JsonResponse(data, status=status)
    resp["Access-Control-Allow-Origin"] = "*"
    return resp


@require_GET
def protected_resource_view(request: HttpRequest, suffix: str = "") -> JsonResponse:
    if not conf.enabled():
        return _disabled()
    return _json(protected_resource_metadata())


@require_GET
def authorization_server_view(request: HttpRequest) -> JsonResponse:
    if not conf.enabled():
        return _disabled()
    return _json(authorization_server_metadata())


# ─── Client registration (RFC 7591) and CIMD ───────────────────────


@csrf_exempt
@require_POST
def register_view(request: HttpRequest) -> JsonResponse:
    if not conf.enabled():
        return _disabled()
    if _throttled(f"register:{_client_ip(request)}", 30, 3600):
        return _oauth_error("slow_down", "Too many registrations. Try again later.", 429)
    try:
        body = json.loads(request.body or b"{}")
        assert isinstance(body, dict)
    except Exception:  # noqa: BLE001
        return _oauth_error("invalid_client_metadata", "Body must be a JSON object.")

    redirect_uris = body.get("redirect_uris")
    if (
        not isinstance(redirect_uris, list)
        or not redirect_uris
        or len(redirect_uris) > 10
        or not all(isinstance(u, str) and len(u) <= 500 for u in redirect_uris)
    ):
        return _oauth_error("invalid_redirect_uri", "redirect_uris must list 1-10 URLs.")
    bad = [u for u in redirect_uris if not redirect_uri_allowed_for_registration(u)]
    if bad:
        return _oauth_error(
            "invalid_redirect_uri",
            "Redirect URL not allowed for Spark: " + bad[0]
            + ". Spark accepts approved AI apps only; contact Ignite to add one.",
        )
    grant_types = body.get("grant_types") or ["authorization_code", "refresh_token"]
    if not isinstance(grant_types, list) or not set(grant_types) <= {
        "authorization_code",
        "refresh_token",
    }:
        return _oauth_error(
            "invalid_client_metadata", "Only authorization_code and refresh_token are supported."
        )
    response_types = body.get("response_types") or ["code"]
    if response_types != ["code"]:
        return _oauth_error("invalid_client_metadata", "Only response_type code is supported.")

    name = str(body.get("client_name") or "")[:200]
    client_uri = str(body.get("client_uri") or "")[:500]
    if client_uri and not client_uri.startswith("https://"):
        client_uri = ""
    client = McpClient.objects.create(
        client_id=_new_secret("spk_c_"),
        kind=McpClient.KIND_DCR,
        client_name=name,
        client_uri=client_uri,
        redirect_uris=redirect_uris,
        metadata={k: body.get(k) for k in ("software_id", "software_version", "scope") if k in body},
    )
    data = {
        "client_id": client.client_id,
        "client_id_issued_at": int(client.created_at.timestamp()),
        "client_name": client.client_name,
        "redirect_uris": client.redirect_uris,
        "grant_types": grant_types,
        "response_types": ["code"],
        # Public clients only: we never issue a secret.
        "token_endpoint_auth_method": "none",
        "scope": conf.SCOPE,
    }
    resp = JsonResponse(data, status=201)
    resp["Cache-Control"] = "no-store"
    return resp


def _cimd_url_ok(client_id: str) -> bool:
    try:
        parts = urlsplit(client_id)
    except ValueError:
        return False
    if parts.scheme != "https" or not parts.hostname or parts.path in ("", "/"):
        return False
    if parts.username or parts.password or parts.fragment or parts.port not in (None, 443):
        return False
    host = parts.hostname.lower()
    try:
        ipaddress.ip_address(host)
        return False  # never fetch from a bare IP
    except ValueError:
        pass
    return _host_allowed(host, conf.allowed_cimd_hosts())


def _fetch_cimd(client_id: str) -> dict | None:
    try:
        with httpx.Client(timeout=5.0, follow_redirects=False) as http:
            with http.stream("GET", client_id, headers={"Accept": "application/json"}) as resp:
                if resp.status_code != 200:
                    return None
                chunks, size = [], 0
                for chunk in resp.iter_bytes():
                    size += len(chunk)
                    if size > CIMD_MAX_BYTES:
                        return None
                    chunks.append(chunk)
        doc = json.loads(b"".join(chunks))
    except Exception:  # noqa: BLE001
        logger.info("CIMD fetch failed for %s", client_id, exc_info=True)
        return None
    if not isinstance(doc, dict) or doc.get("client_id") != client_id:
        return None
    uris = doc.get("redirect_uris")
    if not isinstance(uris, list) or not uris or not all(isinstance(u, str) for u in uris):
        return None
    if doc.get("token_endpoint_auth_method", "none") != "none":
        return None
    return doc


def resolve_client(client_id: str) -> McpClient | None:
    if not client_id or len(client_id) > 512:
        return None
    client = McpClient.objects.filter(client_id=client_id).first()
    if client is not None and client.kind == McpClient.KIND_DCR:
        return None if client.disabled else client
    if not _cimd_url_ok(client_id):
        return None
    if client is not None and client.disabled:
        return None
    if client is not None and client.updated_at > timezone.now() - CIMD_REFRESH:
        return client
    doc = _fetch_cimd(client_id)
    if doc is None:
        return client  # keep using the last good copy if we had one
    uris = [u for u in doc["redirect_uris"] if redirect_uri_allowed_for_registration(u)]
    if not uris:
        return None
    defaults = {
        "kind": McpClient.KIND_CIMD,
        "client_name": str(doc.get("client_name") or "")[:200],
        "client_uri": str(doc.get("client_uri") or "")[:500],
        "redirect_uris": uris,
        "metadata": {"fetched_at": timezone.now().isoformat()},
    }
    client, _ = McpClient.objects.update_or_create(client_id=client_id, defaults=defaults)
    return client


# ─── Authorization endpoint ────────────────────────────────────────


def _redirect_with(redirect_uri: str, params: dict) -> HttpResponseRedirect:
    params = {k: v for k, v in params.items() if v is not None}
    params["iss"] = conf.issuer()
    sep = "&" if "?" in redirect_uri else "?"
    resp = HttpResponseRedirect(redirect_uri + sep + urlencode(params))
    resp["Cache-Control"] = "no-store"
    return resp


def _page(request, mode: str, status: int = 200, **ctx) -> HttpResponse:
    resp = render(request, "spark_mcp/authorize.html", {"mode": mode, **ctx}, status=status)
    resp["Cache-Control"] = "no-store"
    resp["X-Frame-Options"] = "DENY"
    resp["Content-Security-Policy"] = "default-src 'none'; style-src 'unsafe-inline'; form-action 'self' https: http://localhost:* http://127.0.0.1:*; frame-ancestors 'none'"
    resp["Referrer-Policy"] = "no-referrer"
    return resp


def _validate_authorize(params) -> tuple[dict | None, HttpResponse | None, str]:
    """Check an authorization request. Returns (request, redirect_error, page_error)."""
    client_id = params.get("client_id", "")
    redirect_uri = params.get("redirect_uri", "")
    client = resolve_client(client_id)
    if client is None:
        return None, None, "This AI app isn't registered with Spark, or isn't approved to connect."
    if not redirect_uri or not redirect_uri_matches(redirect_uri, client.redirect_uris):
        return None, None, "The app sent a return address Spark doesn't recognise, so we stopped here."
    state = params.get("state")

    def fail(error, description):
        return None, _redirect_with(
            redirect_uri, {"error": error, "error_description": description, "state": state}
        ), ""

    if params.get("response_type") != "code":
        return fail("unsupported_response_type", "Only response_type=code is supported.")
    challenge = params.get("code_challenge", "")
    if not challenge or params.get("code_challenge_method") != "S256":
        return fail("invalid_request", "PKCE with code_challenge_method=S256 is required.")
    if not (43 <= len(challenge) <= 128):
        return fail("invalid_request", "code_challenge is malformed.")
    if not _canonical_resource_ok(params.get("resource")):
        return fail("invalid_target", f"Unknown resource. Use {conf.resource_url()}.")
    requested = set((params.get("scope") or conf.SCOPE).split())
    requested.discard("offline_access")
    if requested and not requested <= {conf.SCOPE}:
        return fail("invalid_scope", f"Spark supports the {conf.SCOPE} scope only.")
    return (
        {
            "client_id": client.client_id,
            "redirect_uri": redirect_uri,
            "state": state,
            "code_challenge": challenge,
            "resource": conf.resource_url(),
            "scope": conf.SCOPE,
        },
        None,
        "",
    )


def _session_user(request):
    uid = request.session.get(SESSION_USER_KEY)
    if not uid:
        return None
    user = get_user_model().objects.filter(pk=uid, is_active=True).first()
    if user is None:
        request.session.pop(SESSION_USER_KEY, None)
    return user


def _render_step(request, req: dict, *, error: str = "", notice: str = "", status: int = 200):
    client = McpClient.objects.filter(client_id=req["client_id"]).first()
    blob = signing.dumps(req, salt=REQUEST_SALT)
    redirect_host = urlsplit(req["redirect_uri"]).hostname or ""
    common = {
        "req": blob,
        "client_name": client.display_name if client else "An AI app",
        "redirect_host": redirect_host,
        "loopback": redirect_host.lower() in LOOPBACK_HOSTS,
        "error": error,
        "notice": notice,
    }
    common["form_token"] = _form_token(request)
    user = _session_user(request)
    if user is None:
        return _page(request, "login", status=status, **common)
    tenants = list(eligible_tenants(user).values("uuid", "name")[:500])
    if not tenants:
        return _page(
            request,
            "no_brand",
            status=403,
            user_email=user.email,
            **common,
        )
    return _page(request, "consent", status=status, user_email=user.email, tenants=tenants, **common)


def _form_token(request) -> str:
    token = request.session.get(SESSION_FORM_TOKEN)
    if not token:
        token = secrets.token_urlsafe(24)
        request.session[SESSION_FORM_TOKEN] = token
    return token


def _form_token_ok(request) -> bool:
    expected = request.session.get(SESSION_FORM_TOKEN) or ""
    sent = request.POST.get("form_token") or ""
    return bool(expected) and secrets.compare_digest(expected, sent)


# Django's CSRF middleware compares Origin with request.is_secure(), which is
# False behind Cloud Run's TLS proxy, so every POST would 403. This view uses
# its own session-bound form token instead (see _form_token_ok).
@csrf_exempt
@require_http_methods(["GET", "POST"])
def authorize_view(request: HttpRequest) -> HttpResponse:
    if not conf.enabled():
        return _page(request, "message", status=503, message="AI connections are turned off right now.")
    if request.method == "GET":
        if "req" in request.GET:
            req = _load_req(request.GET.get("req", ""))
            if req is None:
                return _page(request, "message", status=400, message="This sign-in request expired. Start again from your AI app.")
            return _render_step(request, req)
        req, redirect_error, page_error = _validate_authorize(request.GET)
        if redirect_error is not None:
            return redirect_error
        if req is None:
            return _page(request, "message", status=400, message=page_error)
        return _render_step(request, req)

    req = _load_req(request.POST.get("req", ""))
    if req is None:
        return _page(request, "message", status=400, message="This sign-in request expired. Start again from your AI app.")
    if not _form_token_ok(request):
        return _render_step(request, req, error="Your session expired. Try again.", status=400)
    action = request.POST.get("action", "")
    if action == "login":
        return _do_login(request, req)
    if action == "email_link":
        return _do_email_link(request, req)
    if action == "switch":
        request.session.pop(SESSION_USER_KEY, None)
        return _render_step(request, req)
    if action == "deny":
        return _redirect_with(
            req["redirect_uri"],
            {"error": "access_denied", "error_description": "The user declined.", "state": req["state"]},
        )
    if action == "approve":
        return _do_approve(request, req)
    return _render_step(request, req, error="Something went wrong. Try again.", status=400)


def _load_req(blob: str) -> dict | None:
    try:
        req = signing.loads(blob, salt=REQUEST_SALT, max_age=REQUEST_MAX_AGE)
    except signing.BadSignature:
        return None
    return req if isinstance(req, dict) else None


def _do_login(request, req):
    email = (request.POST.get("email") or "").strip()
    password = request.POST.get("password") or ""
    bucket = f"login:{email.lower()}:{_client_ip(request)}"
    if _throttled(bucket, 10, 900):
        return _render_step(request, req, error="Too many tries. Wait 15 minutes, or use the email link instead.", status=429)
    user = authenticate(request, username=email, password=password) if email and password else None
    if user is None:
        return _render_step(request, req, error="That email and password didn't match a Spark account.", status=400)
    request.session.cycle_key()
    request.session[SESSION_USER_KEY] = user.pk
    return _render_step(request, req)


def _do_email_link(request, req):
    email = (request.POST.get("email") or "").strip()
    notice = "If that email belongs to a Spark account, we've sent a sign-in link. Open it in this browser."
    if not email or _throttled(f"link:{email.lower()}", 5, 900) or _throttled(
        f"linkip:{_client_ip(request)}", 20, 900
    ):
        return _render_step(request, req, notice=notice)
    user = get_user_model().objects.filter(email__iexact=email, is_active=True).first()
    if user is not None:
        # The link only works in the browser that asked for it: the session
        # holds a nonce the link must match. A link forwarded to (or phished
        # out of) someone else's inbox can't finish someone else's connection.
        nonce = secrets.token_urlsafe(16)
        request.session[SESSION_LINK_NONCE] = nonce
        token = signing.dumps(
            {"u": user.pk, "e": user.email, "n": hashlib.sha256(nonce.encode()).hexdigest(), "r": req},
            salt=LINK_SALT,
        )
        link = f"{conf.issuer()}/oauth/authorize/link?{urlencode({'t': token})}"
        try:
            from tenants.envelopes import MagicLinkMailer

            MagicLinkMailer(user=user, link=link, expires_minutes=LINK_MAX_AGE // 60).send_now()
        except Exception:  # noqa: BLE001
            logger.exception("MCP sign-in link email failed for user %s", user.pk)
    return _render_step(request, req, notice=notice)


@require_GET
def signin_link_view(request: HttpRequest) -> HttpResponse:
    if not conf.enabled():
        return _page(request, "message", status=503, message="AI connections are turned off right now.")
    try:
        data = signing.loads(request.GET.get("t", ""), salt=LINK_SALT, max_age=LINK_MAX_AGE)
    except signing.SignatureExpired:
        return _page(request, "message", status=400, message="This sign-in link expired. Start again from your AI app.")
    except signing.BadSignature:
        return _page(request, "message", status=400, message="This sign-in link isn't valid.")
    nonce = request.session.get(SESSION_LINK_NONCE) or ""
    if not nonce or hashlib.sha256(nonce.encode()).hexdigest() != data.get("n"):
        return _page(
            request,
            "message",
            status=400,
            message="Open this link in the same browser where you started connecting your AI app.",
        )
    user = get_user_model().objects.filter(pk=data.get("u"), is_active=True).first()
    if user is None or user.email != data.get("e"):
        return _page(request, "message", status=400, message="This sign-in link isn't valid.")
    request.session.cycle_key()
    request.session.pop(SESSION_LINK_NONCE, None)
    request.session[SESSION_USER_KEY] = user.pk
    req = data.get("r") or {}
    return HttpResponseRedirect(
        "/oauth/authorize?" + urlencode({"req": signing.dumps(req, salt=REQUEST_SALT)})
    )


def _do_approve(request, req):
    user = _session_user(request)
    if user is None:
        return _render_step(request, req, error="Please sign in again.")
    tenant_uuid = request.POST.get("tenant", "")
    tenant = eligible_tenants(user).filter(uuid=tenant_uuid).first() if tenant_uuid else None
    if tenant is None:
        return _render_step(request, req, error="Choose a brand to connect.", status=400)
    client = resolve_client(req["client_id"])
    if client is None or not redirect_uri_matches(req["redirect_uri"], client.redirect_uris):
        return _page(request, "message", status=400, message="This AI app is no longer approved to connect.")
    code = _new_secret("spk_code_")
    with transaction.atomic():
        connection = (
            McpConnection.objects.filter(
                user=user, tenant=tenant, client=client, revoked_at__isnull=True
            ).first()
            or McpConnection.objects.create(user=user, tenant=tenant, client=client, scope=req["scope"])
        )
        McpAuthorizationCode.objects.create(
            code_hash=digest(code),
            connection=connection,
            redirect_uri=req["redirect_uri"],
            code_challenge=req["code_challenge"],
            resource=req["resource"],
            scope=req["scope"],
            expires_at=timezone.now() + timedelta(seconds=conf.auth_code_ttl_seconds()),
        )
    logger.info(
        "MCP connection approved user=%s tenant=%s client=%s", user.pk, tenant.pk, client.client_id
    )
    return _redirect_with(req["redirect_uri"], {"code": code, "state": req["state"]})


# ─── Token endpoint ────────────────────────────────────────────────


def _issue_tokens(connection: McpConnection, *, resource: str, scope: str) -> dict:
    now = timezone.now()
    access = _new_secret("spk_at_")
    refresh = _new_secret("spk_rt_")
    McpToken.objects.bulk_create(
        [
            McpToken(
                token_hash=digest(access),
                kind=McpToken.KIND_ACCESS,
                connection=connection,
                resource=resource,
                scope=scope,
                expires_at=now + timedelta(seconds=conf.access_token_ttl_seconds()),
            ),
            McpToken(
                token_hash=digest(refresh),
                kind=McpToken.KIND_REFRESH,
                connection=connection,
                resource=resource,
                scope=scope,
                expires_at=now + timedelta(seconds=conf.refresh_token_ttl_seconds()),
            ),
        ]
    )
    McpClient.objects.filter(pk=connection.client_id).update(last_used_at=now)
    return {
        "access_token": access,
        "token_type": "Bearer",
        "expires_in": conf.access_token_ttl_seconds(),
        "refresh_token": refresh,
        "scope": scope,
    }


@csrf_exempt
@require_POST
def token_view(request: HttpRequest) -> JsonResponse:
    if not conf.enabled():
        return _disabled()
    if _throttled(f"token:{_client_ip(request)}", 120, 60):
        return _oauth_error("slow_down", "Too many token requests.", 429)
    data = request.POST
    grant = data.get("grant_type", "")
    client_id = data.get("client_id", "")
    if not client_id and request.META.get("HTTP_AUTHORIZATION", "").lower().startswith("basic "):
        # Tolerate client_secret_basic style with an empty secret.
        try:
            raw = base64.b64decode(request.META["HTTP_AUTHORIZATION"][6:]).decode()
            client_id = raw.split(":", 1)[0]
        except Exception:  # noqa: BLE001
            client_id = ""
    if grant == "authorization_code":
        return _grant_code(data, client_id)
    if grant == "refresh_token":
        return _grant_refresh(data, client_id)
    return _oauth_error("unsupported_grant_type", "Use authorization_code or refresh_token.")


def _grant_code(data, client_id: str) -> JsonResponse:
    code = data.get("code", "")
    with transaction.atomic():
        row = (
            McpAuthorizationCode.objects.select_for_update()
            .select_related("connection", "connection__client")
            .filter(code_hash=digest(code))
            .first()
            if code
            else None
        )
        if row is None:
            return _oauth_error("invalid_grant", "Unknown authorization code.")
        connection = row.connection
        if row.used_at is not None:
            # Replay of a code: kill everything minted from it (RFC 6819 §4.4.1.1).
            connection.revoke(reason="authorization code replayed")
            return _oauth_error("invalid_grant", "Authorization code already used.")
        row.used_at = timezone.now()
        row.save(update_fields=["used_at"])
    if row.expires_at <= timezone.now():
        return _oauth_error("invalid_grant", "Authorization code expired.")
    if client_id != connection.client.client_id:
        return _oauth_error("invalid_grant", "Code was issued to a different client.")
    if data.get("redirect_uri", "") != row.redirect_uri:
        return _oauth_error("invalid_grant", "redirect_uri does not match.")
    if not _pkce_ok(data.get("code_verifier", ""), row.code_challenge):
        return _oauth_error("invalid_grant", "PKCE verification failed.")
    if not _canonical_resource_ok(data.get("resource")):
        return _oauth_error("invalid_target", f"Unknown resource. Use {conf.resource_url()}.")
    if not connection.is_active or connection.client.disabled:
        return _oauth_error("invalid_grant", "This connection was revoked.")
    body = _issue_tokens(connection, resource=row.resource, scope=row.scope)
    resp = JsonResponse(body)
    resp["Cache-Control"] = "no-store"
    resp["Pragma"] = "no-cache"
    return resp


def _grant_refresh(data, client_id: str) -> JsonResponse:
    raw = data.get("refresh_token", "")
    with transaction.atomic():
        token = (
            McpToken.objects.select_for_update()
            .select_related("connection", "connection__client")
            .filter(token_hash=digest(raw), kind=McpToken.KIND_REFRESH)
            .first()
            if raw
            else None
        )
        if token is None:
            return _oauth_error("invalid_grant", "Unknown refresh token.")
        connection = token.connection
        if token.used_at is not None:
            connection.revoke(reason="refresh token reused")
            logger.warning("MCP refresh token reuse; connection %s revoked", connection.pk)
            return _oauth_error("invalid_grant", "Refresh token already used.")
        if token.revoked_at is not None or token.expires_at <= timezone.now():
            return _oauth_error("invalid_grant", "Refresh token expired or revoked.")
        if client_id and client_id != connection.client.client_id:
            return _oauth_error("invalid_grant", "Refresh token was issued to a different client.")
        if not connection.is_active or connection.client.disabled:
            return _oauth_error("invalid_grant", "This connection was revoked.")
        if not _canonical_resource_ok(data.get("resource")):
            return _oauth_error("invalid_target", f"Unknown resource. Use {conf.resource_url()}.")
        from .access import user_may_use

        if not user_may_use(connection):
            connection.revoke(reason="user no longer has access to brand")
            return _oauth_error("invalid_grant", "This account no longer has access to this brand.")
        token.used_at = timezone.now()
        token.save(update_fields=["used_at"])
        # The old access tokens of this connection stay valid until expiry;
        # only the refresh token rotates.
        body = _issue_tokens(connection, resource=token.resource, scope=token.scope)
    resp = JsonResponse(body)
    resp["Cache-Control"] = "no-store"
    resp["Pragma"] = "no-cache"
    return resp


@csrf_exempt
@require_POST
def revoke_view(request: HttpRequest) -> HttpResponse:
    """RFC 7009. Revoking a refresh token ends the whole connection; revoking
    an access token ends that token. Always 200 for unknown tokens."""
    raw = request.POST.get("token", "")
    if raw:
        token = McpToken.objects.select_related("connection").filter(token_hash=digest(raw)).first()
        if token is not None:
            if token.kind == McpToken.KIND_REFRESH:
                token.connection.revoke(reason="revoked by the AI app")
            else:
                McpToken.objects.filter(pk=token.pk, revoked_at__isnull=True).update(
                    revoked_at=timezone.now()
                )
    resp = HttpResponse(status=200)
    resp["Cache-Control"] = "no-store"
    return resp
