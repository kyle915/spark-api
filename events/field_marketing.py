"""Torch field marketing: plan events, hand them to Ignite, score the month.

Separate from retail sampling. The monthly targets come from the Field
Marketing KPI scorecard (full cans, 4oz pours, sponsorship days, retail
support, consumer emails). Planned and logged stay apart so an empty
month never looks like a real zero.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time
from zoneinfo import ZoneInfo

import strawberry
from asgiref.sync import sync_to_async
from django.db import transaction
from django.utils import timezone
from graphql import GraphQLError
from strawberry import relay

from django.core.exceptions import MultipleObjectsReturned

from events import models
from events.activity_log import _safe_log
from events.demo_cancel import request_display_code
from tenants.models import Tenant, TenantedUser
from utils.graphql.inputs import SparkGraphQLInput
from utils.graphql.mixins import SparkGraphQLMixin, resolve_id_to_int
from utils.graphql.permissions import StrictIsAuthenticated
from utils.utils import ROLE_ID, build_mutation_response

TORCH_SLUGS = frozenset({"torch", "torch-thc", "keee-torch-thc"})
REQUEST_TYPE_NAME = "Field Marketing"
_PT = ZoneInfo("America/Los_Angeles")

MARKETS: tuple[tuple[str, str, str, str, str], ...] = (
    ("miami", "Miami", "Alec Aparicio", "alec@torchdrinks.com", "786-348-9538"),
    ("orlando", "Orlando", "Octavius Jefferson", "octavius@torchdrinks.com", "772-646-2137"),
    ("houston", "Houston", "Victoria Quintana", "victoria@torchdrinks.com", "979-575-1622"),
    (
        "austin-dallas",
        "Austin / Dallas",
        "Brittany Senglin",
        "brittany@torchdrinks.com",
        "972-978-7253",
    ),
)

ACTIVITIES: dict[str, str] = {
    models.FieldMarketingEvent.ACTIVITY_FULL_CAN: "Full can samples",
    models.FieldMarketingEvent.ACTIVITY_POUR: "4oz pour samples",
    models.FieldMarketingEvent.ACTIVITY_SPONSORSHIP: "Local event sponsorship",
    models.FieldMarketingEvent.ACTIVITY_RETAIL_SUPPORT: "Retail activation / support",
    models.FieldMarketingEvent.ACTIVITY_EVENT_ACTIVATION: "Event activation / sponsorship",
    models.FieldMarketingEvent.ACTIVITY_GUERILLA: "Guerilla event",
    models.FieldMarketingEvent.ACTIVITY_PRODUCT_SEEDING: "Product seeding",
    models.FieldMarketingEvent.ACTIVITY_SALES_SUPPORT: "Sales support",
}

# Exact catalog product names. Onboard writes these; the form lists whatever
# of them exists on the Torch Product catalog.
FIELD_MARKETING_SKU_NAMES: tuple[str, ...] = (
    "Black Cherry 10mg",
    "Strawberry Lemonade 10mg",
    "Watermelon Limeade 10mg",
    "Nonactive",
)

SAMPLING_LABELS = {
    models.FieldMarketingEvent.SAMPLING_FULL_CAN: "Full can",
    models.FieldMarketingEvent.SAMPLING_POUR: "4oz pour",
}

SUPPORT_LABELS = {
    models.FieldMarketingEvent.SUPPORT_DISTRIBUTOR: "Distributor meeting",
    models.FieldMarketingEvent.SUPPORT_RETAIL_VISIT: "Retail visit",
    models.FieldMarketingEvent.SUPPORT_OTHER: "Other",
}

_SPONSORSHIP_ACTIVITIES = frozenset(
    {
        models.FieldMarketingEvent.ACTIVITY_SPONSORSHIP,
        models.FieldMarketingEvent.ACTIVITY_EVENT_ACTIVATION,
    }
)
_RETAIL_ACTIVITIES = frozenset(
    {
        models.FieldMarketingEvent.ACTIVITY_RETAIL_SUPPORT,
        models.FieldMarketingEvent.ACTIVITY_SALES_SUPPORT,
    }
)
_STAFFED_ACTIVITIES = frozenset(
    {
        models.FieldMarketingEvent.ACTIVITY_EVENT_ACTIVATION,
        models.FieldMarketingEvent.ACTIVITY_GUERILLA,
    }
)
_SKU_ACTIVITIES = frozenset(
    {
        models.FieldMarketingEvent.ACTIVITY_EVENT_ACTIVATION,
        models.FieldMarketingEvent.ACTIVITY_GUERILLA,
        models.FieldMarketingEvent.ACTIVITY_PRODUCT_SEEDING,
    }
)
# Tactic filter on the new four activities also includes the legacy rows
# they replaced, so an export of "guerilla" still shows old can/pour plans.
ACTIVITY_FILTERS: dict[str, frozenset[str]] = {
    models.FieldMarketingEvent.ACTIVITY_EVENT_ACTIVATION: frozenset(
        {
            models.FieldMarketingEvent.ACTIVITY_EVENT_ACTIVATION,
            models.FieldMarketingEvent.ACTIVITY_SPONSORSHIP,
        }
    ),
    models.FieldMarketingEvent.ACTIVITY_GUERILLA: frozenset(
        {
            models.FieldMarketingEvent.ACTIVITY_GUERILLA,
            models.FieldMarketingEvent.ACTIVITY_FULL_CAN,
            models.FieldMarketingEvent.ACTIVITY_POUR,
        }
    ),
    models.FieldMarketingEvent.ACTIVITY_PRODUCT_SEEDING: frozenset(
        {models.FieldMarketingEvent.ACTIVITY_PRODUCT_SEEDING}
    ),
    models.FieldMarketingEvent.ACTIVITY_SALES_SUPPORT: frozenset(
        {
            models.FieldMarketingEvent.ACTIVITY_SALES_SUPPORT,
            models.FieldMarketingEvent.ACTIVITY_RETAIL_SUPPORT,
        }
    ),
}


class FieldMarketingError(Exception):
    """A plan the caller can fix: wrong brand, missing address, bad date."""


@dataclass(frozen=True)
class _Market:
    key: str
    label: str
    manager: str
    email: str
    phone: str


def _markets() -> dict[str, _Market]:
    return {
        key: _Market(key, label, manager, email, phone)
        for key, label, manager, email, phone in MARKETS
    }


def is_torch_tenant(tenant) -> bool:
    slug = (getattr(tenant, "slug", None) or "").strip().lower()
    url = (getattr(tenant, "request_url_name", None) or "").strip().lower()
    return slug in TORCH_SLUGS or url in TORCH_SLUGS


def _month_window(month: str | None) -> tuple[date, date, str]:
    if month:
        raw = month.strip()
        try:
            year_s, mon_s = raw.split("-", 1)
            start = date(int(year_s), int(mon_s), 1)
        except (TypeError, ValueError):
            raise FieldMarketingError("Pick a month like 2026-09.") from None
    else:
        today = timezone.now().astimezone(_PT).date()
        start = today.replace(day=1)
    if start.month == 12:
        end = date(start.year + 1, 1, 1)
    else:
        end = date(start.year, start.month + 1, 1)
    return start, end, f"{start.year:04d}-{start.month:02d}"


def _quarter_window(quarter: str) -> tuple[date, date, str]:
    raw = (quarter or "").strip().upper().replace(" ", "")
    try:
        year_s, q_s = raw.split("-Q", 1)
        year = int(year_s)
        q = int(q_s)
    except (TypeError, ValueError):
        raise FieldMarketingError("Pick a quarter like 2026-Q3.") from None
    if q not in (1, 2, 3, 4):
        raise FieldMarketingError("Pick a quarter like 2026-Q3.")
    start_month = (q - 1) * 3 + 1
    start = date(year, start_month, 1)
    end_month = start_month + 3
    end = date(year + 1, 1, 1) if end_month > 12 else date(year, end_month, 1)
    return start, end, f"{year:04d}-Q{q}"


def sku_catalog(tenant) -> list[str]:
    """Field-marketing SKUs that exist on this brand's Product catalog."""
    present = set(
        models.Product.objects.filter(
            tenant=tenant, name__in=FIELD_MARKETING_SKU_NAMES
        ).values_list("name", flat=True)
    )
    return [name for name in FIELD_MARKETING_SKU_NAMES if name in present]


