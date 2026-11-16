"""Where to sample next: a ranked list of stores for a brand, with reasons.

Two kinds of store make the list:

* **Never visited**: a store on the brand's uploaded account list, or a
  retailer on its roster with a street address, that has no filed recap and
  nothing planned. Ranked by how well the stores around it converted.
* **Worth going back to**: a store that converted better than the brand's
  average and has not had a visit for a while.

"A store" is a physical place, as on the Account Map
(:mod:`events.account_stores`). Conversion comes from approved,
non-archived recaps over the last :data:`LOOKBACK_DAYS` days
(:mod:`recaps.explain_facts`). The ranking and every reason are worked out
here; :mod:`recaps.explain_text` may reword a reason and nothing more.

Staff only for now. :func:`clients_may_see` is the one switch.
"""

from __future__ import annotations

import math
from datetime import date, timedelta

from django.conf import settings

from recaps.explain_text import Draft, n, pct, plural

MAX_ITEMS = 10
LOOKBACK_DAYS = 365
# The brand needs this many recaps in its rate before stores are compared.
BRAND_MIN_RECAPS = 10
# A store's own rate is used with at least this much behind it.
RATED_MIN_RECAPS = 2
RATED_MIN_BASE = 50
# A neighbour's results count towards "nearby stores averaged" from here.
NEIGHBOUR_MIN_BASE = 20
NEARBY_MILES = 25
NEARBY_MIN_STORES = 2
# Worth going back to: at least this much above the brand's rate, and this
# long since the last visit.
REVISIT_MIN_RATIO = 1.1
REVISIT_MIN_WEEKS = 6

KIND_NEVER = "never_visited"
KIND_REVISIT = "revisit"

_KM_PER_MILE = 1.609344


def clients_may_see() -> bool:
    """Whether brand users get this list. Off: the owner has not decided.
    Turning it on is this one setting (``SAMPLE_NEXT_FOR_CLIENTS = True``)."""
    return bool(getattr(settings, "SAMPLE_NEXT_FOR_CLIENTS", False))


def _km(a, b) -> float:
    rad = math.pi / 180
    d_lat = (a[0] - b[0]) * rad
    d_lng = (a[1] - b[1]) * rad * math.cos((a[0] + b[0]) / 2 * rad)
    return math.sqrt(d_lat * d_lat + d_lng * d_lng) * 6371.0


def _rate(units: int, base: int) -> float:
    return round(units / base * 100, 1)


def _market(city: str, state: str) -> str:
    return ", ".join(part for part in (city, state) if part)


def _visited_stores(tenant_id: int, index, today: date) -> dict[str, dict]:
    """Every store with a request: name, address, position, last visit and
    its approved results."""
    from django.db.models.functions import Coalesce

    from events.admin_payloads import _event_positions
    from events.map_points import own_point
    from events.market import parse_address_geo
    from events.models import Event, Request
    from recaps.explain_facts import recap_facts

    stores: dict[str, dict] = {}
    needs_position: dict[str, int] = {}
    requests = Request.objects.filter(
        tenant_id=tenant_id, deleted_at__isnull=True
    ).values_list("pk", "name", "retailer__name", "address", "coordinates", "state__code")
    for pk, name, retailer, address, coordinates, state in requests.iterator():
        key = index.store_for_request(pk)
        geo = parse_address_geo(address)
        point = own_point(type("P", (), {"coordinates": coordinates})())
        store = stores.setdefault(
            key,
            {
                "key": key,
                "name": (retailer or name or "").strip(),
                "address": (address or "").strip(),
                "city": (geo.city or "").strip(),
                "state": (state or geo.state_code or "").upper(),
                "point": None,
                "recaps": 0,
                "base": 0,
                "units": 0,
                "last": None,
            },
        )
        if store["point"] is None and point is not None:
            store["point"] = point
        if store["point"] is None:
            needs_position.setdefault(key, pk)

    by_request = {pk: key for key, pk in needs_position.items()}
    for pk, (lat, lng, _source) in _event_positions(list(by_request)).items():
        store = stores[by_request[pk]]
        if store["point"] is None:
            store["point"] = (lat, lng)

    # When each store was last visited: its newest event with a filed recap.
    event_ids = {e for ids in index.visits.values() for e in ids}
    days = {
        pk: when.date()
        for pk, when in Event.objects.filter(id__in=event_ids)
        .annotate(_when=Coalesce("date", "start_time"))
        .values_list("pk", "_when")
        if when is not None
    }
    for key, ids in index.visits.items():
        seen = [days[e] for e in ids if e in days and days[e] <= today]
        if key in stores and seen:
            stores[key]["last"] = max(seen)

    # Approved results, placed at a store the same way the map places them.
    facts = [
        r
        for r in recap_facts(tenant_id, today - timedelta(days=LOOKBACK_DAYS - 1), today)
        if r["base"]
    ]
    events = {
        row[0]: row[1:]
        for row in Event.objects.filter(
            id__in={r["event_id"] for r in facts}
        ).values_list(
            "pk", "request_id", "retailer_id", "request__retailer_id", "address", "coordinates"
        )
    }
    for r in facts:
        held = events.get(r["event_id"])
        if held is None:
            continue
        request_id, retailer_id, request_retailer_id, address, coordinates = held
        key = index.store_for_event(
            tenant_id=tenant_id,
            request_id=request_id,
            retailer_id=retailer_id or request_retailer_id,
            address=address,
            coordinates=coordinates,
        )
        if key in stores:
            stores[key]["recaps"] += 1
            stores[key]["base"] += r["base"]
            stores[key]["units"] += r["units"]
    return stores


