"""Feature 5 service layer: Permissioned retailer POS sales import (store x SKU x week),
clean version replacement, unmatched resolution, store-SKU-week coverage,
store-week deduplication across multiple demos, linked crew-purchase adjustment,
baseline-gated incremental lift, contribution & ROI, and unavailable state."""

from __future__ import annotations

import csv
import hashlib
import io
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any

from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from brand_insights.facts import store_identity
from events.models import Product, Retailer
from events.retailer_aliases import (
    accepted_alias_map,
    resolve_store_name,
    split_store_number,
)
from pilot.costs import compute_visit_cost_summary, quantize_money
from pilot.imports import (
    brand_included_visits_queryset,
    build_product_lookup,
    normalize_product_key,
)
from pilot.models import (
    BrandCostLine,
    CanonicalVisit,
    CrewPurchaseAdjustment,
    ProductAlias,
    RetailerSalesImport,
    RetailerSalesRow,
    WorkspaceProfile,
)
from pilot.permissions import ResolvedWorkspaceAccess, log_workspace_audit

RETAILER_UNAVAILABLE_MESSAGE = "Retailer sales unavailable"

_POS_SYNONYMS: dict[str, tuple[str, ...]] = {
    "retailer": ("retailer", "retailer_banner", "banner", "chain"),
    "store_id": ("store_id", "store id", "store #", "store_number", "location_id"),
    "store_name": ("store_name", "store name", "store", "location_name"),
    "address": ("address", "store_address", "street"),
    "city": ("city", "town"),
    "state_code": ("state", "state_code"),
    "sku": ("sku", "upc", "product", "product_name", "item", "item_code"),
    "pack_size": ("pack_size", "pack size", "uom", "size"),
    "week_start": ("week_start", "week start", "period_start", "week_beginning"),
    "week_end": ("week_end", "week end", "period_end", "week_ending"),
    "register_units": (
        "register_units",
        "pos_units",
        "units_sold",
        "units",
        "scan_units",
    ),
    "sales_dollars": ("sales_dollars", "pos_dollars", "dollar_sales", "revenue"),
    "baseline_units": ("baseline_units", "baseline", "expected_units"),
    "baseline_method": ("baseline_method", "method"),
    "baseline_window_label": ("baseline_window", "baseline_window_label"),
    "baseline_sample_size": ("baseline_weeks", "baseline_sample_size", "sample_size"),
    "has_promotion": ("promo", "has_promotion", "on_promo", "tpr"),
    "has_pack_change": ("pack_change", "has_pack_change"),
    "has_stockout": ("stockout", "has_stockout", "oos"),
}


def monday_of_week(d: date) -> date:
    return d - timedelta(days=d.weekday())


def _parse_pos_date(raw: str | None) -> date | None:
    val = (raw or "").strip()
    if not val:
        return None
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y", "%Y/%m/%d"):
        try:
            return datetime.strptime(val, fmt).date()
        except ValueError:
            continue
    return None


def _parse_bool(raw: str | None) -> bool:
    return (raw or "").strip().lower() in {"1", "true", "yes", "y", "promo", "oos"}


def _infer_pos_map(
    headers: list[str], explicit_map: dict[str, str] | None = None
) -> dict[str, str]:
    lookup = {h.strip().lower(): h for h in headers if h and h.strip()}
    resolved: dict[str, str] = {}
    for key, syns in _POS_SYNONYMS.items():
        for s in syns:
            if s in lookup:
                resolved[key] = lookup[s]
                break
    if explicit_map:
        for k, col in explicit_map.items():
            if col:
                resolved[k] = lookup.get(col.strip().lower(), col)
    return resolved


def _resolve_pos_store(
    *,
    tenant,
    retailer_banner: str,
    raw_store_id: str,
    raw_store_name: str,
    address: str,
    city: str,
    state_code: str,
    aliases: dict[str, str],
) -> tuple[str, str, str, Retailer | None, bool, str]:
    """Match a POS row store to a canonical store_key in this workspace."""
    clean_store_id = (raw_store_id or "").strip().lstrip("#").strip()
    raw_name = (raw_store_name or "").strip()
    banner = resolve_store_name(raw_name or retailer_banner, aliases)
    base_banner, embedded_num, _marked = split_store_number(banner)
    eff_num = clean_store_id or (embedded_num or "")
    candidate_name = (
        f"{base_banner} #{eff_num}".strip() if eff_num and base_banner else banner
    )

    retailers = list(
        Retailer.objects.select_related("location", "location__state").filter(
            tenant=tenant
        )
    )
    visits = list(
        CanonicalVisit.objects.filter(tenant=tenant).only(
            "store_key",
            "store_label",
            "chain_key",
            "store_number",
            "external_store_id",
            "retailer_id",
            "address",
            "city",
            "state_code",
        )
    )

    # 1. Exact match by external_store_id or store_number on existing visits.
    if clean_store_id:
        for v in visits:
            if (
                v.external_store_id
                and v.external_store_id.strip().lower() == clean_store_id.lower()
            ) or (
                v.store_number
                and v.store_number.strip().lstrip("#").lower()
                == clean_store_id.lower()
            ):
                ret = next(
                    (r for r in retailers if r.id == v.retailer_id), None
                )
                return v.store_key, v.store_label, v.chain_key, ret, True, ""

    # 2. Exact match on Retailer name in workspace.
    if candidate_name:
        exact_ret = [
            r
            for r in retailers
            if r.name.strip().lower() == candidate_name.lower()
        ]
        if len(exact_ret) == 1:
            r = exact_ret[0]
            loc = r.location if getattr(r, "location_id", None) else None
            rcity = loc.name if loc else ""
            rstate = loc.state.code if loc and getattr(loc, "state_id", None) and loc.state else ""
            s_key, s_label, c_key, _ = store_identity(
                r.name,
                address or r.address or "",
                city or rcity,
                state_code or rstate,
            )
            return s_key, s_label, c_key, r, True, ""

    # 3. Match by store_identity against existing CanonicalVisits.
    if candidate_name and (eff_num or address or city):
        s_key, s_label, c_key, _ = store_identity(
            candidate_name,
            address or (f"#{eff_num}" if eff_num else ""),
            city,
            state_code,
        )
        if any(v.store_key == s_key for v in visits):
            return s_key, s_label, c_key, None, True, ""
        # Also allow known store with explicit store # + city/state even if no
        # visit occurred yet in that store, PROVIDED raw_store_name or store_id
        # matches the workspace banner rather than an unknown code!
        if eff_num and (city or address):
            return s_key, s_label, c_key, None, True, ""

    return (
        "",
        raw_name or raw_store_id or retailer_banner,
        "",
        None,
        False,
        f"Unmatched store '{raw_name or raw_store_id}': confirm canonical store.",
    )


