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
from dataclasses import dataclass, replace

from events.us_states import US_STATE_CODES, US_STATE_NAME_TO_CODE

_COUNTRY_SEGMENT_RE = re.compile(
    r"^(?:united states(?: of america)?|u\.?\s*s\.?\s*a?\.?)$", re.IGNORECASE
)
_TRAILING_COUNTRY_RE = re.compile(
    r"[\s,]+(?:united states(?: of america)?|usa|u\.s\.a?\.?)\.?$", re.IGNORECASE
)
_ZIP_RE = re.compile(r"(?<!\d)(\d{5})(?:-\d{4})?(?!\d)")
_TRAILING_ZIP_RE = re.compile(r"[\s,]+(\d{5})(?:-\d{4})?(?:\s+\d{1,5})?$")
_GLUED_TRAILING_ZIP_RE = re.compile(r"(?<![A-Za-z])([A-Za-z]{2})(\d{5}(?:-\d{4})?)$")
_TRAILING_CODE_RE = re.compile(r"(?:^|[\s.,])([A-Za-z]{2})$")
_TRAILING_CODE_SHORT_NUMBER_RE = re.compile(r"(?:^|[\s.,])([A-Za-z]{2})[\s,]+\d{2,4}$")
_STATE_NAMES_LONGEST_FIRST = sorted(US_STATE_NAME_TO_CODE, key=len, reverse=True)


_VENUE_SEPARATOR_RE = re.compile(r"\s+(?:-|–|—|//|\|)\s+")
_STREET_SUFFIXES = frozenset(
    "street st avenue ave road rd highway hwy parkway pkwy boulevard blvd drive dr "
    "lane ln way trail trl court ct circle cir expressway expy freeway fwy".split()
)
_MAX_CITY_WORDS = 4


@dataclass(frozen=True)
class AddressGeo:
    city: str | None
    state_code: str | None
    zip: str | None
    street_tail: str | None = None
    """Text right before the state when street and city share it ("4100 Blue
    Diamond Rd Las Vegas") — :func:`resolve_geo_for_address` matches its last
    words against the Location catalog."""


def _tidy_city(raw: str) -> str:
    city = re.sub(r"\s+", " ", raw).strip(" ,.")
    if city.isupper() or city.islower():
        city = re.sub(r"'S\b", "'s", city.title())
    return city


def _city_or_tail(text: str) -> tuple[str | None, str | None]:
    """``(city, street_tail)`` for the text before the state. A venue prefix
    ("Center Parc Stadium - Atlanta") is dropped; text with digits is a street
    plus maybe a city (tail); text ending in a street word is no city."""
    text = _VENUE_SEPARATOR_RE.split(text.strip())[-1].strip(" .,")
    if not text:
        return None, None
    if any(ch.isdigit() for ch in text):
        return None, text
    if text.split()[-1].lower().rstrip(".") in _STREET_SUFFIXES:
        return None, None
    return _tidy_city(text) or None, None


_STATE_SEGMENT_RE = re.compile(
    r"^([A-Za-z]{2})(?:\s+\d{2,5}(?:-\d{4})?|\d{5}(?:-\d{4})?)?$"
)


def _segment_state(segment: str) -> str | None:
    """State code when a comma segment is just a state ("CA", "CA 92108",
    "Missouri", "New York 10001"), else None."""
    seg = segment.strip().rstrip(".")
    m = _STATE_SEGMENT_RE.match(seg)
    if m:
        code = m.group(1).upper()
        return code if code in US_STATE_CODES else None
    name = re.sub(r"\s+\d{2,5}(?:-\d{4})?$", "", seg).strip().lower()
    return US_STATE_NAME_TO_CODE.get(name)


