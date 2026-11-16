"""Which physical store an event or a recap belongs to.

Store identity is the Account Map's and only the Account Map's
(:mod:`events.account_stores`): requests of one tenant that share a street
address or a ~11 m map cell are one store, named ``s<lowest request pk>``.
Nothing here defines a store a second way. This module only answers, for
the store profile, the month highlight and the links to them, "which of
those stores is this event at".

A store key is opaque to the browser. It is never trusted: every reader
rebuilds the tenant's own grouping and looks the key up in it, so a key
from another tenant is simply not found.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from events.account_stores import StoreIndex, _group_requests

_KEY_RE = re.compile(r"^s\d{1,18}$")

_EVENT_FIELDS = (
    "pk",
    "request_id",
    "retailer_id",
    "request__retailer_id",
    "address",
    "coordinates",
)


def valid_key(raw) -> str | None:
    """``raw`` as a store key when it has the shape of one, else None."""
    text = str(raw or "").strip()
    return text if _KEY_RE.match(text) else None


@dataclass
class Places:
    """One tenant's store grouping, and which store each event is at."""

    tenant_id: int
    index: StoreIndex

    def keys(self) -> set[str]:
        return set(self.index.by_request.values())

    def has(self, key: str | None) -> bool:
        return bool(key) and key in self.keys()

    def requests_of(self, key: str) -> list[int]:
        return sorted(pk for pk, store in self.index.by_request.items() if store == key)

    def for_events(self, events) -> dict[int, str]:
        """``{event pk: store key}`` for a queryset of this tenant's events.
        An event that cannot be tied to one store is left out."""
        out: dict[int, str] = {}
        for pk, request_id, retailer_id, request_retailer_id, address, coordinates in (
            events.values_list(*_EVENT_FIELDS).iterator()
        ):
            key = self.index.store_for_event(
                tenant_id=self.tenant_id,
                request_id=request_id,
                retailer_id=retailer_id or request_retailer_id,
                address=address,
                coordinates=coordinates,
            )
            if key:
                out[int(pk)] = key
        return out

    def for_event_ids(self, event_ids) -> dict[int, str]:
        from events.models import Event

        ids = {int(i) for i in event_ids if i}
        if not ids:
            return {}
        return self.for_events(Event.objects.filter(tenant_id=self.tenant_id, id__in=ids))


def tenant_places(tenant_id: int) -> Places:
    """The Account Map's grouping of one tenant's live requests into stores.

    Only the identity half of ``build_store_index``: the visit and planned
    tallies the map draws are not needed here and are not computed.
    """
    from events.models import Request

    rows = (
        Request.objects.filter(tenant_id=tenant_id, deleted_at__isnull=True)
        .values_list("pk", "tenant_id", "retailer_id", "address", "coordinates")
        .iterator()
    )
    return Places(tenant_id=tenant_id, index=_group_requests(rows))


def live_events(tenant_id: int):
    """A tenant's events whose request was not soft-deleted (events with no
    request are kept), the rule every client-facing reader uses."""
    from django.db.models import Q

    from events.models import Event

    return Event.objects.filter(tenant_id=tenant_id).filter(
        Q(request__isnull=True) | Q(request__deleted_at__isnull=True)
    )


def store_header(tenant_id: int, places: Places, key: str) -> dict:
    """Name, address, city and state of a store, from its own requests."""
    from collections import Counter

    from events.market import parse_address_geo
    from events.models import Request
    from events.retailer_aliases import accepted_alias_map, resolve_store_name

    # The name staff accepted for a store written several ways, the same
    # one the store chart shows.
    aliases = accepted_alias_map(tenant_id)
    names: Counter[str] = Counter()
    address = ""
    city = ""
    state = ""
    rows = Request.objects.filter(
        tenant_id=tenant_id, pk__in=places.requests_of(key)
    ).values_list("retailer__name", "retailer_name", "name", "address", "state__code")
    for retailer, typed, name, addr, code in rows.order_by("pk"):
        label = next(
            (s.strip() for s in (retailer, typed, name) if s and s.strip()), ""
        )
        if label:
            names[resolve_store_name(label, aliases)] += 1
        if not address and (addr or "").strip():
            address = addr.strip()
            geo = parse_address_geo(address)
            city = (geo.city or "").strip()
            state = (code or geo.state_code or "").upper()
        if not state and code:
            state = code.upper()
    name = names.most_common(1)[0][0] if names else ""
    return {"name": name, "address": address, "city": city, "state": state}


def named_store_links(tenant_id: int) -> list[dict]:
    """Retailer names that are exactly one store: ``[{name, store_key}]``.

    A banner with several locations under one name ("Total Wine") is left
    out: a chart row for the name is all of them, not one store.
    """
    from events.models import Request
    from events.retailer_aliases import accepted_alias_map, resolve_store_name

    places = tenant_places(tenant_id)
    # The chart rows are named after accepted aliases, so match on those.
    aliases = accepted_alias_map(tenant_id)
    by_name: dict[str, dict] = {}
    rows = Request.objects.filter(
        tenant_id=tenant_id, deleted_at__isnull=True
    ).values_list("pk", "retailer__name", "retailer_name")
    for pk, retailer, typed in rows.iterator():
        label = next((s.strip() for s in (retailer, typed) if s and s.strip()), "")
        if not label:
            continue
        label = resolve_store_name(label, aliases)
        held = by_name.setdefault(label.casefold(), {"name": label, "keys": set()})
        held["keys"].add(places.index.store_for_request(int(pk)))
    return sorted(
        (
            {"name": held["name"], "store_key": next(iter(held["keys"]))}
            for held in by_name.values()
            if len(held["keys"]) == 1
        ),
        key=lambda row: row["name"].casefold(),
    )
