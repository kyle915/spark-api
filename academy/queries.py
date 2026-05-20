"""Academy GraphQL queries.

For the v1 ship we return a flat list (not a Relay connection)
because tenants typically have <50 modules. The mobile + admin
clients can render the full list in one query without pagination.
"""

from __future__ import annotations

from typing import List

import strawberry
from asgiref.sync import sync_to_async
from graphql import GraphQLError

from utils.graphql.permissions import StrictIsAuthenticated
from utils.graphql.mixins import resolve_id_to_int

from . import models, types
from .inputs import AcademyModuleFiltersInput


def _filtered_queryset(
    *,
    tenant_id: int | None,
    kind: str | None,
    published: bool | None,
):
    qs = models.AcademyModule.objects.all()
    if tenant_id:
        qs = qs.filter(tenant_id=tenant_id)
    if kind:
        qs = qs.filter(kind=kind)
    if published is not None:
        qs = qs.filter(published=published)
    return qs.order_by("order", "-updated_at")


@strawberry.type
class AcademyQueries:
    """Admin-side queries: returns *all* modules (drafts included)
    so the front-client can show unpublished entries in the manage UI.
    """

    @strawberry.field(permission_classes=[StrictIsAuthenticated])
    async def academy_modules_admin(
        self,
        info: strawberry.Info,
        filters: AcademyModuleFiltersInput | None = None,
    ) -> List[types.AcademyModule]:
        tenant_id: int | None = (
            resolve_id_to_int(filters.tenant_id)
            if filters and filters.tenant_id not in (None, "")
            else None
        )
        kind = filters.kind if filters else None
        published = filters.published if filters else None
        qs = _filtered_queryset(
            tenant_id=tenant_id, kind=kind, published=published
        )
        return await sync_to_async(list)(qs)

    @strawberry.field(permission_classes=[StrictIsAuthenticated])
    async def academy_module(
        self, info: strawberry.Info, uuid: strawberry.ID
    ) -> types.AcademyModule | None:
        try:
            return await sync_to_async(
                models.AcademyModule.objects.get
            )(uuid=str(uuid))
        except models.AcademyModule.DoesNotExist:
            raise GraphQLError("Academy module not found.")


@strawberry.type
class AcademyMobileQueries:
    """BA-facing query: hard-filters to `published=True` so drafts
    never leak to the mobile app, regardless of caller intent.
    """

    @strawberry.field(permission_classes=[StrictIsAuthenticated])
    async def academy_modules(
        self,
        info: strawberry.Info,
        filters: AcademyModuleFiltersInput | None = None,
    ) -> List[types.AcademyModule]:
        tenant_id: int | None = (
            resolve_id_to_int(filters.tenant_id)
            if filters and filters.tenant_id not in (None, "")
            else None
        )
        kind = filters.kind if filters else None
        qs = _filtered_queryset(
            tenant_id=tenant_id, kind=kind, published=True
        )
        return await sync_to_async(list)(qs)
