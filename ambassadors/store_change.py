"""A BA fixes the store their walk-in shift is logged at.

A BA who typed or tapped the wrong store at identify used to stay logged at
the wrong place for the whole shift: the recap counted for the wrong store,
the Walk-ups card showed a big "miles from venue" and staff fixed the event
by hand. ``change_checkin_store`` moves the shift IN PLACE to where the phone
says the BA is, under the same ownership rules as "Wrong program? Change it"
(``ambassadors.program_change``): only an event this BA's own check-in opened
that nobody else is on, before their recap is filed. Clock-ins, the booking,
GPS pings and mileage all stay on the same event row.

The new place is verified against the phone's GPS: an address more than
``CHECKIN_STORE_FIX_MAX_KM`` (1 km) from the fix needs an explicit "I'm sure",
and that override is recorded. The store resolves through the same rules as
identify (tenant retailer by name or alias, known chain, the store another
record names at the same street address); a store Spark has never seen gets a
new tenant ``Retailer``, flagged on the audit row as created by a BA check-in
so staff can review, rename or merge it. Every move writes a
``CheckinStoreChange`` row.

Time-sheet-only brands record event LOCATIONS, never stores: the move is
address-only there and never touches retailers.
"""

from __future__ import annotations

import logging
import math
import re

from django.conf import settings
from django.db import connection, transaction

logger = logging.getLogger(__name__)

DEFAULT_MAX_KM = 1.0
# A known store this close to the phone (or at the same street address) is
# offered as the store name.
NEARBY_STORE_METERS = 150

ASK_LEAD_MESSAGE = (
    "This shift is shared with your team or was set up by your lead, so the "
    "store can't be changed from here. Message your lead and they'll fix it."
)
RECAP_FILED_MESSAGE = (
    "Your recap is already in, so the store can't be changed here. "
    "Message your lead if it's wrong."
)
NOT_CLOCKED_IN_MESSAGE = "Clock in first. You can fix the store once your shift has started."
MARKET_MESSAGE = (
    "This link logs your market, not a store. Log each spot with Log this stop instead."
)
ADDRESS_NEEDED_MESSAGE = "Enter the address you're working at."
STORE_NAME_NEEDED_MESSAGE = "Enter the store name so your work counts for the right store."
SAME_PLACE_MESSAGE = "Your shift is already at that address."


class StoreChangeRefused(ValueError):
    """The move is not allowed. ``code`` is the JSON error code."""

    def __init__(self, code: str, message: str, status: int = 409, extra: dict | None = None):
        self.code = code
        self.status = status
        self.extra = extra or {}
        super().__init__(message)


def max_distance_km() -> float:
    try:
        value = float(getattr(settings, "CHECKIN_STORE_FIX_MAX_KM", DEFAULT_MAX_KM))
    except (TypeError, ValueError):
        return DEFAULT_MAX_KM
    return value if value > 0 else DEFAULT_MAX_KM


def haversine_km(a_lat, a_lng, b_lat, b_lng) -> float | None:
    try:
        rlat1, rlat2 = math.radians(float(a_lat)), math.radians(float(b_lat))
        dlat = rlat2 - rlat1
        dlng = math.radians(float(b_lng) - float(a_lng))
        h = math.sin(dlat / 2) ** 2 + math.cos(rlat1) * math.cos(rlat2) * math.sin(dlng / 2) ** 2
        return 2 * 6371.0088 * math.asin(min(1.0, math.sqrt(h)))
    except (TypeError, ValueError):
        return None


def far_message(distance_km: float) -> str:
    miles = distance_km * 0.621371
    shown = f"{miles:.1f}".rstrip("0").rstrip(".") if miles < 10 else str(round(miles))
    return (
        f"That address is about {shown} miles from where your phone says you are. "
        "Check it's the store you're standing in. If it is, tap I'm sure."
    )


