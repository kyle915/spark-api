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
from typing import Optional

from asgiref.sync import sync_to_async
from django.db import transaction
from django.utils import timezone

from chats import models
from utils.graphql.permissions import (
    resolve_request_user_access,
    IGNITE_EMAIL_DOMAIN,
)

logger = logging.getLogger(__name__)


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


@sync_to_async
def insert_message(
    *,
    thread_id: int,
    sender_id: int,
    sender_is_ambassador: bool,
    body: str,
) -> models.ChatMessage:
    """Create a message + bump the thread's recency / preview columns
    atomically. The sender's own read-side is set to now (you've read
    your own message); the other side stays null until the recipient
    opens the thread.
    """
    now = timezone.now()
    with transaction.atomic():
        msg = models.ChatMessage.objects.create(
            thread_id=thread_id,
            sender_id=sender_id,
            sender_is_ambassador=sender_is_ambassador,
            body=body,
            read_by_admin_at=None if sender_is_ambassador else now,
            read_by_ambassador_at=now if sender_is_ambassador else None,
        )
        # Cache preview on the thread so the inbox list doesn't have
        # to join into ChatMessage for every row.
        preview = (body or "").strip().replace("\n", " ")
        if len(preview) > 200:
            preview = preview[:197] + "…"
        models.ChatThread.objects.filter(id=thread_id).update(
            last_message_at=now,
            last_message_preview=preview,
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
