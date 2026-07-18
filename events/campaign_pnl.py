"""Campaign cost view — what a brand's program cost over a date range.

`campaignCost(startDate, endDate, tenantId?)` rolls the per-event P&L
(events/pnl.py: labor = clock-hours × booked rate, + expense-receipt spend)
up to one row per brand/campaign (tenant) so the founder can see, at a glance,
what each program is costing. Margin is intentionally NOT computed here: Spark
doesn't hold the client sell-price/revenue, so the web page takes an optional
revenue the admin types and computes margin there — the cost side stays 100%
real, sourced from the same clock + rate + receipt data as everything else.
"""
from __future__ import annotations

from datetime import date as _date

import strawberry
from asgiref.sync import sync_to_async
from strawberry.relay import to_base64

from events.models import Event
from utils.graphql.permissions import IsClientOrSparkAdmin
from utils.graphql.mixins import resolve_id_to_int
from events.staffing_board import _accessible_tenants


@strawberry.type
class CampaignCostRow:
    tenant_id: str
    tenant_name: str
    events: int = 0
    # BA-shifts (sum of assigned BAs across the campaign's events).
    shifts: int = 0
    hours: float = 0.0
    labor_cost: float = 0.0
    spend: float = 0.0
    total_cost: float = 0.0
    # How many events had ≥1 BA with no booked rate (labor understated).
    events_missing_rates: int = 0


@strawberry.type
class CampaignCostReport:
    start_date: str
    end_date: str
    rows: list[CampaignCostRow] = strawberry.field(default_factory=list)
    total_labor: float = 0.0
    total_spend: float = 0.0
    total_cost: float = 0.0


@strawberry.type
class CampaignPnlQueries:
    @strawberry.field(permission_classes=[IsClientOrSparkAdmin])
    async def campaign_cost(
        self,
        info: strawberry.Info,
        start_date: str,
        end_date: str,
        tenant_id: strawberry.ID | None = None,
    ) -> CampaignCostReport:
        """Per-campaign (brand) cost roll-up over [start_date, end_date].
        Tenant-scoped; labor + spend are real, margin is left to the caller."""
        user = info.context.request.user
        is_admin, allowed = await _accessible_tenants(user)
        resolved_tid = None
        if tenant_id is not None:
            try:
                resolved_tid = resolve_id_to_int(tenant_id)
            except Exception:  # noqa: BLE001
                resolved_tid = None

        def _go():
            from events.pnl import event_pnl_rows

            try:
                start = _date.fromisoformat(str(start_date))
                end = _date.fromisoformat(str(end_date))
            except (TypeError, ValueError):
                return CampaignCostReport(
                    start_date=str(start_date), end_date=str(end_date), rows=[]
                )

            # Which tenants are in scope AND actually have events in range.
            tid_qs = (
                Event.objects.filter(
                    date__date__gte=start, date__date__lte=end
                )
                .values_list("tenant_id", flat=True)
                .distinct()
            )
            tenant_ids = {t for t in tid_qs if t is not None}
            if resolved_tid is not None:
                tenant_ids &= {resolved_tid}
            if not is_admin:
                tenant_ids &= set(allowed or set())
            if not tenant_ids:
                return CampaignCostReport(
                    start_date=start.isoformat(), end_date=end.isoformat(), rows=[]
                )

            from tenants.models import Tenant

            names = dict(
                Tenant.objects.filter(id__in=tenant_ids).values_list("id", "name")
            )

            rows: list[CampaignCostRow] = []
            t_labor = t_spend = t_total = 0.0
            for tid in tenant_ids:
                ev_rows = event_pnl_rows(tid, start, end)
                if not ev_rows:
                    continue
                labor = sum(r["labor_cost"] for r in ev_rows)
                spend = sum(r["spend"] for r in ev_rows)
                total = sum(r["total_cost"] for r in ev_rows)
                hours = sum(r["hours"] for r in ev_rows)
                shifts = sum(r["ba_count"] for r in ev_rows)
                missing = sum(1 for r in ev_rows if r.get("missing_rates"))
                t_labor += labor
                t_spend += spend
                t_total += total
                rows.append(
                    CampaignCostRow(
                        tenant_id=to_base64("TenantType", tid),
                        tenant_name=names.get(tid) or "(unnamed)",
                        events=len(ev_rows),
                        shifts=shifts,
                        hours=round(hours, 2),
                        labor_cost=round(labor, 2),
                        spend=round(spend, 2),
                        total_cost=round(total, 2),
                        events_missing_rates=missing,
                    )
                )
            rows.sort(key=lambda r: r.total_cost, reverse=True)
            return CampaignCostReport(
                start_date=start.isoformat(),
                end_date=end.isoformat(),
                rows=rows,
                total_labor=round(t_labor, 2),
                total_spend=round(t_spend, 2),
                total_cost=round(t_total, 2),
            )

        return await sync_to_async(_go)()
