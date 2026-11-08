"""High-confidence walk-up Event → Tracker Request linking.

Used by ``link_walkin_recaps_to_requests`` (nightly / manual) and post-submit
auto-link after a check-in CustomRecap is filed on a request-less Event.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import timedelta

from django.db import transaction
from django.utils import timezone as djtz

from ambassadors.checkin_web import (
    addresses_fuzzy_match,
    event_calendar_date,
    normalize_place,
)
from events.models import Event, Request
from recaps.filed import custom_filed_q, legacy_filed_q
from recaps.models import CustomRecap, Recap
from tenants.models import Tenant


def _name_key(value: str) -> str:
    raw = value or ""
    raw = re.sub(r"[•·].*$", "", raw)
    raw = re.sub(r"\d{1,2}/\d{1,2}/\d{2,4}", "", raw)
    raw = re.sub(r"\d{4}-\d{2}-\d{2}", "", raw)
    return normalize_place(raw)


def _labels(ev: Event) -> list[str]:
    out = [getattr(ev, "name", "") or ""]
    retailer = getattr(ev, "retailer", None)
    if retailer is not None:
        out.append(getattr(retailer, "name", "") or "")
    return [x for x in out if x]


def _same_place(a: Event, b: Event, *, extra_labels: list[str] | None = None) -> bool:
    if addresses_fuzzy_match(a.address or "", b.address or ""):
        return True
    a_keys = {_name_key(x) for x in _labels(a) if _name_key(x)}
    b_keys = {_name_key(x) for x in _labels(b) if _name_key(x)}
    for label in extra_labels or []:
        k = _name_key(label)
        if k:
            b_keys.add(k)
    shared = {k for k in a_keys & b_keys if len(k) >= 4}
    return bool(shared)


def _request_labels(req: Request) -> list[str]:
    out = [getattr(req, "name", "") or "", getattr(req, "address", "") or ""]
    retailer = getattr(req, "retailer", None)
    if retailer is not None:
        out.append(getattr(retailer, "name", "") or "")
    return [x for x in out if x]


def _orphan_matches_request(orphan: Event, req: Request, shells: list[Event]) -> bool:
    extras = _request_labels(req)
    if any(_same_place(orphan, shell, extra_labels=extras) for shell in shells):
        return True
    if addresses_fuzzy_match(orphan.address or "", req.address or ""):
        return True
    o_keys = {
        _name_key(x) for x in _labels(orphan) + [orphan.address or ""] if _name_key(x)
    }
    for label in extras:
        k = _name_key(label)
        if k and len(k) >= 4 and (k in o_keys or any(k in ok for ok in o_keys)):
            return True
    return False


def has_filed_recap(event_id: int) -> bool:
    return (
        CustomRecap.objects.filter(event_id=event_id)
        .filter(custom_filed_q())
        .exists()
        or Recap.objects.filter(event_id=event_id).filter(legacy_filed_q()).exists()
    )


@dataclass
class WalkinLinkResult:
    tenant_slug: str
    apply: bool
    linked: int
    would_link: int
    ambiguous: int
    orphans_with_filed: int
    rows: list[dict]
    orphan_details: list[dict] | None = None


def link_walkin_recaps_for_tenant(
    tenant: Tenant,
    *,
    days_back: int = 45,
    apply: bool = False,
    diagnose: bool = False,
) -> WalkinLinkResult:
    """Link filed request-less walk-up Events onto DUE Tracker Requests."""
    now = djtz.now()
    lo = now - timedelta(days=max(1, int(days_back)))

    orphans = list(
        Event.objects.filter(
            tenant=tenant,
            request__isnull=True,
            date__gte=lo,
            co_activation_host__isnull=True,
        )
        .select_related("retailer")
        .order_by("id")
    )
    orphans = [ev for ev in orphans if has_filed_recap(ev.id)]

    due_requests = list(
        Request.objects.filter(
            tenant=tenant,
            deleted_at__isnull=True,
            date__gte=lo,
        )
        .select_related("retailer")
        .prefetch_related("event_set__retailer")
        .order_by("id")
    )

    linked = would_link = ambiguous = 0
    report: list[dict] = []
    claimed_orphan: set[int] = set()
    claimed_request: set[int] = set()

    for req in due_requests:
        events = list(req.event_set.all())
        if not events:
            continue
        if any(has_filed_recap(ev.id) for ev in events):
            continue
        day = None
        for ev in events:
            day = event_calendar_date(ev) or day
        if day is None and getattr(req, "date", None) is not None:
            rd = req.date
            day = rd.date() if hasattr(rd, "date") else rd

        candidates = []
        for orphan in orphans:
            if orphan.id in claimed_orphan:
                continue
            oday = event_calendar_date(orphan)
            if day is not None and oday is not None and oday != day:
                continue
            if _orphan_matches_request(orphan, req, events):
                candidates.append(orphan)

        if not candidates:
            continue
        if len(candidates) != 1:
            ambiguous += 1
            report.append(
                {
                    "status": "ambiguous-orphans",
                    "request_id": req.id,
                    "orphan_ids": [c.id for c in candidates],
                }
            )
            continue

        orphan = candidates[0]
        rival_requests = [
            r
            for r in due_requests
            if r.id != req.id
            and r.id not in claimed_request
            and not any(has_filed_recap(ev.id) for ev in r.event_set.all())
            and _orphan_matches_request(orphan, r, list(r.event_set.all()))
            and (
                day is None
                or any(
                    event_calendar_date(shell) in (None, day)
                    for shell in r.event_set.all()
                )
            )
        ]
        if rival_requests:
            ambiguous += 1
            report.append(
                {
                    "status": "ambiguous-requests",
                    "orphan_id": orphan.id,
                    "request_ids": [req.id] + [r.id for r in rival_requests],
                }
            )
            continue

        claimed_orphan.add(orphan.id)
        claimed_request.add(req.id)
        entry = {
            "status": "link" if apply else "would-link",
            "request_id": req.id,
            "orphan_id": orphan.id,
            "day": day.isoformat() if day else None,
        }
        report.append(entry)
        if apply:
            with transaction.atomic():
                Event.objects.filter(pk=orphan.id, request__isnull=True).update(
                    request_id=req.id
                )
            linked += 1
        else:
            would_link += 1

    orphan_details = None
    if diagnose:
        orphan_details = _diagnose_orphans(orphans, due_requests)

    return WalkinLinkResult(
        tenant_slug=getattr(tenant, "slug", "") or "",
        apply=apply,
        linked=linked,
        would_link=would_link,
        ambiguous=ambiguous,
        orphans_with_filed=len(orphans),
        rows=report,
        orphan_details=orphan_details,
    )



def _diagnose_orphans(orphans: list[Event], due_requests: list[Request]) -> list[dict]:
    """Explain why each filed orphan did not auto-link (read-only)."""
    due_open: list[tuple[Request, list[Event], object]] = []
    for req in due_requests:
        shells = list(req.event_set.all())
        if not shells:
            continue
        if any(has_filed_recap(ev.id) for ev in shells):
            continue
        day = None
        for ev in shells:
            day = event_calendar_date(ev) or day
        if day is None and getattr(req, "date", None) is not None:
            rd = req.date
            day = rd.date() if hasattr(rd, "date") else rd
        due_open.append((req, shells, day))

    out: list[dict] = []
    for orphan in orphans:
        oday = event_calendar_date(orphan)
        same_day: list[dict] = []
        matched: list[dict] = []
        for req, shells, day in due_open:
            if day is not None and oday is not None and day != oday:
                continue
            entry = {
                "request_id": req.id,
                "name": getattr(req, "name", "") or "",
                "address": getattr(req, "address", "") or "",
                "day": day.isoformat() if day else None,
            }
            same_day.append(entry)
            if _orphan_matches_request(orphan, req, shells):
                matched.append(entry)
        if not same_day:
            reason = "no-same-day-due"
        elif matched:
            reason = "place-match-but-not-unique"
        else:
            reason = "same-day-no-place-match"
        out.append(
            {
                "event_id": orphan.id,
                "name": getattr(orphan, "name", "") or "",
                "address": getattr(orphan, "address", "") or "",
                "day": oday.isoformat() if oday else None,
                "retailer": getattr(getattr(orphan, "retailer", None), "name", None),
                "reason": reason,
                "matched_due": matched,
                "same_day_due_count": len(same_day),
                "same_day_due": same_day[:15],
            }
        )
    return out


def try_link_orphan_event(event_id: int) -> int | None:
    """Best-effort post-submit: if ``event`` is a filed orphan, link it.

    Returns the request_id when linked, else None. Never raises.
    """
    import logging

    logger = logging.getLogger(__name__)
    try:
        event = (
            Event.objects.select_related("tenant", "retailer")
            .filter(pk=event_id, request__isnull=True)
            .first()
        )
        if event is None or event.tenant_id is None:
            return None
        if not has_filed_recap(event.id):
            return None
        result = link_walkin_recaps_for_tenant(
            event.tenant, days_back=14, apply=True
        )
        for row in result.rows:
            if row.get("status") == "link" and row.get("orphan_id") == event_id:
                return int(row["request_id"])
        return None
    except Exception:  # noqa: BLE001
        logger.exception("walkin auto-link failed event=%s", event_id)
        return None
