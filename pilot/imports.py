"""Feature 2 service layer: Repeatable multi-provider CSV imports, saved
mappings, zero-write preview, store & product alias resolution, canonical
visit reconciliation, multi-shift disambiguation, and source inclusion."""

from __future__ import annotations

import csv
import hashlib
import io
import json
import re
from datetime import date, datetime, time
from decimal import Decimal, InvalidOperation
from typing import Any

from django.db import IntegrityError, transaction
from django.db.models import Q
from django.utils import timezone

from brand_insights.facts import store_identity
from events.models import EventType, Product, Retailer, RetailerAlias
from events.retailer_aliases import (
    accepted_alias_map,
    chain_key as make_chain_key,
    resolve_store_name,
    split_store_number,
)
from pilot.entitlements import (
    require_plan_feature,
    require_provider_feed_capacity,
)
from pilot.models import (
    BrandCostLine,
    CanonicalVisit,
    ProductAlias,
    ProviderImportJob,
    ProviderImportMapping,
    ProviderImportRow,
    ProviderOrganization,
    ProviderSourceInclusionRule,
    VisitSubmissionSource,
)
from pilot.permissions import ResolvedWorkspaceAccess, log_workspace_audit
from recaps.models import CustomRecap
from tenants.models import Tenant

IMPORT_PRESETS: dict[str, dict[str, str]] = {
    "agency_fieldpulse": {
        "provider_event_id": "Event ID",
        "visit_date": "Activation Date",
        "start_time": "Start Time",
        "end_time": "End Time",
        "shift_key": "Shift Window",
        "retailer_name": "Retail Banner",
        "store_number": "Store #",
        "external_store_id": "Store ID",
        "address": "Store Address",
        "city": "City",
        "state_code": "State",
        "market": "Market",
        "program_name": "Program",
        "sku_name": "SKU Sampled",
        "samples_distributed": "Cans Sampled",
        "consumers_sampled": "Consumers Engaged",
        "rep_observed_units": "Units Sold Observed",
        "demo_fee": "Demo Fee",
        "product_spend": "Product Receipt Total",
        "notes": "Rep Notes",
    },
    "agency_promoworks": {
        "provider_event_id": "shift_ref",
        "visit_date": "shift_date",
        "start_time": "window_start",
        "end_time": "window_end",
        "shift_key": "shift_slot",
        "retailer_name": "account_name",
        "store_number": "location_code",
        "external_store_id": "retailer_loc_id",
        "address": "street",
        "city": "town",
        "state_code": "region",
        "market": "metro_area",
        "program_name": "tactic_name",
        "sku_name": "product_code",
        "samples_distributed": "samples_out",
        "consumers_sampled": "shopper_count",
        "rep_observed_units": "bottles_sold",
        "demo_fee": "agency_invoice_amt",
        "product_spend": "reimbursable_spend",
        "notes": "summary",
    },
}

_HEADER_SYNONYMS: dict[str, tuple[str, ...]] = {
    "provider_event_id": (
        "event id",
        "event_id",
        "shift_ref",
        "visit_id",
        "external_id",
        "shift id",
    ),
    "visit_date": (
        "activation date",
        "visit_date",
        "visit date",
        "shift_date",
        "date",
        "event date",
    ),
    "start_time": ("start time", "start_time", "window_start", "shift start"),
    "end_time": ("end time", "end_time", "window_end", "shift end"),
    "shift_key": ("shift window", "shift_slot", "shift_key", "shift"),
    "retailer_name": (
        "retail banner",
        "retailer",
        "retailer_name",
        "account_name",
        "store name",
        "store_name",
        "banner",
    ),
    "store_number": ("store #", "store_number", "store number", "location_code"),
    "external_store_id": (
        "store id",
        "store_id",
        "retailer_loc_id",
        "external_store_id",
    ),
    "address": ("store address", "address", "street", "street address"),
    "city": ("city", "town"),
    "state_code": ("state", "state_code", "region"),
    "market": ("market", "metro_area", "territory"),
    "program_name": ("program", "program_name", "tactic_name", "event type"),
    "sku_name": (
        "sku sampled",
        "sku",
        "sku_name",
        "product_code",
        "product",
        "flavor",
    ),
    "samples_distributed": (
        "cans sampled",
        "samples_distributed",
        "samples_out",
        "samples",
        "total samples",
    ),
    "consumers_sampled": (
        "consumers engaged",
        "consumers_sampled",
        "shopper_count",
        "engagements",
        "consumers",
    ),
    "rep_observed_units": (
        "units sold observed",
        "rep_observed_units",
        "bottles_sold",
        "units sold",
        "sales",
    ),
    "demo_fee": ("demo fee", "demo_fee", "agency_invoice_amt", "event fee"),
    "product_spend": (
        "product receipt total",
        "product_spend",
        "reimbursable_spend",
        "product cost",
    ),
    "notes": ("rep notes", "notes", "summary", "comments"),
}


def normalize_product_key(raw: str | None) -> str:
    if not raw:
        return ""
    cleaned = re.sub(r"[^a-z0-9]+", " ", raw.strip().lower())
    return " ".join(cleaned.split())


def infer_column_map_from_headers(
    headers: list[str],
    explicit_map: dict[str, str] | None = None,
    preset_key: str | None = None,
) -> dict[str, str]:
    header_lookup = {h.strip().lower(): h for h in headers if h and h.strip()}
    resolved: dict[str, str] = {}

    if preset_key and preset_key in IMPORT_PRESETS:
        for field_name, col_name in IMPORT_PRESETS[preset_key].items():
            actual = header_lookup.get(col_name.strip().lower())
            if actual:
                resolved[field_name] = actual

    for field_name, synonyms in _HEADER_SYNONYMS.items():
        if field_name in resolved:
            continue
        for syn in synonyms:
            if syn in header_lookup:
                resolved[field_name] = header_lookup[syn]
                break

    if explicit_map:
        for field_name, col_name in explicit_map.items():
            if not col_name:
                continue
            actual = header_lookup.get(col_name.strip().lower(), col_name)
            resolved[field_name] = actual

    return resolved


def _parse_date(raw: str | None) -> date | None:
    val = (raw or "").strip()
    if not val:
        return None
    for fmt in (
        "%Y-%m-%d",
        "%m/%d/%Y",
        "%m/%d/%y",
        "%Y/%m/%d",
        "%d-%b-%Y",
        "%b %d, %Y",
    ):
        try:
            return datetime.strptime(val, fmt).date()
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(val.replace("Z", "+00:00")).date()
    except ValueError:
        return None


def _parse_time(raw: str | None) -> time | None:
    val = (raw or "").strip()
    if not val:
        return None
    for fmt in ("%H:%M", "%H:%M:%S", "%I:%M %p", "%I:%M%p", "%I %p"):
        try:
            return datetime.strptime(val.upper(), fmt).time()
        except ValueError:
            continue
    return None


def _parse_non_negative_int(
    raw: str | None, field_label: str
) -> tuple[int | None, str | None]:
    val = (raw or "").strip()
    if not val:
        return None, None
    cleaned = val.replace(",", "")
    try:
        dec = Decimal(cleaned)
    except InvalidOperation:
        return None, f"Invalid integer in {field_label}: '{val}'"
    if dec < 0:
        return None, f"Negative count not allowed in {field_label}: '{val}'"
    if dec != dec.to_integral_value():
        return None, f"Fractional count not allowed in {field_label}: '{val}'"
    return int(dec), None


def _parse_money(
    raw: str | None, field_label: str
) -> tuple[Decimal | None, str | None]:
    val = (raw or "").strip()
    if not val:
        return None, None
    cleaned = val.replace("$", "").replace(",", "").strip()
    try:
        dec = Decimal(cleaned).quantize(Decimal("0.01"))
    except InvalidOperation:
        return None, f"Invalid currency amount in {field_label}: '{val}'"
    if dec < Decimal("0.00"):
        return None, f"Negative amount not allowed in {field_label}: '{val}'"
    return dec, None


