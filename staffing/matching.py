"""Smart BA Matching — deterministic composite ranking for shift & wave staffing.

Combines four transparent signals per eligible Brand Ambassador:

1. **Historical units-per-hour (30%)** — tenant-scoped units sold from
   ``Recap`` and ``CustomRecap`` divided by clocked/scheduled shift hours.
2. **Check-in reliability rate (30%)** — on-time check-ins vs late check-ins,
   no-shows, and released ``OpenShift`` drops.
3. **Brand familiarity / certification (20%)** — prior shifts worked for this
   brand, active compliance/brand certifications, and tenant shortlist status.
4. **Proximity (20%)** — great-circle haversine distance between the target
   event/store and the BA's coordinates, with state-level fallback.

Missing history uses explicit neutral priors rather than dropping weights, so
a BA with proven high units/hr or high reliability always ranks above an
unproven BA, and every returned match carries transparent ``reasons`` plus a
ready-to-render ``summary_reason`` (e.g.
``"14.2 units/hr · 98% reliable · Brand certified · 4.2 mi away"``).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from django.db.models import Count, Exists, OuterRef, Prefetch
from django.utils import timezone
from strawberry.relay import to_base64

from ambassadors.models import Ambassador, AmbassadorEvent, Attendance, OpenShift
from ambassadors.staffing_suggestions import (
    _ba_display_name,
    _ba_is_available,
    _event_when,
    _haversine_miles,
    _valid_coords,
)
from availability.models import AmbassadorAvailability
from documents.models import AmbassadorDocument, DocumentStatus, DocumentType
from events.market import parse_address_geo
from events.models import Event, Request
from jobs.models import TenantFavoriteAmbassador
from recaps.models import CustomFieldValue, CustomRecap, Recap
from recaps.store_places import tenant_places
from recaps.types import _sold_units_from_fields
from staffing.models import NoShowCascadeAlert
from store_logistics.service import resolve_non_request_store
from tenants.models import Tenant

logger = logging.getLogger(__name__)

MAX_MATCH_LIMIT = 100

WEIGHT_UNITS_PER_HOUR = 0.30
WEIGHT_RELIABILITY = 0.30
WEIGHT_BRAND_FAMILIARITY = 0.20
WEIGHT_PROXIMITY = 0.20

UNITS_PER_HOUR_CAP = 20.0
MAX_DISTANCE_MILES = 75.0
BRAND_GIGS_CAP = 8
ON_TIME_GRACE_MINUTES = 15

# Neutral priors for missing history so proven performers outrank unknowns.
NEUTRAL_UPH_NORM = 0.40
NEUTRAL_RELIABILITY_NORM = 0.70
NEUTRAL_PROXIMITY_NORM = 0.35
STATE_MATCH_PROXIMITY_NORM = 0.65

CERT_DOC_TYPES = frozenset(
    {
        DocumentType.CERTIFICATION,
        DocumentType.ALCOHOL_CERT,
        DocumentType.FOOD_HANDLER,
    }
)


@dataclass(frozen=True)
class SmartBaSignalInput:
    """Normalized inputs for pure deterministic composite scoring."""

    ba_id: int
    ba_uuid: str
    name: str
    email: str | None
    phone: str | None
    units_per_hour: float | None
    reliability_rate: float | None
    on_time_checkins: int
    total_tracked_shifts: int
    brand_gigs: int
    brand_certified: bool
    has_active_cert_doc: bool
    is_favorited: bool
    distance_miles: float | None
    same_state: bool
    is_available: bool | None


def score_smart_ba_candidate(sig: SmartBaSignalInput) -> dict[str, Any]:
    """Pure, deterministic composite scorer (0–100) + transparent reasons."""
    reasons: list[str] = []

    if sig.units_per_hour is not None and sig.units_per_hour >= 0:
        uph_norm = max(0.0, min(1.0, sig.units_per_hour / UNITS_PER_HOUR_CAP))
        reasons.append(f"{sig.units_per_hour:.1f} units/hr")
    else:
        uph_norm = NEUTRAL_UPH_NORM

    if sig.reliability_rate is not None:
        rel_norm = max(0.0, min(1.0, sig.reliability_rate / 100.0))
        rounded_rel = int(round(sig.reliability_rate))
        reasons.append(f"{rounded_rel}% reliable")
    else:
        rel_norm = NEUTRAL_RELIABILITY_NORM

    exp_norm = min(max(0, sig.brand_gigs), BRAND_GIGS_CAP) / float(BRAND_GIGS_CAP)
    cert_bonus = (
        0.35
        if sig.brand_certified
        else (0.15 if sig.has_active_cert_doc else 0.0)
    )
    fav_bonus = 0.15 if sig.is_favorited else 0.0
    brand_norm = min(1.0, exp_norm * 0.65 + cert_bonus + fav_bonus)

    if sig.brand_certified:
        reasons.append("Brand certified")
    elif sig.brand_gigs > 0:
        reasons.append(
            "1 brand shift"
            if sig.brand_gigs == 1
            else f"{sig.brand_gigs} brand shifts"
        )
    elif sig.has_active_cert_doc:
        reasons.append("Certified BA")

    if sig.distance_miles is not None:
        prox_norm = max(
            0.0,
            1.0 - min(sig.distance_miles, MAX_DISTANCE_MILES) / MAX_DISTANCE_MILES,
        )
        reasons.append(f"{sig.distance_miles:.1f} mi away")
    elif sig.same_state:
        prox_norm = STATE_MATCH_PROXIMITY_NORM
        reasons.append("Local market")
    else:
        prox_norm = NEUTRAL_PROXIMITY_NORM

    if sig.is_available is True:
        reasons.append("Available")

    composite01 = (
        WEIGHT_UNITS_PER_HOUR * uph_norm
        + WEIGHT_RELIABILITY * rel_norm
        + WEIGHT_BRAND_FAMILIARITY * brand_norm
        + WEIGHT_PROXIMITY * prox_norm
    )
    # Small availability tie-adjustment when explicitly unavailable vs available
    if sig.is_available is False:
        composite01 = max(0.0, composite01 - 0.10)
    elif sig.is_available is True:
        composite01 = min(1.0, composite01 + 0.03)

    score = int(round(max(0.0, min(1.0, composite01)) * 100))

    if not reasons:
        reasons.append("Eligible BA")

    return {
        "ba_id": to_base64("Ambassador", sig.ba_id),
        "ba_numeric_id": int(sig.ba_id),
        "ba_uuid": sig.ba_uuid,
        "name": sig.name,
        "email": sig.email,
        "phone": sig.phone,
        "score": score,
        "units_per_hour": (
            round(sig.units_per_hour, 1)
            if sig.units_per_hour is not None
            else None
        ),
        "reliability_rate": (
            round(sig.reliability_rate, 1)
            if sig.reliability_rate is not None
            else None
        ),
        "on_time_checkins": int(sig.on_time_checkins),
        "total_tracked_shifts": int(sig.total_tracked_shifts),
        "brand_certified": bool(sig.brand_certified),
        "brand_gigs": int(sig.brand_gigs),
        "distance_miles": (
            round(sig.distance_miles, 1)
            if sig.distance_miles is not None
            else None
        ),
        "is_available": sig.is_available,
        "is_favorited": bool(sig.is_favorited),
        "reasons": reasons,
        "summary_reason": " · ".join(reasons),
    }


def _resolve_store_target(
    tenant_id: int, store_key: str
) -> tuple[list[float] | None, str]:
    """Resolve ``store_key`` to ``([lat, lng] | None, state_code)`` within tenant."""
    clean = (store_key or "").strip()
    if not clean:
        return None, ""

    non_req = resolve_non_request_store(tenant_id, clean)
    if non_req is not None:
        return non_req.get("coordinates"), (non_req.get("state") or "").upper()

    try:
        places = tenant_places(tenant_id)
        req_ids = places.requests_of(clean)
        if req_ids:
            req = (
                Request.objects.select_related("state")
                .filter(tenant_id=tenant_id, pk__in=req_ids)
                .order_by("pk")
                .first()
            )
            if req is not None:
                coords = (
                    list(req.coordinates)
                    if _valid_coords(getattr(req, "coordinates", None))
                    else None
                )
                st = (
                    (getattr(getattr(req, "state", None), "code", "") or "")
                    .strip()
                    .upper()
                )
                if not st and req.address:
                    st = (parse_address_geo(req.address).state_code or "").upper()
                return coords, st
    except Exception:  # noqa: BLE001 — degrade gracefully
        logger.debug("smart_ba_matches: store_key resolve fallback failed", exc_info=True)

    return None, ""


def _shift_hours_for_event(
    event: Event | None,
    clock_in: datetime | None,
    clock_out: datetime | None,
) -> float:
    """Actual clocked hours if both punches exist, else scheduled shift length."""
    if clock_in is not None and clock_out is not None and clock_out > clock_in:
        hrs = (clock_out - clock_in).total_seconds() / 3600.0
        if 0.25 <= hrs <= 18.0:
            return hrs
    if event is not None:
        start = getattr(event, "start_time", None)
        end = getattr(event, "end_time", None) or getattr(event, "new_end_time", None)
        if start is not None and end is not None and end > start:
            hrs = (end - start).total_seconds() / 3600.0
            if 0.25 <= hrs <= 18.0:
                return hrs
    return 0.0


def _units_per_hour_by_ambassador(
    tenant_id: int, ambassador_ids: list[int]
) -> dict[int, float]:
    """Tenant-scoped historical units sold per hour per BA."""
    if not ambassador_ids:
        return {}

    units_by_ba: dict[int, float] = {}
    hours_by_ba: dict[int, float] = {}

    legacy_recaps = list(
        Recap.objects.filter(
            event__tenant_id=tenant_id,
            ambassador_id__in=ambassador_ids,
            archived_at__isnull=True,
        ).select_related("event")
    )
    custom_recaps = list(
        CustomRecap.objects.filter(
            tenant_id=tenant_id,
            ambassador_id__in=ambassador_ids,
            archived_at__isnull=True,
        ).select_related("event")
    )

    event_ids = {
        r.event_id for r in legacy_recaps if r.event_id is not None
    } | {r.event_id for r in custom_recaps if r.event_id is not None}

    punches_by_pair: dict[tuple[int, int], tuple[datetime | None, datetime | None]] = {}
    if event_ids:
        for amb_id, ev_id, src_name, clock_time in (
            Attendance.objects.filter(
                ambassador_id__in=ambassador_ids,
                event_id__in=event_ids,
            )
            .values_list("ambassador_id", "event_id", "source__name", "clock_time")
            .order_by("clock_time")
        ):
            if amb_id is None or ev_id is None or clock_time is None:
                continue
            key = (int(amb_id), int(ev_id))
            ci, co = punches_by_pair.get(key, (None, None))
            kind = (src_name or "").strip().lower()
            if kind == "clock_out":
                co = clock_time
            elif ci is None:
                ci = clock_time
            else:
                co = clock_time
            punches_by_pair[key] = (ci, co)

    for rec in legacy_recaps:
        if rec.ambassador_id is None:
            continue
        sold = (
            rec.products_sold
            if rec.products_sold is not None
            else (rec.total_cans_sold or 0) + (rec.total_packs_sold or 0)
        )
        if sold is None or sold <= 0:
            continue
        ci, co = punches_by_pair.get((int(rec.ambassador_id), int(rec.event_id)), (None, None))
        hrs = _shift_hours_for_event(rec.event, ci, co)
        if hrs > 0:
            units_by_ba[int(rec.ambassador_id)] = units_by_ba.get(int(rec.ambassador_id), 0.0) + float(sold)
            hours_by_ba[int(rec.ambassador_id)] = hours_by_ba.get(int(rec.ambassador_id), 0.0) + hrs

    if custom_recaps:
        field_pairs: dict[int, list[tuple[str | None, str | None]]] = {}
        for rec_id, field_name, value in CustomFieldValue.objects.filter(
            custom_recap_id__in=[r.id for r in custom_recaps]
        ).values_list("custom_recap_id", "custom_field__name", "value"):
            field_pairs.setdefault(int(rec_id), []).append((field_name, value))

        for rec in custom_recaps:
            if rec.ambassador_id is None:
                continue
            sold = _sold_units_from_fields(field_pairs.get(int(rec.id), [])) or 0
            if sold <= 0:
                continue
            ci, co = punches_by_pair.get(
                (int(rec.ambassador_id), int(rec.event_id)), (None, None)
            )
            hrs = _shift_hours_for_event(rec.event, ci, co)
            if hrs > 0:
                units_by_ba[int(rec.ambassador_id)] = (
                    units_by_ba.get(int(rec.ambassador_id), 0.0) + float(sold)
                )
                hours_by_ba[int(rec.ambassador_id)] = (
                    hours_by_ba.get(int(rec.ambassador_id), 0.0) + hrs
                )

    out: dict[int, float] = {}
    for ba_id, hrs in hours_by_ba.items():
        if hrs > 0:
            out[ba_id] = round(units_by_ba[ba_id] / hrs, 1)
    return out


def _checkin_reliability_by_ambassador(
    candidates: list[Ambassador], now: datetime
) -> dict[int, tuple[float | None, int, int]]:
    """Return ``{ba_id: (reliability_rate_pct | None, on_time_checkins, total_tracked)}``.

    Evaluates past approved shifts:
    - Clock-in <= start + 15m -> on-time (1.0 credit)
    - Clock-in > start + 15m  -> late (0.5 credit)
    - No clock-in with ``no_show_alerted_at`` or a ``NoShowCascadeAlert`` -> 0.0
    - No clock-in when no attendance exists (legacy) -> 0.85 credit
    - Plus ``OpenShift`` releases count as 0.0 and claims count as 1.0.
    """
    if not candidates:
        return {}

    ba_ids = [int(ba.id) for ba in candidates]
    user_to_ba = {int(ba.user_id): int(ba.id) for ba in candidates if ba.user_id}

    past_assignments = list(
        AmbassadorEvent.objects.filter(
            ambassador_id__in=ba_ids,
            is_approved=True,
            event__start_time__lt=now,
        )
        .select_related("event")
        .only(
            "id",
            "ambassador_id",
            "event_id",
            "no_show_alerted_at",
            "event__start_time",
        )
    )

    event_ids = {int(ae.event_id) for ae in past_assignments if ae.event_id}

    first_checkin_by_pair: dict[tuple[int, int], datetime] = {}
    if event_ids:
        for amb_id, ev_id, clock_time in (
            Attendance.objects.filter(
                ambassador_id__in=ba_ids,
                event_id__in=event_ids,
            )
            .exclude(source__name="clock_out")
            .values_list("ambassador_id", "event_id", "clock_time")
            .order_by("clock_time")
        ):
            if amb_id is None or ev_id is None or clock_time is None:
                continue
            pair = (int(amb_id), int(ev_id))
            if pair not in first_checkin_by_pair:
                first_checkin_by_pair[pair] = clock_time

    cascade_missed_pairs: set[tuple[int, int]] = set()
    if event_ids:
        for amb_id, ev_id in NoShowCascadeAlert.objects.filter(
            missing_ambassador_id__in=ba_ids,
            event_id__in=event_ids,
        ).exclude(status=NoShowCascadeAlert.STATUS_DISMISSED).values_list(
            "missing_ambassador_id", "event_id"
        ):
            if amb_id is not None and ev_id is not None:
                cascade_missed_pairs.add((int(amb_id), int(ev_id)))

    dropped_by_ba: dict[int, int] = {}
    claimed_by_ba: dict[int, int] = {}
    if user_to_ba:
        uids = list(user_to_ba.keys())
        for row in (
            OpenShift.objects.filter(released_by_id__in=uids)
            .values("released_by_id")
            .annotate(c=Count("id"))
        ):
            ba_id = user_to_ba.get(int(row["released_by_id"]))
            if ba_id is not None:
                dropped_by_ba[ba_id] = int(row["c"])
        for row in (
            OpenShift.objects.filter(claimed_by_id__in=uids)
            .values("claimed_by_id")
            .annotate(c=Count("id"))
        ):
            ba_id = user_to_ba.get(int(row["claimed_by_id"]))
            if ba_id is not None:
                claimed_by_ba[ba_id] = int(row["c"])

    credits: dict[int, float] = {}
    totals: dict[int, int] = {}
    on_time_counts: dict[int, int] = {}

    grace = timedelta(minutes=ON_TIME_GRACE_MINUTES)
    for ae in past_assignments:
        ba_id = int(ae.ambassador_id)
        ev_id = int(ae.event_id)
        start = getattr(ae.event, "start_time", None)
        totals[ba_id] = totals.get(ba_id, 0) + 1
        pair = (ba_id, ev_id)
        ci = first_checkin_by_pair.get(pair)
        if ci is not None:
            if start is None or ci <= start + grace:
                credits[ba_id] = credits.get(ba_id, 0.0) + 1.0
                on_time_counts[ba_id] = on_time_counts.get(ba_id, 0) + 1
            else:
                credits[ba_id] = credits.get(ba_id, 0.0) + 0.5
        elif ae.no_show_alerted_at is not None or pair in cascade_missed_pairs:
            credits[ba_id] = credits.get(ba_id, 0.0) + 0.0
        else:
            credits[ba_id] = credits.get(ba_id, 0.0) + 0.85

    out: dict[int, tuple[float | None, int, int]] = {}
    for ba_id in ba_ids:
        dropped = dropped_by_ba.get(ba_id, 0)
        claimed = claimed_by_ba.get(ba_id, 0)
        total = totals.get(ba_id, 0) + dropped + claimed
        credit = credits.get(ba_id, 0.0) + float(claimed)
        if total <= 0:
            out[ba_id] = (None, 0, 0)
        else:
            rate = max(0.0, min(100.0, round(100.0 * credit / float(total), 1)))
            out[ba_id] = (rate, on_time_counts.get(ba_id, 0), total)
    return out


def compute_smart_ba_matches(
    *,
    tenant_id: int,
    event_id: int | None = None,
    store_key: str | None = None,
    lat: float | None = None,
    lng: float | None = None,
    state_code: str | None = None,
    limit: int = 20,
    exclude_ambassador_ids: set[int] | None = None,
    now: datetime | None = None,
) -> list[dict[str, Any]]:
    """Rank eligible Brand Ambassadors for an event, store, or tenant workspace.

    Strictly scoped to ``tenant_id``: only BAs with active ``TenantedUser``
    membership on ``tenant_id`` are considered, and all units/hr + brand
    familiarity signals are scoped to ``tenant_id``.
    """
    now = now or timezone.now()
    try:
        cap = max(0, min(int(limit), MAX_MATCH_LIMIT))
    except (TypeError, ValueError):
        cap = 20
    if cap == 0:
        return []

    target_coords: list[float] | None = None
    target_state = (state_code or "").strip().upper()
    on_date, start_t, end_t = (None, None, None)
    excluded: set[int] = set(exclude_ambassador_ids or set())

    if event_id is not None:
        event = (
            Event.objects.select_related("state", "request", "request__state")
            .filter(id=event_id, tenant_id=tenant_id)
            .first()
        )
        if event is None:
            return []
        if _valid_coords(getattr(event, "coordinates", None)):
            target_coords = list(event.coordinates)
        elif event.request and _valid_coords(
            getattr(event.request, "coordinates", None)
        ):
            target_coords = list(event.request.coordinates)
        if not target_state:
            st_obj = getattr(event, "state", None) or getattr(
                getattr(event, "request", None), "state", None
            )
            target_state = (getattr(st_obj, "code", "") or "").strip().upper()
            if not target_state and event.address:
                target_state = (
                    parse_address_geo(event.address).state_code or ""
                ).upper()
        on_date, start_t, end_t = _event_when(event)
        excluded |= set(
            AmbassadorEvent.objects.filter(event_id=event.id).values_list(
                "ambassador_id", flat=True
            )
        )
    elif store_key:
        store_coords, store_state = _resolve_store_target(tenant_id, store_key)
        if store_coords is not None:
            target_coords = store_coords
        if not target_state and store_state:
            target_state = store_state

    if target_coords is None and lat is not None and lng is not None:
        if _valid_coords([lat, lng]):
            target_coords = [float(lat), float(lng)]

    tenant_name = (
        Tenant.objects.filter(id=tenant_id).values_list("name", flat=True).first()
        or ""
    ).strip().lower()

    candidates = list(
        Ambassador.objects.filter(
            user__tenanted_users__tenant_id=tenant_id,
            user__tenanted_users__is_active=True,
        )
        .exclude(id__in=excluded)
        .select_related("user", "location", "location__state")
        .prefetch_related(
            Prefetch(
                "availability",
                queryset=AmbassadorAvailability.objects.filter(is_recurring=True),
                to_attr="_recurring_availability",
            )
        )
        .distinct()
    )
    if not candidates:
        return []

    ba_ids = [int(ba.id) for ba in candidates]

    # Tenant-scoped brand experience: past shifts the BA actually worked
    # (approved + clock-in attendance). Future approved rows / no-shows
    # must not inflate brand-certified scoring.
    worked_clock_in = Attendance.objects.filter(
        ambassador_id=OuterRef("ambassador_id"),
        event_id=OuterRef("event_id"),
        source__name__iexact="clock_in",
    )
    brand_gigs_map: dict[int, int] = {
        int(row["ambassador_id"]): int(row["c"])
        for row in (
            AmbassadorEvent.objects.filter(
                tenant_id=tenant_id,
                ambassador_id__in=ba_ids,
                is_approved=True,
                event__start_time__lt=now,
            )
            .annotate(_worked=Exists(worked_clock_in))
            .filter(_worked=True)
            .values("ambassador_id")
            .annotate(c=Count("id"))
        )
        if row["ambassador_id"] is not None
    }

    favorited_ids: set[int] = {
        int(aid)
        for aid in TenantFavoriteAmbassador.objects.filter(
            tenant_id=tenant_id, ambassador_id__in=ba_ids
        ).values_list("ambassador_id", flat=True)
        if aid is not None
    }

    # Active compliance / brand certification documents
    active_cert_bas: set[int] = set()
    brand_cert_doc_bas: set[int] = set()
    today = now.date()
    for amb_id, doc_type, title, expires_on in AmbassadorDocument.objects.filter(
        ambassador_id__in=ba_ids,
        status=DocumentStatus.ACTIVE,
        doc_type__in=CERT_DOC_TYPES,
    ).values_list("ambassador_id", "doc_type", "title", "expires_on"):
        if expires_on is not None and expires_on < today:
            continue
        aid = int(amb_id)
        active_cert_bas.add(aid)
        if tenant_name and title and tenant_name in title.lower():
            brand_cert_doc_bas.add(aid)

    uph_map = _units_per_hour_by_ambassador(tenant_id, ba_ids)
    rel_map = _checkin_reliability_by_ambassador(candidates, now)

    scored: list[dict[str, Any]] = []
    for ba in candidates:
        ba_id = int(ba.id)
        gigs = brand_gigs_map.get(ba_id, 0)
        is_fav = ba_id in favorited_ids
        has_cert = ba_id in active_cert_bas
        brand_certified = (
            ba_id in brand_cert_doc_bas
            or gigs >= 2
            or (is_fav and gigs >= 1)
            or (has_cert and gigs >= 1)
        )

        distance = (
            _haversine_miles(target_coords, ba.coordinates)
            if target_coords is not None
            else None
        )
        ba_state = ""
        if getattr(ba, "location", None) and getattr(ba.location, "state", None):
            ba_state = (getattr(ba.location.state, "code", "") or "").strip().upper()
        if not ba_state and ba.address:
            ba_state = (parse_address_geo(ba.address).state_code or "").upper()
        same_state = bool(target_state and ba_state and target_state == ba_state)

        slots = getattr(ba, "_recurring_availability", []) or []
        available = _ba_is_available(slots, on_date, start_t, end_t)

        rel_rate, on_time, tracked = rel_map.get(ba_id, (None, 0, 0))
        user = getattr(ba, "user", None)

        scored.append(
            score_smart_ba_candidate(
                SmartBaSignalInput(
                    ba_id=ba_id,
                    ba_uuid=str(ba.uuid),
                    name=_ba_display_name(ba),
                    email=getattr(user, "email", None) if user else None,
                    phone=getattr(ba, "phone", None) or None,
                    units_per_hour=uph_map.get(ba_id),
                    reliability_rate=rel_rate,
                    on_time_checkins=on_time,
                    total_tracked_shifts=tracked,
                    brand_gigs=gigs,
                    brand_certified=brand_certified,
                    has_active_cert_doc=has_cert,
                    is_favorited=is_fav,
                    distance_miles=distance,
                    same_state=same_state,
                    is_available=available,
                )
            )
        )

    scored.sort(
        key=lambda row: (
            -row["score"],
            -(row["units_per_hour"] if row["units_per_hour"] is not None else -1.0),
            -(row["reliability_rate"] if row["reliability_rate"] is not None else -1.0),
            row["distance_miles"] if row["distance_miles"] is not None else 999999.0,
            row["ba_numeric_id"],
        )
    )
    return scored[:cap]