def _accepts_cans(event: models.FieldMarketingEvent) -> bool:
    activity = event.activity
    sampling = event.sampling_format or ""
    if activity in (
        models.FieldMarketingEvent.ACTIVITY_PRODUCT_SEEDING,
        models.FieldMarketingEvent.ACTIVITY_POUR,
    ) or activity in _RETAIL_ACTIVITIES:
        return False
    if activity == models.FieldMarketingEvent.ACTIVITY_GUERILLA:
        return sampling == models.FieldMarketingEvent.SAMPLING_FULL_CAN
    if activity == models.FieldMarketingEvent.ACTIVITY_EVENT_ACTIVATION:
        return sampling in ("", models.FieldMarketingEvent.SAMPLING_FULL_CAN)
    return activity in (
        models.FieldMarketingEvent.ACTIVITY_FULL_CAN,
        models.FieldMarketingEvent.ACTIVITY_SPONSORSHIP,
    )


def _accepts_pours(event: models.FieldMarketingEvent) -> bool:
    activity = event.activity
    sampling = event.sampling_format or ""
    if activity in (
        models.FieldMarketingEvent.ACTIVITY_PRODUCT_SEEDING,
        models.FieldMarketingEvent.ACTIVITY_FULL_CAN,
    ) or activity in _RETAIL_ACTIVITIES:
        return False
    if activity == models.FieldMarketingEvent.ACTIVITY_GUERILLA:
        return sampling == models.FieldMarketingEvent.SAMPLING_POUR
    if activity == models.FieldMarketingEvent.ACTIVITY_EVENT_ACTIVATION:
        return sampling in ("", models.FieldMarketingEvent.SAMPLING_POUR)
    return activity in (
        models.FieldMarketingEvent.ACTIVITY_POUR,
        models.FieldMarketingEvent.ACTIVITY_SPONSORSHIP,
    )