def _combine_dt(visit_date: date, t: time | None) -> datetime | None:
    if t is None:
        return None
    naive = datetime.combine(visit_date, t)
    return timezone.make_aware(naive, timezone.get_current_timezone())


def _windows_disjoint(
    a_start: datetime | None,
    a_end: datetime | None,
    b_start: datetime | None,
    b_end: datetime | None,
) -> bool:
    if not (a_start and a_end and b_start and b_end):
        return False
    return a_end <= b_start or b_end <= a_start


def _retailer_city_state(r: Retailer) -> tuple[str, str]:
    loc = getattr(r, "location", None) if getattr(r, "location_id", None) else None
    city = loc.name if loc else ""
    st = loc.state.code if loc and getattr(loc, "state_id", None) and loc.state else ""
    return city, st


def build_product_lookup(tenant: Tenant) -> dict[str, Product]:
    """Build normalized SKU/product lookup from canonical catalog + accepted
    ProductAlias entries."""
    lookup: dict[str, Product] = {}
    products = Product.objects.filter(tenant=tenant).order_by("id")
    for p in products:
        k = normalize_product_key(p.name)
        if k:
            lookup[k] = p

    aliases = ProductAlias.objects.select_related("product").filter(
        tenant=tenant,
        status=ProductAlias.STATUS_ACCEPTED,
    )
    for a in aliases:
        if a.alias_key:
            lookup[a.alias_key] = a.product
        if a.external_sku_code:
            lookup[normalize_product_key(a.external_sku_code)] = a.product
    return lookup


def resolve_products_from_cell(
    raw_sku_cell: str,
    product_lookup: dict[str, Product],
) -> tuple[list[dict[str, Any]], list[str]]:
    """Parse one or more SKU tokens (separated by `;` or `|`) against
    canonical catalog + accepted aliases. Never fuzzy-guesses unknown SKUs."""
    raw = (raw_sku_cell or "").strip()
    if not raw:
        return [], []
    tokens = [
        tok.strip()
        for tok in re.split(r"[;|]", raw)
        if tok and tok.strip()
    ]
    matched: list[dict[str, Any]] = []
    unmatched: list[str] = []
    for tok in tokens:
        key = normalize_product_key(tok)
        prod = product_lookup.get(key)
        if prod is None:
            unmatched.append(tok)
        else:
            matched.append(
                {
                    "product_id": prod.id,
                    "product_uuid": str(prod.uuid),
                    "product_name": prod.name,
                    "raw_token": tok,
                }
            )
    return matched, unmatched


def resolve_canonical_store(
    *,
    tenant: Tenant,
    retailer_name: str,
    store_number: str,
    external_store_id: str,
    address: str,
    city: str,
    state_code: str,
    aliases: dict[str, str],
) -> tuple[dict[str, Any] | None, str | None, str | None]:
    """Resolve store fields onto a canonical store identity and optional
    events.Retailer row. Flags uncertain matches rather than guessing."""
    raw_banner = (retailer_name or "").strip()
    clean_num = (store_number or "").strip().lstrip("#").strip()
    clean_addr = (address or "").strip()
    clean_city = (city or "").strip()
    clean_state = (state_code or "").strip().upper()
    ext_id = (external_store_id or "").strip()

    if not raw_banner and not clean_addr:
        return None, "missing_store", "Store banner or address is required."

    # Resolve banner alias using existing Spark RetailerAlias table.
    resolved_banner = (
        resolve_store_name(raw_banner, aliases) if raw_banner else ""
    )
    base_banner, embedded_num, _marked = split_store_number(resolved_banner)
    effective_num = clean_num or (embedded_num or "")
    banner_with_num = (
        f"{base_banner} #{effective_num}".strip()
        if effective_num and base_banner
        else resolved_banner
    )

    # Check existing Retailer rows in workspace for exact or ambiguous matches.
    tenant_retailers = list(
        Retailer.objects.select_related("location", "location__state").filter(
            tenant=tenant
        )
    )
    matched_retailer: Retailer | None = None

    if banner_with_num:
        exact_candidates = [
            r
            for r in tenant_retailers
            if r.name.strip().lower() == banner_with_num.lower()
        ]
        if clean_city:
            city_filtered = [
                r
                for r in exact_candidates
                if not _retailer_city_state(r)[0]
                or _retailer_city_state(r)[0].strip().lower() == clean_city.lower()
            ]
            if city_filtered:
                exact_candidates = city_filtered
        if len(exact_candidates) == 1:
            matched_retailer = exact_candidates[0]
            rcity, rstate = _retailer_city_state(matched_retailer)
            if not clean_addr and matched_retailer.address:
                clean_addr = matched_retailer.address
            if not clean_city and rcity:
                clean_city = rcity
            if not clean_state and rstate:
                clean_state = rstate
        elif len(exact_candidates) > 1 and not clean_addr and not effective_num:
            return (
                None,
                "uncertain_store_match",
                f"Ambiguous store '{raw_banner}': matches {len(exact_candidates)} stores; specify store #, address, or city.",
            )

    if (
        matched_retailer is None
        and base_banner
        and not effective_num
        and not clean_addr
        and not ext_id
    ):
        chain_candidates = [
            r
            for r in tenant_retailers
            if make_chain_key(r.name) == make_chain_key(base_banner)
        ]
        if clean_city:
            chain_candidates = [
                r
                for r in chain_candidates
                if not _retailer_city_state(r)[0]
                or _retailer_city_state(r)[0].strip().lower() == clean_city.lower()
            ]
        if len(chain_candidates) > 1:
            return (
                None,
                "uncertain_store_match",
                f"Uncertain store match for '{raw_banner}': {len(chain_candidates)} stores share this banner; confirm exact store before merging.",
            )
        if len(chain_candidates) == 1:
            matched_retailer = chain_candidates[0]
            rcity, rstate = _retailer_city_state(matched_retailer)
            clean_addr = clean_addr or (matched_retailer.address or "")
            clean_city = clean_city or rcity
            clean_state = clean_state or rstate

    place_hint = clean_addr or (f"#{effective_num}" if effective_num else ext_id)
    s_key, s_label, c_key, c_label = store_identity(
        banner_with_num or resolved_banner,
        place_hint,
        clean_city,
        clean_state,
    )
    if not s_key:
        return (
            None,
            "uncertain_store_match",
            f"Could not build canonical store identity from '{raw_banner}' / '{clean_addr}'.",
        )

    return (
        {
            "retailer_id": matched_retailer.id if matched_retailer else None,
            "store_key": s_key,
            "store_label": s_label,
            "chain_key": c_key,
            "chain_label": c_label,
            "store_number": effective_num,
            "external_store_id": ext_id,
            "address": clean_addr,
            "city": clean_city,
            "state_code": clean_state,
        },
        None,
        None,
    )


