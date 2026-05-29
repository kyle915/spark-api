"""Service layer for chat reads/writes.

Centralises the tenant + role gating so resolvers stay thin. Two key
helpers:

  - `resolve_caller_context(info)` returns (user, ambassador_row,
    is_ambassador_caller, tenant_ids) — derived authoritatively from
    the DB user row, not the JWT FK that doesn't always hydrate.
  - `get_or_create_thread(...)` matches the partial UniqueConstraints
    on ChatThread so the resolver doesn't race two threads into
    existence under concurrent first-message sends.

Sending and read-marking are wrapped in transaction.atomic() so the
last_message_at bump on the thread and the message insert can't
diverge.
"""
from __future__ import annotations

import logging
import os
from typing import Iterable, Optional

from asgiref.sync import sync_to_async
from django.db import transaction
from django.utils import timezone

from chats import models
from utils.gcs import extract_blob_name_from_url
from utils.graphql.permissions import (
    resolve_request_user_access,
    IGNITE_EMAIL_DOMAIN,
)

logger = logging.getLogger(__name__)


_IMAGE_EXTS = (".jpg", ".jpeg", ".png", ".gif", ".webp", ".heic", ".heif", ".bmp")


def infer_attachment_kind(
    *, kind: Optional[str], content_type: Optional[str], filename: Optional[str], blob: Optional[str]
) -> str:
    """Resolve the coarse render kind for an attachment.

    Honors an explicit `kind` if it's a known choice, else infers from
    content_type then the filename / blob extension. Defaults to "file".
    """
    valid = {c[0] for c in models.ChatMessageAttachment.KIND_CHOICES}
    if kind and kind.lower() in valid:
        return kind.lower()

    ct = (content_type or "").lower()
    if ct.startswith("image/"):
        return models.ChatMessageAttachment.KIND_IMAGE
    if ct == "application/pdf":
        return models.ChatMessageAttachment.KIND_PDF

    name = (filename or blob or "").lower()
    _, ext = os.path.splitext(name)
    if ext in _IMAGE_EXTS:
        return models.ChatMessageAttachment.KIND_IMAGE
    if ext == ".pdf":
        return models.ChatMessageAttachment.KIND_PDF
    return models.ChatMessageAttachment.KIND_FILE


def _normalize_attachment_inputs(attachment_inputs) -> list[dict]:
    """Turn raw GraphQL attachment inputs into clean kwargs dicts for
    ChatMessageAttachment rows. Strips signed-URL noise off the blob,
    infers kind, and drops entries with no resolvable blob path.
    Pure (no DB / async) so it can run inside the write transaction."""
    rows: list[dict] = []
    for a in attachment_inputs or []:
        raw = getattr(a, "file", None)
        blob = extract_blob_name_from_url(raw) if raw else None
        if not blob:
            continue
        rows.append(
            {
                "file": blob,
                "kind": infer_attachment_kind(
                    kind=getattr(a, "kind", None),
                    content_type=getattr(a, "content_type", None),
                    filename=getattr(a, "original_filename", None),
                    blob=blob,
                ),
                "original_filename": (getattr(a, "original_filename", None) or None),
                "content_type": (getattr(a, "content_type", None) or None),
                "byte_size": getattr(a, "byte_size", None),
            }
        )
    return rows


def _is_admin_role(role_slug: str | None, is_staff: bool, is_super: bool, email: str) -> bool:
    """Anything that grants admin access for chats — matches the
    `_is_admin_access` shape used elsewhere in the codebase."""
    if is_staff or is_super:
        return True
    if (email or "").lower().endswith(IGNITE_EMAIL_DOMAIN):
        return True
    return (role_slug or "").lower() == "spark-admin"