def _nonneg(value: int, label: str) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        raise FieldMarketingError(f"{label} has to be a whole number.") from None
    if number < 0 or number > 100_000:
        raise FieldMarketingError(f"{label} is out of range.")
    return number


def _user_can_pick_any_tenant(user) -> bool:
    """Staff / spark-admin can view a brand without a single membership get()."""
    if getattr(user, "is_staff", False) or getattr(user, "is_superuser", False):
        return True
    role = getattr(user, "role", None)
    slug = (getattr(role, "slug", None) or "").lower()
    return slug == "spark-admin"


def _parse_tenant_id(tenant_id) -> int | None:
    if tenant_id is None or tenant_id == "":
        return None
    try:
        return resolve_id_to_int(tenant_id)
    except (TypeError, ValueError, GraphQLError):
        return None


def _active_tenant_for_user(user, tenant_id=None):
    """Resolve the brand the caller is viewing — never blind ``user.tenant``.

    Spark admins have many TenantedUser rows, so ``user.tenant`` raises
    MultipleObjectsReturned. Callers pass the selected dashboard tenant
    (same pattern as recap / chat / tracker queries).
    """
    resolved_id = _parse_tenant_id(tenant_id)
    if resolved_id is not None:
        if _user_can_pick_any_tenant(user):
            try:
                return Tenant.objects.get(id=resolved_id)
            except Tenant.DoesNotExist:
                return None
        try:
            return (
                TenantedUser.objects.select_related("tenant")
                .get(user=user, tenant_id=resolved_id, is_active=True)
                .tenant
            )
        except TenantedUser.DoesNotExist:
            return None

    # No explicit id: only safe when the user has exactly one active membership.
    try:
        return (
            TenantedUser.objects.select_related("tenant")
            .get(user=user, is_active=True)
            .tenant
        )
    except (TenantedUser.DoesNotExist, MultipleObjectsReturned):
        return None


def _require_torch_user(user, tenant_id=None):
    if getattr(user, "role_id", None) == ROLE_ID.Ambassadors:
        raise FieldMarketingError("Field marketing is for the brand team.")
    tenant = _active_tenant_for_user(user, tenant_id=tenant_id)
    if tenant is None or not is_torch_tenant(tenant):
        raise FieldMarketingError("Switch to Torch THC. Field marketing is that program.")
    return tenant


def _serialize(event: models.FieldMarketingEvent):
    market = _markets().get(event.market)
    code = None
    if event.request_id:
        code = request_display_code(event.request_id)
    return {
        "id": str(event.uuid),
        "market": event.market,
        "market_label": market.label if market else event.market,
        "manager_name": market.manager if market else "",
        "activity": event.activity,
        "activity_label": ACTIVITIES.get(event.activity, event.activity),
        "name": event.name,
        "starts_on": event.starts_on.isoformat(),
        "days": event.days,
        "address": event.address or "",
        "notes": event.notes or "",
        "sampling_format": event.sampling_format or "",
        "sampling_label": SAMPLING_LABELS.get(event.sampling_format or "", ""),
        "support_type": event.support_type or "",
        "support_label": SUPPORT_LABELS.get(event.support_type or "", ""),
        "support_other": event.support_other or "",
        "sku_names": list(event.sku_names or []),
        "needs_field_support": bool(event.needs_field_support),
        "ambassador_count": event.ambassador_count or 0,
        "support_times": event.support_times or "",
        "support_scope": event.support_scope or "",
        "planned_full_cans": event.planned_full_cans,
        "planned_pour_samples": event.planned_pour_samples,
        "planned_emails": event.planned_emails,
        "logged_full_cans": event.logged_full_cans,
        "logged_pour_samples": event.logged_pour_samples,
        "logged_emails": event.logged_emails,
        "logged_days": event.logged_days,
        "logged_cases": event.logged_cases,
        "status": event.status,
        "request_code": code,
    }


def _sum_planned(events, field: str) -> int:
    return sum(getattr(event, field) or 0 for event in events)


def _sum_logged(events, field: str) -> int | None:
    vals = [getattr(event, field) for event in events if getattr(event, field) is not None]
    if not vals:
        return None
    return sum(vals)


MONTHLY_TARGETS = {
    "full_cans": 1152,
    "pour_samples": 3456,
    "sponsorship_days": 8,
    "retail_support": 4,
    "emails": 500,
}


