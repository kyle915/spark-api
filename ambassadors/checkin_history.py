"""Shift history for the public web check-in — "what happened to my hours?".

After clock-out the check-in page used to show only the in/out times, so every
"did my hours go through?" / "when do I get paid?" question became a text to an
admin. ``shift_history_for`` gives a BA their own recent shifts for the link's
tenant with real worked hours and a plain status:

* ``pending_review``  — clocked, waiting for Ignite to confirm the walk-up
* ``confirmed``       — an admin confirmed the shift (it counts toward payroll)
* ``hours_approved``  — the pay period containing it was approved
* ``paid``            — the disbursement was recorded (``paidAt`` set)

Deliberately NO dollar amounts: the check-in identity is a phone number, not a
login, so anything returned here is visible to whoever can type that number.
Hours and status are low-risk; rates and pay are not.

Pure-sync, read-only — safe to call from the public sync views.
"""
from __future__ import annotations

from datetime import timedelta

from django.utils import timezone as dj_tz

STATUS_PENDING = "pending_review"
STATUS_CONFIRMED = "confirmed"
STATUS_HOURS_APPROVED = "hours_approved"
STATUS_PAID = "paid"


def shift_history_for(*, ambassador, tenant, days: int = 60, limit: int = 30) -> dict:
    """Recent shifts for ``ambassador`` within ``tenant`` (newest first)."""
    from ambassadors.attendance_hours import clock_facts, worked_hours
    from ambassadors.checkin_web import event_calendar_date
    from ambassadors.models import AmbassadorEvent
    from events.models import PayrollApproval
    from recaps.filed import has_filed_recap

    empty = {"shifts": [], "totalHours": 0.0, "windowDays": days}
    if ambassador is None or tenant is None:
        return empty

    cutoff = dj_tz.now() - timedelta(days=days)
    bookings = list(
        AmbassadorEvent.objects.filter(
            ambassador=ambassador,
            event__tenant=tenant,
            event__date__gte=cutoff,
        )
        .select_related("event")
        .order_by("-event__date", "-id")[: limit * 2]
    )
    if not bookings:
        return empty

    event_ids = [b.event_id for b in bookings]
    facts = clock_facts(event_ids)
    approvals = list(
        PayrollApproval.objects.filter(tenant=tenant, ambassador=ambassador)
    )

    out: list[dict] = []
    total = 0.0
    seen: set[int] = set()
    for b in bookings:
        ev = b.event
        if ev is None or ev.id in seen:
            continue
        seen.add(ev.id)
        f = facts.get((ev.id, ambassador.id))
        # Only shifts the BA actually clocked — a booking with no punch is a
        # schedule entry, not history.
        if not f or not f.get("first_in"):
            continue
        hours, estimated = worked_hours(f, None)
        on_clock = f.get("latest_kind") == "clock_in"
        cal = event_calendar_date(ev)

        status = STATUS_CONFIRMED if b.is_approved else STATUS_PENDING
        paid_at = None
        if b.is_approved and cal is not None:
            for ap in approvals:
                if ap.period_start <= cal <= ap.period_end:
                    status = STATUS_HOURS_APPROVED
                    if ap.paid_at:
                        status = STATUS_PAID
                        paid_at = ap.paid_at.isoformat()
                        break

        if hours is not None and not on_clock:
            total += hours
        out.append(
            {
                "eventDate": cal.isoformat() if cal else None,
                "name": ev.name or "",
                "address": ev.address or "",
                "clockInAt": f["first_in"].isoformat() if f.get("first_in") else None,
                "clockOutAt": (
                    f["last_out"].isoformat()
                    if f.get("last_out") and not on_clock
                    else None
                ),
                "onTheClock": on_clock,
                "hours": None if on_clock or estimated else hours,
                "status": status,
                "paidAt": paid_at,
                "recapFiled": has_filed_recap(
                    ambassador_id=ambassador.id, event_id=ev.id
                ),
            }
        )
        if len(out) >= limit:
            break

    return {"shifts": out, "totalHours": round(total, 2), "windowDays": days}
