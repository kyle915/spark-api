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
    POST checkin/<code>/clear-clock    → {cleared, clockedOut, clock}
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
from ambassadors.payable_mileage import NeedsPayableMileage
from events.checkin_tokens import (
    BadSignature,
    CHECKIN_SESSION_MAX_AGE_SECONDS,
    CHECKIN_TENANT_SESSION_MAX_AGE_SECONDS,
    SignatureExpired,
    make_checkin_session_token,
    read_checkin_session_token,
)

logger = logging.getLogger(__name__)

# How far the public check-in link lets a BA date their own shift.
# Backdating is the normal case; forward-dating is almost always a typo.
CHECKIN_MAX_PAST_DAYS = 90
CHECKIN_MAX_FUTURE_DAYS = 14

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


def _session_error_code(err_response: HttpResponse) -> str:
    """Pull the ``error`` code off a ``_load_session`` failure response.

    Standing-link GET used to fall through to a bare tenant payload when the
    bearer failed, with no signal. The page then wiped localStorage and the BA
    looked "not clocked in" even when Attendance still had their punch
    (Michelle Chin / Feel Free, Aug 2026 — left for the photo-release QR and
    came back to identify). Same failure mode hits every standing code
    (LD / Torch / Brew Dr / …). Surface the reason so the page only drops a
    truly dead token and can resume an open shift from identity.
    """
    try:
        data = json.loads(err_response.content.decode("utf-8") or "{}")
        code = (data.get("error") or "").strip() if isinstance(data, dict) else ""
        return code or "bad_session"
    except (ValueError, UnicodeDecodeError, AttributeError):
        return "bad_session"


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


def _is_recap_only(code: str) -> bool:
    kind, target = checkin_web.resolve_checkin_target(code)
    return kind == "tenant" and checkin_web.is_recap_only_code(code, target)


def _stamp_recap_only(payload: dict, code: str, tenant) -> dict:
    """Mark a standing-link payload as recap-only and drop clock leftovers."""
    recap_only = checkin_web.is_recap_only_code(code, tenant)
    payload["recapOnly"] = recap_only
    if recap_only:
        payload["unfiledShifts"] = []
    return payload