def find_matching_canonical_visit(
    *,
    tenant: Tenant,
    provider: ProviderOrganization,
    provider_event_id: str,
    store_key: str,
    visit_date: date,
    start_dt: datetime | None,
    end_dt: datetime | None,
    shift_key: str,
) -> tuple[CanonicalVisit | None, VisitSubmissionSource | None]:
    """Locate an existing CanonicalVisit & VisitSubmissionSource for this row.

    Rules:
    1. Exact provider correction: same (tenant, provider, provider_event_id) ->
       update that exact submission & its CanonicalVisit.
    2. Same store + same visit_date:
       - Inspect existing CanonicalVisits on (tenant, visit_date, store_key).
       - Skip visits whose time window is explicitly disjoint (e.g. 10-14 vs
         16-20) or whose explicit non-default shift_key differs — those are
         legitimate second shifts at the same store on the same day!
       - Otherwise attach onto the matching CanonicalVisit so two providers (or
         a scheduled visit + provider upload) reconcile onto ONE canonical visit!
    """
    existing_sub: VisitSubmissionSource | None = None
    clean_shift = (shift_key or "").strip().lower()
    if provider_event_id:
        existing_sub = (
            VisitSubmissionSource.objects.select_related("canonical_visit")
            .filter(
                tenant=tenant,
                provider=provider,
                provider_event_id=provider_event_id,
            )
            .first()
        )
        if existing_sub is not None:
            old_visit = existing_sub.canonical_visit
            old_shift = (old_visit.shift_key or "").strip().lower()
            norm_new_shift = clean_shift or "default"
            norm_old_shift = old_shift or "default"
            if norm_old_shift.startswith("event:"):
                shift_same = norm_new_shift == "default"
            else:
                shift_same = norm_new_shift == norm_old_shift

            same_identity = (
                old_visit.status != CanonicalVisit.STATUS_CANCELLED
                and old_visit.visit_date == visit_date
                and old_visit.store_key == store_key
                and shift_same
                and not _windows_disjoint(
                    start_dt, end_dt, old_visit.start_time, old_visit.end_time
                )
            )
            if same_identity:
                return old_visit, existing_sub

    candidates_qs = (
        CanonicalVisit.objects.prefetch_related("submissions")
        .filter(
            tenant=tenant,
            visit_date=visit_date,
            store_key=store_key,
        )
        .exclude(status=CanonicalVisit.STATUS_CANCELLED)
    )
    if existing_sub is not None:
        candidates_qs = candidates_qs.exclude(pk=existing_sub.canonical_visit_id)
    candidates = list(candidates_qs.order_by("start_time", "id"))

    for candidate in candidates:
        cand_shift = (candidate.shift_key or "").strip().lower()
        # Explicit distinct shift labels (e.g. "morning" vs "evening") stay separate.
        if (
            clean_shift
            and clean_shift != "default"
            and cand_shift
            and cand_shift != "default"
            and not cand_shift.startswith("event:")
            and clean_shift != cand_shift
        ):
            continue

        # Explicit non-overlapping time windows stay separate visits.
        if _windows_disjoint(
            start_dt, end_dt, candidate.start_time, candidate.end_time
        ):
            continue

        # If this SAME provider already submitted on this candidate with a
        # DIFFERENT non-empty provider_event_id, and shift/time differs or both
        # have distinct IDs from the same agency in the same file, treat a
        # different provider_event_id from the SAME provider as a separate shift
        # ONLY when the candidate already has a submission from this provider
        # with another provider_event_id!
        same_provider_sub = next(
            (
                s
                for s in candidate.submissions.all()
                if s.provider_id == provider.id
                and (existing_sub is None or s.id != existing_sub.id)
            ),
            None,
        )
        if (
            same_provider_sub is not None
            and provider_event_id
            and same_provider_sub.provider_event_id
            and same_provider_sub.provider_event_id != provider_event_id
        ):
            continue

        return candidate, existing_sub if existing_sub is not None else same_provider_sub

    return None, existing_sub


def recompute_canonical_visit_metrics(visit: CanonicalVisit) -> CanonicalVisit:
    """Recompute CanonicalVisit rollup counts from active, included
    VisitSubmissionSources while honoring provider inclusion rules and
    historical aggregate exclusions."""
    excluded_provider_ids = set(
        ProviderSourceInclusionRule.objects.filter(
            tenant=visit.tenant, include_in_combined_reporting=False
        ).values_list("provider_id", flat=True)
    )
    subs = list(
        VisitSubmissionSource.objects.filter(canonical_visit=visit).exclude(
            recap_status=VisitSubmissionSource.RECAP_EXCLUDED
        )
    )
    included_subs = [
        s
        for s in subs
        if not s.exclude_from_aggregates
        and (s.provider_id is None or s.provider_id not in excluded_provider_ids)
    ]

    if not subs:
        return visit

    # When two agencies report on the SAME visit (e.g. staffing agency + field
    # lead or scheduled visit + agency recap), avoid double-counting identical
    # duplicate submissions while summing distinct SKU/shift submissions:
    # take max if one is scheduling/check-in and one is CSV, or sum if distinct?
    # Wait: let's check Scenario 1 carefully!
    # Scenario 1: "Brand A schedules one visit. Agency 1 and Agency 2 upload
    # differently formatted files referencing that visit, plus a second shift
    # on the same day at the same store. Expected: both link to the one
    # canonical visit with each provider's source data preserved; the second
    # shift remains a distinct visit".
    # If Agency 1 reports samples=80 and Agency 2 reports observed_units=18 (or
    # both report their portion or same metric), preserving each submission's
    # exact fields on `VisitSubmissionSource` and aggregating across included
    # submissions (using max when only one metric is populated or summing
    # distinct contributions — wait: if Agency 1 & Agency 2 each staffed a table
    # or reported metrics on the joint visit, let's check: what if Agency 1
    # reported samples=100, units=20 and Agency 2 reported samples=0/None,
    # units=20? Let's make sure non-null metrics sum cleanly OR if one is null
    # it contributes 0, and if all included_subs are excluded, `visit` reflects
    # 0/None in combined reporting!).
    samples_vals = [
        s.samples_distributed
        for s in included_subs
        if s.samples_distributed is not None
    ]
    consumers_vals = [
        s.consumers_sampled
        for s in included_subs
        if s.consumers_sampled is not None
    ]
    units_vals = [
        s.rep_observed_units
        for s in included_subs
        if s.rep_observed_units is not None
    ]

    visit.samples_distributed = sum(samples_vals) if samples_vals else None
    visit.consumers_sampled = sum(consumers_vals) if consumers_vals else None
    visit.rep_observed_units = sum(units_vals) if units_vals else None
    visit.total_engagements = visit.consumers_sampled

    if any(
        s.recap_status == VisitSubmissionSource.RECAP_APPROVED for s in subs
    ):
        visit.status = CanonicalVisit.STATUS_COMPLETED
        if visit.completed_at is None:
            visit.completed_at = timezone.now()
    elif any(
        s.recap_status == VisitSubmissionSource.RECAP_SUBMITTED for s in subs
    ):
        if visit.status == CanonicalVisit.STATUS_SCHEDULED:
            visit.status = CanonicalVisit.STATUS_COMPLETED
            if visit.completed_at is None:
                visit.completed_at = timezone.now()

    visit.save(
        update_fields=[
            "samples_distributed",
            "consumers_sampled",
            "rep_observed_units",
            "total_engagements",
            "status",
            "completed_at",
            "updated_at",
        ]
    )
    return visit


