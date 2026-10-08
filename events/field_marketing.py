"""Torch field marketing: plan events, hand them to Ignite, score the month.

Separate from retail sampling. The monthly targets come from the Field
Marketing KPI scorecard (full cans, 4oz pours, sponsorship days, retail
support, consumer emails). Planned and logged stay apart so an empty
month never looks like a real zero.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from functools import partial
from zoneinfo import ZoneInfo

import strawberry
from asgiref.sync import sync_to_async
from django.db import transaction
from django.utils import timezone
from graphql import GraphQLError
from strawberry import relay

from django.core.exceptions import MultipleObjectsReturned, ValidationError

from ambassadors.models import Attendance
from events import models
from events.activity_log import _safe_log
from events.demo_cancel import request_display_code
from events.field_marketing_mail import notify_plan_deleted, notify_plan_submitted
from events.request_soft_delete import soft_delete_request
from events.plan_actuals import plan_actuals, result_counts
from recaps import plan_metrics as pm
from recaps.models import CustomRecap, Recap
from tenants.models import Tenant, TenantedUser
from utils.graphql.inputs import SparkGraphQLInput
from utils.graphql.mixins import SparkGraphQLMixin, resolve_id_to_int
from utils.graphql.permissions import StrictIsAuthenticated
from utils.tz import resolve_zoneinfo
from utils.utils import ROLE_ID, build_mutation_response

logger = logging.getLogger(__name__)

TORCH_SLUGS = frozenset({"torch", "torch-thc", "keee-torch-thc"})
# Staffed plan rows book as Event Activation — not the old catch-all
# "Field Marketing" type, and never auto-approve / retail-sheet.
EVENT_ACTIVATION_TYPE_NAME = "Event Activation"
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

MARKET_TIMEZONES: dict[str, str] = {
    "miami": "America/New_York",
    "orlando": "America/New_York",
    "houston": "America/Chicago",
    "austin-dallas": "America/Chicago",
}

# The restore an Undo toast fires. Older deletes need an Ignite admin.
UNDO_WINDOW = timedelta(minutes=15)

DELETE_REMOVE = "remove"
DELETE_CANCEL_REQUEST = "cancel_request"
DELETE_KEEP_REQUEST = "keep_request"

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


def _market_zone(market: str) -> ZoneInfo:
    return ZoneInfo(MARKET_TIMEZONES.get(market, "America/New_York"))


def _market_today(market: str, now: datetime | None = None) -> date:
    return (now or timezone.now()).astimezone(_market_zone(market)).date()


def _parse_clock(raw, label: str) -> time | None:
    text = str(raw or "").strip()
    if not text:
        return None
    try:
        hour_s, minute_s = text.split(":", 1)
        return time(int(hour_s), int(minute_s[:2]))
    except (TypeError, ValueError):
        raise FieldMarketingError(f"Pick a {label} time.") from None


def _clock_label(value: time) -> str:
    hour = value.hour % 12 or 12
    return f"{hour}:{value.minute:02d} {'AM' if value.hour < 12 else 'PM'}"


def _times_label(start: time | None, end: time | None) -> str:
    if start and end:
        return f"{_clock_label(start)} – {_clock_label(end)}"
    if start:
        return f"Starts {_clock_label(start)}"
    return ""


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


def _live_request(event: models.FieldMarketingEvent) -> models.Request | None:
    request = event.request if event.request_id else None
    if request is None or request.deleted_at is not None:
        return None
    return request


def _can_book(event: models.FieldMarketingEvent) -> bool:
    """Book (or confirm) is still open: never booked, or the booking was cancelled."""
    if not _should_create_request(event):
        return event.status != models.FieldMarketingEvent.STATUS_SUBMITTED
    return _live_request(event) is None


def _locked_request_ids(request_ids, now: datetime) -> set[int]:
    """Requests with a recap, a clock-in, or an event that already started."""
    ids = [rid for rid in set(request_ids) if rid]
    if not ids:
        return set()
    locked: set[int] = set()
    for qs in (
        Recap.objects.filter(event__request_id__in=ids),
        CustomRecap.objects.filter(event__request_id__in=ids),
        Attendance.objects.filter(event__request_id__in=ids),
        models.Event.objects.filter(request_id__in=ids, start_time__lte=now),
    ):
        field = "request_id" if qs.model is models.Event else "event__request_id"
        locked.update(qs.values_list(field, flat=True))
    return locked


def _request_started(request: models.Request, event: models.FieldMarketingEvent, now: datetime) -> bool:
    if request.start_time:
        return request.start_time <= now
    today = _market_today(event.market, now)
    if request.date:
        return request.date.astimezone(_market_zone(event.market)).date() < today
    return event.starts_on < today


def _delete_effect(event: models.FieldMarketingEvent, locked: set[int], now: datetime) -> str:
    request = _live_request(event)
    if request is None:
        return DELETE_REMOVE
    if request.id in locked or _request_started(request, event, now):
        return DELETE_KEEP_REQUEST
    return DELETE_CANCEL_REQUEST


def _person_name(user) -> str:
    if user is None:
        return ""
    return (user.get_full_name() or "").strip() or (user.email or "").strip()


def _serialize(
    event: models.FieldMarketingEvent,
    locked: set[int] | None = None,
    now: datetime | None = None,
    results: tuple[int, int] = (0, 0),
):
    market = _markets().get(event.market)
    now = now or timezone.now()
    if locked is None:
        locked = _locked_request_ids([event.request_id], now)
    code = None
    if event.request_id:
        code = request_display_code(event.request_id)
    live_request = _live_request(event)
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
        "start_time": event.start_time.strftime("%H:%M") if event.start_time else "",
        "end_time": event.end_time.strftime("%H:%M") if event.end_time else "",
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
        "request_uuid": str(live_request.uuid) if live_request else None,
        "request_cancelled": bool(event.request_id and live_request is None),
        "can_book": _can_book(event),
        "delete_effect": _delete_effect(event, locked, now),
        "deleted_at": event.deleted_at.isoformat() if event.deleted_at else None,
        "deleted_by_name": _person_name(event.deleted_by) if event.deleted_at else "",
        "delete_cancelled_request": bool(event.delete_cancelled_request),
        "results_counted": results[0],
        "results_pending": results[1],
    }


def _sum_planned(events, field: str) -> int:
    return sum(getattr(event, field) or 0 for event in events)


def _sum_logged(events, field: str, actuals: dict | None = None) -> int | None:
    """Manual log wins per plan; otherwise the attached recaps' actual."""
    vals = []
    for event in events:
        value = getattr(event, field)
        if value is None and actuals:
            value = actuals.get(event.id, {}).get(field)
        if value is not None:
            vals.append(value)
    if not vals:
        return None
    return sum(vals)


