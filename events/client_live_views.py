"""Public (no-JWT) endpoint for the Client Live campaign page.

`GET /api/public/client-live/<token>` — the signed client-live token IS the
authorization (same pattern as the campaign report + approval flows). Returns
a small JSON payload the branded public page renders: the brand, today's shifts
with each BA's live clock status (the "who's working right now" view), and a
few activity KPIs. Read-only; scoped to the one tenant the token encodes.

Note: a recap photo gallery is a deliberate fast-follow — recap images are
served via signed/edge URLs, which needs its own public-image plumbing.
"""
from __future__ import annotations

import logging
from datetime import timedelta

from django.http import HttpRequest, HttpResponse, JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods

from events.client_live_tokens import (
    BadSignature,
    SignatureExpired,
    verify_client_live_token,
)

logger = logging.getLogger(__name__)

NO_SHOW_GRACE_MIN = 45


def _iso(dt):
    try:
        return dt.isoformat() if dt else None
    except Exception:  # noqa: BLE001
        return None


@csrf_exempt
@require_http_methods(["GET"])
def public_client_live_view(request: HttpRequest, token: str) -> HttpResponse:
    try:
        tenant_id = verify_client_live_token(token)
    except SignatureExpired:
        return JsonResponse(
            {"error": "expired", "message": "This link has expired. Ask for a fresh one."},
            status=410,
        )
    except (BadSignature, ValueError):
        return JsonResponse(
            {"error": "invalid", "message": "This link is invalid."}, status=400
        )

    try:
        payload = _build(tenant_id)
    except Exception:  # noqa: BLE001
        logger.exception("client-live page build failed tenant=%s", tenant_id)
        return JsonResponse(
            {"error": "server", "message": "Couldn't load the live view."},
            status=500,
        )
    return JsonResponse(payload)


def _build(tenant_id: int) -> dict:
    from django.utils import timezone

    from ambassadors.attendance_hours import clock_facts
    from events.models import Event
    from tenants.models import Tenant

    tenant = Tenant.objects.filter(id=tenant_id).first()
    if not tenant:
        return {"error": "invalid", "message": "Unknown campaign."}

    # Brand: name + best-effort primary color from the tenant theme.
    primary = None
    try:
        theme = tenant.themes.first()
        cssv = getattr(theme, "css_variables", None) or {}
        if isinstance(cssv, dict):
            primary = (
                cssv.get("--p")
                or cssv.get("primary")
                or cssv.get("--color-primary")
            )
    except Exception:  # noqa: BLE001
        primary = None

    now = timezone.now()
    day = timezone.localdate()
    month_start = day.replace(day=1)

    events = list(
        Event.objects.filter(tenant_id=tenant_id, date__date=day)
        .select_related("retailer", "request")
        .prefetch_related("ambassadors_events__ambassador__user")
        .order_by("start_time", "name")[:200]
    )
    facts = clock_facts([e.id for e in events])
    grace = timedelta(minutes=NO_SHOW_GRACE_MIN)

    shifts = []
    on_clock = 0
    no_shows = 0
    for ev in events:
        start = getattr(ev, "start_time", None)
        end = getattr(ev, "end_time", None)
        started = bool(start and start <= now)
        ended = bool(end and end <= now)
        assigned = []
        for ae in ev.ambassadors_events.all():
            if not ae.is_approved or ae.ambassador is None:
                continue
            amb = ae.ambassador
            u = getattr(amb, "user", None)
            nm = ""
            if u:
                nm = (f"{u.first_name or ''} {u.last_name or ''}".strip()) or ""
            # First name only — this is a client-facing page.
            first = (nm.split(" ")[0] if nm else "") or "BA"
            f = facts.get((ev.id, amb.id))
            latest = f.get("latest_kind") if f else None
            if latest == "clock_in":
                status = "clocked_in"
                on_clock += 1
            elif latest == "clock_out":
                status = "clocked_out"
            elif not started:
                status = "upcoming"
            elif ended:
                status = "missed"
                no_shows += 1
            elif start and (now - start) > grace:
                status = "no_show"
                no_shows += 1
            else:
                status = "awaiting"
            assigned.append(
                {
                    "name": first,
                    "status": status,
                    "clockInAt": _iso(f.get("first_in")) if f else None,
                }
            )
        if not assigned:
            continue
        store = None
        if ev.retailer_id and getattr(ev, "retailer", None):
            store = ev.retailer.name
        elif ev.request_id and getattr(ev, "request", None):
            store = getattr(ev.request, "retailer_name", None)
        shifts.append(
            {
                "venue": store or ev.name,
                "startTime": _iso(start),
                "endTime": _iso(end),
                "assigned": assigned,
            }
        )

    shifts_this_month = (
        Event.objects.filter(
            tenant_id=tenant_id, date__date__gte=month_start, date__date__lte=day
        ).count()
    )

    return {
        "brand": {"name": tenant.name, "primaryColor": primary},
        "date": day.isoformat(),
        "onClock": on_clock,
        "noShows": no_shows,
        "shiftsToday": len(shifts),
        "shiftsThisMonth": shifts_this_month,
        "shifts": shifts,
    }
