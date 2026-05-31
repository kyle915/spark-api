"""Cloud Tasks handler endpoints (no JWT, shared-secret protected).

These power the feature-flagged async path for the slow part of recap
approval. When Cloud Tasks is configured, `recaps.mutations.approve_recap` /
`approve_custom_recap` enqueue a task instead of running the client/RMM
notification + PDF generation inline; the Cloud Tasks service then POSTs here
and we run the same notify work in the background.

Security posture (mirrors `digest/cron_views.py`):
  - The request must carry `X-Tasks-Secret` matching `settings.CLOUD_TASKS_SECRET`.
  - Compared with `hmac.compare_digest` (constant-time, no timing side channel).
  - FAIL CLOSED: if the secret env var is unset/empty, OR the header is missing
    or doesn't match, we return 403. An unconfigured deployment can never have
    this endpoint do work — only Cloud Tasks holding the secret can.
  - CSRF-exempt: it's a server-to-server call authenticated by the secret, not
    a browser session.

This endpoint sends emails. To avoid Cloud Tasks retrying and re-sending
duplicate emails, we ALWAYS return HTTP 200 once we've *attempted* the notify
(any error is caught + logged here). The underlying notify is already
best-effort and dedupes recipients, so a single attempt is the right contract.
No model is read or written beyond loading the recap to notify on.
"""

from __future__ import annotations

import hmac
import json
import logging

from django.conf import settings
from django.http import (
    HttpRequest,
    HttpResponse,
    HttpResponseForbidden,
    JsonResponse,
)
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods

logger = logging.getLogger(__name__)


def _secret_ok(request: HttpRequest) -> bool:
    """Constant-time check of the X-Tasks-Secret header. Fails closed.

    Returns False when the secret isn't configured (so the endpoint refuses to
    do anything in an environment where the feature is off), or when the
    provided header is missing/empty/mismatched.
    """
    expected = getattr(settings, "CLOUD_TASKS_SECRET", "") or ""
    if not expected:
        # Fail closed: an unconfigured secret means the feature is off and this
        # email-sending endpoint must reject everything.
        logger.error(
            "CLOUD_TASKS_SECRET is not configured — refusing tasks call."
        )
        return False
    provided = request.headers.get("X-Tasks-Secret", "") or ""
    return hmac.compare_digest(str(provided), str(expected))


@csrf_exempt
@require_http_methods(["POST"])
async def recap_approved_notify_view(request: HttpRequest) -> HttpResponse:
    """POST `/api/tasks/recap-approved-notify`.

    Body JSON: {"recap_id": int, "recap_kind": "legacy" | "custom"}.

    Runs `_notify_recap_approved_to_rmm_or_clients` for the recap — the same
    client/RMM email + PDF work the inline approval path runs. Returns 403
    without the correct shared secret; otherwise always 200 after attempting
    the notify (so Cloud Tasks doesn't retry and double-send).
    """
    if not _secret_ok(request):
        return HttpResponseForbidden("Forbidden")

    # Imported lazily so the URLConf/handler stays importable even if recaps'
    # mutation module has heavier import-time costs, and to keep this module
    # dependency-light.
    from asgiref.sync import sync_to_async

    from recaps import models
    from recaps.mutations import _notify_recap_approved_to_rmm_or_clients

    try:
        body = json.loads((request.body or b"").decode("utf-8") or "{}")
    except (ValueError, UnicodeDecodeError):
        # Malformed payloads aren't retryable — 200 so Cloud Tasks drops it.
        logger.warning("recap-approved-notify: could not parse request body.")
        return JsonResponse({"ok": False, "error": "bad-json"}, status=200)

    recap_id = body.get("recap_id")
    recap_kind = body.get("recap_kind")
    if not isinstance(recap_id, int) or recap_kind not in ("legacy", "custom"):
        logger.warning(
            "recap-approved-notify: bad payload recap_id=%r recap_kind=%r",
            recap_id,
            recap_kind,
        )
        return JsonResponse({"ok": False, "error": "bad-payload"}, status=200)

    model = models.CustomRecap if recap_kind == "custom" else models.Recap

    try:
        # Same select_related the approve flow re-fetches with so the notify
        # (and PDF) work hits no extra queries.
        recap = await sync_to_async(
            model.objects.select_related(
                "event",
                "event__tenant",
                "event__rmm_asigned",
                "event__timezone",
                "job",
                "retailer",
                "timezone",
                "ambassador",
                "ambassador__user",
            ).get
        )(id=recap_id)
    except model.DoesNotExist:
        # The recap vanished between enqueue and handling — nothing to do, and
        # retrying won't help. 200 so the task is acked and not re-sent.
        logger.warning(
            "recap-approved-notify: %s id=%s not found.", recap_kind, recap_id
        )
        return JsonResponse({"ok": False, "error": "not-found"}, status=200)

    try:
        await _notify_recap_approved_to_rmm_or_clients(recap)
    except Exception:  # noqa: BLE001 — best-effort; never trigger a Cloud Tasks retry
        logger.exception(
            "recap-approved-notify failed for %s recap=%s",
            recap_kind,
            recap_id,
        )

    # Always 200 after attempting the (best-effort, deduped) notify so Cloud
    # Tasks acks the task and does not retry / re-send duplicate emails.
    return JsonResponse({"ok": True})
