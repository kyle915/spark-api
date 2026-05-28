"""Chat read resolvers.

Tenant-scoped: admins see threads for any ambassador in their tenant;
BAs only see their own. Single helper `_scope_threads_for_caller`
applies the gate so individual fields stay tiny.
"""
from typing import List, Optional

import strawberry
from asgiref.sync import sync_to_async
from graphql import GraphQLError

from chats import models
from chats import types
from chats.services import resolve_caller_context
from utils.graphql.permissions import StrictIsAuthenticated


@sync_to_async
def _scope_threads_for_caller(
    *, ambassador_id: Optional[int], tenant_ids: List[int], include_archived: bool
):
    qs = (
        models.ChatThread.objects.select_related("ambassador", "ambassador__user", "job", "tenant")
        .filter(tenant_id__in=tenant_ids)
    )
    if ambassador_id is not None:
        qs = qs.filter(ambassador_id=ambassador_id)
    if not include_archived:
        qs = qs.filter(archived_at__isnull=True)
    return list(qs.order_by("-last_message_at", "-created_at")[:200])


@sync_to_async
def _accessible_tenant_ids_for_user(user_id: int, is_admin: bool) -> List[int]:
    """Tenants the caller can read threads for. Admins → all tenants
    they belong to via TenantedUser. Ambassadors → tenants where they
    have an Ambassador row OR have accepted an event."""
    from tenants.models import TenantedUser

    if is_admin:
        return list(
            TenantedUser.objects.filter(user_id=user_id, is_active=True).values_list(
                "tenant_id", flat=True
            )
        )
    # Ambassador side: derive from accepted events. We need this so
    # the BA can read job-thread context (the JobChatRoom's tenant
    # matches the Event's tenant, which is where the BA's been).
    from ambassadors.models import Ambassador, AmbassadorEvent

    amb = Ambassador.objects.filter(user_id=user_id).first()
    if amb is None:
        return []
    return list(
        AmbassadorEvent.objects.filter(ambassador_id=amb.id)
        .values_list("event__tenant_id", flat=True)
        .distinct()
    )


@strawberry.type
class ChatQueries:
    @strawberry.field(permission_classes=[StrictIsAuthenticated])
    async def chat_threads(
        self,
        info: strawberry.Info,
        include_archived: bool = False,
    ) -> List[types.ChatThread]:
        """All threads the caller can see, newest-first.

        Admins see all threads in their tenants. Ambassadors see only
        their own (their Ambassador row's threads). Empty list if the
        caller has neither role hydrated.
        """
        user, amb, is_ba, is_admin, _ = await resolve_caller_context(info)
        if user is None:
            return []
        tenant_ids = await _accessible_tenant_ids_for_user(user.pk, is_admin)
        if not tenant_ids:
            return []
        ambassador_id = amb.id if (is_ba and amb is not None) else None
        return await _scope_threads_for_caller(
            ambassador_id=ambassador_id,
            tenant_ids=tenant_ids,
            include_archived=include_archived,
        )

    @strawberry.field(permission_classes=[StrictIsAuthenticated])
    async def chat_thread(
        self,
        info: strawberry.Info,
        uuid: strawberry.ID,
    ) -> Optional[types.ChatThread]:
        """Single thread by uuid. Returns None on cross-tenant or
        cross-ambassador access — same posture as recap(uuid) so we
        don't leak existence of other-tenant rows."""
        user, amb, is_ba, is_admin, _ = await resolve_caller_context(info)
        if user is None:
            return None
        tenant_ids = await _accessible_tenant_ids_for_user(user.pk, is_admin)

        @sync_to_async
        def _load():
            qs = models.ChatThread.objects.select_related(
                "ambassador", "ambassador__user", "job", "tenant"
            ).filter(uuid=str(uuid), tenant_id__in=tenant_ids)
            if is_ba and amb is not None:
                qs = qs.filter(ambassador_id=amb.id)
            return qs.first()

        return await _load()

    @strawberry.field(permission_classes=[StrictIsAuthenticated])
    async def chat_unread_count(self, info: strawberry.Info) -> int:
        """Total unread messages across all of the caller's threads.
        Powers the nav badge on both mobile and admin web. Admins see
        unread admin-side counts; BAs see unread BA-side counts.
        """
        user, amb, is_ba, is_admin, _ = await resolve_caller_context(info)
        if user is None:
            return 0
        tenant_ids = await _accessible_tenant_ids_for_user(user.pk, is_admin)
        if not tenant_ids:
            return 0

        @sync_to_async
        def _count():
            base = models.ChatMessage.objects.filter(
                thread__tenant_id__in=tenant_ids,
                thread__archived_at__isnull=True,
            )
            if is_ba and amb is not None:
                base = base.filter(
                    thread__ambassador_id=amb.id,
                    sender_is_ambassador=False,
                    read_by_ambassador_at__isnull=True,
                )
            else:
                base = base.filter(
                    sender_is_ambassador=True,
                    read_by_admin_at__isnull=True,
                )
            return base.count()

        return await _count()
