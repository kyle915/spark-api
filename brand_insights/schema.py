"""GraphQL for store recommendations and alerts (``schema_clients``).

* ``tenantStoreInsights(tenantId)``: the brand dashboard's two cards. A
  brand user is held to a tenant they belong to and gets published rows
  only; Ignite staff may pass any tenant and also get drafts (``isDraft``).
  Reads stored rows; never calls the model.
* ``brandInsightReview``: every brand's current findings, for staff. Null
  for anyone else.
* ``setBrandInsightStatus`` / ``publishBrandInsights`` /
  ``generateBrandInsights``: staff only. Nothing here sends anything.

Staff is decided from the database, as for weekly summaries
(:func:`recaps.weekly_summary._staff_user`).
"""

from __future__ import annotations

import logging

import strawberry
from asgiref.sync import sync_to_async

from recaps.weekly_summary import _person, _staff_user
from pilot.entitlements import (
    is_client_billing_launched,
    resolve_brand_entitlements,
)
from utils.graphql.mixins import resolve_id_to_int
from utils.graphql.permissions import StrictIsAuthenticated

logger = logging.getLogger(__name__)

# The web client keys its cache on ``id`` across every type.
_ID_PREFIX = "BrandInsight:"


def _row_id(raw) -> int | None:
    text = str(raw or "").strip().removeprefix(_ID_PREFIX)
    return int(text) if text.isdigit() else None


@strawberry.type
class InsightEligibleStore:
    store_key: str
    store_label: str


@strawberry.type
class StoreInsight:
    """One finding as a brand sees it. No person is named anywhere in it."""

    id: strawberry.ID
    kind: str
    code: str
    subject: str
    text: str
    period_start: str
    period_end: str
    is_draft: bool
    scope_key: str = ""
    visit_count: int | None = None
    comparison_period: str = ""
    supported_cost: str | None = None
    adjusted_pos_units: int | None = None
    eligible_stores: list[InsightEligibleStore] = strawberry.field(default_factory=list)
    current_action_uuid: str | None = None
    current_action_type: str | None = None
    current_action_status: str | None = None
    current_action_owner: str | None = None
    current_action_provider: str | None = None


@strawberry.type
class TenantStoreInsights:
    recommendations: list[StoreInsight]
    alerts: list[StoreInsight]


@strawberry.type
class BrandInsightReviewItem:
    """One finding for staff: the wording shown, the sentence built in code,
    the numbers it came from, and who published it."""

    id: strawberry.ID
    kind: str
    code: str
    subject: str
    text: str
    template: str
    facts_text: str
    source: str
    status: str
    period_start: str
    period_end: str
    generated_at: str
    reviewed_by_name: str | None
    reviewed_at: str | None


@strawberry.type
class BrandInsightReviewBrand:
    tenant_id: strawberry.ID
    tenant_name: str
    drafts: int
    items: list[BrandInsightReviewItem]


@strawberry.type
class BrandInsightResult:
    ok: bool
    reason: str | None
    item: BrandInsightReviewItem | None


@strawberry.type
class BrandInsightBatchResult:
    ok: bool
    reason: str | None
    count: int


def _brand_item(row, *, access=None, latest_actions=None) -> StoreInsight:
    from pilot.actions import (
        build_store_evidence_for_insight,
        eligible_stores_for_insight,
    )

    elig = eligible_stores_for_insight(row)
    facts = row.facts or {}
    v_count = facts.get("visits") or facts.get("recaps")
    comp_period = f"{row.period_start.isoformat()} to {row.period_end.isoformat()}"
    supp_cost: str | None = None
    adj_pos: int | None = None
    if access is not None and row.scope_key.startswith("store:"):
        sk = row.scope_key[len("store:") :]
        ev = build_store_evidence_for_insight(access, row, sk)
        if v_count is None:
            v_count = ev.get("visit_count")
        supp_cost = ev.get("supported_brand_cost")
        adj_pos = ev.get("adjusted_register_units")

    act = None
    if latest_actions:
        if row.scope_key.startswith("store:"):
            act = latest_actions.get(row.scope_key[len("store:") :])
        else:
            for es in elig:
                if es["store_key"] in latest_actions:
                    act = latest_actions[es["store_key"]]
                    break

    return StoreInsight(
        id=strawberry.ID(f"{_ID_PREFIX}{row.id}"),
        kind=row.kind,
        code=row.code,
        subject=row.subject,
        text=row.body,
        period_start=row.period_start.isoformat(),
        period_end=row.period_end.isoformat(),
        is_draft=row.status != "published",
        scope_key=row.scope_key,
        visit_count=int(v_count) if isinstance(v_count, (int, float)) else None,
        comparison_period=comp_period,
        supported_cost=supp_cost,
        adjusted_pos_units=adj_pos,
        eligible_stores=[
            InsightEligibleStore(
                store_key=s["store_key"], store_label=s["store_label"]
            )
            for s in elig
        ],
        current_action_uuid=str(act.uuid) if act else None,
        current_action_type=act.action_type if act else None,
        current_action_status=act.status if act else None,
        current_action_owner=(
            act.owner.email
            if act and act.owner
            else (act.provider.name if act and act.provider else None)
        ),
        current_action_provider=(
            act.provider.name if act and act.provider else None
        ),
    )


