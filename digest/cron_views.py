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
import traceback
from typing import Any

from django.conf import settings
from django.core.management import call_command
from django.http import HttpRequest, HttpResponse, JsonResponse
from django.utils.decorators import method_decorator
from django.views import View
from django.views.decorators.csrf import csrf_exempt

logger = logging.getLogger(__name__)


def _concise_exc(exc: BaseException) -> dict[str, str]:
    """Concise, log-safe error detail for a cron command that itself crashed:
    exception type + message + the LAST traceback frame (file:line in func).
    Returned in the JSON response so a re-run surfaces the real cause in the
    GitHub Actions log even without Cloud Run / GCP access. Deliberately does
    NOT include the full traceback or any env/secret."""
    message = " ".join(str(exc).split())
    if len(message) > 500:
        message = message[:497] + "..."
    frame = ""
    tb = exc.__traceback__
    if tb is not None:
        last = traceback.extract_tb(tb)[-1]
        frame = f"{last.filename.split('/')[-1]}:{last.lineno} in {last.name}"
    return {
        "type": type(exc).__name__,
        "message": message,
        "frame": frame,
    }


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


@method_decorator(csrf_exempt, name="dispatch")
class SendNewGigDigestView(View):
    """POST `/internal/cron/send-new-gig-digest`.

    Fires the `send_new_gig_digest` command — one digest push per BA of
    the gigs posted in the last N hours that match their preferences.

    Body / query params (all optional):
      - hours: int (default 24) — look-back window
      - dry_run: "1" / "true" / "yes"
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
            hours = int(
                request.GET.get("hours") or request.POST.get("hours") or 24
            )
        except ValueError:
            return JsonResponse(
                {"ok": False, "error": "hours must be an integer"}, status=400
            )

        dry_run = _bool("dry_run", default=False)
        cmd_args: list[str] = ["--hours", str(hours)]
        if dry_run:
            cmd_args.append("--dry-run")

        out = io.StringIO()
        try:
            call_command("send_new_gig_digest", *cmd_args, stdout=out)
        except Exception as exc:  # noqa: BLE001 — surface to caller
            logger.exception("New-gig digest cron failed")
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
            {"ok": True, "hours": hours, "dry_run": dry_run, "log": out.getvalue()}
        )

    def get(self, request: HttpRequest) -> HttpResponse:
        deny = _check_secret(request)
        if deny is not None:
            return deny
        return JsonResponse({"ok": True, "endpoint": "send-new-gig-digest"})


@method_decorator(csrf_exempt, name="dispatch")
class SendRecapRemindersView(View):
    """POST `/internal/cron/send-recap-reminders`.

    Fires the `send_recap_reminders` command — the aggressive daily sweep
    that re-nudges BAs with outstanding recaps for recently-ended shifts.

    Body / query params (all optional):
      - max_age_days: int (default 7)
      - grace_hours: int (default 2)
      - dry_run: "1" / "true" / "yes"
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

        def _int(name: str, default: int) -> int | None:
            raw = request.GET.get(name) or request.POST.get(name)
            if raw is None or raw == "":
                return default
            try:
                return int(raw)
            except ValueError:
                return None

        max_age_days = _int("max_age_days", 7)
        grace_hours = _int("grace_hours", 2)
        if max_age_days is None or grace_hours is None:
            return JsonResponse(
                {"ok": False, "error": "max_age_days/grace_hours must be integers"},
                status=400,
            )
        dry_run = _bool("dry_run", default=False)

        cmd_args: list[str] = [
            "--max-age-days", str(max_age_days),
            "--grace-hours", str(grace_hours),
        ]
        if dry_run:
            cmd_args.append("--dry-run")

        out = io.StringIO()
        try:
            call_command("send_recap_reminders", *cmd_args, stdout=out)
        except Exception as exc:  # noqa: BLE001 — surface to caller
            logger.exception("Recap reminders cron failed")
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
                "max_age_days": max_age_days,
                "grace_hours": grace_hours,
                "dry_run": dry_run,
                "log": out.getvalue(),
            }
        )

    def get(self, request: HttpRequest) -> HttpResponse:
        deny = _check_secret(request)
        if deny is not None:
            return deny
        return JsonResponse({"ok": True, "endpoint": "send-recap-reminders"})


@method_decorator(csrf_exempt, name="dispatch")
class SendPaymentNotificationsView(View):
    """POST `/internal/cron/send-payment-notifications`.

    Fires `notify_payments_sent` — polls Wingspan for newly-sent payments
    and pushes the BA a "you've been paid" notification (deduped).

    Body / query params (all optional):
      - limit: int (default 100) — max payments to pull
      - since_days: int (default 10) — suppress pushes for older dated payments
      - dry_run: "1" / "true" / "yes"
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

        def _int(name: str, default: int) -> int | None:
            raw = request.GET.get(name) or request.POST.get(name)
            if raw is None or raw == "":
                return default
            try:
                return int(raw)
            except ValueError:
                return None

        limit = _int("limit", 100)
        since_days = _int("since_days", 10)
        if limit is None or since_days is None:
            return JsonResponse(
                {"ok": False, "error": "limit/since_days must be integers"},
                status=400,
            )
        dry_run = _bool("dry_run", default=False)

        cmd_args: list[str] = [
            "--limit", str(limit),
            "--since-days", str(since_days),
        ]
        if dry_run:
            cmd_args.append("--dry-run")

        out = io.StringIO()
        try:
            call_command("notify_payments_sent", *cmd_args, stdout=out)
        except Exception as exc:  # noqa: BLE001 — surface to caller
            logger.exception("Payment notifications cron failed")
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
                "limit": limit,
                "since_days": since_days,
                "dry_run": dry_run,
                "log": out.getvalue(),
            }
        )

    def get(self, request: HttpRequest) -> HttpResponse:
        deny = _check_secret(request)
        if deny is not None:
            return deny
        return JsonResponse(
            {"ok": True, "endpoint": "send-payment-notifications"}
        )


@method_decorator(csrf_exempt, name="dispatch")
class SendDocumentExpiryRemindersView(View):
    """POST `/internal/cron/send-document-expiry-reminders`.

    Fires `send_document_expiry_reminders` — pushes each BA a reminder for
    documents expiring within N days, and marks already-expired docs.

    Body / query params (all optional):
      - days: int (default 14) — look-ahead window
      - dry_run: "1" / "true" / "yes"
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
            days = int(request.GET.get("days") or request.POST.get("days") or 14)
        except ValueError:
            return JsonResponse(
                {"ok": False, "error": "days must be an integer"}, status=400
            )
        dry_run = _bool("dry_run", default=False)

        cmd_args: list[str] = ["--days", str(days)]
        if dry_run:
            cmd_args.append("--dry-run")

        out = io.StringIO()
        try:
            call_command("send_document_expiry_reminders", *cmd_args, stdout=out)
        except Exception as exc:  # noqa: BLE001
            logger.exception("Document expiry reminders cron failed")
            return JsonResponse(
                {"ok": False, "error": "command-failed", "detail": str(exc),
                 "log": out.getvalue()},
                status=500,
            )

        return JsonResponse(
            {"ok": True, "days": days, "dry_run": dry_run, "log": out.getvalue()}
        )

    def get(self, request: HttpRequest) -> HttpResponse:
        deny = _check_secret(request)
        if deny is not None:
            return deny
        return JsonResponse({"ok": True, "endpoint": "send-document-expiry-reminders"})


