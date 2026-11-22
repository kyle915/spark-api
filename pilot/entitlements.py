"""Feature 8 service layer: Centralized software plan catalog, server-enforced
plan entitlements, contract & pilot overrides, paid-through access resolution,
and deterministic calendar-month canonical visit metering in the brand's
configured business timezone."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from django.conf import settings
from django.utils import timezone

from events.models import EventType
from pilot.models import (
    BrandSubscription,
    CanonicalVisit,
    PlanCatalogEntry,
    ProviderAssignment,
    ProviderImportJob,
    ProviderOrganization,
    RetailerSalesImport,
    WorkspaceMembership,
    WorkspaceProfile,
)
from tenants.models import Tenant


class PlanEntitlementError(PermissionError):
    """Raised when a brand workspace attempts an action outside its active
    plan tier or monthly visit allowance."""

    def __init__(
        self,
        message: str,
        *,
        code: str = "PLAN_UPGRADE_REQUIRED",
        required_plan: str = "core",
    ) -> None:
        super().__init__(message)
        self.code = code
        self.required_plan = required_plan


DEFAULT_PLAN_SPECS: tuple[dict[str, Any], ...] = (
    {
        "plan_code": WorkspaceProfile.PLAN_FREE,
        "display_name": "Free",
        "tagline": "One active program, starter form, mobile submission, photos & shareable recap.",
        "monthly_price_usd": Decimal("0.00"),
        "included_completed_visits": 5,
        "is_quote_only": False,
        "is_self_serve_selectable": True,
        "stripe_price_setting": "",
        "max_active_programs": 1,
        "max_operating_providers": 1,
        "max_pos_retailers": 0,
        "allows_branded_forms": False,
        "allows_approvals_and_reminders": False,
        "allows_exports_and_weekly_reporting": False,
        "allows_store_recommendations": False,
        "allows_provider_csv_imports": False,
        "allows_brand_cost_tracking": False,
        "allows_finance_review_queue": False,
        "allows_retailer_pos_measurement": False,
        "allows_tracked_store_actions": False,
        "sort_order": 10,
    },
    {
        "plan_code": WorkspaceProfile.PLAN_CORE,
        "display_name": "Core",
        "tagline": "One operating team or provider, branded forms, approvals, exports, weekly reporting & store recommendations.",
        "monthly_price_usd": Decimal("299.00"),
        "included_completed_visits": 50,
        "is_quote_only": False,
        "is_self_serve_selectable": True,
        "stripe_price_setting": "STRIPE_PRICE_ID_CORE",
        "max_active_programs": None,
        "max_operating_providers": 1,
        "max_pos_retailers": 0,
        "allows_branded_forms": True,
        "allows_approvals_and_reminders": True,
        "allows_exports_and_weekly_reporting": True,
        "allows_store_recommendations": True,
        "allows_provider_csv_imports": True,
        "allows_brand_cost_tracking": False,
        "allows_finance_review_queue": False,
        "allows_retailer_pos_measurement": False,
        "allows_tracked_store_actions": False,
        "sort_order": 20,
    },
    {
        "plan_code": WorkspaceProfile.PLAN_PRO,
        "display_name": "Pro",
        "tagline": "Core plus two provider feeds, brand-paid cost, finance review, one retailer POS feed & tracked store actions.",
        "monthly_price_usd": Decimal("750.00"),
        "included_completed_visits": 100,
        "is_quote_only": False,
        "is_self_serve_selectable": True,
        "stripe_price_setting": "STRIPE_PRICE_ID_PRO",
        "max_active_programs": None,
        "max_operating_providers": 2,
        "max_pos_retailers": 1,
        "allows_branded_forms": True,
        "allows_approvals_and_reminders": True,
        "allows_exports_and_weekly_reporting": True,
        "allows_store_recommendations": True,
        "allows_provider_csv_imports": True,
        "allows_brand_cost_tracking": True,
        "allows_finance_review_queue": True,
        "allows_retailer_pos_measurement": True,
        "allows_tracked_store_actions": True,
        "sort_order": 30,
    },
    {
        "plan_code": WorkspaceProfile.PLAN_SCALE,
        "display_name": "Scale",
        "tagline": "Quote-only. Additional provider feeds, retailers, volume & custom support per signed agreement.",
        "monthly_price_usd": Decimal("1500.00"),
        "included_completed_visits": 250,
        "is_quote_only": True,
        "is_self_serve_selectable": False,
        "stripe_price_setting": "STRIPE_PRICE_ID_SCALE",
        "max_active_programs": None,
        "max_operating_providers": None,
        "max_pos_retailers": None,
        "allows_branded_forms": True,
        "allows_approvals_and_reminders": True,
        "allows_exports_and_weekly_reporting": True,
        "allows_store_recommendations": True,
        "allows_provider_csv_imports": True,
        "allows_brand_cost_tracking": True,
        "allows_finance_review_queue": True,
        "allows_retailer_pos_measurement": True,
        "allows_tracked_store_actions": True,
        "sort_order": 40,
    },
    {
        "plan_code": WorkspaceProfile.PLAN_PILOT_PRO,
        "display_name": "60-Day Pro Pilot",
        "tagline": "Manually provisioned $1,500, 60-day Pro pilot with full multi-provider cost, POS & action workflows.",
        "monthly_price_usd": Decimal("750.00"),
        "included_completed_visits": 100,
        "is_quote_only": True,
        "is_self_serve_selectable": False,
        "stripe_price_setting": "",
        "max_active_programs": None,
        "max_operating_providers": 2,
        "max_pos_retailers": 1,
        "allows_branded_forms": True,
        "allows_approvals_and_reminders": True,
        "allows_exports_and_weekly_reporting": True,
        "allows_store_recommendations": True,
        "allows_provider_csv_imports": True,
        "allows_brand_cost_tracking": True,
        "allows_finance_review_queue": True,
        "allows_retailer_pos_measurement": True,
        "allows_tracked_store_actions": True,
        "sort_order": 50,
    },
    {
        "plan_code": WorkspaceProfile.PLAN_MANAGED_SOW,
        "display_name": "Managed SOW",
        "tagline": "Ignite-managed SOW client workspace preserving full contracted reporting and operations.",
        "monthly_price_usd": Decimal("0.00"),
        "included_completed_visits": None,
        "is_quote_only": True,
        "is_self_serve_selectable": False,
        "stripe_price_setting": "",
        "max_active_programs": None,
        "max_operating_providers": None,
        "max_pos_retailers": None,
        "allows_branded_forms": True,
        "allows_approvals_and_reminders": True,
        "allows_exports_and_weekly_reporting": True,
        "allows_store_recommendations": True,
        "allows_provider_csv_imports": True,
        "allows_brand_cost_tracking": True,
        "allows_finance_review_queue": True,
        "allows_retailer_pos_measurement": True,
        "allows_tracked_store_actions": True,
        "sort_order": 60,
    },
)


def ensure_plan_catalog() -> list[PlanCatalogEntry]:
    """Seed or hydrate centralized PlanCatalogEntry rows without clobbering
    admin overrides already saved in the database."""
    today_date = date(2026, 1, 1)
    entries: list[PlanCatalogEntry] = []
    for spec in DEFAULT_PLAN_SPECS:
        price_setting = spec["stripe_price_setting"]
        default_price_id = (
            getattr(settings, price_setting, "") if price_setting else ""
        )
        defaults = {
            k: v
            for k, v in spec.items()
            if k not in {"plan_code", "stripe_price_setting"}
        }
        defaults["stripe_price_id"] = default_price_id
        defaults["effective_from"] = today_date
        entry, _ = PlanCatalogEntry.objects.get_or_create(
            plan_code=spec["plan_code"],
            defaults=defaults,
        )
        entries.append(entry)
    return entries


def get_plan_entry(plan_code: str) -> PlanCatalogEntry:
    ensure_plan_catalog()
    entry = PlanCatalogEntry.objects.filter(plan_code=plan_code).first()
    if entry is None:
        entry = PlanCatalogEntry.objects.get(
            plan_code=WorkspaceProfile.PLAN_FREE
        )
    return entry


def ensure_brand_subscription(tenant: Tenant) -> BrandSubscription:
    """Ensure every tenant has a deterministic BrandSubscription aligned with
    its WorkspaceProfile (defaulting existing tenants to Managed SOW)."""
    ensure_plan_catalog()
    profile, _ = WorkspaceProfile.objects.get_or_create(
        tenant=tenant,
        defaults={
            "mode": WorkspaceProfile.MODE_MANAGED,
            "plan_tier": WorkspaceProfile.PLAN_MANAGED_SOW,
        },
    )
    existing = BrandSubscription.objects.filter(tenant=tenant).first()
    if existing is not None:
        return existing

    if profile.mode == WorkspaceProfile.MODE_SELF_SERVE:
        plan_code = (
            profile.plan_tier
            if profile.plan_tier
            in {
                WorkspaceProfile.PLAN_FREE,
                WorkspaceProfile.PLAN_CORE,
                WorkspaceProfile.PLAN_PRO,
                WorkspaceProfile.PLAN_SCALE,
            }
            else WorkspaceProfile.PLAN_FREE
        )
        default_status = (
            BrandSubscription.STATUS_FREE_ACTIVE
            if plan_code == WorkspaceProfile.PLAN_FREE
            else BrandSubscription.STATUS_ACTIVE
        )
    elif profile.mode == WorkspaceProfile.MODE_PILOT:
        plan_code = WorkspaceProfile.PLAN_PILOT_PRO
        default_status = BrandSubscription.STATUS_PILOT_ACTIVE
    else:
        plan_code = WorkspaceProfile.PLAN_MANAGED_SOW
        default_status = BrandSubscription.STATUS_MANAGED_SOW

    return BrandSubscription.objects.create(
        tenant=tenant,
        plan_code=plan_code,
        status=default_status,
        stripe_mode=getattr(settings, "STRIPE_BILLING_MODE", "test") or "test",
    )


def resolve_business_tz(tz_name: str | None) -> ZoneInfo:
    clean = (tz_name or "America/Chicago").strip() or "America/Chicago"
    try:
        return ZoneInfo(clean)
    except (ZoneInfoNotFoundError, KeyError, ValueError):
        return ZoneInfo("America/Chicago")


@dataclass(frozen=True)
class EffectiveBrandEntitlements:
    tenant_id: int
    workspace_mode: str
    configured_plan_code: str
    effective_plan_code: str
    display_name: str
    subscription_status: str
    business_timezone: str
    monthly_price_usd: Decimal
    included_completed_visits: int | None
    effective_visit_cap: int | None
    overage_agreement_approved: bool
    overage_unit_price_usd: Decimal | None
    is_quote_only: bool
    is_managed_sow: bool
    addon_plan_code: str
    is_pilot: bool
    pilot_fee_usd: Decimal | None
    pilot_ends_at: datetime | None
    paid_through_at: datetime | None
    has_payment_failure: bool
    payment_failure_message: str
    max_active_programs: int | None
    max_operating_providers: int | None
    max_pos_retailers: int | None
    allows_branded_forms: bool
    allows_approvals_and_reminders: bool
    allows_exports_and_weekly_reporting: bool
    allows_store_recommendations: bool
    allows_provider_csv_imports: bool
    allows_brand_cost_tracking: bool
    allows_finance_review_queue: bool
    allows_retailer_pos_measurement: bool
    allows_tracked_store_actions: bool
    viewer_and_contributor_seats_free: bool = True


def resolve_brand_entitlements(
    tenant: Tenant, *, at_time: datetime | None = None
) -> EffectiveBrandEntitlements:
    """Resolve the brand's currently active entitlements at `at_time`,
    accounting for:
    - Managed SOW accounts (and optional standalone paid add-ons)
    - Manually provisioned 60-day Pro pilots (and pilot expiration)
    - Cancelled subscriptions still within their paid-through period
    - Expired / unpaid cancellations falling back cleanly to Free without
      deleting historical records."""
    now = at_time or timezone.now()
    profile, _ = WorkspaceProfile.objects.get_or_create(
        tenant=tenant,
        defaults={
            "mode": WorkspaceProfile.MODE_MANAGED,
            "plan_tier": WorkspaceProfile.PLAN_MANAGED_SOW,
        },
    )
    sub = ensure_brand_subscription(tenant)

    effective_status = sub.status
    effective_code = sub.plan_code

    # Managed SOW accounts retain their contracted SOW reporting regardless of
    # any optional software add-on state.
    if profile.mode == WorkspaceProfile.MODE_MANAGED and (
        sub.plan_code == WorkspaceProfile.PLAN_MANAGED_SOW
    ):
        effective_code = WorkspaceProfile.PLAN_MANAGED_SOW
        effective_status = BrandSubscription.STATUS_MANAGED_SOW
    else:
        # Handle 60-day Pro pilot expiration if recurring billing hasn't started
        if (
            sub.is_pilot
            and effective_status == BrandSubscription.STATUS_PILOT_ACTIVE
            and sub.pilot_ends_at is not None
            and now >= sub.pilot_ends_at
        ):
            effective_status = BrandSubscription.STATUS_PILOT_EXPIRED
            effective_code = WorkspaceProfile.PLAN_FREE

        # Handle cancel_at_period_end: full paid-through access BEFORE period
        # end; falls back to Free AFTER period end.
        if effective_status == BrandSubscription.STATUS_CANCEL_AT_PERIOD_END:
            if sub.current_period_end and now >= sub.current_period_end:
                effective_status = BrandSubscription.STATUS_CANCELED
                effective_code = WorkspaceProfile.PLAN_FREE
                if sub.pending_plan_code:
                    effective_code = sub.pending_plan_code
        elif effective_status == BrandSubscription.STATUS_CANCELED:
            if sub.current_period_end and now < sub.current_period_end:
                # Still inside paid-through window!
                effective_status = BrandSubscription.STATUS_CANCEL_AT_PERIOD_END
            else:
                effective_code = WorkspaceProfile.PLAN_FREE
        elif effective_status == BrandSubscription.STATUS_PAST_DUE:
            # Past-due after period end falls back to Free for new paid actions
            # while keeping historical access and payment warning visible.
            if sub.current_period_end and now >= sub.current_period_end:
                effective_code = WorkspaceProfile.PLAN_FREE

        # Apply scheduled downgrade once current_period_end has passed
        if (
            sub.pending_plan_code
            and sub.current_period_end
            and now >= sub.current_period_end
            and effective_status == BrandSubscription.STATUS_ACTIVE
        ):
            effective_code = sub.pending_plan_code

    entry = get_plan_entry(effective_code)

    # Optional paid add-on on top of a Managed SOW account
    addon_entry = (
        get_plan_entry(sub.addon_plan_code) if sub.addon_plan_code else None
    )

    monthly_price = (
        sub.custom_monthly_price_usd
        if sub.custom_monthly_price_usd is not None
        else entry.monthly_price_usd
    )
    included_visits = (
        sub.custom_included_visits_monthly
        if sub.custom_included_visits_monthly is not None
        else entry.included_completed_visits
    )
    effective_cap = (
        sub.agreed_volume_band_visits
        if sub.agreed_volume_band_visits is not None
        else included_visits
    )
    # If dropped back to Free after cancellation/expiration, enforce Free cap
    if (
        effective_code == WorkspaceProfile.PLAN_FREE
        and sub.plan_code != WorkspaceProfile.PLAN_FREE
    ):
        included_visits = entry.included_completed_visits
        effective_cap = entry.included_completed_visits

    max_providers = (
        sub.custom_max_providers
        if sub.custom_max_providers is not None
        else entry.max_operating_providers
    )
    max_pos_retailers = (
        sub.custom_max_pos_retailers
        if sub.custom_max_pos_retailers is not None
        else entry.max_pos_retailers
    )
    if effective_code == WorkspaceProfile.PLAN_FREE and (
        sub.plan_code != WorkspaceProfile.PLAN_FREE
    ):
        max_providers = entry.max_operating_providers
        max_pos_retailers = entry.max_pos_retailers

    def _flag(attr: str) -> bool:
        val = bool(getattr(entry, attr, False))
        if (
            addon_entry is not None
            and effective_code == WorkspaceProfile.PLAN_MANAGED_SOW
            and sub.last_payment_status != BrandSubscription.PAY_FAILED
        ):
            val = val or bool(getattr(addon_entry, attr, False))
        return val

    return EffectiveBrandEntitlements(
        tenant_id=tenant.id,
        workspace_mode=profile.mode,
        configured_plan_code=sub.plan_code,
        effective_plan_code=effective_code,
        display_name=entry.display_name,
        subscription_status=effective_status,
        business_timezone=profile.business_timezone or "America/Chicago",
        monthly_price_usd=monthly_price,
        included_completed_visits=included_visits,
        effective_visit_cap=effective_cap,
        overage_agreement_approved=bool(
            sub.overage_agreement_approved
            and effective_code != WorkspaceProfile.PLAN_FREE
        ),
        overage_unit_price_usd=sub.overage_unit_price_usd,
        is_quote_only=entry.is_quote_only,
        is_managed_sow=(effective_code == WorkspaceProfile.PLAN_MANAGED_SOW),
        addon_plan_code=sub.addon_plan_code or "",
        is_pilot=bool(sub.is_pilot),
        pilot_fee_usd=sub.pilot_fee_usd,
        pilot_ends_at=sub.pilot_ends_at,
        paid_through_at=sub.current_period_end or sub.pilot_ends_at,
        has_payment_failure=(
            sub.last_payment_status == BrandSubscription.PAY_FAILED
            or effective_status == BrandSubscription.STATUS_PAST_DUE
        ),
        payment_failure_message=sub.last_payment_error or "",
        max_active_programs=entry.max_active_programs,
        max_operating_providers=max_providers,
        max_pos_retailers=max_pos_retailers,
        allows_branded_forms=_flag("allows_branded_forms"),
        allows_approvals_and_reminders=_flag("allows_approvals_and_reminders"),
        allows_exports_and_weekly_reporting=_flag(
            "allows_exports_and_weekly_reporting"
        ),
        allows_store_recommendations=_flag("allows_store_recommendations"),
        allows_provider_csv_imports=_flag("allows_provider_csv_imports"),
        allows_brand_cost_tracking=_flag("allows_brand_cost_tracking"),
        allows_finance_review_queue=_flag("allows_finance_review_queue"),
        allows_retailer_pos_measurement=_flag(
            "allows_retailer_pos_measurement"
        ),
        allows_tracked_store_actions=_flag("allows_tracked_store_actions"),
    )


def _visit_completion_local_date(
    visit: CanonicalVisit, tz: ZoneInfo
) -> date | None:
    """Return the visit's completion calendar date in the brand's configured
    business timezone, or None if the visit does not count as completed."""
    if visit.status != CanonicalVisit.STATUS_COMPLETED:
        return None
    if visit.is_historical_import:
        return None
    if not visit.counts_toward_allowance:
        return None

    if visit.completed_at is not None:
        aware_dt = (
            timezone.make_aware(visit.completed_at, ZoneInfo("UTC"))
            if timezone.is_naive(visit.completed_at)
            else visit.completed_at
        )
        return aware_dt.astimezone(tz).date()

    # Deterministic fallback when a completed visit has visit_date only
    fallback_dt = datetime.combine(visit.visit_date, time(12, 0), tzinfo=tz)
    return fallback_dt.date()


def compute_monthly_visit_usage(
    tenant: Tenant,
    *,
    year: int | None = None,
    month: int | None = None,
    at_time: datetime | None = None,
) -> dict[str, Any]:
    """Count canonical completed visits for `(year, month)` in the brand's
    configured business timezone.
    - 1 canonical completed activation = 1 unit regardless of how many reps,
      co-filed recaps, CSV uploads, or retries are attached to it.
    - Excludes drafts, needs_review, scheduled, in_progress, cancelled,
      archived/reversed, and historical onboarding imports (`is_historical_import=True`).
    - Explicitly surfaces co-branded visits and non-counted categories."""
    now = at_time or timezone.now()
    ent = resolve_brand_entitlements(tenant, at_time=now)
    tz = resolve_business_tz(ent.business_timezone)
    local_now = now.astimezone(tz)
    target_year = year or local_now.year
    target_month = month or local_now.month
    month_key = f"{target_year:04d}-{target_month:02d}"

    all_visits = list(
        CanonicalVisit.objects.select_related("primary_provider")
        .prefetch_related("submissions")
        .filter(tenant=tenant)
        .order_by("visit_date", "id")
    )

    counted_visits: list[dict[str, Any]] = []
    historical_excluded_count = 0
    in_progress_or_pending_count = 0
    cancelled_or_reversed_count = 0
    co_branded_counted = 0

    for v in all_visits:
        if v.is_historical_import:
            if v.visit_date.year == target_year and v.visit_date.month == target_month:
                historical_excluded_count += 1
            continue

        if v.status in {
            CanonicalVisit.STATUS_CANCELLED,
            CanonicalVisit.STATUS_ARCHIVED,
        }:
            if v.visit_date.year == target_year and v.visit_date.month == target_month:
                cancelled_or_reversed_count += 1
            continue

        if v.status != CanonicalVisit.STATUS_COMPLETED:
            if v.visit_date.year == target_year and v.visit_date.month == target_month:
                in_progress_or_pending_count += 1
            continue

        comp_local_date = _visit_completion_local_date(v, tz)
        if comp_local_date is None:
            continue
        if (
            comp_local_date.year != target_year
            or comp_local_date.month != target_month
        ):
            continue

        is_co_branded = v.co_brand_share_pct is not None
        if is_co_branded:
            co_branded_counted += 1

        counted_visits.append(
            {
                "visit_uuid": str(v.uuid),
                "store_label": v.store_label or v.address or "Visit",
                "visit_date": v.visit_date.isoformat(),
                "completion_local_date": comp_local_date.isoformat(),
                "provider_name": (
                    v.primary_provider.name if v.primary_provider else None
                ),
                "submission_count": len(list(v.submissions.all())),
                "co_brand_share_pct": (
                    str(v.co_brand_share_pct)
                    if v.co_brand_share_pct is not None
                    else None
                ),
            }
        )

    used = len(counted_visits)
    cap = ent.effective_visit_cap
    remaining = max(cap - used, 0) if cap is not None else None
    at_or_over_cap = bool(cap is not None and used >= cap)
    overage_units = max(used - cap, 0) if cap is not None else 0
    can_book_new_visit = bool(
        cap is None or used < cap or ent.overage_agreement_approved
    )

    next_upgrade_plan: str | None = None
    if ent.effective_plan_code == WorkspaceProfile.PLAN_FREE:
        next_upgrade_plan = WorkspaceProfile.PLAN_CORE
    elif ent.effective_plan_code == WorkspaceProfile.PLAN_CORE:
        next_upgrade_plan = WorkspaceProfile.PLAN_PRO
    elif ent.effective_plan_code in {
        WorkspaceProfile.PLAN_PRO,
        WorkspaceProfile.PLAN_PILOT_PRO,
    }:
        next_upgrade_plan = WorkspaceProfile.PLAN_SCALE

    return {
        "month_key": month_key,
        "business_timezone": ent.business_timezone,
        "completed_visits_used": used,
        "included_visits": ent.included_completed_visits,
        "effective_visit_cap": cap,
        "remaining_visits": remaining,
        "at_or_over_cap": at_or_over_cap,
        "overage_units": overage_units,
        "overage_agreement_approved": ent.overage_agreement_approved,
        "can_book_new_visit": can_book_new_visit,
        "in_progress_fieldwork_always_allowed": True,
        "historical_excluded_count": historical_excluded_count,
        "in_progress_or_pending_count": in_progress_or_pending_count,
        "cancelled_or_reversed_count": cancelled_or_reversed_count,
        "co_branded_visit_count": co_branded_counted,
        "next_upgrade_plan": next_upgrade_plan,
        "counted_visits": counted_visits,
    }


# ---------------------------------------------------------------------------
# Server-Enforced Guard Helpers (Direct API & Portal Parity)
# ---------------------------------------------------------------------------


def is_client_billing_launched() -> bool:
    """Return True only when client-facing subscription billing & plan gates
    are explicitly launched (`CLIENT_BILLING_LAUNCHED=True`). While False
    (default), billing stays strictly an Ignite-admin internal test surface
    and client workspaces are never blocked by self-serve plan upsells."""
    return bool(getattr(settings, "CLIENT_BILLING_LAUNCHED", False))


def require_new_visit_capacity(
    tenant: Tenant, *, is_ignite_admin: bool = False
) -> None:
    """Gate NEW billable visit bookings when a self-serve brand has reached
    its monthly completed-visit allowance without an approved overage
    agreement. Never called on in-progress fieldwork / recap submission!"""
    if not is_client_billing_launched():
        return
    ent = resolve_brand_entitlements(tenant)
    if ent.is_managed_sow:
        return
    if is_ignite_admin and ent.workspace_mode == WorkspaceProfile.MODE_MANAGED:
        return
    usage = compute_monthly_visit_usage(tenant)
    if not usage["can_book_new_visit"]:
        target = (usage["next_upgrade_plan"] or "core").upper()
        raise PlanEntitlementError(
            f"Monthly visit limit reached ({usage['completed_visits_used']}/"
            f"{usage['effective_visit_cap']} completed visits in "
            f"{usage['month_key']}). Already-scheduled fieldwork can still "
            f"submit recaps with no automatic charge. Upgrade to {target} or "
            f"approve a volume band to book new visits.",
            code="MONTHLY_VISIT_LIMIT_REACHED",
            required_plan=usage["next_upgrade_plan"] or "core",
        )


def require_plan_feature(
    tenant: Tenant,
    feature_attr: str,
    *,
    feature_label: str,
    required_plan: str,
) -> EffectiveBrandEntitlements:
    ent = resolve_brand_entitlements(tenant)
    if not is_client_billing_launched() or ent.is_managed_sow:
        return ent
    if not getattr(ent, feature_attr, False):
        raise PlanEntitlementError(
            f"{feature_label} requires the {required_plan.capitalize()} plan "
            f"(current plan: {ent.display_name}). Upgrade in Plans & Billing.",
            code="PLAN_FEATURE_GATED",
            required_plan=required_plan,
        )
    return ent


def require_active_program_capacity(
    tenant: Tenant, *, existing_event_type_id: int | None = None
) -> None:
    if not is_client_billing_launched():
        return
    ent = resolve_brand_entitlements(tenant)
    if ent.is_managed_sow or ent.max_active_programs is None:
        return
    qs = EventType.objects.filter(tenant=tenant)
    if existing_event_type_id:
        qs = qs.exclude(id=existing_event_type_id)
    if qs.count() >= ent.max_active_programs:
        raise PlanEntitlementError(
            f"The {ent.display_name} plan includes {ent.max_active_programs} "
            f"active program. Upgrade to Core ($299/mo) for unlimited branded programs.",
            code="PROGRAM_LIMIT_REACHED",
            required_plan=WorkspaceProfile.PLAN_CORE,
        )


def require_provider_feed_capacity(
    tenant: Tenant, *, candidate_provider: ProviderOrganization | None = None
) -> None:
    if not is_client_billing_launched():
        return
    ent = resolve_brand_entitlements(tenant)
    if ent.is_managed_sow or ent.max_operating_providers is None:
        return

    existing_provider_ids = (
        set(
            WorkspaceMembership.objects.filter(
                tenant=tenant, status=WorkspaceMembership.STATUS_ACTIVE
            )
            .exclude(provider_id=None)
            .values_list("provider_id", flat=True)
        )
        | set(
            ProviderAssignment.objects.filter(tenant=tenant)
            .exclude(status=ProviderAssignment.STATUS_REVOKED)
            .values_list("provider_id", flat=True)
        )
        | set(
            ProviderImportJob.objects.filter(
                tenant=tenant, is_historical=False
            ).values_list("provider_id", flat=True)
        )
    )
    if candidate_provider and candidate_provider.id in existing_provider_ids:
        return
    if len(existing_provider_ids) >= ent.max_operating_providers:
        next_plan = (
            WorkspaceProfile.PLAN_PRO
            if ent.max_operating_providers < 2
            else WorkspaceProfile.PLAN_SCALE
        )
        raise PlanEntitlementError(
            f"The {ent.display_name} plan supports up to "
            f"{ent.max_operating_providers} operating team/provider feed(s). "
            f"Upgrade to {next_plan.capitalize()} to connect additional agencies.",
            code="PROVIDER_LIMIT_REACHED",
            required_plan=next_plan,
        )


def require_pos_retailer_capacity(
    tenant: Tenant, *, retailer_banner: str
) -> None:
    if not is_client_billing_launched():
        return
    ent = require_plan_feature(
        tenant,
        "allows_retailer_pos_measurement",
        feature_label="Retailer POS sales matching",
        required_plan=WorkspaceProfile.PLAN_PRO,
    )
    if ent.is_managed_sow or ent.max_pos_retailers is None:
        return
    clean_banner = (retailer_banner or "").strip().lower()
    existing_banners = {
        (b or "").strip().lower()
        for b in RetailerSalesImport.objects.filter(
            tenant=tenant, is_active_version=True
        ).values_list("retailer_banner", flat=True)
        if (b or "").strip()
    }
    if clean_banner in existing_banners:
        return
    if len(existing_banners) >= ent.max_pos_retailers:
        raise PlanEntitlementError(
            f"The {ent.display_name} plan includes {ent.max_pos_retailers} "
            f"authorized retailer POS feed ({', '.join(sorted(existing_banners))}). "
            f"Contact Ignite for a Scale agreement to add additional retailers.",
            code="POS_RETAILER_LIMIT_REACHED",
            required_plan=WorkspaceProfile.PLAN_SCALE,
        )
