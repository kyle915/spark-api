"""Activation Plans — FMM forward slates that group Requests.

Read-heavy leadership surface + attach/detach mutations. Execution still
flows Request → approve → staff → Today → Recap; a plan is only the
named grouping + pipeline counts leadership reviews before/while ops runs.
"""

from __future__ import annotations

import datetime
from typing import List

import strawberry
from asgiref.sync import sync_to_async
from django.db.models import Exists, OuterRef, Q
from graphql import GraphQLError
from strawberry import relay

from events import models
from events.inputs import BaseTenantInput
from utils.graphql.inputs import SparkGraphQLInput
from utils.graphql.mixins import (
    SparkGraphQLMixin,
    resolve_id_to_int,
)
from utils.graphql.permissions import StrictIsAuthenticated
from utils.utils import build_mutation_response


def _parse_date(value: str | None) -> datetime.date | None:
    if not value:
        return None
    raw = (value or "").strip()
    if not raw:
        return None
    try:
        return datetime.date.fromisoformat(raw[:10])
    except ValueError as exc:
        raise GraphQLError(f"Invalid date {value!r}; use YYYY-MM-DD.") from exc


def _clean_markets(markets: list[str] | None) -> list[str]:
    if not markets:
        return []
    out: list[str] = []
    seen: set[str] = set()
    for m in markets:
        label = (m or "").strip()
        if not label:
            continue
        key = label.upper()
        if key in seen:
            continue
        seen.add(key)
        out.append(label)
    return out


@strawberry.type
class ActivationPlanPipeline:
    """PINATA-style Planned → Approved → Staffed → Executed → Verified."""

    planned: int
    approved: int
    staffed: int
    executed: int
    verified: int


@strawberry.type
class ActivationPlanType:
    id: strawberry.ID
    uuid: str
    name: str
    concept_brief: str
    start_date: str | None
    end_date: str | None
    markets: List[str]
    request_count: int
    pipeline: ActivationPlanPipeline


@strawberry.type
class ActivationPlanDetailResponse:
    success: bool
    message: str
    client_mutation_id: strawberry.ID | None = None
    activation_plan: ActivationPlanType | None = None


@strawberry.type
class AttachRequestsToPlanResponse:
    success: bool
    message: str
    client_mutation_id: strawberry.ID | None = None
    updated_count: int = 0
    activation_plan: ActivationPlanType | None = None


@strawberry.input
class CreateActivationPlanInput(BaseTenantInput):
    name: str
    concept_brief: str | None = None
    start_date: str | None = None
    end_date: str | None = None
    markets: list[str] | None = None
    owner_id: strawberry.ID | None = None


@strawberry.input
class UpdateActivationPlanInput(SparkGraphQLInput):
    id: strawberry.ID
    name: str | None = None
    concept_brief: str | None = None
    start_date: str | None = None
    end_date: str | None = None
    markets: list[str] | None = None
    owner_id: strawberry.ID | None = None
    clear_owner: bool | None = None


@strawberry.input
class DeleteActivationPlanInput(SparkGraphQLInput):
    id: strawberry.ID


@strawberry.input
class AttachRequestsToPlanInput(SparkGraphQLInput):
    activation_plan_id: strawberry.ID
    request_ids: list[strawberry.ID]


@strawberry.input
class DetachRequestsFromPlanInput(SparkGraphQLInput):
    request_ids: list[strawberry.ID]


