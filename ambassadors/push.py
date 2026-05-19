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