@method_decorator(csrf_exempt, name="dispatch")
class SendScheduledClientReportsView(View):
    """POST `/internal/cron/send-scheduled-client-reports`.

    Fires `send_scheduled_client_reports` — generates each opted-in tenant's
    monthly client performance-report PDF and emails it to that tenant's
    client contacts. Designed for the MONTHLY GHA cron (1st of the month):
    with no args the command reports the prior COMPLETE calendar month, so a
    1st-of-month run covers the month that just ended.

    SAFE — OPT-IN OFF by default. The command only touches tenants with
    `scheduled_report_enabled=True` AND a non-empty recipient list; that flag
    defaults to False, so until Ignite flips a client on, a scheduled run
    emails NOBODY.

    Body / query params (all optional):
      - dry_run: "1" / "true" / "yes" — generate the PDF + log recipients,
        but send NO email.
      - tenant: int — restrict to a single tenant id (does NOT bypass the
        opt-in gate).
      - month: "YYYY-MM" — override the reporting month (default: prior
        complete month).
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

        dry_run = _bool("dry_run", default=False)

        tenant = request.GET.get("tenant") or request.POST.get("tenant")
        if tenant:
            try:
                int(tenant)
            except ValueError:
                return JsonResponse(
                    {"ok": False, "error": "tenant must be an integer"},
                    status=400,
                )

        month = request.GET.get("month") or request.POST.get("month")

        # No args -> command defaults to the prior complete month, which is
        # exactly what a 1st-of-month run wants.
        cmd_args: list[str] = []
        if dry_run:
            cmd_args.append("--dry-run")
        if tenant:
            cmd_args.extend(["--tenant", str(tenant)])
        if month:
            cmd_args.extend(["--month", str(month)])

        out = io.StringIO()
        try:
            call_command("send_scheduled_client_reports", *cmd_args, stdout=out)
        except Exception as exc:  # noqa: BLE001 — surface to caller
            logger.exception("Scheduled client reports cron failed")
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
                "tenant": tenant,
                "month": month,
                "log": out.getvalue(),
            }
        )

    def get(self, request: HttpRequest) -> HttpResponse:
        deny = _check_secret(request)
        if deny is not None:
            return deny
        return JsonResponse(
            {"ok": True, "endpoint": "send-scheduled-client-reports"}
        )


@method_decorator(csrf_exempt, name="dispatch")
class RepairApprovedEventStatusView(View):
    """POST `/internal/cron/repair-approved-event-status`.

    Fires the `repair_approved_event_status` backfill — sets internally-
    materialized Events to their tenant's approved EventStatus when the parent
    Request is approved/scheduled but the Event is still stuck on "pending"
    (see events/management/commands/repair_approved_event_status.py). Idempotent
    and transaction-wrapped per tenant.

    This is a MANUAL one-off (triggered from the GitHub Actions UI), not a
    recurring cron — but it reuses the same `X-Cron-Secret` gating as its
    siblings so the trigger needs no command line.

    SAFE — DRY-RUN IS THE DEFAULT. A plain trigger NEVER writes: the backfill
    runs with `dry_run=True` unless `execute=true` is explicitly passed, so the
    operator always sees the report first and opts in to writes deliberately.

    Body / query params (all optional):
      - execute: "1" / "true" / "yes" — perform the writes. Default OFF →
        the command runs in DRY-RUN with NO DB writes.
      - tenant: tenant slug or numeric id — restrict to a single tenant.
        Default: all tenants.

    The command's full stdout report (incl. the Liquid Death breakdown) is
    captured and returned verbatim in the response under `report`, so the
    caller reads exactly what changed / would change.
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

        # DRY-RUN is the default: only an explicit execute=true writes.
        execute = _bool("execute", default=False)

        tenant = request.GET.get("tenant") or request.POST.get("tenant")
        # tenant may be a slug OR a numeric id (the command resolves either),
        # so we don't reject non-numeric values here — only normalise empty to
        # None so the command falls through to "all tenants".
        tenant = tenant or None

        # Capture the command's stdout so the full report (incl. the Liquid
        # Death breakdown) comes back in the HTTP response.
        out = io.StringIO()
        try:
            call_command(
                "repair_approved_event_status",
                dry_run=(not execute),
                tenant=tenant,
                stdout=out,
            )
        except Exception as exc:  # noqa: BLE001 — surface any error to caller
            logger.exception("Repair approved event status backfill failed")
            return JsonResponse(
                {
                    "ok": False,
                    "error": "command-failed",
                    "detail": str(exc),
                    "executed": execute,
                    "tenant": tenant,
                    "report": out.getvalue(),
                },
                status=500,
            )

        return JsonResponse(
            {
                "executed": execute,
                "tenant": tenant,
                "report": out.getvalue(),
            }
        )

    def get(self, request: HttpRequest) -> HttpResponse:
        deny = _check_secret(request)
        if deny is not None:
            return deny
        return JsonResponse(
            {"ok": True, "endpoint": "repair-approved-event-status"}
        )


