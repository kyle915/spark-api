"""Torch event recaps under each RMM market on the field-marketing scorecard.

Approved, non-archived Event / Guerilla / Seeding recaps for the period —
the field-marketing executions, not retail sampling. Each row's market comes
from the recap's own address (``torch_markets``), then the plan it is
attached to, then the event's RMM. Values are read off the recap as filed;
rows are listed, never summed, because several BAs on one event each report
the event's numbers. 3rd-party recaps stay out like every other rollup.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta

import strawberry
from asgiref.sync import sync_to_async
from django.db.models import F
from django.db.models.functions import Coalesce
from django.utils import timezone
from graphql import GraphQLError

from events.demo_cancel import request_display_code
from events.field_marketing import (
    MARKETS,
    FieldMarketingError,
    _markets,
    _month_window,
    _require_torch_user,
)
from events.market import market_for
from events.plan_actuals import RECAP_RELATED, recap_day
from events.plan_results import _ba_name, _is_admin, recap_href
from events.torch_markets import torch_market_for_event
from events.torch_retail_routing import (
    EXECUTION_EVENT,
    EXECUTION_GUERILLA,
    EXECUTION_SEEDING,
    torch_recap_execution_type,
)
from recaps import plan_metrics as pm
from recaps.models import CustomRecap, Recap
from recaps.recap_types import with_recap_type
from utils.graphql.mixins import SparkGraphQLMixin
from utils.graphql.permissions import StrictIsAuthenticated

EVENT_RECAP_TYPES: dict[str, str] = {
    EXECUTION_EVENT: "Event",
    EXECUTION_GUERILLA: "Guerilla",
    EXECUTION_SEEDING: "Seeding",
}
ATTACHED = "attached"
UNATTACHED = "unattached"
MAX_DAYS = 186
MAX_ROWS = 300
UNASSIGNED = "unassigned"


@dataclass
class EventRecapRow:
    id: str
    kind: str
    uuid: str
    date: str
    type: str
    type_label: str
    name: str
    place: str
    city_state: str
    market: str
    market_label: str
    ba_name: str
    consumers_sampled: int | None
    samples: int | None
    samples_label: str
    sample_format: str
    pours: int | None
    emails: int | None
    plan_id: str | None
    plan_name: str
    plan_code: str | None
    href: str


def _window(month: str | None, start: str | None, end: str | None) -> tuple[date, date]:
    """[start, end] inclusive. Explicit dates win over the month."""
    if start or end:
        try:
            lo = date.fromisoformat((start or end or "").strip())
            hi = date.fromisoformat((end or start or "").strip())
        except ValueError:
            raise FieldMarketingError("Pick dates like 2026-10-01.") from None
        if hi < lo:
            lo, hi = hi, lo
        if (hi - lo).days > MAX_DAYS:
            raise FieldMarketingError("Pick a range of six months or less.")
        return lo, hi
    lo, after, _key = _month_window(month)
    return lo, after - timedelta(days=1)


def _clean_list(values, allowed, label: str) -> set[str]:
    picked = {str(v or "").strip().lower() for v in (values or []) if str(v or "").strip()}
    unknown = picked - set(allowed)
    if unknown:
        raise FieldMarketingError(f"Pick a {label}.")
    return picked


def _metric(metrics, key):
    return next((m for m in metrics if m.key == key), None)


def _int(metric) -> int | None:
    return None if metric is None or metric.value is None else int(round(metric.value))


def _recaps(tenant, lo: date, hi: date, types: set[str]):
    start = timezone.make_aware(datetime.combine(lo - timedelta(days=1), datetime.min.time()))
    end = timezone.make_aware(datetime.combine(hi + timedelta(days=2), datetime.min.time()))
    common = {"approved": True, "archived_at__isnull": True}
    custom = CustomRecap.objects.filter(
        tenant=tenant, exclude_from_aggregates=False, **common
    ).select_related(
        *RECAP_RELATED,
        "custom_recap_template",
        "custom_recap_template__event_type",
        "plan_link__plan",
    )
    legacy = Recap.objects.filter(event__tenant=tenant, **common).select_related(
        *RECAP_RELATED, "plan_link__plan"
    )
    for kind, qs in ((pm.KIND_CUSTOM, custom), (pm.KIND_LEGACY, legacy)):
        qs = (
            with_recap_type(qs)
            .filter(_recap_type__in=sorted(types))
            .annotate(_when=Coalesce(F("event__date"), F("event__start_time"), F("created_at")))
            .filter(_when__gte=start, _when__lt=end)
            .order_by("-_when", "-id")
        )
        for recap in qs[: MAX_ROWS * 2]:
            yield kind, recap


def _row(kind: str, recap, markets) -> EventRecapRow:
    event = getattr(recap, "event", None)
    link = getattr(recap, "plan_link", None)
    plan = link.plan if link is not None and link.plan.deleted_at is None else None
    market_key = torch_market_for_event(event) or (plan.market if plan else None)
    if market_key is None:
        rmm_email = (getattr(getattr(event, "rmm_asigned", None), "email", "") or "").lower()
        market_key = next(
            (key for key, _l, _m, email, _p in MARKETS if rmm_email and email.lower() == rmm_email),
            None,
        )
    market = markets.get(market_key or "")
    metrics = pm.recap_metrics(recap, kind)
    samples = _metric(metrics, pm.SAMPLES_BY_SKU)
    fmt = _metric(metrics, pm.SAMPLE_FORMAT)
    execution = torch_recap_execution_type(recap)
    return EventRecapRow(
        id=str(recap.id),
        kind=kind,
        uuid=str(recap.uuid),
        date=recap_day(recap).isoformat(),
        type=execution,
        type_label=EVENT_RECAP_TYPES.get(execution, execution.title()),
        name=(getattr(event, "name", None) or recap.name or "Recap").strip(),
        place=(getattr(event, "address", None) or getattr(recap, "typed_store_address", "") or "").strip(),
        city_state=(market_for(event).label or "") if event is not None else "",
        market=market.key if market else UNASSIGNED,
        market_label=market.label if market else "No market",
        ba_name=_ba_name(recap),
        consumers_sampled=_int(_metric(metrics, pm.CONSUMERS_SAMPLED)),
        samples=_int(samples),
        samples_label=samples.label if samples else "",
        sample_format=fmt.text if fmt else "",
        pours=_int(_metric(metrics, pm.POURS_SERVED)),
        emails=_int(_metric(metrics, pm.EMAILS_COLLECTED)),
        plan_id=str(plan.uuid) if plan else None,
        plan_name=plan.name if plan else "",
        plan_code=request_display_code(plan.request_id) if plan and plan.request_id else None,
        href=recap_href(recap, kind),
    )


def list_event_recaps(
    tenant,
    *,
    month: str | None = None,
    start: str | None = None,
    end: str | None = None,
    markets: list[str] | None = None,
    types: list[str] | None = None,
    attached: str | None = None,
) -> dict:
    lo, hi = _window(month, start, end)
    market_map = _markets()
    picked_markets = _clean_list(markets, [*market_map, UNASSIGNED], "market")
    picked_types = _clean_list(types, EVENT_RECAP_TYPES, "recap type") or set(EVENT_RECAP_TYPES)
    attached_key = (attached or "").strip().lower()
    if attached_key not in ("", ATTACHED, UNATTACHED):
        raise FieldMarketingError("Pick attached or not attached.")

    rows: list[EventRecapRow] = []
    for kind, recap in _recaps(tenant, lo, hi, picked_types):
        if not lo <= recap_day(recap) <= hi:
            continue
        row = _row(kind, recap, market_map)
        if picked_markets and row.market not in picked_markets:
            continue
        if attached_key == ATTACHED and row.plan_id is None:
            continue
        if attached_key == UNATTACHED and row.plan_id is not None:
            continue
        rows.append(row)
    rows.sort(key=lambda r: (r.date, r.name), reverse=True)
    return {
        "start": lo.isoformat(),
        "end": hi.isoformat(),
        "rows": rows[:MAX_ROWS],
        "truncated": len(rows) > MAX_ROWS,
    }


# --- GraphQL ---------------------------------------------------------------


@strawberry.type(name="FieldMarketingEventRecap")
class FieldMarketingEventRecapType:
    id: str
    kind: str
    uuid: str
    date: str
    type: str
    type_label: str
    name: str
    place: str
    city_state: str
    market: str
    market_label: str
    ba_name: str
    consumers_sampled: int | None
    samples: int | None
    samples_label: str
    sample_format: str
    pours: int | None
    emails: int | None
    plan_id: str | None
    plan_name: str
    plan_code: str | None
    href: str


@strawberry.type(name="FieldMarketingEventRecaps")
class FieldMarketingEventRecapsType:
    start: str
    end: str
    can_attach: bool
    truncated: bool
    rows: list[FieldMarketingEventRecapType]


@strawberry.type
class FieldMarketingRecapQueries:
    @strawberry.field(permission_classes=[StrictIsAuthenticated])
    async def field_marketing_event_recaps(
        self,
        info: strawberry.Info,
        tenant_id: strawberry.ID | None = None,
        month: str | None = None,
        start: str | None = None,
        end: str | None = None,
        markets: list[str] | None = None,
        types: list[str] | None = None,
        attached: str | None = None,
    ) -> FieldMarketingEventRecapsType:
        user = await SparkGraphQLMixin().get_user(info)
        try:
            tenant = await sync_to_async(_require_torch_user)(user, tenant_id)
            payload = await sync_to_async(list_event_recaps)(
                tenant,
                month=month,
                start=start,
                end=end,
                markets=markets,
                types=types,
                attached=attached,
            )
        except FieldMarketingError as exc:
            raise GraphQLError(str(exc)) from exc
        return FieldMarketingEventRecapsType(
            start=payload["start"],
            end=payload["end"],
            can_attach=await _is_admin(user),
            truncated=payload["truncated"],
            rows=[FieldMarketingEventRecapType(**vars(row)) for row in payload["rows"]],
        )
