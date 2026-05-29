"""Chat push notifications.

Fires after sendChatMessage commits. Best-effort — a delivery failure
logs a warning and returns; the message itself is already persisted
so the recipient will see it on next thread open or via in-app polling.

Recipient resolution:
  - BA sent the message → notify every tenant-admin user that's a
    member of this thread's tenant (TenantedUser with is_active=True).
  - Admin sent the message → notify the single Ambassador's User.

Routing data payload includes `type: "chat"` + `threadUuid` so the
mobile app can deep-link the tap into the right conversation.
"""
from __future__ import annotations

import logging
from typing import Iterable

from asgiref.sync import sync_to_async

from chats import models

logger = logging.getLogger(__name__)


def _truncate(body: str, limit: int = 120) -> str:
    body = (body or "").replace("\n", " ").strip()
    return body if len(body) <= limit else body[: limit - 1] + "…"


@sync_to_async
def _resolve_recipients(thread_id: int, sender_is_ambassador: bool) -> list[int]:
    """Return the list of User IDs to push to.

    When the BA sent the message → all active admin users in the
    thread's tenant. When an admin sent → the BA's User.
    """
    from tenants.models import TenantedUser

    thread = (
        models.ChatThread.objects.select_related("ambassador__user")
        .filter(id=thread_id)
        .first()
    )
    if thread is None:
        return []

    if sender_is_ambassador:
        # Notify admin-side: every active TenantedUser on this tenant.
        # Excludes the BA themselves if (somehow) listed.
        ba_user_id = (
            thread.ambassador.user_id
            if thread.ambassador and thread.ambassador.user_id
            else None
        )
        return list(
            TenantedUser.objects.filter(tenant_id=thread.tenant_id, is_active=True)
            .exclude(user_id=ba_user_id)
            .values_list("user_id", flat=True)
            .distinct()
        )

    # Admin sent → BA's User.
    if thread.ambassador and thread.ambassador.user_id:
        return [thread.ambassador.user_id]
    return []


async def notify_chat_recipient(
    *,
    thread_id: int,
    msg_uuid: str,
    body: str,
    sender_is_ambassador: bool,
) -> int:
    """Fan-out push delivery for a new chat message.

    Returns count of users we attempted to push to. Logs and swallows
    individual delivery failures so a dead PushDevice for one user
    doesn't block notifications for everyone else.
    """
    # Lazy import — push module isn't required to import chats.
    try:
        from ambassadors.push import send_push_to_user
    except ImportError:  # pragma: no cover
        logger.warning("chat push: send_push_to_user not importable")
        return 0

    recipient_ids = await _resolve_recipients(thread_id, sender_is_ambassador)
    if not recipient_ids:
        return 0

    # Resolve thread uuid once so the data payload can deep-link the
    # tap into the right conversation without re-querying per recipient.
    @sync_to_async
    def _thread_uuid():
        t = models.ChatThread.objects.only("uuid").filter(id=thread_id).first()
        return str(t.uuid) if t else ""

    thread_uuid = await _thread_uuid()

    title = "New message from your BA" if sender_is_ambassador else "New message"
    body_short = _truncate(body)

    sent = 0
    for uid in recipient_ids:
        try:
            await send_push_to_user(
                uid,
                title=title,
                body=body_short,
                data={
                    "type": "chat",
                    # `kind`/`screen` are what the mobile push-tap router
                    # reads (TAB_FOR_KIND / TAB_FOR_SCREEN). Without them a
                    # tapped chat notification routed nowhere. "chat" maps to
                    # the Chat tab; ChatListScreen then opens threadUuid.
                    "kind": "chat",
                    "screen": "chat",
                    "threadUuid": thread_uuid,
                    "messageUuid": msg_uuid,
                },
            )
            sent += 1
        except Exception as e:  # pragma: no cover
            logger.warning("chat push delivery failed user=%s: %s", uid, e)
    return sent