@method_decorator(csrf_exempt, name="dispatch")
class RepairMissingEventsForApprovedRequestsView(View):
    """POST `/internal/cron/repair-missing-events-for-approved-requests`.

    Fires the `repair_missing_events_for_approved_requests` backfill — CREATES
    the approved Event (+ pending Job) for Requests that are approved/scheduled
    but have NO Event at all (the client auto-approve gap, where the client
    self-serve create-request path approved the request but never materialized
    an Event — so it was invisible to the Missing Recaps query and the recap
    event picker, and no recap could ever be filed). See
    events/management/commands/repair_missing_events_for_approved_requests.py.
    Idempotent and transaction-wrapped per tenant.

    IMPORTANT: run this AFTER the code fix is deployed — it repairs the
    existing backlog; the deployed fix stops new requests from drifting.

    This is a MANUAL one-off (triggered from the GitHub Actions UI), not a
    recurring cron — but it reuses the same `X-Cron-Secret` gating as its
    siblings so the trigger needs no command line.

    SAFE — DRY-RUN IS THE DEFAULT. A plain trigger NEVER writes: the backfill
    runs with `dry_run=True` unless `execute=true` is explicitly passed, so the
    operator always sees the report first and opts in to writes deliberately.

    Body / query params (all optional):
      - execute: "1" / "true" / "yes" — perform the writes. Default OFF →
        the command runs in DRY-RUN with NO DB writes.
      - tenant: tenant slug or numeric id — restrict to a single tenant.
        Default: all tenants.

    The command's full stdout report (incl. the Liquid Death breakdown) is
    captured and returned verbatim in the response under `report`.
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

        # DRY-RUN is the default: only an explicit execute=true writes. The
        # command opts in to writes via --execute, so we pass execute=<bool>.
        execute = _bool("execute", default=False)

        tenant = request.GET.get("tenant") or request.POST.get("tenant")
        # tenant may be a slug OR a numeric id (the command resolves either),
        # so we don't reject non-numeric values here — only normalise empty to
        # None so the command falls through to "all tenants".
        tenant = tenant or None

        # Capture the command's stdout so the full report (incl. the Liquid
        # Death breakdown) comes back in the HTTP response.
        out = io.StringIO()
        try:
            call_command(
                "repair_missing_events_for_approved_requests",
                execute=execute,
                tenant=tenant,
                stdout=out,
            )
        except Exception as exc:  # noqa: BLE001 — surface any error to caller
            logger.exception(
                "Repair missing events for approved requests backfill failed"
            )
            # Include the exception type + message + last frame so the real
            # cause shows in the trigger's JSON response (and the GitHub
            # Actions log) without needing Cloud Run log access. Per-request
            # failures don't reach here — the command catches those and writes
            # them into `report`; this branch is for the command crashing
            # outright.
            return JsonResponse(
                {
                    "ok": False,
                    "error": "command-failed",
                    "detail": str(exc),
                    "exception": _concise_exc(exc),
                    "executed": execute,
                    "tenant": tenant,
                    "report": out.getvalue(),
                },
                status=500,
            )

        return JsonResponse(
            {
                "executed": execute,
                "tenant": tenant,
                "report": out.getvalue(),
            }
        )

    def get(self, request: HttpRequest) -> HttpResponse:
        deny = _check_secret(request)
        if deny is not None:
            return deny
        return JsonResponse(
            {
                "ok": True,
                "endpoint": "repair-missing-events-for-approved-requests",
            }
        )


@method_decorator(csrf_exempt, name="dispatch")
class RepairEventDatesView(View):
    """POST `/internal/cron/repair-event-dates`.

    Fires the `repair_event_dates` backfill — copies a derived date
    (start_time → request.date → request.start_time) into ``Event.date`` for
    events created before the date-copy fix (#718) that have ``date IS NULL``
    but ``start_time`` set, so "Event Date" reads correctly off the stored row
    (sheets sync, raw exports, admin). See
    events/management/commands/repair_event_dates.py.

    NOTE: the accompanying code fix already makes the read side fall back, so
    the recap "Event Date" display is fixed on deploy WITHOUT this backfill —
    this endpoint is DATA HYGIENE. Idempotent and transaction-wrapped per
    tenant.

    This is a MANUAL one-off (triggered from the GitHub Actions UI), not a
    recurring cron — but it reuses the same `X-Cron-Secret` gating as its
    siblings so the trigger needs no command line.

    SAFE — DRY-RUN IS THE DEFAULT. A plain trigger NEVER writes: the backfill
    runs with `dry_run=True` unless `execute=true` is explicitly passed, so the
    operator always sees the report first and opts in to writes deliberately.

    Body / query params (all optional):
      - execute: "1" / "true" / "yes" — perform the writes. Default OFF →
        the command runs in DRY-RUN with NO DB writes.
      - tenant: tenant slug or numeric id — restrict to a single tenant.
        Default: all tenants.

    The command's full stdout report (incl. the per-tenant breakdown) is
    captured and returned verbatim in the response under `report`.
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

        # DRY-RUN is the default: only an explicit execute=true writes.
        execute = _bool("execute", default=False)

        tenant = request.GET.get("tenant") or request.POST.get("tenant")
        tenant = tenant or None

        out = io.StringIO()
        try:
            call_command(
                "repair_event_dates",
                execute=execute,
                tenant=tenant,
                stdout=out,
            )
        except Exception as exc:  # noqa: BLE001 — surface any error to caller
            logger.exception("Repair event dates backfill failed")
            return JsonResponse(
                {
                    "ok": False,
                    "error": "command-failed",
                    "detail": str(exc),
                    "exception": _concise_exc(exc),
                    "executed": execute,
                    "tenant": tenant,
                    "report": out.getvalue(),
                },
                status=500,
            )

        return JsonResponse(
            {
                "executed": execute,
                "tenant": tenant,
                "report": out.getvalue(),
            }
        )

    def get(self, request: HttpRequest) -> HttpResponse:
        deny = _check_secret(request)
        if deny is not None:
            return deny
        return JsonResponse(
            {
                "ok": True,
                "endpoint": "repair-event-dates",
            }
        )


@method_decorator(csrf_exempt, name="dispatch")
class BackfillEventCoordinatesView(View):
    """POST `/internal/cron/backfill-event-coordinates`.

    Fires the `backfill_event_coordinates` command — populates
    ``Event.coordinates`` for events with missing coordinates
    (null/empty/[0,0]) by COPYING from the parent ``Request.coordinates`` when
    valid (free, no network) and otherwise GEOCODING ``Event.address`` via the
    keyless Photon API. Needed so the "new gig nearby" distance push (and the
    map pins) work for the existing backlog of coordinate-less events. See
    events/management/commands/backfill_event_coordinates.py. Idempotent and
    per-row savepointed.

    This is a MANUAL one-off (triggered from the GitHub Actions UI), not a
    recurring cron — but it reuses the same `X-Cron-Secret` gating as its
    siblings so the trigger needs no command line.

    SAFE — DRY-RUN IS THE DEFAULT. A plain trigger NEVER writes: the backfill
    runs with `dry_run=True` unless `execute=true` is explicitly passed, so the
    operator always sees the report (incl. the geocode plan) first and opts in
    to writes deliberately.

    Body / query params (all optional):
      - execute: "1" / "true" / "yes" — perform the writes (and the real
        Photon geocoding). Default OFF → DRY-RUN, no DB writes, no sleeps.
      - tenant: tenant slug or numeric id — restrict to a single tenant.
        Default: all tenants.

    The command's full stdout report (per-tenant breakdown + which rows came
    from the request copy vs geocoding) is captured and returned verbatim
    under `report`.
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

        # DRY-RUN is the default: only an explicit execute=true writes.
        execute = _bool("execute", default=False)

        tenant = request.GET.get("tenant") or request.POST.get("tenant")
        tenant = tenant or None

        out = io.StringIO()
        try:
            call_command(
                "backfill_event_coordinates",
                execute=execute,
                tenant=tenant,
                stdout=out,
            )
        except Exception as exc:  # noqa: BLE001 — surface any error to caller
            logger.exception("Backfill event coordinates failed")
            return JsonResponse(
                {
                    "ok": False,
                    "error": "command-failed",
                    "detail": str(exc),
                    "exception": _concise_exc(exc),
                    "executed": execute,
                    "tenant": tenant,
                    "report": out.getvalue(),
                },
                status=500,
            )

        return JsonResponse(
            {
                "executed": execute,
                "tenant": tenant,
                "report": out.getvalue(),
            }
        )

    def get(self, request: HttpRequest) -> HttpResponse:
        deny = _check_secret(request)
        if deny is not None:
            return deny
        return JsonResponse(
            {"ok": True, "endpoint": "backfill-event-coordinates"}
        )


@method_decorator(csrf_exempt, name="dispatch")
class BackfillAmbassadorCoordinatesView(View):
    """POST `/internal/cron/backfill-ambassador-coordinates`.

    Fires the `backfill_ambassador_coordinates` command — geocodes
    ``Ambassador.address`` via the keyless Photon API to populate
    ``Ambassador.coordinates`` for BAs with empty coordinates and an address,
    so the "new gig nearby" distance push can measure how far each BA is. See
    ambassadors/management/commands/backfill_ambassador_coordinates.py.
    Idempotent and per-row savepointed.

    This is a MANUAL one-off (triggered from the GitHub Actions UI), not a
    recurring cron — but it reuses the same `X-Cron-Secret` gating.

    SAFE — DRY-RUN IS THE DEFAULT. A plain trigger NEVER writes: the backfill
    runs with `dry_run=True` unless `execute=true` is explicitly passed.

    Body / query params (all optional):
      - execute: "1" / "true" / "yes" — perform the writes (and the real
        Photon geocoding). Default OFF → DRY-RUN, no DB writes, no sleeps.
      - tenant: tenant slug or numeric id — restrict to BAs linked to that
        tenant. Default: all ambassadors.

    The command's full stdout report is captured and returned verbatim under
    `report`.
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

        # DRY-RUN is the default: only an explicit execute=true writes.
        execute = _bool("execute", default=False)

        tenant = request.GET.get("tenant") or request.POST.get("tenant")
        tenant = tenant or None

        out = io.StringIO()
        try:
            call_command(
                "backfill_ambassador_coordinates",
                execute=execute,
                tenant=tenant,
                stdout=out,
            )
        except Exception as exc:  # noqa: BLE001 — surface any error to caller
            logger.exception("Backfill ambassador coordinates failed")
            return JsonResponse(
                {
                    "ok": False,
                    "error": "command-failed",
                    "detail": str(exc),
                    "exception": _concise_exc(exc),
                    "executed": execute,
                    "tenant": tenant,
                    "report": out.getvalue(),
                },
                status=500,
            )

        return JsonResponse(
            {
                "executed": execute,
                "tenant": tenant,
                "report": out.getvalue(),
            }
        )

    def get(self, request: HttpRequest) -> HttpResponse:
        deny = _check_secret(request)
        if deny is not None:
            return deny
        return JsonResponse(
            {"ok": True, "endpoint": "backfill-ambassador-coordinates"}
        )


@method_decorator(csrf_exempt, name="dispatch")
class BackfillRequestRmmRoutingView(View):
    """POST `/internal/cron/backfill-request-rmm-routing`.

    Fires the `backfill_request_rmm_routing` command — assigns the territory
    RMM (and stamps ``request.state``) for requests created WITHOUT routing
    (the internally-created / "SCHEDULED" rows), so they show a Market in the
    Master Tracker and land in the right RMM's linked-sheet view. Assignment
    only — no territory email. See
    events/management/commands/backfill_request_rmm_routing.py. Idempotent +
    per-row savepointed.

    Manual one-off (triggered from the GitHub Actions UI), reusing the same
    `X-Cron-Secret` gating as the other backfills.

    SAFE — DRY-RUN IS THE DEFAULT. A plain trigger NEVER writes and makes NO
    Sheets calls; it runs with `dry_run=True` unless `execute=true` is passed.

    Query/body params (all optional):
      - execute: "1"/"true"/"yes" — perform the assignments + sheet re-syncs.
        Default OFF → DRY-RUN (counts only).
      - tenant: tenant slug or numeric id — restrict to one tenant. Default:
        all routable tenants (territory-mapped or with a default RMM).
      - limit: max requests to repair per invocation (default 100). Re-run
        until the report says remaining=0.

    The command's full stdout report is returned verbatim under `report`.
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

        execute = _bool("execute", default=False)
        tenant = request.GET.get("tenant") or request.POST.get("tenant") or None
        limit_raw = request.GET.get("limit") or request.POST.get("limit")
        limit = None
        if limit_raw:
            try:
                limit = int(limit_raw)
            except (TypeError, ValueError):
                limit = None

        geocode_state = _bool("geocode_state", default=False)
        cmd_kwargs: dict = {
            "execute": execute,
            "tenant": tenant,
            "geocode_state": geocode_state,
        }
        if limit is not None:
            cmd_kwargs["limit"] = limit

        # Manual force path: set an explicit state on a known ID list (for the
        # incomplete rows the parser/geocode can't resolve but a human knows,
        # e.g. "Madison Square Garden" → NY). Both must be present together.
        ids = request.GET.get("ids") or request.POST.get("ids") or None
        force_state = (
            request.GET.get("force_state") or request.POST.get("force_state") or None
        )
        if ids:
            cmd_kwargs["ids"] = ids
        if force_state:
            cmd_kwargs["force_state"] = force_state

        out = io.StringIO()
        try:
            call_command("backfill_request_rmm_routing", stdout=out, **cmd_kwargs)
        except Exception as exc:  # noqa: BLE001 — surface any error to caller
            logger.exception("Backfill request RMM routing failed")
            return JsonResponse(
                {
                    "ok": False,
                    "error": "command-failed",
                    "detail": str(exc),
                    "exception": _concise_exc(exc),
                    "executed": execute,
                    "tenant": tenant,
                    "report": out.getvalue(),
                },
                status=500,
            )

        return JsonResponse(
            {
                "executed": execute,
                "tenant": tenant,
                "report": out.getvalue(),
            }
        )

    def get(self, request: HttpRequest) -> HttpResponse:
        deny = _check_secret(request)
        if deny is not None:
            return deny
        return JsonResponse(
            {"ok": True, "endpoint": "backfill-request-rmm-routing"}
        )


@method_decorator(csrf_exempt, name="dispatch")
class RepairRequestActivationTimeView(View):
    """POST `/internal/cron/repair-request-activation-time`.

    Fixes ONE request's mis-captured activation time (e.g. an AM/PM mix-up
    stored as 3:00 AM instead of 3:00 PM) by setting its LOCAL start/end and
    storing the correct UTC. See
    events/management/commands/repair_request_activation_time.py.

    DRY-RUN IS THE DEFAULT — a plain trigger writes nothing. Required query
    params: `request` (id), `start_local` + `end_local` (24h HH:MM). Pass
    `execute=true` to apply. X-Cron-Secret gated like the other ops endpoints.
    """

    def post(self, request: HttpRequest) -> HttpResponse:
        deny = _check_secret(request)
        if deny is not None:
            return deny

        def _q(name: str) -> str | None:
            return request.GET.get(name) or request.POST.get(name) or None

        execute = (_q("execute") or "").lower() in ("1", "true", "yes", "on")
        req_id = _q("request")
        start_local = _q("start_local")
        end_local = _q("end_local")
        if not (req_id and start_local and end_local):
            return JsonResponse(
                {"ok": False, "error": "request, start_local and end_local are required."},
                status=400,
            )

        out = io.StringIO()
        try:
            call_command(
                "repair_request_activation_time",
                request=int(req_id),
                start_local=start_local,
                end_local=end_local,
                execute=execute,
                stdout=out,
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("Repair request activation time failed")
            return JsonResponse(
                {
                    "ok": False,
                    "error": "command-failed",
                    "detail": str(exc),
                    "exception": _concise_exc(exc),
                    "executed": execute,
                    "report": out.getvalue(),
                },
                status=500,
            )

        return JsonResponse({"executed": execute, "report": out.getvalue()})

    def get(self, request: HttpRequest) -> HttpResponse:
        deny = _check_secret(request)
        if deny is not None:
            return deny
        return JsonResponse(
            {"ok": True, "endpoint": "repair-request-activation-time"}
        )


@method_decorator(csrf_exempt, name="dispatch")
class SendActivationRemindersView(View):
    """POST `/internal/cron/activation-reminders`.

    Fires the `send_activation_reminders` command — pushes the per-shift
    "your shift starts soon" activation reminder to every BA with an
    approved shift starting in the next N minutes (once per shift, deduped
    via AmbassadorEvent.activation_reminder_sent_at).

    Replaces the dead django-rq scheduled reminder (no rqscheduler in prod).
    Designed for a frequent GHA cron (every ~10 min). The push is sent
    INLINE in the web process — no worker needed.

    Body / query params (all optional):
      - lead_minutes: int (default 25) — remind shifts starting within this
        many minutes. Wider than the old 15-min lead so a */10 cron + GHA
        jitter still catches every shift exactly once.
      - dry_run: "1" / "true" / "yes" — log who would be reminded, send
        nothing, stamp nothing.
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

        def _int(name: str, default: int) -> int | None:
            raw = request.GET.get(name) or request.POST.get(name)
            if raw is None or raw == "":
                return default
            try:
                return int(raw)
            except ValueError:
                return None

        lead_minutes = _int("lead_minutes", 25)
        if lead_minutes is None:
            return JsonResponse(
                {"ok": False, "error": "lead_minutes must be an integer"},
                status=400,
            )
        dry_run = _bool("dry_run", default=False)

        cmd_args: list[str] = ["--lead-minutes", str(lead_minutes)]
        if dry_run:
            cmd_args.append("--dry-run")

        out = io.StringIO()
        try:
            call_command("send_activation_reminders", *cmd_args, stdout=out)
        except Exception as exc:  # noqa: BLE001 — surface to caller
            logger.exception("Activation reminders cron failed")
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
                "lead_minutes": lead_minutes,
                "dry_run": dry_run,
                "log": out.getvalue(),
            }
        )

    def get(self, request: HttpRequest) -> HttpResponse:
        deny = _check_secret(request)
        if deny is not None:
            return deny
        return JsonResponse({"ok": True, "endpoint": "activation-reminders"})


