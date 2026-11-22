"""Demo budget pacing and ROI leaderboard for a brand workspace.

Honest figures only: spent comes from brand-visible ``BrandCostLine`` rows
(and cached visit supported cost when lines are absent). Committed is the
remaining month's scheduled visits without complete costs, plus open rebook
budgets. Leaderboard ranks by observed units or register sales dollars per
demo dollar — never invents lift when POS/baseline inputs are missing.
"""

from __future__ import annotations

import calendar
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any, Literal

from django.db.models import Q
from django.utils import timezone

from pilot.costs import quantize_money, signed_line_amount
from pilot.models import (
    BrandCostLine,
    CanonicalVisit,
    RetailerSalesRow,
    StoreRecommendationAction,
)
from pilot.permissions import ResolvedWorkspaceAccess

GroupBy = Literal["retailer", "market", "sku"]
PaceStatus = Literal["on_track", "under_booked", "over_booked", "no_target"]

PACE_TOLERANCE = Decimal("0.15")
TWOPLACES = Decimal("0.01")
ZERO = Decimal("0.00")


def month_bounds(month: date) -> tuple[date, date]:
    """Inclusive first/last day of the calendar month containing ``month``."""
    first = month.replace(day=1)
    last_day = calendar.monthrange(first.year, first.month)[1]
    return first, first.replace(day=last_day)


def parse_month(raw: str | None, *, today: date | None = None) -> date:
    """Accept ``YYYY-MM`` or ``YYYY-MM-DD``; default to current month start."""
    today = today or timezone.localdate()
    text = (raw or "").strip()
    if not text:
        return today.replace(day=1)
    if len(text) == 7 and text[4] == "-":
        y, m = int(text[:4]), int(text[5:7])
        return date(y, m, 1)
    return date.fromisoformat(text[:10]).replace(day=1)


@dataclass(frozen=True)
class ScopeTarget:
    scope_type: str
    scope_key: str
    scope_label: str
    target_amount: Decimal
    notes: str
    uuid: str


@dataclass(frozen=True)
class PacingSummary:
    month: date
    month_end: date
    target_amount: Decimal | None
    spent_amount: Decimal
    committed_amount: Decimal
    projected_amount: Decimal
    elapsed_days: int
    days_in_month: int
    elapsed_pct: Decimal
    expected_spend_to_date: Decimal | None
    pace_status: PaceStatus
    pace_note: str
    visit_count: int
    incomplete_cost_visits: int
    can_edit_budget: bool
    targets: list[ScopeTarget]


@dataclass(frozen=True)
class LeaderboardRow:
    group_key: str
    group_label: str
    visit_count: int
    demo_dollars: Decimal
    units: int | None
    sales_dollars: Decimal | None
    units_per_dollar: Decimal | None
    revenue_per_dollar: Decimal | None
    note: str


def _can_edit(access: ResolvedWorkspaceAccess) -> bool:
    return bool(
        access.can_edit_brand_ops
        or access.can_edit_finance
        or access.is_brand_admin
    )


def _visits_qs(access: ResolvedWorkspaceAccess, start: date, end: date):
    return (
        CanonicalVisit.objects.filter(access.allowed_canonical_visits_q())
        .filter(
            visit_date__gte=start,
            visit_date__lte=end,
            exclude_from_aggregates=False,
        )
        .exclude(status=CanonicalVisit.STATUS_CANCELLED)
        .select_related("retailer")
    )


def _spent_for_visits(visits) -> tuple[Decimal, int]:
    """Return (spent, incomplete_visit_count) for the given visit queryset."""
    visit_ids = list(visits.values_list("id", flat=True))
    if not visit_ids:
        return ZERO, 0

    lines = BrandCostLine.objects.filter(canonical_visit_id__in=visit_ids)
    by_visit: dict[int, list[BrandCostLine]] = {}
    for line in lines:
        by_visit.setdefault(line.canonical_visit_id, []).append(line)

    spent = ZERO
    incomplete = 0
    for visit in visits:
        v_lines = by_visit.get(visit.id, [])
        if v_lines:
            known = False
            for line in v_lines:
                amt = signed_line_amount(line)
                if amt is None:
                    incomplete += 1
                    known = True
                    break
                spent += amt
            else:
                known = True
            if not known:
                incomplete += 1
        elif visit.cached_total_cost is not None and not visit.cached_cost_incomplete:
            spent += quantize_money(visit.cached_supported_cost or ZERO)
        elif visit.cached_supported_cost and visit.cached_supported_cost > ZERO:
            spent += quantize_money(visit.cached_supported_cost)
            if visit.cached_cost_incomplete:
                incomplete += 1
        else:
            incomplete += 1
    return quantize_money(spent), incomplete