def _unvisited(tenant_id: int, index, stores: dict[str, dict]) -> list[dict]:
    """Account-list rows and roster retailers with no filed recap and
    nothing planned."""
    from events.account_list import full_address
    from events.account_stores import _address_key
    from events.map_points import is_lookup_worthy
    from events.market import parse_address_geo
    from events.models import AccountListEntry, Retailer

    placed = [(s["point"], key) for key, s in stores.items() if s["point"]]

    def known_store(address: str, point) -> str | None:
        key = index.by_place.get(_address_key(tenant_id, address) or "")
        if key:
            return key
        if point is not None:
            for other, other_key in placed:
                if _km(point, other) <= 0.075:
                    return other_key
        return None

    def open_store(key: str | None) -> bool:
        """True when the place is not a store with a visit or a plan."""
        if key is None:
            return True
        return index.visit_count(key) == 0 and not index.is_planned(key)

    out: list[dict] = []
    seen: set[str] = set()
    for entry in AccountListEntry.objects.filter(tenant_id=tenant_id).iterator():
        address = full_address(entry.address, entry.city, entry.state, entry.zip)
        point = (
            (entry.lat, entry.lng)
            if entry.lat is not None and entry.lng is not None
            else None
        )
        key = known_store(address, point)
        if not open_store(key):
            continue
        ident = key or f"list:{entry.dedupe_key}"
        if ident in seen:
            continue
        seen.add(ident)
        state = (entry.state or "").strip().upper()
        out.append(
            {
                "key": ident,
                "name": entry.name,
                "address": address,
                "city": (entry.city or "").strip(),
                "state": state if len(state) == 2 else (parse_address_geo(address).state_code or ""),
                "point": point,
            }
        )

    roster = Retailer.objects.filter(
        tenant_id=tenant_id, events__isnull=True, requests__isnull=True
    ).values_list("pk", "name", "address", "location__state__code")
    for pk, name, address, state in roster.iterator():
        if not is_lookup_worthy(address):
            continue
        key = known_store(address, None)
        if not open_store(key):
            continue
        ident = key or f"retailer:{pk}"
        if ident in seen:
            continue
        seen.add(ident)
        geo = parse_address_geo(address)
        out.append(
            {
                "key": ident,
                "name": (name or "").strip(),
                "address": (address or "").strip(),
                "city": (geo.city or "").strip(),
                "state": (state or geo.state_code or "").upper(),
                "point": None,
            }
        )
    return out


def _neighbours(target: dict, rated: list[dict]) -> tuple[str, list[dict]]:
    """The stores whose results stand in for a store nobody has visited:
    within :data:`NEARBY_MILES`, else the same city, else the same state."""
    if target["point"]:
        near = [
            s
            for s in rated
            if s["point"] and _km(target["point"], s["point"]) <= NEARBY_MILES * _KM_PER_MILE
        ]
        if len(near) >= NEARBY_MIN_STORES:
            return f"within {NEARBY_MILES} miles", near
    if target["city"] and target["state"]:
        same = [
            s
            for s in rated
            if s["state"] == target["state"]
            and s["city"].casefold() == target["city"].casefold()
        ]
        if len(same) >= NEARBY_MIN_STORES:
            return f"in {target['city']}", same
    if target["state"]:
        same = [s for s in rated if s["state"] == target["state"]]
        if len(same) >= NEARBY_MIN_STORES:
            return f"in {target['state']}", same
    return "", []