def build_board(
    tenant,
    month: str | None = None,
    market: str | None = None,
    quarter: str | None = None,
    activity: str | None = None,
):
    """Projected KPIs sum planned FieldMarketingEvent fields for the filter.

    Not retail recaps. Omit month (or pass blank/"all") for every plan row.
    Monthly targets only apply when a single month is selected.
    """
    month_raw = (month or "").strip()
    quarter_raw = (quarter or "").strip()
    all_dates = not quarter_raw and (not month_raw or month_raw.lower() == "all")
    qs = models.FieldMarketingEvent.objects.filter(tenant=tenant)
    if quarter_raw:
        start, end, key = _quarter_window(quarter_raw)
        label = f"Q{key[-1]} {start.year}"
        apply_monthly_targets = False
        qs = qs.filter(starts_on__gte=start, starts_on__lt=end)
        order = ("starts_on", "id")
    elif all_dates:
        key = "all"
        label = "All plans"
        apply_monthly_targets = False
        # Soonest / newest first across the full slate.
        order = ("-starts_on", "-id")
    else:
        start, end, key = _month_window(month_raw)
        label = start.strftime("%B %Y")
        apply_monthly_targets = True
        qs = qs.filter(starts_on__gte=start, starts_on__lt=end)
        order = ("starts_on", "id")
    market_key = (market or "").strip().lower()
    if market_key:
        if market_key not in _markets():
            raise FieldMarketingError("Pick a market.")
        qs = qs.filter(market=market_key)
    activity_key = (activity or "").strip()
    if activity_key:
        allowed = ACTIVITY_FILTERS.get(activity_key)
        if allowed is None:
            raise FieldMarketingError("Pick a tactic.")
        qs = qs.filter(activity__in=allowed)
    events = list(qs.order_by(*order))
    # Cans and pours ignore product seeding. Planned sums stay on the columns
    # that were actually saved — seeding never writes those columns.
    can_events = [event for event in events if _accepts_cans(event)]
    pour_events = [event for event in events if _accepts_pours(event)]
    sponsorships = [
        event for event in events if event.activity in _SPONSORSHIP_ACTIVITIES
    ]
    retail = [event for event in events if event.activity in _RETAIL_ACTIVITIES]
    retail_logged = [event for event in retail if event.logged_at is not None]

    def _target(key_name: str) -> int:
        # Keep monthly target numbers for UI reference; clients hide the
        # "/ target" score when month is "all" so a multi-month rollup is
        # never scored against one month's bar.
        return MONTHLY_TARGETS[key_name]

    kpis = [
        {
            "key": "full_cans",
            "label": "Full can samples",
            "detail": "Product drops, donations, guerilla events. Drives trial and awareness.",
            "target": _target("full_cans"),
            "planned": _sum_planned(can_events, "planned_full_cans"),
            "logged": _sum_logged(can_events, "logged_full_cans"),
            "unit": "cans",
        },
        {
            "key": "pour_samples",
            "label": "4oz pour samples",
            "detail": "Local sponsorships, events, festivals. 3,456 pours is 1,152 full cans.",
            "target": _target("pour_samples"),
            "planned": _sum_planned(pour_events, "planned_pour_samples"),
            "logged": _sum_logged(pour_events, "logged_pour_samples"),
            "unit": "pours",
        },
        {
            "key": "sponsorship_days",
            "label": "Local event sponsorships",
            "detail": "Minimum days. Builds meaningful local presence.",
            "target": _target("sponsorship_days"),
            "planned": sum(event.days or 0 for event in sponsorships),
            "logged": _sum_logged(sponsorships, "logged_days"),
            "unit": "days",
        },
        {
            "key": "retail_support",
            "label": "Retail activations / support",
            "detail": "On-premise support, retail check-in, sales and DP meetings.",
            "target": _target("retail_support"),
            "planned": len(retail),
            "logged": len(retail_logged) if retail_logged else None,
            "unit": "activations",
        },
        {
            "key": "emails",
            "label": "Consumer data captured",
            "detail": "Email addresses. Builds an addressable audience.",
            "target": _target("emails"),
            "planned": _sum_planned(events, "planned_emails"),
            "logged": _sum_logged(events, "logged_emails"),
            "unit": "emails",
        },
    ]
    # Silence unused when all_dates — targets still returned for FE caption.
    _ = apply_monthly_targets
    return {
        "available": True,
        "month": key,
        "month_label": label,
        "managers": [
            {
                "market": key_,
                "market_label": label,
                "name": manager,
                "email": email,
                "phone": phone,
            }
            for key_, label, manager, email, phone in MARKETS
        ],
        "kpis": kpis,
        "events": [_serialize(event) for event in events],
        "skus": sku_catalog(tenant),
    }


