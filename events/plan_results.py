"""Push a filed recap's numbers onto a field-marketing plan.

Ops picks a recap, ticks which of its metrics count, and attaches it to the
plan it executed (usually the RMM's plan that booked the request). The plan
then shows Planned vs Actual from the attached recaps. Links store which
metrics count, never the values, so results always match the recap.

Actuals only count approved, non-archived recaps — the same gate clients
see on ``Event.recaps``. Admins also see attached recaps still in review,
labeled and kept out of the totals. Attaching never sends email.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, timedelta

import strawberry
from asgiref.sync import sync_to_async
from django.db import transaction
from django.db.models import Q
from graphql import GraphQLError
from strawberry import relay

from events import models
from events.plan_actuals import (
    RECAP_RELATED,
    Total,
    counts_toward_results,
    link_recap,
    links_for_plans,
    recap_day,
    selected_metrics,
    sum_metrics,
)
from events.field_marketing import (
    ACTIVITY_FILTERS,
    MARKETS,
    _accepts_cans,
    _markets,
    _serialize,
    is_torch_tenant,
)
from events.torch_retail_routing import (
    EXECUTION_EVENT,
    EXECUTION_GUERILLA,
    EXECUTION_RETAIL,
    EXECUTION_SEEDING,
    state_code_from_event,
    torch_recap_execution_type,
)
from recaps import plan_metrics as pm
from recaps.models import CustomRecap, PlanRecapLink, Recap
from tenants.models import TenantedUser
from utils.graphql.inputs import SparkGraphQLInput
from utils.graphql.mixins import SparkGraphQLMixin, resolve_id_to_int
from utils.graphql.permissions import (
    StrictIsAuthenticated,
    _is_admin_access,
    resolve_request_user_access,
)
from utils.utils import ROLE_ID, build_mutation_response

logger = logging.getLogger(__name__)

SUGGEST_WINDOW_DAYS = 7
SUGGEST_LIMIT = 8
SEARCH_LIMIT = 20

_FM = models.FieldMarketingEvent
TACTICS_FOR_EXECUTION: dict[str, frozenset[str]] = {
    EXECUTION_EVENT: ACTIVITY_FILTERS[_FM.ACTIVITY_EVENT_ACTIVATION],
    EXECUTION_GUERILLA: ACTIVITY_FILTERS[_FM.ACTIVITY_GUERILLA],
    EXECUTION_SEEDING: ACTIVITY_FILTERS[_FM.ACTIVITY_PRODUCT_SEEDING],
    EXECUTION_RETAIL: ACTIVITY_FILTERS[_FM.ACTIVITY_SALES_SUPPORT],
}
EXECUTION_LABELS = {
    EXECUTION_EVENT: "Event",
    EXECUTION_GUERILLA: "Guerilla",
    EXECUTION_SEEDING: "Seeding",
    EXECUTION_RETAIL: "Retail",
}
MARKET_STATES = {
    "miami": "FL",
    "orlando": "FL",
    "houston": "TX",
    "austin-dallas": "TX",
}
_MARKET_WORDS = {
    "miami": ("miami",),
    "orlando": ("orlando",),
    "houston": ("houston",),
    "austin-dallas": ("austin", "dallas"),
}


class PlanResultsError(Exception):
    """Something the caller can fix: wrong recap, wrong brand, no metrics."""


# --- loading ---------------------------------------------------------------

def _load_recap(kind: str, recap_id):
    try:
        pk = resolve_id_to_int(recap_id)
    except (TypeError, ValueError, GraphQLError):
        raise PlanResultsError("Recap not found.") from None
    if kind == pm.KIND_CUSTOM:
        recap = (
            CustomRecap.objects.select_related(
                *RECAP_RELATED, "custom_recap_template", "custom_recap_template__event_type"
            )
            .filter(id=pk)
            .first()
        )
    elif kind == pm.KIND_LEGACY:
        recap = Recap.objects.select_related(*RECAP_RELATED).filter(id=pk).first()
    else:
        raise PlanResultsError("Pick a recap.")
    if recap is None:
        raise PlanResultsError("Recap not found.")
    return recap


def _recap_tenant_id(recap) -> int | None:
    tenant_id = getattr(recap, "tenant_id", None)
    if tenant_id:
        return tenant_id
    event = getattr(recap, "event", None)
    return getattr(event, "tenant_id", None)


def _link_for(recap, kind: str) -> PlanRecapLink | None:
    lookup = {"custom_recap": recap} if kind == pm.KIND_CUSTOM else {"recap": recap}
    return PlanRecapLink.objects.select_related("plan").filter(**lookup).first()


def _ba_name(recap) -> str:
    user = getattr(getattr(recap, "ambassador", None), "user", None)
    if user is not None:
        name = f"{user.first_name or ''} {user.last_name or ''}".strip()
        if name:
            return name
    return (getattr(recap, "external_ba_name", None) or "").strip()


def _status(recap) -> str:
    if recap.archived_at is not None:
        return "archived"
    return "approved" if recap.approved else "needs_review"


def tenant_has_plans(tenant) -> bool:
    if tenant is None:
        return False
    return is_torch_tenant(tenant) or _FM.objects.filter(tenant=tenant).exists()


# --- suggestions -----------------------------------------------------------


def _recap_market(recap) -> tuple[str | None, str | None]:
    """(market key, state code) the recap ran in, best effort."""
    event = getattr(recap, "event", None)
    rmm = getattr(event, "rmm_asigned", None)
    rmm_email = (getattr(rmm, "email", None) or "").strip().lower()
    for key, _label, _manager, email, _phone in MARKETS:
        if rmm_email and rmm_email == email.lower():
            return key, MARKET_STATES.get(key)
    address = (getattr(event, "address", None) or "").lower()
    state = state_code_from_event(event)
    for key, words in _MARKET_WORDS.items():
        if any(word in address for word in words):
            return key, state or MARKET_STATES.get(key)
    return None, state


def _day_gap(plan, day: date) -> int:
    start = plan.starts_on
    end = start + timedelta(days=max(1, plan.days or 1) - 1)
    if start <= day <= end:
        return 0
    return (start - day).days if day < start else (day - end).days


@dataclass
class Suggestion:
    plan: object
    score: int
    reasons: list[str]
    linked: bool
    gap: int


def suggest_plans(recap, kind: str, query: str | None = None) -> list[Suggestion]:
    tenant_id = _recap_tenant_id(recap)
    execution = torch_recap_execution_type(recap)
    tactics = TACTICS_FOR_EXECUTION.get(execution, frozenset())
    day = recap_day(recap)
    request_id = getattr(getattr(recap, "event", None), "request_id", None)
    market_key, state = _recap_market(recap)
    markets = _markets()

    qs = _FM.objects.filter(tenant_id=tenant_id)
    text = (query or "").strip()
    if text:
        qs = qs.filter(
            Q(name__icontains=text) | Q(address__icontains=text) | Q(market__icontains=text)
        ).order_by("-starts_on", "-id")[:SEARCH_LIMIT]
    else:
        window = Q(
            starts_on__gte=day - timedelta(days=SUGGEST_WINDOW_DAYS + 31),
            starts_on__lte=day + timedelta(days=SUGGEST_WINDOW_DAYS),
        )
        if request_id:
            window |= Q(request_id=request_id)
        qs = qs.filter(window)

    out: list[Suggestion] = []
    for plan in qs:
        linked = bool(request_id) and plan.request_id == request_id
        gap = _day_gap(plan, day)
        if not text and not linked and gap > SUGGEST_WINDOW_DAYS:
            continue
        score = 0
        reasons: list[str] = []
        if linked:
            score += 1000
            reasons.append("Booked from this plan")
        if plan.activity in tactics:
            score += 100
            reasons.append(f"Same tactic ({EXECUTION_LABELS.get(execution, execution)})")
        market = markets.get(plan.market)
        if market_key and plan.market == market_key:
            score += 50
            reasons.append(f"{market.label if market else plan.market} · {market.manager if market else ''}".strip(" ·"))
        elif state and MARKET_STATES.get(plan.market) == state:
            score += 20
            reasons.append(f"Same state ({state})")
        if gap <= SUGGEST_WINDOW_DAYS:
            score += max(0, 35 - 5 * gap)
            reasons.append("Same day" if gap == 0 else f"{gap} day{'s' if gap != 1 else ''} apart")
        out.append(Suggestion(plan=plan, score=score, reasons=reasons, linked=linked, gap=gap))
    out.sort(key=lambda s: (-s.score, s.gap, -s.plan.starts_on.toordinal()))
    return out if text else out[:SUGGEST_LIMIT]


# --- push / remove ---------------------------------------------------------


def _check_tenant(plan, recap) -> None:
    if plan.tenant_id != _recap_tenant_id(recap):
        raise PlanResultsError("That plan belongs to another brand.")


@transaction.atomic
def push_recap(
    *, kind: str, recap_id, plan_id: str, metric_keys: list[str], actor, confirm_move: bool
) -> PlanRecapLink:
    recap = _load_recap(kind, recap_id)
    plan = _FM.objects.filter(uuid=plan_id).first() if plan_id else None
    if plan is None:
        raise PlanResultsError("Pick a plan.")
    _check_tenant(plan, recap)
    available = {m.key for m in pm.recap_metrics(recap, kind)}
    keys = [key for key in pm.METRIC_ORDER if key in set(metric_keys or [])]
    unknown = set(metric_keys or []) - set(pm.METRIC_ORDER)
    if unknown:
        raise PlanResultsError("Pick metrics from the recap.")
    missing = [key for key in keys if key not in available]
    if missing:
        raise PlanResultsError("This recap didn't answer one of those metrics.")
    if not keys:
        raise PlanResultsError("Tick at least one metric to push.")

    link = _link_for(recap, kind)
    if link is not None and link.plan_id != plan.id and not confirm_move:
        raise PlanResultsError(
            f"This recap already counts toward “{link.plan.name}”. Confirm to move it."
        )
    if link is None:
        lookup = {"custom_recap": recap} if kind == pm.KIND_CUSTOM else {"recap": recap}
        link = PlanRecapLink(**lookup)
    link.plan = plan
    link.metric_keys = keys
    link.auto = False
    link.attached_by = actor
    link.save()
    return link


@transaction.atomic
def remove_recap(*, kind: str, recap_id) -> bool:
    recap = _load_recap(kind, recap_id)
    link = _link_for(recap, kind)
    if link is None:
        return False
    link.delete()
    return True


def auto_attach_on_approval(*, kind: str, recap_id: int, actor) -> PlanRecapLink | None:
    """Approved recap whose event came from a plan's booked request → attach.

    Leaves an existing link alone (ops may have moved it on purpose). Picks
    the plan nearest the recap date when one request backs several plans.
    """
    recap = _load_recap(kind, recap_id)
    if _link_for(recap, kind) is not None:
        return None
    request_id = getattr(getattr(recap, "event", None), "request_id", None)
    if not request_id:
        return None
    plans = list(
        _FM.objects.filter(tenant_id=_recap_tenant_id(recap), request_id=request_id)
    )
    if not plans:
        return None
    day = recap_day(recap)
    plan = min(plans, key=lambda p: (_day_gap(p, day), p.id))
    available = pm.recap_metrics(recap, kind)
    keys = pm.default_keys(recap, available)
    if not keys:
        return None
    lookup = {"custom_recap": recap} if kind == pm.KIND_CUSTOM else {"recap": recap}
    return PlanRecapLink.objects.create(
        plan=plan, metric_keys=keys, auto=True, attached_by=actor, **lookup
    )


def auto_attach_quietly(*, kind: str, recap_id: int, actor) -> None:
    try:
        link = auto_attach_on_approval(kind=kind, recap_id=recap_id, actor=actor)
        if link is not None:
            logger.info("auto-attached %s recap %s to plan %s", kind, recap_id, link.plan_id)
    except Exception:
        logger.exception("plan auto-attach failed for %s recap %s", kind, recap_id)


# --- results ---------------------------------------------------------------


@dataclass
class ResultRow:
    key: str
    label: str
    planned: float | None
    actual: float | None
    unit: str
    text: str
    note: str
    parts: list[tuple[str, int]]


def _row(total: Total | None, key: str, label: str, planned=None, note: str = "") -> ResultRow | None:
    actual = total.value if total else None
    if actual is None and not planned and not (total and total.texts):
        return None
    return ResultRow(
        key=key,
        label=(total.label if total else label) or label,
        planned=float(planned) if planned else None,
        actual=actual,
        unit=pm.UNITS.get(key, ""),
        text=", ".join(total.texts) if total else "",
        note=note,
        parts=sorted((total.parts if total else {}).items(), key=lambda kv: -kv[1]),
    )


def comparison_rows(plan, counted_links: list[PlanRecapLink]) -> list[ResultRow]:
    totals = sum_metrics(selected_metrics(link) for link in counted_links)
    rows: list[ResultRow | None] = []

    rows.append(_row(totals.get(pm.CONSUMERS_SAMPLED), pm.CONSUMERS_SAMPLED, "Consumers sampled"))
    rows.append(_row(totals.get(pm.PEOPLE_ENGAGED), pm.PEOPLE_ENGAGED, "People engaged"))

    samples = totals.get(pm.SAMPLES_BY_SKU)
    planned_cans = plan.planned_full_cans if _accepts_cans(plan) else 0
    note = ""
    planned_skus = list(plan.sku_names or [])
    if planned_skus:
        sampled = set((samples.parts if samples else {}).keys())
        missed = [sku for sku in planned_skus if sku not in sampled]
        note = "Planned SKUs: " + ", ".join(planned_skus)
        if samples and missed:
            note += " · not on recaps: " + ", ".join(missed)
    rows.append(_row(samples, pm.SAMPLES_BY_SKU, "Samples handed out", planned_cans, note))

    rows.append(_row(totals.get(pm.EMAILS_COLLECTED), pm.EMAILS_COLLECTED, "Emails collected", plan.planned_emails))
    rows.append(_row(totals.get(pm.SAMPLE_FORMAT), pm.SAMPLE_FORMAT, "Sample format"))
    rows.append(_row(totals.get(pm.UNITS_SOLD), pm.UNITS_SOLD, "Units sold"))
    rows.append(_row(totals.get(pm.CASES_DROPPED), pm.CASES_DROPPED, "Cases dropped"))
    rows.append(_row(totals.get(pm.DROP_OFF_LOCATIONS), pm.DROP_OFF_LOCATIONS, "Drop-off locations"))
    rows.append(_row(totals.get(pm.MILEAGE), pm.MILEAGE, "Mileage"))

    bas = {
        _ba_name(link_recap(link)[0]) or f"BA {i}"
        for i, link in enumerate(counted_links)
    }
    planned_bas = plan.ambassador_count if plan.needs_field_support else 0
    if counted_links or planned_bas:
        rows.append(
            ResultRow(
                key="brand_ambassadors",
                label="Brand ambassadors",
                planned=float(planned_bas) if planned_bas else None,
                actual=float(len(bas)) if counted_links else None,
                unit="BAs",
                text="",
                note="BAs with an approved recap on this plan",
                parts=[],
            )
        )
    hours = totals.get(pm.CLOCKED_HOURS)
    rows.append(
        _row(
            hours,
            pm.CLOCKED_HOURS,
            "BA hours (clocked)",
            note=f"Planned times: {plan.support_times}" if plan.support_times and hours else "",
        )
    )
    if (plan.days or 1) > 1 and counted_links:
        days_with = {recap_day(link_recap(link)[0]) for link in counted_links}
        rows.append(
            ResultRow(
                key="days",
                label="Days with recaps",
                planned=float(plan.days),
                actual=float(len(days_with)),
                unit="days",
                text="",
                note="",
                parts=[],
            )
        )
    rows.append(_row(totals.get(pm.PHOTOS), pm.PHOTOS, "Photos"))
    rows.append(_row(totals.get(pm.SPEND), pm.SPEND, "Spend"))
    return [row for row in rows if row is not None]


def recap_href(recap, kind: str) -> str:
    if kind == pm.KIND_CUSTOM:
        return f"/recap/view-custom/{recap.uuid}"
    return f"/recap/view/{recap.uuid}"


def build_plan_results(plan, *, is_admin: bool) -> dict:
    links = links_for_plans([plan.id])
    counted = [link for link in links if counts_toward_results(link_recap(link)[0])]
    visible = links if is_admin else counted
    recaps = []
    for link in visible:
        recap, kind = link_recap(link)
        recaps.append(
            {
                "id": str(recap.id),
                "kind": kind,
                "uuid": str(recap.uuid),
                "name": recap.name or "Recap",
                "date": recap_day(recap).isoformat(),
                "ba_name": _ba_name(recap),
                "status": _status(recap),
                "auto": link.auto,
                "href": recap_href(recap, kind),
                "metrics": selected_metrics(link),
            }
        )
    return {
        "plan_id": str(plan.uuid),
        "plan_name": plan.name,
        "counted": len(counted),
        "pending": (len(links) - len(counted)) if is_admin else 0,
        "rows": comparison_rows(plan, counted),
        "recaps": recaps,
    }


# --- access ----------------------------------------------------------------


async def _is_admin(user) -> bool:
    role_slug, is_staff, is_super, email = await resolve_request_user_access(user)
    return _is_admin_access(role_slug, is_staff, is_super, email)


async def _require_admin(user) -> None:
    if not await _is_admin(user):
        raise GraphQLError("Only Ignite ops can push recaps to plans.")


def _can_view_plan(user, plan) -> bool:
    if getattr(user, "role_id", None) == ROLE_ID.Ambassadors:
        return False
    return TenantedUser.objects.filter(
        user=user, tenant_id=plan.tenant_id, is_active=True
    ).exists()


# --- GraphQL ---------------------------------------------------------------


@strawberry.type(name="RecapMetricPart")
class RecapMetricPartType:
    label: str
    value: int


@strawberry.type(name="RecapPlanMetric")
class RecapPlanMetricType:
    key: str
    label: str
    value: float | None
    text: str
    unit: str
    parts: list[RecapMetricPartType]
    default_on: bool = False


@strawberry.type(name="PlanSummary")
class PlanSummaryType:
    id: str
    name: str
    starts_on: str
    days: int
    market: str
    market_label: str
    manager_name: str
    activity: str
    activity_label: str
    request_code: str | None


@strawberry.type(name="RecapPlanSuggestion")
class RecapPlanSuggestionType:
    plan: PlanSummaryType
    score: int
    reasons: list[str]
    linked: bool


@strawberry.type(name="RecapPlanPicker")
class RecapPlanPickerType:
    available: bool
    execution_type: str
    execution_label: str
    current_plan: PlanSummaryType | None
    current_metric_keys: list[str]
    current_auto: bool
    metrics: list[RecapPlanMetricType]
    suggestions: list[RecapPlanSuggestionType]


@strawberry.type(name="PlanResultRow")
class PlanResultRowType:
    key: str
    label: str
    planned: float | None
    actual: float | None
    unit: str
    text: str
    note: str
    parts: list[RecapMetricPartType]


@strawberry.type(name="PlanResultRecap")
class PlanResultRecapType:
    id: str
    kind: str
    uuid: str
    name: str
    date: str
    ba_name: str
    status: str
    auto: bool
    href: str
    metrics: list[RecapPlanMetricType]


@strawberry.type(name="PlanResults")
class PlanResultsType:
    plan_id: str
    plan_name: str
    counted: int
    pending: int
    rows: list[PlanResultRowType]
    recaps: list[PlanResultRecapType]


def _parts(parts) -> list[RecapMetricPartType]:
    return [RecapMetricPartType(label=name, value=int(qty)) for name, qty in parts]


def _metric_type(metric: pm.RecapMetric, default_on: bool = False) -> RecapPlanMetricType:
    return RecapPlanMetricType(
        key=metric.key,
        label=metric.label,
        value=metric.value,
        text=metric.text,
        unit=metric.unit,
        parts=_parts(metric.parts),
        default_on=default_on,
    )


def _plan_summary(plan) -> PlanSummaryType:
    row = _serialize(plan)
    return PlanSummaryType(
        id=row["id"],
        name=row["name"],
        starts_on=row["starts_on"],
        days=row["days"],
        market=row["market"],
        market_label=row["market_label"],
        manager_name=row["manager_name"],
        activity=row["activity"],
        activity_label=row["activity_label"],
        request_code=row["request_code"],
    )


def build_picker(kind: str, recap_id, query: str | None) -> RecapPlanPickerType:
    recap = _load_recap(kind, recap_id)
    execution = torch_recap_execution_type(recap)
    tenant = getattr(recap, "tenant", None) or getattr(recap.event, "tenant", None)
    if not tenant_has_plans(tenant):
        return RecapPlanPickerType(
            available=False,
            execution_type=execution,
            execution_label=EXECUTION_LABELS.get(execution, execution),
            current_plan=None,
            current_metric_keys=[],
            current_auto=False,
            metrics=[],
            suggestions=[],
        )
    metrics = pm.recap_metrics(recap, kind)
    link = _link_for(recap, kind)
    on = set(link.metric_keys or []) if link else set(pm.default_keys(recap, metrics))
    return RecapPlanPickerType(
        available=True,
        execution_type=execution,
        execution_label=EXECUTION_LABELS.get(execution, execution),
        current_plan=_plan_summary(link.plan) if link else None,
        current_metric_keys=list(link.metric_keys or []) if link else [],
        current_auto=bool(link.auto) if link else False,
        metrics=[_metric_type(m, m.key in on) for m in metrics],
        suggestions=[
            RecapPlanSuggestionType(
                plan=_plan_summary(s.plan), score=s.score, reasons=s.reasons, linked=s.linked
            )
            for s in suggest_plans(recap, kind, query)
        ],
    )


def _results_type(payload: dict) -> PlanResultsType:
    return PlanResultsType(
        plan_id=payload["plan_id"],
        plan_name=payload["plan_name"],
        counted=payload["counted"],
        pending=payload["pending"],
        rows=[
            PlanResultRowType(
                key=row.key,
                label=row.label,
                planned=row.planned,
                actual=row.actual,
                unit=row.unit,
                text=row.text,
                note=row.note,
                parts=_parts(row.parts),
            )
            for row in payload["rows"]
        ],
        recaps=[
            PlanResultRecapType(
                id=r["id"],
                kind=r["kind"],
                uuid=r["uuid"],
                name=r["name"],
                date=r["date"],
                ba_name=r["ba_name"],
                status=r["status"],
                auto=r["auto"],
                href=r["href"],
                metrics=[_metric_type(m) for m in r["metrics"]],
            )
            for r in payload["recaps"]
        ],
    )


def _results_for(user, plan_id: str, is_admin: bool) -> PlanResultsType:
    plan = _FM.objects.filter(uuid=plan_id).first() if plan_id else None
    if plan is None or (not is_admin and not _can_view_plan(user, plan)):
        raise PlanResultsError("That plan isn't available.")
    return _results_type(build_plan_results(plan, is_admin=is_admin))


@strawberry.type
class PlanResultsQueries:
    @strawberry.field(permission_classes=[StrictIsAuthenticated])
    async def recap_plan_picker(
        self,
        info: strawberry.Info,
        recap_id: strawberry.ID,
        kind: str,
        query: str | None = None,
    ) -> RecapPlanPickerType:
        user = await SparkGraphQLMixin().get_user(info)
        await _require_admin(user)
        try:
            return await sync_to_async(build_picker)(kind, recap_id, query)
        except PlanResultsError as exc:
            raise GraphQLError(str(exc)) from exc

    @strawberry.field(permission_classes=[StrictIsAuthenticated])
    async def field_marketing_plan_results(
        self, info: strawberry.Info, plan_id: str
    ) -> PlanResultsType:
        user = await SparkGraphQLMixin().get_user(info)
        is_admin = await _is_admin(user)
        try:
            return await sync_to_async(_results_for)(user, plan_id, is_admin)
        except PlanResultsError as exc:
            raise GraphQLError(str(exc)) from exc


@strawberry.input
class PushRecapToPlanInput(SparkGraphQLInput):
    recap_id: strawberry.ID
    kind: str
    plan_id: str
    metric_keys: list[str]
    confirm_move: bool = False


@strawberry.input
class RemoveRecapFromPlanInput(SparkGraphQLInput):
    recap_id: strawberry.ID
    kind: str


@strawberry.type
class PushRecapToPlanResponse:
    success: bool
    message: str
    client_mutation_id: strawberry.ID | None = None
    plan: PlanSummaryType | None = None
    metric_keys: list[str] | None = None


@strawberry.type
class PlanResultsMutations:
    @relay.mutation(permission_classes=[StrictIsAuthenticated])
    async def push_recap_to_plan(
        self, info: strawberry.Info, input: PushRecapToPlanInput
    ) -> PushRecapToPlanResponse:
        user = await SparkGraphQLMixin().get_user(info)
        await _require_admin(user)
        try:
            link = await sync_to_async(push_recap)(
                kind=input.kind,
                recap_id=input.recap_id,
                plan_id=input.plan_id,
                metric_keys=list(input.metric_keys or []),
                actor=user,
                confirm_move=bool(input.confirm_move),
            )
            summary = await sync_to_async(_plan_summary)(link.plan)
        except PlanResultsError as exc:
            raise GraphQLError(str(exc)) from exc
        return build_mutation_response(
            PushRecapToPlanResponse,
            success=True,
            message=f"Pushed {len(link.metric_keys)} metrics to “{link.plan.name}”.",
            input_obj=input,
            plan=summary,
            metric_keys=list(link.metric_keys),
        )

    @relay.mutation(permission_classes=[StrictIsAuthenticated])
    async def remove_recap_from_plan(
        self, info: strawberry.Info, input: RemoveRecapFromPlanInput
    ) -> PushRecapToPlanResponse:
        user = await SparkGraphQLMixin().get_user(info)
        await _require_admin(user)
        try:
            removed = await sync_to_async(remove_recap)(kind=input.kind, recap_id=input.recap_id)
        except PlanResultsError as exc:
            raise GraphQLError(str(exc)) from exc
        return build_mutation_response(
            PushRecapToPlanResponse,
            success=True,
            message="Removed from the plan." if removed else "This recap wasn't on a plan.",
            input_obj=input,
        )
