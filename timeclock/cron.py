"""Secret-gated ``/internal/cron/clock-links`` — runs ``clock_links``.

Scheduled every few minutes with ``action=sync&apply=true`` to retry any
session the Sheet missed; dispatched by hand for setup and cleanup.
"""

from __future__ import annotations

import io
import logging

from django.core.management import call_command
from django.core.management.base import CommandError
from django.http import HttpRequest, HttpResponse, JsonResponse
from django.utils.decorators import method_decorator
from django.views import View
from django.views.decorators.csrf import csrf_exempt

from digest.cron_views import _check_secret

logger = logging.getLogger(__name__)

_STRING_ARGS = ("action", "label", "tenant_slug", "sheet_url", "share_with", "session_uuid", "rows")


@method_decorator(csrf_exempt, name="dispatch")
class ClockLinksCronView(View):
    def post(self, request: HttpRequest) -> HttpResponse:
        deny = _check_secret(request)
        if deny is not None:
            return deny

        def _get(name: str) -> str:
            return (request.GET.get(name) or request.POST.get(name) or "").strip()

        kwargs = {name: _get(name) for name in _STRING_ARGS if _get(name)}
        if "rows" in kwargs:
            try:
                kwargs["rows"] = int(kwargs["rows"])
            except ValueError:
                return JsonResponse({"ok": False, "error": "bad-rows"}, status=400)
        kwargs["apply"] = _get("apply").lower() in ("1", "true", "yes", "on")
        out = io.StringIO()
        try:
            call_command("clock_links", stdout=out, **kwargs)
        except CommandError as exc:
            return JsonResponse({"ok": False, "error": str(exc), "log": out.getvalue()}, status=400)
        except Exception as exc:  # noqa: BLE001 — surface to the workflow log
            logger.exception("clock-links cron failed")
            return JsonResponse(
                {"ok": False, "error": type(exc).__name__, "detail": str(exc)[:500], "log": out.getvalue()},
                status=500,
            )
        return JsonResponse({"ok": True, "log": out.getvalue()})