def parse_point(lat, lng) -> tuple[float, float] | None:
    """``(lat, lng)`` floats, or None for missing / out-of-range / null island."""
    try:
        lat_f, lng_f = float(lat), float(lng)
    except (TypeError, ValueError):
        return None
    if not (-90 <= lat_f <= 90 and -180 <= lng_f <= 180):
        return None
    if lat_f == 0 and lng_f == 0:
        return None
    if math.isnan(lat_f) or math.isnan(lng_f):
        return None
    return lat_f, lng_f


def _float_or_none(value) -> float | None:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(out) or out < 0 else out


# --------------------------------------------------------------------------
# Who may move
# --------------------------------------------------------------------------
def store_change_refusal(*, event, ambassador) -> tuple[str, str] | None:
    """``(code, message)`` when this BA may NOT move this event, else None.
    Read-only; the move re-checks under a row lock. Same ownership rules as
    ``program_change_refusal``, minus the "more than one program" one."""
    from ambassadors import checkin_web
    from ambassadors.models import AmbassadorEvent
    from events.models import Event
    from recaps.models import CustomRecap, Recap
    from tenants.models import Tenant

    tenant = getattr(event, "tenant", None)
    if tenant is None:
        return "shared_event", ASK_LEAD_MESSAGE
    if checkin_web.has_recap(ambassador_id=ambassador.id, event_id=event.id):
        return "recap_filed", RECAP_FILED_MESSAGE
    if checkin_web.tenant_location_mode(tenant) == Tenant.CHECKIN_LOCATION_MARKET:
        return "market_mode", MARKET_MESSAGE

    if getattr(event, "request_id", None):
        return "shared_event", ASK_LEAD_MESSAGE
    if getattr(event, "co_activation_host_id", None) or Event.objects.filter(
        co_activation_host_id=event.id
    ).exists():
        return "shared_event", ASK_LEAD_MESSAGE
    if getattr(event, "custom_recap_template_id", None):
        return "shared_event", ASK_LEAD_MESSAGE
    user_id = getattr(ambassador, "user_id", None)
    if not user_id or getattr(event, "created_by_id", None) != user_id:
        return "shared_event", ASK_LEAD_MESSAGE
    if (event.address or "").startswith("Product Seeding · BA "):
        return "shared_event", ASK_LEAD_MESSAGE

    if (
        AmbassadorEvent.objects.filter(event_id=event.id)
        .exclude(ambassador_id=ambassador.id)
        .exists()
    ):
        return "shared_event", ASK_LEAD_MESSAGE
    if (
        CustomRecap.objects.filter(event_id=event.id)
        .exclude(ambassador_id=ambassador.id)
        .exists()
        or Recap.objects.filter(event_id=event.id)
        .exclude(ambassador_id=ambassador.id)
        .exists()
    ):
        return "shared_event", ASK_LEAD_MESSAGE

    state = checkin_web.clock_state(ambassador_id=ambassador.id, event_id=event.id)
    if state.get("state") not in ("clocked_in", "clocked_out"):
        return "not_clocked_in", NOT_CLOCKED_IN_MESSAGE
    return None


def address_only(event) -> bool:
    """Time-sheet tenants record a location, never a store."""
    from ambassadors import checkin_web

    return checkin_web.is_timesheet_only(getattr(event, "tenant", None))


def store_name_needed(event) -> bool:
    """A Retail Sampling shift must name its store (same rule as identify)."""
    from ambassadors import checkin_web

    if address_only(event):
        return False
    return checkin_web.store_name_required(event.tenant, getattr(event, "event_type", None))


def is_venue_program(event) -> bool:
    from ambassadors import checkin_web

    return checkin_web.is_event_activation_type(getattr(event, "event_type", None))


def current_store_name(event) -> str:
    """The store this shift is logged at, as the BA should read it."""
    from ambassadors import checkin_web

    if address_only(event):
        return ""
    retailer = getattr(event, "retailer", None)
    if retailer is not None and (retailer.name or "").strip():
        return retailer.name.strip()
    name = checkin_web._store_display_name(event.name or "", event.address or "")
    if not name or checkin_web.normalize_place(name) == checkin_web.normalize_place(
        event.address or ""
    ):
        return ""
    return name


