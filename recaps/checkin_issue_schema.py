"""Staff view of what went wrong on the ambassador web check-in.

Rolls up ``CheckinFieldIssue`` rows (reported by ambassadors' phones) by
kind for one brand over the last few days. Ignite staff only — it is an
operations signal, not client reporting.
"""

from __future__ import annotations

import logging
from datetime import timedelta

import strawberry
from asgiref.sync import sync_to_async
from django.utils import timezone

from utils.graphql.mixins import resolve_id_to_int
from utils.graphql.permissions import (
    StrictIsAuthenticated,
    _is_admin_access,
    resolve_request_user_access,
)

logger = logging.getLogger(__name__)

KIND_LABELS = {
    "photo_upload_failed": "Photo uploads failed",
    "gps_denied": "Location permission denied",
    "gps_timeout": "Location timed out",
    "request_timeout": "Requests timed out (weak signal)",
    "punch_not_saved": "Clock punch not saved",
    "storage_unavailable": "Phone storage unavailable",
    "recap_submit_failed": "Recap submit failed",
}


@strawberry.type
class CheckinIssueSummary:
    kind: str
    label: str
    count: int
    events: int
    last_at: str
    last_detail: str


def summarize_issues(rows) -> list[dict]:
    """``(kind, event_id, created_at, detail)`` rows, newest first → one
    summary per kind, most frequent first. Pure."""
    out: dict[str, dict] = {}
    for kind, event_id, created_at, detail in rows:
        s = out.get(kind)
        if s is None:
            s = out[kind] = {
                "kind": kind,
                "label": KIND_LABELS.get(kind, kind.replace("_", " ").capitalize()),
                "count": 0,
                "_events": set(),
                "last_at": created_at.isoformat() if created_at else "",
                "last_detail": detail or "",
            }
        s["count"] += 1
        if event_id:
            s["_events"].add(event_id)
    result = []
    for s in out.values():
        events = s.pop("_events")
        result.append({**s, "events": len(events)})
    result.sort(key=lambda s: (-s["count"], s["label"]))
    return result


@strawberry.type
class CheckinIssueQueries:
    @strawberry.field(permission_classes=[StrictIsAuthenticated])
    async def checkin_field_issues(
        self,
        info: strawberry.Info,
        tenant_id: strawberry.ID,
        days: int = 7,
    ) -> list[CheckinIssueSummary]:
        """Problems ambassadors' phones reported on the web check-in for this
        brand in the last ``days`` days, by kind. Ignite staff only; anyone
        else, or any failure, gets an empty list."""
        try:
            user = info.context.request.user
            role_slug, is_staff, is_super, email = await resolve_request_user_access(user)
            if not _is_admin_access(role_slug, is_staff, is_super, email):
                return []
            tenant_pk = resolve_id_to_int(tenant_id)
        except Exception:  # noqa: BLE001
            return []
        window = max(1, min(int(days or 7), 60))

        def _read():
            from recaps.models import CheckinFieldIssue

            rows = (
                CheckinFieldIssue.objects.filter(
                    tenant_id=tenant_pk,
                    created_at__gte=timezone.now() - timedelta(days=window),
                )
                .order_by("-created_at")
                .values_list("kind", "event_id", "created_at", "detail")[:5000]
            )
            return summarize_issues(rows)

        try:
            data = await sync_to_async(_read, thread_sensitive=True)()
        except Exception as exc:  # noqa: BLE001
            logger.warning("checkinFieldIssues failed tenant=%s: %s", tenant_pk, exc)
            return []
        return [CheckinIssueSummary(**row) for row in data]