async def resolve_caller_context(info):
    """Return (user, ambassador_row_or_None, is_ambassador, is_admin, role_email).

    `is_ambassador` is True for users with role_slug == "ambassador"
    and a hydrated Ambassador row. `is_admin` is True for any of the
    admin escalation paths. The two are mutually exclusive in practice
    but the resolver can mix them (e.g. an admin who's also an
    Ambassador for testing) — admin wins.
    """
    request = getattr(info.context, "request", None)
    user = getattr(request, "user", None) if request else None
    if user is None or not getattr(user, "is_authenticated", False):
        return None, None, False, False, None
    role_slug, is_staff, is_super, email = await resolve_request_user_access(user)
    is_admin = _is_admin_role(role_slug, is_staff, is_super, email)
    is_ambassador = (role_slug or "").lower() == "ambassador" and not is_admin

    # Load the ambassador row if this user is on the BA side. Best-effort
    # — if the FK isn't there we treat them as admin-only.
    @sync_to_async
    def _amb():
        try:
            from ambassadors.models import Ambassador

            return Ambassador.objects.filter(user_id=user.pk).first()
        except Exception:
            return None

    ambassador = await _amb() if is_ambassador else None
    return user, ambassador, is_ambassador, is_admin, email


@sync_to_async
def get_or_create_thread(
    *,
    tenant_id: int,
    ambassador_id: int,
    kind: str,
    job_id: Optional[int],
    created_by_id: int,
) -> models.ChatThread:
    """Get or create a thread for (tenant, ambassador, kind, job).

    Wrapped in a transaction so two concurrent first-sends can't race
    past the partial uniques and create duplicates.
    """
    with transaction.atomic():
        qs = models.ChatThread.objects.filter(
            tenant_id=tenant_id,
            ambassador_id=ambassador_id,
            kind=kind,
        )
        if kind == models.ChatThread.KIND_JOB:
            qs = qs.filter(job_id=job_id)
        else:
            qs = qs.filter(job__isnull=True)

        thread = qs.first()
        if thread:
            return thread

        thread = models.ChatThread.objects.create(
            tenant_id=tenant_id,
            ambassador_id=ambassador_id,
            kind=kind,
            job_id=job_id if kind == models.ChatThread.KIND_JOB else None,
            created_by_id=created_by_id,
        )
        return thread


def _preview_for(body: str, attachment_rows: list[dict]) -> str:
    """Inbox preview string. Falls back to an attachment label when the
    body is empty so attachment-only messages still preview sanely."""
    preview = (body or "").strip().replace("\n", " ")
    if not preview and attachment_rows:
        n = len(attachment_rows)
        first_kind = attachment_rows[0]["kind"]
        if n == 1:
            label = {
                models.ChatMessageAttachment.KIND_IMAGE: "📷 Photo",
                models.ChatMessageAttachment.KIND_PDF: "📎 PDF",
            }.get(first_kind, "📎 Attachment")
            preview = label
        else:
            preview = f"📎 {n} attachments"
    if len(preview) > 200:
        preview = preview[:197] + "…"
    return preview


@sync_to_async
def insert_message(
    *,
    thread_id: int,
    sender_id: int,
    sender_is_ambassador: bool,
    body: str,
    attachment_rows: Optional[list[dict]] = None,
) -> models.ChatMessage:
    """Create a message (+ any attachments) and bump the thread's
    recency / preview columns atomically. The sender's own read-side is
    set to now (you've read your own message); the other side stays null
    until the recipient opens the thread.

    `attachment_rows` is the cleaned output of
    `_normalize_attachment_inputs` — pure dicts, no GraphQL objects.
    """
    attachment_rows = attachment_rows or []
    now = timezone.now()
    with transaction.atomic():
        msg = models.ChatMessage.objects.create(
            thread_id=thread_id,
            sender_id=sender_id,
            sender_is_ambassador=sender_is_ambassador,
            body=body or "",
            read_by_admin_at=None if sender_is_ambassador else now,
            read_by_ambassador_at=now if sender_is_ambassador else None,
        )
        if attachment_rows:
            models.ChatMessageAttachment.objects.bulk_create(
                [
                    models.ChatMessageAttachment(message_id=msg.id, **row)
                    for row in attachment_rows
                ]
            )
        # Cache preview on the thread so the inbox list doesn't have
        # to join into ChatMessage for every row.
        models.ChatThread.objects.filter(id=thread_id).update(
            last_message_at=now,
            last_message_preview=_preview_for(body, attachment_rows),
            last_message_sender_is_ambassador=sender_is_ambassador,
        )
        return msg