def store_change_state(*, event, ambassador) -> dict:
    """``session.storeChange`` for the check-in context."""
    refusal = store_change_refusal(event=event, ambassador=ambassador)
    base = {
        "addressOnly": address_only(event),
        "storeNameRequired": store_name_needed(event),
        "venue": is_venue_program(event),
        "maxDistanceKm": max_distance_km(),
        "storeName": current_store_name(event),
    }
    if refusal is None:
        return {"allowed": True, "code": "", "message": "", **base}
    code, message = refusal
    return {"allowed": False, "code": code, "message": message, **base}


# --------------------------------------------------------------------------
# Known store near a point / at an address
# --------------------------------------------------------------------------
def known_store_near(tenant, *, point: tuple[float, float] | None, address: str) -> dict | None:
    """The store Spark already knows at this spot: a tenant record within
    ``NEARBY_STORE_METERS`` of ``point`` (Account Map venues, earlier events),
    else one at the same normalized street address. ``None`` when nothing
    matches. Never creates anything."""
    from ambassadors import checkin_web
    from events.models import Event, Request, Retailer

    if tenant is None:
        return None
    best: dict | None = None

    def offer(name: str, addr: str, *, retailer=None, source: str, meters: float | None):
        nonlocal best
        label = (name or "").strip()
        if not label or checkin_web._looks_like_address(label):
            return
        if checkin_web.normalize_place(label) == checkin_web.normalize_place(addr or ""):
            return
        rank = meters if meters is not None else NEARBY_STORE_METERS + 1
        if best is not None and best["_rank"] <= rank:
            return
        best = {
            "name": label[:255],
            "address": (addr or "").strip(),
            "retailerUuid": str(retailer.uuid) if retailer is not None else None,
            "source": source,
            "distanceM": round(meters) if meters is not None else None,
            "_rank": rank,
        }

    if point is not None:
        lat, lng = point
        dlat = NEARBY_STORE_METERS / 111_000 * 1.5
        dlng = dlat / max(0.2, math.cos(math.radians(lat)))
        box = {
            "coordinates__0__gte": lat - dlat,
            "coordinates__0__lte": lat + dlat,
            "coordinates__1__gte": lng - dlng,
            "coordinates__1__lte": lng + dlng,
        }
        for req in (
            Request.objects.filter(tenant=tenant, deleted_at__isnull=True, **box)
            .select_related("retailer")
            .order_by("-id")[:50]
        ):
            km = haversine_km(lat, lng, req.coordinates[0], req.coordinates[1])
            if km is None or km * 1000 > NEARBY_STORE_METERS:
                continue
            name = (req.retailer_name or "").strip() or getattr(req.retailer, "name", "") or ""
            offer(name, req.address or req.retailer_address or "", retailer=req.retailer,
                  source="account_map", meters=km * 1000)
        for ev in (
            Event.objects.filter(tenant=tenant, co_activation_host__isnull=True, **box)
            .select_related("retailer")
            .order_by("-id")[:50]
        ):
            km = haversine_km(lat, lng, ev.coordinates[0], ev.coordinates[1])
            if km is None or km * 1000 > NEARBY_STORE_METERS:
                continue
            name = getattr(ev.retailer, "name", "") or checkin_web._store_display_name(
                ev.name or "", ev.address or ""
            )
            offer(name, ev.address or "", retailer=ev.retailer, source="earlier_event",
                  meters=km * 1000)
        if best is not None:
            best.pop("_rank", None)
            return best

    address = (address or "").strip()
    if not address:
        return None
    for retailer in Retailer.objects.filter(tenant=tenant).exclude(address__isnull=True).exclude(
        address=""
    ):
        if checkin_web.addresses_fuzzy_match(address, retailer.address or ""):
            offer(retailer.name, retailer.address, retailer=retailer, source="same_address",
                  meters=None)
            if best is not None:
                break
    if best is None:
        name = checkin_web.known_store_name(tenant.id, address)
        if name:
            offer(name, address, source="same_address", meters=None)
    if best is None:
        found = checkin_web.resolve_walkin_location(tenant, store_name="", address=address)
        if found.get("retailer_name"):
            retailer = (
                Retailer.objects.filter(pk=found["retailer_id"]).first()
                if found.get("retailer_id")
                else None
            )
            offer(found["retailer_name"], address, retailer=retailer, source="same_address",
                  meters=None)
    if best is not None:
        best.pop("_rank", None)
    return best