@transaction.atomic
def import_retailer_pos_csv(
    access: ResolvedWorkspaceAccess,
    *,
    retailer_banner: str,
    csv_text: str,
    filename: str = "pos.csv",
    column_map: dict[str, str] | None = None,
    feed_completeness: str = RetailerSalesImport.COMPLETENESS_COMPLETE,
    period_start_override: date | None = None,
    period_end_override: date | None = None,
) -> RetailerSalesImport:
    """Import a permissioned retailer POS sales CSV (`store x SKU x week`),
    superseding any previous active version for the same retailer & period."""
    if not access.can_manage_pos:
        raise PermissionError(
            "You do not have permission to import retailer POS sales data."
        )
    clean_banner = (retailer_banner or "").strip()
    if not clean_banner:
        raise ValueError("Retailer banner name is required.")
    if feed_completeness not in {
        RetailerSalesImport.COMPLETENESS_COMPLETE,
        RetailerSalesImport.COMPLETENESS_PARTIAL,
        RetailerSalesImport.COMPLETENESS_DELAYED,
    }:
        raise ValueError(f"Invalid feed_completeness: {feed_completeness}")

    cleaned_csv = (csv_text or "").lstrip("\ufeff")
    reader = csv.DictReader(io.StringIO(cleaned_csv))
    headers = [h for h in (reader.fieldnames or []) if h is not None]
    if not headers:
        raise ValueError("POS CSV has no header row.")

    resolved_map = _infer_pos_map(headers, explicit_map=column_map)
    raw_rows = [
        {
            str(k): (str(v).strip() if v is not None else "")
            for k, v in r.items()
            if k is not None
        }
        for r in reader
    ]
    raw_rows = [r for r in raw_rows if any(v != "" for v in r.values())]
    if not raw_rows:
        raise ValueError("POS CSV contains no data rows.")

    aliases = accepted_alias_map(access.tenant.id)
    product_lookup = build_product_lookup(access.tenant)
    parsed_rows: list[dict[str, Any]] = []
    week_starts: list[date] = []
    week_ends: list[date] = []

    for idx, raw in enumerate(raw_rows, start=1):
        def _c(k: str) -> str:
            col = resolved_map.get(k)
            return (raw.get(col) or "").strip() if col else ""

        w_start = _parse_pos_date(_c("week_start"))
        w_end = _parse_pos_date(_c("week_end"))
        if w_start is None and w_end is not None:
            w_start = w_end - timedelta(days=6)
        if w_start is not None:
            w_start = monday_of_week(w_start)
            if w_end is None:
                w_end = w_start + timedelta(days=6)
            week_starts.append(w_start)
            week_ends.append(w_end)

        raw_units = _c("register_units")
        units_val: int | None = None
        is_zero = False
        units_valid = True
        if raw_units == "":
            units_valid = False
        else:
            try:
                dec_u = Decimal(raw_units.replace(",", ""))
                if dec_u < 0 or dec_u != dec_u.to_integral_value():
                    units_valid = False
                else:
                    units_val = int(dec_u)
                    is_zero = units_val == 0
            except InvalidOperation:
                units_valid = False

        raw_dollars = _c("sales_dollars").replace("$", "").replace(",", "")
        dollars_val: Decimal | None = None
        if raw_dollars:
            try:
                dollars_val = quantize_money(raw_dollars)
            except InvalidOperation:
                dollars_val = None

        raw_base = _c("baseline_units").replace(",", "")
        base_val: Decimal | None = None
        if raw_base:
            try:
                base_val = quantize_money(raw_base)
            except InvalidOperation:
                base_val = None

        raw_base_n = _c("baseline_sample_size")
        base_n = int(raw_base_n) if raw_base_n.isdigit() else (4 if base_val is not None else 0)

        raw_sku = _c("sku")
        prod = product_lookup.get(normalize_product_key(raw_sku))

        s_key, s_label, c_key, ret_obj, store_ok, store_note = _resolve_pos_store(
            tenant=access.tenant,
            retailer_banner=_c("retailer") or clean_banner,
            raw_store_id=_c("store_id"),
            raw_store_name=_c("store_name"),
            address=_c("address"),
            city=_c("city"),
            state_code=_c("state_code"),
            aliases=aliases,
        )

        if w_start is None:
            m_status = RetailerSalesRow.MATCH_UNMATCHED_DATE
            m_note = f"Invalid or missing week_start: '{_c('week_start')}'."
        elif not units_valid:
            m_status = RetailerSalesRow.MATCH_INVALID_UNITS
            m_note = f"Invalid register_units value: '{raw_units}'."
        elif not store_ok:
            m_status = RetailerSalesRow.MATCH_UNMATCHED_STORE
            m_note = store_note
        elif prod is None:
            m_status = RetailerSalesRow.MATCH_UNMATCHED_PRODUCT
            m_note = f"Unmatched SKU '{raw_sku}': map to canonical product."
        else:
            m_status = RetailerSalesRow.MATCH_MATCHED
            m_note = ""

        parsed_rows.append(
            {
                "row_number": idx,
                "raw_retailer": _c("retailer") or clean_banner,
                "raw_store_id": _c("store_id"),
                "raw_store_name": _c("store_name"),
                "raw_sku": raw_sku,
                "raw_pack_size": _c("pack_size"),
                "raw_week_start": _c("week_start"),
                "raw_week_end": _c("week_end"),
                "raw_row": raw,
                "store_key": s_key,
                "store_label": s_label,
                "chain_key": c_key,
                "retailer_store": ret_obj,
                "product": prod,
                "pack_size": _c("pack_size"),
                "week_start": w_start,
                "week_end": w_end,
                "register_units": units_val,
                "is_explicit_zero": is_zero,
                "sales_dollars": dollars_val,
                "baseline_units": base_val,
                "baseline_method": _c("baseline_method")
                or ("provided_baseline" if base_val is not None else ""),
                "baseline_window_label": _c("baseline_window_label")
                or ("4-week prior average" if base_val is not None else ""),
                "baseline_sample_size": base_n,
                "has_promotion": _parse_bool(_c("has_promotion")),
                "has_pack_change": _parse_bool(_c("has_pack_change")),
                "has_stockout": _parse_bool(_c("has_stockout")),
                "match_status": m_status,
                "match_note": m_note,
            }
        )

    p_start = period_start_override or (
        min(week_starts) if week_starts else timezone.now().date()
    )
    p_end = period_end_override or (
        max(week_ends) if week_ends else p_start + timedelta(days=6)
    )

    prior_imports = list(
        RetailerSalesImport.objects.filter(
            tenant=access.tenant,
            retailer_banner__iexact=clean_banner,
            period_start=p_start,
            period_end=p_end,
            is_active_version=True,
        ).order_by("-version")
    )
    next_version = (prior_imports[0].version + 1) if prior_imports else 1
    file_hash = hashlib.sha256(csv_text.encode("utf-8")).hexdigest()

    matched_c = sum(
        1
        for r in parsed_rows
        if r["match_status"] == RetailerSalesRow.MATCH_MATCHED
    )
    unmatched_c = len(parsed_rows) - matched_c

    sales_import = RetailerSalesImport.objects.create(
        tenant=access.tenant,
        retailer_banner=clean_banner,
        owner=access.user,
        period_start=p_start,
        period_end=p_end,
        version=next_version,
        is_active_version=True,
        feed_completeness=feed_completeness,
        filename=(filename or "pos.csv")[:255],
        file_sha256=file_hash,
        mapping_snapshot=resolved_map,
        total_rows=len(parsed_rows),
        matched_rows=matched_c,
        unmatched_rows=unmatched_c,
    )

    # Supersede prior versions cleanly so reimports never double-count!
    for prior in prior_imports:
        prior.is_active_version = False
        prior.superseded_by = sales_import
        prior.save(update_fields=["is_active_version", "superseded_by"])

    RetailerSalesRow.objects.bulk_create(
        [
            RetailerSalesRow(
                sales_import=sales_import,
                tenant=access.tenant,
                **r,
            )
            for r in parsed_rows
        ]
    )

    log_workspace_audit(
        tenant=access.tenant,
        actor=access.user,
        category="pos",
        action="pos_csv_imported",
        target_type="RetailerSalesImport",
        target_id=str(sales_import.uuid),
        summary=(
            f"Imported POS feed v{next_version} for {clean_banner} "
            f"({p_start.isoformat()} to {p_end.isoformat()}): "
            f"{matched_c} matched, {unmatched_c} unmatched."
        ),
    )
    return sales_import


