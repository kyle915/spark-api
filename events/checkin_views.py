"""Public (no-JWT) endpoints for the web check-in flow.

The shareable link ``/checkin/<code>`` carries an event's ``walkup_code``.
Possession of the code lets a BA start a check-in; once they identify
themselves the ``identify`` endpoint mints a signed session token that
authorizes the follow-up calls (clock, photo upload URL, recap submit) for that
one (event, BA) pair — same signed-token, cookie-free pattern as the client-live
page and campaign report. All logic lives in ``ambassadors/checkin_web.py``;
these views are thin HTTP wrappers (parse → authorize → delegate → JSON).

Routes (mounted under ``/api/public/`` in ``events/urls.py``):

    GET  checkin/<code>                → event + brand + template (+ session state)
    POST checkin/<code>/identify       → {sessionToken, session}
    POST checkin/<code>/clock          → {clock}
    POST checkin/<code>/upload-url     → {uploadUrl, blobName}
    POST checkin/<code>/recap          → {success}
"""
from __future__ import annotations

import json
import logging
import re
import secrets

from django.core.cache import cache
from django.utils import timezone as dj_tz
from django.http import HttpRequest, HttpResponse, JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods

from ambassadors import checkin_web
from events.checkin_tokens import (
    BadSignature,
    SignatureExpired,
    make_checkin_session_token,
    read_checkin_session_token,
)

logger = logging.getLogger(__name__)

# Photo uploads only — the check-in page never uploads anything else.
_ALLOWED_UPLOAD_TYPES = {
    "image/jpeg",
    "image/jpg",
    "image/png",
    "image/heic",
    "image/heif",
    "image/webp",
}


def _err(message: str, status: int = 400, code: str = "invalid") -> JsonResponse:
    return JsonResponse({"error": code, "message": message}, status=status)


def _body(request: HttpRequest) -> dict:
    try:
        raw = request.body.decode("utf-8") or "{}"
        data = json.loads(raw)
        return data if isinstance(data, dict) else {}
    except (ValueError, UnicodeDecodeError):
        return {}


def _client_ip(request: HttpRequest) -> str:
    """Best-effort client IP. Cloud Run sits behind a proxy, so the real client
    is the first hop in X-Forwarded-For; fall back to REMOTE_ADDR."""
    xff = request.META.get("HTTP_X_FORWARDED_FOR", "")
    if xff:
        return xff.split(",")[0].strip() or "?"
    return request.META.get("REMOTE_ADDR", "") or "?"


def _over_limit(scope: str, ident: str, *, limit: int, window: int) -> bool:
    """True if (scope, ident) has exceeded `limit` hits in the last `window`s.

    Uses the default cache (LocMemCache in prod — per-instance), so this is a
    speed bump against a single-source flood layered on top of the
    pending-review gate + code expiry, not a hard global quota. Cache trouble
    never blocks a legitimate check-in (fails open)."""
    key = f"checkin:rl:{scope}:{ident}"
    try:
        cache.add(key, 0, timeout=window)
        count = cache.incr(key)
    except ValueError:
        # Key expired between add and incr — start a fresh window.
        cache.set(key, 1, timeout=window)
        count = 1
    except Exception:  # noqa: BLE001 — never block on cache failure
        return False
    return count > limit


def _rate_limited() -> JsonResponse:
    return _err(
        "Too many attempts. Wait a minute and try again.",
        status=429,
        code="rate_limited",
    )


def _parse_iso_date(value: str):
    """Parse a YYYY-MM-DD string from the store step, or None."""
    from datetime import date as _date

    try:
        y, m, d = (value or "").split("-")
        return _date(int(y), int(m), int(d))
    except Exception:  # noqa: BLE001 — any malformed input is just "no date"
        return None


def _load_event(code: str):
    """Resolve the code to a live event, or ``None``."""
    from asgiref.sync import async_to_sync  # noqa: F401 — not needed; kept sync

    return checkin_web.resolve_event_by_code(code)