def _clean_skus(tenant, raw, activity: str) -> list[str]:
    if raw is None:
        raw = []
    if not isinstance(raw, (list, tuple)):
        raise FieldMarketingError("Pick SKUs from the catalog.")
    names: list[str] = []
    for item in raw:
        name = str(item or "").strip()
        if not name:
            continue
        if name not in FIELD_MARKETING_SKU_NAMES:
            raise FieldMarketingError("Pick a Torch field marketing SKU.")
        if name not in names:
            names.append(name)
    if activity in _SKU_ACTIVITIES and not names:
        raise FieldMarketingError("Pick at least one SKU.")
    if activity not in _SKU_ACTIVITIES:
        return []
    present = set(sku_catalog(tenant))
    missing = [name for name in names if name not in present]
    if missing:
        raise FieldMarketingError("Those SKUs aren't in the Torch catalog yet.")
    return names


def _clean_plan(data: dict, tenant) -> dict:
    markets = _markets()
    market = (data.get("market") or "").strip().lower()
    if market not in markets:
        raise FieldMarketingError("Pick a market.")
    activity = (data.get("activity") or "").strip()
    if activity not in ACTIVITIES:
        raise FieldMarketingError("Pick the kind of field marketing.")
    name = (data.get("name") or "").strip()
    if len(name) < 2:
        raise FieldMarketingError("Name the event.")
    try:
        starts_on = date.fromisoformat((data.get("starts_on") or "").strip())
    except ValueError:
        raise FieldMarketingError("Pick the date.") from None
    days = _nonneg(data.get("days") or 1, "Days")
    if activity in _SPONSORSHIP_ACTIVITIES and days < 1:
        raise FieldMarketingError("Sponsorships need at least one day.")
    if days > 31:
        raise FieldMarketingError("Days has to be 31 or fewer.")
    if activity not in _SPONSORSHIP_ACTIVITIES:
        days = 1
    sampling = (data.get("sampling_format") or "").strip()
    if activity == models.FieldMarketingEvent.ACTIVITY_GUERILLA:
        if sampling not in (
            models.FieldMarketingEvent.SAMPLING_FULL_CAN,
            models.FieldMarketingEvent.SAMPLING_POUR,
        ):
            raise FieldMarketingError("Pick full cans or 4oz pours.")
    elif activity == models.FieldMarketingEvent.ACTIVITY_EVENT_ACTIVATION:
        if sampling and sampling not in (
            models.FieldMarketingEvent.SAMPLING_FULL_CAN,
            models.FieldMarketingEvent.SAMPLING_POUR,
        ):
            raise FieldMarketingError("Pick full cans or 4oz pours.")
    else:
        sampling = ""
    support_type = (data.get("support_type") or "").strip()
    support_other = (data.get("support_other") or "").strip()
    if activity == models.FieldMarketingEvent.ACTIVITY_SALES_SUPPORT:
        if support_type not in SUPPORT_LABELS:
            raise FieldMarketingError("Pick the kind of sales support.")
        if support_type == models.FieldMarketingEvent.SUPPORT_OTHER and len(support_other) < 2:
            raise FieldMarketingError("Say what the other sales support is.")
    else:
        support_type = ""
        support_other = ""
    needs_support = bool(data.get("needs_field_support"))
    ambassador_count = _nonneg(data.get("ambassador_count") or 0, "Brand ambassadors")
    support_times = (data.get("support_times") or "").strip()
    support_scope = (data.get("support_scope") or "").strip()
    if activity not in _STAFFED_ACTIVITIES:
        needs_support = False
        ambassador_count = 0
        support_times = ""
        support_scope = ""
    elif needs_support and ambassador_count < 1:
        raise FieldMarketingError("Say how many brand ambassadors you need.")
    emails = _nonneg(data.get("planned_emails") or 0, "Emails")
    # New activities do not take a forecast of cans or pours. Legacy rows still do.
    legacy = activity in (
        models.FieldMarketingEvent.ACTIVITY_FULL_CAN,
        models.FieldMarketingEvent.ACTIVITY_POUR,
        models.FieldMarketingEvent.ACTIVITY_SPONSORSHIP,
        models.FieldMarketingEvent.ACTIVITY_RETAIL_SUPPORT,
    )
    if legacy:
        full_cans = _nonneg(data.get("planned_full_cans") or 0, "Full cans")
        pours = _nonneg(data.get("planned_pour_samples") or 0, "Pour samples")
        if activity == models.FieldMarketingEvent.ACTIVITY_FULL_CAN:
            pours = 0
        elif activity == models.FieldMarketingEvent.ACTIVITY_POUR:
            full_cans = 0
        elif activity == models.FieldMarketingEvent.ACTIVITY_RETAIL_SUPPORT:
            full_cans = 0
            pours = 0
    else:
        full_cans = 0
        pours = 0
    return {
        "market": market,
        "activity": activity,
        "name": name[:255],
        "starts_on": starts_on,
        "days": days,
        "address": (data.get("address") or "").strip(),
        "notes": (data.get("notes") or "").strip(),
        "sampling_format": sampling,
        "support_type": support_type,
        "support_other": support_other[:255],
        "sku_names": _clean_skus(tenant, data.get("sku_names"), activity),
        "needs_field_support": needs_support,
        "ambassador_count": ambassador_count,
        "support_times": support_times[:255],
        "support_scope": support_scope,
        "planned_full_cans": full_cans,
        "planned_pour_samples": pours,
        "planned_emails": emails,
    }


