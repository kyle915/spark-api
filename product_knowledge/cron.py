"""Cron endpoints for BA product knowledge (``X-Cron-Secret`` gated).

* ``/internal/cron/refresh-product-knowledge`` — ``action=list`` returns the
  brand ids to refresh; ``action=refresh&tenant=<id>[&scope=…][&dry_run=1]``
  refreshes one brand. The weekly workflow lists, then refreshes one brand per
  request so a slow site can't time the batch out.
* ``/internal/cron/set-brand-websites`` — ``map`` = JSON object of tenant
  id/slug/name → website; dry run unless ``apply=true``.
"""

from __future__ import annotations

import io
import json
import logging

from django.core.management import call_command
from django.http import HttpRequest, HttpResponse, JsonResponse
from django.views import View

from digest.cron_views import _check_secret, _concise_exc

logger = logging.getLogger(__name__)


def _param(request: HttpRequest, name: str) -> str:
    return (request.GET.get(name) or request.POST.get(name) or "").strip()


def _flag(request: HttpRequest, name: str) -> bool:
    return _param(request, name).lower() in ("1", "true", "yes", "on")


class RefreshProductKnowledgeView(View):
    def post(self, request: HttpRequest) -> HttpResponse:
        deny = _check_secret(request)
        if deny is not None:
            return deny
        action = _param(request, "action") or "refresh"
        if action == "list":
            from product_knowledge.refresh import tenants_to_refresh

            return JsonResponse({"ok": True, "tenants": tenants_to_refresh()})
        if action != "refresh":
            return JsonResponse({"ok": False, "error": "invalid-action"}, status=400)
        tenant = _param(request, "tenant")
        if not tenant.isdigit():
            return JsonResponse({"ok": False, "error": "tenant-required"}, status=400)
        scope = _param(request, "scope") or "all"
        args = ["--tenant", tenant, "--scope", scope]
        if _flag(request, "dry_run"):
            args.append("--dry-run")
        out = io.StringIO()
        try:
            call_command("refresh_product_knowledge", *args, stdout=out)
        except Exception as exc:  # noqa: BLE001
            logger.exception("refresh-product-knowledge failed tenant=%s", tenant)
            return JsonResponse(
                {
                    "ok": False,
                    "error": "command-failed",
                    "detail": _concise_exc(exc),
                    "log": out.getvalue(),
                },
                status=500,
            )
        return JsonResponse({"ok": True, "log": out.getvalue()})

    def get(self, request: HttpRequest) -> HttpResponse:
        deny = _check_secret(request)
        if deny is not None:
            return deny
        return JsonResponse({"ok": True, "endpoint": "refresh-product-knowledge"})


class SetBrandWebsitesView(View):
    def post(self, request: HttpRequest) -> HttpResponse:
        deny = _check_secret(request)
        if deny is not None:
            return deny
        raw = _param(request, "map")
        try:
            mapping = json.loads(raw or "{}")
        except ValueError:
            return JsonResponse({"ok": False, "error": "invalid-json"}, status=400)
        if not isinstance(mapping, dict) or not mapping:
            return JsonResponse({"ok": False, "error": "map-required"}, status=400)
        from product_knowledge.management.commands.set_brand_websites import apply_map

        apply = _flag(request, "apply")
        rows = apply_map(mapping, apply=apply)
        return JsonResponse({"ok": True, "dry_run": not apply, "rows": rows})
