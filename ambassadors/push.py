"""High-level push delivery for spark-mobile.

The low-level Expo client is in ``utils/expo_push.py``. This module
provides ``send_push_to_user`` — the canonical entry point any caller
elsewhere in spark-api should use to deliver a notification. It:

  1. Pulls all active ``PushDevice`` rows for the user.
  2. Builds one ``ExpoPushMessage`` per device, with Android channel
     defaulted to "default" (matches the channel registered by
     spark-mobile in ``src/lib/push.ts``).
  3. Sends via the Expo relay.
  4. Reads back the tickets and deactivates any token that came back
     ``DeviceNotRegistered`` — the mobile client will re-register on
     next launch and we'll get a fresh token.

Best-effort by design: failures are logged but never raised to the
caller. A failed push must not break the request that triggered it
(e.g. clock-in shouldn't fail because the Expo relay is having a
bad day).

Usage::

    from ambassadors.push import send_push_to_user

    await send_push_to_user(
        user,
        title="Your shift starts in 15 minutes",
        body="Live Nation · Houston Pavilion",
        data={"screen": "shifts", "shiftId": str(shift.uuid)},
    )
"""

from __future__ import annotations

import asyncio
import datetime
import logging
from typing import Any

from asgiref.sync import sync_to_async
from django.contrib.auth import get_user_model
from django.utils import timezone

from utils.expo_push import (
    ExpoPushClient,
    ExpoPushError,
    ExpoPushMessage,
    expo_push_client,
)

from .models import PushDevice

logger = logging.getLogger(__name__)
User = get_user_model()

# Matches Notifications.setNotificationChannelAsync("default", ...) in
# spark-mobile/src/lib/push.ts. If we ever ship a channel-per-category
# (shifts, recap, payroll) the mobile side needs to register them too.
DEFAULT_ANDROID_CHANNEL = "default"


async def send_push_to_user(
    user: "User | int",
    *,
    title: str,
    body: str,
    data: dict[str, Any] | None = None,
    sound: str | None = "default",
    badge: int | None = None,
    priority: str | None = "high",
    client: ExpoPushClient | None = None,
) -> int:
    """Send a push to every active device registered to the user.

    Returns the number of tickets the Expo relay reported as ``ok``.
    Errors are swallowed and logged — see module docstring.

    ``user`` accepts either a User instance or a primary key, so callers
    don't need to fetch the user just to push.
    """
    user_id = user.pk if hasattr(user, "pk") else int(user)
    client = client or expo_push_client

    devices = await sync_to_async(
        lambda: list(
            PushDevice.objects.filter(user_id=user_id, is_active=True).only(
                "id", "token", "platform"
            )
        )
    )()
    if not devices:
        return 0

    messages = [
        ExpoPushMessage(
            to=d.token,
            title=title,
            body=body,
            data=data,
            sound=sound,
            badge=badge,
            channel_id=DEFAULT_ANDROID_CHANNEL if d.platform == "android" else None,
            priority=priority,
        )
        for d in devices
    ]

    try:
        tickets = await client.send(messages)
    except ExpoPushError as exc:
        logger.warning("expo push relay failed for user_id=%s: %s", user_id, exc)
        return 0
    except Exception:
        logger.exception("unexpected expo push failure for user_id=%s", user_id)
        return 0

    now = timezone.now()
    invalid_ids: list[int] = []
    used_ids: list[int] = []
    ok_count = 0

    for device, ticket in zip(devices, tickets):
        if ticket.ok:
            ok_count += 1
            used_ids.append(device.id)
        elif ticket.is_invalid_token:
            invalid_ids.append(device.id)
        else:
            logger.warning(
                "expo push ticket error user_id=%s token=%s code=%s message=%s",
                user_id,
                device.token[:12],
                ticket.error_code,
                ticket.message,
            )

    @sync_to_async
    def mark_devices():
        if invalid_ids:
            PushDevice.objects.filter(id__in=invalid_ids).update(is_active=False)
        if used_ids:
            PushDevice.objects.filter(id__in=used_ids).update(last_used_at=now)

    try:
        await mark_devices()
    except Exception:
        # Don't let bookkeeping failures hide the push outcome.
        logger.exception("failed to update PushDevice rows after send")

    return ok_count


# ---------------------------------------------------------------------------
# RQ-friendly entry points
# ---------------------------------------------------------------------------
#
# RQ workers are sync. The helpers below let any caller — signals,
# mutations, the recap nudge cron — enqueue a push without awaiting
# anything themselves.


