"""Academy GraphQL queries.

For the v1 ship we return a flat list (not a Relay connection)
because tenants typically have <50 modules. The mobile + admin
clients can render the full list in one query without pagination.
"""

from __future__ import annotations

from typing import List

import strawberry
from asgiref.sync import sync_to_async

from utils.graphql.permissions import StrictIsAuthenticated
from utils.graphql.mixins import resolve_id_to_int

from . import models, types
from .academy_scope import AcademyScope
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
        """List a tenant's academy modules (drafts included).

        Tenant-scoped: clients see only their OWN tenant's modules (the
        ``tenant_id`` filter is overridden to their tenant); admins see the
        requested tenant's modules. Never raises past the auth gate — returns
        ``[]`` for an out-of-scope/garbage request or on error.
        """
        requested_tenant_id = (
            filters.tenant_id if filters and filters.tenant_id not in (None, "")
            else None
        )
        try:
            tenant_id = await AcademyScope().resolve_target_tenant_id(
                info, requested_tenant_id
            )
        except Exception:  # noqa: BLE001
            return []
        # Clients always resolve to their own tenant; an admin with no usable
        # tenant in scope sees nothing rather than every tenant's modules.
        if not tenant_id:
            return []

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
        """Fetch one academy module by uuid.

        Tenant-scoped: returns ``null`` when the module doesn't exist or its
        tenant is outside the caller's scope (a client can't read another
        brand's module by guessing/holding its uuid). Never raises.
        """
        scope = AcademyScope()
        try:
            allowed = await scope.accessible_tenant_ids(info)
        except Exception:  # noqa: BLE001
            return None

        def _load() -> types.AcademyModule | None:
            module = models.AcademyModule.objects.filter(uuid=str(uuid)).first()
            if module is None:
                return None
            if allowed is not None and module.tenant_id not in allowed:
                return None
            return module

        return await sync_to_async(_load)()


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