def _request_notes(event: models.FieldMarketingEvent) -> str:
    market = _markets().get(event.market)
    lines = [
        "Field marketing — separate from retail sampling.",
        f"Market: {market.label if market else event.market}",
        f"Manager: {market.manager if market else '—'}"
        + (f" · {market.email} · {market.phone}" if market else ""),
        f"Activity: {ACTIVITIES.get(event.activity, event.activity)}",
        f"Days: {event.days}",
    ]
    if event.sampling_format:
        lines.append(
            f"Sampling: {SAMPLING_LABELS.get(event.sampling_format, event.sampling_format)}"
        )
    if event.sku_names:
        lines.append("SKUs: " + ", ".join(event.sku_names))
    if event.support_type:
        label = SUPPORT_LABELS.get(event.support_type, event.support_type)
        if event.support_type == models.FieldMarketingEvent.SUPPORT_OTHER and event.support_other:
            label = f"{label}: {event.support_other}"
        lines.append(f"Sales support: {label}")
    if event.needs_field_support:
        lines.append(f"Field support: {event.ambassador_count} brand ambassadors")
        if event.support_times:
            lines.append(f"Times: {event.support_times}")
        if event.support_scope:
            lines.append(f"Scope: {event.support_scope}")
    if event.planned_full_cans:
        lines.append(f"Planned full cans: {event.planned_full_cans}")
    if event.planned_pour_samples:
        lines.append(f"Planned 4oz pours: {event.planned_pour_samples}")
    if event.planned_emails:
        lines.append(f"Planned emails: {event.planned_emails}")
    if event.notes:
        lines.append(event.notes)
    return "\n".join(lines)


def _ensure_request_type(tenant, actor):
    existing = models.RequestType.objects.filter(
        tenant=tenant, name=REQUEST_TYPE_NAME
    ).first()
    if existing:
        return existing
    return models.RequestType.objects.create(
        tenant=tenant,
        name=REQUEST_TYPE_NAME,
        created_by=actor,
    )


def _submit_event(event: models.FieldMarketingEvent, actor) -> models.FieldMarketingEvent:
    if event.status == models.FieldMarketingEvent.STATUS_SUBMITTED and event.request_id:
        return event
    address = (event.address or "").strip()
    if not address:
        raise FieldMarketingError("Add an address before submitting this to Ignite.")
    request_type = _ensure_request_type(event.tenant, actor)
    when = timezone.make_aware(
        datetime.combine(event.starts_on, time(12, 0)),
        timezone.get_current_timezone(),
    )
    request = models.Request.objects.create(
        name=f"Field marketing · {event.name}"[:255],
        date=when,
        address=address,
        notes=_request_notes(event),
        requestor_email=(getattr(actor, "email", None) or "")[:254] or None,
        request_type=request_type,
        tenant=event.tenant,
        created_by=actor,
        scheduling_status=models.SchedulingStatus.NEEDS_SCHEDULING,
    )
    _safe_log(
        request=request,
        kind=models.RequestActivityLog.KIND_CREATED,
        actor_user=actor,
        summary=f"Field marketing submitted: {event.name}"[:512],
        metadata={"field_marketing_event": str(event.uuid), "market": event.market},
    )
    event.request = request
    event.status = models.FieldMarketingEvent.STATUS_SUBMITTED
    event.save(update_fields=["request", "status", "updated_at"])
    return event


@transaction.atomic
def plan_event(*, user, payload: dict, submit: bool, tenant_id=None) -> models.FieldMarketingEvent:
    tenant = _require_torch_user(user, tenant_id=tenant_id)
    cleaned = _clean_plan(payload, tenant)
    event = models.FieldMarketingEvent.objects.create(
        tenant=tenant,
        created_by=user,
        status=models.FieldMarketingEvent.STATUS_PLANNED,
        **cleaned,
    )
    if submit:
        event = _submit_event(event, user)
    return event


@transaction.atomic
def submit_event(*, user, event_id: str, tenant_id=None) -> models.FieldMarketingEvent:
    tenant = _require_torch_user(user, tenant_id=tenant_id)
    event = models.FieldMarketingEvent.objects.filter(
        tenant=tenant, uuid=event_id
    ).first()
    if event is None:
        raise FieldMarketingError("That field marketing event isn't on Torch.")
    return _submit_event(event, user)


