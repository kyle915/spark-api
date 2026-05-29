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
from utils.gcs import public_url, extract_blob_name_from_url


@strawberry_django.type(models.ChatMessageAttachment)
class ChatMessageAttachment:
    """A file attached to a chat message — image (rendered inline) or
    pdf/file (rendered as a tappable chip). The `url` is the NON-signed
    public GCS URL (recap-photo serving path) so it loads on Cloud Run
    without the private-key signing failure."""

    uuid: str
    kind: str  # "image" | "pdf" | "file"
    original_filename: Optional[str]
    content_type: Optional[str]
    byte_size: Optional[int]
    created_at: str

    @strawberry.field
    def id(self) -> strawberry.ID:
        return strawberry.ID(str(self.uuid))

    # Named with a `_url` python method aliased back to `url` so a bare
    # `file` resolver doesn't shadow the Django FileField (same gotcha
    # documented on RecapFile.file_url — a resolver literally named
    # `file` forces a per-row DB hit).
    @strawberry.field
    async def url(self) -> Optional[str]:
        """Public (non-signed) URL for the attachment blob."""
        field_file = self.__dict__.get("file")
        if field_file is None:
            def _reload():
                self.refresh_from_db(fields=["file"])
                return self.__dict__.get("file")
            try:
                field_file = await sync_to_async(
                    _reload, thread_sensitive=True
                )()
            except Exception:
                return None
        if not field_file:
            return None
        try:
            blob = field_file.name
        except Exception:
            blob = str(field_file)
        blob_name = extract_blob_name_from_url(blob)
        return public_url(blob_name)


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
    async def attachments(self) -> List[ChatMessageAttachment]:
        """Files attached to this message (images + PDFs). Empty list
        for plain text messages. Exposed on both the web-admin and
        mobile chat surfaces so either side can render them."""
        @sync_to_async
        def _fetch():
            return list(
                models.ChatMessageAttachment.objects.filter(
                    message_id=self.id
                ).order_by("id")
            )

        return await _fetch()

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


@strawberry.type
class BroadcastChatMessageResult:
    """Outcome of a broadcast fan-out. `recipient_count` is how many BAs
    actually received the message; `thread_uuids` are the (possibly
    pre-existing) 1:1 threads the message landed in."""

    success: bool
    message: str
    recipient_count: int
    thread_uuids: List[str]


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
    async def ambassador_uuid(self) -> str:
        if not getattr(self, "ambassador_id", None):
            return ""

        @sync_to_async
        def _get():
            return str(self.ambassador.uuid)

        return await _get()

    @strawberry.field
    async def ambassador_name(self) -> str:
        if not getattr(self, "ambassador_id", None):
            return ""

        @sync_to_async
        def _get():
            amb = getattr(self, "ambassador", None)
            u = getattr(amb, "user", None) if amb else None
            if u is None:
                return ""
            full = " ".join(
                x for x in [getattr(u, "first_name", "") or "", getattr(u, "last_name", "") or ""] if x
            ).strip()
            return full or (getattr(u, "email", "") or "")

        return await _get()

    @strawberry.field
    async def job_uuid(self) -> Optional[str]:
        if not getattr(self, "job_id", None):
            return None

        @sync_to_async
        def _get():
            return str(self.job.uuid)

        return await _get()

    @strawberry.field
    async def job_name(self) -> Optional[str]:
        if not getattr(self, "job_id", None):
            return None

        @sync_to_async
        def _get():
            j = getattr(self, "job", None)
            return getattr(j, "name", None) if j else None

        return await _get()

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
