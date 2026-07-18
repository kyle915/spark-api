"""Shared worked-hours + pay primitives over Attendance clock data.

One place to answer "how long was this BA actually on the clock, what were
they booked at, and are they on the clock RIGHT NOW" — so the mobile Earnings
screen, the admin live board, and the payroll export all compute hours the
SAME way instead of three subtly-different joins.

Worked hours = last clock-out − first clock-in per (event, ambassador), from
Attendance (Source-name based, the modern mobile clock path). No complete pair
→ the caller decides whether to fall back to the event's scheduled length
(flagged estimated). Pay = worked_hours × the BA's booked AmbassadorJob rate;
no booked rate → pay is None (honest: we never invent a dollar figure).

Mirrors events/pnl.py's join exactly — that module is the P&L (admin, per
event) lens; this is the reusable primitive the other three surfaces share.
"""

from __future__ import annotations

from collections import defaultdict


def clock_facts(event_ids) -> dict[tuple[int, int], dict]:
    """{(event_id, ambassador_id): {first_in, last_out, latest_kind}} over the
    given events. `latest_kind` is "clock_in"/"clock_out"/None (the most recent
    clock event by time) — "clock_in" means the BA is currently ON the clock.
    """
    from ambassadors.models import Attendance

    if not event_ids:
        return {}

    ins: dict[tuple[int, int], object] = {}
    outs: dict[tuple[int, int], object] = {}
    latest_at: dict[tuple[int, int], object] = {}
    latest_kind: dict[tuple[int, int], str] = {}

    rows = (
        Attendance.objects.filter(
            event_id__in=list(event_ids),
            source__name__in=["clock_in", "clock_out"],
        )
        .values_list("event_id", "ambassador_id", "source__name", "clock_time")
        .order_by("clock_time", "id")
    )
    for ev_id, amb_id, src, when in rows:
        if amb_id is None:
            continue
        key = (ev_id, amb_id)
        if src == "clock_in":
            if key not in ins or when < ins[key]:
                ins[key] = when
        else:
            if key not in outs or when > outs[key]:
                outs[key] = when
        # rows are time-ordered, so the last write wins as "latest"
        latest_at[key] = when
        latest_kind[key] = src

    out: dict[tuple[int, int], dict] = {}
    for key in set(ins) | set(outs):
        out[key] = {
            "first_in": ins.get(key),
            "last_out": outs.get(key),
            "latest_kind": latest_kind.get(key),
        }
    return out


def worked_hours(facts: dict | None, sched_hours: float | None):
    """(hours: float|None, estimated: bool) for one (event, ambassador).

    Clocked pair present → real worked hours, estimated=False.
    Otherwise fall back to the scheduled length (estimated=True) when we have
    one; None hours when we have neither.
    """
    if facts:
        t_in, t_out = facts.get("first_in"), facts.get("last_out")
        if t_in and t_out and t_out > t_in:
            return round((t_out - t_in).total_seconds() / 3600.0, 2), False
    if sched_hours is not None:
        return round(float(sched_hours), 2), True
    return None, True


def rate_map(event_ids) -> dict[tuple[int, int], float]:
    """{(event_id, ambassador_id): booked hourly rate} — latest AmbassadorJob
    rate wins. Missing → the pair is absent (caller shows pay as None)."""
    from jobs.models import AmbassadorJob

    if not event_ids:
        return {}
    rates: dict[tuple[int, int], float] = {}
    for ev_id, amb_id, amount in (
        AmbassadorJob.objects.filter(job__event_id__in=list(event_ids))
        .exclude(rate__isnull=True)
        .order_by("created_at")
        .values_list("job__event_id", "ambassador_id", "rate__amount")
    ):
        if amb_id is not None and amount is not None:
            rates[(ev_id, amb_id)] = float(amount)
    return rates


def scheduled_hours(start, end) -> float | None:
    """Wall-clock scheduled length in decimal hours, rolling a negative delta
    over midnight (+24h) — matches the earnings resolver's `_hours`. Accepts
    time or datetime objects."""
    if not start or not end:
        return None
    s = (start.hour * 3600) + (start.minute * 60) + start.second
    e = (end.hour * 3600) + (end.minute * 60) + end.second
    delta = e - s
    if delta < 0:
        delta += 24 * 3600
    return round(delta / 3600.0, 2)


def pay_for(hours: float | None, rate: float | None) -> float | None:
    """hours × rate, or None when either is missing (never invent a figure)."""
    if hours is None or rate is None:
        return None
    return round(hours * rate, 2)


__all__ = [
    "clock_facts",
    "worked_hours",
    "rate_map",
    "scheduled_hours",
    "pay_for",
    "defaultdict",
]