@transaction.atomic
def log_results(*, user, event_id: str, payload: dict, tenant_id=None) -> models.FieldMarketingEvent:
    tenant = _require_torch_user(user, tenant_id=tenant_id)
    event = models.FieldMarketingEvent.objects.filter(
        tenant=tenant, uuid=event_id
    ).first()
    if event is None:
        raise FieldMarketingError("That field marketing event isn't on Torch.")
    touched = False
    checks = (
        ("logged_full_cans", "Full cans", _accepts_cans(event)),
        ("logged_pour_samples", "Pour samples", _accepts_pours(event)),
        ("logged_emails", "Emails", True),
        ("logged_days", "Days", event.activity in _SPONSORSHIP_ACTIVITIES),
        (
            "logged_cases",
            "Cases seeded",
            event.activity == models.FieldMarketingEvent.ACTIVITY_PRODUCT_SEEDING,
        ),
    )
    update_fields = ["logged_at", "updated_at"]
    for field, label, allowed in checks:
        raw = payload.get(field)
        if raw is None or not allowed:
            continue
        setattr(event, field, _nonneg(raw, label))
        update_fields.append(field)
        touched = True
    if not touched and event.activity in _RETAIL_ACTIVITIES:
        touched = True
    if not touched:
        raise FieldMarketingError("Log a result, or mark the sales support done.")
    event.logged_at = timezone.now()
    event.save(update_fields=update_fields)
    return event


def empty_board(month: str | None = None):
    month_raw = (month or "").strip()
    if not month_raw or month_raw.lower() == "all":
        key, label = "all", "All plans"
    else:
        start, _end, key = _month_window(month_raw)
        label = start.strftime("%B %Y")
    return {
        "available": False,
        "month": key,
        "month_label": label,
        "managers": [],
        "kpis": [],
        "events": [],
        "skus": [],
    }


@strawberry.type(name="FieldMarketingManager")
class FieldMarketingManagerType:
    market: str
    market_label: str
    name: str
    email: str
    phone: str


@strawberry.type(name="FieldMarketingKpi")
class FieldMarketingKpiType:
    key: str
    label: str
    detail: str
    target: int
    planned: int
    logged: int | None
    unit: str


@strawberry.type(name="FieldMarketingEvent")
class FieldMarketingEventType:
    id: str
    market: str
    market_label: str
    manager_name: str
    activity: str
    activity_label: str
    name: str
    starts_on: str
    days: int
    address: str
    notes: str
    sampling_format: str
    sampling_label: str
    support_type: str
    support_label: str
    support_other: str
    sku_names: list[str]
    needs_field_support: bool
    ambassador_count: int
    support_times: str
    support_scope: str
    planned_full_cans: int
    planned_pour_samples: int
    planned_emails: int
    logged_full_cans: int | None
    logged_pour_samples: int | None
    logged_emails: int | None
    logged_days: int | None
    logged_cases: int | None
    status: str
    request_code: str | None


@strawberry.type(name="FieldMarketingBoard")
class FieldMarketingBoardType:
    available: bool
    month: str
    month_label: str
    managers: list[FieldMarketingManagerType]
    kpis: list[FieldMarketingKpiType]
    events: list[FieldMarketingEventType]
    skus: list[str]


def _board_type(payload: dict) -> FieldMarketingBoardType:
    return FieldMarketingBoardType(
        available=payload["available"],
        month=payload["month"],
        month_label=payload["month_label"],
        managers=[FieldMarketingManagerType(**row) for row in payload["managers"]],
        kpis=[FieldMarketingKpiType(**row) for row in payload["kpis"]],
        events=[FieldMarketingEventType(**row) for row in payload["events"]],
        skus=list(payload.get("skus") or []),
    )


def _event_type(event: models.FieldMarketingEvent) -> FieldMarketingEventType:
    return FieldMarketingEventType(**_serialize(event))


@strawberry.input
class PlanFieldMarketingInput(SparkGraphQLInput):
    market: str
    activity: str
    name: str
    starts_on: str
    days: int = 1
    address: str = ""
    notes: str = ""
    planned_full_cans: int = 0
    planned_pour_samples: int = 0
    planned_emails: int = 0
    sampling_format: str = ""
    support_type: str = ""
    support_other: str = ""
    sku_names: list[str] | None = None
    needs_field_support: bool = False
    ambassador_count: int = 0
    support_times: str = ""
    support_scope: str = ""
    submit: bool = False
    tenant_id: strawberry.ID | None = None


@strawberry.input
class SubmitFieldMarketingInput(SparkGraphQLInput):
    event_id: str
    tenant_id: strawberry.ID | None = None


@strawberry.input
class LogFieldMarketingInput(SparkGraphQLInput):
    event_id: str
    logged_full_cans: int | None = None
    logged_pour_samples: int | None = None
    logged_emails: int | None = None
    logged_days: int | None = None
    logged_cases: int | None = None
    tenant_id: strawberry.ID | None = None


