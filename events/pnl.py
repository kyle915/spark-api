"""Per-event labor cost + spend roll-up ("P&L lens").

Spark already records everything needed to know what an activation
COST — clock-in/clock-out attendance, the booked hourly rate on
AmbassadorJob, and the expense receipts in recaps — it just never put
them in one place. This module does the join, honestly:

  * hours — first clock_in → last clock_out per (event, ambassador)
    from Attendance (Source name based, the modern mobile path). A
    booked BA with no clock pair falls back to the event's SCHEDULED
    duration and the row is flagged ``estimated``.
  * rate — the BA's booked AmbassadorJob.rate.amount for that event's
    job (latest row wins). No rate → that BA contributes 0 and the
    event is flagged ``missing_rates`` (the admin sees the gap instead
    of a silently-wrong number).
  * spend — the expense-receipts collector's per-recap amounts
    (custom 'Account Spend Amount'-style fields + legacy
    account_spend_amount), grouped by event.

Pure read-side; returns plain dicts for the GraphQL layer.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import date


def event_pnl_rows(tenant_id: int, start: date, end: date) -> list[dict]:
    from ambassadors.models import AmbassadorEvent, Attendance
    from events.models import Event
    from jobs.models import AmbassadorJob
    from recaps.receipts_export import collect_expense_rows

    events = list(
        Event.objects.filter(
            tenant_id=tenant_id,
            date__date__gte=start,
            date__date__lte=end,
        ).values("id", "uuid", "name", "date", "start_time", "end_time")
    )
    if not events:
        return []
    event_ids = [e["id"] for e in events]

    # Scheduled duration fallback (hours) per event.
    sched_hours: dict[int, float] = {}
    for e in events:
        st, en = e["start_time"], e["end_time"]
        if st and en and en > st:
            sched_hours[e["id"]] = (en - st).total_seconds() / 3600.0

    # Approved roster per event.
    roster: dict[int, set[int]] = defaultdict(set)
    for ev_id, amb_id in AmbassadorEvent.objects.filter(
        event_id__in=event_ids, is_approved=True
    ).values_list("event_id", "ambassador_id"):
        roster[ev_id].add(amb_id)

    # Clock pairs per (event, ambassador): first in, last out.
    ins: dict[tuple[int, int], object] = {}
    outs: dict[tuple[int, int], object] = {}
    for ev_id, amb_id, src, when in Attendance.objects.filter(
        event_id__in=event_ids,
        source__name__in=["clock_in", "clock_out"],
    ).values_list("event_id", "ambassador_id", "source__name", "clock_time"):
        key = (ev_id, amb_id)
        if src == "clock_in":
            if key not in ins or when < ins[key]:
                ins[key] = when
        else:
            if key not in outs or when > outs[key]:
                outs[key] = when

    # Booked hourly rate per (event, ambassador) — latest AmbassadorJob.
    rates: dict[tuple[int, int], float] = {}
    for ev_id, amb_id, amount in (
        AmbassadorJob.objects.filter(job__event_id__in=event_ids)
        .exclude(rate__isnull=True)
        .order_by("created_at")
        .values_list("job__event_id", "ambassador_id", "rate__amount")
    ):
        rates[(ev_id, amb_id)] = float(amount)

    # Spend per event from the receipts collector (its rows carry the
    # event date/name; we re-key by event via a uuid map).
    spend_by_event: dict[int, float] = defaultdict(float)
    uuid_to_id = {str(e["uuid"]): e["id"] for e in events}
    for row in collect_expense_rows(tenant_id, start, end):
        ev_id = uuid_to_id.get(row.get("event_uuid") or "")
        if ev_id and row["amount"] is not None:
            spend_by_event[ev_id] += float(row["amount"])

    rows: list[dict] = []
    for e in events:
        ev_id = e["id"]
        labor = 0.0
        hours_total = 0.0
        estimated = False
        missing_rates = 0
        for amb_id in roster.get(ev_id, set()):
            key = (ev_id, amb_id)
            t_in, t_out = ins.get(key), outs.get(key)
            if t_in and t_out and t_out > t_in:
                hours = (t_out - t_in).total_seconds() / 3600.0
            elif ev_id in sched_hours:
                hours = sched_hours[ev_id]
                estimated = True
            else:
                hours = 0.0
                estimated = True
            rate = rates.get(key)
            if rate is None:
                missing_rates += 1
            else:
                labor += hours * rate
            hours_total += hours
        spend = spend_by_event.get(ev_id, 0.0)
        rows.append(
            {
                "event_id": ev_id,
                "uuid": str(e["uuid"]),
                "name": e["name"] or "(unnamed)",
                "date": e["date"].date().isoformat() if e["date"] else None,
                "ba_count": len(roster.get(ev_id, set())),
                "hours": round(hours_total, 2),
                "labor_cost": round(labor, 2),
                "spend": round(spend, 2),
                "total_cost": round(labor + spend, 2),
                "estimated": estimated,
                "missing_rates": missing_rates,
            }
        )

    # Cost-bearing or staffed events only — empty shells are noise.
    rows = [
        r
        for r in rows
        if r["ba_count"] or r["total_cost"] or r["spend"]
    ]
    rows.sort(key=lambda r: (r["date"] or "", r["name"]))
    return rows
