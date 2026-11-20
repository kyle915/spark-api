"""Consumers sampled, split by week, by store and by state.

One pass over a tenant's approved recaps feeds all three splits, so each of
them adds up to the same ``total``: the weekly trend, the store ranking and
the state ranking can sit under one headline without disagreeing with it.

Same basis as :func:`recaps.tenant_overview.tenant_kpi_totals`'s
``consumers_reached`` (which the dashboards print as "consumers sampled"):

* approved recaps only, legacy ``Recap`` (``ConsumerEngagements``) and
  custom ``CustomRecap`` (the shared "consumers sampled" field matcher);
* windowed on the EVENT date (event date, then start time, then the
  request's date), never on when the recap row was created;
* recaps of soft-deleted requests, events kept off the dashboard and agency
  filings kept out of aggregates are left out
  (:func:`recaps.tenant_overview._filter_event_window`). A pending walk-up
  has no approved recap, so it is left out too.

"By store" groups by store NAME, after accepted store-name aliases
(:mod:`events.retailer_aliases`) are applied: "HEB" and "H-E-B" are one row
once staff have accepted that they are the same. A name that carries a store
number ("HEB #725") stays its own row, so it is a physical store; a
banner-only name is one row for the banner, as before. Proposed and dismissed
aliases change nothing here.

A recap's own retailer (``CustomRecap.retailer``, with its ``store_number``)
and state win over / fill in for its event's, the same order the recap info
panel uses; the recap location normalizer (:mod:`recaps.recap_location`)
fills them from free text.

When no retailer resolves at all, a walk-in event's title still names the
place: "M/D/YYYY - <address> (<place>) · <program>". That place is used
(:func:`place_from_walkin_title`): an event activation (festival, venue) is
counted under a row of ``kind="venue"`` so it reads as a venue, not a store;
any other program (a retail store typed at check-in, e.g. "Sunoco") is
counted as a named store. Venue rows rank with named rows by consumers.

A recap whose event has no store or no state is not dropped: it is counted
under a row of ``kind="missing"`` so the split still adds up and the gap in
the data stays visible. Recaps whose event has no date at all cannot be
placed in a week and are left out of every figure here.

READ-ONLY. No ambassador names leave this module.
"""

from __future__ import annotations

import re
from datetime import date, timedelta
from types import SimpleNamespace

from recaps import metric_mapping
from recaps.models import ConsumerEngagements, CustomFieldValue, CustomRecap
from recaps.tenant_overview import (
    _approved_only,
    _event_date_expr,
    _filter_event_window,
    _inclusive_dates_to_window,
    _kpi_gate,
)
from recaps.types import _consumers_sampled_from_fields

# Named stores returned before the tail is folded into one "other" row.
TOP_STORES = 30

KIND_NAMED = "named"
KIND_MISSING = "missing"
KIND_OTHER = "other"
# A festival / event-activation venue named only by the walk-in title.
KIND_VENUE = "venue"
# Rows that name a real place (a store, a state, or an event venue).
LABELED_KINDS = (KIND_NAMED, KIND_VENUE)

_WALKIN_DATE = re.compile(r"^\d{1,2}/\d{1,2}/\d{4}\s*-\s*")
_PROGRAM_SEP = " · "
_NOT_A_PLACE = re.compile(r"^(product seeding|walk-in event)\b", re.I)


