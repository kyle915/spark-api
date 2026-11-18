"""Hourly per-user usage rollups + distinct-ambassador read log.

    record_usage(identity, operation, *, ip, ua, error=False, throttled=False,
                 paged=False, response_bytes=0, now=None)
    record_ambassador_reads(identity, keys, operation, now=None)

One upsert per request (no row per request). Both fail open.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

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

_USAGE_SQL = """
INSERT INTO abuse_usagebucket
    (hour, user_id, tenant_id, is_admin, operation, request_count, error_count,
     throttled_count, paged_count, response_bytes, ips, user_agents)
VALUES (%s, %s, %s, %s, %s, 1, %s, %s, %s, %s, %s::jsonb, %s::jsonb)
ON CONFLICT (hour, user_id, operation) DO UPDATE SET
    request_count = abuse_usagebucket.request_count + 1,
    error_count = abuse_usagebucket.error_count + EXCLUDED.error_count,
    throttled_count = abuse_usagebucket.throttled_count + EXCLUDED.throttled_count,
    paged_count = abuse_usagebucket.paged_count + EXCLUDED.paged_count,
    response_bytes = abuse_usagebucket.response_bytes + EXCLUDED.response_bytes,
    ips = CASE
        WHEN abuse_usagebucket.ips @> EXCLUDED.ips
          OR jsonb_array_length(abuse_usagebucket.ips) >= %s
        THEN abuse_usagebucket.ips
        ELSE abuse_usagebucket.ips || EXCLUDED.ips END,
    user_agents = CASE
        WHEN abuse_usagebucket.user_agents @> EXCLUDED.user_agents
          OR jsonb_array_length(abuse_usagebucket.user_agents) >= %s
        THEN abuse_usagebucket.user_agents
        ELSE abuse_usagebucket.user_agents || EXCLUDED.user_agents END
"""


def _hour(now: datetime | None) -> datetime:
    now = now or datetime.now(timezone.utc)
    return now.replace(minute=0, second=0, microsecond=0)


def record_usage(identity: Identity, operation: str, *, ip: str = "", ua: str = "",
                 error: bool = False, throttled: bool = False, paged: bool = False,
                 response_bytes: int = 0, now: datetime | None = None) -> None:
    if identity is None or identity.user_id is None:
        return
    import json

    try:
        with connection.cursor() as cursor:
            cursor.execute(
                _USAGE_SQL,
                [
                    _hour(now), identity.user_id, identity.tenant_id, identity.is_admin,
                    (operation or "anonymous")[:120],
                    int(bool(error)), int(bool(throttled)), int(bool(paged)),
                    int(max(response_bytes, 0)),
                    json.dumps([ip[:64]] if ip else []),
                    json.dumps([ua[:200]] if ua else []),
                    MAX_SAMPLES, MAX_SAMPLES,
                ],
            )
    except DatabaseError:
        _reset_connection()
        logger.warning("usage rollup write failed", exc_info=True)


def record_ambassador_reads(identity: Identity, keys, operation: str,
                            now: datetime | None = None) -> int:
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
                AmbassadorReadLog(day=day, user_id=identity.user_id,
                                  tenant_id=identity.tenant_id, ambassador_key=k,
                                  first_operation=(operation or "")[:120])
                for k in list(keys)[:2000]
            ],
            ignore_conflicts=True,
        )
    except DatabaseError:
        _reset_connection()
        logger.warning("ambassador read log write failed", exc_info=True)
    return len(keys)