def _recap_logged(event: models.FieldMarketingEvent, totals: dict) -> dict[str, int]:
    """Attached-recap actuals in the scorecard's logged fields.

    Cans count only on full-can-only plans: on a pour plan, cans sampled
    are cans opened for pours, not full cans handed out.
    """
    out: dict[str, int] = {}
    emails = totals.get(pm.EMAILS_COLLECTED)
    if emails and emails.value is not None:
        out["logged_emails"] = int(emails.value)
    samples = totals.get(pm.SAMPLES_BY_SKU)
    if (
        samples
        and samples.value is not None
        and _accepts_cans(event)
        and not _accepts_pours(event)
    ):
        out["logged_full_cans"] = int(samples.value)
    cases = totals.get(pm.CASES_DROPPED)
    if (
        cases
        and cases.value is not None
        and event.activity == models.FieldMarketingEvent.ACTIVITY_PRODUCT_SEEDING
    ):
        out["logged_cases"] = int(cases.value)
    return out


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
    include_deleted: bool = False,
    include_pending: bool = False,
):
    """Projected KPIs sum planned FieldMarketingEvent fields for the filter.

    Not retail recaps. Omit month (or pass blank/"all") for every plan row.
    Monthly targets only apply when a single month is selected. Deleted plans
    never count; ``include_deleted`` lists them separately for restore. Logged uses
    the manual log, else the recaps ops pushed to the plan.
    """
    month_raw = (month or "").strip()
    quarter_raw = (quarter or "").strip()
    all_dates = not quarter_raw and (not month_raw or month_raw.lower() == "all")
    qs = models.FieldMarketingEvent.objects.filter(tenant=tenant).select_related(
        "request", "deleted_by"
    )
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
    events = list(qs.filter(deleted_at__isnull=True).order_by(*order))
    deleted = (
        list(qs.filter(deleted_at__isnull=False).order_by("-deleted_at", "-id"))
        if include_deleted
        else []
    )
    now = timezone.now()
    locked = _locked_request_ids([event.request_id for event in events], now)
    event_ids = [event.id for event in events]
    counts = result_counts(event_ids)
    by_id = {event.id: event for event in events}
    actuals = {
        plan_id: _recap_logged(by_id[plan_id], totals)
        for plan_id, totals in plan_actuals(
            [plan_id for plan_id, (counted, _) in counts.items() if counted]
        ).items()
    }
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
            "logged": _sum_logged(can_events, "logged_full_cans", actuals),
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
            "logged": _sum_logged(events, "logged_emails", actuals),
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
        "events": [
            _serialize(
                event,
                locked,
                now,
                (
                    counts.get(event.id, (0, 0))[0],
                    counts.get(event.id, (0, 0))[1] if include_pending else 0,
                ),
            )
            for event in events
        ],
        "deleted_events": [_serialize(event, set(), now) for event in deleted],
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
    start_time = _parse_clock(data.get("start_time"), "start")
    end_time = _parse_clock(data.get("end_time"), "end")
    if activity not in _STAFFED_ACTIVITIES:
        start_time = None
        end_time = None
    if end_time and not start_time:
        raise FieldMarketingError("Pick a start time too.")
    if start_time:
        support_times = _times_label(start_time, end_time)
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
        "start_time": start_time,
        "end_time": end_time,
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
        # Same "BA count: N" line the request forms write; the approve page reads it.
        lines.append(f"BA count: {event.ambassador_count}")
    if event.support_times:
        lines.append(f"Times: {event.support_times}")
    if event.needs_field_support and event.support_scope:
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