def _committed_amount(
    access: ResolvedWorkspaceAccess, start: date, end: date, today: date
) -> Decimal:
    """Future scheduled visits this month without complete costs + open rebooks."""
    future = (
        _visits_qs(access, start, end)
        .filter(visit_date__gte=today)
        .filter(
            status__in={
                CanonicalVisit.STATUS_SCHEDULED,
                CanonicalVisit.STATUS_IN_PROGRESS,
            }
        )
    )
    committed = ZERO
    for visit in future:
        if visit.cached_cost_incomplete or visit.cached_total_cost is None:
            # Prefer agreement demo fee cache when present; else skip unknown.
            if visit.cached_supported_cost and visit.cached_supported_cost > ZERO:
                committed += quantize_money(visit.cached_supported_cost)
        # Fully costed future visits already count in spent via lines; do not
        # double-count them here.

    open_rebooks = StoreRecommendationAction.objects.filter(
        tenant=access.tenant,
        action_type=StoreRecommendationAction.ACTION_REBOOK,
        budget_amount__isnull=False,
    ).exclude(
        status__in={
            StoreRecommendationAction.STATUS_COMPLETED,
            StoreRecommendationAction.STATUS_CANCELLED,
            StoreRecommendationAction.STATUS_DECLINED,
        }
    )
    if access.is_provider_scoped and access.provider_id:
        open_rebooks = open_rebooks.filter(provider_id=access.provider_id)
    for action in open_rebooks:
        # Count rebook budgets targeting this month (or undated).
        if action.target_date is None or (start <= action.target_date <= end):
            # Skip if a return visit was already linked (avoid double count).
            if action.outcome_visit_id:
                continue
            committed += quantize_money(action.budget_amount or ZERO)

    return quantize_money(committed)


def _pace_status(
    *,
    target: Decimal | None,
    projected: Decimal,
    expected: Decimal | None,
) -> tuple[PaceStatus, str]:
    if target is None or target <= ZERO:
        return "no_target", "Set a monthly demo budget to see pacing."
    if expected is None or expected <= ZERO:
        return "on_track", "Early in the month — keep booking."
    ratio = projected / expected
    if ratio < (Decimal("1") - PACE_TOLERANCE):
        return (
            "under_booked",
            "Under pace — book more retail demos or lower the target.",
        )
    if ratio > (Decimal("1") + PACE_TOLERANCE):
        return (
            "over_booked",
            "Ahead of pace — watch early overspend before month-end.",
        )
    return "on_track", "Spending is within about 15% of expected pace."


def load_targets(tenant_id: int, month: date) -> list[ScopeTarget]:
    from brand_insights.models import DemoBudgetTarget

    rows = DemoBudgetTarget.objects.filter(tenant_id=tenant_id, month=month).order_by(
        "scope_type", "scope_key"
    )
    return [
        ScopeTarget(
            scope_type=r.scope_type,
            scope_key=r.scope_key,
            scope_label=r.scope_label or r.scope_key or r.scope_type,
            target_amount=quantize_money(r.target_amount),
            notes=r.notes or "",
            uuid=str(r.uuid),
        )
        for r in rows
    ]


def workspace_target(targets: list[ScopeTarget]) -> Decimal | None:
    for t in targets:
        if t.scope_type == "workspace":
            return t.target_amount
    return None


