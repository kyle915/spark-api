"""``POST /internal/cron/tag-recap-photos`` — runs ``tag_recap_photos``.
Guarded by ``X-Cron-Secret`` like every cron.

Dry run unless ``dry_run=false`` is passed. Params: dry_run, tenant (int),
max_calls (int), max_seconds (int), max_files (int).
"""

from __future__ import annotations

import io
import logging

from django.core.management import call_command
from django.http import HttpRequest, HttpResponse, JsonResponse
from django.views import View

from digest.cron_views import _check_secret

logger = logging.getLogger(__name__)


class TagRecapPhotosView(View):
    def post(self, request: HttpRequest) -> HttpResponse:
        deny = _check_secret(request)
        if deny is not None:
            return deny

        def _param(name: str) -> str:
            return (request.GET.get(name) or request.POST.get(name) or "").strip()

        apply = _param("dry_run").lower() in ("0", "false", "no", "off")
        args: list[str] = ["--apply"] if apply else []
        for name, flag in (
            ("tenant", "--tenant"),
            ("max_calls", "--max-calls"),
            ("max_seconds", "--max-seconds"),
            ("max_files", "--max-files"),
        ):
            value = _param(name)
            if value.isdigit():
                args += [flag, value]
        out = io.StringIO()
        try:
            call_command("tag_recap_photos", *args, stdout=out, stderr=out)
        except Exception as exc:  # noqa: BLE001
            logger.exception("tag-recap-photos cron failed")
            return JsonResponse(
                {"ok": False, "error": "command-failed", "detail": str(exc), "log": out.getvalue()},
                status=500,
            )
        return JsonResponse({"ok": True, "dry_run": not apply, "log": out.getvalue()})

    def get(self, request: HttpRequest) -> HttpResponse:
        deny = _check_secret(request)
        if deny is not None:
            return deny
        return JsonResponse({"ok": True, "endpoint": "tag-recap-photos"})