def _load_session(code: str, token: str):
    """Return ``(event, ambassador)`` for a valid session token; otherwise
    ``(None, error_response)``.

    Two link shapes land here. An EVENT code names one event, so the token's
    event must equal it. A TENANT code names no event — the session token minted
    at identify carries whichever event identify found-or-created — so the check
    becomes "does that event belong to this tenant?". Both are equally strict:
    a token can never reach an event outside the link's own scope.
    """
    from ambassadors.models import Ambassador
    from events.models import Event

    kind, target = checkin_web.resolve_checkin_target(code)
    if kind is None:
        return None, _err("This link is no longer active.", status=404, code="not_found")
    if not token:
        return None, _err("Your check-in session is missing. Reload the link.", status=401, code="no_session")
    try:
        event_id, amb_id = read_checkin_session_token(token)
    except SignatureExpired:
        return None, _err("Your check-in session expired. Reload the link.", status=401, code="expired")
    except (BadSignature, ValueError):
        return None, _err("Invalid check-in session.", status=401, code="bad_session")

    if kind == "event":
        event = target
        if event_id != event.id:
            return None, _err("This session doesn't match the event.", status=401, code="mismatch")
    else:
        event = (
            Event.objects.select_related("tenant", "request", "retailer", "location", "state", "timezone")
            .filter(id=event_id)
            .first()
        )
        if event is None:
            return None, _err("Couldn't find your check-in.", status=404, code="not_found")
        if getattr(event, "tenant_id", None) != target.id:
            return None, _err("This session doesn't match the link.", status=401, code="mismatch")

    ambassador = (
        Ambassador.objects.select_related("user").filter(id=amb_id).first()
    )
    if ambassador is None:
        return None, _err("Couldn't find your check-in profile.", status=404, code="no_profile")
    return (event, ambassador), None


# --------------------------------------------------------------------------
# GET context
# --------------------------------------------------------------------------
@csrf_exempt
@require_http_methods(["GET"])
def public_checkin_context(request: HttpRequest, code: str) -> HttpResponse:
    kind, target = checkin_web.resolve_checkin_target(code)
    if kind is None:
        return _err(
            "This check-in link isn't active. Ask your lead for a current one.",
            status=404,
            code="not_found",
        )

    # Standing tenant link: there is no event yet. Hand back the brand + the
    # store autocomplete and let the page ask for store + date first; identify
    # is what resolves an actual event.
    if kind == "tenant":
        token = request.headers.get("X-Checkin-Session") or ""
        if token:
            loaded, err = _load_session(code, token)
            if err is None:
                event, ambassador = loaded
                payload = checkin_web.build_public_context(event, ambassador)
                payload["mode"] = "tenant"
                return JsonResponse(payload)
        try:
            return JsonResponse(checkin_web.build_tenant_context(target))
        except Exception:  # noqa: BLE001
            logger.exception("checkin tenant context failed code=%s", code)
            return _err("Couldn't load this check-in.", status=500, code="server")

    event = target
    ambassador = None
    # Read the session token from a header, NOT the query string — a bearer
    # token in a URL leaks into access logs, browser history, and Referer
    # headers. The POST endpoints already carry it in the body.
    token = request.headers.get("X-Checkin-Session") or ""
    if token:
        try:
            event_id, amb_id = read_checkin_session_token(token)
            if event_id == event.id:
                from ambassadors.models import Ambassador

                ambassador = (
                    Ambassador.objects.select_related("user").filter(id=amb_id).first()
                )
        except (SignatureExpired, BadSignature, ValueError):
            ambassador = None
    try:
        payload = checkin_web.build_public_context(event, ambassador)
    except Exception:  # noqa: BLE001
        logger.exception("checkin context build failed code=%s", code)
        return _err("Couldn't load this check-in.", status=500, code="server")
    return JsonResponse(payload)