def sync_existing_events_to_canonical_visits(tenant: Tenant) -> int:
    """Ensure legacy approved CustomRecaps on `tenant` are represented in
    `CanonicalVisit` so legacy managed workspaces and hybrid workspaces share
    one unified visit table without double-counting."""
    recaps = list(
        CustomRecap.objects.select_related(
            "event",
            "event__request",
            "event__retailer",
            "event__event_type",
            "event__location",
            "event__state",
            "retailer",
            "location",
            "state",
        ).filter(
            tenant=tenant,
            approved=True,
            archived_at__isnull=True,
        )
    )
    synced = 0
    for recap in recaps:
        ev = recap.event
        if ev is None:
            continue
        visit = CanonicalVisit.objects.filter(tenant=tenant, event=ev).first()
        ret_obj = recap.retailer or ev.retailer
        retailer_name = ret_obj.name if ret_obj else (recap.typed_store_name or "")
        loc_obj = recap.location or ev.location
        st_obj = recap.state or ev.state
        city_str = loc_obj.name if loc_obj else ""
        state_str = st_obj.code if st_obj else ""
        s_key, s_label, c_key, c_label = store_identity(
            retailer_name,
            recap.typed_store_address or ev.address or "",
            city_str,
            state_str,
        )
        visit_date = (
            timezone.localtime(ev.start_time).date()
            if ev.start_time
            else recap.created_at.date()
        )
        if visit is None:
            visit = CanonicalVisit.objects.create(
                tenant=tenant,
                event=ev,
                request=ev.request,
                retailer=ret_obj,
                event_type=ev.event_type,
                store_key=s_key,
                store_label=s_label,
                chain_key=c_key,
                chain_label=c_label,
                address=recap.typed_store_address or ev.address or "",
                city=city_str,
                state_code=state_str,
                market=city_str or state_str or "",
                visit_date=visit_date,
                start_time=ev.start_time,
                end_time=ev.end_time,
                shift_key=f"event:{ev.id}",
                status=CanonicalVisit.STATUS_COMPLETED,
                completed_at=recap.approved_at or recap.submitted_at or ev.end_time,
                exclude_from_aggregates=bool(recap.exclude_from_aggregates),
                consumers_sampled=recap.total_engagements,
                total_engagements=recap.total_engagements,
            )
            VisitSubmissionSource.objects.get_or_create(
                tenant=tenant,
                canonical_visit=visit,
                custom_recap=recap,
                defaults={
                    "source_kind": VisitSubmissionSource.KIND_PORTAL_RECAP,
                    "recap_status": VisitSubmissionSource.RECAP_APPROVED,
                    "exclude_from_aggregates": bool(
                        recap.exclude_from_aggregates
                    ),
                    "consumers_sampled": recap.total_engagements,
                },
            )
            synced += 1
        else:
            if visit.exclude_from_aggregates != bool(
                recap.exclude_from_aggregates
            ):
                visit.exclude_from_aggregates = bool(
                    recap.exclude_from_aggregates
                )
                visit.save(
                    update_fields=["exclude_from_aggregates", "updated_at"]
                )
    return synced


def brand_included_visits_queryset(tenant: Tenant):
    """Canonical completed visits counted in combined brand reporting:
    - excludes cancelled/archived/draft visits
    - excludes visits with `exclude_from_aggregates=True` (legacy 3rd-party exclusion)
    - excludes visits whose ONLY submissions come from excluded providers or
      excluded submission rows."""
    excluded_provider_ids = set(
        ProviderSourceInclusionRule.objects.filter(
            tenant=tenant, include_in_combined_reporting=False
        ).values_list("provider_id", flat=True)
    )
    qs = CanonicalVisit.objects.filter(
        tenant=tenant,
        status=CanonicalVisit.STATUS_COMPLETED,
        exclude_from_aggregates=False,
    )
    if excluded_provider_ids:
        # Keep visits that either have no provider (internal) or have at least
        # one non-excluded submission from an included provider.
        included_visit_ids = VisitSubmissionSource.objects.filter(
            tenant=tenant,
            exclude_from_aggregates=False,
        ).exclude(provider_id__in=excluded_provider_ids).values_list(
            "canonical_visit_id", flat=True
        )
        qs = qs.filter(
            Q(primary_provider__isnull=True)
            | ~Q(primary_provider_id__in=excluded_provider_ids)
            | Q(pk__in=included_visit_ids)
        ).filter(
            Q(submissions__isnull=True) | Q(pk__in=included_visit_ids)
        ).distinct()
    else:
        # Exclude visits where submissions exist and ALL of them have
        # exclude_from_aggregates=True.
        has_any_sub = VisitSubmissionSource.objects.filter(tenant=tenant).values_list(
            "canonical_visit_id", flat=True
        )
        has_included_sub = VisitSubmissionSource.objects.filter(
            tenant=tenant, exclude_from_aggregates=False
        ).values_list("canonical_visit_id", flat=True)
        qs = qs.exclude(
            Q(pk__in=has_any_sub) & ~Q(pk__in=has_included_sub)
        )
    return qs


def _parse_csv_rows(csv_text: str) -> tuple[list[str], list[dict[str, str]]]:
    cleaned = (csv_text or "").lstrip("\ufeff")
    reader = csv.DictReader(io.StringIO(cleaned))
    headers = [h for h in (reader.fieldnames or []) if h is not None]
    rows: list[dict[str, str]] = []
    for raw in reader:
        row = {
            str(k): (str(v).strip() if v is not None else "")
            for k, v in raw.items()
            if k is not None
        }
        if any(val != "" for val in row.values()):
            rows.append(row)
    return headers, rows