def _review_item(row) -> BrandInsightReviewItem:
    return BrandInsightReviewItem(
        id=strawberry.ID(f"{_ID_PREFIX}{row.id}"),
        kind=row.kind,
        code=row.code,
        subject=row.subject,
        text=row.body,
        template=row.template,
        facts_text=row.facts_text,
        source=row.source,
        status=row.status,
        period_start=row.period_start.isoformat(),
        period_end=row.period_end.isoformat(),
        generated_at=row.generated_at.isoformat(),
        reviewed_by_name=_person(row.reviewed_by),
        reviewed_at=row.reviewed_at.isoformat() if row.reviewed_at else None,
    )


@strawberry.type
class BrandInsightQueries:
    @strawberry.field(permission_classes=[StrictIsAuthenticated])
    async def tenant_store_insights(
        self, info: strawberry.Info, tenant_id: strawberry.ID
    ) -> TenantStoreInsights | None:
        """Recommendations and alerts for a brand's dashboard, or null."""
        from recaps.report_types import _CampaignReportService

        service = _CampaignReportService()
        try:
            target = await service.resolve_target_tenant_id(info, tenant_id)
            staff = await service.is_staff_caller(info)
        except Exception:  # noqa: BLE001
            return None
        if target is None:
            return None

        def _build():
            from brand_insights.service import brand_view
            from pilot.models import StoreRecommendationAction
            from pilot.permissions import resolve_workspace_access
            from tenants.models import Tenant

            tenant_obj = Tenant.objects.filter(id=target).first()
            if tenant_obj is None:
                return None
            access = None
            if not staff:
                try:
                    access = resolve_workspace_access(
                        info.context.request.user,
                        tenant=tenant_obj,
                        request=info.context.request,
                    )
                except PermissionError:
                    return None
                if not access.can_view_brand_consolidated:
                    return None
                if is_client_billing_launched() and not resolve_brand_entitlements(
                    tenant_obj
                ).allows_store_recommendations:
                    return TenantStoreInsights(recommendations=[], alerts=[])
            else:
                try:
                    access = resolve_workspace_access(
                        info.context.request.user,
                        tenant=tenant_obj,
                        request=info.context.request,
                    )
                except PermissionError:
                    access = None

            latest_actions: dict[str, StoreRecommendationAction] = {}
            for a in StoreRecommendationAction.objects.select_related(
                "owner", "provider"
            ).filter(tenant_id=target).order_by("-created_at", "-id"):
                latest_actions.setdefault(a.store_key, a)

            view = brand_view(target, include_drafts=staff)
            return TenantStoreInsights(
                recommendations=[
                    _brand_item(r, access=access, latest_actions=latest_actions)
                    for r in view["recommendations"]
                ],
                alerts=[
                    _brand_item(r, access=access, latest_actions=latest_actions)
                    for r in view["alerts"]
                ],
            )

        try:
            return await sync_to_async(_build, thread_sensitive=True)()
        except Exception:  # noqa: BLE001
            logger.exception("tenantStoreInsights failed tenant=%s", target)
            return None

    @strawberry.field(permission_classes=[StrictIsAuthenticated])
    async def brand_insight_review(
        self, info: strawberry.Info
    ) -> list[BrandInsightReviewBrand] | None:
        """Every brand's current findings. Ignite staff only; null otherwise."""
        if await _staff_user(info) is None:
            return None

        def _build():
            from brand_insights.service import review_rows

            return [
                BrandInsightReviewBrand(
                    tenant_id=strawberry.ID(str(r["tenant_id"])),
                    tenant_name=r["tenant_name"],
                    drafts=r["drafts"],
                    items=[_review_item(i) for i in r["items"]],
                )
                for r in review_rows()
            ]

        try:
            return await sync_to_async(_build, thread_sensitive=True)()
        except Exception:  # noqa: BLE001
            logger.exception("brandInsightReview failed")
            return None