def lookup_place(*, event, gps: tuple[float, float] | None, address: str = "") -> dict:
    """What the page shows before the BA confirms: the address (reverse
    geocoded from GPS when none is typed), where it is, how far that is from
    GPS, and the store Spark already knows there. Never writes."""
    from utils.geocoding import photon_geocode, photon_reverse

    address = re.sub(r"\s+", " ", address or "").strip()
    out = {
        "address": "",
        "city": "",
        "state": "",
        "postcode": "",
        "placeLatitude": None,
        "placeLongitude": None,
        "distanceKm": None,
        "far": False,
        "maxDistanceKm": max_distance_km(),
        "store": None,
    }
    place: tuple[float, float] | None = None
    if address:
        out["address"] = address
        coords = None
        try:
            coords = photon_geocode(address)
        except Exception:  # noqa: BLE001 — a lookup must never block the BA
            coords = None
        if coords:
            place = parse_point(coords[0], coords[1])
    elif gps is not None:
        hit = None
        try:
            hit = photon_reverse(gps[0], gps[1])
        except Exception:  # noqa: BLE001
            hit = None
        if hit:
            out.update(
                address=hit.get("address") or "",
                city=hit.get("city") or "",
                state=hit.get("state") or "",
                postcode=hit.get("postcode") or "",
            )
        place = gps
    if place is not None:
        out["placeLatitude"], out["placeLongitude"] = place
    if place is not None and gps is not None:
        km = haversine_km(gps[0], gps[1], place[0], place[1])
        if km is not None:
            out["distanceKm"] = round(km, 3)
            out["far"] = km > max_distance_km()
    if not address_only(event):
        out["store"] = known_store_near(
            event.tenant,
            point=gps if not address else place,
            address=out["address"],
        )
    return out


# --------------------------------------------------------------------------
# Store resolution for the new place
# --------------------------------------------------------------------------
def _resolve_retailer(*, event, store_name: str, address: str, actor) -> tuple[object | None, bool, str]:
    """``(retailer, created, source)`` for the new place.

    In order: the typed name resolved like identify does (tenant retailer by
    name or accepted alias, else a known chain, found-or-created by name);
    a tenant retailer at the same street address; the store another record
    names at that address. A typed store that matches none of those is new:
    a ``Retailer`` is created with the name and address (retail programs
    only; a venue name on an activation stays in the title). Lookup always
    comes first, so a retry or a second BA never makes a duplicate."""
    from ambassadors import checkin_web
    from events.models import Retailer

    tenant = event.tenant
    typed = checkin_web._usable_store_name(store_name, address)
    found = checkin_web.resolve_walkin_location(
        tenant, store_name=typed, address=address, exclude_event_id=event.id
    )
    if found.get("retailer_id"):
        return Retailer.objects.filter(pk=found["retailer_id"]).first(), False, found["retailer_source"]
    if found.get("retailer_name"):
        # A known chain the tenant has no row for yet (same as identify).
        existing = (
            Retailer.objects.filter(tenant_id=tenant.id, name__iexact=found["retailer_name"].strip())
            .order_by("id")
            .first()
        )
        if existing is not None:
            return existing, False, found["retailer_source"]
        created = Retailer.objects.create(
            tenant_id=tenant.id, name=found["retailer_name"].strip()[:255], created_by=actor
        )
        return created, True, "new_chain"

    # A tenant store at the same street address.
    for retailer in Retailer.objects.filter(tenant_id=tenant.id).exclude(
        address__isnull=True
    ).exclude(address="").order_by("id"):
        if checkin_web.addresses_fuzzy_match(address, retailer.address or ""):
            if not typed or checkin_web.normalize_place(typed) in checkin_web.normalize_place(
                retailer.name or ""
            ) or checkin_web.normalize_place(retailer.name or "") in checkin_web.normalize_place(typed):
                return retailer, False, "same_address_retailer"

    if not typed or is_venue_program(event):
        return None, False, ""

    existing = (
        Retailer.objects.filter(tenant_id=tenant.id, name__iexact=typed)
        .order_by("id")
        .first()
    )
    if existing is not None:
        return existing, False, "tenant_retailer"
    created = Retailer.objects.create(
        tenant_id=tenant.id,
        name=typed[:255],
        address=address[:100] or None,
        created_by=actor,
        updated_by=actor,
    )
    return created, True, "new_store"


