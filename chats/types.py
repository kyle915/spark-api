"""GraphQL types for the chat surface.

ChatThread and ChatMessage are exposed read-only here; writes go
through chats.mutations (sendChatMessage, markChatThreadRead) so the
service layer can normalize sender_is_ambassador + bump
thread.last_message_at atomically.

Unread counts are computed per-side and exposed on ChatThread so the
client can render a badge without an extra query per row.
"""
from typing import List, Optional
import strawberry
import strawberry_django
from asgiref.sync import sync_to_async

from chats import models


@strawberry_django.type(models.ChatMessage)
class ChatMessage:
    uuid: str
    body: str
    sender_is_ambassador: bool
    created_at: str
    # Two-sided read tracking. Both nullable; non-null is the ISO
    # timestamp the corresponding side opened the thread.
    read_by_admin_at: Optional[str]
    read_by_ambassador_at: Optional[str]

    @strawberry.field
    def id(self) -> strawberry.ID:
        return strawberry.ID(str(self.uuid))

    @strawberry.field
    def sender_name(self) -> str:
        """Display name for the sender — for admin-side rendering of
        the BA's name on incoming messages and admin's name on
        outgoing. Falls back to email if no first/last on the User."""
        u = getattr(self, "sender", None)
        if u is None:
            return ""
        full = " ".join(
            x for x in [getattr(u, "first_name", "") or "", getattr(u, "last_name", "") or ""] if x
        ).strip()
        return full or (getattr(u, "email", "") or "")


@strawberry_django.type(models.ChatThread)
class ChatThread:
    uuid: str
    kind: str  # "general" or "job"
    last_message_at: Optional[str]
    last_message_preview: Optional[str]
    last_message_sender_is_ambassador: bool
    archived_at: Optional[str]
    created_at: str

    @strawberry.field
    def id(self) -> strawberry.ID:
        return strawberry.ID(str(self.uuid))

    @strawberry.field
    def ambassador_uuid(self) -> str:
        return str(self.ambassador.uuid) if self.ambassador_id else ""

    @strawberry.field
    def ambassador_name(self) -> str:
        amb = getattr(self, "ambassador", None)
        u = getattr(amb, "user", None) if amb else None
        if u is None:
            return ""
        full = " ".join(
            x for x in [getattr(u, "first_name", "") or "", getattr(u, "last_name", "") or ""] if x
        ).strip()
        return full or (getattr(u, "email", "") or "")

    @strawberry.field
    def job_uuid(self) -> Optional[str]:
        return str(self.job.uuid) if getattr(self, "job_id", None) else None

    @strawberry.field
    def job_name(self) -> Optional[str]:
        j = getattr(self, "job", None)
        return getattr(j, "name", None) if j else None

    @strawberry.field
    async def unread_for_admin(self) -> int:
        """Messages sent by the BA that no admin has read yet. Cheap
        — one count(*) keyed on the (thread, sender_is_ambassador)
        index. Returns 0 for archived threads to keep nav badges
        clean."""
        if getattr(self, "archived_at", None):
            return 0

        @sync_to_async
        def _count():
            return models.ChatMessage.objects.filter(
                thread_id=self.id,
                sender_is_ambassador=True,
                read_by_admin_at__isnull=True,
            ).count()

        return await _count()

    @strawberry.field
    async def unread_for_ambassador(self) -> int:
        """Messages sent by an admin that the BA hasn't read yet."""
        if getattr(self, "archived_at", None):
            return 0

        @sync_to_async
        def _count():
            return models.ChatMessage.objects.filter(
                thread_id=self.id,
                sender_is_ambassador=False,
                read_by_ambassador_at__isnull=True,
            ).count()

        return await _count()

    @strawberry.field
    async def messages(
        self, first: int = 50, before_uuid: Optional[str] = None
    ) -> List[ChatMessage]:
        """Paginated, newest-first message list. before_uuid is the
        cursor for older pages — pass the oldest currently-loaded
        message's uuid and you get the previous page. first capped at
        100 to keep payloads bounded."""
        first = min(max(first, 1), 100)

        @sync_to_async
        def _fetch():
            qs = (
                models.ChatMessage.objects.select_related("sender")
                .filter(thread_id=self.id)
                .order_by("-created_at")
            )
            if before_uuid:
                anchor = qs.filter(uuid=before_uuid).first()
                if anchor is not None:
                    qs = qs.filter(created_at__lt=anchor.created_at)
            return list(qs[:first])

        return await _fetch()
