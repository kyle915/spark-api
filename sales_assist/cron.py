"""``/internal/cron/sales-assist`` (``X-Cron-Secret`` gated).

``action``:

* ``renewals`` — quarterly "what we did for you" drafts for linked clients.
  ``mode=quarterly`` (every active link, last complete quarter) or ``due``
  (links whose renewal_date is within SALES_ASSIST_RENEWAL_LEAD_DAYS).
  Optional ``tenant``, ``start``/``end`` (YYYY-MM-DD).
* ``prospects`` — research + proposal drafts for leads created in Close
  since the last run (optional ``lead`` = one Close lead id, on demand;
  ``since`` = ISO datetime override).
* ``suggest-links`` — fuzzy tenant -> Close lead suggestions. Read-only.
* ``link`` — ``map`` = JSON {"<tenant id>": "lead_..."}; dry run unless
  ``apply=true``.
* ``status`` — config + recent runs. Never shows the key.

Every action is a dry run unless ``apply=true`` AND SALES_ASSIST_ENABLED is
on AND CLOSE_API_KEY is set. Nothing ever sends an email or a text.
"""

from __future__ import annotations

import json
import logging
from datetime import date

from django.conf import settings
from django.http import HttpRequest, HttpResponse, JsonResponse
from django.views import View

from digest.cron_views import _check_secret, _concise_exc

logger = logging.getLogger(__name__)


def _param(request: HttpRequest, name: str) -> str:
    return (request.GET.get(name) or request.POST.get(name) or "").strip()


def _flag(request: HttpRequest, name: str) -> bool:
    return _param(request, name).lower() in ("1", "true", "yes", "on")


def _date(raw: str) -> date | None:
    try:
        return date.fromisoformat(raw) if raw else None
    except ValueError:
        return None


def _status() -> dict:
    from sales_assist.models import CloseLink, SalesAssistCursor, SalesAssistRun

    return {
        "close_configured": bool((getattr(settings, "CLOSE_API_KEY", "") or "").strip()),
        "openai_configured": bool((getattr(settings, "OPENAI_API_KEY", "") or "").strip()),
        "enabled": bool(getattr(settings, "SALES_ASSIST_ENABLED", False)),
        "active_links": CloseLink.objects.filter(active=True).count(),
        "prospect_cursor": SalesAssistCursor.objects.filter(name="prospects.since").values_list("value", flat=True).first() or "",
        "recent_runs": [
            {"id": r.id, "action": r.action, "applied": r.applied, "ai_calls": r.ai_calls,
             "est_cost_usd": float(r.est_cost_usd), "started_at": r.started_at.isoformat()}
            for r in SalesAssistRun.objects.order_by("-id")[:10]
        ],
    }


class SalesAssistView(View):
    def get(self, request: HttpRequest) -> HttpResponse:
        deny = _check_secret(request)
        if deny is not None:
            return deny
        return JsonResponse({"ok": True, "endpoint": "sales-assist", **_status()})

    def post(self, request: HttpRequest) -> HttpResponse:
        deny = _check_secret(request)
        if deny is not None:
            return deny
        from sales_assist import runner

        action = _param(request, "action") or "status"
        apply = _flag(request, "apply")
        try:
            if action == "status":
                return JsonResponse({"ok": True, **_status()})
            if action == "renewals":
                tenant = _param(request, "tenant")
                result = runner.run_renewals(
                    apply=apply,
                    tenant_id=int(tenant) if tenant.isdigit() else None,
                    start=_date(_param(request, "start")),
                    end=_date(_param(request, "end")),
                    mode=_param(request, "mode") or "quarterly",
                )
                return JsonResponse({"ok": True, **result})
            if action == "prospects":
                result = runner.run_prospects(
                    apply=apply,
                    lead_id=_param(request, "lead") or None,
                    since=_param(request, "since") or None,
                )
                return JsonResponse({"ok": True, **result})
            if action == "suggest-links":
                return JsonResponse(runner.suggest_links())
            if action == "link":
                try:
                    mapping = json.loads(_param(request, "map") or "{}")
                except ValueError:
                    return JsonResponse({"ok": False, "error": "invalid-json"}, status=400)
                if not isinstance(mapping, dict) or not mapping:
                    return JsonResponse({"ok": False, "error": "map-required"}, status=400)
                rows = runner.apply_link_map(mapping, apply=apply)
                return JsonResponse({"ok": True, "dry_run": not apply, "rows": rows})
        except Exception as exc:  # noqa: BLE001
            logger.exception("sales-assist cron failed action=%s", action)
            return JsonResponse({"ok": False, "error": "failed", "detail": _concise_exc(exc)}, status=500)
        return JsonResponse({"ok": False, "error": "invalid-action"}, status=400)