def place_from_walkin_title(
    name: str | None, address: str | None, event_type_name: str | None = None
) -> tuple[str, str]:
    """``(label, kind)`` for the place a walk-in event's title names, else
    ``("", "")``.

    Walk-in titles read "M/D/YYYY - <address> (<place>) · <program>"; the
    place is the trailing parenthetical once the " · <program>" suffix is
    off. A title with no parenthetical names the place directly
    ("8/16/2026 - Wisconsin State Fair · Event Activation") unless what is
    left is just the street address. ``kind`` is ``venue`` when the event
    type (or the program suffix) is an event activation / guerilla shift,
    otherwise ``named`` (a store typed at check-in).
    """
    from ambassadors.checkin_web import (
        _looks_like_address,
        _trailing_parenthetical,
        is_event_activation_type,
        normalize_place,
    )

    title = (name or "").strip()
    if not _WALKIN_DATE.match(title):
        return "", ""
    programs: list[str] = []
    while _PROGRAM_SEP in title:
        cut = title.rfind(_PROGRAM_SEP)
        tail = title[cut + len(_PROGRAM_SEP) :]
        if tail.count("(") != tail.count(")"):
            break  # the separator sits inside the place's parenthetical
        programs.append(tail.strip())
        title = title[:cut].rstrip()
    addr_key = normalize_place(address or "")
    inner = _trailing_parenthetical(title)
    if inner:
        label = inner.split(_PROGRAM_SEP)[0].strip()
    else:
        label = _WALKIN_DATE.sub("", title).strip()
        if _looks_like_address(label):
            return "", ""
    key = normalize_place(label)
    if (
        not key
        or _NOT_A_PLACE.match(label)
        or (addr_key and (key == addr_key or (label[:1].isdigit() and key in addr_key)))
    ):
        return "", ""
    venue = is_event_activation_type(SimpleNamespace(name=event_type_name)) or any(
        is_event_activation_type(SimpleNamespace(name=p)) for p in programs
    )
    return label, KIND_VENUE if venue else KIND_NAMED


def week_start(day: date) -> date:
    """The Monday of ``day``'s week."""
    return day - timedelta(days=day.weekday())


def _ranked(groups: dict[str, dict], limit: int | None) -> tuple[list[dict], int]:
    """Named rows largest first, then "other", then "missing" last.

    ``groups`` maps a case-folded key to ``{label, consumers, recaps}`` (and
    ``kind`` for a venue); the ``""`` key is the recaps with nothing
    recorded. Venue rows rank with named rows by consumers. Returns the rows
    and the number of distinct labeled groups.
    """
    named = sorted(
        (g for key, g in groups.items() if key and g["consumers"] > 0),
        key=lambda g: (-g["consumers"], g["label"].casefold()),
    )
    head = named if limit is None else named[:limit]
    tail = [] if limit is None else named[limit:]
    rows = [{**g, "kind": g.get("kind") or KIND_NAMED} for g in head]
    if tail:
        rows.append(
            {
                "label": "",
                "kind": KIND_OTHER,
                "consumers": sum(g["consumers"] for g in tail),
                "recaps": sum(g["recaps"] for g in tail),
            }
        )
    missing = groups.get("")
    if missing and missing["consumers"] > 0:
        rows.append({**missing, "label": "", "kind": KIND_MISSING})
    return rows, len(named)


def summarize_consumers(
    rows: list[dict],
    start: date | None = None,
    end: date | None = None,
    top_stores: int = TOP_STORES,
) -> dict:
    """Pure roll-up. Each row is one recap: ``consumers`` (int), ``day``
    (date of the event), ``store`` and ``state`` (may be blank), ``event``
    (its id, optional) and ``store_kind`` (``"venue"`` when ``store`` is an
    event venue rather than a store; optional).

    Weeks run Monday to Sunday and are contiguous, zero-filled, from the
    first week to the last: the window's own weeks when ``start`` / ``end``
    are given, widened to any recap that falls outside them.
    """
    total = 0
    weeks: dict[date, dict] = {}
    stores: dict[str, dict] = {}
    states: dict[str, dict] = {}

    def add(
        groups: dict[str, dict], raw: str | None, consumers: int, kind: str | None = None
    ) -> None:
        label = (raw or "").strip()
        venue = bool(label) and kind == KIND_VENUE
        # A venue never merges into a store row of the same name.
        key = f"venue:{label.casefold()}" if venue else label.casefold()
        bucket = groups.setdefault(
            key,
            {"label": label, "consumers": 0, "recaps": 0, **({"kind": KIND_VENUE} if venue else {})},
        )
        bucket["consumers"] += consumers
        bucket["recaps"] += 1

    for r in rows:
        consumers = int(r["consumers"] or 0)
        total += consumers
        monday = week_start(r["day"])
        week = weeks.setdefault(monday, {"consumers": 0, "recaps": 0})
        week["consumers"] += consumers
        week["recaps"] += 1
        add(stores, r.get("store"), consumers, r.get("store_kind"))
        add(states, (r.get("state") or "").upper(), consumers)

    mondays = list(weeks)
    if start is not None:
        mondays.append(week_start(start))
    if end is not None:
        mondays.append(week_start(end))
    by_week: list[dict] = []
    if mondays and rows:
        cursor, last = min(mondays), max(mondays)
        while cursor <= last:
            week = weeks.get(cursor, {"consumers": 0, "recaps": 0})
            by_week.append({"week_start": cursor.isoformat(), **week})
            cursor += timedelta(days=7)

    by_store, store_count = _ranked(stores, top_stores)
    by_state, state_count = _ranked(states, None)
    return {
        "total": total,
        "recaps": len(rows),
        # Distinct events behind those recaps: the base for "per event".
        "events": len({r["event"] for r in rows if r.get("event") is not None}),
        "by_week": by_week,
        "by_store": by_store,
        "by_state": by_state,
        "store_count": store_count,
        "state_count": state_count,
    }


