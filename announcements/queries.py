"""Announcement GraphQL queries.

Admin side (Spark/web): announcementsAdmin returns the tenant's
announcements (newest-first) for the manage UI.

Mobile side (BA): myAnnouncements returns published announcements for
every tenant the caller belongs to (derived via AmbassadorEvent, the
same membership idiom chats uses). myAnnouncementsUnreadCount powers
the unread badge — it counts announcements published after a `since`
ISO timestamp the client passes (its locally-stored last-seen mark).
"""
from __future__ import annotations

from typing import List, Optional

import strawberry
from asgiref.sync import sync_to_async
from django.utils.dateparse import parse_datetime

from utils.graphql.permissions import StrictIsAuthenticated
from utils.graphql.mixins import resolve_id_to_int

from . import models, types
from .inputs import AnnouncementFiltersInput


@sync_to_async
def _tenant_ids_for_ba(user_id: int) -> List[int]:
    """Tenants a BA belongs to, via accepted/any AmbassadorEvent rows."""
    from ambassadors.models import Ambassador, AmbassadorEvent

    amb = Ambassador.objects.filter(user_id=user_id).only("id").first()
    if amb is None:
        return []
    return list(
        AmbassadorEvent.objects.filter(ambassador_id=amb.id)
        .values_list("tenant_id", flat=True)
        .distinct()
    )


@strawberry.type
class AnnouncementQueries:
    """Admin-side: all announcements for a tenant (manage UI)."""

    @strawberry.field(permission_classes=[StrictIsAuthenticated])
    async def announcements_admin(
        self,
        info: strawberry.Info,
        filters: AnnouncementFiltersInput | None = None,
    ) -> List[types.Announcement]:
        tenant_id: int | None = (
            resolve_id_to_int(filters.tenant_id)
            if filters and filters.tenant_id not in (None, "")
            else None
        )

        @sync_to_async
        def _load():
            qs = models.Announcement.objects.select_related("created_by").all()
            if tenant_id:
                qs = qs.filter(tenant_id=tenant_id)
            return list(qs.order_by("-published_at", "-created_at")[:200])

        return await _load()


@strawberry.type
class AnnouncementMobileQueries:
    """BA-facing: published announcements across the caller's tenants."""

    @strawberry.field(permission_classes=[StrictIsAuthenticated])
    async def my_announcements(
        self, info: strawberry.Info
    ) -> List[types.Announcement]:
        user = getattr(getattr(info.context, "request", None), "user", None)
        if user is None or not getattr(user, "is_authenticated", False):
            return []
        tenant_ids = await _tenant_ids_for_ba(user.pk)
        if not tenant_ids:
            return []

        @sync_to_async
        def _load():
            return list(
                models.Announcement.objects.select_related("created_by")
                .filter(
                    tenant_id__in=tenant_ids,
                    published_at__isnull=False,
                )
                .order_by("-published_at", "-created_at")[:100]
            )

        return await _load()

    @strawberry.field(permission_classes=[StrictIsAuthenticated])
    async def my_announcements_unread_count(
        self, info: strawberry.Info, since: Optional[str] = None
    ) -> int:
        """Count of published announcements newer than `since` (ISO
        datetime). When `since` is None/empty, returns the total
        published count (first-run: everything is 'unread')."""
        user = getattr(getattr(info.context, "request", None), "user", None)
        if user is None or not getattr(user, "is_authenticated", False):
            return 0
        tenant_ids = await _tenant_ids_for_ba(user.pk)
        if not tenant_ids:
            return 0

        since_dt = parse_datetime(since) if since else None

        @sync_to_async
        def _count():
            qs = models.Announcement.objects.filter(
                tenant_id__in=tenant_ids,
                published_at__isnull=False,
            )
            if since_dt is not None:
                qs = qs.filter(published_at__gt=since_dt)
            return qs.count()

        return await _count()