def _resolve_event_activation_type(tenant) -> models.RequestType:
    """Lookup-only — Torch onboard seeds Event Activation; don't invent a type."""
    existing = models.RequestType.objects.filter(
        tenant=tenant, name=EVENT_ACTIVATION_TYPE_NAME
    ).first()
    if existing:
        return existing
    raise FieldMarketingError(
        "Torch is missing an Event Activation request type. "
        "Ask Ignite to add it before booking from Plans."
    )


def _should_create_request(event: models.FieldMarketingEvent) -> bool:
    """Staffed activations book with Ignite; seeding / sales support stay plan+log."""
    activity = event.activity
    if activity in (
        models.FieldMarketingEvent.ACTIVITY_EVENT_ACTIVATION,
        models.FieldMarketingEvent.ACTIVITY_SPONSORSHIP,
    ):
        return True
    if activity in (
        models.FieldMarketingEvent.ACTIVITY_GUERILLA,
        models.FieldMarketingEvent.ACTIVITY_FULL_CAN,
        models.FieldMarketingEvent.ACTIVITY_POUR,
    ):
        return bool(event.needs_field_support)
    return False


def _market_manager_name(event: models.FieldMarketingEvent) -> str:
    market = _markets().get(event.market)
    return market.manager if market else ""


def _activation_team_details(event: models.FieldMarketingEvent) -> str:
    parts: list[str] = []
    if event.support_scope:
        parts.append(event.support_scope.strip())
    if event.sampling_format:
        parts.append(
            f"Sampling: {SAMPLING_LABELS.get(event.sampling_format, event.sampling_format)}"
        )
    if event.days and event.days > 1:
        parts.append(f"{event.days} days")
    if event.sku_names:
        parts.append("SKUs: " + ", ".join(event.sku_names))
    if event.needs_field_support and event.ambassador_count:
        parts.append(f"{event.ambassador_count} brand ambassadors")
    return " · ".join(parts)[:2000]


