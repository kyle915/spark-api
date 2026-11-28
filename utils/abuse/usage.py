"""Hourly per-user usage rollups + distinct-ambassador read log.

    record_usage(identity, operation, *, ip, ua, error=False, throttled=False,
                 paged=False, response_bytes=0, now=None)
    flush_usage_buffer()
    record_ambassador_reads(identity, keys, operation, now=None)

In production (`ABUSE_USAGE_FLUSH_INTERVAL_SECONDS > 0`, default 15s), hits are
coalesced in a bounded thread-safe in-memory map and flushed in a single DB
batch so 8–12 concurrent dashboard GraphQL queries never contend on Postgres
row locks on the request critical path. When interval <= 0 (tests or explicit
env override), each call flushes immediately. All paths fail open.
"""

from __future__ import annotations

import atexit
import json
import logging
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone

from django.conf import settings
from django.db import DatabaseError, connection

from .identity import Identity

logger = logging.getLogger("spark.abuse")


def _reset_connection() -> None:
    """Drop a possibly-dead connection (Cloud SQL idle drop on the shared
    asgiref thread) so the next guardrail call reconnects instead of
    failing open forever."""
    try:
        connection.close()
    except Exception:  # noqa: BLE001
        pass


MAX_SAMPLES = 10
MAX_BUFFERED_BUCKETS = 256

_USAGE_SQL = """
INSERT INTO abuse_usagebucket
    (hour, user_id, tenant_id, is_admin, operation, request_count, error_count,
     throttled_count, paged_count, response_bytes, ips, user_agents)
VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s::jsonb)
ON CONFLICT (hour, user_id, operation) DO UPDATE SET
    request_count = abuse_usagebucket.request_count + EXCLUDED.request_count,
    error_count = abuse_usagebucket.error_count + EXCLUDED.error_count,
    throttled_count = abuse_usagebucket.throttled_count + EXCLUDED.throttled_count,
    paged_count = abuse_usagebucket.paged_count + EXCLUDED.paged_count,
    response_bytes = abuse_usagebucket.response_bytes + EXCLUDED.response_bytes,
    ips = (
        SELECT COALESCE(jsonb_agg(val), '[]'::jsonb)
        FROM (
            SELECT DISTINCT val
            FROM jsonb_array_elements(abuse_usagebucket.ips || EXCLUDED.ips) AS t(val)
            LIMIT %s
        ) sub
    ),
    user_agents = (
        SELECT COALESCE(jsonb_agg(val), '[]'::jsonb)
        FROM (
            SELECT DISTINCT val
            FROM jsonb_array_elements(abuse_usagebucket.user_agents || EXCLUDED.user_agents) AS t(val)
            LIMIT %s
        ) sub
    )
"""


@dataclass
class _PendingBucket:
    hour: datetime
    user_id: int
    tenant_id: int | None
    is_admin: bool
    operation: str
    request_count: int = 0
    error_count: int = 0
    throttled_count: int = 0
    paged_count: int = 0
    response_bytes: int = 0
    ips: list[str] = field(default_factory=list)
    user_agents: list[str] = field(default_factory=list)

    def add_sample(self, ip: str, ua: str) -> None:
        if ip:
            clipped_ip = ip[:64]
            if clipped_ip not in self.ips and len(self.ips) < MAX_SAMPLES:
                self.ips.append(clipped_ip)
        if ua:
            clipped_ua = ua[:200]
            if clipped_ua not in self.user_agents and len(self.user_agents) < MAX_SAMPLES:
                self.user_agents.append(clipped_ua)


_BUFFER_LOCK = threading.Lock()
_BUFFER: dict[tuple[datetime, int, str], _PendingBucket] = {}
_LAST_FLUSH_MONOTONIC: float = time.monotonic()


def _hour(now: datetime | None) -> datetime:
    now = now or datetime.now(timezone.utc)
    return now.replace(minute=0, second=0, microsecond=0)


