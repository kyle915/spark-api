"""Shared, database-backed throttles for the MCP sign-in and OAuth endpoints.

Counters live in ``McpThrottle`` so the limits hold across every Cloud Run
instance. ``client_ip`` reads the address Google's front end saw, not the
left-most ``X-Forwarded-For`` entry, which the caller controls.
"""

from __future__ import annotations

import hashlib
import ipaddress
import logging
import random
from datetime import timedelta

from django.conf import settings
from django.db import IntegrityError, transaction
from django.db.models import F
from django.http import HttpRequest
from django.utils import timezone

logger = logging.getLogger(__name__)


def client_ip(request: HttpRequest) -> str:
    """The connecting client's IP.

    Cloud Run's front end appends the address it received the connection
    from to ``X-Forwarded-For``, so the trustworthy entry is counted from the
    right: ``MCP_TRUSTED_PROXY_HOPS`` (default 1) entries from the end. Every
    entry to the left of it is whatever the caller chose to send.
    """
    hops = int(getattr(settings, "MCP_TRUSTED_PROXY_HOPS", 1) or 0)
    fwd = request.META.get("HTTP_X_FORWARDED_FOR", "")
    parts = [p.strip() for p in fwd.split(",") if p.strip()]
    candidate = parts[-hops] if hops > 0 and len(parts) >= hops else ""
    if not candidate:
        candidate = request.META.get("REMOTE_ADDR", "") or ""
    try:
        return str(ipaddress.ip_address(candidate))
    except ValueError:
        return "?"


def _key(bucket: str) -> str:
    return hashlib.sha256(bucket.encode("utf-8")).hexdigest()


def hit(bucket: str, limit: int, seconds: int) -> bool:
    """Count one attempt in ``bucket``. True when it is over ``limit`` for the
    current ``seconds``-long window (the attempt should be refused)."""
    from .models import McpThrottle

    now = timezone.now()
    key = _key(bucket)
    window_floor = now - timedelta(seconds=seconds)
    try:
        with transaction.atomic():
            row = McpThrottle.objects.select_for_update().filter(key=key).first()
            if row is None:
                try:
                    with transaction.atomic():
                        McpThrottle.objects.create(key=key, window_start=now, count=1)
                    return 1 > limit
                except IntegrityError:
                    row = McpThrottle.objects.select_for_update().get(key=key)
            if row.window_start <= window_floor:
                row.window_start, row.count = now, 1
                row.save(update_fields=["window_start", "count"])
                return 1 > limit
            McpThrottle.objects.filter(pk=row.pk).update(count=F("count") + 1)
            over = row.count + 1 > limit
    except Exception:  # noqa: BLE001 — a database hiccup here must not lock everyone out
        logger.exception("MCP throttle failed for a bucket")
        return False
    if random.random() < 0.01:
        _sweep(now)
    return over


def mark(bucket: str) -> None:
    """Remember that something happened now (e.g. a failed fetch)."""
    from .models import McpThrottle

    McpThrottle.objects.update_or_create(
        key=_key(bucket), defaults={"window_start": timezone.now(), "count": 1}
    )


def marked_within(bucket: str, seconds: int) -> bool:
    from .models import McpThrottle

    since = timezone.now() - timedelta(seconds=seconds)
    return McpThrottle.objects.filter(key=_key(bucket), window_start__gt=since).exists()


def _sweep(now) -> None:
    from .models import McpThrottle

    try:
        McpThrottle.objects.filter(window_start__lt=now - timedelta(days=1)).delete()
    except Exception:  # noqa: BLE001
        logger.info("MCP throttle sweep failed", exc_info=True)