def _attach_request_products(request: models.Request, event: models.FieldMarketingEvent, actor) -> None:
    names = [n for n in (event.sku_names or []) if (n or "").strip()]
    if not names:
        return
    products = list(
        models.Product.objects.filter(tenant=event.tenant, name__in=names)
    )
    by_name = {p.name: p for p in products}
    for name in names:
        product = by_name.get(name)
        if product is None:
            continue
        models.RequestProduct.objects.get_or_create(
            request=request,
            product=product,
            defaults={"created_by": actor, "tenant": event.tenant},
        )


def _date_range_label(event: models.FieldMarketingEvent) -> str:
    start = event.starts_on
    days = event.days or 1
    if days <= 1:
        return start.strftime("%a, %b %-d, %Y")
    end = start + timedelta(days=days - 1)
    return f"{start.strftime('%a, %b %-d')} – {end.strftime('%a, %b %-d, %Y')} ({days} days)"


def _summary_rows(event: models.FieldMarketingEvent) -> list[tuple[str, str]]:
    """Label/value rows the plan-submitted emails show."""
    market = _markets().get(event.market)
    tactic = ACTIVITIES.get(event.activity, event.activity)
    if event.sampling_format:
        tactic += f" · {SAMPLING_LABELS.get(event.sampling_format, event.sampling_format)}"
    rows = [
        ("Tactic", tactic),
        ("Date", _date_range_label(event)),
        (
            "Market",
            f"{market.label} · {market.manager}" if market else event.market,
        ),
    ]
    if event.address:
        rows.append(("Location", event.address))
    if event.sku_names:
        rows.append(("SKUs", ", ".join(event.sku_names)))
    if event.support_type:
        label = SUPPORT_LABELS.get(event.support_type, event.support_type)
        if event.support_type == models.FieldMarketingEvent.SUPPORT_OTHER and event.support_other:
            label = f"{label}: {event.support_other}"
        rows.append(("Sales support", label))
    if event.support_times:
        rows.append(("Times", event.support_times))
    if event.needs_field_support:
        rows.append(("Brand ambassadors", str(event.ambassador_count)))
        if event.support_scope:
            rows.append(("Scope", event.support_scope))
    if event.planned_full_cans:
        rows.append(("Planned full cans", f"{event.planned_full_cans:,}"))
    if event.planned_pour_samples:
        rows.append(("Planned 4oz pours", f"{event.planned_pour_samples:,}"))
    if event.planned_emails:
        rows.append(("Planned consumer emails", f"{event.planned_emails:,}"))
    if event.notes:
        rows.append(("Notes", event.notes))
    return rows


def _send_submitted_mail(event_id: int, actor) -> None:
    try:
        event = models.FieldMarketingEvent.objects.select_related(
            "tenant", "request"
        ).get(id=event_id)
        code = request_display_code(event.request_id) if event.request_id else None
        notify_plan_submitted(event, actor, _summary_rows(event), code)
    except Exception:
        logger.exception("Plan-submitted mail failed for field marketing event %s", event_id)