@method_decorator(csrf_exempt, name="dispatch")
class SendRecapNudgesView(View):
    """POST `/internal/cron/recap-nudges`.

    Fires the `send_recap_nudges` command — the single, timely per-shift
    "don't forget your recap" nudge for every BA whose approved shift ended
    a few hours ago with no recap on file (once per shift, deduped via
    AmbassadorEvent.recap_nudge_sent_at).

    Replaces the dead django-rq scheduled nudge (no rqscheduler in prod).
    COMPLEMENTS the daily recap-reminders sweep — this is the timely
    once-per-shift ping, the sweep is the escalating daily hammer; the
    dedup stamp guarantees this fires at most once per shift. The push is
    sent INLINE in the web process — no worker needed. Designed for an
    hourly GHA cron.

    Body / query params (all optional):
      - grace_hours: int (default 1) — don't nudge until this many hours
        after the shift ends.
      - max_age_hours: int (default 24) — stop the timely nudge once a shift
        is older than this (the daily sweep takes over).
      - dry_run: "1" / "true" / "yes" — log who would be nudged, send
        nothing, stamp nothing.
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

        def _int(name: str, default: int) -> int | None:
            raw = request.GET.get(name) or request.POST.get(name)
            if raw is None or raw == "":
                return default
            try:
                return int(raw)
            except ValueError:
                return None

        grace_hours = _int("grace_hours", 1)
        max_age_hours = _int("max_age_hours", 24)
        if grace_hours is None or max_age_hours is None:
            return JsonResponse(
                {"ok": False, "error": "grace_hours/max_age_hours must be integers"},
                status=400,
            )
        dry_run = _bool("dry_run", default=False)

        cmd_args: list[str] = [
            "--grace-hours", str(grace_hours),
            "--max-age-hours", str(max_age_hours),
        ]
        if dry_run:
            cmd_args.append("--dry-run")

        out = io.StringIO()
        try:
            call_command("send_recap_nudges", *cmd_args, stdout=out)
        except Exception as exc:  # noqa: BLE001 — surface to caller
            logger.exception("Recap nudges cron failed")
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
                "grace_hours": grace_hours,
                "max_age_hours": max_age_hours,
                "dry_run": dry_run,
                "log": out.getvalue(),
            }
        )

    def get(self, request: HttpRequest) -> HttpResponse:
        deny = _check_secret(request)
        if deny is not None:
            return deny
        return JsonResponse({"ok": True, "endpoint": "recap-nudges"})


@method_decorator(csrf_exempt, name="dispatch")
class SendOpenShiftAlertsView(View):
    """POST `/internal/cron/send-open-shift-alerts`.

    Fires `send_open_shift_alerts` — pushes eligible BAs when a shift is
    dropped (an OpenShift opens up). Runs off-request because there's no RQ
    worker in prod and the fan-out shouldn't block the drop mutation; each
    OpenShift is alerted exactly once (deduped via `notified_at`).

    Body / query params (all optional):
      - radius_miles: float (proximity gate when the event has coordinates)
      - max_per_shift: int (cap the per-shift fan-out)
      - dry_run: "1" / "true" / "yes" — log who'd be alerted, send nothing
    """

    def post(self, request: HttpRequest) -> HttpResponse:
        deny = _check_secret(request)
        if deny is not None:
            return deny

        def _bool(name: str, default: bool = False) -> bool:
            raw = (request.GET.get(name) or request.POST.get(name) or "").lower()
            return raw in ("1", "true", "yes", "on") if raw else default

        cmd_args: list[str] = []
        radius = request.GET.get("radius_miles") or request.POST.get("radius_miles")
        if radius:
            cmd_args += ["--radius-miles", str(radius)]
        max_per = request.GET.get("max_per_shift") or request.POST.get(
            "max_per_shift"
        )
        if max_per:
            cmd_args += ["--max-per-shift", str(max_per)]
        dry_run = _bool("dry_run", default=False)
        if dry_run:
            cmd_args.append("--dry-run")

        out = io.StringIO()
        try:
            call_command("send_open_shift_alerts", *cmd_args, stdout=out)
        except Exception as exc:  # noqa: BLE001 — surface to caller
            logger.exception("Open-shift alerts cron failed")
            return JsonResponse(
                {
                    "ok": False,
                    "error": "command-failed",
                    "detail": str(exc),
                    "log": out.getvalue(),
                },
                status=500,
            )

        return JsonResponse({"ok": True, "dry_run": dry_run, "log": out.getvalue()})

    def get(self, request: HttpRequest) -> HttpResponse:
        deny = _check_secret(request)
        if deny is not None:
            return deny
        return JsonResponse({"ok": True, "endpoint": "send-open-shift-alerts"})


@method_decorator(csrf_exempt, name="dispatch")
class SendClientWeeklyDigestView(View):
    """POST `/internal/cron/send-client-weekly-digest`.

    Fires `send_client_weekly_digest` — builds each opted-in tenant's weekly
    rollup (this week at a glance / coming up / needs your approval) and emails
    it to that tenant's client contacts. Designed for a WEEKLY GHA cron
    (Monday AM): the command's trailing window is the last 7 days and the
    look-ahead is the next 7 days.

    SAFE — OPT-IN OFF by default. Reuses the SAME gate as the monthly report:
    only tenants with `scheduled_report_enabled=True` AND recipients get email,
    and that flag defaults to False — so a scheduled run emails NOBODY until
    Ignite flips a client on. Quiet weeks are skipped unless `force`.

    Body / query params (all optional):
      - dry_run: "1" / "true" / "yes" — build the digest + log recipients,
        but send NO email.
      - tenant: int — restrict to a single tenant id (does NOT bypass opt-in).
      - force: "1" / "true" / "yes" — send even if the week is quiet.
    """

    def post(self, request: HttpRequest) -> HttpResponse:
        deny = _check_secret(request)
        if deny is not None:
            return deny

        def _bool(name: str, default: bool = False) -> bool:
            raw = (request.GET.get(name) or request.POST.get(name) or "").lower()
            return raw in ("1", "true", "yes", "on") if raw else default

        dry_run = _bool("dry_run", default=False)
        force = _bool("force", default=False)

        tenant = request.GET.get("tenant") or request.POST.get("tenant")
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

        out = io.StringIO()
        try:
            call_command("send_client_weekly_digest", *cmd_args, stdout=out)
        except Exception as exc:  # noqa: BLE001 — surface to caller
            logger.exception("Client weekly digest cron failed")
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
                "log": out.getvalue(),
            }
        )

    def get(self, request: HttpRequest) -> HttpResponse:
        deny = _check_secret(request)
        if deny is not None:
            return deny
        return JsonResponse({"ok": True, "endpoint": "send-client-weekly-digest"})


@method_decorator(csrf_exempt, name="dispatch")
class BackfillGirlBeerReceiptsView(View):
    """POST `/internal/cron/backfill-girlbeer-receipts`.

    Fires `backfill_girlbeer_receipts`. Girl Beer was onboarded WITHOUT its
    own FileRecapCategory rows, so receipt uploads (positional sentinel "2")
    fell through to a foreign/global PK-2 "Table setup" — a cross-tenant leak
    (the #765 bug ran deeper than a keyword match could fix). This command
    does the real fix in two safe steps: (1) SEED Girl Beer's own default
    categories ("Sampling photos", "Table setup", "Receipts") so NEW uploads
    resolve correctly, and (2) BACKFILL the existing mis-filed receipts —
    Girl Beer recap files (scoped by the file's RECAP tenant, to catch the
    foreign-category leak) currently in a `source`-named category are moved to
    the tenant's `target` ("Receipts"). RECATEGORIZE + SEED only: never
    deletes a file or moves a blob. Idempotent. One-off manual backfill.

    SAFE — DRY-RUN by default. Only an explicit `execute=true` writes (seeds +
    moves). Without it, the endpoint reports what it WOULD seed and move.

    Body / query params (all optional):
      - execute: "1"/"true"/"yes" — perform the writes (default OFF → dry-run).
      - tenant_slug: default "girl-beer".
      - source: source category name to drain, default "Table setup".
      - target: target category name, default "Receipts".
    """

    def post(self, request: HttpRequest) -> HttpResponse:
        deny = _check_secret(request)
        if deny is not None:
            return deny

        def _bool(name: str, default: bool = False) -> bool:
            raw = (request.GET.get(name) or request.POST.get(name) or "").lower()
            return raw in ("1", "true", "yes", "on") if raw else default

        def _str(name: str):
            return request.GET.get(name) or request.POST.get(name) or None

        execute = _bool("execute", default=False)
        tenant_slug = _str("tenant_slug") or "girl-beer"
        source = _str("source") or "Table setup"
        target = _str("target")

        kwargs = {
            "execute": execute,
            "tenant_slug": tenant_slug,
            "source": source,
        }
        if target is not None:
            kwargs["target"] = target

        out = io.StringIO()
        try:
            call_command("backfill_girlbeer_receipts", stdout=out, **kwargs)
        except Exception as exc:  # noqa: BLE001 — surface to caller
            logger.exception("Girl Beer receipt backfill cron failed")
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
                "executed": execute,
                "tenant_slug": tenant_slug,
                "source": source,
                "log": out.getvalue(),
            }
        )

    def get(self, request: HttpRequest) -> HttpResponse:
        deny = _check_secret(request)
        if deny is not None:
            return deny
        return JsonResponse({"ok": True, "endpoint": "backfill-girlbeer-receipts"})


@method_decorator(csrf_exempt, name="dispatch")
class AuditTenantOnboardingView(View):
    """POST `/internal/cron/audit-tenant-onboarding`.

    Fires `audit_tenant_onboarding` — reports, for every tenant: missing
    onboarding seeds (file categories, event/request types + statuses, rate
    types, types of good), recap files sitting in ANOTHER tenant's category
    (the Girl Beer cross-tenant leak), and duplicate global skills.

    READ-ONLY by default. Optional writes (each ALSO requires `execute=true`):
      - seed_file_categories: seed default file categories for tenants with
        NONE (additive only).
      - seed_defaults: same, plus rate types / types of good for tenants with
        zero of those (additive only).
      - rehome_foreign_files: move each cross-tenant recap file to the OWNER
        tenant's same-NAME category (created if missing) — the category name
        the UI groups by is preserved exactly; never deletes anything.

    Body / query params (all optional):
      - seed_file_categories / seed_defaults / rehome_foreign_files /
        execute: "1"/"true"/"yes" (all default OFF).
      - notify: "1"/"true"/"yes" — email the Ignite team when the audit finds
        a regression (seed gaps or cross-tenant files). Quiet runs send
        nothing. Used by the weekly scheduled run.
    """

    def post(self, request: HttpRequest) -> HttpResponse:
        deny = _check_secret(request)
        if deny is not None:
            return deny

        def _bool(name: str, default: bool = False) -> bool:
            raw = (request.GET.get(name) or request.POST.get(name) or "").lower()
            return raw in ("1", "true", "yes", "on") if raw else default

        execute = _bool("execute", default=False)
        seed_file_categories = _bool("seed_file_categories", default=False)
        seed_defaults = _bool("seed_defaults", default=False)
        rehome_foreign_files = _bool("rehome_foreign_files", default=False)
        notify = _bool("notify", default=False)

        out = io.StringIO()
        try:
            call_command(
                "audit_tenant_onboarding",
                stdout=out,
                execute=execute,
                seed_file_categories=seed_file_categories,
                seed_defaults=seed_defaults,
                rehome_foreign_files=rehome_foreign_files,
                notify=notify,
            )
        except Exception as exc:  # noqa: BLE001 — surface to caller
            logger.exception("Tenant onboarding audit cron failed")
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
                "executed": execute,
                "seed_file_categories": seed_file_categories,
                "seed_defaults": seed_defaults,
                "rehome_foreign_files": rehome_foreign_files,
                "notify": notify,
                "log": out.getvalue(),
            }
        )

    def get(self, request: HttpRequest) -> HttpResponse:
        deny = _check_secret(request)
        if deny is not None:
            return deny
        return JsonResponse({"ok": True, "endpoint": "audit-tenant-onboarding"})


@method_decorator(csrf_exempt, name="dispatch")
class DedupeSkillsView(View):
    """POST `/internal/cron/dedupe-skills`.

    Fires `dedupe_skills` — merges duplicate global Skill rows
    (case-insensitive name): AmbassadorSkill links are repointed to the
    lowest-id keeper (redundant double-links dropped), then the duplicate
    Skill rows are deleted. The command refuses to run if any relation other
    than AmbassadorSkill points at Skill. One-off manual cleanup approved by
    Kyle (the create-side guard shipped in #772 stops new duplicates).

    SAFE — DRY-RUN by default; only an explicit `execute=true` writes.

    Body / query params (all optional):
      - execute: "1"/"true"/"yes" — apply the merge (default OFF → dry-run).
    """

    def post(self, request: HttpRequest) -> HttpResponse:
        deny = _check_secret(request)
        if deny is not None:
            return deny

        raw = (
            request.GET.get("execute") or request.POST.get("execute") or ""
        ).lower()
        execute = raw in ("1", "true", "yes", "on")

        out = io.StringIO()
        try:
            call_command("dedupe_skills", stdout=out, execute=execute)
        except Exception as exc:  # noqa: BLE001 — surface to caller
            logger.exception("Skill dedupe cron failed")
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
            {"ok": True, "executed": execute, "log": out.getvalue()}
        )

    def get(self, request: HttpRequest) -> HttpResponse:
        deny = _check_secret(request)
        if deny is not None:
            return deny
        return JsonResponse({"ok": True, "endpoint": "dedupe-skills"})


@method_decorator(csrf_exempt, name="dispatch")
class SendShiftConfirmationsView(View):
    """POST `/internal/cron/shift-confirmations`.

    Fires the `send_shift_confirmations` command — the day-before
    "confirm you're in" push for every approved shift starting within
    `lead_hours` (default 26, once per shift via
    confirmation_requested_at), plus the morning-of "still unconfirmed"
    alert email to the Ignite team for shifts starting within
    `alert_hours` (default 4, once per row via unconfirmed_alerted_at).
    Designed for an hourly GHA cron; windows are wider than the cadence
    so nothing slips between runs.

    Body / query params (all optional):
      - lead_hours: int (default 26)
      - alert_hours: int (default 4)
      - dry_run: "1" / "true" / "yes" — log, send nothing, stamp nothing.
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

        def _int(name: str, default: int) -> int | None:
            raw = request.GET.get(name) or request.POST.get(name)
            if raw is None or raw == "":
                return default
            try:
                return int(raw)
            except ValueError:
                return None

        lead_hours = _int("lead_hours", 26)
        alert_hours = _int("alert_hours", 4)
        if lead_hours is None or alert_hours is None:
            return JsonResponse(
                {"ok": False, "error": "lead_hours/alert_hours must be integers"},
                status=400,
            )
        dry_run = _bool("dry_run", default=False)

        cmd_args: list[str] = [
            "--lead-hours", str(lead_hours),
            "--alert-hours", str(alert_hours),
        ]
        if dry_run:
            cmd_args.append("--dry-run")

        out = io.StringIO()
        try:
            call_command("send_shift_confirmations", *cmd_args, stdout=out)
        except Exception as exc:  # noqa: BLE001 — surface to caller
            logger.exception("Shift confirmations cron failed")
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
                "lead_hours": lead_hours,
                "alert_hours": alert_hours,
                "dry_run": dry_run,
                "log": out.getvalue(),
            }
        )