# --------------------------------------------------------------------------
# POST identify
# --------------------------------------------------------------------------
@csrf_exempt
@require_http_methods(["POST"])
def public_checkin_identify(request: HttpRequest, code: str) -> HttpResponse:
    ip = _client_ip(request)
    # Per-IP burst guard first — it's the real flood defence and costs nothing.
    if _over_limit("identify-ip", ip, limit=10, window=300):
        return _rate_limited()

    kind, target = checkin_web.resolve_checkin_target(code)
    if kind is None:
        return _err("This check-in link isn't active.", status=404, code="not_found")

    # Then a per-code cap on how many stub accounts one link can spawn (the
    # account-creating endpoint is the worst vector). The ceiling has to depend
    # on what the code IS: an EVENT code serves one activation, so 50/hour is
    # generous. A TENANT code is a standing link the whole field shares across
    # hundreds of events — 50/hour would throttle real BAs on a busy morning,
    # which is a self-inflicted outage, not security. 500/hour still bounds
    # abuse (~8 sign-ups a minute, sustained) without ever touching real use.
    code_cap = 500 if kind == "tenant" else 50
    if _over_limit("identify-code", code, limit=code_cap, window=3600):
        return _rate_limited()

    data = _body(request)
    first_name = (data.get("firstName") or data.get("first_name") or "").strip()
    last_name = (data.get("lastName") or data.get("last_name") or "").strip()
    phone = (data.get("phone") or "").strip()
    email = (data.get("email") or "").strip() or None

    # If they typed a single "full name", split it.
    if not last_name and " " in first_name:
        first_name, _, last_name = first_name.partition(" ")
        first_name = first_name.strip()
        last_name = last_name.strip()

    if not first_name:
        return _err("Enter your name so we can credit your work.")
    if not phone:
        return _err("Enter a phone number so your lead can confirm you.")

    # Identify the BA FIRST. On a standing tenant link the event may not exist
    # yet and Event.created_by is NOT NULL, so we need a real user in hand
    # before creating one — and attributing it to the BA who opened it is what
    # the Walk-ups queue wants to show anyway.
    try:
        ambassador, _ = checkin_web.get_or_create_checkin_ambassador(
            first_name=first_name, last_name=last_name, phone=phone, email=email
        )
    except Exception:  # noqa: BLE001
        logger.exception("checkin identify failed code=%s", code)
        return _err("Couldn't start your check-in. Try again.", status=500, code="server")

    # A standing tenant link carries no event, so the BA supplies the store and
    # the date and we find-or-create it. Several BAs at the same store on the
    # same day resolve to the SAME event (see find_or_create_walkin_event).
    event = target if kind == "event" else None
    if kind == "tenant":
        address = (data.get("address") or data.get("storeAddress") or "").strip()
        store_name = (data.get("storeName") or data.get("eventName") or "").strip()
        date_raw = (data.get("eventDate") or data.get("date") or "").strip()
        if not address:
            return _err("Enter the store address so your work is logged to the right place.")
        on_date = _parse_iso_date(date_raw)
        if on_date is None:
            return _err("Pick the date you worked (YYYY-MM-DD).")
        if abs((on_date - dj_tz.localdate()).days) > 14:
            return _err("That date is too far from today. Ask your lead to log it.")
        try:
            event, _new = checkin_web.find_or_create_walkin_event(
                tenant=target, store_name=store_name, address=address,
                on_date=on_date, actor=ambassador.user,
            )
        except ValueError as exc:
            return _err(str(exc))
        except Exception:  # noqa: BLE001
            logger.exception("checkin tenant event resolve failed code=%s", code)
            return _err("Couldn't set up your event. Try again.", status=500, code="server")

    try:
        amb_event, _created = checkin_web.ensure_walkup_booking(
            event, ambassador, actor=ambassador.user
        )
    except Exception:  # noqa: BLE001
        logger.exception("checkin booking failed code=%s", code)
        return _err("Couldn't start your check-in. Try again.", status=500, code="server")

    token = make_checkin_session_token(event.id, ambassador.id)
    payload = checkin_web.build_public_context(event, ambassador)
    if kind == "tenant":
        payload["mode"] = "tenant"
    payload["sessionToken"] = token
    payload["ambassadorEventUuid"] = str(amb_event.uuid)
    return JsonResponse(payload)