def _request_times(
    event: models.FieldMarketingEvent,
) -> tuple[datetime, datetime | None, datetime | None]:
    """Request date / start / end in the market's own timezone."""
    zone = _market_zone(event.market)
    when = datetime.combine(event.starts_on, event.start_time or time(12, 0), tzinfo=zone)
    if not event.start_time:
        return when, None, None
    if not event.end_time:
        return when, when, None
    ends = datetime.combine(event.starts_on, event.end_time, tzinfo=zone)
    if ends <= when:
        ends += timedelta(days=1)
    return when, when, ends


def _timezone_row(when: datetime) -> models.TimeZone | None:
    """The TimeZone row for this instant: exact DST code first, then any row in that zone."""
    zone_key = getattr(when.tzinfo, "key", None)
    rows = list(models.TimeZone.objects.order_by("id"))
    abbreviation = (when.tzname() or "").upper()
    for row in rows:
        if (row.code or "").strip().upper() == abbreviation:
            return row
    for row in rows:
        resolved = resolve_zoneinfo(row)
        if resolved is not None and resolved.key == zone_key:
            return row
    return None


def _submit_event(event: models.FieldMarketingEvent, actor) -> bool:
    """Submit the plan. True when this call submitted it or booked its request.

    Submitting an already-submitted plan is a no-op, so double clicks and
    retries never resend the emails.
    """
    already_submitted = event.status == models.FieldMarketingEvent.STATUS_SUBMITTED
    create_request = _should_create_request(event)
    if already_submitted and (_live_request(event) is not None or not create_request):
        return False

    if not create_request:
        # Plan-only confirm — seeding / sales support / unstaffed guerilla.
        event.status = models.FieldMarketingEvent.STATUS_SUBMITTED
        event.save(update_fields=["status", "updated_at"])
        return True

    address = (event.address or "").strip()
    if not address:
        raise FieldMarketingError("Add an address before submitting this to Ignite.")
    request_type = _resolve_event_activation_type(event.tenant)
    when, starts_at, ends_at = _request_times(event)
    request = models.Request.objects.create(
        name=(event.name or "Event activation")[:255],
        date=when,
        start_time=starts_at,
        end_time=ends_at,
        timezone=_timezone_row(when),
        address=address,
        notes=_request_notes(event),
        requestor_email=(getattr(actor, "email", None) or "")[:254] or None,
        request_type=request_type,
        tenant=event.tenant,
        created_by=actor,
        scheduling_status=models.SchedulingStatus.NEEDS_SCHEDULING,
        load_in_time=(event.support_times or "")[:255],
        onsite_poc=_market_manager_name(event)[:255],
        additional_team_details=_activation_team_details(event),
        # Leave event_assets_needed blank — ops fills on the full request form.
    )
    _attach_request_products(request, event, actor)
    _safe_log(
        request=request,
        kind=models.RequestActivityLog.KIND_CREATED,
        actor_user=actor,
        summary=f"Field marketing booked as Event Activation: {event.name}"[:512],
        metadata={
            "field_marketing_event": str(event.uuid),
            "market": event.market,
            "activity": event.activity,
        },
    )
    event.request = request
    event.status = models.FieldMarketingEvent.STATUS_SUBMITTED
    event.save(update_fields=["request", "status", "updated_at"])
    return True


def _submit_and_mail(event: models.FieldMarketingEvent, actor) -> None:
    if _submit_event(event, actor):
        transaction.on_commit(partial(_send_submitted_mail, event.id, actor))


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
        _submit_and_mail(event, user)
    return event


def _plan_for(tenant, event_id: str, *, deleted: bool = False) -> models.FieldMarketingEvent:
    qs = models.FieldMarketingEvent.objects.select_for_update().filter(
        tenant=tenant, deleted_at__isnull=not deleted
    )
    try:
        event = qs.filter(uuid=event_id).first()
    except (ValueError, ValidationError):
        event = None
    if event is None:
        if deleted:
            raise FieldMarketingError("That deleted plan isn't on Torch.")
        raise FieldMarketingError("That field marketing event isn't on Torch.")
    return event


