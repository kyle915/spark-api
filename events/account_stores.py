"""Which physical store a request or an event belongs to, for the Account Map.

A store is a PLACE: one street address, or one spot on the ground. It is not
a Retailer row. Bulk imports hang every location of a banner off one Retailer
("Total Wine"), and some stores have two Retailer rows spelled differently, so
counting by Retailer gave every Total Wine the whole banner's visit count and
drew them as one account.

Two requests are the same store when, inside one tenant, they share a
normalised street address (and their positions, where known, are within a
kilometre) or sit on the same ~11 m map cell. Visits (events
with a filed recap) and planned work are then counted per store.

Nothing here is estimated: an event that cannot be tied to one store is left
out of every store's count rather than spread across a banner.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from django.utils import timezone

from events.map_points import is_lookup_worthy, normalise_address, own_point

# 4 decimal places of a degree is about 11 m: the same building.
_CELL_DECIMALS = 4


def _address_key(tenant_id, address) -> str | None:
    if not is_lookup_worthy(address):
        return None
    return f"{tenant_id}|a|{normalise_address(address)}"


def _cell_key(tenant_id, point) -> str | None:
    if point is None:
        return None
    lat, lng = point
    return f"{tenant_id}|g|{lat:.{_CELL_DECIMALS}f}|{lng:.{_CELL_DECIMALS}f}"


# Two positions further apart than this are not one store, whatever the
# address text says.
_SAME_PLACE_KM = 1.0


def _far_apart(a, b) -> bool:
    """True when both positions are known and more than ~1 km apart."""
    if a is None or b is None:
        return False
    d_lat = (a[0] - b[0]) * 111.0
    d_lng = (a[1] - b[1]) * 111.0 * math.cos(math.radians((a[0] + b[0]) / 2))
    return (d_lat * d_lat + d_lng * d_lng) ** 0.5 > _SAME_PLACE_KM


class _Holder:
    """Anything with ``coordinates``, so :func:`own_point` can read a tuple."""

    __slots__ = ("coordinates",)

    def __init__(self, coordinates):
        self.coordinates = coordinates


def _point(coordinates):
    return own_point(_Holder(coordinates))


@dataclass
class StoreIndex:
    """Store identity for every live request of a tenant, plus the per-store
    filed-visit and planned tallies."""

    # request pk -> store key ("s<lowest request pk in the store>")
    by_request: dict[int, str] = field(default_factory=dict)
    # place key (address or map cell) -> store key (None = ambiguous)
    by_place: dict[str, str | None] = field(default_factory=dict)
    # retailer pk -> store keys its requests sit at
    by_retailer: dict[int, set[str]] = field(default_factory=dict)
    # store key -> distinct events with a filed recap
    visits: dict[str, set[int]] = field(default_factory=dict)
    # store key -> date of its newest filed visit
    last_visits: dict[str, object] = field(default_factory=dict)
    planned: set[str] = field(default_factory=set)

    def store_for_request(self, request_id: int) -> str:
        return self.by_request.get(request_id) or f"s{request_id}"

    def visit_count(self, store_key: str) -> int:
        return len(self.visits.get(store_key, ()))

    def last_visit(self, store_key: str):
        """When this store's newest filed visit happened; None when never."""
        return self.last_visits.get(store_key)

    def is_planned(self, store_key: str) -> bool:
        return store_key in self.planned

    def store_for_event(
        self, *, tenant_id, request_id, retailer_id, address, coordinates
    ) -> str | None:
        """The store an event happened at, or None when it cannot be told."""
        if request_id and request_id in self.by_request:
            return self.by_request[request_id]
        for key in (
            _address_key(tenant_id, address),
            _cell_key(tenant_id, _point(coordinates)),
        ):
            if key and self.by_place.get(key):
                return self.by_place[key]
        if retailer_id:
            stores = self.by_retailer.get(int(retailer_id)) or set()
            if len(stores) == 1:
                return next(iter(stores))
        return None