def _store(*names: str | None, aliases: dict[str, str] | None = None) -> str:
    """The first name recorded, shown under its accepted alias if it has one."""
    for name in names:
        cleaned = (name or "").strip()
        if cleaned:
            if aliases:
                from events.retailer_aliases import resolve_store_name

                return resolve_store_name(cleaned, aliases)
            return cleaned
    return ""


def _numbered(name: str | None, store_number: str | None) -> str:
    """ "H-E-B" + "614" -> "H-E-B #614": a recap's retailer with its store
    number, so a numbered store stays its own row in the store split. A
    name that already carries a number is left alone."""
    from events.retailer_aliases import split_store_number

    cleaned = (name or "").strip()
    number = (store_number or "").strip()
    if not cleaned or not number or split_store_number(cleaned)[1]:
        return cleaned
    return f"{cleaned} #{number}"


def _where(
    aliases: dict[str, str] | None,
    event_name: str | None,
    address: str | None,
    event_type: str | None,
    *names: str | None,
) -> dict:
    """``store`` (and ``store_kind`` for a venue) for one recap: the first
    retailer name recorded, else the place its walk-in title names."""
    store = _store(*names, aliases=aliases)
    if store:
        return {"store": store}
    label, kind = place_from_walkin_title(event_name, address, event_type)
    if not label:
        return {"store": ""}
    if kind == KIND_VENUE:
        return {"store": label, "store_kind": KIND_VENUE}
    return {"store": _store(label, aliases=aliases)}


