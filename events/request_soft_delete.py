"""Soft-delete a request: the one cancel path the tracker has.

Requests have no "cancelled" status. Deleting sets ``deleted_at`` so the
request, its events, and its jobs drop off every list, detail page, export,
and the BA job board. The row stays, so activity, events, and recaps survive.
No emails go out.
"""

from __future__ import annotations

import logging

from django.utils import timezone

from events import models
from jobs.models import Job

logger = logging.getLogger(__name__)


def soft_delete_request(request: models.Request, actor, *, summary: str = "Request deleted") -> None:
    request.deleted_at = timezone.now()
    request.updated_by = actor if getattr(actor, "id", None) else None
    request.save(update_fields=["deleted_at", "updated_by", "updated_at"])
    try:
        Job.objects.filter(event__request_id=request.id, closed=False).update(
            closed=True, ongoing=False
        )
    except Exception:
        logger.exception("Closing jobs failed for deleted request %s", request.id)
    try:
        models.RequestActivityLog.objects.create(
            tenant=request.tenant,
            request=request,
            kind=models.RequestActivityLog.KIND_UPDATED,
            actor_user=actor if getattr(actor, "id", None) else None,
            summary=summary[:512],
            metadata={"deleted": True},
        )
    except Exception:
        logger.exception("Activity log failed for deleted request %s", request.id)
