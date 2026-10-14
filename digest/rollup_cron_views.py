"""Cron endpoints for the executive daily retail roll-up.

* ``/internal/cron/send-daily-retail-rollup`` — runs ``send_daily_retail_rollup``.
  Params: dry_run, tenant, date (YYYY-MM-DD), preview_to, not_before /
  not_after (local hour window), include_html. With
  ``include_html`` (requires tenant) the rendered email is returned as
  ``html`` so the workflow can save it as an artifact.
* ``/internal/cron/set-daily-retail-rollup`` — runs ``set_daily_retail_rollup``.
  Params: tenant, enabled, recipients, timezone, apply (dry-run unless true).

Both are secret-gated by ``X-Cron-Secret`` like every other cron view.
"""

from __future__ import annotations

import io
import logging
import os
import tempfile

from django.core.management import call_command
from django.http import HttpRequest, HttpResponse, JsonResponse
from django.utils.decorators import method_decorator
from django.views import View
from django.views.decorators.csrf import csrf_exempt

from digest.cron_views import _check_secret, _concise_exc

logger = logging.getLogger(__name__)


def _param(request: HttpRequest, name: str) -> str:
    return (request.GET.get(name) or request.POST.get(name) or "").strip()


def _flag(request: HttpRequest, name: str, default: bool = False) -> bool:
    raw = _param(request, name).lower()
    return raw in ("1", "true", "yes", "on") if raw else default


@method_decorator(csrf_exempt, name="dispatch")
class SendDailyRetailRollupView(View):
    def post(self, request: HttpRequest) -> HttpResponse:
        deny = _check_secret(request)
        if deny is not None:
            return deny
        tenant = _param(request, "tenant")
        preview_to = _param(request, "preview_to")
        include_html = _flag(request, "include_html")
        if (preview_to or include_html) and not tenant:
            return JsonResponse(
                {"ok": False, "error": "preview_to / include_html require tenant"}, status=400
            )
        dry_run = _flag(request, "dry_run")

        args: list[str] = []
        if dry_run:
            args.append("--dry-run")
        if tenant:
            args += ["--tenant", tenant]
        if _param(request, "date"):
            args += ["--date", _param(request, "date")]
        if preview_to:
            args += ["--preview-to", preview_to]
        for name in ("not_before", "not_after"):
            if _param(request, name).isdigit():
                args += [f"--{name.replace('_', '-')}", _param(request, name)]
        html_path = ""
        if include_html:
            fd, html_path = tempfile.mkstemp(suffix=".html")
            os.close(fd)
            args += ["--html-file", html_path]

        out = io.StringIO()
        try:
            call_command("send_daily_retail_rollup", *args, stdout=out)
        except Exception as exc:  # noqa: BLE001 — surface to caller
            logger.exception("Daily retail roll-up cron failed")
            return JsonResponse(
                {"ok": False, "error": "command-failed", "detail": _concise_exc(exc), "log": out.getvalue()},
                status=500,
            )
        finally:
            html = ""
            if html_path:
                with open(html_path, encoding="utf-8") as fh:
                    html = fh.read()
                os.unlink(html_path)
        body = {"ok": True, "dry_run": dry_run, "log": out.getvalue()}
        if include_html:
            body["html"] = html
        return JsonResponse(body)


@method_decorator(csrf_exempt, name="dispatch")
class SetDailyRetailRollupView(View):
    def post(self, request: HttpRequest) -> HttpResponse:
        deny = _check_secret(request)
        if deny is not None:
            return deny
        tenant = _param(request, "tenant")
        if not tenant:
            return JsonResponse({"ok": False, "error": "tenant is required"}, status=400)
        apply = _flag(request, "apply")
        args = ["--tenant", tenant]
        for name in ("enabled", "recipients", "timezone"):
            if _param(request, name):
                args += [f"--{name}", _param(request, name)]
        if apply:
            args.append("--apply")
        out = io.StringIO()
        try:
            call_command("set_daily_retail_rollup", *args, stdout=out)
        except Exception as exc:  # noqa: BLE001 — surface to caller
            return JsonResponse(
                {"ok": False, "error": "command-failed", "detail": _concise_exc(exc), "log": out.getvalue()},
                status=400,
            )
        return JsonResponse({"ok": True, "applied": apply, "log": out.getvalue()})


ROLLUP_VIEWS = {
    "send-daily-retail-rollup": SendDailyRetailRollupView,
    "set-daily-retail-rollup": SetDailyRetailRollupView,
}