@sync_to_async
def mark_thread_read(
    *,
    thread_id: int,
    by_ambassador: bool,
) -> int:
    """Sweep all unread messages on the side the caller represents
    forward to now. Returns count of messages updated. Idempotent."""
    now = timezone.now()
    if by_ambassador:
        # BA opened the thread — mark admin-sent messages as read.
        return models.ChatMessage.objects.filter(
            thread_id=thread_id,
            sender_is_ambassador=False,
            read_by_ambassador_at__isnull=True,
        ).update(read_by_ambassador_at=now)
    else:
        # Admin opened — mark BA-sent messages as read.
        return models.ChatMessage.objects.filter(
            thread_id=thread_id,
            sender_is_ambassador=True,
            read_by_admin_at__isnull=True,
        ).update(read_by_admin_at=now)


@sync_to_async
def set_thread_archived(*, thread_id: int, archived: bool) -> None:
    """Soft-archive (or restore) a thread by stamping / clearing
    `archived_at`. Idempotent — archiving an already-archived thread is a
    no-op on state. Messages are never touched, so the thread is fully
    recoverable by passing archived=False."""
    models.ChatThread.objects.filter(id=thread_id).update(
        archived_at=timezone.now() if archived else None
    )


@sync_to_async
def resolve_broadcast_target_ambassador_ids(
    *,
    tenant_id: int,
    ambassador_uuids: Iterable[str] | None,
    group_uuids: Iterable[str] | None,
) -> list[int]:
    """Resolve the de-duplicated set of Ambassador IDs a broadcast
    targets, STRICTLY scoped to `tenant_id`.

    The set is the UNION of:
      - explicit BAs (by uuid) that have event history in this tenant
        (so we never DM a BA who has never worked for this brand), and
      - members of each AmbassadorGroup (by uuid) that belongs to this
        tenant.

    Cross-tenant BAs and cross-tenant groups are silently dropped — a
    caller can't reach another brand's ambassadors no matter what uuids
    they pass. Returns a sorted list of Ambassador PKs for determinism.
    """
    from ambassadors.models import (
        Ambassador,
        AmbassadorEvent,
        AmbassadorGroup,
        UserGroup,
    )

    target_ids: set[int] = set()

    # --- Explicit BAs, gated to this tenant by event history. ---
    uuids = [str(u) for u in (ambassador_uuids or []) if u]
    if uuids:
        amb_rows = list(
            Ambassador.objects.filter(uuid__in=uuids).values_list("id", "uuid")
        )
        candidate_ids = [r[0] for r in amb_rows]
        if candidate_ids:
            in_tenant = set(
                AmbassadorEvent.objects.filter(
                    ambassador_id__in=candidate_ids,
                    event__tenant_id=tenant_id,
                )
                .values_list("ambassador_id", flat=True)
                .distinct()
            )
            target_ids |= in_tenant

    # --- Group members, gated to groups owned by this tenant. ---
    g_uuids = [str(u) for u in (group_uuids or []) if u]
    if g_uuids:
        group_ids = list(
            AmbassadorGroup.objects.filter(
                uuid__in=g_uuids, tenant_id=tenant_id
            ).values_list("id", flat=True)
        )
        if group_ids:
            member_amb_ids = set(
                UserGroup.objects.filter(
                    group_id__in=group_ids,
                    ambassador_id__isnull=False,
                )
                .values_list("ambassador_id", flat=True)
                .distinct()
            )
            target_ids |= member_amb_ids

    return sorted(target_ids)