@method_decorator(csrf_exempt, name="dispatch")
class ProvisionReviewAmbassadorView(View):
    """POST `/internal/cron/provision-review-ambassador`.

    One-off, secret-gated provisioning of the app-store review BA login
    (see ambassadors/management/commands/seed_review_ambassador.py).
    Bounded HARD to a single allow-listed email so this endpoint can
    never touch any other account — the password arrives in the POST
    body (never the URL/logs).

    Body params: email (must be allow-listed), password.
    """

    # Only this account may be provisioned through the public endpoint.
    _ALLOWED = {"kylechristiansen93@gmail.com"}

    def post(self, request: HttpRequest) -> HttpResponse:
        deny = _check_secret(request)
        if deny is not None:
            return deny

        email = (
            request.POST.get("email") or request.GET.get("email") or ""
        ).strip().lower()
        password = request.POST.get("password") or request.GET.get("password") or ""
        if email not in self._ALLOWED:
            return JsonResponse(
                {"ok": False, "error": "email not allow-listed"}, status=400
            )
        if not password:
            return JsonResponse(
                {"ok": False, "error": "password required"}, status=400
            )

        out = io.StringIO()
        try:
            call_command(
                "seed_review_ambassador",
                "--email", email,
                "--password", password,
                stdout=out,
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("Review-ambassador provisioning failed")
            return JsonResponse(
                {"ok": False, "error": "command-failed", "detail": str(exc)},
                status=500,
            )
        # Never echo the password back; log is the command's own summary.
        return JsonResponse({"ok": True, "log": out.getvalue()})


@method_decorator(csrf_exempt, name="dispatch")
class ImportEventScheduleView(View):
    """POST `/internal/cron/import-event-schedule`.

    Bulk-creates a client's activation schedule (approved Requests + approved
    Events on the Master Tracker) from a committed JSON file, via the
    `import_event_schedule` command — which reuses the same importer the admin
    Bulk Upload UI calls (per-store+start-time DEDUP, atomic rollback). Used to
    load Stone House Bread's Q2–Q3 2026 Kroger sampling schedule without
    hand-copying tenant IDs into a sheet (the command resolves tenant +
    Retail Sampling type + Eastern timezone by name).

    SAFE — DRY-RUN IS THE DEFAULT. A plain trigger NEVER writes the events:
    the import runs with dry_run=True unless `execute=true` is passed. (The
    command does idempotent get_or_create on the tenant's EventType /
    RequestType / approved statuses regardless, since the rows can't validate
    without them — but that's safe, reusable setup, not the bulk data.)

    Body / query params (all optional):
      - execute: "1"/"true"/"yes" — perform the writes. Default OFF → dry-run.
      - schedule: schedule key (default "stone_house_q2q3_2026").
      - tenant_name: override the tenant name baked into the schedule JSON.

    The command's full stdout report (resolved IDs, timezone sample, per-row
    outcomes) is captured and returned verbatim under `report`.
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

        # DRY-RUN is the default: only an explicit execute=true writes events.
        execute = _bool("execute", default=False)
        schedule = (
            request.GET.get("schedule")
            or request.POST.get("schedule")
            or "stone_house_q2q3_2026"
        )
        tenant_name = (
            request.GET.get("tenant_name") or request.POST.get("tenant_name") or None
        )

        cmd_kwargs: dict[str, Any] = {"schedule": schedule, "commit": execute}
        if tenant_name:
            cmd_kwargs["tenant_name"] = tenant_name

        out = io.StringIO()
        try:
            call_command("import_event_schedule", stdout=out, **cmd_kwargs)
        except Exception as exc:  # noqa: BLE001 — surface any error to caller
            logger.exception("Import event schedule failed")
            return JsonResponse(
                {
                    "ok": False,
                    "error": "command-failed",
                    "detail": str(exc),
                    "executed": execute,
                    "schedule": schedule,
                    "report": out.getvalue(),
                },
                status=500,
            )

        return JsonResponse(
            {
                "executed": execute,
                "schedule": schedule,
                "report": out.getvalue(),
            }
        )

    def get(self, request: HttpRequest) -> HttpResponse:
        deny = _check_secret(request)
        if deny is not None:
            return deny
        return JsonResponse({"ok": True, "endpoint": "import-event-schedule"})


@method_decorator(csrf_exempt, name="dispatch")
class VerifyUserView(View):
    """POST `/internal/cron/verify-user`.

    Marks a user verified + active (gqlauth `UserStatus.verified=True`,
    `is_active=True`) so they can sign in — via the `verify_user` command.
    Unblocks a client/RMM user stuck at "Please verify your account" when the
    create-client flow left them unverified. Does NOT touch role/password.

    SAFE — DRY-RUN IS THE DEFAULT. A plain trigger only reports the user's
    current state; pass `execute=true` to apply.

    Body / query params:
      - email: REQUIRED — the account to verify.
      - execute: "1"/"true"/"yes" — apply. Default OFF → dry-run.
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

        execute = _bool("execute", default=False)
        email = request.GET.get("email") or request.POST.get("email")
        if not email:
            return JsonResponse(
                {"ok": False, "error": "email-required"}, status=400
            )

        out = io.StringIO()
        try:
            call_command("verify_user", email=email, commit=execute, stdout=out)
        except Exception as exc:  # noqa: BLE001 — surface to caller
            logger.exception("verify_user failed")
            return JsonResponse(
                {
                    "ok": False,
                    "error": "command-failed",
                    "detail": str(exc),
                    "executed": execute,
                    "report": out.getvalue(),
                },
                status=500,
            )

        return JsonResponse({"executed": execute, "report": out.getvalue()})

    def get(self, request: HttpRequest) -> HttpResponse:
        deny = _check_secret(request)
        if deny is not None:
            return deny
        return JsonResponse({"ok": True, "endpoint": "verify-user"})


@method_decorator(csrf_exempt, name="dispatch")
class SetTenantEventTypesView(View):
    """POST `/internal/cron/set-tenant-event-types`.

    Standardizes tenant EventTypes to Retail Sampling / On-Premise Sampling /
    Event — via the `set_tenant_event_types` command. "Swap stock, keep
    custom": ensures the three exist + Retail Sampling default, retires only
    the legacy stock types (Sampling / Promotion / Launch / Special Event,
    repointing their events + recap templates onto Retail Sampling first), and
    leaves any client-specific custom types alone. Jeeter is excluded.

    SAFE — DRY-RUN IS THE DEFAULT. A plain trigger writes NOTHING: it reports
    per-tenant what a real run would create / repoint / delete / keep. Pass
    `execute=true` to apply.

    Body / query params (all optional):
      - execute: "1"/"true"/"yes" — apply. Default OFF → dry-run.
      - tenant_name: scope to ONE tenant. Default (empty) → the whole fleet.
      - exclude: comma-separated tenant names to skip (default "Jeeter").
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

        execute = _bool("execute", default=False)
        tenant_name = (
            request.GET.get("tenant_name") or request.POST.get("tenant_name") or None
        )
        exclude = request.GET.get("exclude") or request.POST.get("exclude") or None

        cmd_kwargs: dict[str, Any] = {"commit": execute}
        if tenant_name:
            cmd_kwargs["tenant_name"] = tenant_name
        else:
            cmd_kwargs["all_tenants"] = True
        if exclude:
            cmd_kwargs["exclude"] = exclude

        out = io.StringIO()
        try:
            call_command("set_tenant_event_types", stdout=out, **cmd_kwargs)
        except Exception as exc:  # noqa: BLE001 — surface to caller
            logger.exception("set_tenant_event_types failed")
            return JsonResponse(
                {
                    "ok": False,
                    "error": "command-failed",
                    "detail": str(exc),
                    "executed": execute,
                    "report": out.getvalue(),
                },
                status=500,
            )

        return JsonResponse({"executed": execute, "report": out.getvalue()})

    def get(self, request: HttpRequest) -> HttpResponse:
        deny = _check_secret(request)
        if deny is not None:
            return deny
        return JsonResponse({"ok": True, "endpoint": "set-tenant-event-types"})


@method_decorator(csrf_exempt, name="dispatch")
class AuditTenantConsumersView(View):
    """GET/POST `/internal/cron/audit-tenant-consumers`.

    READ-ONLY per-recap "consumers sampled" breakdown for ONE tenant — explains
    the dashboard's "Consumers reached" total: which recaps contribute, and
    whether any event has BOTH a legacy + custom recap (double-counting). Fires
    the `audit_tenant_consumers` command; the response `log` is the table.

    Query params:
      - tenant: id / request-url-name / name (required)
      - year: int (optional; default current calendar year)
      - all_time: "1"/"true"/"yes" — ignore the year window
    """

    def _run(self, request: HttpRequest) -> HttpResponse:
        deny = _check_secret(request)
        if deny is not None:
            return deny

        tenant = request.GET.get("tenant") or request.POST.get("tenant")
        if not tenant:
            return JsonResponse(
                {"ok": False, "error": "tenant-required (id / name / url-name)"},
                status=400,
            )
        raw = (
            request.GET.get("all_time") or request.POST.get("all_time") or ""
        ).lower()
        kwargs: dict = {
            "tenant": str(tenant),
            "all_time": raw in ("1", "true", "yes", "on"),
        }
        year = request.GET.get("year") or request.POST.get("year")
        if year:
            try:
                kwargs["year"] = int(year)
            except ValueError:
                return JsonResponse(
                    {"ok": False, "error": "year-must-be-int"}, status=400
                )

        out = io.StringIO()
        try:
            call_command("audit_tenant_consumers", stdout=out, **kwargs)
        except Exception as exc:  # noqa: BLE001 — surface to caller
            logger.exception("Tenant consumers audit cron failed")
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
            {"ok": True, "tenant": str(tenant), "log": out.getvalue()}
        )

    def post(self, request: HttpRequest) -> HttpResponse:
        return self._run(request)

    def get(self, request: HttpRequest) -> HttpResponse:
        return self._run(request)


@method_decorator(csrf_exempt, name="dispatch")
class SetCustomRecapFieldView(View):
    """GET/POST `/internal/cron/set-custom-recap-field`.

    Targeted, reversible correction of ONE custom-recap field value (e.g.
    fixing a "Consumers Sampled" typed as 1960 -> 30). Fires the
    `set_custom_recap_field` command; the response `log` shows the before/after
    (and, on a dry-run, exactly what WOULD change). DRY-RUN unless apply=true.

    Query params:
      - recap: CustomRecap id or uuid (required)
      - field_contains: case-insensitive substring of the field NAME (required)
      - value: new value (required)
      - expect_current: only write if the current value equals this (optional
        safety guard — makes re-runs idempotent)
      - apply: "1"/"true"/"yes" — actually write (omit for dry-run)
    """

    def _run(self, request: HttpRequest) -> HttpResponse:
        deny = _check_secret(request)
        if deny is not None:
            return deny

        def _param(name: str) -> str | None:
            return request.GET.get(name) or request.POST.get(name)

        recap = _param("recap")
        field_contains = _param("field_contains")
        value = _param("value")
        if not recap or not field_contains or value is None:
            return JsonResponse(
                {
                    "ok": False,
                    "error": "recap, field_contains and value are required",
                },
                status=400,
            )
        apply_raw = (_param("apply") or "").lower()
        kwargs: dict = {
            "recap": str(recap),
            "field_contains": str(field_contains),
            "value": str(value),
            "apply": apply_raw in ("1", "true", "yes", "on"),
        }
        expect = _param("expect_current")
        if expect is not None:
            kwargs["expect_current"] = str(expect)

        out = io.StringIO()
        try:
            call_command("set_custom_recap_field", stdout=out, **kwargs)
        except Exception as exc:  # noqa: BLE001 — surface to caller
            logger.exception("set_custom_recap_field cron failed")
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
            {"ok": True, "applied": kwargs["apply"], "log": out.getvalue()}
        )

    def post(self, request: HttpRequest) -> HttpResponse:
        return self._run(request)

    def get(self, request: HttpRequest) -> HttpResponse:
        return self._run(request)


@method_decorator(csrf_exempt, name="dispatch")
class ExportRecapsToSheetView(View):
    """POST `/internal/cron/export-recaps-to-sheet`.

    Full-refreshes a tenant's recap data ("demo info" — every custom-template
    field value per recap, including the demographic breakdowns) into their
    `recap_export_sheet_url`. Fired daily by GitHub Actions.

    Params (query or POST, all optional):
      - tenant_slug: restrict to one tenant (default: every tenant that has a
        recap_export_sheet_url set — i.e. --all-linked)
      - sheet_url: with tenant_slug, persist this URL onto the tenant first,
        so the scheduled workflow can seed the sheet without DB access
      - tab: worksheet name (default "Recap Data")
      - dry_run: "1"/"true"/"yes" — report row/column counts, write nothing
    """

    def _run(self, request: HttpRequest) -> HttpResponse:
        deny = _check_secret(request)
        if deny is not None:
            return deny

        def _get(name: str) -> str:
            return (request.GET.get(name) or request.POST.get(name) or "").strip()

        def _bool(name: str, default: bool = False) -> bool:
            raw = _get(name).lower()
            if not raw:
                return default
            return raw in ("1", "true", "yes", "on")

        tenant_slug = _get("tenant_slug")
        sheet_url = _get("sheet_url")
        tab = _get("tab")
        dry_run = _bool("dry_run", default=False)

        cmd_args: list[str] = []
        if tenant_slug:
            cmd_args += ["--tenant-slug", tenant_slug]
            if sheet_url:
                cmd_args += ["--sheet-url", sheet_url]
        else:
            cmd_args += ["--all-linked"]
        if tab:
            cmd_args += ["--tab", tab]
        if not dry_run:
            cmd_args += ["--apply"]

        out = io.StringIO()
        try:
            call_command("export_recaps_to_sheet", *cmd_args, stdout=out)
        except Exception as exc:
            logger.exception("export-recaps-to-sheet cron failed")
            return JsonResponse(
                {
                    "ok": False,
                    "error": "command-failed",
                    "exception": _concise_exc(exc),
                    "log": out.getvalue(),
                },
                status=500,
            )
        return JsonResponse(
            {
                "ok": True,
                "tenant_slug": tenant_slug or "(all-linked)",
                "dry_run": dry_run,
                "log": out.getvalue(),
            }
        )

    def post(self, request: HttpRequest) -> HttpResponse:
        return self._run(request)

    def get(self, request: HttpRequest) -> HttpResponse:
        return self._run(request)


@method_decorator(csrf_exempt, name="dispatch")
class ExportLdSummaryView(View):
    """POST `/internal/cron/export-ld-summary`.

    Rebuilds the Liquid Death "Summary" tab from Spark recaps (branded LD).
    Params (query or POST): tenant_slug, sheet_url, tab (default "Summary"),
    target_tab (stage to a scratch tab), dry_run.
    """

    def _run(self, request: HttpRequest) -> HttpResponse:
        deny = _check_secret(request)
        if deny is not None:
            return deny

        def _get(name: str) -> str:
            return (request.GET.get(name) or request.POST.get(name) or "").strip()

        def _bool(name: str, default: bool = False) -> bool:
            raw = _get(name).lower()
            return raw in ("1", "true", "yes", "on") if raw else default

        cmd_args: list[str] = []
        for flag in ("tenant_slug", "sheet_url", "tab", "target_tab"):
            val = _get(flag)
            if val:
                cmd_args += [f"--{flag.replace('_', '-')}", val]
        if not _bool("dry_run", default=False):
            cmd_args.append("--apply")

        out = io.StringIO()
        try:
            call_command("export_ld_summary_to_sheet", *cmd_args, stdout=out)
        except Exception as exc:
            logger.exception("export-ld-summary cron failed")
            return JsonResponse(
                {"ok": False, "error": "command-failed", "exception": _concise_exc(exc), "log": out.getvalue()},
                status=500,
            )
        return JsonResponse({"ok": True, "log": out.getvalue()})

    def post(self, request: HttpRequest) -> HttpResponse:
        return self._run(request)

    def get(self, request: HttpRequest) -> HttpResponse:
        return self._run(request)


@method_decorator(csrf_exempt, name="dispatch")
class DescribeSheetTabsView(View):
    """GET/POST `/internal/cron/describe-sheet-tabs` — read-only.

    Lists a sheet's tab titles + row/col counts + row 1 of tracker/summary
    tabs, so we can confirm exact tab names in prod before any write. Params:
    sheet_url or tenant_slug.
    """

    def _run(self, request: HttpRequest) -> HttpResponse:
        deny = _check_secret(request)
        if deny is not None:
            return deny

        def _get(name: str) -> str:
            return (request.GET.get(name) or request.POST.get(name) or "").strip()

        cmd_args: list[str] = []
        for flag in ("sheet_url", "tenant_slug"):
            val = _get(flag)
            if val:
                cmd_args += [f"--{flag.replace('_', '-')}", val]

        out = io.StringIO()
        try:
            call_command("describe_sheet_tabs", *cmd_args, stdout=out)
        except Exception as exc:
            logger.exception("describe-sheet-tabs cron failed")
            return JsonResponse(
                {"ok": False, "error": "command-failed", "exception": _concise_exc(exc), "log": out.getvalue()},
                status=500,
            )
        return JsonResponse({"ok": True, "log": out.getvalue()})

    def post(self, request: HttpRequest) -> HttpResponse:
        return self._run(request)

    def get(self, request: HttpRequest) -> HttpResponse:
        return self._run(request)


@method_decorator(csrf_exempt, name="dispatch")
class BackfillLdMasterTrackerView(View):
    """POST `/internal/cron/backfill-ld-master-tracker`.

    One-time/maintenance: pin the Liquid Death tenant's Master-Tracker tab name
    (so the request mirror targets the live tab, not the first worksheet) and
    backfill all of the tenant's requests into it. Writes only columns A-O —
    manual columns past "Spark Link" are preserved. dry_run by default; pass
    execute=1 to write. Params: tenant_slug, sheet_url (sets linked_sheet_url),
    tab_name (default "MASTER_Tracker"), insert_by_date (default 1 — new rows
    land at their date-sorted slot, descending, instead of appended), execute.
    """

    def _run(self, request: HttpRequest) -> HttpResponse:
        deny = _check_secret(request)
        if deny is not None:
            return deny

        def _get(name: str) -> str:
            return (request.GET.get(name) or request.POST.get(name) or "").strip()

        def _bool(name: str, default: bool = False) -> bool:
            raw = _get(name).lower()
            return raw in ("1", "true", "yes", "on") if raw else default

        from tenants.models import Tenant

        slug = _get("tenant_slug") or "ighn-liquid-death"
        sheet_url = _get("sheet_url")
        tab_name = _get("tab_name") or "MASTER_Tracker"
        insert_by_date = _bool("insert_by_date", default=True)
        execute = _bool("execute", default=False)

        tenant = Tenant.objects.filter(slug=slug).first()
        if tenant is None:
            tenant = Tenant.objects.filter(name__icontains="liquid death").first()
        if tenant is None:
            return JsonResponse({"ok": False, "error": "no-tenant", "slug": slug}, status=404)

        changes: dict[str, str] = {}
        if tab_name and (tenant.master_tracker_tab_name or "") != tab_name:
            tenant.master_tracker_tab_name = tab_name
            changes["master_tracker_tab_name"] = tab_name
        if sheet_url and (tenant.linked_sheet_url or "") != sheet_url:
            tenant.linked_sheet_url = sheet_url
            changes["linked_sheet_url"] = sheet_url
        if bool(tenant.master_tracker_insert_by_date) != insert_by_date:
            tenant.master_tracker_insert_by_date = insert_by_date
            changes["master_tracker_insert_by_date"] = str(insert_by_date)
        if changes:
            tenant.save(update_fields=list(changes.keys()))

        out = io.StringIO()
        cmd_args = ["--tenant-slug", tenant.slug]
        if execute:
            cmd_args.append("--apply")
        try:
            call_command("sync_tenant_to_sheet", *cmd_args, stdout=out)
        except Exception as exc:
            logger.exception("backfill-ld-master-tracker cron failed")
            return JsonResponse(
                {"ok": False, "error": "command-failed", "exception": _concise_exc(exc), "changes": changes, "log": out.getvalue()},
                status=500,
            )
        return JsonResponse(
            {
                "ok": True,
                "tenant": tenant.slug,
                "changes": changes,
                "execute": execute,
                "master_tracker_tab_name": tenant.master_tracker_tab_name,
                "master_tracker_insert_by_date": tenant.master_tracker_insert_by_date,
                "log": out.getvalue(),
            }
        )

    def post(self, request: HttpRequest) -> HttpResponse:
        return self._run(request)

    def get(self, request: HttpRequest) -> HttpResponse:
        return self._run(request)


@method_decorator(csrf_exempt, name="dispatch")
class ExportLdRecapsView(View):
    """POST `/internal/cron/export-ld-recaps`.

    Writes Liquid Death's raw recap data into a branded "Spark Recaps" tab and
    (on a write) pins the tenant's recap-export config so the on-save signal +
    daily cron target that tab. Params: tenant_slug, sheet_url, tab (default
    "Spark Recaps"), year (optional), no_on_submit, dry_run.
    """

    def _run(self, request: HttpRequest) -> HttpResponse:
        deny = _check_secret(request)
        if deny is not None:
            return deny

        def _get(name: str) -> str:
            return (request.GET.get(name) or request.POST.get(name) or "").strip()

        def _bool(name: str, default: bool = False) -> bool:
            raw = _get(name).lower()
            return raw in ("1", "true", "yes", "on") if raw else default

        cmd_args: list[str] = []
        for flag in ("tenant_slug", "sheet_url", "tab", "year"):
            val = _get(flag)
            if val:
                cmd_args += [f"--{flag.replace('_', '-')}", val]
        if _bool("no_on_submit", default=False):
            cmd_args.append("--no-on-submit")
        if not _bool("dry_run", default=False):
            cmd_args.append("--apply")

        out = io.StringIO()
        try:
            call_command("export_ld_recaps_to_sheet", *cmd_args, stdout=out)
        except Exception as exc:
            logger.exception("export-ld-recaps cron failed")
            return JsonResponse(
                {"ok": False, "error": "command-failed", "exception": _concise_exc(exc), "log": out.getvalue()},
                status=500,
            )
        return JsonResponse({"ok": True, "log": out.getvalue()})

    def post(self, request: HttpRequest) -> HttpResponse:
        return self._run(request)

    def get(self, request: HttpRequest) -> HttpResponse:
        return self._run(request)


@method_decorator(csrf_exempt, name="dispatch")
class LdDataCensusView(View):
    """GET/POST `/internal/cron/ld-data-census` — READ-ONLY, writes nothing.

    Reports the Liquid Death tenant's Spark data coverage so the Summary
    year-split + recaps export can be designed against real data: CustomRecap
    counts by event-year and month, Event counts by year, and the distinct
    recap field names (future recaps-tab columns). Secret-gated.
    """

    def _run(self, request: HttpRequest) -> HttpResponse:
        deny = _check_secret(request)
        if deny is not None:
            return deny

        from collections import Counter

        from events.models import Event
        from recaps.models import CustomField, CustomRecap
        from recaps.pdf import _event_date
        from tenants.models import Tenant

        slug = (
            request.GET.get("tenant_slug") or request.POST.get("tenant_slug") or ""
        ).strip() or "ighn-liquid-death"
        tenant = Tenant.objects.filter(slug=slug).first()
        if tenant is None:
            tenant = Tenant.objects.filter(name__icontains="liquid death").first()
        if tenant is None:
            return JsonResponse({"ok": False, "error": "no-tenant", "slug": slug}, status=404)

        recap_year: Counter = Counter()
        recap_month: Counter = Counter()
        total_recaps = 0
        for r in CustomRecap.objects.filter(tenant=tenant).select_related("event"):
            total_recaps += 1
            d = _event_date(r)
            if d:
                recap_year[d.year] += 1
                recap_month[d.strftime("%Y-%m")] += 1
            else:
                recap_year["no-date"] += 1

        event_year: Counter = Counter()
        for e in Event.objects.filter(tenant=tenant).only("date"):
            event_year[e.date.year if e.date else "no-date"] += 1

        field_names = sorted(
            set(
                CustomField.objects.filter(
                    custom_recap_template__tenant=tenant
                ).values_list("name", flat=True)
            )
        )

        def _ord(d: dict) -> dict:
            return {str(k): v for k, v in sorted(d.items(), key=lambda kv: str(kv[0]))}

        return JsonResponse(
            {
                "ok": True,
                "tenant": tenant.slug,
                "total_recaps": total_recaps,
                "recaps_by_year": _ord(recap_year),
                "recaps_by_month": _ord(recap_month),
                "events_by_year": _ord(event_year),
                "recap_field_count": len(field_names),
                "recap_field_names": field_names,
            }
        )

    def post(self, request: HttpRequest) -> HttpResponse:
        return self._run(request)

    def get(self, request: HttpRequest) -> HttpResponse:
        return self._run(request)


def _registered_views() -> dict[str, Any]:
    """Map URL path → view class. Lets `digest/urls.py` mount these
    without each one being re-exported explicitly.
    """
    return {
        "send-admin-digest": SendAdminDigestView,
        "send-executive-summary": SendExecutiveSummaryView,
        "send-new-gig-digest": SendNewGigDigestView,
        "send-recap-reminders": SendRecapRemindersView,
        "send-open-shift-alerts": SendOpenShiftAlertsView,
        "activation-reminders": SendActivationRemindersView,
        "recap-nudges": SendRecapNudgesView,
        "send-payment-notifications": SendPaymentNotificationsView,
        "send-document-expiry-reminders": SendDocumentExpiryRemindersView,
        "send-scheduled-client-reports": SendScheduledClientReportsView,
        "send-client-weekly-digest": SendClientWeeklyDigestView,
        "repair-approved-event-status": RepairApprovedEventStatusView,
        "repair-missing-events-for-approved-requests": (
            RepairMissingEventsForApprovedRequestsView
        ),
        "repair-event-dates": RepairEventDatesView,
        "backfill-event-coordinates": BackfillEventCoordinatesView,
        "backfill-ambassador-coordinates": BackfillAmbassadorCoordinatesView,
        "backfill-request-rmm-routing": BackfillRequestRmmRoutingView,
        "repair-request-activation-time": RepairRequestActivationTimeView,
        "backfill-girlbeer-receipts": BackfillGirlBeerReceiptsView,
        "audit-tenant-onboarding": AuditTenantOnboardingView,
        "audit-tenant-consumers": AuditTenantConsumersView,
        "dedupe-skills": DedupeSkillsView,
        "shift-confirmations": SendShiftConfirmationsView,
        "provision-review-ambassador": ProvisionReviewAmbassadorView,
        "import-event-schedule": ImportEventScheduleView,
        "verify-user": VerifyUserView,
        "set-tenant-event-types": SetTenantEventTypesView,
        "set-custom-recap-field": SetCustomRecapFieldView,
        "export-recaps-to-sheet": ExportRecapsToSheetView,
        "export-ld-summary": ExportLdSummaryView,
        "describe-sheet-tabs": DescribeSheetTabsView,
        "backfill-ld-master-tracker": BackfillLdMasterTrackerView,
        "ld-data-census": LdDataCensusView,
        "export-ld-recaps": ExportLdRecapsView,
    }
