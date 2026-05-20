"""
Tiny helpers for writing to the RequestActivityLog audit trail.

Centralizing writes here keeps the per-mutation code small and makes
it easy to add new event kinds later without touching every callsite.

All helpers are sync-safe: the calling mutation wraps with
sync_to_async if needed. None of these raise on bad inputs (a missing
actor or stale request still writes a log row with reduced info) —
audit logging should never break the underlying business operation.
"""

from __future__ import annotations

from typing import Any, Optional

from asgiref.sync import sync_to_async

from .models import Request, RequestActivityLog


def _safe_log(
    *,
    request: Request,
    kind: str,
    actor_user: Optional[Any] = None,
    summary: str = "",
    metadata: Optional[dict] = None,
) -> Optional[RequestActivityLog]:
    """Write a single audit row. Swallows DB errors — audit must never
    break the underlying mutation."""
    try:
        return RequestActivityLog.objects.create(
            tenant=request.tenant,
            request=request,
            kind=kind,
            actor_user=actor_user if getattr(actor_user, "id", None) else None,
            summary=summary[:512],
            metadata=metadata or {},
        )
    except Exception:
        # Best-effort: never let a logging failure cascade.
        return None


async def alog(
    *,
    request: Request,
    kind: str,
    actor_user: Optional[Any] = None,
    summary: str = "",
    metadata: Optional[dict] = None,
) -> None:
    """Async fire-and-forget version. Most strawberry mutations are
    async, so this is the path they should call."""
    await sync_to_async(_safe_log)(
        request=request,
        kind=kind,
        actor_user=actor_user,
        summary=summary,
        metadata=metadata,
    )


# --------------------------------------------------------------------
# Convenience constructors — one per event kind. Keeps callsites in the
# mutation file short and self-documenting.
# --------------------------------------------------------------------


async def log_created(*, request, actor_user) -> None:
    await alog(
        request=request,
        actor_user=actor_user,
        kind=RequestActivityLog.KIND_CREATED,
        summary=f"Request created for {request.retailer_name or request.address or 'venue'}",
    )


async def log_updated(*, request, actor_user, changed_fields=None) -> None:
    fields = list(changed_fields or [])
    summary = (
        "Request updated"
        if not fields
        else "Updated: " + ", ".join(fields[:6]) + ("…" if len(fields) > 6 else "")
    )
    await alog(
        request=request,
        actor_user=actor_user,
        kind=RequestActivityLog.KIND_UPDATED,
        summary=summary,
        metadata={"fields": fields[:20]},
    )


async def log_status_change(
    *, request, actor_user, from_status: str, to_status: str
) -> None:
    await alog(
        request=request,
        actor_user=actor_user,
        kind=RequestActivityLog.KIND_STATUS_CHANGED,
        summary=f"Status: {from_status or '—'} → {to_status or '—'}",
        metadata={"from": from_status, "to": to_status},
    )


async def log_ba_invited(*, request, actor_user, ba_name: str, ba_uuid: str) -> None:
    await alog(
        request=request,
        actor_user=actor_user,
        kind=RequestActivityLog.KIND_BA_INVITED,
        summary=f"Invited {ba_name}",
        metadata={"ambassador_uuid": ba_uuid, "ba_name": ba_name},
    )


async def log_cloned_from(*, request, actor_user, source_request_uuid: str) -> None:
    await alog(
        request=request,
        actor_user=actor_user,
        kind=RequestActivityLog.KIND_CLONED_FROM,
        summary=f"Cloned from {source_request_uuid[:8]}",
        metadata={"source_request_uuid": source_request_uuid},
    )


async def log_recap_filed(
    *, request, actor_user, recap_uuid: str, ba_name: Optional[str] = None
) -> None:
    suffix = f" by {ba_name}" if ba_name else ""
    await alog(
        request=request,
        actor_user=actor_user,
        kind=RequestActivityLog.KIND_RECAP_FILED,
        summary=f"Recap filed{suffix}",
        metadata={"recap_uuid": recap_uuid, "ba_name": ba_name or ""},
    )


async def log_nudge_sent(*, request, actor_user, ba_name: str) -> None:
    await alog(
        request=request,
        actor_user=actor_user,
        kind=RequestActivityLog.KIND_NUDGE_SENT,
        summary=f"Nudged {ba_name} for recap",
        metadata={"ba_name": ba_name},
    )
