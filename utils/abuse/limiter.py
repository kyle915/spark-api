"""Postgres-backed fixed-window rate limiter shared by every Cloud Run instance.

Why Postgres: there is no Redis on Cloud Run and the default cache is a
per-instance LocMemCache, so a limiter built on it multiplies its limit by
the instance count (and resets on every cold start). One atomic
``INSERT ... ON CONFLICT DO UPDATE ... RETURNING count`` per check is a
single round trip on an indexed unique key — cheap enough per request.

Semantics: fixed windows aligned to the epoch (``window_seconds``). The count
for the current window is incremented first, then compared, so concurrent
callers can never both slip under the limit.

Failure policy: any database error FAILS OPEN (allowed=True) and is logged on
``spark.abuse`` — the limiter must never take the product down. Callers that
need fail-closed behaviour can check ``result.degraded``.

Function API::

    hit(key, limit, window_seconds, cost=1) -> RateLimitResult
    await ahit(key, limit, window_seconds, cost=1, raise_on_limit=False)
    peek(key, window_seconds) -> int          # current window count, no increment
    raise_if_limited(result, message=..., code=...)  # GraphQLError w/ extensions
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import datetime, timezone

from asgiref.sync import sync_to_async
from django.db import DatabaseError, connection
from graphql import GraphQLError

logger = logging.getLogger("spark.abuse")


def _reset_connection() -> None:
    """Drop a possibly-dead connection (Cloud SQL idle drop on the shared
    asgiref thread) so the next guardrail call reconnects instead of
    failing open forever."""
    try:
        connection.close()
    except Exception:  # noqa: BLE001
        pass

RATE_LIMITED_MESSAGE = "Too many requests — slow down and try again in a minute."


@dataclass(frozen=True)
class RateLimitResult:
    allowed: bool
    count: int
    limit: int
    window_seconds: int
    retry_after: int
    degraded: bool = False  # True when the DB failed and we failed open
    key: str = ""


class RateLimitExceeded(GraphQLError):
    """GraphQLError carrying ``extensions.code`` + ``retryAfter`` for the FE."""

    def __init__(self, message: str = RATE_LIMITED_MESSAGE, *, code: str = "RATE_LIMITED",
                 retry_after: int = 60, extra: dict | None = None):
        ext = {"code": code, "retryAfter": int(max(retry_after, 1))}
        if extra:
            ext.update(extra)
        super().__init__(message, extensions=ext)


def _window_start(window_seconds: int, now: float | None = None) -> tuple[datetime, int]:
    now = time.time() if now is None else now
    start = int(now // window_seconds) * window_seconds
    retry_after = int(start + window_seconds - now) + 1
    return datetime.fromtimestamp(start, tz=timezone.utc), retry_after


_UPSERT_SQL = (
    "INSERT INTO abuse_ratelimitcounter (key, window_start, count) "
    "VALUES (%s, %s, %s) "
    "ON CONFLICT (key, window_start) DO UPDATE "
    "SET count = abuse_ratelimitcounter.count + EXCLUDED.count "
    "RETURNING count"
)


def hit(key: str, limit: int, window_seconds: int, *, cost: int = 1,
        now: float | None = None) -> RateLimitResult:
    """Count ``cost`` hits against ``key`` in the current window.

    ``allowed`` is False once the window's count exceeds ``limit``. A
    ``limit`` <= 0 disables the check (always allowed, nothing written).
    """
    key = key[:200]
    if limit <= 0:
        return RateLimitResult(True, 0, limit, window_seconds, 0)
    window_start, retry_after = _window_start(window_seconds, now)
    try:
        with connection.cursor() as cursor:
            cursor.execute(_UPSERT_SQL, [key, window_start, max(int(cost), 0)])
            count = int(cursor.fetchone()[0])
    except DatabaseError:
        _reset_connection()
        logger.warning("rate limiter unavailable for %s — failing open", key, exc_info=True)
        return RateLimitResult(True, 0, limit, window_seconds, 0, degraded=True)
    return RateLimitResult(count <= limit, count, limit, window_seconds, retry_after, key=key)


def peek(key: str, window_seconds: int, *, now: float | None = None) -> int:
    window_start, _ = _window_start(window_seconds, now)
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT count FROM abuse_ratelimitcounter WHERE key=%s AND window_start=%s",
                [key[:200], window_start],
            )
            row = cursor.fetchone()
    except DatabaseError:
        _reset_connection()
        logger.warning("rate limiter peek failed for %s", key, exc_info=True)
        return 0
    return int(row[0]) if row else 0


def raise_if_limited(result: RateLimitResult, message: str = RATE_LIMITED_MESSAGE,
                     code: str = "RATE_LIMITED") -> None:
    if not result.allowed:
        raise RateLimitExceeded(message, code=code, retry_after=result.retry_after)


async def ahit(key: str, limit: int, window_seconds: int, *, cost: int = 1,
               raise_on_limit: bool = False, message: str = RATE_LIMITED_MESSAGE,
               code: str = "RATE_LIMITED") -> RateLimitResult:
    result = await sync_to_async(hit)(key, limit, window_seconds, cost=cost)
    if raise_on_limit:
        raise_if_limited(result, message, code)
    return result


def hit_many(rules: list[tuple[str, int, int]], *, cost: int = 1,
             now: float | None = None) -> RateLimitResult | None:
    """Apply several ``(key, limit, window_seconds)`` rules in ONE round trip.

    Every rule is counted (so a burst that trips the minute window still
    accrues against the hour window). Returns the over-limit result with the
    longest ``retry_after``, or None when every rule is within its limit.
    Fails open (returns None) on database errors.
    """
    active = [(k[:200], lim, win) for k, lim, win in rules if lim > 0]
    if not active:
        return None
    rows, params = [], []
    meta: dict[str, tuple[int, int, int]] = {}
    for key, limit, window in active:
        start, retry_after = _window_start(window, now)
        rows.append("(%s, %s, %s)")
        params.extend([key, start, max(int(cost), 0)])
        meta[key] = (limit, window, retry_after)
    sql = (
        "INSERT INTO abuse_ratelimitcounter (key, window_start, count) VALUES "
        + ", ".join(rows)
        + " ON CONFLICT (key, window_start) DO UPDATE "
        "SET count = abuse_ratelimitcounter.count + EXCLUDED.count "
        "RETURNING key, count"
    )
    try:
        with connection.cursor() as cursor:
            cursor.execute(sql, params)
            results = cursor.fetchall()
    except DatabaseError:
        _reset_connection()
        logger.warning("rate limiter unavailable (%d rules) — failing open", len(active),
                       exc_info=True)
        return None
    worst: RateLimitResult | None = None
    for key, count in results:
        limit, window, retry_after = meta[key]
        if count > limit:
            res = RateLimitResult(False, int(count), limit, window, retry_after, key=key)
            if worst is None or res.retry_after > worst.retry_after:
                worst = res
    return worst


async def ahit_many(rules: list[tuple[str, int, int]], *, cost: int = 1) -> RateLimitResult | None:
    return await sync_to_async(hit_many)(rules, cost=cost)
