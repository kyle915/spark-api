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

A rate the clock cannot support is held (returned as null) rather than
printed. One person cannot sustain more than one sample every 30 seconds
(120 samples an hour) or one unit sold a minute (60 an hour) across the
clocked time. Chevron at 5,500 samples an hour and Albertsons at 294 are
held; the units-sold count on that row stays.

The Friday / midday sentence is worded from the day and shift rows after
those rates are held. Spark still owns samples per hour and units per hour.

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
# Faster than this, the clock does not support the rate.
MAX_SAMPLES_PER_HOUR = 120
MAX_UNITS_PER_HOUR = 60
_DAY_NAMES = {
    "Mon": "Mondays",
    "Tue": "Tuesdays",
    "Wed": "Wednesdays",
    "Thu": "Thursdays",
    "Fri": "Fridays",
    "Sat": "Saturdays",
    "Sun": "Sundays",
}


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


def _hold(rate: float | None, ceiling: float) -> tuple[float | None, bool]:
    """Null a per-hour figure the clocked time cannot support."""
    if rate is None or rate <= ceiling:
        return rate, False
    return None, True


def _bucket() -> dict:
    return {"recaps": 0, "sampled": 0, "sold": 0, "hours": 0.0, "timed_sampled": 0, "timed_sold": 0}


def _finish(label: str, b: dict) -> tuple[dict, bool]:
    samples, held_samples = _hold(
        _rate(b["timed_sampled"], b["hours"]), MAX_SAMPLES_PER_HOUR
    )
    units, held_units = _hold(_rate(b["timed_sold"], b["hours"]), MAX_UNITS_PER_HOUR)
    return {
        "label": label,
        "recaps": b["recaps"],
        "sampled": b["sampled"],
        "sold": b["sold"],
        "hours": round(b["hours"], 1),
        # Rates use only the recaps that have clocked hours behind them.
        "samples_per_hour": samples,
        "units_per_hour": units,
    }, held_samples or held_units


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

    def _rows(pairs) -> tuple[list[dict], int]:
        finished: list[dict] = []
        held = 0
        for label, bucket in pairs:
            row, was_held = _finish(label, bucket)
            finished.append(row)
            held += int(was_held)
        return finished, held

    total_row, total_held = _finish("All activations", total)
    by_weekday, weekday_held = _rows((d, b) for d, b in by_day.items() if b["recaps"])
    by_daypart, daypart_held = _rows((p, b) for p, b in by_part.items() if b["recaps"])
    top_stores, store_held = _rows(top[:TOP_STORES])
    return {
        "total": total_row,
        "by_weekday": by_weekday,
        "by_daypart": by_daypart,
        "top_stores": top_stores,
        "recaps_without_hours": sum(1 for r in rows if r["hours"] <= 0),
        "rates_held": total_held + weekday_held + daypart_held + store_held,
    }


def _best_slice(rows: list[dict], min_recaps: int = 2) -> dict | None:
    """The slice with the best units-per-hour among rows whose rate was kept.
    Falls back to samples per hour when nothing was sold."""
    solid = [r for r in rows if r["recaps"] >= min_recaps and r["hours"] > 0]
    by_sold = [r for r in solid if (r["units_per_hour"] or 0) > 0]
    pool = by_sold or [r for r in solid if (r["samples_per_hour"] or 0) > 0]
    if not pool:
        return None
    key = (
        (lambda r: r["units_per_hour"] or 0)
        if by_sold
        else (lambda r: r["samples_per_hour"] or 0)
    )
    return max(pool, key=key)


def _slice_sold(row: dict | None) -> bool:
    return row is not None and (row["units_per_hour"] or 0) > 0


def _slice_line(row: dict, *, sold: bool) -> str:
    verb = "sell the most per hour" if sold else "sample the most per hour"
    if row["label"] in _DAY_NAMES:
        return f"{_DAY_NAMES[row['label']]} {verb}."
    return f"{row['label']} shifts {verb}."


def efficiency_headline(summary: dict) -> str:
    """The day and shift line, from rows whose per-hour figures were kept.

    A day picked by units sold and a shift picked by samples are two
    sentences. One verb would call the samples shift a sales leader.
    """
    day = _best_slice(summary["by_weekday"])
    part = _best_slice(summary["by_daypart"])
    day_sold = _slice_sold(day)
    part_sold = _slice_sold(part)
    if day and part and day_sold != part_sold:
        return f"{_slice_line(day, sold=day_sold)} {_slice_line(part, sold=part_sold)}"
    verb = "sell the most per hour" if day_sold or part_sold else "sample the most per hour"
    if day and part:
        name = _DAY_NAMES.get(day["label"], day["label"])
        return f"{name} and {part['label'].lower()} shifts {verb}."
    if day:
        return _slice_line(day, sold=day_sold)
    if part:
        return _slice_line(part, sold=part_sold)
    return ""


