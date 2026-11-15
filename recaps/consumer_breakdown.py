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

A recap whose event has no store or no state is not dropped: it is counted
under a row of ``kind="missing"`` so the split still adds up and the gap in
the data stays visible. Recaps whose event has no date at all cannot be
placed in a week and are left out of every figure here.

READ-ONLY. No ambassador names leave this module.
"""

from __future__ import annotations

from datetime import date, timedelta

from recaps.models import ConsumerEngagements, CustomFieldValue, CustomRecap
from recaps.tenant_overview import (
    _CUSTOM_KPI_NAME_RE,
    _approved_only,
    _event_date_expr,
    _filter_event_window,
    _inclusive_dates_to_window,
)
from recaps.types import _consumers_sampled_from_fields

# Named stores returned before the tail is folded into one "other" row.
TOP_STORES = 30

KIND_NAMED = "named"
KIND_MISSING = "missing"
KIND_OTHER = "other"


def week_start(day: date) -> date:
    """The Monday of ``day``'s week."""
    return day - timedelta(days=day.weekday())


def _ranked(groups: dict[str, dict], limit: int | None) -> tuple[list[dict], int]:
    """Named rows largest first, then "other", then "missing" last.

    ``groups`` maps a case-folded key to ``{label, consumers, recaps}``; the
    ``""`` key is the recaps with nothing recorded. Returns the rows and the
    number of distinct named groups.
    """
    named = sorted(
        (g for key, g in groups.items() if key and g["consumers"] > 0),
        key=lambda g: (-g["consumers"], g["label"].casefold()),
    )
    head = named if limit is None else named[:limit]
    tail = [] if limit is None else named[limit:]
    rows = [{**g, "kind": KIND_NAMED} for g in head]
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
    (date of the event), ``store`` and ``state`` (may be blank), and ``event``
    (its id, optional).

    Weeks run Monday to Sunday and are contiguous, zero-filled, from the
    first week to the last: the window's own weeks when ``start`` / ``end``
    are given, widened to any recap that falls outside them.
    """
    total = 0
    weeks: dict[date, dict] = {}
    stores: dict[str, dict] = {}
    states: dict[str, dict] = {}

    def add(groups: dict[str, dict], raw: str | None, consumers: int) -> None:
        label = (raw or "").strip()
        bucket = groups.setdefault(
            label.casefold(), {"label": label, "consumers": 0, "recaps": 0}
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
        add(stores, r.get("store"), consumers)
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


def _store(*names: str | None) -> str:
    for name in names:
        cleaned = (name or "").strip()
        if cleaned:
            return cleaned
    return ""


def tenant_consumer_breakdown(
    tenant_id: int, start: date | None, end: date | None
) -> dict:
    from events.routing import extract_state_code

    window = _inclusive_dates_to_window(start, end)
    rows: list[dict] = []

    # Custom recaps: where and when each one happened…
    placed: dict[int, dict] = {}
    custom = (
        _approved_only(
            _filter_event_window(
                CustomRecap.objects.filter(tenant_id=tenant_id), "event__", window
            ),
            "",
        )
        .annotate(_day=_event_date_expr("event__"))
        .filter(_day__isnull=False)
        .values_list(
            "id",
            "event_id",
            "_day",
            "event__retailer__name",
            "event__request__retailer__name",
            "event__request__retailer_name",
            "event__state__code",
            "event__address",
        )
    )
    for (
        recap_id,
        event_id,
        day,
        retailer,
        req_retailer,
        req_name,
        state,
        address,
    ) in custom.iterator():
        placed[recap_id] = {
            "event": event_id,
            "day": day.date(),
            "store": _store(retailer, req_retailer, req_name),
            "state": state or extract_state_code(address) or "",
        }

    # …and how many consumers it sampled, by the same field matcher (and the
    # same SQL gate on field names) the KPI totals use.
    pairs: dict[int, list[tuple[str | None, str | None]]] = {}
    values = (
        _approved_only(
            _filter_event_window(
                CustomFieldValue.objects.filter(
                    custom_recap__tenant_id=tenant_id,
                    custom_field__name__iregex=_CUSTOM_KPI_NAME_RE.pattern,
                ),
                "custom_recap__event__",
                window,
            ),
            "custom_recap__",
        )
        .values_list("custom_recap_id", "custom_field__name", "value")
        .order_by("custom_recap_id")
    )
    for recap_id, name, value in values.iterator():
        pairs.setdefault(recap_id, []).append((name, value))
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
                ConsumerEngagements.objects.filter(recap__event__tenant_id=tenant_id),
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
            "recap__event__retailer__name",
            "recap__event__request__retailer__name",
            "recap__event__request__retailer_name",
            "recap__event__state__code",
            "recap__event__address",
        )
    )
    for (
        recap_id,
        event_id,
        consumers,
        day,
        retailer,
        req_retailer,
        req_name,
        state,
        address,
    ) in engagements.iterator():
        row = legacy.setdefault(
            recap_id,
            {
                "event": event_id,
                "day": day.date(),
                "store": _store(retailer, req_retailer, req_name),
                "state": state or extract_state_code(address) or "",
                "consumers": 0,
            },
        )
        row["consumers"] += int(consumers or 0)
    rows.extend(legacy.values())

    return summarize_consumers(rows, start, end)
