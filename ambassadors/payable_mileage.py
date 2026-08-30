"""Feel Free payable mileage: storage → sampling stops (or stops-only).

Business rules (Kyle):
  1. BA answers yes/no: did you start at the market storage unit?
  2. YES → payable miles = driving distance Storage → stop1 → stop2 → …
  3. NO  → do NOT include storage; pay only stop1 → stop2 → …
  4. On recap submit, the computed miles are written into the template's
     Mileage / Miles-driven custom field so the BA never re-types them.

Storage units live on ``Tenant.checkin_storage_units`` as
``[{"market": "Austin, TX", "address": "...", "lat": …, "lng": …}, …]``.
Feature is ON when that list is non-empty (Feel Free after seed).
"""

from __future__ import annotations

import logging
import re
from decimal import Decimal

logger = logging.getLogger(__name__)

# Matches Feel Free "Mileage", "Insert your mileage", and common variants
# ("Miles driven", …).
MILEAGE_FIELD_RE = re.compile(
    r"mileage|^\s*miles?\s*(driven|traveled|travelled|reimbursed)?\s*$",
    re.IGNORECASE,
)


def is_mileage_custom_field(name: str | None) -> bool:
    n = (name or "").strip()
    if not n:
        return False
    return bool(MILEAGE_FIELD_RE.search(n))

# Canonical Feel Free storage roster (seeded onto the tenant).
FEEL_FREE_STORAGE_UNITS: list[dict] = [
    {
        "market": "Miami, FL",
        "address": "13101 NE 16th Ave Miami, Florida FL 33161",
    },
    {
        "market": "Ft. Lauderdale, FL",
        "address": "4551 W Sunrise Blvd, Plantation, FL, 33313",
    },
    {
        "market": "Tampa, FL",
        "address": "10700 US Highway 19 N Pinellas Park, Florida FL 33782",
    },
    {
        "market": "Austin, TX",
        "address": "6330 Harold Ct Austin, Texas TX 78721",
    },
    {
        "market": "San Antonio, TX",
        "address": "3440 Fredericksburg Road, San Antonio TX 78201",
    },
    {
        "market": "Phoenix, AZ",
        "address": "1356 E Baseline Rd, Mesa, AZ, 85204",
    },
]

# Field name we seed onto the Feel Free Sampling Details / Wrap Up section.
MILEAGE_FIELD_NAME = "Mileage"


def payable_mileage_enabled(tenant) -> bool:
    """True when this brand uses storage→stops payable mileage capture."""
    units = getattr(tenant, "checkin_storage_units", None) or []
    return isinstance(units, list) and len(units) > 0


def tenant_storage_units(tenant) -> list[dict]:
    """Normalized storage unit list for the check-in page / matcher."""
    raw = getattr(tenant, "checkin_storage_units", None) or []
    out: list[dict] = []
    if not isinstance(raw, list):
        return out
    for row in raw:
        if not isinstance(row, dict):
            continue
        market = str(row.get("market") or "").strip()
        address = str(row.get("address") or "").strip()
        if not market or not address:
            continue
        entry: dict = {"market": market, "address": address}
        for key in ("lat", "lng"):
            try:
                if row.get(key) is not None:
                    entry[key] = float(row[key])
            except (TypeError, ValueError):
                pass
        out.append(entry)
    return out


def _norm_market(value: str) -> str:
    v = (value or "").strip().lower()
    v = re.sub(r"[^\w\s]", " ", v)
    return re.sub(r"\s+", " ", v).strip()


def resolve_storage_unit(tenant, market_hint: str | None) -> dict | None:
    """Pick the storage unit for this shift's market.

    Market-mode walk-ins key ``Event.address`` (and often ``Event.name``) on
    the market label the BA picked ("Austin, TX"). Exact match first, then
    substring either way so "Austin" still finds "Austin, TX".

    Also token-overlap so Feel Free's check-in market
    ``Tampa / St. Pete, FL`` resolves to storage ``Tampa, FL``.
    """
    units = tenant_storage_units(tenant)
    if not units:
        return None
    hint = _norm_market(market_hint or "")
    if not hint:
        return units[0] if len(units) == 1 else None
    for u in units:
        if _norm_market(u["market"]) == hint:
            return u
    for u in units:
        m = _norm_market(u["market"])
        if hint in m or m in hint:
            return u

    # Shared city tokens (skip state abbreviations / tiny words).
    skip = {
        "fl",
        "tx",
        "az",
        "ca",
        "nv",
        "ga",
        "nc",
        "sc",
        "oh",
        "ny",
        "nj",
        "pa",
        "il",
        "co",
        "wa",
        "or",
        "st",
        "pete",
        "ft",
        "fort",
    }
    hint_tokens = {t for t in hint.split() if t not in skip and len(t) > 2}
    if not hint_tokens:
        return None
    best = None
    best_score = 0
    for u in units:
        m_tokens = {
            t
            for t in _norm_market(u["market"]).split()
            if t not in skip and len(t) > 2
        }
        score = len(hint_tokens & m_tokens)
        if score > best_score:
            best_score = score
            best = u
    return best if best_score > 0 else None