def _state_for(address: str, geocoded_state: str = ""):
    from events.market import parse_address_geo
    from events.models import State
    from events.us_states import US_STATE_NAME_TO_CODE

    code = (parse_address_geo(address or "").state_code or "").upper()
    if not code and geocoded_state:
        raw = geocoded_state.strip()
        code = raw.upper() if len(raw) == 2 else US_STATE_NAME_TO_CODE.get(raw.lower(), "")
    if not code:
        return None
    return State.objects.filter(code__iexact=code).order_by("id").first()


def new_event_title(event, *, store_name: str, address: str) -> str:
    """"M/D/YYYY - <address> (<Store>) · <Program>" — the identify format."""
    from ambassadors import checkin_web

    program = ""
    etype = getattr(event, "event_type", None)
    if (
        etype is not None
        and not address_only(event)
        and len(checkin_web.selectable_event_types(event.tenant)) > 1
    ):
        program = (etype.name or "").strip()
    return checkin_web.walkin_event_name(
        store_name="" if address_only(event) else store_name,
        address=address,
        on_date=checkin_web.event_calendar_date(event),
        program=program,
    )


def _same_day_sibling(event, address: str):
    """Another walk-in event of the same program at the new address that day.
    Not merged (see the module docstring); recorded for staff."""
    from ambassadors import checkin_web
    from events.models import Event

    day = getattr(event, "date", None)
    if day is None:
        return None
    lo, hi = day.replace(hour=0, minute=0, second=0), day.replace(hour=23, minute=59, second=59)
    for other in Event.objects.filter(
        tenant_id=event.tenant_id,
        date__gte=lo,
        date__lte=hi,
        event_type_id=event.event_type_id,
        co_activation_host__isnull=True,
    ).exclude(pk=event.pk):
        if checkin_web.addresses_fuzzy_match(address, other.address or "") or (
            checkin_web.normalize_place(address) == checkin_web.normalize_place(other.address or "")
        ):
            return other
    return None


