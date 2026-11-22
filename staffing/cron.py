"""Cloud Scheduler-ready internal cron view for no-show backup cascades.

Mounted at ``/api/internal/cron/noshow-cascade`` (and ``/internal/cron/noshow-cascade``).
Guarded by ``X-Cron-Secret`` via :func:`digest.cron_views._check_secret`.
"""

from __future__ import annotations

import json
import logging

from django.http import HttpRequest, HttpResponse, JsonResponse
from django.utils.decorators import method_decorator
from django.views import View
from django.views.decorators.csrf import csrf_exempt

from digest.cron_views import _check_secret
from staffing.cascade import evaluate_noshow_cascades

logger = logging.getLogger(__name__)


def _read_param(request: HttpRequest, body: dict, name: str, default: str = "") -> str:
    raw = request.GET.get(name)
    if raw is not None and str(raw).strip():
        return str(raw).strip()
    if name in body and body[name] is not None:
        return str(body[name]).strip()
    raw_post = request.POST.get(name)
    if raw_post is not None and str(raw_post).strip():
        return str(raw_post).strip()
    return default


@method_decorator(csrf_exempt, name="dispatch")
class NoShowCascadeCronView(View):
    def post(self, request: HttpRequest) -> HttpResponse:
        deny = _check_secret(request)
        if deny is not None:
            return deny

        body: dict = {}
        if request.body:
            try:
                parsed = json.loads(request.body.decode("utf-8"))
                if isinstance(parsed, dict):
                    body = parsed
            except (ValueError, UnicodeDecodeError):
                body = {}

        def _int_param(name: str, default: int) -> int:
            raw = _read_param(request, body, name)
            if not raw:
                return default
            try:
                return max(1, int(raw))
            except ValueError:
                return default

        dry_run = _read_param(request, body, "dry_run").lower() in (
            "1",
            "true",
            "yes",
            "on",
        )
        try:
            summary = evaluate_noshow_cascades(
                checkin_grace_minutes=_int_param("checkin_grace_minutes", 15),
                unconfirmed_lead_minutes=_int_param("unconfirmed_lead_minutes", 120),
                lookback_hours=_int_param("lookback_hours", 6),
                backup_limit=_int_param("backup_limit", 5),
                dry_run=dry_run,
            )
        except Exception as exc:  # noqa: BLE001 — surface structured error to Cloud Scheduler
            logger.exception("noshow-cascade cron failed")
            return JsonResponse(
                {"ok": False, "error": type(exc).__name__, "detail": str(exc)[:300]},
                status=500,
            )

        return JsonResponse({"ok": True, **summary})