def consumer_rows(
    tenant_id: int,
    start: date | None,
    end: date | None,
    *,
    include_archived: bool = True,
) -> list[dict]:
    """One row per approved recap that recorded consumers sampled.

    Each row: ``kind`` ("custom" / "legacy"), ``id``, ``day``, ``store``,
    ``state``, ``consumers``, and ``store_kind="venue"`` when ``store`` is an
    event venue taken from a walk-in title. ``include_archived=False`` also leaves out
    archived recaps (the dashboard totals count them today; the sentences
    written from these rows must not).
    """
    from events.retailer_aliases import accepted_alias_map
    from events.routing import extract_state_code

    live = {} if include_archived else {"archived_at__isnull": True}

    window = _inclusive_dates_to_window(start, end)
    # Accepted store-name aliases only; empty for most tenants.
    aliases = accepted_alias_map(tenant_id)
    rows: list[dict] = []

    # Custom recaps: where and when each one happened…
    placed: dict[int, dict] = {}
    custom = (
        _approved_only(
            _filter_event_window(
                CustomRecap.objects.filter(tenant_id=tenant_id, **live),
                "event__",
                window,
            ),
            "",
        )
        .annotate(_day=_event_date_expr("event__"))
        .filter(_day__isnull=False)
        .values_list(
            "id",
            "event_id",
            "_day",
            "retailer__name",
            "store_number",
            "event__retailer__name",
            "event__request__retailer__name",
            "event__request__retailer_name",
            "event__state__code",
            "state__code",
            "event__address",
            "event__name",
            "event__event_type__name",
        )
    )
    for (
        recap_id,
        event_id,
        day,
        recap_retailer,
        store_number,
        retailer,
        req_retailer,
        req_name,
        state,
        recap_state,
        address,
        event_name,
        event_type,
    ) in custom.iterator():
        placed[recap_id] = {
            "event": event_id,
            "kind": "custom",
            "id": recap_id,
            "day": day.date(),
            # The recap's own retailer first (the eventRetailer resolver's
            # order), with its store number when one is known; then the
            # place a walk-in title names.
            **_where(
                aliases,
                event_name,
                address,
                event_type,
                _numbered(recap_retailer, store_number),
                retailer,
                req_retailer,
                req_name,
            ),
            # The event's state, then the recap's own, then the address.
            "state": state or recap_state or extract_state_code(address) or "",
        }

    # …and how many consumers it sampled, by the same field matcher (and the
    # same SQL gate on field names) the KPI totals use.
    pairs: dict[int, list[tuple[str | None, str | None]]] = {}
    gate, confirmed = _kpi_gate(tenant_id)
    values = (
        _approved_only(
            _filter_event_window(
                CustomFieldValue.objects.filter(
                    gate,
                    custom_recap__tenant_id=tenant_id,
                    **{f"custom_recap__{k}": v for k, v in live.items()},
                ),
                "custom_recap__event__",
                window,
            ),
            "custom_recap__",
        )
        .values_list("custom_recap_id", "custom_field_id", "custom_field__name", "value")
        .order_by("custom_recap_id")
    )
    for recap_id, field_id, name, value in values.iterator():
        pairs.setdefault(recap_id, []).append(
            (metric_mapping.tag_name(name, field_id, confirmed), value)
        )
    for recap_id, fields in pairs.items():
        where = placed.get(recap_id)
        if where is None:
            continue
        consumers = _consumers_sampled_from_fields(fields)
        if consumers is None:
            continue
        rows.append({**where, "consumers": int(consumers)})

    # Legacy recaps: one row per recap, summing its engagement rows.
    legacy: dict[int, dict] = {}
    engagements = (
        _approved_only(
            _filter_event_window(
                ConsumerEngagements.objects.filter(
                    recap__event__tenant_id=tenant_id,
                    **{f"recap__{k}": v for k, v in live.items()},
                ),
                "recap__event__",
                window,
            ),
            "recap__",
        )
        .annotate(_day=_event_date_expr("recap__event__"))
        .filter(_day__isnull=False)
        .values_list(
            "recap_id",
            "recap__event_id",
            "total_consumer",
            "_day",
            "recap__retailer__name",
            "recap__event__retailer__name",
            "recap__event__request__retailer__name",
            "recap__event__request__retailer_name",
            "recap__event__state__code",
            "recap__state__code",
            "recap__event__address",
            "recap__event__name",
            "recap__event__event_type__name",
        )
    )
    for (
        recap_id,
        event_id,
        consumers,
        day,
        recap_retailer,
        retailer,
        req_retailer,
        req_name,
        state,
        recap_state,
        address,
        event_name,
        event_type,
    ) in engagements.iterator():
        row = legacy.setdefault(
            recap_id,
            {
                "event": event_id,
                "kind": "legacy",
                "id": recap_id,
                "day": day.date(),
                # Same order as the custom path: the recap's own retailer
                # wins, its own state fills in for the event's.
                **_where(
                    aliases,
                    event_name,
                    address,
                    event_type,
                    recap_retailer,
                    retailer,
                    req_retailer,
                    req_name,
                ),
                "state": state or recap_state or extract_state_code(address) or "",
                "consumers": 0,
            },
        )
        row["consumers"] += int(consumers or 0)
    rows.extend(legacy.values())
    return rows


def tenant_consumer_breakdown(
    tenant_id: int, start: date | None, end: date | None
) -> dict:
    return summarize_consumers(consumer_rows(tenant_id, start, end), start, end)
