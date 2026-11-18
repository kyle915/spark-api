"""Quota, rate limit and call log for MCP tool calls.

This is a deliberately small interface (``check`` + ``record``) so it can be
switched over to the shared abuse guardrails when they land.

TODO(phase-1 guardrails): once the spark-api rate limiter / audit log
(``utils/abuse/``) is merged, implement ``check`` with its limiter and have
``record`` also write to its audit log. Keep ``McpToolCall`` as the per-call
MCP log either way (the admin and the brand's "Connected AI apps" view read
it), and keep the daily per-brand quota semantics described below.

Counting is done on ``McpToolCall`` rows in the database, so the limits hold
across every Cloud Run instance:

* per brand (tenant): ``MCP_DAILY_CALLS_PER_TENANT`` calls per ops day
  (Pacific), summed over every person and AI app connected to that brand;
* per connection: ``MCP_CALLS_PER_MINUTE_PER_CONNECTION`` calls in any
  rolling 60 seconds.

Only calls that ran (status ``ok`` or ``error``) count; refused calls are
logged but do not eat into the quota.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

from django.utils import timezone

from . import conf
from .models import McpToolCall

OPS_TZ = ZoneInfo("America/Los_Angeles")
COUNTED = (McpToolCall.STATUS_OK, McpToolCall.STATUS_ERROR)


@dataclass
class Verdict:
    allowed: bool
    status: str = McpToolCall.STATUS_OK
    message: str = ""
    retry_after_seconds: int | None = None


def _ops_day_start(now: datetime) -> datetime:
    local = now.astimezone(OPS_TZ)
    return datetime.combine(local.date(), time.min, tzinfo=OPS_TZ)


def used_today(tenant_id: int, now: datetime | None = None) -> int:
    now = now or timezone.now()
    return McpToolCall.objects.filter(
        tenant_id=tenant_id,
        created_at__gte=_ops_day_start(now),
        status__in=COUNTED,
    ).count()


def check(connection, now: datetime | None = None) -> Verdict:
    now = now or timezone.now()
    per_minute = conf.calls_per_minute_per_connection()
    # Rejected-argument calls still hit the server, so they count here.
    recent = McpToolCall.objects.filter(
        connection=connection,
        created_at__gte=now - timedelta(seconds=60),
        status__in=COUNTED + (McpToolCall.STATUS_DENIED,),
    ).count()
    if recent >= per_minute:
        return Verdict(
            False,
            McpToolCall.STATUS_RATE,
            f"Too many requests: this connection is limited to {per_minute} "
            "tool calls a minute. Wait a minute and try again.",
            retry_after_seconds=60,
        )
    daily = conf.daily_calls_per_tenant()
    if used_today(connection.tenant_id, now) >= daily:
        tomorrow = _ops_day_start(now) + timedelta(days=1)
        return Verdict(
            False,
            McpToolCall.STATUS_QUOTA,
            f"This brand has used its {daily} AI tool calls for today. "
            "The quota resets at midnight Pacific time.",
            retry_after_seconds=max(60, int((tomorrow - now).total_seconds())),
        )
    return Verdict(True)


def record(
    connection,
    *,
    tool: str,
    args_summary: dict,
    status: str,
    result_count: int | None = None,
    latency_ms: int | None = None,
    error: str = "",
) -> McpToolCall:
    return McpToolCall.objects.create(
        connection=connection,
        user_id=connection.user_id,
        tenant_id=connection.tenant_id,
        client_name=connection.client.display_name[:200],
        tool=tool[:80],
        args_summary=args_summary,
        status=status,
        result_count=result_count,
        latency_ms=latency_ms,
        error=(error or "")[:300],
    )
