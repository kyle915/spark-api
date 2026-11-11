"""Where and when a program performs: samples and units per staffed hour.

Answers "which stores and time slots convert" for the program report:

* totals — samples per hour and units sold per hour across the window
* by weekday and by part of day (venue-local time)
* top stores by units sold, with their own per-hour rates

Built from the same sources as everything else so the figures agree:
sampled / sold come from the recap field matchers the recap cards use
(``_consumers_sampled_from_fields`` / ``_sold_units_from_fields``), and
staffed hours come from the clock punches behind Campaign Cost
(``events.pnl.event_pnl_rows``). Approved, non-archived custom recaps only,
windowed by EVENT date.

An event with no clocked hours still counts toward sampled/sold totals but
is left out of every per-hour rate — dividing by zero hours, or by an
assumed shift length, would invent a number.

READ-ONLY.
"""

from __future__ import annotations

from collections import OrderedDict
from datetime import date

from recaps.models import CustomFieldValue, CustomRecap
from recaps.tenant_overview import (
    _approved_only,
    _filter_event_window,
    _inclusive_dates_to_window,
)
from recaps.types import _consumers_sampled_from_fields, _sold_units_from_fields

WEEKDAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
DAYPARTS = ["Morning", "Midday", "Afternoon", "Evening"]
TOP_STORES = 10


def daypart_for_hour(hour: int) -> str:
    """Morning <11, Midday 11–2, Afternoon 2–5, Evening 5+ (venue local)."""
    if hour < 11:
        return "Morning"
    if hour < 14:
        return "Midday"
    if hour < 17:
        return "Afternoon"
    return "Evening"


def _rate(amount: int, hours: float) -> float | None:
    return round(amount / hours, 1) if hours > 0 else None


def _bucket() -> dict:
    return {"recaps": 0, "sampled": 0, "sold": 0, "hours": 0.0, "timed_sampled": 0, "timed_sold": 0}


def _finish(label: str, b: dict) -> dict:
    return {
        "label": label,
        "recaps": b["recaps"],
        "sampled": b["sampled"],
        "sold": b["sold"],
        "hours": round(b["hours"], 1),
        # Rates use only the recaps that have clocked hours behind them.
        "samples_per_hour": _rate(b["timed_sampled"], b["hours"]),
        "units_per_hour": _rate(b["timed_sold"], b["hours"]),
    }


def summarize_efficiency(rows: list[dict]) -> dict:
    """Pure roll-up. Each row: ``sampled``, ``sold``, ``hours`` (0 when no
    clock data), ``weekday`` (0=Mon or None), ``hour`` (0–23 or None),
    ``store`` (display name)."""
    total = _bucket()
    by_day = OrderedDict((d, _bucket()) for d in WEEKDAYS)
    by_part = OrderedDict((p, _bucket()) for p in DAYPARTS)
    stores: dict[str, dict] = {}

    def add(b: dict, r: dict) -> None:
        b["recaps"] += 1
        b["sampled"] += r["sampled"]
        b["sold"] += r["sold"]
        if r["hours"] > 0:
            b["hours"] += r["hours"]
            b["timed_sampled"] += r["sampled"]
            b["timed_sold"] += r["sold"]

    for r in rows:
        add(total, r)
        if r.get("weekday") is not None:
            add(by_day[WEEKDAYS[r["weekday"]]], r)
        if r.get("hour") is not None:
            add(by_part[daypart_for_hour(r["hour"])], r)
        name = (r.get("store") or "").strip()
        if name:
            add(stores.setdefault(name, _bucket()), r)

    top = sorted(stores.items(), key=lambda kv: (-kv[1]["sold"], -kv[1]["sampled"], kv[0]))
    return {
        "total": _finish("All activations", total),
        "by_weekday": [_finish(d, b) for d, b in by_day.items() if b["recaps"]],
        "by_daypart": [_finish(p, b) for p, b in by_part.items() if b["recaps"]],
        "top_stores": [_finish(n, b) for n, b in top[:TOP_STORES]],
        "recaps_without_hours": sum(1 for r in rows if r["hours"] <= 0),
    }


def tenant_field_efficiency(tenant_id: int, start: date | None, end: date | None) -> dict:
    from events.pnl import event_pnl_rows
    from utils.tz import iana_for_us_state, resolve_zoneinfo, _try_zone

    window = _inclusive_dates_to_window(start, end)
    recaps = _filter_event_window(
        _approved_only(
            CustomRecap.objects.filter(tenant_id=tenant_id, archived_at__isnull=True),
            "",
        ),
        "event__",
        window,
    ).select_related("event", "event__timezone", "event__retailer", "event__state")
    recaps = list(recaps)
    if not recaps:
        return summarize_efficiency([])

    pairs: dict[int, list[tuple[str | None, str | None]]] = {}
    values = CustomFieldValue.objects.filter(
        custom_recap_id__in=[r.id for r in recaps]
    ).values_list("custom_recap_id", "custom_field__name", "value")
    for recap_id, name, value in values.iterator(chunk_size=5000):
        pairs.setdefault(recap_id, []).append((name, value))

    # Clocked hours per event. Bound the P&L pull to the recaps' own dates so
    # an all-time report does not scan every event the brand ever had.
    event_dates = [
        (getattr(r.event, "date", None) or getattr(r.event, "start_time", None))
        for r in recaps
        if r.event is not None
    ]
    event_dates = [d.date() for d in event_dates if d is not None]
    hours_by_event: dict[int, float] = {}
    if event_dates:
        for row in event_pnl_rows(tenant_id, min(event_dates), max(event_dates)):
            hours_by_event[row["event_id"]] = float(row.get("hours") or 0)

    # Several recaps can sit on one event (two ambassadors): split the
    # event's clocked hours evenly so they are not counted once per recap.
    per_event: dict[int, int] = {}
    for r in recaps:
        if r.event_id:
            per_event[r.event_id] = per_event.get(r.event_id, 0) + 1

    rows: list[dict] = []
    for r in recaps:
        fields = pairs.get(r.id, [])
        sampled = _consumers_sampled_from_fields(fields) or 0
        sold = _sold_units_from_fields(fields) or 0
        event = r.event
        when = None
        store = ""
        hours = 0.0
        if event is not None:
            when = getattr(event, "start_time", None) or getattr(event, "date", None)
            zone = resolve_zoneinfo(getattr(event, "timezone", None))
            if zone is None:
                state_code = getattr(getattr(event, "state", None), "code", None)
                iana = iana_for_us_state(state_code)
                zone = _try_zone(iana) if iana else None
            if when is not None and zone is not None:
                try:
                    when = when.astimezone(zone)
                except (ValueError, OverflowError):
                    pass
            store = (
                getattr(getattr(event, "retailer", None), "name", None)
                or getattr(event, "name", None)
                or ""
            )
            share = per_event.get(event.id, 1) or 1
            hours = hours_by_event.get(event.id, 0.0) / share
        rows.append(
            {
                "sampled": int(sampled),
                "sold": int(sold),
                "hours": hours,
                "weekday": when.weekday() if when is not None else None,
                "hour": when.hour if when is not None else None,
                "store": store,
            }
        )
    return summarize_efficiency(rows)
