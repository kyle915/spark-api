"""
HTTP entry points the scheduler hits to fire scheduled jobs.

This module exposes the admin digest as a POST endpoint so a cron
runner (GitHub Actions, Cloud Scheduler, Render Cron, etc.) can
trigger it on a wall-clock schedule. The endpoint runs the existing
`send_admin_digest` management command synchronously — for a tenant
of any reasonable size this returns in well under 30 seconds.

Security posture:
  - `X-Cron-Secret` HTTP header must match `settings.INTERNAL_CRON_SECRET`
  - Unauthenticated otherwise (no Django auth — the secret is the only
    barrier). The endpoint URL is also obfuscated (`/internal/cron/…`)
    to keep it off opportunistic-scanner radar, but the secret check
    is the real guard.
  - Returns 401 on bad/missing secret, 503 if the secret env var
    isn't configured (fail-closed rather than fail-open).

Why not just expose it via Cloud Scheduler → OIDC auth? That works
but adds GCP-specific plumbing the user would have to wire per
environment. A simple header-secret keeps the option open to host
on any cron runner with a curl-shaped trigger.
"""

from __future__ import annotations

import io
import logging
from typing import Any

from django.conf import settings
from django.core.management import call_command
from django.http import HttpRequest, HttpResponse, JsonResponse
from django.utils.decorators import method_decorator
from django.views import View
from django.views.decorators.csrf import csrf_exempt

logger = logging.getLogger(__name__)


def _check_secret(request: HttpRequest) -> JsonResponse | None:
    """Return a JsonResponse (401/503) if the request fails secret
    validation; None when the caller may proceed.
    """
    expected = getattr(settings, "INTERNAL_CRON_SECRET", None)
    if not expected:
        # Fail closed if the env var isn't set: better to alert via a
        # 503 than to ship an endpoint that runs jobs for anyone.
        logger.error(
            "INTERNAL_CRON_SECRET is not configured — refusing cron call."
        )
        return JsonResponse(
            {"ok": False, "error": "internal-cron-secret-not-configured"},
            status=503,
        )
    provided = request.headers.get("X-Cron-Secret", "")
    # Constant-time compare to dodge timing-side-channel attacks.
    import hmac as _hmac
    if not _hmac.compare_digest(str(provided), str(expected)):
        return JsonResponse(
            {"ok": False, "error": "unauthorized"}, status=401
        )
    return None


@method_decorator(csrf_exempt, name="dispatch")
class SendAdminDigestView(View):
    """POST `/internal/cron/send-admin-digest`.

    Body / query params (all optional):
      - window: "daily" (default) or "weekly"
      - skip_empty: "1" / "true" / "yes" (default ON) — don't email
        "all clear" digests
      - dry_run: "1" / "true" / "yes" — log the plan but don't send
      - tenant_id: int — restrict to a single tenant

    Mirrors the `send_admin_digest` management command's flags so the
    same logic powers both surfaces.
    """

    def post(self, request: HttpRequest) -> HttpResponse:
        deny = _check_secret(request)
        if deny is not None:
            return deny

        def _bool(name: str, default: bool = False) -> bool:
            raw = (request.GET.get(name) or request.POST.get(name) or "").lower()
            if not raw:
                return default
            return raw in ("1", "true", "yes", "on")

        window = (
            request.GET.get("window")
            or request.POST.get("window")
            or "daily"
        )
        if window not in ("daily", "weekly"):
            return JsonResponse(
                {"ok": False, "error": "window must be 'daily' or 'weekly'"},
                status=400,
            )
        # skip_empty defaults ON — scheduled runs that produce nothing
        # interesting shouldn't spam admins with "all clear" emails.
        skip_empty = _bool("skip_empty", default=True)
        dry_run = _bool("dry_run", default=False)
        tenant_id = request.GET.get("tenant_id") or request.POST.get("tenant_id")

        cmd_args: list[str] = ["--window", window]
        if skip_empty:
            cmd_args.append("--skip-empty")
        if dry_run:
            cmd_args.append("--dry-run")
        if tenant_id:
            cmd_args.extend(["--tenant-id", str(tenant_id)])

        # Capture stdout so the response body shows the per-tenant
        # summary the cron caller can pipe into logs.
        out = io.StringIO()
        try:
            call_command("send_admin_digest", *cmd_args, stdout=out)
        except Exception as exc:  # noqa: BLE001 — surface any error to caller
            logger.exception("Admin digest cron failed")
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
                "window": window,
                "dry_run": dry_run,
                "skip_empty": skip_empty,
                "log": out.getvalue(),
            }
        )

    def get(self, request: HttpRequest) -> HttpResponse:
        # GET responds with a benign liveness check (still secret-gated).
        # Useful for confirming the URL is mounted without running the
        # command.
        deny = _check_secret(request)
        if deny is not None:
            return deny
        return JsonResponse({"ok": True, "endpoint": "send-admin-digest"})


@method_decorator(csrf_exempt, name="dispatch")
class SendExecutiveSummaryView(View):
    """POST `/internal/cron/send-executive-summary`.

    Same secret-gating + dry_run / skip_empty flags as the daily
    digest endpoint; fires the `send_executive_summary` management
    command. Designed for the weekly GHA cron (Monday morning).

    Body / query params (all optional):
      - days: int (default 7)
      - skip_empty: "1" / "true" / "yes" (default ON)
      - dry_run: "1" / "true" / "yes"
      - tenant_id: int — restrict to a single tenant
    """

    def post(self, request: HttpRequest) -> HttpResponse:
        deny = _check_secret(request)
        if deny is not None:
            return deny

        def _bool(name: str, default: bool = False) -> bool:
            raw = (request.GET.get(name) or request.POST.get(name) or "").lower()
            if not raw:
                return default
            return raw in ("1", "true", "yes", "on")

        try:
            days = int(
                request.GET.get("days") or request.POST.get("days") or 7
            )
        except ValueError:
            return JsonResponse(
                {"ok": False, "error": "days must be an integer"}, status=400
            )

        skip_empty = _bool("skip_empty", default=True)
        dry_run = _bool("dry_run", default=False)
        tenant_id = request.GET.get("tenant_id") or request.POST.get(
            "tenant_id"
        )

        cmd_args: list[str] = ["--days", str(days)]
        if skip_empty:
            cmd_args.append("--skip-empty")
        if dry_run:
            cmd_args.append("--dry-run")
        if tenant_id:
            cmd_args.extend(["--tenant-id", str(tenant_id)])

        out = io.StringIO()
        try:
            call_command("send_executive_summary", *cmd_args, stdout=out)
        except Exception as exc:  # noqa: BLE001 — surface to caller
            logger.exception("Executive summary cron failed")
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
                "days": days,
                "dry_run": dry_run,
                "skip_empty": skip_empty,
                "log": out.getvalue(),
            }
        )

    def get(self, request: HttpRequest) -> HttpResponse:
        deny = _check_secret(request)
        if deny is not None:
            return deny
        return JsonResponse({"ok": True, "endpoint": "send-executive-summary"})


def _registered_views() -> dict[str, Any]:
    """Map URL path → view class. Lets `digest/urls.py` mount these
    without each one being re-exported explicitly.
    """
    return {
        "send-admin-digest": SendAdminDigestView,
        "send-executive-summary": SendExecutiveSummaryView,
    }
