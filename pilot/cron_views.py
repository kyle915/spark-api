"""Internal cron HTTP endpoints for automated POS ingest + buyer digests.

Mounted under `/internal/cron/` via ``digest.urls`` (PILOT_CRON_VIEWS).
Secret-gated with the same ``X-Cron-Secret`` as other digest crons.
"""

from __future__ import annotations

import io
import logging

from django.core.management import call_command
from django.http import HttpRequest, HttpResponse, JsonResponse
from django.views import View
from django.views.decorators.csrf import csrf_exempt
from django.utils.decorators import method_decorator

logger = logging.getLogger(__name__)


def _check_secret(request: HttpRequest) -> HttpResponse | None:
    # Reuse digest's secret checker so auth stays identical.
    from digest.cron_views import _check_secret as digest_check

    return digest_check(request)


@method_decorator(csrf_exempt, name="dispatch")
class SendWeeklyBuyerDigestView(View):
    """POST `/internal/cron/send-weekly-buyer-digest`."""

    def post(self, request: HttpRequest) -> HttpResponse:
        deny = _check_secret(request)
        if deny is not None:
            return deny

        def _bool(name: str, default: bool = False) -> bool:
            raw = (request.GET.get(name) or request.POST.get(name) or "").lower()
            return raw in ("1", "true", "yes", "on") if raw else default

        only_email = (
            request.GET.get("only_email") or request.POST.get("only_email") or ""
        ).strip()
        dry_run = _bool("dry_run", default=bool(only_email))
        force = _bool("force", default=False)
        tenant = request.GET.get("tenant") or request.POST.get("tenant")
        period_start = (
            request.GET.get("period_start") or request.POST.get("period_start") or ""
        ).strip()
        period_end = (
            request.GET.get("period_end") or request.POST.get("period_end") or ""
        ).strip()
        if tenant:
            try:
                int(tenant)
            except ValueError:
                return JsonResponse(
                    {"ok": False, "error": "tenant must be an integer"},
                    status=400,
                )

        cmd_args: list[str] = []
        if dry_run:
            cmd_args.append("--dry-run")
        if force:
            cmd_args.append("--force")
        if tenant:
            cmd_args.extend(["--tenant", str(tenant)])
        if only_email:
            if not tenant:
                return JsonResponse(
                    {"ok": False, "error": "only_email requires tenant"},
                    status=400,
                )
            cmd_args.extend(["--only-email", only_email])
        if period_start:
            cmd_args.extend(["--period-start", period_start])
        if period_end:
            cmd_args.extend(["--period-end", period_end])

        out = io.StringIO()
        try:
            call_command("send_weekly_buyer_digest", *cmd_args, stdout=out)
        except Exception as exc:  # noqa: BLE001
            logger.exception("Weekly buyer digest cron failed")
            return JsonResponse(
                {
                    "ok": False,
                    "error": "command-failed",
                    "detail": str(exc),
                    "log": out.getvalue(),
                },
                status=500,
            )
        return JsonResponse(
            {
                "ok": True,
                "dry_run": dry_run,
                "force": force,
                "tenant": tenant,
                "only_email": only_email or None,
                "log": out.getvalue(),
            }
        )

    def get(self, request: HttpRequest) -> HttpResponse:
        deny = _check_secret(request)
        if deny is not None:
            return deny
        return JsonResponse({"ok": True, "endpoint": "send-weekly-buyer-digest"})


@method_decorator(csrf_exempt, name="dispatch")
class ProcessPosFeedDropsView(View):
    """POST `/internal/cron/process-pos-feed-drops`."""

    def post(self, request: HttpRequest) -> HttpResponse:
        deny = _check_secret(request)
        if deny is not None:
            return deny

        tenant = request.GET.get("tenant") or request.POST.get("tenant")
        limit = request.GET.get("limit") or request.POST.get("limit") or "50"
        if tenant:
            try:
                int(tenant)
            except ValueError:
                return JsonResponse(
                    {"ok": False, "error": "tenant must be an integer"},
                    status=400,
                )
        try:
            int(limit)
        except ValueError:
            return JsonResponse(
                {"ok": False, "error": "limit must be an integer"},
                status=400,
            )

        cmd_args: list[str] = ["--limit", str(limit)]
        if tenant:
            cmd_args.extend(["--tenant", str(tenant)])

        out = io.StringIO()
        try:
            call_command("process_pos_feed_drops", *cmd_args, stdout=out)
        except Exception as exc:  # noqa: BLE001
            logger.exception("Process POS feed drops cron failed")
            return JsonResponse(
                {
                    "ok": False,
                    "error": "command-failed",
                    "detail": str(exc),
                    "log": out.getvalue(),
                },
                status=500,
            )
        return JsonResponse(
            {
                "ok": True,
                "tenant": tenant,
                "limit": int(limit),
                "log": out.getvalue(),
            }
        )

    def get(self, request: HttpRequest) -> HttpResponse:
        deny = _check_secret(request)
        if deny is not None:
            return deny
        return JsonResponse({"ok": True, "endpoint": "process-pos-feed-drops"})


@method_decorator(csrf_exempt, name="dispatch")
class SendSavedViewDigestsView(View):
    """POST `/internal/cron/send-saved-view-digests`."""

    def post(self, request: HttpRequest) -> HttpResponse:
        deny = _check_secret(request)
        if deny is not None:
            return deny

        dry_run = (request.GET.get("dry_run") or request.POST.get("dry_run") or "").lower() in (
            "1",
            "true",
            "yes",
        )
        force = (request.GET.get("force") or request.POST.get("force") or "").lower() in (
            "1",
            "true",
            "yes",
        )
        tenant = request.GET.get("tenant") or request.POST.get("tenant")
        view_uuid = (request.GET.get("view_uuid") or request.POST.get("view_uuid") or "").strip()

        cmd_args: list[str] = []
        if dry_run:
            cmd_args.append("--dry-run")
        if force:
            cmd_args.append("--force")
        if tenant:
            try:
                int(tenant)
            except ValueError:
                return JsonResponse({"ok": False, "error": "tenant must be an integer"}, status=400)
            cmd_args.extend(["--tenant", str(tenant)])
        if view_uuid:
            cmd_args.extend(["--view-uuid", view_uuid])

        out = io.StringIO()
        try:
            call_command("send_saved_view_digests", *cmd_args, stdout=out)
        except Exception as exc:  # noqa: BLE001
            logger.exception("Saved view digests cron failed")
            return JsonResponse(
                {"ok": False, "error": "command-failed", "detail": str(exc), "log": out.getvalue()},
                status=500,
            )
        return JsonResponse({"ok": True, "dry_run": dry_run, "force": force, "log": out.getvalue()})

    def get(self, request: HttpRequest) -> HttpResponse:
        deny = _check_secret(request)
        if deny is not None:
            return deny
        return JsonResponse({"ok": True, "endpoint": "send-saved-view-digests"})


PILOT_CRON_VIEWS = {
    "send-weekly-buyer-digest": SendWeeklyBuyerDigestView,
    "process-pos-feed-drops": ProcessPosFeedDropsView,
    "send-saved-view-digests": SendSavedViewDigestsView,
}