def _pipeline_for_plan(plan_id: int) -> ActivationPlanPipeline:
    """Aggregate pipeline counts for live (non-deleted) requests on a plan."""
    from recaps import models as rm
    from recaps.filed import custom_filed_q, legacy_filed_q

    qs = models.Request.objects.filter(
        activation_plan_id=plan_id, deleted_at__isnull=True
    )
    planned = qs.count()
    approved = qs.filter(
        status__slug__in=["approved", "scheduled", "done"]
    ).count()
    staffed = qs.filter(status__slug__in=["scheduled", "done"]).count()

    filed_legacy = rm.Recap.objects.filter(
        legacy_filed_q(),
        event__request_id=OuterRef("pk"),
        archived_at__isnull=True,
    )
    filed_custom = rm.CustomRecap.objects.filter(
        custom_filed_q(),
        event__request_id=OuterRef("pk"),
        archived_at__isnull=True,
    )
    executed = (
        qs.annotate(has_filed=Exists(filed_legacy) | Exists(filed_custom))
        .filter(has_filed=True)
        .count()
    )

    verified_legacy = rm.Recap.objects.filter(
        legacy_filed_q(),
        event__request_id=OuterRef("pk"),
        approved=True,
        archived_at__isnull=True,
    )
    verified_custom = rm.CustomRecap.objects.filter(
        custom_filed_q(),
        event__request_id=OuterRef("pk"),
        approved=True,
        archived_at__isnull=True,
    )
    verified = (
        qs.annotate(
            has_verified=Exists(verified_legacy) | Exists(verified_custom)
        )
        .filter(has_verified=True)
        .count()
    )

    return ActivationPlanPipeline(
        planned=planned,
        approved=approved,
        staffed=staffed,
        executed=executed,
        verified=verified,
    )


def _serialize_plan(plan: models.ActivationPlan) -> ActivationPlanType:
    pipe = _pipeline_for_plan(plan.id)
    return ActivationPlanType(
        id=strawberry.ID(str(plan.id)),
        uuid=str(plan.uuid),
        name=plan.name or "",
        concept_brief=plan.concept_brief or "",
        start_date=plan.start_date.isoformat() if plan.start_date else None,
        end_date=plan.end_date.isoformat() if plan.end_date else None,
        markets=list(plan.markets or []),
        request_count=pipe.planned,
        pipeline=pipe,
    )


class ActivationPlanService(SparkGraphQLMixin):
    async def resolve_tenant_for_user(self, info, tenant_id: strawberry.ID | None):
        user = await self.get_user(info)
        is_spark = self.is_spark_schema_request(info, user=user)
        if is_spark and tenant_id:
            return await self._get_tenant_without_membership(tenant_id)
        resolved = None
        if tenant_id not in (None, ""):
            resolved = resolve_id_to_int(tenant_id)
        return await self.get_tenant(user, resolved)


@strawberry.type
class ActivationPlanQueries:
    @strawberry.field(permission_classes=[StrictIsAuthenticated])
    async def activation_plans(
        self,
        info: strawberry.Info,
        tenant_id: strawberry.ID | None = None,
        first: int | None = 50,
    ) -> List[ActivationPlanType]:
        service = ActivationPlanService()
        tenant = await service.resolve_tenant_for_user(info, tenant_id)

        def _list() -> list[ActivationPlanType]:
            rows = list(
                models.ActivationPlan.objects.filter(tenant_id=tenant.id).order_by(
                    "-start_date", "-created_at"
                )[: max(1, min(first or 50, 200))]
            )
            return [_serialize_plan(p) for p in rows]

        return await sync_to_async(_list)()

    @strawberry.field(permission_classes=[StrictIsAuthenticated])
    async def activation_plan(
        self,
        info: strawberry.Info,
        id: strawberry.ID | None = None,
        uuid: str | None = None,
    ) -> ActivationPlanType | None:
        service = ActivationPlanService()
        user = await service.get_user(info)

        def _get() -> ActivationPlanType | None:
            qs = models.ActivationPlan.objects.all()
            if id:
                pk = resolve_id_to_int(id)
                plan = qs.filter(id=pk).first()
            elif uuid:
                plan = qs.filter(uuid=uuid).first()
            else:
                raise GraphQLError("Provide id or uuid.")
            if plan is None:
                return None
            if not service.is_spark_schema_request(info, user=user):
                from tenants.models import TenantedUser

                ok = TenantedUser.objects.filter(
                    user_id=user.id, tenant_id=plan.tenant_id, is_active=True
                ).exists()
                if not ok and not getattr(user, "is_staff", False):
                    raise GraphQLError("Not allowed to view this plan.")
            return _serialize_plan(plan)

        return await sync_to_async(_get)()