def compute_sample_next(tenant_id: int, today: date) -> list[dict]:
    """The ranked list. Each item: ``key``, ``kind``, ``name``, ``address``,
    ``market``, ``lat`` / ``lng`` (None when unknown), ``template`` (the
    reason) and ``facts`` (the lines the reason was built from)."""
    from events.account_stores import build_store_index

    index = build_store_index(tenant_id)
    stores = _visited_stores(tenant_id, index, today)
    with_results = [s for s in stores.values() if s["base"] > 0]
    total_recaps = sum(s["recaps"] for s in with_results)
    total_base = sum(s["base"] for s in with_results)
    total_units = sum(s["units"] for s in with_results)
    average = (
        _rate(total_units, total_base)
        if total_recaps >= BRAND_MIN_RECAPS and total_base > 0 and total_units > 0
        else None
    )
    neighbours_pool = [s for s in with_results if s["base"] >= NEIGHBOUR_MIN_BASE]
    rated = [
        {**s, "rate": _rate(s["units"], s["base"])}
        for s in with_results
        if s["recaps"] >= RATED_MIN_RECAPS and s["base"] >= RATED_MIN_BASE
    ]

    items: list[dict] = []

    for target in _unvisited(tenant_id, index, stores):
        where, near = _neighbours(target, neighbours_pool) if average else ("", [])
        if near:
            base = sum(s["base"] for s in near)
            rate = _rate(sum(s["units"] for s in near), base)
            count = plural(len(near), "store")
            reason = f"Never visited. {count} {where} averaged {pct(rate)}% conversion."
            facts = (
                f"Store: {target['name']}. It has never been visited.\n"
                f"Stores with results {where}: {len(near)}.\n"
                f"Their conversion rate together: {pct(rate)}%.\n"
                f"Brand average conversion rate: {pct(average)}%."
            )
            score = rate / average
        else:
            reason = "Never visited. No store nearby has results to compare yet."
            facts = f"Store: {target['name']}. It has never been visited."
            score = 0.0
        items.append({**target, "kind": KIND_NEVER, "template": reason, "facts": facts, "score": score})

    if average:
        best_in_city: dict[tuple[str, str], dict] = {}
        city_count: dict[tuple[str, str], int] = {}
        for s in rated:
            if not s["city"]:
                continue
            place = (s["city"].casefold(), s["state"])
            city_count[place] = city_count.get(place, 0) + 1
            if place not in best_in_city or s["rate"] > best_in_city[place]["rate"]:
                best_in_city[place] = s
        for s in rated:
            if s["last"] is None or index.is_planned(s["key"]):
                continue
            weeks = (today - s["last"]).days // 7
            if weeks < REVISIT_MIN_WEEKS or s["rate"] < average * REVISIT_MIN_RATIO:
                continue
            place = (s["city"].casefold(), s["state"])
            ago = f"last visited {plural(weeks, 'week')} ago"
            if s["city"] and city_count.get(place, 0) >= 2 and best_in_city[place]["key"] == s["key"]:
                reason = f"Best conversion in {s['city']} at {pct(s['rate'])}%, {ago}."
            else:
                reason = (
                    f"Converted at {pct(s['rate'])}% against a {pct(average)}% "
                    f"average, {ago}."
                )
            facts = (
                f"Store: {s['name']}.\n"
                f"Its conversion rate: {pct(s['rate'])}% "
                f"({n(s['units'])} sold, {n(s['base'])} sampled, "
                f"{plural(s['recaps'], 'approved recap')}).\n"
                f"Brand average conversion rate: {pct(average)}%.\n"
                f"Weeks since the last visit: {weeks}."
            )
            items.append(
                {
                    "key": s["key"],
                    "name": s["name"],
                    "address": s["address"],
                    "city": s["city"],
                    "state": s["state"],
                    "point": s["point"],
                    "kind": KIND_REVISIT,
                    "template": reason,
                    "facts": facts,
                    "score": s["rate"] / average * min(2.0, weeks / REVISIT_MIN_WEEKS),
                }
            )

    items.sort(key=lambda i: (-i["score"], i["name"].casefold(), i["key"]))
    out = []
    for item in items[:MAX_ITEMS]:
        point = item.pop("point")
        out.append(
            {
                **item,
                "market": _market(item["city"], item["state"]),
                "lat": point[0] if point else None,
                "lng": point[1] if point else None,
            }
        )
    return out


def draft_sample_next(items: list[dict]) -> list[Draft]:
    return [Draft(key=i["key"], template=i["template"], facts=i["facts"]) for i in items]