def market_hint_for_event(event) -> str:
    """Best guess at the market label for a walk-in / market-mode event."""
    for raw in (getattr(event, "address", None), getattr(event, "name", None)):
        s = (raw or "").strip()
        if s:
            return s
    return ""


def find_mileage_custom_field(template):
    """The template's Mileage / Miles-driven field, or None."""
    if template is None:
        return None
    from recaps import models as rmodels

    for f in rmodels.CustomField.objects.filter(
        custom_recap_template_id=template.id
    ).order_by("order", "id"):
        if is_mileage_custom_field(f.name):
            return f
    return None


def inject_mileage_into_field_values(
    *, template, field_values: list, payable_miles
) -> list:
    """Ensure ``field_values`` carries payable miles on the Mileage field.

    Prefer a BA-submitted value when present (construction / detour bump).
    Otherwise fill from the itinerary claim so required Mileage never blocks
    submit. Returns a new list.
    """
    field = find_mileage_custom_field(template)
    if field is None or payable_miles is None:
        return list(field_values or [])
    claim_str = str(payable_miles)
    fid = str(field.id)
    out: list = []
    found = False
    for fv in field_values or []:
        if not isinstance(fv, dict):
            continue
        raw_id = fv.get("customFieldId") or fv.get("custom_field_id")
        if str(raw_id) == fid:
            typed = str(fv.get("value") or "").strip()
            out.append(
                {
                    **fv,
                    "customFieldId": fid,
                    "value": typed if typed else claim_str,
                }
            )
            found = True
        else:
            out.append(fv)
    if not found:
        out.append({"customFieldId": fid, "value": claim_str})
    return out


def _coords_of(stop: dict) -> tuple[float, float] | None:
    try:
        lat = float(stop.get("lat"))
        lng = float(stop.get("lng"))
    except (TypeError, ValueError):
        return None
    if lat == 0.0 and lng == 0.0:
        return None
    return (lat, lng)


def build_route_points(
    *,
    started_from_storage: bool,
    storage: dict | None,
    stops: list[dict],
) -> list[tuple[float, float]]:
    """Ordered (lat, lng) waypoints for the payable itinerary."""
    points: list[tuple[float, float]] = []
    if started_from_storage and storage:
        sc = _coords_of(storage)
        if sc:
            points.append(sc)
    for stop in stops:
        c = _coords_of(stop)
        if c:
            points.append(c)
    return points


def compute_payable_miles(
    *,
    started_from_storage: bool,
    storage: dict | None,
    stops: list[dict],
) -> tuple[Decimal, str, list | None]:
    """Return (miles, route_source, route_geometry).

    YES + ≥1 stop with coords → Storage → stops chain.
    NO  + ≥2 stops with coords → stops-only chain.
    Prefer Google Directions (Maps driving miles) when
    ``GOOGLE_MAPS_API_KEY`` is set; else OSRM; else haversine.
    """
    from utils.map_matching import (
        google_directions_route_miles,
        haversine_route_miles,
        osrm_route_waypoints,
    )

    points = build_route_points(
        started_from_storage=started_from_storage,
        storage=storage,
        stops=stops,
    )
    if len(points) < 2:
        return Decimal("0.00"), "none", None

    google = google_directions_route_miles(points)
    if google and google.get("miles") is not None:
        return (
            Decimal(str(google["miles"])).quantize(Decimal("0.01")),
            "google_route",
            google.get("route"),
        )

    routed = osrm_route_waypoints(points)
    if routed and routed.get("miles") is not None:
        return (
            Decimal(str(routed["miles"])).quantize(Decimal("0.01")),
            "osrm_route",
            routed.get("route"),
        )
    fallback = haversine_route_miles(points)
    if fallback is None:
        return Decimal("0.00"), "none", None
    return (
        Decimal(str(fallback)).quantize(Decimal("0.01")),
        "haversine",
        [[p[0], p[1]] for p in points],
    )


def ensure_storage_coords(unit: dict) -> dict:
    """Geocode storage address into lat/lng when missing (Photon, best-effort)."""
    if _coords_of(unit):
        return unit
    address = (unit.get("address") or "").strip()
    if not address:
        return unit
    try:
        from utils.geocoding import photon_geocode

        coords = photon_geocode(address)
    except Exception:  # noqa: BLE001
        logger.exception("storage geocode failed for %r", address)
        return unit
    if not coords or len(coords) < 2:
        return unit
    return {**unit, "lat": float(coords[0]), "lng": float(coords[1])}


def normalize_stop_input(raw) -> dict | None:
    """Validate one BA-entered stop from the public check-in payload."""
    if not isinstance(raw, dict):
        return None
    address = str(raw.get("address") or raw.get("formattedAddress") or "").strip()
    name = str(raw.get("name") or raw.get("placeName") or "").strip()
    place_id = str(raw.get("placeId") or raw.get("place_id") or "").strip()
    try:
        lat = float(raw.get("lat"))
        lng = float(raw.get("lng"))
    except (TypeError, ValueError):
        return None
    if lat == 0.0 and lng == 0.0:
        return None
    if not address and not name:
        return None
    return {
        "name": name[:255],
        "address": address[:512],
        "placeId": place_id[:255],
        "lat": lat,
        "lng": lng,
    }