@sync_to_async
def list_recipient_ambassadors_for_tenant(
    *, tenant_id: int, q: Optional[str] = None, limit: int = 500
) -> list[dict]:
    """The BAs an admin can message in `tenant_id`, optionally filtered
    by a name/email search term.

    Scoped by the SAME mechanism broadcastChatMessage uses to decide who
    a message can reach — Ambassador event history in this tenant
    (`AmbassadorEvent.event__tenant_id`) — NOT TenantedUser membership.
    This is the fix for "No ambassadors found": the composer previously
    used the generic `ambassadors` query, which scopes by TenantedUser
    (the admin/client-user join), so BAs — who are linked to a brand
    through their worked events, not a TenantedUser row — never showed
    up. Mirrors recapEventOptions: strictly tenant-scoped, returns
    nothing without a tenant in scope (the resolver enforces that).

    Returns lightweight dicts (uuid, name, email) so the picker doesn't
    drag the full Ambassador type's own FK resolvers through the list.
    """
    from ambassadors.models import Ambassador, AmbassadorEvent

    amb_ids = list(
        AmbassadorEvent.objects.filter(event__tenant_id=tenant_id)
        .values_list("ambassador_id", flat=True)
        .distinct()
    )
    if not amb_ids:
        return []

    qs = Ambassador.objects.select_related("user").filter(
        id__in=amb_ids, is_active=True
    )
    term = (q or "").strip()
    if term:
        from django.db.models import Q

        qs = qs.filter(
            Q(user__first_name__icontains=term)
            | Q(user__last_name__icontains=term)
            | Q(user__email__icontains=term)
        )

    rows: list[dict] = []
    for amb in qs.order_by("user__first_name", "user__last_name", "id")[:limit]:
        u = amb.user
        first = (getattr(u, "first_name", "") or "") if u else ""
        last = (getattr(u, "last_name", "") or "") if u else ""
        email = (getattr(u, "email", "") or "") if u else ""
        name = " ".join(x for x in [first, last] if x).strip() or email
        rows.append(
            {
                "uuid": str(amb.uuid),
                "name": name,
                "email": email,
            }
        )
    return rows


@sync_to_async
def thread_uuids_for_ids(thread_ids: list[int]) -> list[str]:
    """Map a list of thread PKs to their uuids (preserving input order),
    for building the broadcast result payload."""
    if not thread_ids:
        return []
    by_id = dict(
        models.ChatThread.objects.filter(id__in=thread_ids).values_list("id", "uuid")
    )
    return [str(by_id[i]) for i in thread_ids if i in by_id]


@sync_to_async
def fan_out_broadcast(
    *,
    tenant_id: int,
    ambassador_ids: list[int],
    sender_id: int,
    body: str,
    attachment_rows: list[dict],
) -> list[tuple[int, int]]:
    """Create one ChatMessage (with attachments) in each targeted BA's
    general 1:1 thread, get-or-creating the thread first.

    Returns a list of (ambassador_id, thread_id) so the caller can fire
    pushes + build the result. Each BA's thread + message is committed
    in its own transaction so one bad row can't roll back the whole
    blast. No duplicate threads — get-or-create matches the partial
    unique on (tenant, ambassador, kind=general).
    """
    now = timezone.now()
    results: list[tuple[int, int]] = []
    for amb_id in ambassador_ids:
        try:
            with transaction.atomic():
                qs = models.ChatThread.objects.filter(
                    tenant_id=tenant_id,
                    ambassador_id=amb_id,
                    kind=models.ChatThread.KIND_GENERAL,
                    job__isnull=True,
                )
                thread = qs.first()
                if thread is None:
                    thread = models.ChatThread.objects.create(
                        tenant_id=tenant_id,
                        ambassador_id=amb_id,
                        kind=models.ChatThread.KIND_GENERAL,
                        job_id=None,
                        created_by_id=sender_id,
                    )
                msg = models.ChatMessage.objects.create(
                    thread_id=thread.id,
                    sender_id=sender_id,
                    sender_is_ambassador=False,  # broadcast is admin → BA
                    body=body or "",
                    read_by_admin_at=now,  # admin "read" their own send
                    read_by_ambassador_at=None,
                )
                if attachment_rows:
                    models.ChatMessageAttachment.objects.bulk_create(
                        [
                            models.ChatMessageAttachment(message_id=msg.id, **row)
                            for row in attachment_rows
                        ]
                    )
                models.ChatThread.objects.filter(id=thread.id).update(
                    last_message_at=now,
                    last_message_preview=_preview_for(body, attachment_rows),
                    last_message_sender_is_ambassador=False,
                )
            results.append((amb_id, thread.id))
        except Exception as e:  # pragma: no cover — never let one BA abort the blast
            logger.warning(
                "broadcast fan-out failed for ambassador %s: %s", amb_id, e
            )
    return results
