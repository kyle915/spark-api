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


@sync_to_async
def _record_push_notification(
    user_id: int,
    title: str,
    body: str,
    data: "dict[str, Any] | None",
) -> None:
    """Best-effort: persist an in-app record of this push for the inbox.

    Logged regardless of device reachability, so the BA's Notifications inbox
    reflects everything we sent (push delivery itself is fire-and-forget and
    keeps no history). Never raises into the caller.
    """
    from .models import PushNotification

    kind = ""
    if isinstance(data, dict):
        kind = str(data.get("kind") or data.get("screen") or "")[:64]
    PushNotification.objects.create(
        user_id=user_id,
        title=(title or "")[:255],
        body=body or "",
        data=data if isinstance(data, dict) else None,
        kind=kind,
    )


# Discretionary push categories the BA can mute (PushPreference). Everything
# not resolved here is transactional (you got booked, your shift was
# cancelled, an applicant decision, …) and ALWAYS sends. Resolution leans on
# the payload fields each sender already sets — `type` for shift offers,
# `kind` for chat/pay/gigs/checklist, `screen` for reminders/pay fallbacks.
def _push_category(data: "dict[str, Any] | None") -> "str | None":
    """Map a push ``data`` payload to a mutable preference category, or None.

    None means "not a discretionary category" → never gated.
    """
    if not isinstance(data, dict):
        return None
    type_ = str(data.get("type") or "").strip().lower()
    kind = str(data.get("kind") or "").strip().lower()
    screen = str(data.get("screen") or "").strip().lower()

    if type_ == "shift_offer":
        return "shift_offers"
    if kind == "chat" or screen == "chat":
        return "chat"
    if kind == "payment" or screen == "earnings":
        return "pay"
    if kind in {"new_gig_nearby", "new_gig", "gig_digest", "job_digest", "open_shift"}:
        return "gigs"
    if (
        kind in {"activation_reminder", "recap_nudge", "recap_reminder", "pre_shift_checklist"}
        or screen == "recap"
    ):
        return "reminders"
    return None


@sync_to_async
def _is_push_category_muted(user_id: int, category: str) -> bool:
    """True if the user has explicitly turned this category off.

    Missing PushPreference row → all categories on (returns False).
    """
    from .models import PushPreference

    pref = PushPreference.objects.filter(user_id=user_id).first()
    if pref is None:
        return False
    return not bool(getattr(pref, category, True))


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

    # Log the in-app record FIRST, before the device check — the BA's
    # Notifications inbox should reflect everything we sent even when no device
    # is currently reachable. Best-effort: a logging failure never blocks send.
    try:
        await _record_push_notification(user_id, title, body, data)
    except Exception:
        logger.warning(
            "failed to record push notification for user_id=%s", user_id, exc_info=True
        )

    # Respect the BA's push opt-outs for discretionary categories. The inbox
    # record above is already written, so muting silences the banner without
    # losing history. Best-effort: a preference lookup failure must never
    # block a send (fail open).
    try:
        category = _push_category(data)
        if category and await _is_push_category_muted(user_id, category):
            logger.info(
                "push suppressed by preference user_id=%s category=%s", user_id, category
            )
            return 0
    except Exception:
        logger.warning(
            "push preference check failed for user_id=%s", user_id, exc_info=True
        )

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
    """Sync wrapper for the async push send.

    Safe to call from ANY context: a plain sync RQ worker, an async GraphQL
    resolver's inline fallback, a sync Django view running under ASGI, or
    inside a ``sync_to_async`` block.

    It ALWAYS drives the coroutine on a fresh, dedicated thread (its own clean
    event loop) — never on the calling thread. This is deliberate and load-
    bearing:

    Under ASGI (Cloud Run), Django runs sync views — and ``sync_to_async``
    bodies — on asgiref's *thread-sensitive* executor thread, which has no
    running event loop. The old code detected "no running loop" and called
    ``asyncio.run()`` directly on that thread. But ``send_push_to_user`` then
    ``await``s ``_record_push_notification``, a ``thread_sensitive=True``
    ``sync_to_async`` DB write, which asgiref routes back to that SAME
    thread-sensitive thread — the one now blocked inside ``asyncio.run()``.
    Result: deadlock until Cloud Run's ~300s request timeout (504). It only
    bit when the target user had a registered push device (recap-nudge cron,
    extension decisions, etc.).

    A fresh thread starts with no asgiref thread-local, so the nested
    ``thread_sensitive`` write falls back to asgiref's shared single-thread
    executor (a *different* thread) and completes — no deadlock from any
    caller. This is the same dedicated-thread path that already worked in prod
    for the inline async fallback (#820).
    """

    def _run() -> int:
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

    import concurrent.futures

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
        return executor.submit(_run).result()


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


# NOTE: the recap-nudge scheduler (schedule_recap_nudge_at /
# _send_recap_nudge_if_unfiled) used to live here, scheduling a per-shift
# "don't forget your recap" push via django-rq at event end + N hours. It
# never fired — there is no rqscheduler in prod — so it was removed. The
# recap nudge is now a wall-clock cron that sends inline (no worker):
# recaps/management/commands/send_recap_nudges.py, hit via
# /internal/cron/recap-nudges. Likewise the activation reminder is now
# send_activation_reminders.py → /internal/cron/activation-reminders.
# `schedule_push_at` above is still used (the pre-shift checklist), and
# `enqueue_push` (immediate sends, with inline fallback) is the live path
# for shift offers and the other event-driven pushes.
