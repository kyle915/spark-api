"""Brand-facing "Connected AI apps" queries on the clients schema.

A brand user sees and revokes only their own connections. Ignite admins
(same DB-backed rule as the rest of the API) see and can revoke everyone's:
the Django admin is not mounted on this service, so this plus the
``mcp_connections`` management command is how staff manage connections.
"""

from __future__ import annotations

import strawberry
from asgiref.sync import sync_to_async
from django.utils import timezone

from utils.graphql.permissions import StrictIsAuthenticated

from . import conf


@strawberry.type
class AiConnection:
    id: strawberry.ID
    app_name: str
    brand_name: str
    # Only filled in for Ignite admins looking at everyone's connections.
    user_email: str | None
    created_at: str
    last_used_at: str | None
    calls_last_30_days: int


@strawberry.type
class AiConnectorInfo:
    connector_url: str
    help_url: str
    daily_call_limit: int
    is_admin_view: bool
    connections: list[AiConnection]


@strawberry.type
class RevokeAiConnectionResult:
    ok: bool
    message: str


def _user(info):
    user = info.context.request.user
    return user if getattr(user, "is_authenticated", False) else None


def _connections_for(user_id: int, *, everyone: bool) -> list[AiConnection]:
    from datetime import timedelta

    from django.db.models import Count, Q

    from .models import McpConnection

    since = timezone.now() - timedelta(days=30)
    qs = McpConnection.objects.filter(revoked_at__isnull=True)
    if not everyone:
        qs = qs.filter(user_id=user_id)
    rows = (
        qs.select_related("client", "tenant", "user")
        .annotate(recent=Count("calls", filter=Q(calls__created_at__gte=since)))
        .order_by("-created_at")[:100]
    )
    return [
        AiConnection(
            id=strawberry.ID(str(c.uuid)),
            app_name=c.client.display_name,
            brand_name=c.tenant.name,
            user_email=c.user.email if everyone else None,
            created_at=c.created_at.isoformat(),
            last_used_at=c.last_used_at.isoformat() if c.last_used_at else None,
            calls_last_30_days=c.recent,
        )
        for c in rows
    ]


@strawberry.type
class AiConnectionQueries:
    @strawberry.field(permission_classes=[StrictIsAuthenticated])
    async def my_ai_connections(self, info: strawberry.Info) -> AiConnectorInfo:
        """The signed-in person's connected AI apps (Claude, ChatGPT, ...)."""
        from .access import is_admin

        user = _user(info)
        admin = await sync_to_async(is_admin, thread_sensitive=True)(user)
        connections = await sync_to_async(_connections_for, thread_sensitive=True)(
            user.pk, everyone=admin
        )
        return AiConnectorInfo(
            is_admin_view=admin,
            connector_url=conf.resource_url(),
            help_url=f"{conf.frontend_url()}/help/ai-connector",
            daily_call_limit=conf.daily_calls_per_tenant(),
            connections=connections,
        )


@strawberry.type
class AiConnectionMutations:
    @strawberry.mutation(permission_classes=[StrictIsAuthenticated])
    async def revoke_ai_connection(
        self, info: strawberry.Info, id: strawberry.ID
    ) -> RevokeAiConnectionResult:
        """Disconnect an AI app (your own; Ignite admins: anyone's). Its tokens
        stop working at once."""
        user = _user(info)

        def _revoke() -> bool:
            from .access import is_admin
            from .models import McpConnection

            qs = McpConnection.objects.filter(uuid=str(id), revoked_at__isnull=True)
            staff = is_admin(user)
            if not staff:
                qs = qs.filter(user_id=user.pk)
            conn = qs.first()
            if conn is None:
                return False
            conn.revoke(
                by=user,
                reason="revoked by Ignite staff in Spark" if staff else "revoked by the user in Spark",
            )
            return True

        try:
            ok = await sync_to_async(_revoke, thread_sensitive=True)()
        except Exception:  # noqa: BLE001 — malformed id etc.
            ok = False
        return RevokeAiConnectionResult(
            ok=ok, message="Disconnected." if ok else "Connection not found."
        )