def _group_requests(rows) -> StoreIndex:
    """Union requests that share an address or a map cell (per tenant)."""
    parent: dict[int, int] = {}

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: int, b: int) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            # The lowest pk names the store, so the key is stable.
            lo, hi = (ra, rb) if ra < rb else (rb, ra)
            parent[hi] = lo

    # One typed address can be two places ("100 Main St" in two towns), so
    # an address only joins rows whose positions do not disagree.
    at_address: dict[str, list[tuple[int, tuple[float, float] | None]]] = {}
    at_cell: dict[str, int] = {}
    places: dict[int, list[str]] = {}
    retailers: dict[int, int | None] = {}
    for pk, tenant_id, retailer_id, address, coordinates in rows:
        pk = int(pk)
        parent[pk] = pk
        retailers[pk] = retailer_id
        point = _point(coordinates)
        keys: list[str] = []
        address_key = _address_key(tenant_id, address)
        if address_key:
            keys.append(address_key)
            held = at_address.setdefault(address_key, [])
            for i, (other, other_point) in enumerate(held):
                if not _far_apart(point, other_point):
                    union(other, pk)
                    if other_point is None and point is not None:
                        held[i] = (other, point)
                    break
            else:
                held.append((pk, point))
        cell_key = _cell_key(tenant_id, point)
        if cell_key:
            keys.append(cell_key)
            if cell_key in at_cell:
                union(at_cell[cell_key], pk)
            else:
                at_cell[cell_key] = pk
        places[pk] = keys

    index = StoreIndex()
    for pk in parent:
        store = f"s{find(pk)}"
        index.by_request[pk] = store
        for key in places[pk]:
            # One address text at two places: an event cannot be placed by
            # that text alone, so the key answers "unknown" (None).
            held = index.by_place.get(key, store)
            index.by_place[key] = store if held == store else None
        rid = retailers[pk]
        if rid:
            index.by_retailer.setdefault(int(rid), set()).add(store)
    return index


def build_store_index(tenant_id: int | None = None, *, now=None) -> StoreIndex:
    """Store identity and per-store tallies for one tenant (all when None).

    Soft-deleted requests are not stores, and neither their filed recaps nor
    their future events count towards any store.
    """
    from events.admin_payloads import _status_q
    from events.models import Event, Request
    from recaps.filed import custom_filed_q, legacy_filed_q
    from recaps.models import CustomRecap, Recap

    now = now or timezone.now()
    requests = Request.objects.filter(deleted_at__isnull=True)
    if tenant_id is not None:
        requests = requests.filter(tenant_id=tenant_id)
    index = _group_requests(
        requests.values_list(
            "pk", "tenant_id", "retailer_id", "address", "coordinates"
        ).iterator()
    )

    event_fields = (
        "event_id",
        "event__tenant_id",
        "event__request_id",
        "retailer_id",
        "event__retailer_id",
        "event__request__retailer_id",
        "event__address",
        "event__coordinates",
        "event__date",
    )
    legacy = Recap.objects.filter(legacy_filed_q(), archived_at__isnull=True)
    custom = CustomRecap.objects.filter(custom_filed_q(), archived_at__isnull=True)
    for qs in (legacy, custom):
        qs = qs.exclude(event__request__deleted_at__isnull=False)
        if tenant_id is not None:
            qs = qs.filter(event__tenant_id=tenant_id)
        for (
            event_id,
            ev_tenant,
            request_id,
            recap_retailer,
            event_retailer,
            request_retailer,
            address,
            coordinates,
            event_date,
        ) in qs.values_list(*event_fields).iterator():
            if not event_id:
                continue
            store = index.store_for_event(
                tenant_id=ev_tenant,
                request_id=request_id,
                retailer_id=recap_retailer or event_retailer or request_retailer,
                address=address,
                coordinates=coordinates,
            )
            if store:
                index.visits.setdefault(store, set()).add(int(event_id))
                if event_date is not None:
                    held = index.last_visits.get(store)
                    if held is None or event_date > held:
                        index.last_visits[store] = event_date

    future_events = Event.objects.filter(start_time__gte=now).exclude(
        request__deleted_at__isnull=False
    )
    if tenant_id is not None:
        future_events = future_events.filter(tenant_id=tenant_id)
    for ev_tenant, request_id, event_retailer, request_retailer, address, coordinates in (
        future_events.values_list(
            "tenant_id",
            "request_id",
            "retailer_id",
            "request__retailer_id",
            "address",
            "coordinates",
        ).iterator()
    ):
        store = index.store_for_event(
            tenant_id=ev_tenant,
            request_id=request_id,
            retailer_id=event_retailer or request_retailer,
            address=address,
            coordinates=coordinates,
        )
        if store:
            index.planned.add(store)

    future_requests = requests.filter(date__gte=now).filter(
        _status_q("approved", "scheduled")
    )
    for pk in future_requests.values_list("pk", flat=True).iterator():
        index.planned.add(index.store_for_request(int(pk)))
    return index