@transaction.atomic
def submit_event(*, user, event_id: str, tenant_id=None) -> models.FieldMarketingEvent:
    tenant = _require_torch_user(user, tenant_id=tenant_id)
    event = _plan_for(tenant, event_id)
    _submit_and_mail(event, user)
    return event


@transaction.atomic
def update_event(*, user, event_id: str, payload: dict, tenant_id=None) -> models.FieldMarketingEvent:
    """Edit a plan that isn't booked. Booked rows change on the request itself."""
    tenant = _require_torch_user(user, tenant_id=tenant_id)
    event = _plan_for(tenant, event_id)
    request = _live_request(event)
    if request is not None:
        raise FieldMarketingError(
            f"This plan is booked as {request_display_code(request.id)}. "
            "Change it on the request, or delete the plan."
        )
    cleaned = _clean_plan(payload, tenant)
    for field, value in cleaned.items():
        setattr(event, field, value)
    event.save()
    return event


@dataclass(frozen=True)
class DeleteOutcome:
    event: models.FieldMarketingEvent
    effect: str
    request_code: str | None


def _send_deleted_mail(event_id: int, actor, request_code: str | None, cancelled: bool) -> None:
    try:
        event = models.FieldMarketingEvent.objects.select_related(
            "tenant", "request"
        ).get(id=event_id)
        notify_plan_deleted(event, actor, _summary_rows(event), request_code, cancelled)
    except Exception:
        logger.exception("Plan-deleted mail failed for field marketing event %s", event_id)


@transaction.atomic
def delete_event(*, user, event_id: str, tenant_id=None) -> DeleteOutcome:
    """Soft-delete a plan. A linked request that hasn't happened is cancelled too.

    Started, past, recapped, or clocked-in requests stay untouched; only the
    plan is hidden. Deleting a submitted plan notifies the brand's internal list.
    """
    tenant = _require_torch_user(user, tenant_id=tenant_id)
    event = _plan_for(tenant, event_id)
    now = timezone.now()
    effect = _delete_effect(event, _locked_request_ids([event.request_id], now), now)
    code = request_display_code(event.request_id) if event.request_id else None
    request = _live_request(event)
    if effect == DELETE_CANCEL_REQUEST and request is not None:
        soft_delete_request(
            request,
            user,
            summary=f"Cancelled with deleted field marketing plan: {event.name}",
        )
    elif effect == DELETE_KEEP_REQUEST and request is not None:
        _safe_log(
            request=request,
            kind=models.RequestActivityLog.KIND_UPDATED,
            actor_user=user,
            summary=f"Field marketing plan deleted (request kept): {event.name}"[:512],
            metadata={"field_marketing_event": str(event.uuid), "plan_deleted": True},
        )
    event.deleted_at = now
    event.deleted_by = user if getattr(user, "id", None) else None
    event.delete_cancelled_request = effect == DELETE_CANCEL_REQUEST
    event.save(update_fields=["deleted_at", "deleted_by", "delete_cancelled_request", "updated_at"])
    if event.status == models.FieldMarketingEvent.STATUS_SUBMITTED:
        transaction.on_commit(
            partial(
                _send_deleted_mail,
                event.id,
                user,
                code if request is not None else None,
                effect == DELETE_CANCEL_REQUEST,
            )
        )
    return DeleteOutcome(event=event, effect=effect, request_code=code if request else None)


def can_restore(user, event: models.FieldMarketingEvent | None = None, now: datetime | None = None) -> bool:
    """Ignite admins restore anything; the person who deleted gets an Undo window."""
    if _user_can_pick_any_tenant(user):
        return True
    if event is None or event.deleted_at is None:
        return False
    return (
        event.deleted_by_id is not None
        and event.deleted_by_id == getattr(user, "id", None)
        and (now or timezone.now()) - event.deleted_at <= UNDO_WINDOW
    )


