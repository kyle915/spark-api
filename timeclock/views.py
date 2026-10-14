"""Public (no-login) clock endpoints for ``/clock/<token>``.

The link token is the only credential to open the page. Clock-in returns a
signed session token the phone keeps so Clock out needs no re-typing; a phone
that lost it clocks out by email instead. GPS is a single fix sent with each
action — nothing is tracked in between.

Routes (mounted under ``/api/public/``):

    GET  clock/<token>        → link + brand (+ the device's session)
    POST clock/<token>/in     → {sessionToken, session}
    POST clock/<token>/out    → {session}
"""

from __future__ import annotations

import json
import logging

from django.http import HttpRequest, HttpResponse, JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods

from events.checkin_views import _body, _client_ip, _err, _over_limit, _rate_limited
from tasks.views import _secret_ok
from timeclock import services
from timeclock.sheets import sync_session

logger = logging.getLogger(__name__)

SESSION_HEADER = "X-Clock-Session"


def _not_found() -> JsonResponse:
    return _err("This clock link doesn't exist. Ask your manager for the current one.",
                status=404, code="not_found")


def _clock_error(exc: services.ClockError) -> JsonResponse:
    return JsonResponse({"error": exc.code, "message": exc.message, **exc.extra}, status=exc.status)


def _throttled(request: HttpRequest, scope: str, token: str, *, ip_limit: int) -> bool:
    ip = _client_ip(request)
    return _over_limit(f"clock-{scope}-ip", ip, limit=ip_limit, window=300) or _over_limit(
        f"clock-{scope}-link", token, limit=2000, window=3600
    )


@csrf_exempt
@require_http_methods(["GET"])
def public_clock_context(request: HttpRequest, token: str) -> HttpResponse:
    if _throttled(request, "get", token, ip_limit=120):
        return _rate_limited()
    link = services.resolve_link(token)
    if link is None:
        return _not_found()
    payload = services.link_payload(link)
    session = services.read_session_token(request.headers.get(SESSION_HEADER, ""), link)
    payload["session"] = services.session_payload(session) if session else None
    return JsonResponse(payload)


@csrf_exempt
@require_http_methods(["POST"])
def public_clock_in(request: HttpRequest, token: str) -> HttpResponse:
    if _throttled(request, "in", token, ip_limit=20):
        return _rate_limited()
    link = services.resolve_link(token)
    if link is None:
        return _not_found()
    try:
        session = services.clock_in(
            link,
            _body(request),
            ip=_client_ip(request),
            user_agent=request.headers.get("User-Agent", ""),
        )
    except services.ClockError as exc:
        return _clock_error(exc)
    return JsonResponse(
        {
            "sessionToken": services.make_session_token(session),
            "session": services.session_payload(session),
        },
        status=201,
    )


@csrf_exempt
@require_http_methods(["POST"])
def public_clock_out(request: HttpRequest, token: str) -> HttpResponse:
    if _throttled(request, "out", token, ip_limit=20):
        return _rate_limited()
    link = services.resolve_link(token)
    if link is None:
        return _not_found()
    data = _body(request)
    session = None
    session_token = data.get("sessionToken") if isinstance(data.get("sessionToken"), str) else ""
    if session_token:
        session = services.read_session_token(session_token, link)
    try:
        if session is None and not data.get("email"):
            raise services.ClockError(
                "We lost track of your clock-in on this phone. Enter your email to clock out.",
                code="no_session",
                status=401,
            )
        session = services.clock_out(link, data, session=session)
    except services.ClockError as exc:
        return _clock_error(exc)
    return JsonResponse({"session": services.session_payload(session)})


@csrf_exempt
@require_http_methods(["POST"])
def clock_sheet_sync_task(request: HttpRequest) -> HttpResponse:
    """Cloud Tasks handler: mirror one session. 500 lets the queue retry."""
    if not _secret_ok(request):
        return HttpResponse("Forbidden", status=403)
    try:
        body = json.loads((request.body or b"").decode("utf-8") or "{}")
    except (ValueError, UnicodeDecodeError):
        return JsonResponse({"ok": False, "error": "bad-json"})
    session_id = body.get("session_id")
    if not isinstance(session_id, int):
        return JsonResponse({"ok": False, "error": "bad-payload"})
    if sync_session(session_id):
        return JsonResponse({"ok": True})
    return JsonResponse({"ok": False}, status=500)