# --------------------------------------------------------------------------
# POST clock in / out
# --------------------------------------------------------------------------
@csrf_exempt
@require_http_methods(["POST"])
def public_checkin_clock(request: HttpRequest, code: str) -> HttpResponse:
    if _over_limit("clock-ip", _client_ip(request), limit=40, window=300):
        return _rate_limited()
    data = _body(request)
    loaded, err = _load_session(code, data.get("session") or "")
    if err is not None:
        return err
    event, ambassador = loaded

    kind_raw = (data.get("kind") or "").strip().lower()
    if kind_raw in ("in", "clock_in", "clockin"):
        source_name = "clock_in"
    elif kind_raw in ("out", "clock_out", "clockout"):
        source_name = "clock_out"
    else:
        return _err("Tell us whether you're clocking in or out.")

    coordinates = None
    lat, lng = data.get("latitude"), data.get("longitude")
    if lat is not None and lng is not None:
        try:
            coordinates = [float(lat), float(lng)]
        except (TypeError, ValueError):
            coordinates = None

    try:
        amb_event, _created = checkin_web.ensure_walkup_booking(
            event, ambassador, actor=ambassador.user
        )
        checkin_web.record_attendance(
            amb_event=amb_event,
            kind=source_name,
            coordinates=coordinates,
            actor=ambassador.user,
        )
        # Same coordinates, but PLOTTED: Attendance records the punch,
        # LocationPing is what the admin map and GPS trail actually read.
        checkin_web.record_location_ping(
            ambassador=ambassador,
            event=event,
            coordinates=coordinates,
            source=source_name,
        )
        # First clock-IN → email admins so the pending walk-up gets seen.
        if source_name == "clock_in":
            checkin_web.notify_checkin_landed_if_first(event, ambassador)
        state = checkin_web.clock_state(
            ambassador_id=ambassador.id, event_id=event.id
        )
    except Exception:  # noqa: BLE001
        logger.exception("checkin clock failed code=%s kind=%s", code, source_name)
        return _err("Couldn't record that. Try again.", status=500, code="server")
    return JsonResponse({"clock": state})


# --------------------------------------------------------------------------
# POST upload-url (signed GCS PUT for one photo)
# --------------------------------------------------------------------------
_SAFE_NAME = re.compile(r"[^A-Za-z0-9._-]+")


@csrf_exempt
@require_http_methods(["POST"])
def public_checkin_upload_url(request: HttpRequest, code: str) -> HttpResponse:
    if _over_limit("upload-ip", _client_ip(request), limit=80, window=300):
        return _rate_limited()
    data = _body(request)
    loaded, err = _load_session(code, data.get("session") or "")
    if err is not None:
        return err
    event, ambassador = loaded

    content_type = (data.get("contentType") or data.get("content_type") or "").strip().lower()
    if content_type not in _ALLOWED_UPLOAD_TYPES:
        return _err("Only photo uploads are allowed here.")
    filename = (data.get("filename") or "photo.jpg").strip()
    safe = _SAFE_NAME.sub("-", filename)[-80:] or "photo.jpg"
    blob_name = (
        f"recap_files/checkin/{event.uuid}/"
        f"{secrets.token_hex(8)}-{safe}"
    )

    try:
        from utils.gcs import generate_upload_url

        upload_url = generate_upload_url(blob_name, content_type=content_type)
    except Exception:  # noqa: BLE001
        logger.exception("checkin upload-url failed code=%s", code)
        return _err("Couldn't prepare the photo upload. Try again.", status=500, code="server")
    return JsonResponse({"uploadUrl": upload_url, "blobName": blob_name})


# --------------------------------------------------------------------------
# POST recap
# --------------------------------------------------------------------------
@csrf_exempt
@require_http_methods(["POST"])
def public_checkin_recap(request: HttpRequest, code: str) -> HttpResponse:
    if _over_limit("recap-ip", _client_ip(request), limit=20, window=300):
        return _rate_limited()
    data = _body(request)
    loaded, err = _load_session(code, data.get("session") or "")
    if err is not None:
        return err
    event, ambassador = loaded

    template = checkin_web.resolve_template_for_event(event)
    if template is None:
        return _err(
            "This event has no recap form set up. Ask your lead.",
            status=409,
            code="no_template",
        )

    field_values = data.get("fieldValues") or data.get("field_values") or []
    files = data.get("files") or []
    product_samples = data.get("productSamples") or data.get("product_samples") or []
    total_engagements = data.get("totalEngagements")
    if total_engagements is not None:
        try:
            total_engagements = int(total_engagements)
        except (TypeError, ValueError):
            total_engagements = None

    if not isinstance(field_values, list) or not isinstance(files, list):
        return _err("Malformed recap.")

    try:
        checkin_web.submit_checkin_recap(
            event=event,
            ambassador=ambassador,
            template=template,
            field_values=field_values,
            files=files,
            total_engagements=total_engagements,
            product_samples=product_samples if isinstance(product_samples, list) else [],
        )
    except Exception:  # noqa: BLE001
        logger.exception("checkin recap submit failed code=%s", code)
        return _err("Couldn't submit your recap. Try again.", status=500, code="server")
    return JsonResponse(
        {
            "success": True,
            "message": "Recap submitted. Thanks!",
            "pendingReview": not bool(getattr(ambassador, "is_active", False)),
        }
    )