def compute_budget_pacing(
    access: ResolvedWorkspaceAccess,
    *,
    month: date | None = None,
    today: date | None = None,
) -> PacingSummary:
    today = today or timezone.localdate()
    month_start = (month or today).replace(day=1)
    start, end = month_bounds(month_start)
    days_in_month = end.day
    elapsed_days = max(1, min((today - start).days + 1, days_in_month))
    if today < start:
        elapsed_days = 1
    if today > end:
        elapsed_days = days_in_month
    elapsed_pct = quantize_money(Decimal(elapsed_days) / Decimal(days_in_month))

    visits = _visits_qs(access, start, end)
    visit_count = visits.count()
    spent, incomplete = _spent_for_visits(visits)
    committed = _committed_amount(access, start, end, today)
    # Spent already includes costed future visits; committed only adds
    # incomplete future + rebook budgets. Projected = spent + committed.
    projected = quantize_money(spent + committed)

    targets = load_targets(access.tenant.id, month_start)
    target = workspace_target(targets)
    expected = (
        quantize_money(target * elapsed_pct) if target is not None else None
    )
    status, note = _pace_status(
        target=target, projected=projected, expected=expected
    )

    return PacingSummary(
        month=month_start,
        month_end=end,
        target_amount=target,
        spent_amount=spent,
        committed_amount=committed,
        projected_amount=projected,
        elapsed_days=elapsed_days,
        days_in_month=days_in_month,
        elapsed_pct=elapsed_pct,
        expected_spend_to_date=expected,
        pace_status=status,
        pace_note=note,
        visit_count=visit_count,
        incomplete_cost_visits=incomplete,
        can_edit_budget=_can_edit(access),
        targets=targets,
    )


def _group_identity(visit: CanonicalVisit, group_by: GroupBy) -> tuple[str, str]:
    if group_by == "market":
        key = (visit.market or visit.state_code or "unknown").strip() or "unknown"
        label = visit.market or visit.state_code or "Unknown market"
        return key.lower(), label
    if group_by == "sku":
        # SKU grouping uses POS product when available; fall back to visit store.
        return "visit", "All visits (no SKU breakdown without POS)"
    key = (visit.chain_key or "").strip() or (
        f"store:{visit.store_key}" if visit.store_key else "unknown"
    )
    label = (
        visit.chain_label
        or (visit.retailer.name if visit.retailer_id else "")
        or visit.store_label
        or key
    )
    return key.lower(), label or key