@strawberry.type
class FieldMarketingEventResponse:
    success: bool
    message: str
    client_mutation_id: strawberry.ID | None = None
    event: FieldMarketingEventType | None = None


@strawberry.type
class FieldMarketingQueries:
    @strawberry.field(permission_classes=[StrictIsAuthenticated])
    async def field_marketing(
        self,
        info: strawberry.Info,
        month: str | None = None,
        tenant_id: strawberry.ID | None = None,
        market: str | None = None,
        quarter: str | None = None,
        activity: str | None = None,
    ) -> FieldMarketingBoardType:
        user = await SparkGraphQLMixin().get_user(info)
        tenant = await sync_to_async(_active_tenant_for_user)(user, tenant_id)
        if tenant is None or not is_torch_tenant(tenant):
            return _board_type(empty_board(month=month))
        try:
            payload = await sync_to_async(build_board)(
                tenant,
                month=month,
                market=market,
                quarter=quarter,
                activity=activity,
            )
        except FieldMarketingError as exc:
            raise GraphQLError(str(exc)) from exc
        return _board_type(payload)


def _payload_from_plan(input: PlanFieldMarketingInput) -> dict:
    return {
        "market": input.market,
        "activity": input.activity,
        "name": input.name,
        "starts_on": input.starts_on,
        "days": input.days,
        "address": input.address,
        "notes": input.notes,
        "planned_full_cans": input.planned_full_cans,
        "planned_pour_samples": input.planned_pour_samples,
        "planned_emails": input.planned_emails,
        "sampling_format": input.sampling_format,
        "support_type": input.support_type,
        "support_other": input.support_other,
        "sku_names": list(input.sku_names or []),
        "needs_field_support": input.needs_field_support,
        "ambassador_count": input.ambassador_count,
        "support_times": input.support_times,
        "support_scope": input.support_scope,
    }


@strawberry.type
class FieldMarketingMutations:
    @relay.mutation(permission_classes=[StrictIsAuthenticated])
    async def plan_field_marketing(
        self, info: strawberry.Info, input: PlanFieldMarketingInput
    ) -> FieldMarketingEventResponse:
        user = await SparkGraphQLMixin().get_user(info)
        try:
            event = await sync_to_async(plan_event)(
                user=user,
                payload=_payload_from_plan(input),
                submit=bool(input.submit),
                tenant_id=input.tenant_id,
            )
        except FieldMarketingError as exc:
            raise GraphQLError(str(exc)) from exc
        if input.submit and event.request_id:
            await _notify_ignite(event)
        message = (
            "Submitted to Ignite. It is on the tracker as Field Marketing."
            if input.submit
            else "Saved to the plan."
        )
        return build_mutation_response(
            FieldMarketingEventResponse,
            success=True,
            message=message,
            input_obj=input,
            event=_event_type(event),
        )

    @relay.mutation(permission_classes=[StrictIsAuthenticated])
    async def submit_field_marketing(
        self, info: strawberry.Info, input: SubmitFieldMarketingInput
    ) -> FieldMarketingEventResponse:
        user = await SparkGraphQLMixin().get_user(info)
        try:
            event = await sync_to_async(submit_event)(
                user=user,
                event_id=input.event_id,
                tenant_id=input.tenant_id,
            )
        except FieldMarketingError as exc:
            raise GraphQLError(str(exc)) from exc
        if event.request_id:
            await _notify_ignite(event)
        return build_mutation_response(
            FieldMarketingEventResponse,
            success=True,
            message="Submitted to Ignite. It is on the tracker as Field Marketing.",
            input_obj=input,
            event=_event_type(event),
        )

    @relay.mutation(permission_classes=[StrictIsAuthenticated])
    async def log_field_marketing(
        self, info: strawberry.Info, input: LogFieldMarketingInput
    ) -> FieldMarketingEventResponse:
        user = await SparkGraphQLMixin().get_user(info)
        try:
            event = await sync_to_async(log_results)(
                user=user,
                event_id=input.event_id,
                payload={
                    "logged_full_cans": input.logged_full_cans,
                    "logged_pour_samples": input.logged_pour_samples,
                    "logged_emails": input.logged_emails,
                    "logged_days": input.logged_days,
                    "logged_cases": input.logged_cases,
                },
                tenant_id=input.tenant_id,
            )
        except FieldMarketingError as exc:
            raise GraphQLError(str(exc)) from exc
        return build_mutation_response(
            FieldMarketingEventResponse,
            success=True,
            message="Logged. The scorecard uses this number.",
            input_obj=input,
            event=_event_type(event),
        )


async def _notify_ignite(event: models.FieldMarketingEvent) -> None:
    request = await sync_to_async(lambda: event.request)()
    if request is None:
        return
    from events.mutations import _notify_spark_admins_for_client_request

    await _notify_spark_admins_for_client_request(request, None)