def efficiency_draft(summary: dict):
    """Facts for the day/shift sentence: cleaned day and shift rows only."""
    from recaps.explain_text import Draft

    template = efficiency_headline(summary)
    if not template:
        return None
    lines = [
        "Day and shift rows after per-hour figures the clock cannot support were removed.",
        "Do not mention a store. Do not mention a rate that is not listed.",
    ]
    for group, rows in (
        ("Day", summary["by_weekday"]),
        ("Shift", summary["by_daypart"]),
    ):
        for row in rows:
            if row["samples_per_hour"] is None and row["units_per_hour"] is None:
                continue
            bits = [f"{group} {row['label']}: {row['recaps']} activations"]
            if row["samples_per_hour"] is not None:
                bits.append(f"{row['samples_per_hour']} samples per hour")
            if row["units_per_hour"] is not None:
                bits.append(f"{row['units_per_hour']} units sold per hour")
            lines.append(", ".join(bits) + ".")
    return Draft("headline", template, "\n".join(lines))


def _with_headline(tenant_id: int, start: date | None, end: date | None, summary: dict) -> dict:
    """Word the day/shift line. The template is used when the model is out
    or adds a number that is not in the cleaned tables."""
    draft = efficiency_draft(summary)
    if draft is None:
        summary["headline"] = ""
        summary["headline_source"] = "template"
        return summary
    try:
        from recaps import explain_text

        written = explain_text.write(
            tenant_id,
            "field_efficiency",
            explain_text.window_key(start, end),
            [draft],
        )[0]
        summary["headline"] = written.text
        summary["headline_source"] = written.source
    except Exception:  # noqa: BLE001 — the tables still stand without the sentence
        summary["headline"] = draft.template
        summary["headline_source"] = "template"
    return summary


def _resolve_efficiency_store(recap, aliases: dict[str, str]) -> str:
    """Resolve a recap's display store in the same order as consumer_breakdown,
    falling back to a clean walk-in place/address without leading dates."""
    from events.retailer_aliases import resolve_store_name
    from recaps.consumer_breakdown import (
        KIND_VENUE,
        _PROGRAM_SEP,
        _WALKIN_DATE,
        _numbered,
        place_from_walkin_title,
    )

    event = recap.event
    req = getattr(event, "request", None) if event is not None else None
    recap_retailer = _numbered(
        getattr(getattr(recap, "retailer", None), "name", None),
        getattr(recap, "store_number", None),
    )
    for candidate in (
        recap_retailer,
        getattr(getattr(event, "retailer", None), "name", None) if event is not None else None,
        getattr(getattr(req, "retailer", None), "name", None) if req is not None else None,
        getattr(req, "retailer_name", None) if req is not None else None,
    ):
        cleaned = (candidate or "").strip()
        if cleaned:
            return resolve_store_name(cleaned, aliases) if aliases else cleaned

    if event is None:
        return ""
    raw_name = (getattr(event, "name", None) or "").strip()
    if not raw_name:
        return ""
    if not _WALKIN_DATE.match(raw_name):
        return raw_name

    event_type_name = getattr(getattr(event, "event_type", None), "name", None)
    label, kind = place_from_walkin_title(
        raw_name, getattr(event, "address", None), event_type_name
    )
    if label:
        if kind == KIND_VENUE:
            return label
        return resolve_store_name(label, aliases) if aliases else label

    # Bare street-address walk-in: strip "M/D/YYYY - " and trailing " · <program>".
    stripped = _WALKIN_DATE.sub("", raw_name).strip()
    while _PROGRAM_SEP in stripped:
        cut = stripped.rfind(_PROGRAM_SEP)
        tail = stripped[cut + len(_PROGRAM_SEP) :]
        if tail.count("(") != tail.count(")"):
            break
        stripped = stripped[:cut].rstrip()
    return stripped or raw_name


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
    ).select_related(
        "retailer",
        "event",
        "event__timezone",
        "event__retailer",
        "event__state",
        "event__event_type",
        "event__request",
        "event__request__retailer",
    )
    recaps = list(recaps)
    if not recaps:
        return _with_headline(tenant_id, start, end, summarize_efficiency([]))

    pairs: dict[int, list[tuple[str | None, str | None]]] = {}
    from recaps import metric_mapping

    values = CustomFieldValue.objects.filter(
        custom_recap_id__in=[r.id for r in recaps]
    ).values_list("custom_recap_id", "custom_field_id", "custom_field__name", "value")
    # Names carry staff's confirmed metric when there is one.
    confirmed = metric_mapping.confirmed_metrics(tenant_id=tenant_id)
    for recap_id, field_id, name, value in values.iterator(chunk_size=5000):
        pairs.setdefault(recap_id, []).append(
            (metric_mapping.tag_name(name, field_id, confirmed), value)
        )

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

    # Accepted store-name aliases ("HEB" shown as "H-E-B"); usually empty.
    from events.retailer_aliases import accepted_alias_map

    aliases = accepted_alias_map(tenant_id)

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
            share = per_event.get(event.id, 1) or 1
            hours = hours_by_event.get(event.id, 0.0) / share
        store = _resolve_efficiency_store(r, aliases)
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
    return _with_headline(tenant_id, start, end, summarize_efficiency(rows))