def _trailing_state(text: str) -> tuple[str, str, str | None] | None:
    """``(state_code, text_before_state, zip)`` when ``text`` *ends* with a
    state ("Chicago IL", "Orlando Florida 32803", "Sandy, UT 84070 United
    States"), else None.

    Only the end of the string counts, so street names never read as states
    ("13657 Washington Street", "3101 Texas Sage"). A title-case code is a
    word, not a state ("... Portland Or"), and "NE" needs a zip after it
    because "Peachtree Rd NE" is a street direction, not Nebraska.

    Sheet imports also carry a store number after the zip ("OR 97045 242"),
    a zip glued to the code ("Atlanta GA30319"), or a New-England zip with
    its leading zero stripped ("WOLFEBORO NH 3894") — a short number only
    counts right after a 2-letter code, never after a state name.
    """
    t = _TRAILING_COUNTRY_RE.sub("", text.strip()).strip(" .,")
    t = _GLUED_TRAILING_ZIP_RE.sub(r"\1 \2", t)
    zip_code: str | None = None
    zm = _TRAILING_ZIP_RE.search(t)
    if zm:
        zip_code = zm.group(1)
        t = t[: zm.start()].strip(" .,")
    else:
        sm = _TRAILING_CODE_SHORT_NUMBER_RE.search(t)
        if sm:
            raw = sm.group(1)
            code = raw.upper()
            if code in US_STATE_CODES and code != "NE" and (raw.isupper() or raw.islower()):
                return code, t[: sm.start(1)].strip(" .,"), None
            return None
    low = t.lower()
    for name in _STATE_NAMES_LONGEST_FIRST:
        if low.endswith(name) and (len(low) == len(name) or low[-len(name) - 1] in " .,"):
            before = t[: len(t) - len(name)].strip(" .,")
            last_word = before.split()[-1] if before.split() else ""
            if last_word.isdigit():
                return None
            return US_STATE_NAME_TO_CODE[name], before, zip_code
    m = _TRAILING_CODE_RE.search(t)
    if not m:
        return None
    raw = m.group(1)
    code = raw.upper()
    if code not in US_STATE_CODES or not (raw.isupper() or raw.islower()):
        return None
    if code == "NE" and zip_code is None:
        return None
    return code, t[: m.start(1)].strip(" .,"), zip_code


def parse_address_geo(address: str | None) -> AddressGeo:
    """Pull ``(city, state_code, zip)`` out of a US street address.

    The state is the last comma segment that is only a state ("CA 92108",
    "Missouri", "New York") — so "4 Pennsylvania Plaza, New York, New York"
    is NY. City is the segment right before it (never one that starts with
    a street number). Comma-less tails ("1357 N Elston Ave, Chicago IL",
    "2714 W Southern Ave Tempe AZ 85282") fall back to a state at the very
    end of the address; the city is taken only when it sits alone after the
    last comma. A state named only mid-address is not a state.
    """
    if not address:
        return AddressGeo(None, None, None)
    segments = [
        s.strip()
        for s in re.sub(r"[\t ]+", " ", address).split(",")
        if s.strip() and not _COUNTRY_SEGMENT_RE.match(s.strip())
    ]
    for idx in range(len(segments) - 1, -1, -1):
        code = _segment_state(segments[idx])
        if not code:
            continue
        zm = _ZIP_RE.search(" ".join(segments[idx:]))
        city, street_tail = _city_or_tail(segments[idx - 1]) if idx > 0 else (None, None)
        return AddressGeo(city, code, zm.group(1) if zm else None, street_tail)
    if not segments:
        return AddressGeo(None, None, None)
    trailing = _trailing_state(segments[-1])
    if trailing is None:
        return AddressGeo(None, None, None)
    code, before, zip_code = trailing
    if not before:
        return AddressGeo(None, code, zip_code)
    city, street_tail = _city_or_tail(before)
    if len(segments) == 1 and city:
        city, street_tail = None, before
    return AddressGeo(city, code, zip_code, street_tail)


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
        same_state = location is not None and _loc_state_code(location) == geo.state_code
        if same_state and (not city or (location.name or "").lower() == city.lower()):
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


def _catalog_city_from_tail(tail: str, state_id: int, lookup: GeoLookup) -> str | None:
    """Longest run of trailing words in ``tail`` that is a Location in the
    state ("2200 E 12 Mile Rd Royal Oak" → "Royal Oak"). Only the end of the
    text counts, so a street name never becomes the city."""
    words = tail.replace("’", "'").split()
    for n in range(min(_MAX_CITY_WORDS, len(words)), 0, -1):
        candidate = " ".join(words[-n:]).strip(" .,")
        if not candidate or candidate[0].isdigit():
            continue
        if lookup.city_locations(candidate, state_id):
            return _tidy_city(candidate)
    return None


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
    if not geo.city and geo.street_tail:
        city = _catalog_city_from_tail(geo.street_tail, state_id, lookup)
        if city:
            geo = replace(geo, city=city, street_tail=None)
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
    lookup: GeoLookup | None = None,
) -> dict[str, int | None]:
    """FK changes (``state_id`` / ``location_id``) that would make ``obj``
    agree with its address. Empty when already consistent or underivable.

    A location in another state is cleared; one in the right state but a
    different city is relinked when the address city has a Location row and
    otherwise kept (metro / neighborhood picks like "Los Angeles" for North
    Hollywood are defensible; the market label follows the address anyway).
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
        if loc is not None and loc[0] != resolution.state_id:
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
