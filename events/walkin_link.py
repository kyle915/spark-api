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


@dataclass
class WalkinAttachSuggestion:
    """One high-confidence orphan → DUE request pair for admin 1-click."""

    orphan_event_id: int
    orphan_event_uuid: str
    orphan_event_name: str
    orphan_address: str | None
    request_id: int
    request_uuid: str
    day: str | None = None


class WalkinAttachError(Exception):
    """Staff-facing attach failure (not found, ambiguous, already linked)."""


def _parked_orphan_ids() -> frozenset[int]:
    try:
        from events.missing_recaps_gate import ACCEPTED_WALKUP_ORPHAN_EVENT_IDS

        return ACCEPTED_WALKUP_ORPHAN_EVENT_IDS
    except Exception:  # noqa: BLE001
        return frozenset()


def safe_attach_suggestions_for_tenant(
    tenant: Tenant,
    *,
    days_back: int = 45,
) -> list[WalkinAttachSuggestion]:
    """Dry-run unique matches, enriched for /recaps/missing chips."""
    parked = _parked_orphan_ids()
    result = link_walkin_recaps_for_tenant(
        tenant, days_back=days_back, apply=False
    )
    pair_ids = [
        (int(row["orphan_id"]), int(row["request_id"]), row.get("day"))
        for row in result.rows
        if row.get("status") == "would-link"
        and row.get("orphan_id") is not None
        and row.get("request_id") is not None
        and int(row["orphan_id"]) not in parked
    ]
    if not pair_ids:
        return []

    orphan_ids = [oid for oid, _, _ in pair_ids]
    request_ids = [rid for _, rid, _ in pair_ids]
    orphans = {
        e.id: e
        for e in Event.objects.filter(pk__in=orphan_ids).only(
            "id", "uuid", "name", "address"
        )
    }
    requests = {
        r.id: r
        for r in Request.objects.filter(pk__in=request_ids).only("id", "uuid")
    }
    out: list[WalkinAttachSuggestion] = []
    for orphan_id, request_id, day in pair_ids:
        orphan = orphans.get(orphan_id)
        req = requests.get(request_id)
        if orphan is None or req is None:
            continue
        out.append(
            WalkinAttachSuggestion(
                orphan_event_id=orphan.id,
                orphan_event_uuid=str(orphan.uuid),
                orphan_event_name=(getattr(orphan, "name", None) or "(walk-up)"),
                orphan_address=getattr(orphan, "address", None) or None,
                request_id=req.id,
                request_uuid=str(req.uuid),
                day=day if isinstance(day, str) else None,
            )
        )
    return out


def attach_orphan_to_request(
    orphan_event_id: int,
    request_id: int,
    *,
    days_back: int = 45,
) -> WalkinAttachSuggestion:
    """1-click attach: only when the pair is still a unique safe match.

    Sets ``request_id`` on the orphan Event (same write as
    ``link_walkin_recaps_to_requests --apply``). Sibling filed-recap
    logic then clears the MasterTracker / Missing Recaps DUE shell.
    """
    orphan = (
        Event.objects.select_related("tenant", "retailer")
        .filter(pk=orphan_event_id)
        .first()
    )
    if orphan is None:
        raise WalkinAttachError("Walk-up event not found.")
    if orphan.request_id is not None:
        if orphan.request_id == request_id:
            req = Request.objects.filter(pk=request_id).only("id", "uuid").first()
            if req is None:
                raise WalkinAttachError("Request not found.")
            return WalkinAttachSuggestion(
                orphan_event_id=orphan.id,
                orphan_event_uuid=str(orphan.uuid),
                orphan_event_name=(getattr(orphan, "name", None) or "(walk-up)"),
                orphan_address=getattr(orphan, "address", None) or None,
                request_id=req.id,
                request_uuid=str(req.uuid),
            )
        raise WalkinAttachError("Walk-up is already attached to another request.")
    if orphan.id in _parked_orphan_ids():
        raise WalkinAttachError(
            "This walk-up is parked as accepted debt — no safe Tracker twin."
        )
    if not has_filed_recap(orphan.id):
        raise WalkinAttachError("Walk-up has no filed recap to attach.")
    if orphan.tenant_id is None:
        raise WalkinAttachError("Walk-up has no tenant.")

    req = (
        Request.objects.select_related("retailer", "tenant")
        .prefetch_related("event_set__retailer")
        .filter(pk=request_id, deleted_at__isnull=True)
        .first()
    )
    if req is None:
        raise WalkinAttachError("Request not found.")
    if req.tenant_id != orphan.tenant_id:
        raise WalkinAttachError("Walk-up and request must be the same brand.")

    shells = list(req.event_set.all())
    if not shells:
        raise WalkinAttachError("Request has no schedule shell to clear.")
    if any(has_filed_recap(ev.id) for ev in shells):
        raise WalkinAttachError("Request already has a filed recap.")

    # Re-run unique match against live DUE set — refuse ambiguous attaches.
    suggestions = safe_attach_suggestions_for_tenant(
        orphan.tenant, days_back=days_back
    )
    match = next(
        (
            s
            for s in suggestions
            if s.orphan_event_id == orphan.id and s.request_id == req.id
        ),
        None,
    )
    if match is None:
        raise WalkinAttachError(
            "Not a unique same-day place match — attach refused."
        )

    with transaction.atomic():
        updated = Event.objects.filter(
            pk=orphan.id, request__isnull=True
        ).update(request_id=req.id)
    if not updated:
        raise WalkinAttachError("Walk-up was linked by someone else just now.")

    orphan.refresh_from_db(fields=["request_id"])
    return match


def attach_all_safe_orphans(
    tenant: Tenant,
    *,
    days_back: int = 45,
) -> WalkinLinkResult:
    """Bulk apply every high-confidence orphan → DUE link for a tenant."""
    return link_walkin_recaps_for_tenant(
        tenant, days_back=days_back, apply=True
    )
