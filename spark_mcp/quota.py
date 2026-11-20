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

from django.db import connection as db_connection, transaction
from django.utils import timezone

from . import conf
from .models import McpToolCall

OPS_TZ = ZoneInfo("America/Los_Angeles")
# pg_advisory_xact_lock namespace for "one brand's quota" (first key).
_LOCK_NAMESPACE = 0x5350  # "SP"
# At most this many refused calls are logged per connection per minute, so a
# client hammering a closed quota can't fill the table.
MAX_REFUSED_LOGGED_PER_MINUTE = 5
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


def _lock_tenant(tenant_id: int) -> None:
    if db_connection.vendor != "postgresql":
        return
    with db_connection.cursor() as cur:
        cur.execute("SELECT pg_advisory_xact_lock(%s, %s)", [_LOCK_NAMESPACE, int(tenant_id)])


def reserve(connection, *, tool: str, args_summary: dict) -> tuple[Verdict, McpToolCall | None]:
    """Check the limits and claim a slot in one step.

    The check and the row that counts against the quota are written under a
    per-brand lock, so two simultaneous calls can't both see "one left" and
    both run. The returned row is the call's log entry: ``finish`` fills in
    the outcome. Refused calls get a log row too, capped per minute."""
    now = timezone.now()
    with transaction.atomic():
        _lock_tenant(connection.tenant_id)
        verdict = check(connection, now)
        if not verdict.allowed:
            refused_recently = McpToolCall.objects.filter(
                connection=connection,
                created_at__gte=now - timedelta(seconds=60),
                status__in=(McpToolCall.STATUS_RATE, McpToolCall.STATUS_QUOTA),
            ).count()
            if refused_recently < MAX_REFUSED_LOGGED_PER_MINUTE:
                record(connection, tool=tool, args_summary=args_summary, status=verdict.status)
            return verdict, None
        row = record(connection, tool=tool, args_summary=args_summary, status=McpToolCall.STATUS_OK)
    return verdict, row


def finish(
    row: McpToolCall,
    *,
    status: str,
    result_count: int | None = None,
    latency_ms: int | None = None,
    error: str = "",
) -> None:
    McpToolCall.objects.filter(pk=row.pk).update(
        status=status,
        result_count=result_count,
        latency_ms=latency_ms,
        error=(error or "")[:300],
    )