def _flush_interval_seconds() -> float:
    try:
        return float(getattr(settings, "ABUSE_USAGE_FLUSH_INTERVAL_SECONDS", 15.0) or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _write_buckets(buckets: list[_PendingBucket]) -> None:
    if not buckets:
        return
    try:
        with connection.cursor() as cursor:
            for b in buckets:
                cursor.execute(
                    _USAGE_SQL,
                    [
                        b.hour,
                        b.user_id,
                        b.tenant_id,
                        b.is_admin,
                        b.operation,
                        b.request_count,
                        b.error_count,
                        b.throttled_count,
                        b.paged_count,
                        b.response_bytes,
                        json.dumps(b.ips),
                        json.dumps(b.user_agents),
                        MAX_SAMPLES,
                        MAX_SAMPLES,
                    ],
                )
    except DatabaseError:
        _reset_connection()
        logger.warning("usage rollup write failed", exc_info=True)


def flush_usage_buffer() -> int:
    """Drain all buffered usage buckets to Postgres. Returns bucket count flushed."""
    global _LAST_FLUSH_MONOTONIC
    with _BUFFER_LOCK:
        if not _BUFFER:
            _LAST_FLUSH_MONOTONIC = time.monotonic()
            return 0
        pending = list(_BUFFER.values())
        _BUFFER.clear()
        _LAST_FLUSH_MONOTONIC = time.monotonic()
    _write_buckets(pending)
    return len(pending)


def clear_usage_buffer() -> None:
    """Drop buffered usage entries without writing (test isolation helper)."""
    global _LAST_FLUSH_MONOTONIC
    with _BUFFER_LOCK:
        _BUFFER.clear()
        _LAST_FLUSH_MONOTONIC = time.monotonic()


atexit.register(flush_usage_buffer)


def record_usage(
    identity: Identity,
    operation: str,
    *,
    ip: str = "",
    ua: str = "",
    error: bool = False,
    throttled: bool = False,
    paged: bool = False,
    response_bytes: int = 0,
    now: datetime | None = None,
) -> None:
    if identity is None or identity.user_id is None:
        return

    hour = _hour(now)
    op = (operation or "anonymous")[:120]
    interval = _flush_interval_seconds()

    if interval <= 0.0:
        entry = _PendingBucket(
            hour=hour,
            user_id=identity.user_id,
            tenant_id=identity.tenant_id,
            is_admin=bool(identity.is_admin),
            operation=op,
            request_count=1,
            error_count=int(bool(error)),
            throttled_count=int(bool(throttled)),
            paged_count=int(bool(paged)),
            response_bytes=int(max(response_bytes, 0)),
        )
        entry.add_sample(ip, ua)
        _write_buckets([entry])
        return

    to_flush: list[_PendingBucket] | None = None
    global _LAST_FLUSH_MONOTONIC
    with _BUFFER_LOCK:
        key = (hour, identity.user_id, op)
        bucket = _BUFFER.get(key)
        if bucket is None:
            bucket = _PendingBucket(
                hour=hour,
                user_id=identity.user_id,
                tenant_id=identity.tenant_id,
                is_admin=bool(identity.is_admin),
                operation=op,
            )
            _BUFFER[key] = bucket
        bucket.request_count += 1
        bucket.error_count += int(bool(error))
        bucket.throttled_count += int(bool(throttled))
        bucket.paged_count += int(bool(paged))
        bucket.response_bytes += int(max(response_bytes, 0))
        bucket.add_sample(ip, ua)

        elapsed = time.monotonic() - _LAST_FLUSH_MONOTONIC
        if elapsed >= interval or len(_BUFFER) >= MAX_BUFFERED_BUCKETS:
            to_flush = list(_BUFFER.values())
            _BUFFER.clear()
            _LAST_FLUSH_MONOTONIC = time.monotonic()

    if to_flush:
        _write_buckets(to_flush)


def record_ambassador_reads(
    identity: Identity,
    keys,
    operation: str,
    now: datetime | None = None,
) -> int:
    """Insert-ignore one row per distinct ambassador key for today. Returns
    how many keys were offered (not how many were new)."""
    if identity is None or identity.user_id is None:
        return 0
    keys = {str(k)[:80] for k in keys if k not in (None, "")}
    if not keys:
        return 0
    from .models import AmbassadorReadLog

    day = (now or datetime.now(timezone.utc)).date()
    try:
        AmbassadorReadLog.objects.bulk_create(
            [
                AmbassadorReadLog(
                    day=day,
                    user_id=identity.user_id,
                    tenant_id=identity.tenant_id,
                    ambassador_key=k,
                    first_operation=(operation or "")[:120],
                )
                for k in list(keys)[:2000]
            ],
            ignore_conflicts=True,
        )
    except DatabaseError:
        _reset_connection()
        logger.warning("ambassador read log write failed", exc_info=True)
    return len(keys)