def compute_roi_leaderboard(
    access: ResolvedWorkspaceAccess,
    *,
    month: date | None = None,
    group_by: GroupBy = "retailer",
    sort_by: Literal["units_per_dollar", "revenue_per_dollar"] = "units_per_dollar",
    today: date | None = None,
) -> list[LeaderboardRow]:
    today = today or timezone.localdate()
    month_start = (month or today).replace(day=1)
    start, end = month_bounds(month_start)
    visits = list(_visits_qs(access, start, end))
    if not visits:
        return []

    visit_ids = [v.id for v in visits]
    lines = list(BrandCostLine.objects.filter(canonical_visit_id__in=visit_ids))
    lines_by_visit: dict[int, list[BrandCostLine]] = {}
    for line in lines:
        lines_by_visit.setdefault(line.canonical_visit_id, []).append(line)

    # POS sales for the month window (matched rows only).
    sales_qs = RetailerSalesRow.objects.filter(
        tenant=access.tenant,
        match_status=RetailerSalesRow.MATCH_MATCHED,
        week_start__lte=end,
    ).filter(Q(week_end__gte=start) | Q(week_end__isnull=True, week_start__gte=start))
    if not access.can_view_brand_consolidated and access.provider_id:
        # Provider-scoped: only sales for stores on their visits.
        store_keys = {v.store_key for v in visits if v.store_key}
        sales_qs = sales_qs.filter(store_key__in=store_keys)

    sales_by_store: dict[str, dict[str, Any]] = {}
    for row in sales_qs.select_related("product"):
        units = int(row.register_units or 0) if row.register_units is not None else 0
        dollars = (
            quantize_money(row.sales_dollars)
            if row.sales_dollars is not None
            else ZERO
        )
        if row.store_key:
            bucket = sales_by_store.setdefault(
                row.store_key, {"units": 0, "dollars": ZERO}
            )
            bucket["units"] += units
            bucket["dollars"] += dollars

    buckets: dict[str, dict[str, Any]] = {}

    for visit in visits:
        if group_by == "sku":
            # SKU ranking is built from POS rows below; skip visit buckets here.
            continue
        v_lines = lines_by_visit.get(visit.id, [])
        cost = ZERO
        cost_known = False
        if v_lines:
            cost_known = True
            for line in v_lines:
                amt = signed_line_amount(line)
                if amt is None:
                    cost_known = False
                    break
                cost += amt
        elif visit.cached_supported_cost is not None:
            cost = quantize_money(visit.cached_supported_cost)
            cost_known = not visit.cached_cost_incomplete

        key, label = _group_identity(visit, group_by)
        b = buckets.setdefault(
            key,
            {
                "label": label,
                "visits": 0,
                "cost": ZERO,
                "units": 0,
                "sales": ZERO,
                "has_units": False,
                "has_sales": False,
                "cost_known": True,
                "store_keys": set(),
            },
        )
        b["visits"] += 1
        if cost_known:
            b["cost"] += cost
        else:
            b["cost_known"] = False
            if cost > ZERO:
                b["cost"] += cost
        if visit.rep_observed_units is not None:
            b["units"] += int(visit.rep_observed_units)
            b["has_units"] = True
        if visit.store_key:
            b["store_keys"].add(visit.store_key)

    if group_by == "sku":
        # Build from POS SKU buckets; attach proportional demo dollars from
        # visits sharing the same store_key as the POS row's store.
        visit_cost_by_store: dict[str, Decimal] = {}
        for visit in visits:
            sk = visit.store_key or ""
            if not sk:
                continue
            v_lines = lines_by_visit.get(visit.id, [])
            cost = ZERO
            if v_lines:
                for line in v_lines:
                    amt = signed_line_amount(line)
                    if amt is not None:
                        cost += amt
            elif visit.cached_supported_cost:
                cost = quantize_money(visit.cached_supported_cost)
            visit_cost_by_store[sk] = visit_cost_by_store.get(sk, ZERO) + cost

        # Re-query sales with product for SKU buckets (already in sales_by_sku
        # but without store linkage for cost share). Rebuild with store.
        sales_by_sku_store: dict[str, dict[str, Any]] = {}
        for row in sales_qs.select_related("product"):
            if not row.product_id:
                continue
            key = f"product:{row.product_id}"
            bucket = sales_by_sku_store.setdefault(
                key,
                {
                    "label": row.product.name if row.product else row.raw_sku,
                    "units": 0,
                    "dollars": ZERO,
                    "store_keys": set(),
                    "visits": 0,
                },
            )
            if row.register_units is not None:
                bucket["units"] += int(row.register_units)
            if row.sales_dollars is not None:
                bucket["dollars"] += quantize_money(row.sales_dollars)
            if row.store_key:
                bucket["store_keys"].add(row.store_key)

        # Count visits per store for visit_count attribution.
        visits_by_store: dict[str, int] = {}
        for visit in visits:
            if visit.store_key:
                visits_by_store[visit.store_key] = (
                    visits_by_store.get(visit.store_key, 0) + 1
                )

        rows: list[LeaderboardRow] = []
        for key, data in sales_by_sku_store.items():
            stores = data["store_keys"]
            # Share of demo dollars: sum of visit costs at those stores,
            # split equally across SKUs present at each store (honest, simple).
            demo = ZERO
            vcount = 0
            for sk in stores:
                store_skus = sum(
                    1
                    for other in sales_by_sku_store.values()
                    if sk in other["store_keys"]
                )
                share = Decimal(store_skus) if store_skus else Decimal("1")
                demo += visit_cost_by_store.get(sk, ZERO) / share
                vcount += visits_by_store.get(sk, 0)
            demo = quantize_money(demo)
            units = int(data["units"]) if data["units"] else None
            sales = (
                quantize_money(data["dollars"])
                if data["dollars"] > ZERO
                else None
            )
            upd = (
                quantize_money(Decimal(units) / demo)
                if units is not None and demo > ZERO
                else None
            )
            rpd = (
                quantize_money(sales / demo)
                if sales is not None and demo > ZERO
                else None
            )
            note = ""
            if demo <= ZERO:
                note = "Demo dollars incomplete — rate withheld."
            elif sales is None and units is None:
                note = "No matched retailer sales for this SKU."
            rows.append(
                LeaderboardRow(
                    group_key=key,
                    group_label=data["label"] or key,
                    visit_count=vcount,
                    demo_dollars=demo,
                    units=units,
                    sales_dollars=sales,
                    units_per_dollar=upd,
                    revenue_per_dollar=rpd,
                    note=note,
                )
            )
        reverse = True
        rows.sort(
            key=lambda r: (
                (r.revenue_per_dollar or ZERO)
                if sort_by == "revenue_per_dollar"
                else (r.units_per_dollar or ZERO),
                r.demo_dollars,
            ),
            reverse=reverse,
        )
        return rows

    # retailer / market buckets: fold in POS sales by store_key.
    rows = []
    for key, data in buckets.items():
        pos_units = 0
        pos_dollars = ZERO
        has_pos = False
        for sk in data.get("store_keys") or []:
            s = sales_by_store.get(sk)
            if not s:
                continue
            has_pos = True
            pos_units += int(s["units"])
            pos_dollars += s["dollars"]

        # Prefer POS adjusted units when available; else rep-observed.
        units: int | None
        if has_pos:
            units = pos_units
        elif data["has_units"]:
            units = int(data["units"])
        else:
            units = None

        sales = quantize_money(pos_dollars) if has_pos and pos_dollars > ZERO else None
        demo = quantize_money(data["cost"])
        upd = (
            quantize_money(Decimal(units) / demo)
            if units is not None and demo > ZERO
            else None
        )
        rpd = (
            quantize_money(sales / demo)
            if sales is not None and demo > ZERO
            else None
        )
        note = ""
        if not data["cost_known"] or demo <= ZERO:
            note = "Demo dollars incomplete — efficiency rates may be understated."
        elif units is None and sales is None:
            note = "No observed units or retailer sales for this group."
        elif sales is None:
            note = "Revenue/dollar withheld — retailer sales unavailable."

        rows.append(
            LeaderboardRow(
                group_key=key,
                group_label=data["label"],
                visit_count=int(data["visits"]),
                demo_dollars=demo,
                units=units,
                sales_dollars=sales,
                units_per_dollar=upd,
                revenue_per_dollar=rpd,
                note=note,
            )
        )

    rows.sort(
        key=lambda r: (
            (r.revenue_per_dollar or ZERO)
            if sort_by == "revenue_per_dollar"
            else (r.units_per_dollar or ZERO),
            r.demo_dollars,
        ),
        reverse=True,
    )
    return rows