@strawberry.type
class ActivationPlanMutations:
    @relay.mutation(permission_classes=[StrictIsAuthenticated])
    async def create_activation_plan(
        self,
        info: strawberry.Info,
        input: CreateActivationPlanInput,
    ) -> ActivationPlanDetailResponse:
        service = ActivationPlanService()
        try:
            user = await service.get_user(info)
            tenant = await service.resolve_tenant_for_user(info, input.tenant_id)
            name = (input.name or "").strip()
            if not name:
                raise GraphQLError("Plan name is required.")

            owner_id = None
            if input.owner_id:
                owner_id = resolve_id_to_int(input.owner_id)

            def _create() -> models.ActivationPlan:
                return models.ActivationPlan.objects.create(
                    name=name,
                    concept_brief=(input.concept_brief or "").strip(),
                    start_date=_parse_date(input.start_date),
                    end_date=_parse_date(input.end_date),
                    markets=_clean_markets(input.markets),
                    tenant=tenant,
                    owner_id=owner_id or user.id,
                    created_by=user,
                    updated_by=user,
                )

            plan = await sync_to_async(_create)()
            serialized = await sync_to_async(_serialize_plan)(plan)
            return build_mutation_response(
                ActivationPlanDetailResponse,
                success=True,
                message="Plan created.",
                input_obj=input,
                activation_plan=serialized,
            )
        except GraphQLError as exc:
            return build_mutation_response(
                ActivationPlanDetailResponse,
                success=False,
                message=str(exc),
                input_obj=input,
            )
        except Exception as exc:  # noqa: BLE001
            return build_mutation_response(
                ActivationPlanDetailResponse,
                success=False,
                message=str(exc),
                input_obj=input,
            )

    @relay.mutation(permission_classes=[StrictIsAuthenticated])
    async def update_activation_plan(
        self,
        info: strawberry.Info,
        input: UpdateActivationPlanInput,
    ) -> ActivationPlanDetailResponse:
        service = ActivationPlanService()
        try:
            user = await service.get_user(info)
            pk = resolve_id_to_int(input.id)

            def _update() -> models.ActivationPlan:
                plan = models.ActivationPlan.objects.filter(id=pk).first()
                if plan is None:
                    raise GraphQLError("Plan not found.")
                if input.name is not None:
                    name = input.name.strip()
                    if not name:
                        raise GraphQLError("Plan name cannot be empty.")
                    plan.name = name
                if input.concept_brief is not None:
                    plan.concept_brief = input.concept_brief.strip()
                if input.start_date is not None:
                    plan.start_date = (
                        _parse_date(input.start_date) if input.start_date else None
                    )
                if input.end_date is not None:
                    plan.end_date = (
                        _parse_date(input.end_date) if input.end_date else None
                    )
                if input.markets is not None:
                    plan.markets = _clean_markets(input.markets)
                if input.clear_owner:
                    plan.owner_id = None
                elif input.owner_id:
                    plan.owner_id = resolve_id_to_int(input.owner_id)
                plan.updated_by = user
                plan.save()
                return plan

            plan = await sync_to_async(_update)()
            serialized = await sync_to_async(_serialize_plan)(plan)
            return build_mutation_response(
                ActivationPlanDetailResponse,
                success=True,
                message="Plan updated.",
                input_obj=input,
                activation_plan=serialized,
            )
        except GraphQLError as exc:
            return build_mutation_response(
                ActivationPlanDetailResponse,
                success=False,
                message=str(exc),
                input_obj=input,
            )
        except Exception as exc:  # noqa: BLE001
            return build_mutation_response(
                ActivationPlanDetailResponse,
                success=False,
                message=str(exc),
                input_obj=input,
            )

    @relay.mutation(permission_classes=[StrictIsAuthenticated])
    async def delete_activation_plan(
        self,
        info: strawberry.Info,
        input: DeleteActivationPlanInput,
    ) -> ActivationPlanDetailResponse:
        """Detach requests then delete the plan row (requests stay live)."""
        service = ActivationPlanService()
        try:
            await service.get_user(info)
            pk = resolve_id_to_int(input.id)

            def _delete() -> None:
                plan = models.ActivationPlan.objects.filter(id=pk).first()
                if plan is None:
                    raise GraphQLError("Plan not found.")
                models.Request.objects.filter(activation_plan_id=pk).update(
                    activation_plan_id=None
                )
                plan.delete()

            await sync_to_async(_delete)()
            return build_mutation_response(
                ActivationPlanDetailResponse,
                success=True,
                message="Plan deleted. Requests were detached, not deleted.",
                input_obj=input,
            )
        except GraphQLError as exc:
            return build_mutation_response(
                ActivationPlanDetailResponse,
                success=False,
                message=str(exc),
                input_obj=input,
            )
        except Exception as exc:  # noqa: BLE001
            return build_mutation_response(
                ActivationPlanDetailResponse,
                success=False,
                message=str(exc),
                input_obj=input,
            )

    @relay.mutation(permission_classes=[StrictIsAuthenticated])
    async def attach_requests_to_plan(
        self,
        info: strawberry.Info,
        input: AttachRequestsToPlanInput,
    ) -> AttachRequestsToPlanResponse:
        service = ActivationPlanService()
        try:
            user = await service.get_user(info)
            plan_id = resolve_id_to_int(input.activation_plan_id)
            req_ids = [resolve_id_to_int(rid) for rid in (input.request_ids or [])]
            if not req_ids:
                raise GraphQLError("Provide at least one request id.")

            def _attach() -> tuple[models.ActivationPlan, int]:
                plan = models.ActivationPlan.objects.filter(id=plan_id).first()
                if plan is None:
                    raise GraphQLError("Plan not found.")
                updated = models.Request.objects.filter(
                    id__in=req_ids,
                    tenant_id=plan.tenant_id,
                    deleted_at__isnull=True,
                ).update(activation_plan_id=plan.id)
                plan.updated_by = user
                plan.save(update_fields=["updated_by", "updated_at"])
                return plan, updated

            plan, updated = await sync_to_async(_attach)()
            serialized = await sync_to_async(_serialize_plan)(plan)
            return build_mutation_response(
                AttachRequestsToPlanResponse,
                success=True,
                message=f"Attached {updated} request(s) to plan.",
                input_obj=input,
                updated_count=updated,
                activation_plan=serialized,
            )
        except GraphQLError as exc:
            return build_mutation_response(
                AttachRequestsToPlanResponse,
                success=False,
                message=str(exc),
                input_obj=input,
            )
        except Exception as exc:  # noqa: BLE001
            return build_mutation_response(
                AttachRequestsToPlanResponse,
                success=False,
                message=str(exc),
                input_obj=input,
            )

    @relay.mutation(permission_classes=[StrictIsAuthenticated])
    async def detach_requests_from_plan(
        self,
        info: strawberry.Info,
        input: DetachRequestsFromPlanInput,
    ) -> AttachRequestsToPlanResponse:
        service = ActivationPlanService()
        try:
            await service.get_user(info)
            req_ids = [resolve_id_to_int(rid) for rid in (input.request_ids or [])]
            if not req_ids:
                raise GraphQLError("Provide at least one request id.")

            def _detach() -> int:
                return models.Request.objects.filter(
                    id__in=req_ids, deleted_at__isnull=True
                ).update(activation_plan_id=None)

            updated = await sync_to_async(_detach)()
            return build_mutation_response(
                AttachRequestsToPlanResponse,
                success=True,
                message=f"Detached {updated} request(s) from plan.",
                input_obj=input,
                updated_count=updated,
            )
        except GraphQLError as exc:
            return build_mutation_response(
                AttachRequestsToPlanResponse,
                success=False,
                message=str(exc),
                input_obj=input,
            )
        except Exception as exc:  # noqa: BLE001
            return build_mutation_response(
                AttachRequestsToPlanResponse,
                success=False,
                message=str(exc),
                input_obj=input,
            )