def _reject_clock_on_recap_link(code: str):
    if _is_recap_only(code):
        return _err(
            "This link is for filing a recap only — it has no time clock.",
            status=403,
            code="recap_only",
        )
    return None


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
    # Standing links keep a session long enough to file a late recap
    # (Friday's shift, Sunday night). Per-event codes stay at 2 days.
    max_age = (
        CHECKIN_TENANT_SESSION_MAX_AGE_SECONDS
        if kind == "tenant"
        else CHECKIN_SESSION_MAX_AGE_SECONDS
    )
    try:
        event_id, amb_id = read_checkin_session_token(token, max_age=max_age)
    except SignatureExpired:
        expired = (
            "Your check-in session expired. Start over and pick the date you worked."
            if kind == "tenant"
            else "Your check-in session expired. Reload the link."
        )
        return None, _err(expired, status=401, code="expired")
    except (BadSignature, ValueError):
        return None, _err("Invalid check-in session.", status=401, code="bad_session")

    if kind == "event":
        event = target
        if event_id != event.id:
            return None, _err("This session doesn't match the event.", status=401, code="mismatch")
    else:
        event = (
            Event.objects.select_related("tenant", "request", "retailer", "location", "state", "timezone", "event_type")
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
        session_error = None
        if token:
            loaded, err = _load_session(code, token)
            if err is None:
                event, ambassador = loaded
                payload = checkin_web.build_public_context(event, ambassador)
                payload["mode"] = "tenant"
                payload["unfiledShifts"] = checkin_web.unfiled_shifts_for(
                    ambassador=ambassador, tenant=target
                )
                return JsonResponse(_stamp_recap_only(payload, code, target))
            session_error = _session_error_code(err)
        try:
            recap_only = checkin_web.is_recap_only_code(code, target)
            payload = checkin_web.build_tenant_context(target, recap_only=recap_only)
            if session_error:
                payload["sessionError"] = session_error
            return JsonResponse(payload)
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

    recap_only = kind == "tenant" and checkin_web.is_recap_only_code(code, target)

    if not first_name:
        return _err("Enter your name so we can credit your work.")
    if not phone:
        if recap_only:
            phone = checkin_web.recap_only_identity_phone(
                first_name=first_name, last_name=last_name, email=email
            )
        else:
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
    on_date = None
    if kind == "tenant":
        date_raw = (data.get("eventDate") or data.get("date") or "").strip()
        on_date = _parse_iso_date(date_raw)
        if date_raw and on_date is None:
            return _err("Pick the date you worked (YYYY-MM-DD).")
        # Same-day open punch: resume even if they retyped the store
        # (lost session used to fork a second event). A leftover punch
        # from another calendar day must NOT win — Feel Free Alicia
        # picked Wednesday and landed on Sunday's still-open market.
        resumed = (
            None
            if recap_only
            else checkin_web.open_shift_event_for(
                ambassador=ambassador, tenant=target, on_date=on_date
            )
        )
        if resumed is not None:
            event = resumed
            logger.info(
                "checkin identify resumed open shift ambassador=%s event=%s on_date=%s",
                ambassador.id, resumed.id, on_date,
            )
    if kind == "tenant" and event is None:
        address = (data.get("address") or data.get("storeAddress") or "").strip()
        store_name = (data.get("storeName") or data.get("eventName") or "").strip()

        # ROAMING brands pick a market; the market becomes the event's location
        # key, so everyone working Austin today shares one event instead of
        # forking one per typed address. Where they actually sampled is
        # captured as SamplingStops during the shift.
        from tenants.models import Tenant

        if checkin_web.tenant_location_mode(target) == Tenant.CHECKIN_LOCATION_MARKET:
            market = (data.get("market") or "").strip()
            allowed = checkin_web.tenant_markets(target)
            if not market:
                return _err("Pick your market so your work is logged to the right place.")
            # Match case-insensitively but STORE the canonical spelling — the
            # event key is the normalized market, and two spellings would fork
            # the very event this mode exists to keep single.
            canon = next(
                (m for m in allowed if m.strip().lower() == market.lower()), None
            )
            if allowed and canon is None:
                return _err("Pick your market from the list.")
            address = canon or market
            store_name = ""
        elif recap_only:
            if not store_name:
                return _err("Enter the store name.")
            if not address:
                return _err("Enter the store address.")
        elif not address:
            return _err("Enter the store address so your work is logged to the right place.")
        if on_date is None:
            return _err("Pick the date you worked (YYYY-MM-DD).")
        # Asymmetric on purpose. Backdating is ordinary — a BA writes the recap
        # up on the drive home, or the brand closes out last month's paperwork
        # a few weeks late — so the past side is a full quarter. Forward-dating
        # is not: you cannot recap work you have not done yet, and the only
        # legitimate reason to be a day or two ahead is a shift that crosses
        # midnight or a phone in a different timezone from the server.
        #
        # It stays bounded rather than open because a mistyped year (2025 for
        # 2026) would otherwise create an event a year deep in the tracker,
        # where it silently lands in the wrong KPI period and nobody looks.
        delta = (on_date - dj_tz.localdate()).days
        if delta > CHECKIN_MAX_FUTURE_DAYS:
            return _err(
                "That date is in the future. Pick the day you actually worked."
            )
        if -delta > CHECKIN_MAX_PAST_DAYS:
            return _err(
                f"That date is more than {CHECKIN_MAX_PAST_DAYS} days ago. "
                "Ask your lead to log it."
            )

        # WHICH PROGRAM. A brand running more than one off the same link asks
        # the BA ("Retail Sampling" or "Event Activation"), and the answer picks
        # their recap form via the event's type. Resolved tenant-scoped, so a
        # forged id can't reach another brand's type — or, through it, another
        # brand's template. Anything unresolvable is treated as unanswered and
        # falls through to the tenant's pinned default, which is exactly what
        # the link did before the question existed.
        chosen_type = checkin_web.resolve_checkin_event_type(
            target, data.get("eventTypeId") or data.get("event_type_id")
        )
        if chosen_type is None and len(checkin_web.selectable_event_types(target)) > 1:
            logger.info(
                "checkin identify: no valid event type on a multi-program link "
                "code=%s raw=%r — using the tenant default",
                code, data.get("eventTypeId"),
            )
        # Prefer a shift this BA already clocked that day. Feel Free shares
        # one event per market per day; landing on that event is what lets
        # Rocio file Friday's recap on Sunday instead of inventing a new one.
        #
        # No punch is not a refusal. Standing walk-up links (KKC, Torch BA,
        # Feel Free, TH-AGENCY) are self-serve: typed/GPS location + date
        # mint or join the walk-in event. The old "must have clocked in"
        # gate 400'd KKC Start check-in on a Boston address the BA had
        # never punched — including "today" when the phone date is still
        # yesterday in UTC.
        existing = checkin_web.existing_shift_event_for(
            ambassador=ambassador,
            tenant=target,
            on_date=on_date,
            address=address,
        )
        if existing is not None:
            event = existing
        else:
            try:
                event, _new = checkin_web.find_or_create_walkin_event(
                    tenant=target, store_name=store_name, address=address,
                    on_date=on_date, actor=ambassador.user, event_type=chosen_type,
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
        payload["unfiledShifts"] = checkin_web.unfiled_shifts_for(
            ambassador=ambassador, tenant=target
        )
        _stamp_recap_only(payload, code, target)
    payload["sessionToken"] = token
    payload["ambassadorEventUuid"] = str(amb_event.uuid)
    return JsonResponse(payload)


# --------------------------------------------------------------------------
# POST unfiled recaps (standing link — list shifts still missing a recap)
# --------------------------------------------------------------------------
@csrf_exempt
@require_http_methods(["POST"])
def public_checkin_unfiled_recaps(request: HttpRequest, code: str) -> HttpResponse:
    """Shifts this BA already clocked that still need a recap.

    Used by the standing identify step so a BA who forgot Friday can tap
    that day instead of defaulting to today and getting stuck. Identity
    is the same phone-keyed stub identify uses — no session required.
    """
    if _over_limit("unfiled-ip", _client_ip(request), limit=20, window=300):
        return _rate_limited()

    kind, target = checkin_web.resolve_checkin_target(code)
    if kind != "tenant" or checkin_web.is_recap_only_code(code, target):
        return JsonResponse({"shifts": []})

    data = _body(request)
    phone = (data.get("phone") or "").strip()
    if not phone:
        return JsonResponse({"shifts": []})

    ambassador = checkin_web.find_checkin_ambassador(phone=phone)
    if ambassador is None:
        return JsonResponse({"shifts": []})

    return JsonResponse(
        {
            "shifts": checkin_web.unfiled_shifts_for(
                ambassador=ambassador, tenant=target
            )
        }
    )


# --------------------------------------------------------------------------
# POST clock in / out
# --------------------------------------------------------------------------
@csrf_exempt
@require_http_methods(["POST"])
def public_checkin_clock(request: HttpRequest, code: str) -> HttpResponse:
    if _over_limit("clock-ip", _client_ip(request), limit=40, window=300):
        return _rate_limited()
    blocked = _reject_clock_on_recap_link(code)
    if blocked is not None:
        return blocked
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

    clock_time = None
    if source_name == "clock_in":
        raw_when = (
            data.get("clockedInAt")
            or data.get("clocked_in_at")
            or data.get("occurredAt")
        )
        try:
            clock_time = checkin_web.parse_client_clock_time(raw_when)
        except checkin_web.ClientClockTimeError as exc:
            return _err(str(exc), status=400, code=exc.reason)

    # Already on the clock: a queued flush (or a double-tap) must not insert
    # a second clock_in. Same for an idempotency key the client retries.
    state = checkin_web.clock_state(
        ambassador_id=ambassador.id, event_id=event.id
    )
    idem = (data.get("idempotencyKey") or data.get("idempotency_key") or "")
    idem = str(idem).strip()[:80]
    idem_key = (
        f"checkin-clock-idemp:{event.id}:{ambassador.id}:{idem}" if idem else ""
    )
    if source_name == "clock_in" and state.get("state") == "clocked_in":
        return JsonResponse({"clock": state, "alreadyIn": True})
    if source_name == "clock_in" and idem_key and cache.get(idem_key):
        return JsonResponse({"clock": state, "alreadyIn": True, "replayed": True})

    try:
        amb_event, _created = checkin_web.ensure_walkup_booking(
            event, ambassador, actor=ambassador.user
        )
        checkin_web.record_attendance(
            amb_event=amb_event,
            kind=source_name,
            coordinates=coordinates,
            actor=ambassador.user,
            clock_time=clock_time,
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
            if idem_key:
                cache.set(idem_key, 1, timeout=24 * 3600)
        state = checkin_web.clock_state(
            ambassador_id=ambassador.id, event_id=event.id
        )
    except Exception:  # noqa: BLE001
        logger.exception("checkin clock failed code=%s kind=%s", code, source_name)
        return _err("Couldn't record that. Try again.", status=500, code="server")
    return JsonResponse({"clock": state})


# --------------------------------------------------------------------------
# POST clear leftover clock-in (standing link — start a new day)
# --------------------------------------------------------------------------
@csrf_exempt
@require_http_methods(["POST"])
def public_checkin_clear_clock(request: HttpRequest, code: str) -> HttpResponse:
    """Close a leftover open punch so the BA can start today's check-in.

    Does not delete recaps. Recap-only agency links have no clock — still
    200 so the page can drop the restored session and pick a new date.
    """
    if _over_limit("clear-clock-ip", _client_ip(request), limit=40, window=300):
        return _rate_limited()
    data = _body(request)
    loaded, err = _load_session(code, data.get("session") or "")
    if err is not None:
        return err
    event, ambassador = loaded
    if _is_recap_only(code):
        state = checkin_web.clock_state(
            ambassador_id=ambassador.id, event_id=event.id
        )
        return JsonResponse(
            {"cleared": True, "clockedOut": False, "clock": state}
        )
    try:
        result = checkin_web.abandon_open_clock(
            ambassador=ambassador, event=event
        )
    except Exception:  # noqa: BLE001
        logger.exception("checkin clear-clock failed code=%s", code)
        return _err("Couldn't clear that clock-in. Try again.", status=500, code="server")
    logger.info(
        "checkin clear-clock ambassador=%s event=%s clocked_out=%s",
        ambassador.id,
        event.id,
        result.get("clockedOut"),
    )
    return JsonResponse(result)


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

    # Standing tenant links may file more than one recap on the same event
    # (same market, same day). Per-event codes stay one-per-activation.
    kind, _target = checkin_web.resolve_checkin_target(code)
    force_new = bool(data.get("forceNew") or data.get("asNew"))
    if kind != "tenant":
        force_new = False
    elif checkin_web.is_recap_only_code(code, _target):
        # Agency filers submit many recaps on the same store/day without a
        # clock; never overwrite the previous filing.
        force_new = True

    shift_label = (
        data.get("shiftLabel") or data.get("shift_label") or ""
    )
    if isinstance(shift_label, str):
        shift_label = shift_label.strip() or None
    else:
        shift_label = None
    if force_new and (
        data.get("secondShift") or data.get("asSecondShift") or data.get("second_shift")
    ):
        shift_label = shift_label or checkin_web.SECOND_SHIFT_LABEL

    try:
        checkin_web.submit_checkin_recap(
            event=event,
            ambassador=ambassador,
            template=template,
            field_values=field_values,
            files=files,
            total_engagements=total_engagements,
            product_samples=product_samples if isinstance(product_samples, list) else [],
            force_new=force_new,
            third_party=checkin_web.is_recap_only_code(code, _target),
            shift_label=shift_label,
        )
    except checkin_web.RecapNeedsAPhoto as exc:
        # The BA's to fix, not a server fault — so a 400 carrying the SAME
        # sentence the page uses for its own check, and whichever layer refuses,
        # the BA reads one message. `warning`, not `exception`: a refused
        # request is not a bug and shouldn't page anyone.
        logger.warning("checkin recap refused, no photo, code=%s: %s", code, exc)
        return _err(
            "Add at least one photo of your event.",
            status=400,
            code="needs_photo",
        )
    except NeedsPayableMileage as exc:
        logger.warning("checkin recap refused, mileage, code=%s: %s", code, exc)
        return _err(
            str(exc) or "Complete your mileage stops before filing the recap.",
            status=400,
            code="needs_payable_mileage",
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
    blocked = _reject_clock_on_recap_link(code)
    if blocked is not None:
        return blocked
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


def _mileage_coords(data) -> list | None:
    lat, lng = data.get("latitude"), data.get("longitude")
    if lat is None or lng is None:
        return None
    try:
        return [float(lat), float(lng)]
    except (TypeError, ValueError):
        return None


# ── Web mileage: start / stop an odometer leg ───────────────────────────────
#
# ONE fix at start, ONE at stop, routed over real roads by OSRM. Deliberately
# NOT a breadcrumb loop: mobile Safari suspends JS and geolocation while the
# screen is off, which is most of a drive, so a browser trail would be full of
# holes and would UNDER-report mileage while looking precise. See
# ambassadors/checkin_web's mileage section. The app remains the only thing
# that records a true GPS trail.
@csrf_exempt
@require_http_methods(["POST"])
def public_checkin_mileage_start(request: HttpRequest, code: str) -> HttpResponse:
    if _over_limit("mileage-ip", _client_ip(request), limit=60, window=3600):
        return _rate_limited()
    blocked = _reject_clock_on_recap_link(code)
    if blocked is not None:
        return blocked
    data = _body(request)
    loaded, err = _load_session(code, data.get("session") or "")
    if err is not None:
        return err
    event, ambassador = loaded

    state, message = checkin_web.start_mileage_leg(
        ambassador=ambassador, event=event, coordinates=_mileage_coords(data)
    )
    if state is None:
        return _err(message or "Couldn't start the drive.")
    return JsonResponse({"mileage": state})


@csrf_exempt
@require_http_methods(["POST"])
def public_checkin_mileage_stop(request: HttpRequest, code: str) -> HttpResponse:
    if _over_limit("mileage-ip", _client_ip(request), limit=60, window=3600):
        return _rate_limited()
    blocked = _reject_clock_on_recap_link(code)
    if blocked is not None:
        return blocked
    data = _body(request)
    loaded, err = _load_session(code, data.get("session") or "")
    if err is not None:
        return err
    event, ambassador = loaded

    state, message = checkin_web.stop_mileage_leg(
        ambassador=ambassador, event=event, coordinates=_mileage_coords(data)
    )
    if state is None:
        return _err(message or "Couldn't stop the drive.")
    # `message` here is a WARNING on an otherwise-successful stop (the leg
    # closed but OSRM couldn't produce a distance) — 200, with the note.
    payload: dict = {"mileage": state}
    if message:
        payload["warning"] = message
    return JsonResponse(payload)


# ── Sampling stops — where a roaming BA actually worked ─────────────────────
#
# A market-mode event says "Austin today". This is the finer grain: an
# explicit, timestamped "I sampled here", tapped in the moment rather than
# recalled into the recap at the end. Each stop also mirrors to LocationPing,
# so it lands on the admin map and the per-event trail with no new admin UI.
@csrf_exempt
@require_http_methods(["POST"])
def public_checkin_sampling_stop(request: HttpRequest, code: str) -> HttpResponse:
    if _over_limit("stop-ip", _client_ip(request), limit=120, window=3600):
        return _rate_limited()
    blocked = _reject_clock_on_recap_link(code)
    if blocked is not None:
        return blocked
    data = _body(request)
    loaded, err = _load_session(code, data.get("session") or "")
    if err is not None:
        return err
    event, ambassador = loaded

    coordinates = None
    lat, lng = data.get("latitude"), data.get("longitude")
    if lat is not None and lng is not None:
        try:
            coordinates = [float(lat), float(lng)]
        except (TypeError, ValueError):
            coordinates = None

    stop, message = checkin_web.log_sampling_stop(
        ambassador=ambassador,
        event=event,
        coordinates=coordinates,
        name=str(data.get("name") or ""),
    )
    if stop is None:
        return _err(message or "Couldn't log that stop.")
    return JsonResponse(
        {"stop": stop, "stops": checkin_web.sampling_stops(ambassador=ambassador, event=event)}
    )


# ── Feel Free payable mileage (storage → sampling stops) ────────────────────
#
# Distinct from the GPS odometer (mileage/start|stop). The BA answers whether
# they started at the market storage unit, enters ordered Places stops, and we
# compute driving miles. Those miles are written into the recap Mileage field
# on submit — the BA never re-types them.
@csrf_exempt
@require_http_methods(["POST"])
def public_checkin_payable_mileage(request: HttpRequest, code: str) -> HttpResponse:
    if _over_limit("paymile-ip", _client_ip(request), limit=60, window=3600):
        return _rate_limited()
    blocked = _reject_clock_on_recap_link(code)
    if blocked is not None:
        return blocked
    data = _body(request)
    loaded, err = _load_session(code, data.get("session") or "")
    if err is not None:
        return err
    event, ambassador = loaded

    started_raw = data.get("startedFromStorage")
    if started_raw is None:
        started_raw = data.get("started_from_storage")
    if started_raw is None:
        return _err("Tell us whether you started at the storage unit (yes or no).")
    started = bool(started_raw) if not isinstance(started_raw, str) else started_raw.strip().lower() in (
        "1",
        "true",
        "yes",
        "y",
    )

    stops = data.get("stops") or []
    if not isinstance(stops, list):
        return _err("Stops must be a list of places.")

    shift_label = data.get("shiftLabel") or data.get("shift_label") or ""
    if not isinstance(shift_label, str):
        shift_label = ""
    storage_market = data.get("storageMarket") or data.get("storage_market") or ""

    from ambassadors.payable_mileage import (
        payable_mileage_state,
        save_payable_mileage_claim,
    )

    payload, message = save_payable_mileage_claim(
        ambassador=ambassador,
        event=event,
        started_from_storage=started,
        stops=stops,
        shift_label=shift_label.strip(),
        storage_market=str(storage_market or "").strip() or None,
    )
    if payload is None:
        return _err(message or "Couldn't save mileage.")
    return JsonResponse(
        {
            "payableMileage": payable_mileage_state(
                ambassador=ambassador,
                event=event,
                shift_label=shift_label.strip(),
            ),
            "claim": payload,
        }
    )