# --------------------------------------------------------------------------
# POST where  (reverse-geocode the BA's phone GPS -> a street address)
# --------------------------------------------------------------------------
#
# Used by the standing tenant link's "Use my current location" button, which
# fires BEFORE identify — there is no session yet, so this cannot require one.
# That is safe because it reveals nothing: the caller supplies the coordinates
# and gets back the address of the coordinates they already had. It is still
# rate-limited per IP so it can't be used as a free bulk geocoder.
@csrf_exempt
@require_http_methods(["POST"])
def public_checkin_where(request: HttpRequest, code: str) -> HttpResponse:
    if _over_limit("where-ip", _client_ip(request), limit=30, window=300):
        return _rate_limited()
    # Still require the code to name a real link, so this isn't an open
    # endpoint hanging off the API for anyone who finds the path.
    kind, _target = checkin_web.resolve_checkin_target(code)
    if kind is None:
        return _err(
            "This check-in link isn't active. Ask your lead for a current one.",
            status=404,
            code="not_found",
        )

    data = _body(request)
    lat, lng = data.get("latitude"), data.get("longitude")
    if lat is None or lng is None:
        return _err("We didn't get a location from your phone.")

    from utils.geocoding import photon_reverse

    try:
        hit = photon_reverse(lat, lng)
    except Exception:  # noqa: BLE001 — belt and braces; photon_reverse
        logger.exception("reverse geocode blew up lat=%s lng=%s", lat, lng)
        hit = None
    if not hit:
        # Not an error the BA should be blocked by — they can type it.
        return JsonResponse({"address": None, "message": "Couldn't name that spot."})
    return JsonResponse(hit)


# --------------------------------------------------------------------------
# POST ping  (periodic location while the BA is on the clock)
# --------------------------------------------------------------------------
#
# The page posts here on a timer while it is OPEN and the BA is clocked in.
# IMPORTANT and deliberately limited: a mobile browser suspends timers when
# the tab is backgrounded or the screen locks, so this yields an on-site trail
# while the BA is actually looking at the page, NOT continuous background
# tracking. Only the native app can do the latter (see spark-mobile's
# locationTracker). Do not describe this to clients as full-shift tracking.
@csrf_exempt
@require_http_methods(["POST"])
def public_checkin_ping(request: HttpRequest, code: str) -> HttpResponse:
    if _over_limit("ping-ip", _client_ip(request), limit=240, window=3600):
        return _rate_limited()
    data = _body(request)
    loaded, err = _load_session(code, data.get("session") or "")
    if err is not None:
        return err
    event, ambassador = loaded

    lat, lng = data.get("latitude"), data.get("longitude")
    if lat is None or lng is None:
        return _err("No location supplied.")
    try:
        coordinates = [float(lat), float(lng)]
    except (TypeError, ValueError):
        return _err("No location supplied.")

    # Only record while genuinely on the clock. Otherwise a page left open in
    # a pocket after clock-out would keep reporting the BA's whereabouts,
    # which is both useless to ops and not something we should collect.
    state = checkin_web.clock_state(ambassador_id=ambassador.id, event_id=event.id)
    if state.get("state") != "clocked_in":
        return JsonResponse({"recorded": False, "reason": "not_clocked_in"})

    ping = checkin_web.record_location_ping(
        ambassador=ambassador,
        event=event,
        coordinates=coordinates,
        source="foreground",
    )
    return JsonResponse({"recorded": bool(ping)})