@transaction.atomic
def resolve_pos_sales_row(
    access: ResolvedWorkspaceAccess,
    *,
    row_uuid: str,
    store_key: str | None = None,
    store_label: str | None = None,
    retailer_id: int | None = None,
    product_id: int | None = None,
    save_product_alias: bool = True,
) -> RetailerSalesRow:
    if not access.can_manage_pos:
        raise PermissionError(
            "You do not have permission to resolve POS matching issues."
        )
    row = (
        RetailerSalesRow.objects.select_related("sales_import")
        .filter(tenant=access.tenant, uuid=row_uuid)
        .first()
    )
    if row is None:
        raise ValueError("POS row not found.")

    if retailer_id is not None:
        ret = (
            Retailer.objects.select_related("location", "location__state")
            .filter(tenant=access.tenant, id=retailer_id)
            .first()
        )
        if ret is None:
            raise ValueError("Retailer store not found.")
        loc = ret.location if getattr(ret, "location_id", None) else None
        rcity = loc.name if loc else ""
        rstate = loc.state.code if loc and getattr(loc, "state_id", None) and loc.state else ""
        s_key, s_label, c_key, _ = store_identity(
            ret.name, ret.address or "", rcity, rstate
        )
        row.retailer_store = ret
        row.store_key = s_key
        row.store_label = s_label
        row.chain_key = c_key
    elif store_key:
        row.store_key = store_key.strip()
        if store_label:
            row.store_label = store_label.strip()

    if product_id is not None:
        prod = Product.objects.filter(
            tenant=access.tenant,
            id=product_id,
        ).first()
        if prod is None:
            raise ValueError("Canonical product not found.")
        row.product = prod
        if save_product_alias and row.raw_sku:
            ak = normalize_product_key(row.raw_sku)
            if ak:
                ProductAlias.objects.update_or_create(
                    tenant=access.tenant,
                    alias_key=ak,
                    defaults={
                        "alias_name": row.raw_sku[:200],
                        "product": prod,
                        "status": ProductAlias.STATUS_ACCEPTED,
                        "decided_by": access.user,
                        "decided_at": timezone.now(),
                    },
                )

    if (
        row.week_start is not None
        and row.register_units is not None
        and row.store_key
        and row.product_id is not None
    ):
        was_unmatched = row.match_status != RetailerSalesRow.MATCH_MATCHED
        row.match_status = RetailerSalesRow.MATCH_MATCHED
        row.match_note = ""
        row.resolved_by = access.user
        row.resolved_at = timezone.now()
        row.save()
        if was_unmatched:
            imp = row.sales_import
            imp.matched_rows += 1
            if imp.unmatched_rows > 0:
                imp.unmatched_rows -= 1
            imp.save(update_fields=["matched_rows", "unmatched_rows"])
    else:
        row.save()
    return row


