"""POST /internal/cron/abuse-usage-watch — daily suspicious-usage detector.

Secret-gated with ``X-Cron-Secret`` (same check as digest/cron_views.py).

Query params:
  - window_hours (default 24)
  - notify: "true" to email the Ignite team when something is flagged.
    Default OFF, and always off when dry_run=true.
  - dry_run: "true" → compute + return the report only (no email, no pruning).

Rate-limit counters older than 48h are pruned on non-dry runs (they're
ephemeral window counters). Usage / audit / BA-read rows are kept.
"""

from __future__ import annotations

import logging

from django.http import HttpRequest, JsonResponse
from django.utils.decorators import method_decorator
from django.views import View
from django.views.decorators.csrf import csrf_exempt

from digest.cron_views import _check_secret, _concise_exc

from . import detection

logger = logging.getLogger("spark.abuse")


def _flag(request: HttpRequest, name: str, default: bool = False) -> bool:
    raw = request.GET.get(name, request.POST.get(name))
    if raw is None:
        return default
    return str(raw).strip().lower() in ("1", "true", "yes", "on")


@method_decorator(csrf_exempt, name="dispatch")
class AbuseUsageWatchView(View):
    def post(self, request: HttpRequest):
        deny = _check_secret(request)
        if deny is not None:
            return deny
        dry_run = _flag(request, "dry_run")
        notify = _flag(request, "notify") and not dry_run
        try:
            window_hours = max(1, min(int(request.GET.get("window_hours", 24)), 24 * 14))
        except ValueError:
            window_hours = 24
        try:
            findings = detection.detect(window_hours=window_hours)
            emailed = detection.send_report(findings, window_hours) if notify else 0
            pruned = 0 if dry_run else detection.prune_counters()
        except Exception as exc:  # noqa: BLE001
            logger.exception("abuse usage watch failed")
            return JsonResponse({"ok": False, "error": _concise_exc(exc)}, status=500)
        return JsonResponse({
            "ok": True,
            "dry_run": dry_run,
            "notify": notify,
            "window_hours": window_hours,
            "flagged": len(findings),
            "emailed_recipients": emailed,
            "pruned_counters": pruned,
            "findings": [f.as_dict() for f in findings],
        })