def save_payable_mileage_claim(
    *,
    ambassador,
    event,
    started_from_storage: bool,
    stops: list,
    shift_label: str = "",
    storage_market: str | None = None,
) -> tuple[dict | None, str | None]:
    """Upsert the BA's itinerary for this shift. Returns (payload, error)."""
    from ambassadors.models import PayableMileageClaim

    tenant = getattr(event, "tenant", None)
    if tenant is None or not payable_mileage_enabled(tenant):
        return None, "Mileage capture isn't set up for this brand."

    cleaned: list[dict] = []
    for raw in stops or []:
        stop = normalize_stop_input(raw)
        if stop:
            cleaned.append(stop)

    if started_from_storage and len(cleaned) < 1:
        return None, "Add each place you sampled after leaving storage."
    if not started_from_storage and len(cleaned) < 2:
        return (
            None,
            "Add at least two sampling locations so we can measure miles between them.",
        )

    hint = (storage_market or "").strip() or market_hint_for_event(event)
    storage = resolve_storage_unit(tenant, hint)
    if started_from_storage:
        if storage is None:
            # Ambiguous market — let the BA's explicit pick win later; for now
            # require a resolvable unit.
            units = tenant_storage_units(tenant)
            if len(units) == 1:
                storage = units[0]
            else:
                return (
                    None,
                    "We couldn't match this market to a storage unit. "
                    "Ask your lead, or pick the market that matches your storage.",
                )
        storage = ensure_storage_coords(storage)

    miles, source, route = compute_payable_miles(
        started_from_storage=bool(started_from_storage),
        storage=storage if started_from_storage else None,
        stops=cleaned,
    )

    label = (shift_label or "").strip()[:64]
    defaults = {
        "started_from_storage": bool(started_from_storage),
        "storage_market": (storage or {}).get("market", "") if storage else "",
        "storage_address": (storage or {}).get("address", "") if storage else "",
        "storage_lat": (storage or {}).get("lat") if storage else None,
        "storage_lng": (storage or {}).get("lng") if storage else None,
        "stops": cleaned,
        "payable_miles": miles,
        "route": route,
        "route_source": source,
        "tenant": tenant,
    }
    claim, _created = PayableMileageClaim.objects.update_or_create(
        ambassador=ambassador,
        event=event,
        shift_label=label,
        defaults=defaults,
    )
    return claim_payload(claim), None


def claim_payload(claim) -> dict:
    return {
        "uuid": str(claim.uuid),
        "startedFromStorage": bool(claim.started_from_storage),
        "storageMarket": claim.storage_market or "",
        "storageAddress": claim.storage_address or "",
        "stops": claim.stops or [],
        "payableMiles": float(claim.payable_miles or 0),
        "routeSource": claim.route_source or "",
        "shiftLabel": claim.shift_label or "",
        "completed": True,
    }


def payable_mileage_state(*, ambassador, event, shift_label: str = "") -> dict:
    """Session payload for the check-in page."""
    tenant = getattr(event, "tenant", None)
    enabled = payable_mileage_enabled(tenant)
    units = tenant_storage_units(tenant) if enabled else []
    hint = market_hint_for_event(event)
    matched = resolve_storage_unit(tenant, hint) if enabled else None
    base = {
        "enabled": enabled,
        "required": enabled,
        "storageUnits": units,
        "matchedStorage": matched,
        "marketHint": hint,
        "claim": None,
    }
    if not enabled or ambassador is None:
        return base
    from ambassadors.models import PayableMileageClaim

    label = (shift_label or "").strip()[:64]
    claim = (
        PayableMileageClaim.objects.filter(
            ambassador=ambassador, event=event, shift_label=label
        )
        .order_by("-id")
        .first()
    )
    # Fall back to unlabeled claim when editing the first shift.
    if claim is None and label:
        claim = (
            PayableMileageClaim.objects.filter(
                ambassador=ambassador, event=event, shift_label=""
            )
            .order_by("-id")
            .first()
        )
    if claim is not None:
        base["claim"] = claim_payload(claim)
    return base


def get_claim_for_submit(*, ambassador, event, shift_label: str = ""):
    from ambassadors.models import PayableMileageClaim

    label = (shift_label or "").strip()[:64]
    claim = PayableMileageClaim.objects.filter(
        ambassador=ambassador, event=event, shift_label=label
    ).first()
    if claim is None and not label:
        claim = (
            PayableMileageClaim.objects.filter(ambassador=ambassador, event=event)
            .order_by("-id")
            .first()
        )
    return claim


class NeedsPayableMileage(Exception):
    """Raised when Feel Free recap submit is missing the itinerary claim."""