@transaction.atomic
def record_crew_purchase_adjustment(
    access: ResolvedWorkspaceAccess,
    *,
    visit_uuid: str,
    product_id: int,
    units_purchased: int,
    purchase_date: date | None = None,
    retailer_banner: str = "",
    receipt_reading_id: int | None = None,
    recap_file_id: int | None = None,
    cost_line_uuid: str | None = None,
    transaction_ref: str = "",
) -> CrewPurchaseAdjustment:
    """Link an explicit in-period crew/BA product purchase transaction to its
    exact store, SKU, and Monday POS week so adjusted register units subtract
    crew purchases without mutating original register_units."""
    if not access.can_manage_pos and not access.can_edit_finance:
        raise PermissionError(
            "You do not have permission to link crew purchase adjustments."
        )
    if units_purchased <= 0:
        raise ValueError("units_purchased must be a positive integer.")

    visit = CanonicalVisit.objects.filter(
        tenant=access.tenant, uuid=visit_uuid
    ).first()
    if visit is None:
        raise ValueError("Canonical visit not found.")
    if not visit.store_key:
        raise ValueError("Canonical visit has no canonical store_key.")

    product = Product.objects.filter(
        tenant=access.tenant,
        id=product_id,
    ).first()
    if product is None:
        raise ValueError("Product not found.")

    eff_date = purchase_date or visit.visit_date
    w_start = monday_of_week(eff_date)
    cost_line = (
        BrandCostLine.objects.filter(
            tenant=access.tenant, uuid=cost_line_uuid
        ).first()
        if cost_line_uuid
        else None
    )

    adj = CrewPurchaseAdjustment.objects.create(
        tenant=access.tenant,
        canonical_visit=visit,
        product=product,
        store_key=visit.store_key,
        retailer_banner=(retailer_banner or visit.chain_label or "").strip(),
        week_start=w_start,
        purchase_date=eff_date,
        units_purchased=units_purchased,
        receipt_reading_id=receipt_reading_id,
        recap_file_id=recap_file_id,
        cost_line=cost_line,
        transaction_ref=(transaction_ref or "").strip()[:120],
        created_by=access.user,
    )
    log_workspace_audit(
        tenant=access.tenant,
        actor=access.user,
        category="pos",
        action="crew_purchase_linked",
        target_type="CrewPurchaseAdjustment",
        target_id=str(adj.uuid),
        summary=(
            f"Linked {units_purchased} crew-purchased units of {product.name} "
            f"at {visit.store_label} for week of {w_start.isoformat()}."
        ),
    )
    return adj