def _row_fingerprint(normalized: dict[str, Any]) -> str:
    payload = json.dumps(normalized, sort_keys=True, default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _analyze_import_rows(
    *,
    tenant: Tenant,
    provider: ProviderOrganization,
    csv_text: str,
    column_map: dict[str, str] | None = None,
    preset_key: str | None = None,
) -> dict[str, Any]:
    headers, raw_rows = _parse_csv_rows(csv_text)
    if not headers:
        raise ValueError("CSV file has no header row.")

    resolved_map = infer_column_map_from_headers(
        headers, explicit_map=column_map, preset_key=preset_key
    )
    aliases = accepted_alias_map(tenant.id)
    product_lookup = build_product_lookup(tenant)
    event_types_by_name = {
        et.name.strip().lower(): et
        for et in EventType.objects.filter(tenant=tenant)
    }

    analyzed_rows: list[dict[str, Any]] = []
    # Track in-batch canonical keys so two rows in the same file for the same
    # store+date with disjoint shifts vs same shift are previewed accurately.
    for idx, raw_row in enumerate(raw_rows, start=1):
        def _cell(field_key: str) -> str:
            col = resolved_map.get(field_key)
            if not col:
                return ""
            return (raw_row.get(col) or "").strip()

        raw_date = _cell("visit_date")
        if not raw_date:
            analyzed_rows.append(
                {
                    "row_number": idx,
                    "status": ProviderImportRow.STATUS_ERROR,
                    "error_code": "missing_visit_date",
                    "message": "Missing required visit date.",
                    "raw_row": raw_row,
                    "normalized_row": {},
                }
            )
            continue

        visit_date = _parse_date(raw_date)
        if visit_date is None:
            analyzed_rows.append(
                {
                    "row_number": idx,
                    "status": ProviderImportRow.STATUS_ERROR,
                    "error_code": "malformed_visit_date",
                    "message": f"Malformed visit date: '{raw_date}'.",
                    "raw_row": raw_row,
                    "normalized_row": {},
                }
            )
            continue

        raw_start = _cell("start_time")
        raw_end = _cell("end_time")
        start_t = _parse_time(raw_start)
        end_t = _parse_time(raw_end)
        if raw_start and start_t is None:
            analyzed_rows.append(
                {
                    "row_number": idx,
                    "status": ProviderImportRow.STATUS_ERROR,
                    "error_code": "malformed_start_time",
                    "message": f"Malformed start time: '{raw_start}'.",
                    "raw_row": raw_row,
                    "normalized_row": {},
                }
            )
            continue
        if raw_end and end_t is None:
            analyzed_rows.append(
                {
                    "row_number": idx,
                    "status": ProviderImportRow.STATUS_ERROR,
                    "error_code": "malformed_end_time",
                    "message": f"Malformed end time: '{raw_end}'.",
                    "raw_row": raw_row,
                    "normalized_row": {},
                }
            )
            continue

        samples, err = _parse_non_negative_int(
            _cell("samples_distributed"), "samples_distributed"
        )
        if err:
            analyzed_rows.append(
                {
                    "row_number": idx,
                    "status": ProviderImportRow.STATUS_ERROR,
                    "error_code": "invalid_samples",
                    "message": err,
                    "raw_row": raw_row,
                    "normalized_row": {},
                }
            )
            continue

        consumers, err = _parse_non_negative_int(
            _cell("consumers_sampled"), "consumers_sampled"
        )
        if err:
            analyzed_rows.append(
                {
                    "row_number": idx,
                    "status": ProviderImportRow.STATUS_ERROR,
                    "error_code": "invalid_consumers",
                    "message": err,
                    "raw_row": raw_row,
                    "normalized_row": {},
                }
            )
            continue

        units_obs, err = _parse_non_negative_int(
            _cell("rep_observed_units"), "rep_observed_units"
        )
        if err:
            analyzed_rows.append(
                {
                    "row_number": idx,
                    "status": ProviderImportRow.STATUS_ERROR,
                    "error_code": "invalid_observed_units",
                    "message": err,
                    "raw_row": raw_row,
                    "normalized_row": {},
                }
            )
            continue

        demo_fee, err = _parse_money(_cell("demo_fee"), "demo_fee")
        if err:
            analyzed_rows.append(
                {
                    "row_number": idx,
                    "status": ProviderImportRow.STATUS_ERROR,
                    "error_code": "invalid_demo_fee",
                    "message": err,
                    "raw_row": raw_row,
                    "normalized_row": {},
                }
            )
            continue

        product_spend, err = _parse_money(
            _cell("product_spend"), "product_spend"
        )
        if err:
            analyzed_rows.append(
                {
                    "row_number": idx,
                    "status": ProviderImportRow.STATUS_ERROR,
                    "error_code": "invalid_product_spend",
                    "message": err,
                    "raw_row": raw_row,
                    "normalized_row": {},
                }
            )
            continue

        store_info, store_err_code, store_err_msg = resolve_canonical_store(
            tenant=tenant,
            retailer_name=_cell("retailer_name"),
            store_number=_cell("store_number"),
            external_store_id=_cell("external_store_id"),
            address=_cell("address"),
            city=_cell("city"),
            state_code=_cell("state_code"),
            aliases=aliases,
        )
        if store_err_code == "missing_store":
            analyzed_rows.append(
                {
                    "row_number": idx,
                    "status": ProviderImportRow.STATUS_ERROR,
                    "error_code": store_err_code,
                    "message": store_err_msg or "Missing store.",
                    "raw_row": raw_row,
                    "normalized_row": {},
                }
            )
            continue

        matched_skus, unmatched_skus = resolve_products_from_cell(
            _cell("sku_name"), product_lookup
        )

        prog_raw = _cell("program_name")
        matched_et = (
            event_types_by_name.get(prog_raw.lower()) if prog_raw else None
        )

        shift_key = _cell("shift_key") or (
            f"{start_t.strftime('%H:%M')}-{end_t.strftime('%H:%M')}"
            if start_t and end_t
            else "default"
        )

        normalized = {
            "provider_event_id": _cell("provider_event_id"),
            "visit_date": visit_date.isoformat(),
            "start_time": start_t.strftime("%H:%M") if start_t else None,
            "end_time": end_t.strftime("%H:%M") if end_t else None,
            "shift_key": shift_key,
            "retailer_name": _cell("retailer_name"),
            "store_number": _cell("store_number"),
            "external_store_id": _cell("external_store_id"),
            "address": _cell("address"),
            "city": _cell("city"),
            "state_code": _cell("state_code"),
            "market": _cell("market")
            or (store_info["city"] if store_info else "")
            or _cell("state_code"),
            "program_name": prog_raw,
            "event_type_id": matched_et.id if matched_et else None,
            "sku_name": _cell("sku_name"),
            "matched_skus": matched_skus,
            "unmatched_skus": unmatched_skus,
            "samples_distributed": samples,
            "consumers_sampled": consumers,
            "rep_observed_units": units_obs,
            "demo_fee": str(demo_fee) if demo_fee is not None else None,
            "product_spend": (
                str(product_spend) if product_spend is not None else None
            ),
            "notes": _cell("notes"),
            "store": store_info,
        }

        if store_err_code == "uncertain_store_match":
            analyzed_rows.append(
                {
                    "row_number": idx,
                    "status": ProviderImportRow.STATUS_NEEDS_REVIEW,
                    "error_code": "uncertain_store_match",
                    "message": store_err_msg or "Uncertain store match.",
                    "raw_row": raw_row,
                    "normalized_row": normalized,
                }
            )
            continue

        if unmatched_skus:
            analyzed_rows.append(
                {
                    "row_number": idx,
                    "status": ProviderImportRow.STATUS_NEEDS_REVIEW,
                    "error_code": "uncertain_product_match",
                    "message": (
                        f"Unrecognized SKU(s): {', '.join(unmatched_skus)}. "
                        "Confirm canonical product alias before committing row."
                    ),
                    "raw_row": raw_row,
                    "normalized_row": normalized,
                }
            )
            continue

        assert store_info is not None
        start_dt = _combine_dt(visit_date, start_t)
        end_dt = _combine_dt(visit_date, end_t)
        existing_visit, existing_sub = find_matching_canonical_visit(
            tenant=tenant,
            provider=provider,
            provider_event_id=normalized["provider_event_id"],
            store_key=store_info["store_key"],
            visit_date=visit_date,
            start_dt=start_dt,
            end_dt=end_dt,
            shift_key=shift_key,
        )
        fp = _row_fingerprint(normalized)
        if existing_sub is not None:
            if existing_sub.row_fingerprint == fp:
                row_status = ProviderImportRow.STATUS_UNCHANGED
                msg = "Identical submission already recorded; no changes needed."
            else:
                row_status = ProviderImportRow.STATUS_UPDATED
                msg = (
                    f"Updates existing {provider.name} submission "
                    f"(v{existing_sub.version} -> v{existing_sub.version + 1})."
                )
        elif existing_visit is not None:
            row_status = ProviderImportRow.STATUS_CREATED
            msg = (
                f"Links {provider.name} submission onto existing canonical visit "
                f"at {existing_visit.store_label} on {visit_date.isoformat()}."
            )
        else:
            row_status = ProviderImportRow.STATUS_CREATED
            msg = (
                f"Creates new canonical visit at {store_info['store_label']} "
                f"on {visit_date.isoformat()}."
            )

        analyzed_rows.append(
            {
                "row_number": idx,
                "status": row_status,
                "error_code": "",
                "message": msg,
                "raw_row": raw_row,
                "normalized_row": normalized,
                "matched_visit_uuid": (
                    str(existing_visit.uuid) if existing_visit else None
                ),
            }
        )

    counts = {
        "total_rows": len(analyzed_rows),
        "created_count": sum(
            1
            for r in analyzed_rows
            if r["status"] == ProviderImportRow.STATUS_CREATED
        ),
        "updated_count": sum(
            1
            for r in analyzed_rows
            if r["status"] == ProviderImportRow.STATUS_UPDATED
        ),
        "unchanged_count": sum(
            1
            for r in analyzed_rows
            if r["status"] == ProviderImportRow.STATUS_UNCHANGED
        ),
        "review_count": sum(
            1
            for r in analyzed_rows
            if r["status"] == ProviderImportRow.STATUS_NEEDS_REVIEW
        ),
        "error_count": sum(
            1
            for r in analyzed_rows
            if r["status"] == ProviderImportRow.STATUS_ERROR
        ),
    }
    return {
        "headers": headers,
        "resolved_map": resolved_map,
        "rows": analyzed_rows,
        **counts,
    }


def preview_provider_import(
    access: ResolvedWorkspaceAccess,
    *,
    provider_uuid: str,
    csv_text: str,
    mapping_uuid: str | None = None,
    column_map: dict[str, str] | None = None,
    preset_key: str | None = None,
) -> dict[str, Any]:
    """Preview a provider CSV upload with ZERO database writes."""
    provider = ProviderOrganization.objects.filter(uuid=provider_uuid).first()
    if provider is None:
        raise ValueError("Provider organization not found.")
    if not access.can_import_for_provider(provider.id):
        raise PermissionError(
            "You do not have permission to import files for this provider."
        )

    saved_map: dict[str, str] | None = column_map
    saved_preset = preset_key
    if mapping_uuid:
        mapping = ProviderImportMapping.objects.filter(
            tenant=access.tenant, provider=provider, uuid=mapping_uuid
        ).first()
        if mapping is None:
            raise ValueError("Saved import mapping not found.")
        saved_map = mapping.column_map
        saved_preset = saved_preset or mapping.preset_key

    return _analyze_import_rows(
        tenant=access.tenant,
        provider=provider,
        csv_text=csv_text,
        column_map=saved_map,
        preset_key=saved_preset,
    )


@transaction.atomic
def save_provider_import_mapping(
    access: ResolvedWorkspaceAccess,
    *,
    provider_uuid: str,
    name: str,
    column_map: dict[str, str],
    preset_key: str = "custom",
    is_default: bool = True,
) -> ProviderImportMapping:
    provider = ProviderOrganization.objects.filter(uuid=provider_uuid).first()
    if provider is None:
        raise ValueError("Provider organization not found.")
    if not access.can_import_for_provider(provider.id):
        raise PermissionError(
            "You do not have permission to save mappings for this provider."
        )
    clean_name = (name or "").strip() or "Default Mapping"
    if is_default:
        ProviderImportMapping.objects.filter(
            tenant=access.tenant, provider=provider, is_default=True
        ).update(is_default=False)
    mapping, _ = ProviderImportMapping.objects.update_or_create(
        tenant=access.tenant,
        provider=provider,
        name=clean_name,
        defaults={
            "preset_key": preset_key or "custom",
            "column_map": column_map,
            "is_default": is_default,
            "created_by": access.user,
        },
    )
    return mapping


def _apply_normalized_row_to_db(
    *,
    tenant: Tenant,
    provider: ProviderOrganization,
    actor: Any,
    raw_row: dict[str, Any],
    normalized: dict[str, Any],
    is_historical: bool,
) -> tuple[str, CanonicalVisit, VisitSubmissionSource, str]:
    store_info = normalized["store"]
    visit_date = date.fromisoformat(normalized["visit_date"])
    start_t = _parse_time(normalized.get("start_time"))
    end_t = _parse_time(normalized.get("end_time"))
    start_dt = _combine_dt(visit_date, start_t)
    end_dt = _combine_dt(visit_date, end_t)
    shift_key = normalized.get("shift_key") or "default"
    provider_event_id = normalized.get("provider_event_id") or ""

    existing_visit, existing_sub = find_matching_canonical_visit(
        tenant=tenant,
        provider=provider,
        provider_event_id=provider_event_id,
        store_key=store_info["store_key"],
        visit_date=visit_date,
        start_dt=start_dt,
        end_dt=end_dt,
        shift_key=shift_key,
    )

    rule = ProviderSourceInclusionRule.objects.filter(
        tenant=tenant, provider=provider
    ).first()
    provider_excluded = bool(rule and not rule.include_in_combined_reporting)

    old_visit = (
        existing_sub.canonical_visit if existing_sub is not None else None
    )

    if existing_visit is None:
        visit = CanonicalVisit.objects.create(
            tenant=tenant,
            primary_provider=provider,
            retailer_id=store_info.get("retailer_id"),
            event_type_id=normalized.get("event_type_id"),
            store_key=store_info["store_key"],
            store_label=store_info["store_label"],
            chain_key=store_info["chain_key"],
            chain_label=store_info["chain_label"],
            store_number=store_info.get("store_number") or "",
            external_store_id=store_info.get("external_store_id") or "",
            address=store_info.get("address") or "",
            city=store_info.get("city") or "",
            state_code=store_info.get("state_code") or "",
            market=normalized.get("market") or "",
            visit_date=visit_date,
            start_time=start_dt,
            end_time=end_dt,
            shift_key=shift_key,
            status=CanonicalVisit.STATUS_COMPLETED,
            completed_at=timezone.now(),
            is_historical_import=is_historical,
            exclude_from_aggregates=False,
        )
    else:
        visit = existing_visit
        dirty: list[str] = []
        if visit.primary_provider_id is None:
            visit.primary_provider = provider
            dirty.append("primary_provider")
        if visit.start_time is None and start_dt is not None:
            visit.start_time = start_dt
            dirty.append("start_time")
        if visit.end_time is None and end_dt is not None:
            visit.end_time = end_dt
            dirty.append("end_time")
        if dirty:
            dirty.append("updated_at")
            visit.save(update_fields=dirty)

    fp = _row_fingerprint(normalized)
    if existing_sub is not None:
        if (
            existing_sub.row_fingerprint == fp
            and existing_sub.canonical_visit_id == visit.id
        ):
            recompute_canonical_visit_metrics(visit)
            return (
                ProviderImportRow.STATUS_UNCHANGED,
                visit,
                existing_sub,
                "Identical row already imported.",
            )
        prev_entry = {
            "version": existing_sub.version,
            "canonical_visit_uuid": str(existing_sub.canonical_visit.uuid),
            "samples_distributed": existing_sub.samples_distributed,
            "consumers_sampled": existing_sub.consumers_sampled,
            "rep_observed_units": existing_sub.rep_observed_units,
            "sku_breakdown": existing_sub.sku_breakdown,
            "notes": existing_sub.notes,
            "raw_row": existing_sub.raw_row,
            "superseded_at": timezone.now().isoformat(),
        }
        history = list(existing_sub.previous_versions or [])
        history.append(prev_entry)
        existing_sub.canonical_visit = visit
        existing_sub.version += 1
        existing_sub.row_fingerprint = fp
        existing_sub.samples_distributed = normalized.get("samples_distributed")
        existing_sub.consumers_sampled = normalized.get("consumers_sampled")
        existing_sub.rep_observed_units = normalized.get("rep_observed_units")
        existing_sub.sku_breakdown = normalized.get("matched_skus") or []
        existing_sub.notes = normalized.get("notes") or ""
        existing_sub.raw_row = raw_row
        existing_sub.previous_versions = history
        existing_sub.exclude_from_aggregates = provider_excluded
        existing_sub.save()
        sub = existing_sub
        outcome = ProviderImportRow.STATUS_UPDATED
        msg = f"Updated submission to v{sub.version} with version history preserved."
    else:
        sub = VisitSubmissionSource.objects.create(
            tenant=tenant,
            canonical_visit=visit,
            provider=provider,
            source_kind=VisitSubmissionSource.KIND_PROVIDER_CSV,
            provider_event_id=provider_event_id,
            recap_status=VisitSubmissionSource.RECAP_APPROVED,
            exclude_from_aggregates=provider_excluded,
            version=1,
            row_fingerprint=fp,
            samples_distributed=normalized.get("samples_distributed"),
            consumers_sampled=normalized.get("consumers_sampled"),
            rep_observed_units=normalized.get("rep_observed_units"),
            sku_breakdown=normalized.get("matched_skus") or [],
            notes=normalized.get("notes") or "",
            raw_row=raw_row,
            submitted_by=actor if getattr(actor, "is_authenticated", False) else None,
        )
        outcome = ProviderImportRow.STATUS_CREATED
        msg = "Imported provider submission onto canonical visit."

    from pilot.costs import recompute_visit_cost_summary
    from pilot.finance import evaluate_finance_exceptions
    from pilot.models import FinanceException

    # Record optional provider-reported cost lines, moving them if the visit
    # changed and removing stale lines when a correction clears an amount.
    demo_fee_ref = f"import-sub:{sub.id}:demo_fee"
    if normalized.get("demo_fee") is not None:
        fee_amt = Decimal(str(normalized["demo_fee"]))
        BrandCostLine.objects.update_or_create(
            tenant=tenant,
            provider=provider,
            category=BrandCostLine.CAT_DEMO_FEE,
            source_reference=demo_fee_ref,
            defaults={
                "canonical_visit": visit,
                "source_type": BrandCostLine.SOURCE_INVOICE_LINE,
                "description": f"{provider.name} reported demo fee",
                "amount": fee_amt,
                "is_confirmed_zero": fee_amt == Decimal("0.00"),
                "settlement_status": BrandCostLine.SETTLEMENT_INVOICED,
                "evidence_status": BrandCostLine.EVIDENCE_ESTIMATED,
                "created_by": (
                    actor if getattr(actor, "is_authenticated", False) else None
                ),
            },
        )
    else:
        BrandCostLine.objects.filter(
            tenant=tenant,
            source_reference=demo_fee_ref,
        ).delete()

    product_ref = f"import-sub:{sub.id}:product"
    if normalized.get("product_spend") is not None:
        prod_amt = Decimal(str(normalized["product_spend"]))
        BrandCostLine.objects.update_or_create(
            tenant=tenant,
            provider=provider,
            category=BrandCostLine.CAT_PRODUCT,
            source_reference=product_ref,
            defaults={
                "canonical_visit": visit,
                "source_type": BrandCostLine.SOURCE_EXPENSE_RECEIPT,
                "description": f"{provider.name} reported product spend",
                "amount": prod_amt,
                "is_confirmed_zero": prod_amt == Decimal("0.00"),
                "settlement_status": BrandCostLine.SETTLEMENT_INVOICED,
                "evidence_status": (
                    BrandCostLine.EVIDENCE_SUPPORTED
                    if prod_amt == Decimal("0.00")
                    else BrandCostLine.EVIDENCE_MISSING
                ),
                "created_by": (
                    actor if getattr(actor, "is_authenticated", False) else None
                ),
            },
        )
    else:
        BrandCostLine.objects.filter(
            tenant=tenant,
            source_reference=product_ref,
        ).delete()

    recompute_canonical_visit_metrics(visit)
    recompute_visit_cost_summary(visit)
    if FinanceException.objects.filter(
        tenant=tenant, canonical_visit=visit
    ).exists():
        evaluate_finance_exceptions(tenant, visit=visit)

    if old_visit is not None and old_visit.id != visit.id:
        remaining_subs = VisitSubmissionSource.objects.filter(
            canonical_visit=old_visit
        ).exclude(recap_status=VisitSubmissionSource.RECAP_EXCLUDED)
        if remaining_subs.exists():
            recompute_canonical_visit_metrics(old_visit)
        else:
            old_visit.samples_distributed = None
            old_visit.consumers_sampled = None
            old_visit.rep_observed_units = None
            old_visit.total_engagements = None
            from pilot.models import ProviderAssignment, StoreRecommendationAction

            has_other_refs = (
                old_visit.event_id is not None
                or old_visit.request_id is not None
                or BrandCostLine.objects.filter(canonical_visit=old_visit).exists()
                or ProviderAssignment.objects.filter(
                    canonical_visit=old_visit
                ).exists()
                or StoreRecommendationAction.objects.filter(
                    outcome_visit=old_visit
                ).exists()
            )
            if has_other_refs:
                if old_visit.event_id is not None:
                    old_visit.status = CanonicalVisit.STATUS_SCHEDULED
                    old_visit.completed_at = None
            else:
                old_visit.status = CanonicalVisit.STATUS_CANCELLED
                old_visit.exclude_from_aggregates = True
            old_visit.save(
                update_fields=[
                    "samples_distributed",
                    "consumers_sampled",
                    "rep_observed_units",
                    "total_engagements",
                    "status",
                    "completed_at",
                    "exclude_from_aggregates",
                    "updated_at",
                ]
            )
        recompute_visit_cost_summary(old_visit)
        if FinanceException.objects.filter(
            tenant=tenant, canonical_visit=old_visit
        ).exists():
            evaluate_finance_exceptions(tenant, visit=old_visit)

    return outcome, visit, sub, msg


@transaction.atomic
def commit_provider_import(
    access: ResolvedWorkspaceAccess,
    *,
    provider_uuid: str,
    csv_text: str,
    filename: str = "upload.csv",
    mapping_uuid: str | None = None,
    column_map: dict[str, str] | None = None,
    preset_key: str | None = None,
    save_mapping_name: str | None = None,
    idempotency_key: str | None = None,
    is_historical: bool = False,
) -> ProviderImportJob:
    provider = ProviderOrganization.objects.filter(uuid=provider_uuid).first()
    if provider is None:
        raise ValueError("Provider organization not found.")
    if not access.can_import_for_provider(provider.id):
        raise PermissionError(
            "You do not have permission to import files for this provider."
        )
    if not is_historical:
        require_plan_feature(
            access.tenant,
            "allows_provider_csv_imports",
            feature_label="Ongoing provider CSV imports",
            required_plan="core",
        )
        require_provider_feed_capacity(
            access.tenant, candidate_provider=provider
        )

    mapping_obj: ProviderImportMapping | None = None
    effective_map = column_map
    effective_preset = preset_key
    if mapping_uuid:
        mapping_obj = ProviderImportMapping.objects.filter(
            tenant=access.tenant, provider=provider, uuid=mapping_uuid
        ).first()
        if mapping_obj is None:
            raise ValueError("Saved import mapping not found.")
        effective_map = mapping_obj.column_map
        effective_preset = effective_preset or mapping_obj.preset_key

    analysis = _analyze_import_rows(
        tenant=access.tenant,
        provider=provider,
        csv_text=csv_text,
        column_map=effective_map,
        preset_key=effective_preset,
    )

    if save_mapping_name:
        mapping_obj = save_provider_import_mapping(
            access,
            provider_uuid=provider_uuid,
            name=save_mapping_name,
            column_map=analysis["resolved_map"],
            preset_key=effective_preset or "custom",
            is_default=True,
        )

    file_hash = hashlib.sha256(csv_text.encode("utf-8")).hexdigest()
    idem = (idempotency_key or "").strip()
    if idem:
        existing_job = ProviderImportJob.objects.filter(
            tenant=access.tenant, provider=provider, idempotency_key=idem
        ).first()
        if existing_job is not None:
            return existing_job
    else:
        idem = f"sha:{file_hash}:{timezone.now().strftime('%Y%m%d%H%M%S%f')}"

    ProviderSourceInclusionRule.objects.get_or_create(
        tenant=access.tenant,
        provider=provider,
        defaults={
            "include_in_combined_reporting": True,
            "updated_by": access.user,
        },
    )

    try:
        job = ProviderImportJob.objects.create(
            tenant=access.tenant,
            provider=provider,
            mapping=mapping_obj,
            mapping_snapshot=analysis["resolved_map"],
            filename=(filename or "upload.csv")[:255],
            file_sha256=file_hash,
            idempotency_key=idem,
            is_historical=is_historical,
            status=ProviderImportJob.STATUS_COMMITTED,
            total_rows=analysis["total_rows"],
            actor=access.user,
        )
    except IntegrityError:
        existing = ProviderImportJob.objects.filter(
            tenant=access.tenant, provider=provider, idempotency_key=idem
        ).first()
        if existing is not None:
            return existing
        raise

    created_c = 0
    updated_c = 0
    unchanged_c = 0
    review_c = 0
    error_c = 0

    for row_data in analysis["rows"]:
        r_status = row_data["status"]
        if r_status in {
            ProviderImportRow.STATUS_ERROR,
            ProviderImportRow.STATUS_NEEDS_REVIEW,
        }:
            ProviderImportRow.objects.create(
                job=job,
                tenant=access.tenant,
                provider=provider,
                row_number=row_data["row_number"],
                status=r_status,
                error_code=row_data["error_code"],
                message=row_data["message"],
                raw_row=row_data["raw_row"],
                normalized_row=row_data["normalized_row"],
            )
            if r_status == ProviderImportRow.STATUS_NEEDS_REVIEW:
                review_c += 1
            else:
                error_c += 1
            continue

        outcome, visit, sub, msg = _apply_normalized_row_to_db(
            tenant=access.tenant,
            provider=provider,
            actor=access.user,
            raw_row=row_data["raw_row"],
            normalized=row_data["normalized_row"],
            is_historical=is_historical,
        )
        ProviderImportRow.objects.create(
            job=job,
            tenant=access.tenant,
            provider=provider,
            row_number=row_data["row_number"],
            status=outcome,
            error_code="",
            message=msg,
            raw_row=row_data["raw_row"],
            normalized_row=row_data["normalized_row"],
            canonical_visit=visit,
            submission=sub,
        )
        if outcome == ProviderImportRow.STATUS_CREATED:
            created_c += 1
        elif outcome == ProviderImportRow.STATUS_UPDATED:
            updated_c += 1
        else:
            unchanged_c += 1

    job.created_count = created_c
    job.updated_count = updated_c
    job.unchanged_count = unchanged_c
    job.review_count = review_c
    job.error_count = error_c
    job.status = (
        ProviderImportJob.STATUS_PARTIAL_REVIEW
        if review_c > 0
        else (
            ProviderImportJob.STATUS_FAILED
            if error_c > 0 and (created_c + updated_c + unchanged_c) == 0
            else ProviderImportJob.STATUS_COMMITTED
        )
    )
    job.save()

    log_workspace_audit(
        tenant=access.tenant,
        actor=access.user,
        category="import",
        action="provider_csv_committed",
        target_type="ProviderImportJob",
        target_id=str(job.uuid),
        summary=(
            f"Imported {job.filename} for {provider.name}: "
            f"{created_c} created, {updated_c} updated, {unchanged_c} unchanged, "
            f"{review_c} review, {error_c} errors."
        ),
    )
    return job


@transaction.atomic
def resolve_provider_import_row(
    access: ResolvedWorkspaceAccess,
    *,
    row_uuid: str,
    retailer_id: int | None = None,
    confirmed_store_number: str | None = None,
    confirmed_address: str | None = None,
    confirmed_city: str | None = None,
    confirmed_state: str | None = None,
    save_store_alias: bool = False,
    product_id: int | None = None,
    save_product_alias: bool = True,
) -> ProviderImportRow:
    """Resolve an uncertain-match row in place (optionally saving reusable
    RetailerAlias / ProductAlias rules) and retry JUST this row without
    re-uploading the file."""
    if not access.can_edit_brand_ops:
        raise PermissionError(
            "Only brand administrators or reviewers can confirm uncertain matches."
        )
    row = (
        ProviderImportRow.objects.select_related("job", "provider")
        .filter(tenant=access.tenant, uuid=row_uuid)
        .first()
    )
    if row is None:
        raise ValueError("Import row not found.")

    norm = dict(row.normalized_row or {})
    if retailer_id is not None:
        retailer = (
            Retailer.objects.select_related("location", "location__state")
            .filter(tenant=access.tenant, id=retailer_id)
            .first()
        )
        if retailer is None:
            raise ValueError("Selected canonical store not found.")
        rcity, rstate = _retailer_city_state(retailer)
        if save_store_alias and norm.get("retailer_name"):
            raw_alias = norm["retailer_name"].strip()
            alias_k = re.sub(r"[^a-z0-9]+", " ", raw_alias.lower()).strip()
            if alias_k:
                RetailerAlias.objects.update_or_create(
                    tenant=access.tenant,
                    alias_key=alias_k,
                    defaults={
                        "alias_name": raw_alias,
                        "canonical_name": retailer.name,
                        "status": RetailerAlias.STATUS_ACCEPTED,
                        "decided_by": access.user,
                        "decided_at": timezone.now(),
                    },
                )
        s_key, s_label, c_key, c_label = store_identity(
            retailer.name,
            confirmed_address or retailer.address or "",
            confirmed_city or rcity,
            confirmed_state or rstate,
        )
        norm["store"] = {
            "retailer_id": retailer.id,
            "store_key": s_key,
            "store_label": s_label,
            "chain_key": c_key,
            "chain_label": c_label,
            "store_number": confirmed_store_number or "",
            "external_store_id": norm.get("external_store_id") or "",
            "address": confirmed_address or retailer.address or "",
            "city": confirmed_city or rcity,
            "state_code": confirmed_state or rstate,
        }
    elif confirmed_store_number or confirmed_address or confirmed_city:
        aliases = accepted_alias_map(access.tenant.id)
        store_info, err_code, err_msg = resolve_canonical_store(
            tenant=access.tenant,
            retailer_name=norm.get("retailer_name") or "",
            store_number=confirmed_store_number or norm.get("store_number") or "",
            external_store_id=norm.get("external_store_id") or "",
            address=confirmed_address or norm.get("address") or "",
            city=confirmed_city or norm.get("city") or "",
            state_code=confirmed_state or norm.get("state_code") or "",
            aliases=aliases,
        )
        if err_code or store_info is None:
            raise ValueError(err_msg or "Store details remain ambiguous.")
        norm["store"] = store_info

    if product_id is not None:
        product = Product.objects.filter(
            tenant=access.tenant,
            id=product_id,
        ).first()
        if product is None:
            raise ValueError("Selected canonical product not found.")
        unmatched_tokens = list(norm.get("unmatched_skus") or [])
        if save_product_alias:
            for tok in unmatched_tokens:
                ak = normalize_product_key(tok)
                if ak:
                    ProductAlias.objects.update_or_create(
                        tenant=access.tenant,
                        alias_key=ak,
                        defaults={
                            "provider": row.provider,
                            "alias_name": tok[:200],
                            "product": product,
                            "status": ProductAlias.STATUS_ACCEPTED,
                            "decided_by": access.user,
                            "decided_at": timezone.now(),
                        },
                    )
        product_lookup = build_product_lookup(access.tenant)
        matched_skus, still_unmatched = resolve_products_from_cell(
            norm.get("sku_name") or "", product_lookup
        )
        if still_unmatched:
            raise ValueError(
                f"Remaining unrecognized SKUs: {', '.join(still_unmatched)}"
            )
        norm["matched_skus"] = matched_skus
        norm["unmatched_skus"] = []

    if not norm.get("store"):
        raise ValueError("Canonical store match is still required.")
    if norm.get("unmatched_skus"):
        raise ValueError(
            f"Product alias still required for: {', '.join(norm['unmatched_skus'])}"
        )

    outcome, visit, sub, msg = _apply_normalized_row_to_db(
        tenant=access.tenant,
        provider=row.provider,
        actor=access.user,
        raw_row=row.raw_row,
        normalized=norm,
        is_historical=row.job.is_historical,
    )
    prev_was_review = row.status == ProviderImportRow.STATUS_NEEDS_REVIEW
    row.status = outcome
    row.error_code = ""
    row.message = msg
    row.normalized_row = norm
    row.canonical_visit = visit
    row.submission = sub
    row.resolved_by = access.user
    row.resolved_at = timezone.now()
    row.save()

    job = row.job
    if prev_was_review and job.review_count > 0:
        job.review_count -= 1
    if outcome == ProviderImportRow.STATUS_CREATED:
        job.created_count += 1
    elif outcome == ProviderImportRow.STATUS_UPDATED:
        job.updated_count += 1
    else:
        job.unchanged_count += 1
    if job.review_count == 0 and job.error_count == 0:
        job.status = ProviderImportJob.STATUS_COMMITTED
    job.save()

    log_workspace_audit(
        tenant=access.tenant,
        actor=access.user,
        category="import",
        action="import_row_resolved",
        target_type="ProviderImportRow",
        target_id=str(row.uuid),
        summary=f"Resolved row #{row.row_number} in {job.filename} -> {outcome}.",
    )
    return row