@transaction.atomic
def restore_event(*, user, event_id: str, tenant_id=None) -> models.FieldMarketingEvent:
    """Bring a deleted plan back. A request the delete cancelled stays cancelled."""
    tenant = _require_torch_user(user, tenant_id=tenant_id)
    event = _plan_for(tenant, event_id, deleted=True)
    if not can_restore(user, event):
        raise FieldMarketingError("Only Ignite admins can restore deleted plans.")
    event.deleted_at = None
    event.deleted_by = None
    event.save(update_fields=["deleted_at", "deleted_by", "updated_at"])
    return event


@transaction.atomic
def log_results(*, user, event_id: str, payload: dict, tenant_id=None) -> models.FieldMarketingEvent:
    tenant = _require_torch_user(user, tenant_id=tenant_id)
    event = _plan_for(tenant, event_id)
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
        "deleted_events": [],
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
    start_time: str
    end_time: str
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
    request_uuid: str | None
    request_cancelled: bool
    can_book: bool
    delete_effect: str
    deleted_at: str | None
    deleted_by_name: str
    delete_cancelled_request: bool
    results_counted: int = 0
    results_pending: int = 0


@strawberry.type(name="FieldMarketingBoard")
class FieldMarketingBoardType:
    available: bool
    month: str
    month_label: str
    managers: list[FieldMarketingManagerType]
    kpis: list[FieldMarketingKpiType]
    events: list[FieldMarketingEventType]
    deleted_events: list[FieldMarketingEventType]
    can_restore: bool
    skus: list[str]


def _board_type(payload: dict, can_restore_any: bool = False) -> FieldMarketingBoardType:
    return FieldMarketingBoardType(
        available=payload["available"],
        month=payload["month"],
        month_label=payload["month_label"],
        managers=[FieldMarketingManagerType(**row) for row in payload["managers"]],
        kpis=[FieldMarketingKpiType(**row) for row in payload["kpis"]],
        events=[FieldMarketingEventType(**row) for row in payload["events"]],
        deleted_events=[
            FieldMarketingEventType(**row) for row in payload.get("deleted_events") or []
        ],
        can_restore=can_restore_any,
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
    start_time: str = ""
    end_time: str = ""
    support_scope: str = ""
    submit: bool = False
    tenant_id: strawberry.ID | None = None


@strawberry.input
class UpdateFieldMarketingInput(SparkGraphQLInput):
    event_id: str
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
    start_time: str = ""
    end_time: str = ""
    support_scope: str = ""
    tenant_id: strawberry.ID | None = None


@strawberry.input
class SubmitFieldMarketingInput(SparkGraphQLInput):
    event_id: str
    tenant_id: strawberry.ID | None = None


@strawberry.input
class DeleteFieldMarketingInput(SparkGraphQLInput):
    event_id: str
    tenant_id: strawberry.ID | None = None


@strawberry.input
class RestoreFieldMarketingInput(SparkGraphQLInput):
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
class FieldMarketingDeleteResponse:
    success: bool
    message: str
    client_mutation_id: strawberry.ID | None = None
    event: FieldMarketingEventType | None = None
    request_code: str | None = None
    request_cancelled: bool = False
    can_undo: bool = False


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
        include_deleted: bool = False,
    ) -> FieldMarketingBoardType:
        user = await SparkGraphQLMixin().get_user(info)
        tenant = await sync_to_async(_active_tenant_for_user)(user, tenant_id)
        if tenant is None or not is_torch_tenant(tenant):
            return _board_type(empty_board(month=month))
        admin = _user_can_pick_any_tenant(user)
        try:
            payload = await sync_to_async(build_board)(
                tenant,
                month=month,
                market=market,
                quarter=quarter,
                activity=activity,
                include_deleted=include_deleted and admin,
                include_pending=admin,
            )
        except FieldMarketingError as exc:
            raise GraphQLError(str(exc)) from exc
        return _board_type(payload, can_restore_any=admin)


