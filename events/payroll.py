"""Payroll timesheet — per-BA worked hours + estimated pay for a pay period.

`payrollTimesheet(startDate, endDate, tenantId?)` rolls the clock data up by
ambassador across every shift in the window: total worked hours (real
clock-in→out, scheduled fallback flagged), total estimated pay (hours × booked
rate), and shift count. The web page renders it and offers a CSV the admin
hands to Wingspan/Gusto. Same clock+rate join as pnl.py / the BA earnings
screen, aggregated the other way (by BA, not by event).

Honest: `estimated_pay` only counts shifts that had a booked rate; hours with no
rate still show (as hours), they just don't contribute dollars, and the row
flags how many shifts were rate-less so the admin sees the gap.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import date as _date

import strawberry
from asgiref.sync import sync_to_async

from events.models import Event
from utils.graphql.permissions import IsClientOrSparkAdmin
from utils.graphql.mixins import resolve_id_to_int
from events.staffing_board import _accessible_tenants


@strawberry.type
class PayrollTimesheetRow:
    ambassador_uuid: str
    name: str
    email: str | None = None
    shifts: int = 0
    hours: float = 0.0
    # How many of `shifts` fell back to scheduled length (no clock pair).
    estimated_shifts: int = 0
    # Sum of hours×rate over shifts that HAD a booked rate.
    estimated_pay: float | None = None
    # Shifts with no booked rate (contribute hours but no pay).
    shifts_missing_rate: int = 0
    # Payroll-prep flags a human should check before paying this row:
    #   no_rate          — ≥1 shift has no booked rate (pay is incomplete)
    #   unverified_hours — ≥1 shift used scheduled length (no clock pair) —
    #                      confirm the BA actually worked it
    flags: list[str] = strawberry.field(default_factory=list)
    # True when `flags` is non-empty — the FE surfaces a "Review" chip.
    needs_review: bool = False


@strawberry.type
class PayrollTimesheet:
    start_date: str
    end_date: str
    rows: list[PayrollTimesheetRow] = strawberry.field(default_factory=list)
    total_hours: float = 0.0
    total_estimated_pay: float | None = None
    # How many rows carry at least one flag.
    flagged_rows: int = 0


@strawberry.type
class PayrollQueries:
    @strawberry.field(permission_classes=[IsClientOrSparkAdmin])
    async def payroll_timesheet(
        self,
        info: strawberry.Info,
        start_date: str,
        end_date: str,
        tenant_id: strawberry.ID | None = None,
    ) -> PayrollTimesheet:
        """Per-BA worked hours + estimated pay over [start_date, end_date]
        (inclusive, by event date). Tenant-scoped."""
        user = info.context.request.user
        is_admin, allowed = await _accessible_tenants(user)
        resolved_tid = None
        if tenant_id is not None:
            try:
                resolved_tid = resolve_id_to_int(tenant_id)
            except Exception:  # noqa: BLE001
                resolved_tid = None

        def _go():
            from ambassadors.attendance_hours import (
                clock_facts,
                rate_map,
                scheduled_hours,
                worked_hours,
            )

            try:
                start = _date.fromisoformat(str(start_date))
                end = _date.fromisoformat(str(end_date))
            except (TypeError, ValueError):
                return PayrollTimesheet(
                    start_date=str(start_date), end_date=str(end_date), rows=[]
                )

            qs = (
                Event.objects.filter(
                    date__date__gte=start, date__date__lte=end
                )
                .prefetch_related("ambassadors_events__ambassador__user")
                .order_by("date")
            )
            if not is_admin:
                qs = qs.filter(tenant_id__in=(allowed or set()))
            elif resolved_tid is not None:
                qs = qs.filter(tenant_id=resolved_tid)

            events = list(qs[:5000])
            event_ids = [ev.id for ev in events]
            facts = clock_facts(event_ids)
            rates = rate_map(event_ids)

            # ambassador_id -> aggregate dict
            agg: dict[int, dict] = defaultdict(
                lambda: {
                    "uuid": None, "name": "", "email": None,
                    "shifts": 0, "hours": 0.0, "estimated_shifts": 0,
                    "pay": 0.0, "any_pay": False, "missing_rate": 0,
                }
            )
            for ev in events:
                sched = scheduled_hours(
                    getattr(ev, "start_time", None),
                    getattr(ev, "end_time", None),
                )
                for ae in ev.ambassadors_events.all():
                    if not ae.is_approved:
                        continue
                    amb = ae.ambassador
                    if amb is None:
                        continue
                    key = (ev.id, amb.id)
                    wh, est = worked_hours(facts.get(key), sched)
                    rate = rates.get(key)
                    a = agg[amb.id]
                    if a["uuid"] is None:
                        u = getattr(amb, "user", None)
                        a["uuid"] = str(amb.uuid)
                        a["name"] = (
                            (f"{u.first_name or ''} {u.last_name or ''}".strip()
                             or (u.email or "")) if u else ""
                        ) or "(unnamed)"
                        a["email"] = getattr(u, "email", None) if u else None
                    a["shifts"] += 1
                    if wh is not None:
                        a["hours"] += wh
                        if est:
                            a["estimated_shifts"] += 1
                    if rate is not None and wh is not None:
                        a["pay"] += wh * rate
                        a["any_pay"] = True
                    else:
                        a["missing_rate"] += 1

            rows: list[PayrollTimesheetRow] = []
            total_hours = 0.0
            total_pay = 0.0
            any_pay_total = False
            flagged = 0
            for a in agg.values():
                total_hours += a["hours"]
                if a["any_pay"]:
                    total_pay += a["pay"]
                    any_pay_total = True
                flags: list[str] = []
                if a["missing_rate"] > 0:
                    flags.append("no_rate")
                if a["estimated_shifts"] > 0:
                    flags.append("unverified_hours")
                if flags:
                    flagged += 1
                rows.append(
                    PayrollTimesheetRow(
                        ambassador_uuid=a["uuid"] or "",
                        name=a["name"],
                        email=a["email"],
                        shifts=a["shifts"],
                        hours=round(a["hours"], 2),
                        estimated_shifts=a["estimated_shifts"],
                        estimated_pay=round(a["pay"], 2) if a["any_pay"] else None,
                        shifts_missing_rate=a["missing_rate"],
                        flags=flags,
                        needs_review=bool(flags),
                    )
                )
            # Flagged rows first (they need attention), then alphabetical.
            rows.sort(key=lambda r: (not r.needs_review, r.name.lower()))
            return PayrollTimesheet(
                start_date=start.isoformat(),
                end_date=end.isoformat(),
                rows=rows,
                total_hours=round(total_hours, 2),
                total_estimated_pay=round(total_pay, 2) if any_pay_total else None,
                flagged_rows=flagged,
            )

        return await sync_to_async(_go)()