def _fail(reason: str) -> BrandInsightResult:
    return BrandInsightResult(ok=False, reason=reason, item=None)


@strawberry.type
class BrandInsightMutations:
    @strawberry.mutation(permission_classes=[StrictIsAuthenticated])
    async def set_brand_insight_status(
        self, info: strawberry.Info, id: strawberry.ID, status: str
    ) -> BrandInsightResult:
        """Publish one finding, hide it, or put it back to draft. Staff only.
        Publishing shows it on the brand's dashboard; it sends nothing."""
        user = await _staff_user(info)
        if user is None:
            return _fail("Not available.")
        rid = _row_id(id)
        if rid is None or status not in ("draft", "published", "hidden"):
            return _fail("Not found.")

        def _save():
            from brand_insights.service import set_status

            row = set_status(rid, status, user)
            return _review_item(row) if row is not None else None

        item = await sync_to_async(_save, thread_sensitive=True)()
        if item is None:
            return _fail("Not found.")
        return BrandInsightResult(ok=True, reason=None, item=item)

    @strawberry.mutation(permission_classes=[StrictIsAuthenticated])
    async def publish_brand_insights(
        self,
        info: strawberry.Info,
        tenant_id: strawberry.ID,
        ids: list[strawberry.ID] | None = None,
    ) -> BrandInsightBatchResult:
        """Publish every current draft for one brand (or only ``ids``). Staff
        only. Sends nothing."""
        user = await _staff_user(info)
        if user is None:
            return BrandInsightBatchResult(ok=False, reason="Not available.", count=0)
        try:
            tid = resolve_id_to_int(str(tenant_id).strip())
        except Exception:  # noqa: BLE001
            return BrandInsightBatchResult(ok=False, reason="Pick a brand.", count=0)
        wanted = None
        if ids is not None:
            wanted = [i for i in (_row_id(raw) for raw in ids) if i is not None]

        def _save():
            from brand_insights.service import publish_drafts

            return publish_drafts(tid, user, wanted)

        count = await sync_to_async(_save, thread_sensitive=True)()
        return BrandInsightBatchResult(ok=True, reason=None, count=count)

    @strawberry.mutation(permission_classes=[StrictIsAuthenticated])
    async def generate_brand_insights(
        self, info: strawberry.Info, tenant_id: strawberry.ID
    ) -> BrandInsightBatchResult:
        """Work out one brand's findings again now. New and changed ones are
        drafts; published and hidden ones are left as they are. Staff only."""
        if await _staff_user(info) is None:
            return BrandInsightBatchResult(ok=False, reason="Not available.", count=0)
        try:
            tid = resolve_id_to_int(str(tenant_id).strip())
        except Exception:  # noqa: BLE001
            return BrandInsightBatchResult(ok=False, reason="Pick a brand.", count=0)

        def _run():
            from brand_insights.service import generate_for_tenant
            from tenants.models import Tenant

            if not Tenant.objects.filter(id=tid).exists():
                return None
            return generate_for_tenant(tid)

        try:
            counts = await sync_to_async(_run, thread_sensitive=True)()
        except Exception:  # noqa: BLE001
            logger.exception("generateBrandInsights failed tenant=%s", tid)
            return BrandInsightBatchResult(
                ok=False, reason="Findings could not be worked out right now.", count=0
            )
        if counts is None:
            return BrandInsightBatchResult(ok=False, reason="Pick a brand.", count=0)
        return BrandInsightBatchResult(ok=True, reason=None, count=counts["written"])