def _send_push_to_user_sync(
    user_id: int,
    *,
    title: str,
    body: str,
    data: dict[str, Any] | None = None,
    sound: str | None = "default",
    badge: int | None = None,
    priority: str | None = "high",
) -> int:
    """Sync wrapper RQ workers can call. Runs the async send in a loop."""
    return asyncio.run(
        send_push_to_user(
            user_id,
            title=title,
            body=body,
            data=data,
            sound=sound,
            badge=badge,
            priority=priority,
        )
    )


def enqueue_push(
    user_id: int,
    *,
    title: str,
    body: str,
    data: dict[str, Any] | None = None,
) -> None:
    """Enqueue a push to be sent ASAP via RQ. Best-effort.

    Falls back to running the send inline if the queue (Redis) is
    unreachable — same posture as utils/mailer.send().
    """
    try:
        from utils.queues import Queues

        Queues().default.add(
            _send_push_to_user_sync,
            user_id,
            title=title,
            body=body,
            data=data,
        )
    except Exception as exc:
        logger.warning(
            "push queue unreachable (%s); sending inline to user_id=%s", exc, user_id
        )
        try:
            _send_push_to_user_sync(user_id, title=title, body=body, data=data)
        except Exception:
            logger.exception("inline push fallback failed for user_id=%s", user_id)


def schedule_push_at(
    eta: datetime.datetime,
    user_id: int,
    *,
    title: str,
    body: str,
    data: dict[str, Any] | None = None,
) -> None:
    """Schedule a push to fire at ``eta`` (UTC). Best-effort.

    If ``eta`` is in the past or within the next 5 seconds, we just
    enqueue it immediately. If the scheduler is unreachable, we log
    and drop — there's no inline fallback for a future-dated send.
    """
    now = timezone.now()
    if eta <= now + datetime.timedelta(seconds=5):
        enqueue_push(user_id, title=title, body=body, data=data)
        return

    try:
        import django_rq

        scheduler = django_rq.get_scheduler("default")
        scheduler.enqueue_at(
            eta,
            _send_push_to_user_sync,
            user_id,
            title=title,
            body=body,
            data=data,
        )
    except Exception as exc:
        logger.warning(
            "push scheduler unreachable (%s); dropping eta=%s user_id=%s title=%r",
            exc, eta.isoformat(), user_id, title,
        )


def _send_recap_nudge_if_unfiled(
    user_id: int,
    ambassador_id: int,
    event_id: int,
    *,
    title: str,
    body: str,
    data: dict[str, Any] | None = None,
) -> None:
    """Recap-nudge worker entry point.

    Runs at event end + N hours. Short-circuits if the BA already filed
    a recap for this event — we don't want to nag people who did the
    thing.
    """
    from recaps.models import Recap  # local import to avoid app-loading order issues

    already_filed = Recap.objects.filter(
        event_id=event_id,
        ambassador_id=ambassador_id,
        submited_at__isnull=False,
    ).exists()
    if already_filed:
        logger.info(
            "recap nudge skipped — already filed event_id=%s ambassador_id=%s",
            event_id, ambassador_id,
        )
        return
    _send_push_to_user_sync(user_id, title=title, body=body, data=data)


def schedule_recap_nudge_at(
    eta: datetime.datetime,
    user_id: int,
    ambassador_id: int,
    event_id: int,
    *,
    title: str,
    body: str,
    data: dict[str, Any] | None = None,
) -> None:
    """Like ``schedule_push_at``, but checks the recap state at fire time."""
    now = timezone.now()
    if eta <= now + datetime.timedelta(seconds=5):
        # Past-due: fire the recap-check inline.
        try:
            _send_recap_nudge_if_unfiled(
                user_id, ambassador_id, event_id, title=title, body=body, data=data,
            )
        except Exception:
            logger.exception("inline recap nudge failed event_id=%s", event_id)
        return

    try:
        import django_rq

        scheduler = django_rq.get_scheduler("default")
        scheduler.enqueue_at(
            eta,
            _send_recap_nudge_if_unfiled,
            user_id,
            ambassador_id,
            event_id,
            title=title,
            body=body,
            data=data,
        )
    except Exception as exc:
        logger.warning(
            "recap nudge scheduler unreachable (%s); dropping eta=%s event_id=%s",
            exc, eta.isoformat(), event_id,
        )
