"""GraphQL queries for the Consumer Receipt Validation feature (clients schema).

Two read surfaces, both admin-only + tenant-scoped:

* `receipts` — the review queue. Relay-paginated, filterable by `status` and
  `eventId`, scoped to the caller's tenant. Mirrors the recaps list: client
  -role users are forced to their own tenant, and an unrestricted role with
  no tenant in scope gets an EMPTY page (never every tenant's receipts).
* `eventReceiptUploadLink` — returns the public upload URL + per-event token
  so the admin UI can render the shareable link and a QR.
"""

from __future__ import annotations

import strawberry
from asgiref.sync import sync_to_async
from graphql import GraphQLError

from django.conf import settings
from django.db.models import Model, QuerySet

from events.models import Event
from receipts import models, types
from receipts.inputs import ConsumerReceiptFiltersInput
from receipts.tokens import make_event_receipt_token
from utils.graphql.mixins import SparkGraphQLMixin, resolve_id_to_int
from utils.graphql.permissions import (
    IGNITE_EMAIL_DOMAIN,
    StrictIsAuthenticated,
    resolve_request_user_access,
)
from utils.graphql.relay import (
    CountableConnection,
    connection_from_queryset_async,
)

# Page ceiling for the receipts admin queue. The web admin loads a tenant's
# receipts and filters client-side, so we lift the default 50 cap the same
# way the recaps list does (RECAPS_LIST_MAX_LIMIT). default_limit is left at
# the service default, so callers passing no `first` still get a small page.
RECEIPTS_LIST_MAX_LIMIT = 2000


# ---------------------------------------------------------------------------
# Tenant-scoping helper. Identical posture to recaps `_enforce_client_tenant`:
# a client-role user is pinned to their own tenant regardless of what
# filters.tenant_id carried, closing the cross-tenant read footgun. Admins
# (spark-admin / staff / superuser / @igniteproductions.co) keep the
# pass-through behavior.
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

    `receipts` / `eventReceiptUploadLink` are admin/console surfaces. We
    resolve the role authoritatively (the JWT user.role FK is often
    unhydrated inside async resolvers) and allow clients + admins; anyone
    else is rejected. `StrictIsAuthenticated` already gated authentication.
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
    raise GraphQLError("You do not have permission to view receipts.")


class ConsumerReceiptQueriesService(SparkGraphQLMixin):
    """Service encapsulating the receipts queryset + tenant scoping."""

    def get_model(self) -> Model:
        return models.ConsumerReceipt

    def get_queryset(self) -> QuerySet:
        """Base queryset with the relations the type's resolvers read.

        `select_related` on event / tenant / reviewed_by means the
        `publicUrl` / `eventName` / `reviewedBy` field resolvers all read
        prefetched objects synchronously — no per-row `sync_to_async`.
        """
        return models.ConsumerReceipt.objects.select_related(
            "event",
            "tenant",
            "reviewed_by",
        )

    def get_ordered_queryset(
        self,
        *,
        tenant_id: int,
        event_id: int | None = None,
        status: str | None = None,
    ) -> QuerySet:
        queryset = self.get_queryset().filter(tenant_id=tenant_id)
        if event_id is not None:
            queryset = queryset.filter(event_id=event_id)
        if status:
            queryset = queryset.filter(status=status)
        # Newest submissions first — matches the model Meta ordering and the
        # composite (tenant, status, -submitted_at) index.
        return queryset.order_by("-submitted_at")


@strawberry.type
class ReceiptQueries:
    @strawberry.field(permission_classes=[StrictIsAuthenticated])
    async def receipts(
        self,
        info: strawberry.Info,
        first: int | None = None,
        after: str | None = None,
        last: int | None = None,
        before: str | None = None,
        filters: ConsumerReceiptFiltersInput | None = None,
    ) -> CountableConnection[types.ConsumerReceiptType]:
        """Tenant-scoped consumer-receipt review queue (Relay pagination)."""
        await _require_admin_or_client(info)
        service = ConsumerReceiptQueriesService()
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
        # page, never every tenant's receipts. Same hard scope as recaps.
        if not tenant_id:
            empty = service.get_model().objects.none()
            return await connection_from_queryset_async(
                empty,
                first=first,
                after=after,
                last=last,
                before=before,
                max_limit=RECEIPTS_LIST_MAX_LIMIT,
            )

        event_id: int | None = (
            resolve_id_to_int(filters.event_id)
            if filters and filters.event_id not in (None, "")
            else None
        )
        status: str | None = (
            filters.status if filters and filters.status else None
        )

        queryset = service.get_ordered_queryset(
            tenant_id=tenant_id,
            event_id=event_id,
            status=status,
        )
        return await connection_from_queryset_async(
            queryset,
            first=first,
            after=after,
            last=last,
            before=before,
            max_limit=RECEIPTS_LIST_MAX_LIMIT,
        )

    @strawberry.field(permission_classes=[StrictIsAuthenticated])
    async def event_receipt_upload_link(
        self,
        info: strawberry.Info,
        event_id: strawberry.ID,
    ) -> types.EventReceiptUploadLinkType:
        """Return the public upload URL + per-event token for an event.

        Tenant-scoped: client-role users can only mint a link for an event
        in their own tenant. The token is the same `TimestampSigner` token
        the public endpoints verify. The returned `url` is the absolute
        GET-resolve endpoint (`/api/public/receipts/<token>`); the admin UI
        can render that as a QR or wrap the token in its own upload-page
        URL.
        """
        await _require_admin_or_client(info)
        service = ConsumerReceiptQueriesService()
        user = await service.get_user(info)

        try:
            resolved_event_id = resolve_id_to_int(event_id)
        except (TypeError, ValueError, GraphQLError):
            raise GraphQLError("Invalid event ID.")

        try:
            event = await sync_to_async(
                Event.objects.select_related("tenant").get
            )(id=resolved_event_id)
        except Event.DoesNotExist:
            raise GraphQLError("Event not found.")

        # Tenant gate: client-role users may only address their own tenant's
        # events. Admins pass through.
        if service.get_role_slug(user) == "client":
            user_tenant = await service.get_user_tenant(info)
            if event.tenant_id != user_tenant.id:
                raise GraphQLError("Event not found.")

        token = make_event_receipt_token(event.id)
        url = _build_public_receipt_url(info, token)
        return types.EventReceiptUploadLinkType(
            event_id=strawberry.ID(str(event.id)),
            token=token,
            url=url,
        )


def _build_public_receipt_url(info: strawberry.Info, token: str) -> str:
    """Build the absolute `/api/public/receipts/<token>` URL.

    Prefer the incoming request's scheme + host so the link is correct per
    environment (local / staging / prod) without extra config. Falls back
    to a relative path if the host can't be determined.
    """
    path = f"/api/public/receipts/{token}"
    request = getattr(info.context, "request", None)
    host = ""
    scheme = "https"
    try:
        # Strawberry's ASGI request wraps Django's; META lives on the inner
        # wsgi_request in tests and on the ASGI request's headers in prod.
        wsgi = getattr(request, "wsgi_request", None)
        if wsgi is not None:
            host = wsgi.get_host()
            scheme = "https" if wsgi.is_secure() else "http"
        else:
            headers = getattr(request, "headers", None)
            if headers:
                host = headers.get("host", "") or ""
    except Exception:
        host = ""
    if host:
        return f"{scheme}://{host}{path}"
    # Last resort: a configured public base, else the relative path.
    base = (getattr(settings, "CLIENT_FRONTEND_URL", "") or "").rstrip("/")
    return f"{base}{path}" if base else path
