"""Announcement GraphQL mutations — admin-only writes (Spark/web).

createAnnouncement saves the row, stamps published_at=now, then fans
out a push to every active BA in the tenant (best-effort). Tenant
resolution matches createAcademyModule: accept an explicit tenant_id,
else fall back to the caller's current tenant.
"""
from __future__ import annotations

import logging

import strawberry
from asgiref.sync import sync_to_async
from django.utils import timezone

from utils.graphql.permissions import StrictIsAuthenticated
from utils.graphql.mixins import SparkGraphQLMixin

from . import models, types
from .announcement_scope import AnnouncementScope
from .inputs import CreateAnnouncementInput, DeleteAnnouncementInput

logger = logging.getLogger(__name__)


class _AnnouncementService(SparkGraphQLMixin):
    """Only the auth/user resolution from the mixin is used here."""


@strawberry.type
class AnnouncementMutations:
    @strawberry.mutation(permission_classes=[StrictIsAuthenticated])
    async def create_announcement(
        self, info: strawberry.Info, input: CreateAnnouncementInput
    ) -> types.AnnouncementResponse:
        svc = _AnnouncementService()
        user = await svc.get_user(info)

        title = (input.title or "").strip()
        if not title:
            return types.AnnouncementResponse(
                success=False, message="Title is required."
            )

        # Tenant-scoped: a client always posts to their OWN tenant (any
        # supplied tenant_id is ignored), so a client can never broadcast a
        # push to another brand's BAs; an admin may target the requested
        # tenant. A client with no tenant, or an admin who passed no usable
        # tenant id, gets a safe success=False rather than a cross-tenant post.
        try:
            tenant_id = await AnnouncementScope().resolve_target_tenant_id(
                info, input.tenant_id
            )
        except Exception:  # noqa: BLE001
            tenant_id = None
        if not tenant_id:
            return types.AnnouncementResponse(
                success=False,
                message="No tenant in scope to post the announcement under.",
            )

        audience = (input.audience or models.Announcement.AUDIENCE_ALL_BAS).strip()
        if audience not in dict(models.Announcement.AUDIENCE_CHOICES):
            audience = models.Announcement.AUDIENCE_ALL_BAS

        try:
            announcement = await sync_to_async(
                models.Announcement.objects.create
            )(
                tenant_id=tenant_id,
                title=title[:200],
                body=input.body or "",
                audience=audience,
                published_at=timezone.now(),
                created_by=user,
            )
        except Exception as exc:  # noqa: BLE001
            return types.AnnouncementResponse(
                success=False,
                message=f"Failed to create announcement: {exc}",
            )

        # Fan out the push — best-effort, never blocks the write.
        try:
            from announcements.push import fan_out_announcement

            await fan_out_announcement(
                tenant_id=tenant_id,
                announcement_uuid=str(announcement.uuid),
                title=announcement.title,
                body=announcement.body,
            )
        except Exception as e:  # pragma: no cover
            logger.warning(
                "announcement fan-out failed id=%s: %s", announcement.id, e
            )

        return types.AnnouncementResponse(
            success=True,
            message="Announcement posted.",
            announcement=announcement,
        )

    @strawberry.mutation(permission_classes=[StrictIsAuthenticated])
    async def delete_announcement(
        self, info: strawberry.Info, input: DeleteAnnouncementInput
    ) -> types.AnnouncementResponse:
        svc = _AnnouncementService()
        await svc.get_user(info)

        # Tenant gate: a client may only delete their OWN tenant's
        # announcements; an out-of-scope row is surfaced as "not found" (no
        # cross-tenant existence leak / deletion). Admins may delete any
        # tenant's announcement.
        try:
            allowed = await AnnouncementScope().accessible_tenant_ids(info)
        except Exception:  # noqa: BLE001
            return types.AnnouncementResponse(
                success=False, message="Announcement not found."
            )

        try:
            announcement = await sync_to_async(
                models.Announcement.objects.get
            )(uuid=str(input.uuid))
        except models.Announcement.DoesNotExist:
            return types.AnnouncementResponse(
                success=False, message="Announcement not found."
            )

        if allowed is not None and announcement.tenant_id not in allowed:
            return types.AnnouncementResponse(
                success=False, message="Announcement not found."
            )

        await sync_to_async(announcement.delete)()
        return types.AnnouncementResponse(
            success=True, message="Announcement deleted."
        )