# --------------------------------------------------------------------------
# The move
# --------------------------------------------------------------------------
def change_checkin_store(
    *,
    event,
    ambassador,
    address: str,
    store_name: str = "",
    store_number: str = "",
    gps: tuple[float, float] | None = None,
    gps_accuracy_m=None,
    place: tuple[float, float] | None = None,
    geocoded_state: str = "",
    address_source: str = "",
    confirm_far: bool = False,
    request_key: str = "",
) -> dict:
    """Move this BA's walk-in event to ``address`` (and store), in place.

    Raises ``StoreChangeRefused`` with a BA-facing message when not allowed,
    when a store name is needed, or when the address is far from GPS and the
    BA has not confirmed. Returns ``{change, event, repeated}``; ``repeated``
    is True when this exact move already happened (a retry or double tap)."""
    from ambassadors import checkin_web
    from ambassadors.models import CheckinStoreChange, WalkupRecapDraft
    from events.market import geo_changes, resolve_geo_for_address
    from events.models import Event
    from recaps.filed import custom_filed_q
    from recaps.models import CustomRecap
    from utils.geocoding import photon_geocode

    address = re.sub(r"\s+", " ", address or "").strip()
    if not address:
        raise StoreChangeRefused("address_required", ADDRESS_NEEDED_MESSAGE, status=400)
    if address.startswith("Product Seeding · BA "):
        raise StoreChangeRefused("address_required", ADDRESS_NEEDED_MESSAGE, status=400)
    request_key = (request_key or "").strip()[:64]
    store_number = checkin_web.real_store_number((store_number or "")[:64])
    source = address_source if address_source in ("gps", "search", "typed") else "typed"

    with transaction.atomic():
        locked = (
            Event.objects.select_for_update(of=("self",))
            if connection.vendor != "sqlite"
            else Event.objects
        )
        event = (
            locked.select_related("tenant", "event_type", "retailer", "state")
            .filter(pk=event.pk)
            .first()
        )

        # A retry of a move that already landed (the response was lost, or a
        # double tap) returns that move instead of refusing "same place".
        if request_key:
            done = (
                CheckinStoreChange.objects.filter(
                    event_id=event.id, ambassador_id=ambassador.id, request_key=request_key
                )
                .order_by("-id")
                .first()
            )
            if done is not None:
                return {"change": done, "event": event, "repeated": True}

        refusal = store_change_refusal(event=event, ambassador=ambassador)
        if refusal is not None:
            raise StoreChangeRefused(*refusal)

        timesheet = address_only(event)
        typed = "" if timesheet else checkin_web._usable_store_name(store_name, address)

        # Where the new address is. A GPS-sourced address IS the fix.
        if place is None and source == "gps" and gps is not None:
            place = gps
        if place is None:
            try:
                coords = photon_geocode(address)
            except Exception:  # noqa: BLE001
                coords = None
            if coords:
                place = parse_point(coords[0], coords[1])
        distance = (
            haversine_km(gps[0], gps[1], place[0], place[1])
            if gps is not None and place is not None
            else None
        )
        far = distance is not None and distance > max_distance_km()
        if far and not confirm_far:
            raise StoreChangeRefused(
                "far_from_gps",
                far_message(distance),
                status=409,
                extra={"distanceKm": round(distance, 3), "maxDistanceKm": max_distance_km()},
            )

        actor = ambassador.user
        from_address = event.address or ""
        from_store = current_store_name(event)
        from_retailer = event.retailer
        from_state = getattr(event.state, "code", "") if event.state_id else ""

        if timesheet:
            retailer, created, retailer_source = None, False, ""
        else:
            retailer, created, retailer_source = _resolve_retailer(
                event=event, store_name=typed, address=address, actor=actor
            )
        display_store = typed or (getattr(retailer, "name", "") or "")
        if not timesheet and not display_store:
            known = known_store_near(event.tenant, point=None, address=address)
            if known:
                display_store = known["name"]
        if store_name_needed(event) and not display_store:
            raise StoreChangeRefused(
                "store_name_required", STORE_NAME_NEEDED_MESSAGE, status=400
            )

        same_address = checkin_web.normalize_place(address) == checkin_web.normalize_place(
            from_address
        )
        same_store = (
            checkin_web.normalize_place(display_store) == checkin_web.normalize_place(from_store)
        )
        if same_address and same_store and (not store_number):
            raise StoreChangeRefused("same_place", SAME_PLACE_MESSAGE, status=400)

        old_name = event.name or ""
        new_name = new_event_title(event, store_name=display_store, address=address)
        state = _state_for(address, geocoded_state)

        # Probe the new address's geo FKs on a throwaway copy so only the
        # state / location columns change.
        probe = Event(address=address, state_id=event.state_id, location_id=event.location_id)
        geo = geo_changes(probe, resolve_geo_for_address(address))
        updates = {
            "address": address,
            "name": new_name,
            "coordinates": [place[0], place[1]] if place is not None else None,
            "retailer": retailer,
            "updated_by": actor,
        }
        if "state_id" in geo:
            updates["state_id"] = geo["state_id"]
        elif state is not None:
            updates["state_id"] = state.id
        if "location_id" in geo:
            updates["location_id"] = geo["location_id"]
        elif "state_id" in updates and updates["state_id"] != event.state_id:
            # A city in the old state is wrong now; the address names no
            # city the catalog knows.
            updates["location_id"] = None
        # update(), not save(): the address-geo mixin would re-derive the
        # state and undo a geocoded fill.
        Event.objects.filter(pk=event.pk).update(**updates)
        event = (
            Event.objects.select_related("tenant", "event_type", "retailer", "state", "location")
            .filter(pk=event.pk)
            .first()
        )

        # The clock-out stub carries the old title, store and state, and a
        # reused stub keeps them at submit. Point it at the new place.
        stub_ids = list(
            CustomRecap.objects.filter(event_id=event.id, ambassador_id=ambassador.id)
            .exclude(custom_filed_q())
            .values_list("id", flat=True)
        )
        if stub_ids:
            CustomRecap.objects.filter(id__in=stub_ids).update(
                name=new_name,
                retailer=event.retailer,
                state=event.state,
                location=event.location,
            )

        # Torch-style "Store name / Store #" boxes on the recap: the saved
        # draft takes the corrected store.
        if not timesheet and display_store:
            for row in WalkupRecapDraft.objects.filter(
                event_id=event.id, ambassador_id=ambassador.id
            ):
                payload = row.payload if isinstance(row.payload, dict) else {}
                if isinstance(payload.get("store"), dict):
                    payload["store"] = {"name": display_store[:255], "number": store_number}
                    row.payload = payload
                    row.save(update_fields=["payload"])

        sibling = _same_day_sibling(event, address)
        change = CheckinStoreChange.objects.create(
            tenant_id=event.tenant_id,
            event_id=event.id,
            ambassador_id=ambassador.id,
            from_address=from_address,
            to_address=address,
            from_store_name=from_store[:255],
            to_store_name=("" if timesheet else display_store)[:255],
            store_number=store_number,
            from_retailer=from_retailer,
            to_retailer=retailer,
            created_retailer=bool(created),
            old_event_name=old_name[:255],
            new_event_name=new_name[:255],
            gps_latitude=gps[0] if gps else None,
            gps_longitude=gps[1] if gps else None,
            gps_accuracy_m=_float_or_none(gps_accuracy_m),
            place_latitude=place[0] if place else None,
            place_longitude=place[1] if place else None,
            distance_km=round(distance, 3) if distance is not None else None,
            far_override=bool(far and confirm_far),
            address_source=source,
            request_key=request_key,
            detail={
                "retailerSource": retailer_source,
                "addressOnly": timesheet,
                "maxDistanceKm": max_distance_km(),
                "fromState": from_state,
                "toState": getattr(event.state, "code", "") if event.state_id else "",
                "sameDayEventUuid": str(sibling.uuid) if sibling is not None else None,
            },
        )

    logger.info(
        "checkin store change event=%s ambassador=%s %r -> %r km=%s override=%s created_retailer=%s",
        event.id,
        ambassador.id,
        from_address,
        address,
        change.distance_km,
        change.far_override,
        change.created_retailer,
    )
    return {"change": change, "event": event, "repeated": False}


def latest_store_change(*, event_id: int, ambassador_id: int):
    from ambassadors.models import CheckinStoreChange

    return (
        CheckinStoreChange.objects.filter(event_id=event_id, ambassador_id=ambassador_id)
        .select_related("to_retailer")
        .order_by("-created_at", "-id")
        .first()
    )


def store_change_payload(change) -> dict:
    """``storeChange`` on the change-store response."""
    return {
        "fromAddress": change.from_address,
        "toAddress": change.to_address,
        "fromStore": change.from_store_name,
        "toStore": change.to_store_name,
        "distanceKm": change.distance_km,
        "farOverride": change.far_override,
        "createdStore": change.created_retailer,
    }
