"""Fan-out push for a new announcement.

Called from createAnnouncement after the row commits. Best-effort: a
delivery failure logs a warning and is swallowed so it never rolls back
the announcement (the BA still sees it in the feed on next open).

Recipient resolution: every active BA in the announcement's tenant.
Ambassador has no direct tenant FK, so we derive membership through
AmbassadorEvent (same idiom as chats/push.py + digest/services.py):
distinct ambassador__user_id where the AmbassadorEvent.tenant matches,
filtered to active Ambassador + active User rows.
"""
from __future__ import annotations

import logging

from asgiref.sync import sync_to_async

logger = logging.getLogger(__name__)


def _truncate(body: str, limit: int = 140) -> str:
    body = (body or "").replace("\n", " ").strip()
    return body if len(body) <= limit else body[: limit - 1] + "…"


@sync_to_async
def _active_ba_user_ids_for_tenant(tenant_id: int) -> list[int]:
    """Distinct active-BA User ids for the tenant, via AmbassadorEvent."""
    from ambassadors.models import AmbassadorEvent

    return list(
        AmbassadorEvent.objects.filter(
            tenant_id=tenant_id,
            ambassador__is_active=True,
            ambassador__user__is_active=True,
        )
        .values_list("ambassador__user_id", flat=True)
        .distinct()
    )


async def fan_out_announcement(
    *,
    tenant_id: int,
    announcement_uuid: str,
    title: str,
    body: str,
) -> int:
    """Enqueue a push to every active BA in the tenant. Returns the
    number of users we attempted to notify. Each enqueue is itself
    best-effort (RQ with inline fallback); one bad user never blocks
    the rest."""
    try:
        from ambassadors.push import enqueue_push
    except ImportError:  # pragma: no cover
        logger.warning("announcement push: enqueue_push not importable")
        return 0

    user_ids = await _active_ba_user_ids_for_tenant(tenant_id)
    if not user_ids:
        return 0

    push_title = title.strip()[:80] or "New announcement"
    push_body = _truncate(body) or "Tap to read the latest update."
    data = {
        "screen": "announcements",
        "kind": "announcement",
        "announcementUuid": announcement_uuid,
    }

    sent = 0
    for uid in user_ids:
        try:
            enqueue_push(uid, title=push_title, body=push_body, data=data)
            sent += 1
        except Exception as e:  # pragma: no cover — never block on one BA
            logger.warning(
                "announcement push enqueue failed user=%s: %s", uid, e
            )
    return sent