def _payload_from_plan(input: PlanFieldMarketingInput | UpdateFieldMarketingInput) -> dict:
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
        "start_time": input.start_time,
        "end_time": input.end_time,
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
            message = "Booked with Ignite as Event Activation — pending on the tracker."
        elif input.submit:
            message = "Confirmed on the plan (no Ignite request for this tactic)."
        else:
            message = "Saved to the plan."
        return build_mutation_response(
            FieldMarketingEventResponse,
            success=True,
            message=message,
            input_obj=input,
            event=await sync_to_async(_event_type)(event),
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
        message = (
            "Booked with Ignite as Event Activation — pending on the tracker."
            if event.request_id
            else "Confirmed on the plan (no Ignite request for this tactic)."
        )
        return build_mutation_response(
            FieldMarketingEventResponse,
            success=True,
            message=message,
            input_obj=input,
            event=await sync_to_async(_event_type)(event),
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
            event=await sync_to_async(_event_type)(event),
        )

    @relay.mutation(permission_classes=[StrictIsAuthenticated])
    async def update_field_marketing(
        self, info: strawberry.Info, input: UpdateFieldMarketingInput
    ) -> FieldMarketingEventResponse:
        user = await SparkGraphQLMixin().get_user(info)
        try:
            event = await sync_to_async(update_event)(
                user=user,
                event_id=input.event_id,
                payload=_payload_from_plan(input),
                tenant_id=input.tenant_id,
            )
        except FieldMarketingError as exc:
            raise GraphQLError(str(exc)) from exc
        return build_mutation_response(
            FieldMarketingEventResponse,
            success=True,
            message="Plan updated.",
            input_obj=input,
            event=await sync_to_async(_event_type)(event),
        )

    @relay.mutation(permission_classes=[StrictIsAuthenticated])
    async def delete_field_marketing(
        self, info: strawberry.Info, input: DeleteFieldMarketingInput
    ) -> FieldMarketingDeleteResponse:
        user = await SparkGraphQLMixin().get_user(info)
        try:
            outcome = await sync_to_async(delete_event)(
                user=user,
                event_id=input.event_id,
                tenant_id=input.tenant_id,
            )
        except FieldMarketingError as exc:
            raise GraphQLError(str(exc)) from exc
        code = outcome.request_code
        if outcome.effect == DELETE_CANCEL_REQUEST:
            message = f"Plan deleted. {code} was cancelled."
        elif outcome.effect == DELETE_KEEP_REQUEST:
            message = f"Plan deleted. {code} and its recap stay on the tracker."
        else:
            message = "Plan deleted."
        return build_mutation_response(
            FieldMarketingDeleteResponse,
            success=True,
            message=message,
            input_obj=input,
            event=await sync_to_async(_event_type)(outcome.event),
            request_code=code,
            request_cancelled=outcome.effect == DELETE_CANCEL_REQUEST,
            can_undo=await sync_to_async(can_restore)(user, outcome.event),
        )

    @relay.mutation(permission_classes=[StrictIsAuthenticated])
    async def restore_field_marketing(
        self, info: strawberry.Info, input: RestoreFieldMarketingInput
    ) -> FieldMarketingEventResponse:
        user = await SparkGraphQLMixin().get_user(info)
        try:
            event = await sync_to_async(restore_event)(
                user=user,
                event_id=input.event_id,
                tenant_id=input.tenant_id,
            )
        except FieldMarketingError as exc:
            raise GraphQLError(str(exc)) from exc
        message = "Plan restored."
        if event.delete_cancelled_request:
            message += " Its cancelled request stays cancelled — book it again if it's back on."
        return build_mutation_response(
            FieldMarketingEventResponse,
            success=True,
            message=message,
            input_obj=input,
            event=await sync_to_async(_event_type)(event),
        )
