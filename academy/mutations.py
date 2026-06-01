"""Academy GraphQL mutations — admin-only writes."""

from __future__ import annotations

import strawberry
from asgiref.sync import sync_to_async
from graphql import GraphQLError

from utils.graphql.permissions import StrictIsAuthenticated
from utils.graphql.mixins import SparkGraphQLMixin

from . import models, types
from .academy_scope import AcademyScope
from .inputs import (
    CreateAcademyModuleInput,
    UpdateAcademyModuleInput,
    DeleteAcademyModuleInput,
)


class _AcademyService(SparkGraphQLMixin):
    """Shared helpers — only the auth/user resolution from the mixin
    is used here. Writes are direct ORM calls because the model has
    a tiny surface area.
    """


@strawberry.type
class AcademyMutations:
    @strawberry.mutation(permission_classes=[StrictIsAuthenticated])
    async def create_academy_module(
        self, info: strawberry.Info, input: CreateAcademyModuleInput
    ) -> types.AcademyModuleResponse:
        svc = _AcademyService()
        user = await svc.get_user(info)

        # Tenant-scoped: a client always creates under their OWN tenant (any
        # supplied tenant_id is ignored); an admin may target the requested
        # tenant. A client with no tenant, or an admin who passed no usable
        # tenant id, gets a safe success=False rather than a cross-tenant write.
        try:
            tenant_id = await AcademyScope().resolve_target_tenant_id(
                info, input.tenant_id
            )
        except Exception:  # noqa: BLE001
            tenant_id = None

        if not tenant_id:
            return types.AcademyModuleResponse(
                success=False,
                message="No tenant in scope to create the academy module under.",
            )

        try:
            module = await sync_to_async(models.AcademyModule.objects.create)(
                tenant_id=tenant_id,
                title=input.title.strip()[:200],
                kind=input.kind or "training",
                body=input.body or "",
                order=int(input.order or 0),
                published=bool(input.published),
                created_by=user,
                updated_by=user,
            )
        except Exception as exc:  # noqa: BLE001
            return types.AcademyModuleResponse(
                success=False,
                message=f"Failed to create academy module: {exc}",
            )

        return types.AcademyModuleResponse(
            success=True,
            message="Academy module created.",
            academy_module=module,
        )

    @strawberry.mutation(permission_classes=[StrictIsAuthenticated])
    async def update_academy_module(
        self, info: strawberry.Info, input: UpdateAcademyModuleInput
    ) -> types.AcademyModuleResponse:
        svc = _AcademyService()
        user = await svc.get_user(info)

        # Tenant gate: a client may only edit their OWN tenant's modules; an
        # out-of-scope module is surfaced as "not found" so we don't leak
        # cross-tenant existence. Admins may edit any tenant's module.
        try:
            allowed = await AcademyScope().accessible_tenant_ids(info)
        except Exception:  # noqa: BLE001
            return types.AcademyModuleResponse(
                success=False, message="Academy module not found."
            )

        try:
            module = await sync_to_async(
                models.AcademyModule.objects.get
            )(uuid=str(input.uuid))
        except models.AcademyModule.DoesNotExist:
            return types.AcademyModuleResponse(
                success=False, message="Academy module not found."
            )

        if allowed is not None and module.tenant_id not in allowed:
            return types.AcademyModuleResponse(
                success=False, message="Academy module not found."
            )

        if input.title is not None:
            module.title = input.title.strip()[:200]
        if input.kind is not None:
            module.kind = input.kind
        if input.body is not None:
            module.body = input.body
        if input.order is not None:
            module.order = int(input.order)
        if input.published is not None:
            module.published = bool(input.published)
        module.updated_by = user

        try:
            await sync_to_async(module.save)()
        except Exception as exc:  # noqa: BLE001
            return types.AcademyModuleResponse(
                success=False,
                message=f"Failed to update academy module: {exc}",
            )

        return types.AcademyModuleResponse(
            success=True,
            message="Academy module updated.",
            academy_module=module,
        )

    @strawberry.mutation(permission_classes=[StrictIsAuthenticated])
    async def delete_academy_module(
        self, info: strawberry.Info, input: DeleteAcademyModuleInput
    ) -> types.AcademyModuleResponse:
        svc = _AcademyService()
        await svc.get_user(info)

        # Tenant gate: a client may only delete their OWN tenant's modules; an
        # out-of-scope module is surfaced as "not found" (no cross-tenant
        # existence leak / deletion). Admins may delete any tenant's module.
        try:
            allowed = await AcademyScope().accessible_tenant_ids(info)
        except Exception:  # noqa: BLE001
            return types.AcademyModuleResponse(
                success=False, message="Academy module not found."
            )

        try:
            module = await sync_to_async(
                models.AcademyModule.objects.get
            )(uuid=str(input.uuid))
        except models.AcademyModule.DoesNotExist:
            return types.AcademyModuleResponse(
                success=False, message="Academy module not found."
            )

        if allowed is not None and module.tenant_id not in allowed:
            return types.AcademyModuleResponse(
                success=False, message="Academy module not found."
            )

        try:
            await sync_to_async(module.delete)()
        except Exception as exc:  # noqa: BLE001
            return types.AcademyModuleResponse(
                success=False,
                message=f"Failed to delete academy module: {exc}",
            )

        return types.AcademyModuleResponse(
            success=True,
            message="Academy module deleted.",
        )