def upsert_budget_target(
    access: ResolvedWorkspaceAccess,
    *,
    month: date,
    target_amount: Decimal,
    scope_type: str = "workspace",
    scope_key: str = "",
    scope_label: str = "",
    notes: str = "",
) -> ScopeTarget:
    from brand_insights.models import DemoBudgetTarget

    if not _can_edit(access):
        raise PermissionError("Not allowed to edit demo budget targets.")
    if scope_type not in {
        DemoBudgetTarget.SCOPE_WORKSPACE,
        DemoBudgetTarget.SCOPE_RETAILER,
        DemoBudgetTarget.SCOPE_MARKET,
    }:
        raise ValueError("scope_type must be workspace, retailer, or market.")
    month = month.replace(day=1)
    amount = quantize_money(target_amount)
    if amount < ZERO:
        raise ValueError("target_amount cannot be negative.")
    key = (scope_key or "").strip()
    if scope_type == DemoBudgetTarget.SCOPE_WORKSPACE:
        key = ""
    label = (scope_label or "").strip()
    if scope_type == DemoBudgetTarget.SCOPE_WORKSPACE and not label:
        label = "Workspace"
    row, _ = DemoBudgetTarget.objects.update_or_create(
        tenant=access.tenant,
        month=month,
        scope_type=scope_type,
        scope_key=key,
        defaults={
            "scope_label": label,
            "target_amount": amount,
            "notes": (notes or "").strip(),
            "updated_by": access.user,
        },
    )
    return ScopeTarget(
        scope_type=row.scope_type,
        scope_key=row.scope_key,
        scope_label=row.scope_label,
        target_amount=quantize_money(row.target_amount),
        notes=row.notes or "",
        uuid=str(row.uuid),
    )


def delete_budget_target(access: ResolvedWorkspaceAccess, *, uuid: str) -> bool:
    from brand_insights.models import DemoBudgetTarget

    if not _can_edit(access):
        raise PermissionError("Not allowed to edit demo budget targets.")
    deleted, _ = DemoBudgetTarget.objects.filter(
        tenant=access.tenant, uuid=uuid
    ).delete()
    return deleted > 0
