"""GraphQL queries for client invoicing (clients schema).

Two read surfaces, both admin/console + tenant-scoped exactly like the
receipts list (see ``receipts.queries``):

* ``invoices(filters)`` — the tenant's invoices, optionally filtered by
  ``status``, soft-deleted rows excluded, newest first. Client-role users are
  forced to their own tenant; an unrestricted role with no tenant in scope
  gets an EMPTY list (never every tenant's invoices).
* ``invoice(id)`` — a single invoice by UUID **or** numeric pk (accepts both,
  like ``recaps.report_service.get_report_request``), tenant-scoped the same
  way. Returns ``null`` when missing / soft-deleted / out of scope.
"""

from __future__ import annotations

import uuid as _uuid

import strawberry
from asgiref.sync import sync_to_async
from graphql import GraphQLError

from django.db.models import Model, QuerySet

from billing import models, types
from billing.inputs import InvoiceFiltersInput
from utils.graphql.mixins import SparkGraphQLMixin, resolve_id_to_int
from utils.graphql.permissions import (
    IGNITE_EMAIL_DOMAIN,
    StrictIsAuthenticated,
    resolve_request_user_access,
)


# ---------------------------------------------------------------------------
# Tenant-scoping helpers. Identical posture to receipts
# (`_enforce_client_tenant` / `_require_admin_or_client`): a client-role user
# is pinned to their own tenant regardless of any `filters.tenant_id`, closing
# the cross-tenant read footgun. Admins (spark-admin / staff / superuser /
# @igniteproductions.co) keep the pass-through behavior.
# ---------------------------------------------------------------------------
async def _enforce_client_tenant(
    service: SparkGraphQLMixin,
    info: strawberry.Info,
    filters_tenant_id: int | None,
) -> int | None:
    user = await service.get_user(info)
    role_slug = service.get_role_slug(user)
    if role_slug == "client":
        tenant = await service.get_user_tenant(info, tenant_id=filters_tenant_id)
        return tenant.id
    return filters_tenant_id


async def _require_admin_or_client(info: strawberry.Info) -> None:
    """Authorize a tenant-side user (client) or any admin.

    Invoicing is an admin/console surface. We resolve the role
    authoritatively (the JWT user.role FK is often unhydrated inside async
    resolvers) and allow clients + admins; anyone else is rejected.
    ``StrictIsAuthenticated`` already gated authentication.
    """
    request = getattr(info.context, "request", None)
    user = getattr(request, "user", None) if request else None
    if user is None or not getattr(user, "is_authenticated", False):
        raise GraphQLError("Authentication required.")
    role_slug, is_staff, is_super, email = await resolve_request_user_access(user)
    if (
        is_staff
        or is_super
        or role_slug in {"spark-admin", "client"}
        or (email or "").lower().endswith(IGNITE_EMAIL_DOMAIN)
    ):
        return
    raise GraphQLError("You do not have permission to view invoices.")


class InvoiceQueriesService(SparkGraphQLMixin):
    """Service encapsulating the invoices queryset + tenant scoping."""

    def get_model(self) -> Model:
        return models.Invoice

    def get_queryset(self) -> QuerySet:
        """Base queryset with the relations the type's resolvers read.

        ``select_related("tenant")`` so ``clientName`` reads a prefetched
        object; ``prefetch_related("line_items")`` so ``lineItems`` resolves
        with no N+1. Soft-deleted invoices are excluded here so every read
        surface inherits the exclusion.
        """
        return (
            models.Invoice.objects.filter(deleted_at__isnull=True)
            .select_related("tenant")
            .prefetch_related("line_items")
        )

    def get_ordered_queryset(
        self,
        *,
        tenant_id: int,
        status: str | None = None,
    ) -> QuerySet:
        queryset = self.get_queryset().filter(tenant_id=tenant_id)
        if status:
            queryset = queryset.filter(status=status)
        # Newest first — matches the model Meta ordering and the composite
        # (tenant, status, -created_at) index.
        return queryset.order_by("-created_at")


@strawberry.type
class BillingQueries:
    @strawberry.field(permission_classes=[StrictIsAuthenticated])
    async def invoices(
        self,
        info: strawberry.Info,
        filters: InvoiceFiltersInput | None = None,
    ) -> list[types.InvoiceType]:
        """Tenant-scoped list of invoices (excludes soft-deleted; newest first)."""
        await _require_admin_or_client(info)
        service = InvoiceQueriesService()
        await service.get_user(info)

        filters_tenant_id_raw: int | None = (
            resolve_id_to_int(filters.tenant_id)
            if filters and filters.tenant_id not in (None, "")
            else None
        )
        tenant_id = await _enforce_client_tenant(
            service, info, filters_tenant_id_raw
        )
        # No tenant in scope (unrestricted role, no tenant passed) → EMPTY
        # list, never every tenant's invoices. Same hard scope as receipts.
        if not tenant_id:
            return []

        status: str | None = (
            filters.status if filters and filters.status else None
        )

        queryset = service.get_ordered_queryset(
            tenant_id=tenant_id, status=status
        )
        return await sync_to_async(list, thread_sensitive=True)(queryset)

    @strawberry.field(permission_classes=[StrictIsAuthenticated])
    async def invoice(
        self,
        info: strawberry.Info,
        id: strawberry.ID,
    ) -> types.InvoiceType | None:
        """A single invoice by UUID or numeric pk; tenant-scoped.

        Returns ``null`` when the invoice doesn't exist, is soft-deleted, or
        is out of the caller's tenant scope (a client asking for another
        tenant's invoice). Accepts either the invoice UUID or its numeric pk,
        like ``recaps.report_service.get_report_request``.
        """
        await _require_admin_or_client(info)
        service = InvoiceQueriesService()
        await service.get_user(info)

        identifier = str(id).strip()
        if not identifier:
            return None

        # Client-role users are pinned to their own tenant; admins (None) see
        # any tenant's invoice.
        scope_tenant_id = await _enforce_client_tenant(service, info, None)

        def _load() -> models.Invoice | None:
            qs = service.get_queryset()
            if scope_tenant_id:
                qs = qs.filter(tenant_id=scope_tenant_id)
            raw = identifier
            try:
                _uuid.UUID(raw)
            except (ValueError, AttributeError, TypeError):
                # Not a uuid → treat it as a numeric / global pk.
                try:
                    return qs.filter(id=resolve_id_to_int(raw)).first()
                except (ValueError, TypeError, GraphQLError):
                    return None
            return qs.filter(uuid=raw).first()

        return await sync_to_async(_load, thread_sensitive=True)()
