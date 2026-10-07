"""Market (city + state) for a request/event, derived from its own address.

The Master Tracker "Market" column, its market chips/filter, and the
notification-group routing used to read ``retailer.location`` first. Retailer
accounts are banner-level and shared across every store of a chain (the bulk
importer links all "Total Wine" rows to one account, stamped with whichever
store came first), so every Total Wine request showed that first store's city
(e.g. "Tucson, AZ" on a San Diego address). The request's own address is the
source of truth; ``retailer.location`` is never a per-store market.

``sync_geo_from_address`` keeps the ``state`` / ``location`` FKs consistent
with the address on save, and ``market_for`` is the read-side answer.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from events.routing import _US_STATE_NAME_TO_CODE, extract_state_code

_COUNTRY_SEGMENT_RE = re.compile(
    r"^(?:united states(?: of america)?|u\.?\s*s\.?\s*a?\.?)$", re.IGNORECASE
)
_ZIP_RE = re.compile(r"\b(\d{5})(?:-\d{4})?\b")


@dataclass(frozen=True)
class AddressGeo:
    city: str | None
    state_code: str | None
    zip: str | None


def _tidy_city(raw: str) -> str:
    city = re.sub(r"\s+", " ", raw).strip(" ,.")
    if city.isupper() or city.islower():
        city = city.title()
    return city


def _is_state_segment(segment: str, code: str) -> bool:
    tokens = segment.replace(".", " ").split()
    if not tokens:
        return False
    if tokens[0].upper() == code:
        return True
    low = re.sub(r"\d", "", segment).strip().lower()
    return _US_STATE_NAME_TO_CODE.get(low) == code


def parse_address_geo(address: str | None) -> AddressGeo:
    """Pull ``(city, state_code, zip)`` out of a US street address.

    Handles "8740 Rio San Diego Dr, San Diego, CA 92108", Google-Places
    ", USA" suffixes, "EDMOND, OK, 73034" and full state names. City is
    only returned when it is its own comma segment right before the state
    (a segment that starts with a street number is never a city).
    """
    if not address:
        return AddressGeo(None, None, None)
    code = extract_state_code(address)
    if not code:
        return AddressGeo(None, None, None)

    segments = [
        s.strip()
        for s in re.sub(r"[\t ]+", " ", address).split(",")
        if s.strip() and not _COUNTRY_SEGMENT_RE.match(s.strip())
    ]
    zip_code: str | None = None
    city: str | None = None
    for idx in range(len(segments) - 1, -1, -1):
        seg = segments[idx]
        if not _is_state_segment(seg, code):
            continue
        tail = " ".join(segments[idx:])
        zm = _ZIP_RE.search(tail)
        zip_code = zm.group(1) if zm else None
        if idx > 0:
            candidate = segments[idx - 1]
            if candidate and not candidate[0].isdigit() and not _ZIP_RE.search(
                candidate
            ):
                city = _tidy_city(candidate)
        break
    return AddressGeo(city or None, code, zip_code)


@dataclass(frozen=True)
class Market:
    city: str | None
    state_code: str | None

    @property
    def label(self) -> str | None:
        parts = [p for p in (self.city, self.state_code) if p]
        return ", ".join(parts) if parts else None


def _loc_state_code(location) -> str | None:
    state = getattr(location, "state", None) if location is not None else None
    code = getattr(state, "code", None)
    return code.upper() if code else None


def market_for(obj) -> Market:
    """Market for a Request or Event: its address first, then its own
    ``location`` / ``state`` FKs. Never ``retailer.location``."""
    geo = parse_address_geo(getattr(obj, "address", None))
    location = getattr(obj, "location", None)
    if geo.state_code:
        city = geo.city
        if not city and location is not None and _loc_state_code(location) == geo.state_code:
            city = location.name
        return Market(city, geo.state_code)
    if location is not None:
        return Market(location.name or None, _loc_state_code(location))
    state = getattr(obj, "state", None)
    code = getattr(state, "code", None)
    return Market(None, code.upper() if code else None)


@dataclass(frozen=True)
class GeoResolution:
    """What the address says the ``state`` / ``location`` FKs should be.

    ``location_id`` is None when the address city has no unique Location in
    that state — callers clear a conflicting location rather than keep a
    wrong market, and the data-fix report flags the row.
    """

    state_id: int | None
    location_id: int | None
    geo: AddressGeo
    location_ambiguous: bool = False


class GeoLookup:
    """State / Location lookups for :func:`resolve_geo_for_address`.
    Hits the DB per call; :class:`PreloadedGeoLookup` caches for batch jobs."""

    def state_id(self, code: str) -> int | None:
        from events.models import State

        return (
            State.objects.filter(code__iexact=code)
            .order_by("id")
            .values_list("id", flat=True)
            .first()
        )

    def city_locations(self, city: str, state_id: int) -> list[tuple[int, str]]:
        from events.models import Location

        return list(
            Location.objects.filter(name__iexact=city, state_id=state_id)
            .order_by("id")
            .values_list("id", "zip")
        )

    def location(self, location_id: int) -> tuple[int | None, str] | None:
        from events.models import Location

        row = (
            Location.objects.filter(id=location_id)
            .values_list("state_id", "name")
            .first()
        )
        return (row[0], row[1] or "") if row else None


class PreloadedGeoLookup(GeoLookup):
    def __init__(self) -> None:
        from events.models import Location, State

        self._states: dict[str, int] = {}
        for sid, code in State.objects.order_by("-id").values_list("id", "code"):
            if code:
                self._states[code.strip().upper()] = sid
        self._by_id: dict[int, tuple[int | None, str]] = {}
        self._by_city: dict[tuple[str, int], list[tuple[int, str]]] = {}
        for lid, name, zip_code, sid in Location.objects.order_by("id").values_list(
            "id", "name", "zip", "state_id"
        ):
            self._by_id[lid] = (sid, name or "")
            if sid is not None and name:
                self._by_city.setdefault((name.strip().lower(), sid), []).append(
                    (lid, zip_code or "")
                )

    def state_id(self, code: str) -> int | None:
        return self._states.get(code.strip().upper())

    def city_locations(self, city: str, state_id: int) -> list[tuple[int, str]]:
        return list(self._by_city.get((city.strip().lower(), state_id), []))

    def location(self, location_id: int) -> tuple[int | None, str] | None:
        return self._by_id.get(location_id)


def resolve_geo_for_address(
    address: str | None, lookup: GeoLookup | None = None
) -> GeoResolution | None:
    """Map an address to ``State`` / ``Location`` rows. None when the
    address carries no parseable US state (nothing to derive)."""
    lookup = lookup or GeoLookup()
    geo = parse_address_geo(address)
    if not geo.state_code:
        return None
    state_id = lookup.state_id(geo.state_code)
    if state_id is None:
        return None
    if not geo.city:
        return GeoResolution(state_id, None, geo)
    ids = lookup.city_locations(geo.city, state_id)
    if len(ids) == 1:
        return GeoResolution(state_id, ids[0][0], geo)
    if len(ids) > 1:
        by_zip = [i for i, z in ids if geo.zip and (z or "").strip() == geo.zip]
        if len(by_zip) == 1:
            return GeoResolution(state_id, by_zip[0], geo)
        return GeoResolution(state_id, ids[0][0], geo, location_ambiguous=True)
    return GeoResolution(state_id, None, geo)


def geo_changes(
    obj,
    resolution: GeoResolution | None,
    *,
    clear_city_mismatch: bool = False,
    lookup: GeoLookup | None = None,
) -> dict[str, int | None]:
    """FK changes (``state_id`` / ``location_id``) that would make ``obj``
    agree with its address. Empty when already consistent or underivable.

    A location in another state is always cleared; one in the right state
    but a different city is relinked when the address city has a Location
    row, and only cleared with ``clear_city_mismatch``. A location that
    can't be checked (address has no city) is left alone.
    """
    if resolution is None:
        return {}
    lookup = lookup or GeoLookup()

    changes: dict[str, int | None] = {}
    if obj.state_id != resolution.state_id:
        changes["state_id"] = resolution.state_id

    current_loc_id = obj.location_id
    if resolution.location_id is not None:
        if current_loc_id != resolution.location_id:
            changes["location_id"] = resolution.location_id
    elif current_loc_id is not None:
        loc = lookup.location(current_loc_id)
        if loc is not None:
            loc_state_id, loc_name = loc
            wrong_state = loc_state_id != resolution.state_id
            wrong_city = (
                clear_city_mismatch
                and bool(resolution.geo.city)
                and loc_name.strip().lower() != resolution.geo.city.lower()
            )
            if wrong_state or wrong_city:
                changes["location_id"] = None
    return changes


class AddressGeoSyncMixin:
    """Model mixin: on save, align ``state`` / ``location`` with ``address``
    for new rows and whenever the address or geo FKs change. Untouched
    rows (status saves, clock-ins) skip the lookups."""

    @classmethod
    def from_db(cls, db, field_names, values):
        instance = super().from_db(db, field_names, values)
        instance._loaded_geo = _geo_snapshot(instance)
        return instance

    def save(self, *args, **kwargs):
        if "address" in self.__dict__:
            loaded = getattr(self, "_loaded_geo", None)
            if loaded is None or loaded != _geo_snapshot(self):
                update_fields = kwargs.get("update_fields")
                touched = sync_geo_from_address(self, update_fields)
                if touched and update_fields is not None:
                    kwargs["update_fields"] = list(
                        dict.fromkeys([*update_fields, *touched])
                    )
        super().save(*args, **kwargs)
        self._loaded_geo = _geo_snapshot(self)


def _geo_snapshot(instance) -> tuple:
    d = instance.__dict__
    return (d.get("address"), d.get("state_id"), d.get("location_id"))


def sync_geo_from_address(obj, update_fields=None) -> list[str]:
    """Align ``obj.state`` / ``obj.location`` with ``obj.address`` in place.

    Returns the touched field names so ``save(update_fields=...)`` callers
    can include them. No-op when the save doesn't touch geo fields.
    """
    if update_fields is not None and not (
        {"address", "location", "location_id", "state", "state_id"}
        & set(update_fields)
    ):
        return []
    changes = geo_changes(obj, resolve_geo_for_address(obj.address))
    touched: list[str] = []
    for attr, value in changes.items():
        setattr(obj, attr, value)
        touched.append(attr.removesuffix("_id"))
    return touched