def build_retailer_pos_measurement(
    access: ResolvedWorkspaceAccess,
    *,
    period_start: date | None = None,
    period_end: date | None = None,
    store_key: str | None = None,
    contribution_per_unit: Decimal | None = None,
) -> dict[str, Any]:
    """Compute POS coverage, deduplicated store-week sales, crew-purchase
    adjustments, baseline-gated incremental lift, contribution, and ROI.
    Returns clean 'Retailer sales unavailable' state when no active feed exists."""
    if not access.can_view_brand_consolidated:
        raise PermissionError(
            "Outside providers cannot view retailer POS sales measurement."
        )

    active_imports = list(
        RetailerSalesImport.objects.filter(
            tenant=access.tenant, is_active_version=True
        ).order_by("-period_end", "-id")
    )
    if not active_imports:
        return {
            "available": False,
            "unavailable_message": RETAILER_UNAVAILABLE_MESSAGE,
            "retailer_banners": [],
            "coverage": {
                "matched_store_sku_weeks": 0,
                "eligible_store_sku_weeks": 0,
                "coverage_ratio": None,
                "missing_rows": [],
                "unmatched_row_count": 0,
            },
            "totals": {
                "rep_observed_units": 0,
                "register_units": None,
                "crew_purchased_units": 0,
                "adjusted_register_units": None,
                "observed_vs_baseline_diff": None,
                "incremental_lift_units": None,
                "incremental_contribution": None,
                "eligible_brand_cost": None,
                "roi": None,
                "lift_withheld_reason": RETAILER_UNAVAILABLE_MESSAGE,
                "roi_withheld_reason": RETAILER_UNAVAILABLE_MESSAGE,
            },
            "store_weeks": [],
        }

    eff_contrib = contribution_per_unit
    if eff_contrib is None:
        profile = WorkspaceProfile.objects.filter(tenant=access.tenant).first()
        if profile and profile.default_contribution_per_unit is not None:
            eff_contrib = profile.default_contribution_per_unit

    pos_rows_qs = RetailerSalesRow.objects.select_related(
        "sales_import", "product"
    ).filter(
        tenant=access.tenant,
        sales_import__is_active_version=True,
    )
    if period_start:
        pos_rows_qs = pos_rows_qs.filter(
            Q(week_start__gte=period_start) | Q(week_start__isnull=True)
        )
    if period_end:
        pos_rows_qs = pos_rows_qs.filter(
            Q(week_start__lte=period_end) | Q(week_start__isnull=True)
        )
    if store_key:
        pos_rows_qs = pos_rows_qs.filter(store_key=store_key)

    all_pos_rows = list(pos_rows_qs)
    matched_rows = [
        r
        for r in all_pos_rows
        if r.match_status == RetailerSalesRow.MATCH_MATCHED
        and r.store_key
        and r.product_id is not None
        and r.week_start is not None
    ]
    unmatched_count = sum(
        1
        for r in all_pos_rows
        if r.match_status != RetailerSalesRow.MATCH_MATCHED
    )

    # Index all historical matched rows by (store_key, product_id) for trailing
    # non-demo baseline fallback when baseline_units wasn't provided inline.
    all_hist_matched = list(
        RetailerSalesRow.objects.filter(
            tenant=access.tenant,
            sales_import__is_active_version=True,
            match_status=RetailerSalesRow.MATCH_MATCHED,
        )
        .exclude(week_start__isnull=True)
        .order_by("week_start")
    )
    hist_by_store_sku: dict[tuple[str, int], list[RetailerSalesRow]] = {}
    for hr in all_hist_matched:
        if hr.product_id is not None and hr.store_key:
            hist_by_store_sku.setdefault((hr.store_key, hr.product_id), []).append(
                hr
            )

    # Gather included CanonicalVisits in the window.
    visits_qs = brand_included_visits_queryset(access.tenant).prefetch_related(
        "submissions", "cost_lines"
    )
    if period_start:
        visits_qs = visits_qs.filter(visit_date__gte=period_start)
    if period_end:
        visits_qs = visits_qs.filter(visit_date__lte=period_end)
    if store_key:
        visits_qs = visits_qs.filter(store_key=store_key)
    visits = list(visits_qs)

    # Map demo store-weeks -> list of CanonicalVisits (for store-week deduplication!).
    visits_by_store_week: dict[tuple[str, date], list[CanonicalVisit]] = {}
    demo_store_weeks_all: set[tuple[str, date]] = set(
        (
            v.store_key,
            monday_of_week(v.visit_date),
        )
        for v in brand_included_visits_queryset(access.tenant).only(
            "store_key", "visit_date"
        )
        if v.store_key
    )
    for v in visits:
        if not v.store_key:
            continue
        wk = monday_of_week(v.visit_date)
        visits_by_store_week.setdefault((v.store_key, wk), []).append(v)

    # Index matched POS rows by (store_key, product_id, week_start) — deduplicated!
    matched_cell_map: dict[tuple[str, int, date], RetailerSalesRow] = {}
    for r in matched_rows:
        assert r.product_id is not None and r.week_start is not None
        matched_cell_map[(r.store_key, r.product_id, r.week_start)] = r

    # Build eligible (store_key, product_id, week_start) cells from demo visits +
    # matched cells in period.
    default_products = list(
        Product.objects.filter(tenant=access.tenant).order_by("id")[:1]
    )
    eligible_cells: dict[tuple[str, int, date], dict[str, Any]] = {}
    for (s_key, wk), wk_visits in visits_by_store_week.items():
        sku_ids: set[int] = set()
        for v in wk_visits:
            for sub in v.submissions.all():
                for item in sub.sku_breakdown or []:
                    if item.get("product_id"):
                        sku_ids.add(int(item["product_id"]))
        if not sku_ids:
            # Check POS rows for that store-week first, then fallback to first brand SKU.
            pos_skus = {
                pid
                for (sk, pid, pwk) in matched_cell_map.keys()
                if sk == s_key and pwk == wk
            }
            if pos_skus:
                sku_ids = pos_skus
            elif default_products:
                sku_ids = {default_products[0].id}
        for pid in sku_ids:
            eligible_cells[(s_key, pid, wk)] = {
                "store_label": wk_visits[0].store_label,
            }

    for key, r in matched_cell_map.items():
        eligible_cells.setdefault(key, {"store_label": r.store_label})

    product_names = {
        p.id: p.name
        for p in Product.objects.filter(
            id__in={pid for (_, pid, _) in eligible_cells.keys()}
        )
    }

    # Index CrewPurchaseAdjustments by (store_key, product_id, week_start) —
    # ONLY counting adjustments whose purchase_date falls within [week_start, week_start + 6d]!
    crew_map: dict[tuple[str, int, date], int] = {}
    crew_qs = CrewPurchaseAdjustment.objects.filter(tenant=access.tenant)
    if store_key:
        crew_qs = crew_qs.filter(store_key=store_key)
    for adj in crew_qs:
        w_end_bound = adj.week_start + timedelta(days=6)
        if adj.week_start <= adj.purchase_date <= w_end_bound:
            ckey = (adj.store_key, adj.product_id, adj.week_start)
            crew_map[ckey] = crew_map.get(ckey, 0) + adj.units_purchased

    matched_eligible_count = 0
    missing_rows_detail: list[dict[str, Any]] = []

    # Group cells into store-week records so multi-demo store-weeks NEVER double-count POS!
    store_week_groups: dict[tuple[str, date], dict[str, Any]] = {}

    for (s_key, pid, wk), meta in sorted(
        eligible_cells.items(), key=lambda x: (x[0][2], x[0][0], x[0][1])
    ):
        pos_row = matched_cell_map.get((s_key, pid, wk))
        wk_visits = visits_by_store_week.get((s_key, wk), [])
        grp = store_week_groups.setdefault(
            (s_key, wk),
            {
                "store_key": s_key,
                "store_label": meta["store_label"],
                "week_start": wk.isoformat(),
                "week_end": (wk + timedelta(days=6)).isoformat(),
                "demo_count": len(wk_visits),
                "shared_store_week": len(wk_visits) > 1,
                "visit_uuids": [str(v.uuid) for v in wk_visits],
                "rep_observed_units": sum(
                    (v.rep_observed_units or 0) for v in wk_visits
                ),
                "sku_cells": [],
            },
        )

        if pos_row is None:
            any_delayed = any(
                imp.feed_completeness == RetailerSalesImport.COMPLETENESS_DELAYED
                and imp.period_start <= wk <= imp.period_end
                for imp in active_imports
            )
            missing_status = (
                "delayed_feed" if any_delayed else "missing_from_feed"
            )
            missing_rows_detail.append(
                {
                    "store_key": s_key,
                    "store_label": meta["store_label"],
                    "product_id": pid,
                    "product_name": product_names.get(pid, f"SKU #{pid}"),
                    "week_start": wk.isoformat(),
                    "status": missing_status,
                }
            )
            grp["sku_cells"].append(
                {
                    "product_id": pid,
                    "product_name": product_names.get(pid, f"SKU #{pid}"),
                    "status": missing_status,
                    "register_units": None,
                    "is_explicit_zero": False,
                    "crew_purchased_units": crew_map.get((s_key, pid, wk), 0),
                    "adjusted_register_units": None,
                    "baseline_units": None,
                    "baseline_method": None,
                    "baseline_window_label": None,
                    "baseline_sample_size": 0,
                    "observed_vs_baseline_diff": None,
                    "incremental_lift_units": None,
                    "lift_withheld_reason": (
                        "Feed delayed for this store-SKU-week."
                        if any_delayed
                        else "Store-SKU-week missing from retailer POS feed."
                    ),
                }
            )
            continue

        matched_eligible_count += 1
        orig_units = int(pos_row.register_units or 0)
        crew_u = crew_map.get((s_key, pid, wk), 0)
        adj_units = max(orig_units - crew_u, 0)

        # Resolve baseline (inline or trailing >=2 non-demo weeks).
        base_u = pos_row.baseline_units
        base_method = pos_row.baseline_method
        base_window = pos_row.baseline_window_label
        base_n = pos_row.baseline_sample_size

        if base_u is None:
            prior_non_demo = [
                hr
                for hr in hist_by_store_sku.get((s_key, pid), [])
                if hr.week_start is not None
                and hr.week_start < wk
                and (s_key, hr.week_start) not in demo_store_weeks_all
                and not hr.has_pack_change
                and not hr.has_stockout
                and hr.register_units is not None
            ][-4:]
            if len(prior_non_demo) >= 2:
                avg_val = sum(
                    Decimal(int(hr.register_units or 0))
                    for hr in prior_non_demo
                ) / Decimal(len(prior_non_demo))
                base_u = quantize_money(avg_val)
                base_method = "trailing_non_demo_avg"
                base_window = f"Trailing {len(prior_non_demo)} non-demo weeks"
                base_n = len(prior_non_demo)

        obs_diff: Decimal | None = None
        if base_u is not None:
            obs_diff = quantize_money(Decimal(adj_units) - base_u)

        # Gate incremental lift strictly on sufficient baseline & clean feed!
        lift_u: Decimal | None = None
        lift_reason: str | None = None
        imp_completeness = pos_row.sales_import.feed_completeness
        if imp_completeness != RetailerSalesImport.COMPLETENESS_COMPLETE:
            lift_reason = (
                f"Incremental lift withheld because POS feed is {imp_completeness}."
            )
        elif base_u is None or base_n < 2:
            lift_reason = (
                "Incremental lift withheld: requires at least 2 baseline comparison weeks."
            )
        elif pos_row.has_pack_change:
            lift_reason = "Incremental lift withheld due to pack-size change in period."
        elif pos_row.has_stockout:
            lift_reason = "Incremental lift withheld due to reported store stockout."
        elif pos_row.has_promotion:
            lift_reason = (
                "Incremental lift withheld due to overlapping price promotion (unattributed)."
            )
        else:
            lift_u = obs_diff

        grp["sku_cells"].append(
            {
                "product_id": pid,
                "product_name": product_names.get(pid, f"SKU #{pid}"),
                "status": (
                    "explicit_zero" if pos_row.is_explicit_zero else "matched"
                ),
                "register_units": orig_units,
                "is_explicit_zero": pos_row.is_explicit_zero,
                "crew_purchased_units": crew_u,
                "adjusted_register_units": adj_units,
                "baseline_units": str(base_u) if base_u is not None else None,
                "baseline_method": base_method or None,
                "baseline_window_label": base_window or None,
                "baseline_sample_size": base_n,
                "observed_vs_baseline_diff": (
                    str(obs_diff) if obs_diff is not None else None
                ),
                "incremental_lift_units": (
                    str(lift_u) if lift_u is not None else None
                ),
                "has_promotion": pos_row.has_promotion,
                "has_pack_change": pos_row.has_pack_change,
                "has_stockout": pos_row.has_stockout,
                "lift_withheld_reason": lift_reason,
            }
        )

    # Finalize per-store-week and overall totals (deduplicated per store-week!).
    store_weeks_out: list[dict[str, Any]] = []
    total_rep_obs = sum((v.rep_observed_units or 0) for v in visits)
    total_reg_units = 0
    total_crew_units = 0
    total_adj_units = 0
    any_matched_cells = False

    all_cells_have_obs_diff = True
    total_obs_diff = Decimal("0.00")

    all_demo_cells_have_lift = True
    total_demo_lift = Decimal("0.00")
    first_lift_withheld: str | None = None

    all_demo_costs_complete = True
    total_eligible_brand_cost = Decimal("0.00")
    demo_week_count = 0

    for (s_key, wk), grp in store_week_groups.items():
        cells = grp["sku_cells"]
        matched_c = [c for c in cells if c["register_units"] is not None]
        wk_visits = visits_by_store_week.get((s_key, wk), [])

        sw_reg = sum(c["register_units"] for c in matched_c) if matched_c else None
        sw_crew = sum(c["crew_purchased_units"] for c in cells)
        sw_adj = (
            sum(c["adjusted_register_units"] for c in matched_c)
            if matched_c
            else None
        )
        if matched_c:
            any_matched_cells = True
            total_reg_units += sw_reg or 0
            total_crew_units += sw_crew
            total_adj_units += sw_adj or 0

        if cells and all(c["observed_vs_baseline_diff"] is not None for c in cells):
            sw_diff = sum(
                (Decimal(c["observed_vs_baseline_diff"]) for c in cells),
                Decimal("0.00"),
            )
            total_obs_diff += sw_diff
        else:
            sw_diff = None
            if wk_visits:
                all_cells_have_obs_diff = False

        if cells and all(c["incremental_lift_units"] is not None for c in cells):
            sw_lift = sum(
                (Decimal(c["incremental_lift_units"]) for c in cells),
                Decimal("0.00"),
            )
        else:
            sw_lift = None
            sw_reason = next(
                (
                    c["lift_withheld_reason"]
                    for c in cells
                    if c.get("lift_withheld_reason")
                ),
                "Insufficient POS or baseline coverage.",
            )
            if wk_visits:
                all_demo_cells_have_lift = False
                first_lift_withheld = first_lift_withheld or sw_reason

        # Compute store-week eligible brand cost across the visits in this store-week.
        sw_cost: Decimal | None = Decimal("0.00")
        sw_cost_incomplete = False
        if wk_visits:
            demo_week_count += 1
            for v in wk_visits:
                csum = compute_visit_cost_summary(v, list(v.cost_lines.all()))
                if csum.is_incomplete or csum.total_cost is None:
                    sw_cost_incomplete = True
                    sw_cost = None
                    break
                assert sw_cost is not None
                sw_cost += csum.total_cost
            if sw_cost_incomplete or sw_cost is None:
                all_demo_costs_complete = False
            else:
                total_eligible_brand_cost += sw_cost
                if sw_lift is not None:
                    total_demo_lift += sw_lift
        else:
            sw_cost = None

        sw_contrib: Decimal | None = None
        sw_roi: Decimal | None = None
        sw_roi_reason: str | None = None
        if not wk_visits:
            sw_roi_reason = "No demo visit in this store-week."
        elif sw_lift is None:
            sw_roi_reason = first_lift_withheld or "Incremental lift unavailable."
        elif eff_contrib is None or eff_contrib <= Decimal("0.00"):
            sw_roi_reason = "Unit contribution margin not configured."
        elif sw_cost is None or sw_cost <= Decimal("0.00"):
            sw_roi_reason = (
                "Eligible brand demo cost is missing, incomplete, or zero."
            )
        else:
            sw_contrib = quantize_money(sw_lift * eff_contrib)
            sw_roi = (
                (sw_contrib - sw_cost) / sw_cost
            ).quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)

        grp.update(
            {
                "register_units": sw_reg,
                "crew_purchased_units": sw_crew,
                "adjusted_register_units": sw_adj,
                "observed_vs_baseline_diff": (
                    str(quantize_money(sw_diff)) if sw_diff is not None else None
                ),
                "incremental_lift_units": (
                    str(quantize_money(sw_lift)) if sw_lift is not None else None
                ),
                "eligible_brand_cost": (
                    str(quantize_money(sw_cost)) if sw_cost is not None else None
                ),
                "incremental_contribution": (
                    str(sw_contrib) if sw_contrib is not None else None
                ),
                "roi": str(sw_roi) if sw_roi is not None else None,
                "roi_withheld_reason": sw_roi_reason,
            }
        )
        store_weeks_out.append(grp)

    elig_total = len(eligible_cells)
    cov_ratio = (
        (Decimal(matched_eligible_count) / Decimal(elig_total)).quantize(
            Decimal("0.0001"), rounding=ROUND_HALF_UP
        )
        if elig_total > 0
        else None
    )

    overall_lift: Decimal | None = None
    overall_lift_reason: str | None = None
    if demo_week_count == 0:
        # If evaluating pure POS rows without demo visits in filter, summarize lift if all cells have lift
        all_cells = [
            c for sw in store_weeks_out for c in sw["sku_cells"]
        ]
        if all_cells and all(c["incremental_lift_units"] is not None for c in all_cells):
            overall_lift = sum(
                (Decimal(c["incremental_lift_units"]) for c in all_cells),
                Decimal("0.00"),
            )
        else:
            overall_lift_reason = next(
                (
                    c["lift_withheld_reason"]
                    for c in all_cells
                    if c.get("lift_withheld_reason")
                ),
                "No completed demo store-weeks with full baseline coverage.",
            )
    elif all_demo_cells_have_lift and not missing_rows_detail:
        overall_lift = quantize_money(total_demo_lift)
    else:
        overall_lift_reason = (
            first_lift_withheld
            or "Coverage is incomplete across eligible demo store-SKU-weeks."
        )

    overall_contrib: Decimal | None = None
    overall_roi: Decimal | None = None
    overall_roi_reason: str | None = None
    if overall_lift is None:
        overall_roi_reason = overall_lift_reason
    elif eff_contrib is None or eff_contrib <= Decimal("0.00"):
        overall_roi_reason = "Configure positive contribution per unit to compute incremental contribution and ROI."
    elif (
        demo_week_count == 0
        or not all_demo_costs_complete
        or total_eligible_brand_cost <= Decimal("0.00")
    ):
        overall_roi_reason = "ROI withheld until all demo visits in period have complete, non-zero supported brand cost."
    else:
        overall_contrib = quantize_money(overall_lift * eff_contrib)
        overall_roi = (
            (overall_contrib - total_eligible_brand_cost)
            / total_eligible_brand_cost
        ).quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)

    return {
        "available": True,
        "unavailable_message": None,
        "retailer_banners": sorted({imp.retailer_banner for imp in active_imports}),
        "coverage": {
            "matched_store_sku_weeks": matched_eligible_count,
            "eligible_store_sku_weeks": elig_total,
            "coverage_ratio": str(cov_ratio) if cov_ratio is not None else None,
            "missing_rows": missing_rows_detail,
            "unmatched_row_count": unmatched_count,
        },
        "totals": {
            "rep_observed_units": total_rep_obs,
            "register_units": total_reg_units if any_matched_cells else None,
            "crew_purchased_units": total_crew_units,
            "adjusted_register_units": (
                total_adj_units if any_matched_cells else None
            ),
            "observed_vs_baseline_diff": (
                str(quantize_money(total_obs_diff))
                if any_matched_cells and all_cells_have_obs_diff
                else None
            ),
            "incremental_lift_units": (
                str(overall_lift) if overall_lift is not None else None
            ),
            "incremental_contribution": (
                str(overall_contrib) if overall_contrib is not None else None
            ),
            "eligible_brand_cost": (
                str(quantize_money(total_eligible_brand_cost))
                if demo_week_count > 0 and all_demo_costs_complete
                else None
            ),
            "roi": str(overall_roi) if overall_roi is not None else None,
            "lift_withheld_reason": overall_lift_reason,
            "roi_withheld_reason": overall_roi_reason,
        },
        "store_weeks": store_weeks_out,
    }
