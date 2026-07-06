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
class SendAmbassadorJobRemindersView(View):
    """POST `/internal/cron/ambassador-job-reminders`.

    Fires `send_ambassador_job_reminders` — the four per-shift BA reminders
    (24h / 3h / 15-min-before / 15-min-after-end), deduped via the
    AmbassadorJob.reminder_*_sent_at columns. Replaces the dead django-rq
    scheduled reminders (no rqscheduler in prod); sends INLINE, no worker
    needed. Meant for a frequent GHA cron (~every 10 min).

    Params (optional):
      - dry_run: "1"/"true"/"yes" — report per-reminder counts, send nothing.
      - end_lookback_minutes: int (default 120) — the after-end reminder only
        fires for shifts that ended within this window (first-run/gap safety).
    """

    def post(self, request: HttpRequest) -> HttpResponse:
        deny = _check_secret(request)
        if deny is not None:
            return deny

        def _bool(name: str, default: bool = False) -> bool:
            raw = (request.GET.get(name) or request.POST.get(name) or "").lower()
            return raw in ("1", "true", "yes", "on") if raw else default

        def _int(name: str, default: int) -> int | None:
            raw = request.GET.get(name) or request.POST.get(name)
            if raw is None or raw == "":
                return default
            try:
                return int(raw)
            except ValueError:
                return None

        dry_run = _bool("dry_run", default=False)
        end_lookback = _int("end_lookback_minutes", 120)
        if end_lookback is None:
            return JsonResponse(
                {"ok": False, "error": "end_lookback_minutes must be an integer"},
                status=400,
            )

        cmd_args: list[str] = ["--end-lookback-minutes", str(end_lookback)]
        if dry_run:
            cmd_args.append("--dry-run")

        out = io.StringIO()
        try:
            call_command("send_ambassador_job_reminders", *cmd_args, stdout=out)
        except Exception as exc:  # noqa: BLE001 — surface to caller
            logger.exception("Ambassador job reminders cron failed")
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
            {"ok": True, "dry_run": dry_run, "log": out.getvalue()}
        )

    def get(self, request: HttpRequest) -> HttpResponse:
        deny = _check_secret(request)
        if deny is not None:
            return deny
        return JsonResponse(
            {"ok": True, "endpoint": "ambassador-job-reminders"}
        )


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
class ApplyGirlBeerBrandingView(View):
    """POST `/internal/cron/apply-girl-beer-branding`.

    Fires `apply_girl_beer_branding` — sets Tenant.image (logo), a light
    TenantTheme (brand purple #830DFF, sampled from the live girlbeer.com
    stylesheet), and the "girlbeer" ReceiptCampaign's hero_image /
    product_image, all downloaded from the official girlbeer.com storefront.
    Requested by Kyle to make /c/girlbeer look like the real Girl Beer
    brand instead of Spark's neutral default. One-off manual apply —
    idempotent (each of the three writes skips if already set, unless
    `force=true`).

    SAFE — DRY-RUN IS THE DEFAULT. dry_run=true → no writes, just narrates
    what would happen. Set dry_run=false to apply.

    Body / query params (all optional):
      - dry_run: "1"/"true"/"yes" — preview only (default ON → dry-run).
      - force: "1"/"true"/"yes" — re-download + overwrite already-set values.
    """

    def post(self, request: HttpRequest) -> HttpResponse:
        deny = _check_secret(request)
        if deny is not None:
            return deny

        dry_run_raw = (
            request.GET.get("dry_run") or request.POST.get("dry_run") or "1"
        ).lower()
        dry_run = dry_run_raw in ("1", "true", "yes", "on")
        force_raw = (
            request.GET.get("force") or request.POST.get("force") or ""
        ).lower()
        force = force_raw in ("1", "true", "yes", "on")

        out = io.StringIO()
        try:
            call_command(
                "apply_girl_beer_branding",
                stdout=out,
                dry_run=dry_run,
                force=force,
            )
        except Exception as exc:  # noqa: BLE001 — surface to caller
            logger.exception("apply-girl-beer-branding cron failed")
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
            {"ok": True, "dry_run": dry_run, "force": force, "log": out.getvalue()}
        )

    def get(self, request: HttpRequest) -> HttpResponse:
        deny = _check_secret(request)
        if deny is not None:
            return deny
        return JsonResponse({"ok": True, "endpoint": "apply-girl-beer-branding"})


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

        # Opt-in: create the tenant when the schedule is for a brand-new
        # client (name + slug, owned by the command's owner-email). Happens
        # even on dry-run — same idempotent-setup rationale as EventType.
        create_tenant = _bool("create_tenant", default=False)

        cmd_kwargs: dict[str, Any] = {"schedule": schedule, "commit": execute}
        if tenant_name:
            cmd_kwargs["tenant_name"] = tenant_name
        if create_tenant:
            cmd_kwargs["create_tenant"] = True

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
class DemoteAdminView(View):
    """POST `/internal/cron/demote-admin`.

    Strip platform-admin access from a user by email: clears `is_staff` and
    `is_superuser` (this is what removes Django `/admin/` access). The
    in-app GraphQL admin surfaces are removed in CODE via
    `utils.graphql.permissions.IGNITE_ADMIN_EXCLUDE` (the @igniteproductions.co
    domain otherwise auto-grants admin), so this endpoint handles only the
    DB-flag side.

    A `role` of "spark-admin" is reassigned to a non-admin role (the FK is NOT
    NULL, so we reassign rather than clear). Default target is "client", the
    only safe demotion target — the inverted `role == "client"` gates restrict
    it to the user's own tenant (a removed admin has none → no data), whereas
    "ambassador" would fall through those gates to the admin branch. A
    non-spark-admin role is left as-is.

    SAFE — DRY-RUN IS THE DEFAULT. A plain trigger only reports the user's
    current flags; pass `apply=true` to write.

    Body / query params:
      - email: REQUIRED — the account to demote.
      - apply: "1"/"true"/"yes" — apply. Default OFF → dry-run.
      - demote_to: target role slug for a spark-admin (default "client").
    """

    def post(self, request: HttpRequest) -> HttpResponse:
        deny = _check_secret(request)
        if deny is not None:
            return deny

        def _bool(name: str, default: bool = False) -> bool:
            raw = (request.GET.get(name) or request.POST.get(name) or "").lower()
            return raw in ("1", "true", "yes", "on") if raw else default

        email = (request.GET.get("email") or request.POST.get("email") or "").strip()
        if not email:
            return JsonResponse({"ok": False, "error": "email-required"}, status=400)
        apply = _bool("apply", default=False)

        from django.contrib.auth import get_user_model

        User = get_user_model()
        user = (
            User.objects.select_related("role").filter(email__iexact=email).first()
        )
        if user is None:
            return JsonResponse(
                {"ok": False, "error": "user-not-found", "email": email}, status=404
            )

        from tenants.models import Role

        # Target non-admin role for a spark-admin (the role FK is NOT NULL, so
        # we reassign rather than clear). "client" is the only safe demotion
        # target: the inverted `role == "client"` gates restrict it to the
        # user's own tenant (a removed admin has none → no data), whereas
        # "ambassador" would fall through those gates to the admin branch.
        demote_to = (
            request.GET.get("demote_to") or request.POST.get("demote_to") or "client"
        ).strip().lower()
        cur_role = getattr(user.role, "slug", None)

        before = {
            "email": user.email,
            "is_staff": user.is_staff,
            "is_superuser": user.is_superuser,
            "role": cur_role,
            "is_active": user.is_active,
        }
        will_reassign_role = cur_role == Role.SPARK_ADMIN_SLUG
        if not apply:
            return JsonResponse(
                {
                    "ok": True,
                    "dry_run": True,
                    "before": before,
                    "planned": {
                        "is_staff": False,
                        "is_superuser": False,
                        "role": demote_to if will_reassign_role else cur_role,
                    },
                    "note": (
                        "clears Django /admin/ flags; reassigns a spark-admin "
                        "role to the demote_to role; @ignite domain admin is "
                        "cleared in-app via IGNITE_ADMIN_EXCLUDE"
                    ),
                }
            )

        changed: list[str] = []
        if user.is_staff:
            user.is_staff = False
            changed.append("is_staff")
        if user.is_superuser:
            user.is_superuser = False
            changed.append("is_superuser")
        if will_reassign_role:
            target = Role.objects.filter(slug=demote_to).first()
            if target is None:
                return JsonResponse(
                    {"ok": False, "error": "demote-to-role-not-found",
                     "demote_to": demote_to, "before": before}, status=422
                )
            user.role = target
            changed.append("role")
        if changed:
            user.save(update_fields=changed)

        fresh = User.objects.select_related("role").filter(pk=user.pk).first()
        after = {
            "email": fresh.email,
            "is_staff": fresh.is_staff,
            "is_superuser": fresh.is_superuser,
            "role": getattr(fresh.role, "slug", None),
            "is_active": fresh.is_active,
        }
        return JsonResponse(
            {"ok": True, "applied": True, "changed_fields": changed,
             "before": before, "after": after}
        )

    def get(self, request: HttpRequest) -> HttpResponse:
        deny = _check_secret(request)
        if deny is not None:
            return deny
        return JsonResponse({"ok": True, "endpoint": "demote-admin"})


@method_decorator(csrf_exempt, name="dispatch")
class EnsureAmbassadorProfileView(View):
    """POST `/internal/cron/ensure-ambassador-profile`.

    Give an EXISTING user a BA (Ambassador) profile so the mobile BA flows
    ("my gigs / today", apply, accept shift offers — all keyed on
    `Ambassador.objects.get(user=...)`) resolve for them. Used to let a
    staff/admin use their own account as a test BA.

    SAFE: only `get_or_create`s the Ambassador row (+ reactivates it if it
    was soft-disabled). It NEVER touches the user's password, role, staff
    flags, or verification — unlike seed_review_ambassador, which repurposes
    an account into a pure reviewer BA. So an admin keeps their admin access
    and login; they just additionally gain a BA profile.

    Body / query params:
      - email: REQUIRED — the existing user to give a BA profile.
      - remove: optional ("1"/"true") — REMOVE the user's BA profile instead.
        Hard-deletes the Ambassador row when it has no protected children
        (the common case for a never-used test profile); if FK-protected
        because it carries bookings, it soft-disables (is_active=False).
        Still NEVER touches the user's password, role, or staff flags.
    """

    def post(self, request: HttpRequest) -> HttpResponse:
        deny = _check_secret(request)
        if deny is not None:
            return deny

        def _bool(name: str) -> bool:
            raw = (request.GET.get(name) or request.POST.get(name) or "").lower()
            return raw in ("1", "true", "yes", "on")

        email = (
            request.GET.get("email") or request.POST.get("email") or ""
        ).strip()
        if not email:
            return JsonResponse({"ok": False, "error": "email-required"}, status=400)

        from django.contrib.auth import get_user_model
        from django.db import transaction
        from ambassadors.models import Ambassador

        User = get_user_model()
        user = User.objects.filter(email__iexact=email).first()
        if user is None:
            return JsonResponse(
                {"ok": False, "error": "user-not-found", "email": email}, status=404
            )

        # Removal mode: take away the (stray) BA profile.
        if _bool("remove"):
            amb = Ambassador.objects.filter(user=user).first()
            if amb is None:
                return JsonResponse(
                    {
                        "ok": True,
                        "email": user.email,
                        "removed": False,
                        "note": "no ambassador profile to remove",
                        "role_unchanged": getattr(user.role, "slug", None),
                    }
                )
            amb_uuid = str(amb.uuid)
            try:
                with transaction.atomic():
                    amb.delete()
                action = "deleted"
            except Exception as exc:  # noqa: BLE001 — FK-protected → soft-disable
                logger.warning(
                    "Hard-delete of ambassador %s blocked (%s); deactivating",
                    amb_uuid, exc,
                )
                amb.is_active = False
                amb.save(update_fields=["is_active"])
                action = "deactivated"
            return JsonResponse(
                {
                    "ok": True,
                    "email": user.email,
                    "removed": True,
                    "action": action,
                    "ambassador_uuid": amb_uuid,
                    "role_unchanged": getattr(user.role, "slug", None),
                    "note": "password/role/flags untouched",
                }
            )

        amb, created = Ambassador.objects.get_or_create(
            user=user,
            defaults={"created_by": user, "updated_by": user, "is_active": True},
        )
        reactivated = False
        if not created and not amb.is_active:
            amb.is_active = True
            amb.save(update_fields=["is_active"])
            reactivated = True

        return JsonResponse(
            {
                "ok": True,
                "email": user.email,
                "ambassador_uuid": str(amb.uuid),
                "created": created,
                "reactivated": reactivated,
                "already_existed": (not created and not reactivated),
                "role_unchanged": getattr(user.role, "slug", None),
                "note": "password/role/flags untouched",
            }
        )

    def get(self, request: HttpRequest) -> HttpResponse:
        deny = _check_secret(request)
        if deny is not None:
            return deny
        return JsonResponse({"ok": True, "endpoint": "ensure-ambassador-profile"})


@method_decorator(csrf_exempt, name="dispatch")
class SetEventDateView(View):
    """POST `/internal/cron/set-event-date`.

    Move a single Event to a new calendar date (default: TODAY, server-local),
    shifting its ``start_time`` / ``end_time`` / ``date`` by the same whole-day
    delta so the time-of-day and duration are preserved. The mobile BA shift
    screens (my_active_shifts / my_upcoming_shifts) key off ``event.start_time``
    / ``event.date`` — so this is how you make an accepted booking appear on a
    BA's Today/Upcoming (handy for testing, or to fix a stale gig date).

    Resolve the target event by exactly ONE of:
      - event_uuid: the Event uuid directly, OR
      - job_uuid:   a Job uuid → its linked event, OR
      - ba_email:   a BA's email → their single APPROVED booking's event
                    (refuses + lists them if they have 0 or >1).

    Params:
      - date: YYYY-MM-DD (optional; default = today, server-local).
      - execute: "1"/"true" to write. DRY-RUN by default (reports only).
    """

    def _resolve_event(self, request: HttpRequest):
        """Return (event, meta_dict) or (None, error_dict)."""
        from events.models import Event
        from jobs.models import Job
        from ambassadors.models import Ambassador, AmbassadorEvent

        def _p(name: str) -> str:
            return (request.GET.get(name) or request.POST.get(name) or "").strip()

        event_uuid = _p("event_uuid")
        job_uuid = _p("job_uuid")
        ba_email = _p("ba_email")

        if event_uuid:
            ev = Event.objects.filter(uuid=event_uuid).first()
            if ev is None:
                return None, {"error": "event-not-found", "event_uuid": event_uuid}
            return ev, {"resolved_by": "event_uuid"}

        if job_uuid:
            job = Job.objects.filter(uuid=job_uuid).select_related("event").first()
            if job is None:
                return None, {"error": "job-not-found", "job_uuid": job_uuid}
            if not job.event_id:
                return None, {"error": "job-has-no-event", "job_uuid": job_uuid}
            return job.event, {"resolved_by": "job_uuid"}

        if ba_email:
            amb = Ambassador.objects.filter(user__email__iexact=ba_email).first()
            if amb is None:
                return None, {"error": "ba-not-found", "ba_email": ba_email}
            bookings = list(
                AmbassadorEvent.objects.filter(
                    ambassador=amb, is_approved=True
                ).select_related("event")
            )
            events = [b.event for b in bookings if b.event_id]
            if len(events) == 0:
                return None, {"error": "no-approved-bookings", "ba_email": ba_email}
            if len(events) > 1:
                return None, {
                    "error": "multiple-approved-bookings",
                    "ba_email": ba_email,
                    "events": [
                        {"uuid": str(e.uuid), "name": e.name} for e in events
                    ],
                    "hint": "pass event_uuid to pick one",
                }
            return events[0], {"resolved_by": "ba_email"}

        return None, {"error": "no-target", "hint": "pass event_uuid, job_uuid, or ba_email"}

    def post(self, request: HttpRequest) -> HttpResponse:
        deny = _check_secret(request)
        if deny is not None:
            return deny

        from datetime import datetime, time as dtime, timedelta
        from django.utils import timezone
        from ambassadors.models import AmbassadorEvent

        def _bool(name: str) -> bool:
            raw = (request.GET.get(name) or request.POST.get(name) or "").lower()
            return raw in ("1", "true", "yes", "on")

        execute = _bool("execute")

        date_raw = (
            request.GET.get("date") or request.POST.get("date") or ""
        ).strip()
        if date_raw:
            try:
                target = datetime.strptime(date_raw, "%Y-%m-%d").date()
            except ValueError:
                return JsonResponse(
                    {"ok": False, "error": "bad-date", "date": date_raw,
                     "hint": "use YYYY-MM-DD"},
                    status=400,
                )
        else:
            target = timezone.localdate()

        event, meta = self._resolve_event(request)
        if event is None:
            return JsonResponse({"ok": False, **meta}, status=400)

        # Anchor the shift on the event's current start (fall back to date).
        anchor = event.start_time or event.date
        before = {
            "start_time": event.start_time.isoformat() if event.start_time else None,
            "end_time": event.end_time.isoformat() if event.end_time else None,
            "date": event.date.isoformat() if event.date else None,
        }

        changed_fields: list[str] = []
        if anchor is None:
            # No basis to shift — seat the event at noon on the target day.
            seat = timezone.make_aware(datetime.combine(target, dtime(12, 0)))
            event.start_time = seat
            event.date = seat
            changed_fields = ["start_time", "date"]
            delta_days = 0
        else:
            old_local = timezone.localdate(anchor)
            delta_days = (target - old_local).days
            for f in ("start_time", "end_time", "date"):
                v = getattr(event, f, None)
                if v is not None:
                    setattr(event, f, v + timedelta(days=delta_days))
                    changed_fields.append(f)

        after = {
            "start_time": event.start_time.isoformat() if event.start_time else None,
            "end_time": event.end_time.isoformat() if event.end_time else None,
            "date": event.date.isoformat() if event.date else None,
        }

        # Who's booked on it (so the caller can confirm it's the right event).
        bookings = [
            {
                "ba_email": getattr(getattr(b.ambassador, "user", None), "email", None),
                "is_approved": b.is_approved,
            }
            for b in AmbassadorEvent.objects.filter(event=event).select_related(
                "ambassador__user"
            )
        ]

        if execute and changed_fields:
            event.save(update_fields=changed_fields)

        return JsonResponse(
            {
                "ok": True,
                "executed": execute,
                "resolved_by": meta.get("resolved_by"),
                "event_uuid": str(event.uuid),
                "event_name": event.name,
                "target_date": target.isoformat(),
                "delta_days": delta_days,
                "before": before,
                "after": after,
                "bookings": bookings,
                "note": (
                    "wrote changes" if (execute and changed_fields)
                    else "dry-run — pass execute=true to write"
                ),
            }
        )

    def get(self, request: HttpRequest) -> HttpResponse:
        deny = _check_secret(request)
        if deny is not None:
            return deny
        return JsonResponse({"ok": True, "endpoint": "set-event-date"})


@method_decorator(csrf_exempt, name="dispatch")
class BookAmbassadorOnEventView(View):
    """POST `/internal/cron/book-ambassador-on-event`.

    Create (or approve) an ``AmbassadorEvent`` linking a BA to an Event so the
    booking shows on that BA's mobile shift screens (my_active_shifts /
    my_upcoming_shifts read APPROVED AmbassadorEvents). This mirrors the
    accept-flow's `_confirm_booking` helper (jobs/mutations.py): it
    get_or_creates the row with is_approved=True, and flips an existing
    unapproved row to approved.

    Use this when an admin "assigned a BA to a gig" but no booking landed on
    the event (e.g. the assignment only created a job-level row, or the
    approve path that calls `_confirm_booking` never fired), so the gig is
    missing from the BA's app.

    Resolve the event by ONE of: event_uuid OR job_uuid → its linked event.
    Resolve the BA by ba_email.

    Params:
      - ba_email: REQUIRED — the BA to book.
      - event_uuid OR job_uuid: REQUIRED — which event.
      - execute: "1"/"true" to write. DRY-RUN by default.
    """

    def post(self, request: HttpRequest) -> HttpResponse:
        deny = _check_secret(request)
        if deny is not None:
            return deny

        from django.utils import timezone
        from events.models import Event
        from jobs.models import Job
        from ambassadors.models import Ambassador, AmbassadorEvent

        def _p(name: str) -> str:
            return (request.GET.get(name) or request.POST.get(name) or "").strip()

        def _bool(name: str) -> bool:
            return _p(name).lower() in ("1", "true", "yes", "on")

        execute = _bool("execute")
        ba_email = _p("ba_email")
        event_uuid = _p("event_uuid")
        job_uuid = _p("job_uuid")

        if not ba_email:
            return JsonResponse(
                {"ok": False, "error": "ba_email-required"}, status=400
            )

        # Resolve the event.
        event = None
        if event_uuid:
            event = Event.objects.filter(uuid=event_uuid).first()
            if event is None:
                return JsonResponse(
                    {"ok": False, "error": "event-not-found",
                     "event_uuid": event_uuid}, status=400
                )
        elif job_uuid:
            job = Job.objects.filter(uuid=job_uuid).select_related("event").first()
            if job is None:
                return JsonResponse(
                    {"ok": False, "error": "job-not-found",
                     "job_uuid": job_uuid}, status=400
                )
            if not job.event_id:
                return JsonResponse(
                    {"ok": False, "error": "job-has-no-event",
                     "job_uuid": job_uuid}, status=400
                )
            event = job.event
        else:
            return JsonResponse(
                {"ok": False, "error": "no-event-target",
                 "hint": "pass event_uuid or job_uuid"}, status=400
            )

        amb = Ambassador.objects.filter(user__email__iexact=ba_email).first()
        if amb is None:
            return JsonResponse(
                {"ok": False, "error": "ba-not-found", "ba_email": ba_email},
                status=400,
            )

        existing = AmbassadorEvent.objects.filter(
            ambassador=amb, event=event
        ).first()
        before = {
            "exists": existing is not None,
            "is_approved": getattr(existing, "is_approved", None),
        }

        action = "noop"
        if not execute:
            action = "would-create" if existing is None else (
                "already-approved" if existing.is_approved else "would-approve"
            )
        else:
            if existing is None:
                AmbassadorEvent.objects.create(
                    ambassador=amb,
                    event=event,
                    tenant_id=event.tenant_id,
                    is_approved=True,
                    created_by=amb.user,
                    updated_by=amb.user,
                )
                action = "created"
            elif not existing.is_approved:
                existing.is_approved = True
                existing.updated_by = amb.user
                existing.save(
                    update_fields=["is_approved", "updated_by", "updated_at"]
                )
                action = "approved"
            else:
                action = "already-approved"

        return JsonResponse(
            {
                "ok": True,
                "executed": execute,
                "action": action,
                "ba_email": ba_email,
                "ambassador_uuid": str(amb.uuid),
                "event_uuid": str(event.uuid),
                "event_name": event.name,
                "event_start_time": (
                    event.start_time.isoformat() if event.start_time else None
                ),
                "before": before,
                "note": (
                    "wrote booking" if execute
                    else "dry-run — pass execute=true to write"
                ),
            }
        )

    def get(self, request: HttpRequest) -> HttpResponse:
        deny = _check_secret(request)
        if deny is not None:
            return deny
        return JsonResponse({"ok": True, "endpoint": "book-ambassador-on-event"})


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
class ExportGirlbeerSummaryView(View):
    """POST `/internal/cron/export-girlbeer-summary`.

    Rebuilds the Girl Beer "Summary" dashboard tab from Spark recaps as plain
    VALUES (KPIs + per-ambassador / date / store / flavor / age), so it never
    #REF!s and stays current. On apply, also persists
    `tenant.recap_summary_tab_name` so the daily recap export keeps it fresh.

    Params (query or POST): tenant_slug (default "girl-beer"),
    tab (default "Summary"), sheet_url, dry_run.
    """

    def _run(self, request: HttpRequest) -> HttpResponse:
        deny = _check_secret(request)
        if deny is not None:
            return deny

        from recaps.girlbeer_summary_export import write_girlbeer_summary
        from tenants.models import Tenant

        def _get(name: str) -> str:
            return (request.GET.get(name) or request.POST.get(name) or "").strip()

        def _bool(name: str, default: bool = False) -> bool:
            raw = _get(name).lower()
            return raw in ("1", "true", "yes", "on") if raw else default

        slug = _get("tenant_slug") or "girl-beer"
        tab = _get("tab") or "Summary"
        sheet_url = _get("sheet_url")
        dry_run = _bool("dry_run", default=False)

        tenant = Tenant.objects.filter(slug=slug).first()
        if tenant is None:
            return JsonResponse({"ok": False, "error": "tenant-not-found", "slug": slug}, status=404)

        # On a real run, persist the opt-in so the daily export keeps the
        # Summary fresh going forward.
        if not dry_run and (getattr(tenant, "recap_summary_tab_name", "") or "") != tab:
            tenant.recap_summary_tab_name = tab
            tenant.save(update_fields=["recap_summary_tab_name"])

        try:
            result = write_girlbeer_summary(
                tenant, tab=tab, sheet_url=sheet_url or None, dry_run=dry_run
            )
        except Exception as exc:
            logger.exception("export-girlbeer-summary cron failed")
            return JsonResponse(
                {"ok": False, "error": "command-failed", "exception": _concise_exc(exc)},
                status=500,
            )
        return JsonResponse({"ok": result.get("ok", False), "tenant_slug": slug,
                             "dry_run": dry_run, "result": result})

    def post(self, request: HttpRequest) -> HttpResponse:
        return self._run(request)

    def get(self, request: HttpRequest) -> HttpResponse:
        return self._run(request)


@method_decorator(csrf_exempt, name="dispatch")
class BackfillRecapRetailersView(View):
    """POST `/internal/cron/backfill-recap-retailers`.

    Fires `backfill_recap_retailers` — sets `CustomRecap.retailer` on specific
    recaps whose Retailer was blank (so they grouped by city in the summary) or
    wrong, so the "PERFORMANCE BY RETAILER" table folds them into the right
    store. Per-recap + idempotent; the shared Event is untouched.

    SAFE — DRY-RUN by default; only `apply=true` (alias execute=true) writes.

    Params (query or POST, all optional): apply/execute, tenant_slug
    (default girl-beer), targets_json (override the built-in 4 Girl Beer fixes).
    """

    def _run(self, request: HttpRequest) -> HttpResponse:
        deny = _check_secret(request)
        if deny is not None:
            return deny

        def _bool(name: str, default: bool = False) -> bool:
            raw = (request.GET.get(name) or request.POST.get(name) or "").lower()
            return raw in ("1", "true", "yes", "on") if raw else default

        def _str(name: str):
            return request.GET.get(name) or request.POST.get(name) or None

        apply = _bool("apply", default=False) or _bool("execute", default=False)
        kwargs = {"apply": apply, "tenant_slug": _str("tenant_slug") or "girl-beer"}
        targets_json = _str("targets_json")
        if targets_json:
            kwargs["targets_json"] = targets_json

        out = io.StringIO()
        try:
            call_command("backfill_recap_retailers", stdout=out, **kwargs)
        except Exception as exc:  # noqa: BLE001 — surface to caller
            logger.exception("backfill-recap-retailers cron failed")
            return JsonResponse(
                {"ok": False, "error": "command-failed", "detail": str(exc),
                 "log": out.getvalue()},
                status=500,
            )
        return JsonResponse(
            {"ok": True, "applied": apply, "tenant_slug": kwargs["tenant_slug"],
             "log": out.getvalue()}
        )

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
        for flag in ("sheet_url", "tenant_slug", "peek_tab", "peek_rows",
                     "peek_render", "peek_start"):
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
class FixLdKpiTotalsView(View):
    """POST `/internal/cron/fix-ld-kpi-totals`.

    One-off: widen the LD RMM KPI workbook's per-tab annual Total Cans
    Sampled / Total Sales formulas to include the "Others" column (they
    were written before that column existed and stop one column short).
    Dry-run by default; pass apply=1 to write. Params: sheet_url, tabs
    (comma-separated), apply. See fix_ld_kpi_totals for details.
    """

    def _run(self, request: HttpRequest) -> HttpResponse:
        deny = _check_secret(request)
        if deny is not None:
            return deny

        def _get(name: str) -> str:
            return (request.GET.get(name) or request.POST.get(name) or "").strip()

        apply = _get("apply").lower() in ("1", "true", "yes", "on")
        cmd_args: list[str] = []
        for flag in ("sheet_url", "tabs"):
            val = _get(flag)
            if val:
                cmd_args += [f"--{flag.replace('_', '-')}", val]
        if apply:
            cmd_args.append("--apply")

        out = io.StringIO()
        try:
            call_command("fix_ld_kpi_totals", *cmd_args, stdout=out)
        except Exception as exc:
            logger.exception("fix-ld-kpi-totals cron failed")
            return JsonResponse(
                {"ok": False, "error": "command-failed",
                 "exception": _concise_exc(exc), "log": out.getvalue()},
                status=500,
            )
        return JsonResponse({"ok": True, "apply": apply, "log": out.getvalue()})

    def post(self, request: HttpRequest) -> HttpResponse:
        return self._run(request)

    def get(self, request: HttpRequest) -> HttpResponse:
        return self._run(request)


@method_decorator(csrf_exempt, name="dispatch")
class ResendBaWelcomeView(View):
    """POST `/internal/cron/resend-ba-welcome`.

    Reset an EXISTING user onto admin-created-BA rails (new temp password
    with forced change, verified + active, active Ambassador profile) and
    re-send the "Welcome to Spark by Ignite" email with the app buttons —
    for BAs whose account predated a staffing run so the bulk path skipped
    their welcome email. Dry-run (default) prints last_login/password state
    so an actively-used account isn't blindly reset. Params: email, execute.
    """

    def _run(self, request: HttpRequest) -> HttpResponse:
        deny = _check_secret(request)
        if deny is not None:
            return deny

        def _get(name: str) -> str:
            return (request.GET.get(name) or request.POST.get(name) or "").strip()

        execute = _get("execute").lower() in ("1", "true", "yes", "on")
        email = _get("email")
        if not email:
            return JsonResponse({"ok": False, "error": "email-required"}, status=400)
        cmd_args = ["--email", email] + (["--apply"] if execute else [])

        out = io.StringIO()
        try:
            call_command("resend_ba_welcome", *cmd_args, stdout=out)
        except Exception as exc:
            logger.exception("resend-ba-welcome cron failed")
            return JsonResponse(
                {"ok": False, "error": "command-failed",
                 "exception": _concise_exc(exc), "log": out.getvalue()},
                status=500,
            )
        return JsonResponse({"ok": True, "execute": execute, "log": out.getvalue()})

    def post(self, request: HttpRequest) -> HttpResponse:
        return self._run(request)

    def get(self, request: HttpRequest) -> HttpResponse:
        return self._run(request)


@method_decorator(csrf_exempt, name="dispatch")
class RetextTenantEventsView(View):
    """POST `/internal/cron/retext-tenant-events`.

    Literal find/replace in Event.notes + Request.notes + Job.description
    for one tenant/event-type — for facts baked into imported text that
    changed later (Feel Free 5.5h → 5h billable). Dry-run by default with
    per-model match counts. Params: tenant_name, event_type, find, replace,
    execute. See retext_tenant_events.
    """

    def _run(self, request: HttpRequest) -> HttpResponse:
        deny = _check_secret(request)
        if deny is not None:
            return deny

        def _get(name: str) -> str:
            return (request.GET.get(name) or request.POST.get(name) or "").strip()

        execute = _get("execute").lower() in ("1", "true", "yes", "on")
        cmd_args: list[str] = []
        for flag in ("tenant_name", "event_type", "find", "replace"):
            val = _get(flag)
            if val:
                cmd_args += [f"--{flag.replace('_', '-')}", val]
        if execute:
            cmd_args.append("--apply")

        out = io.StringIO()
        try:
            call_command("retext_tenant_events", *cmd_args, stdout=out)
        except Exception as exc:
            logger.exception("retext-tenant-events cron failed")
            return JsonResponse(
                {"ok": False, "error": "command-failed",
                 "exception": _concise_exc(exc), "log": out.getvalue()},
                status=500,
            )
        return JsonResponse({"ok": True, "execute": execute, "log": out.getvalue()})

    def post(self, request: HttpRequest) -> HttpResponse:
        return self._run(request)

    def get(self, request: HttpRequest) -> HttpResponse:
        return self._run(request)


@method_decorator(csrf_exempt, name="dispatch")
class WeeklyMileageReportView(View):
    """POST `/internal/cron/weekly-mileage-report`.

    Emails the prior Mon-Sun week's mileage reimbursement summary + CSV
    (weekly_mileage_report command). Scheduled Mondays 13:00 UTC; params:
    week_ending (Sunday YYYY-MM-DD, for reruns), dry_run.
    """

    def _run(self, request: HttpRequest) -> HttpResponse:
        deny = _check_secret(request)
        if deny is not None:
            return deny

        def _get(name: str) -> str:
            return (request.GET.get(name) or request.POST.get(name) or "").strip()

        dry_run = _get("dry_run").lower() in ("1", "true", "yes", "on")
        cmd_args: list[str] = []
        week_ending = _get("week_ending")
        if week_ending:
            cmd_args += ["--week-ending", week_ending]
        if dry_run:
            cmd_args.append("--dry-run")

        out = io.StringIO()
        try:
            call_command("weekly_mileage_report", *cmd_args, stdout=out)
        except Exception as exc:
            logger.exception("weekly-mileage-report cron failed")
            return JsonResponse(
                {"ok": False, "error": "command-failed",
                 "exception": _concise_exc(exc), "log": out.getvalue()},
                status=500,
            )
        return JsonResponse({"ok": True, "dry_run": dry_run, "log": out.getvalue()})

    def post(self, request: HttpRequest) -> HttpResponse:
        return self._run(request)

    def get(self, request: HttpRequest) -> HttpResponse:
        return self._run(request)


@method_decorator(csrf_exempt, name="dispatch")
class AuditBaAccountsView(View):
    """POST `/internal/cron/audit-ba-accounts`.

    BA account health dump (audit_ba_accounts command): relay duplicates,
    per-name account sets, tenant recap/clock-in vitals, tenant-less
    census, recent backend errors. Params: names, tenant_slug,
    deactivate_empty_relay_dups, apply. Read-only unless apply=true.
    """

    def _run(self, request: HttpRequest) -> HttpResponse:
        deny = _check_secret(request)
        if deny is not None:
            return deny

        def _get(name: str) -> str:
            return (request.GET.get(name) or request.POST.get(name) or "").strip()

        def _flag(name: str) -> bool:
            return _get(name).lower() in ("1", "true", "yes", "on")

        cmd_args: list[str] = []
        if _get("names"):
            cmd_args += ["--names", _get("names")]
        if _get("tenant_slug"):
            cmd_args += ["--tenant-slug", _get("tenant_slug")]
        if _flag("deactivate_empty_relay_dups"):
            cmd_args.append("--deactivate-empty-relay-dups")
        if _flag("backfill_memberships"):
            cmd_args.append("--backfill-memberships")
        if _flag("apply"):
            cmd_args.append("--apply")

        out = io.StringIO()
        try:
            call_command("audit_ba_accounts", *cmd_args, stdout=out)
        except Exception as exc:
            logger.exception("audit-ba-accounts cron failed")
            return JsonResponse(
                {"ok": False, "error": "command-failed",
                 "exception": _concise_exc(exc), "log": out.getvalue()},
                status=500,
            )
        return JsonResponse({"ok": True, "log": out.getvalue()})

    def post(self, request: HttpRequest) -> HttpResponse:
        return self._run(request)

    def get(self, request: HttpRequest) -> HttpResponse:
        return self._run(request)


@method_decorator(csrf_exempt, name="dispatch")
class ActivationAutopilotView(View):
    """POST `/internal/cron/activation-autopilot`.

    Emails never-signed-in BAs with a shift in the next window (once each,
    fresh welcome + temp password) and digests the stragglers to the
    Ignite team (send_activation_autopilot command). Scheduled every 6h.
    Params: window_hours, dry_run.
    """

    def _run(self, request: HttpRequest) -> HttpResponse:
        deny = _check_secret(request)
        if deny is not None:
            return deny

        def _get(name: str) -> str:
            return (request.GET.get(name) or request.POST.get(name) or "").strip()

        dry_run = _get("dry_run").lower() in ("1", "true", "yes", "on")
        cmd_args: list[str] = []
        if _get("window_hours"):
            cmd_args += ["--window-hours", _get("window_hours")]
        if dry_run:
            cmd_args.append("--dry-run")

        out = io.StringIO()
        try:
            call_command("send_activation_autopilot", *cmd_args, stdout=out)
        except Exception as exc:
            logger.exception("activation-autopilot cron failed")
            return JsonResponse(
                {"ok": False, "error": "command-failed",
                 "exception": _concise_exc(exc), "log": out.getvalue()},
                status=500,
            )
        return JsonResponse({"ok": True, "dry_run": dry_run, "log": out.getvalue()})

    def post(self, request: HttpRequest) -> HttpResponse:
        return self._run(request)

    def get(self, request: HttpRequest) -> HttpResponse:
        return self._run(request)


@method_decorator(csrf_exempt, name="dispatch")
class RecapDataHealthView(View):
    """GET/POST `/internal/cron/recap-data-health`.

    Flags custom recaps whose parsed KPIs are implausible (conversion
    >100%, absurd consumer/unit counts) — catches bad numbers before they
    reach a client deck. Fires the `audit_recap_data_health` command;
    read-only. Params: tenant_slug, year, all_time, max_consumers,
    max_units, email.
    """

    def _run(self, request: HttpRequest) -> HttpResponse:
        deny = _check_secret(request)
        if deny is not None:
            return deny

        def _get(name: str) -> str:
            return (request.GET.get(name) or request.POST.get(name) or "").strip()

        def _flag(name: str) -> bool:
            return _get(name).lower() in ("1", "true", "yes", "on")

        cmd_args: list[str] = []
        if _get("tenant_slug"):
            cmd_args += ["--tenant-slug", _get("tenant_slug")]
        if _get("year"):
            cmd_args += ["--year", _get("year")]
        if _flag("all_time"):
            cmd_args.append("--all-time")
        if _get("max_consumers"):
            cmd_args += ["--max-consumers", _get("max_consumers")]
        if _get("max_units"):
            cmd_args += ["--max-units", _get("max_units")]
        # Default to emailing the digest unless explicitly disabled.
        if _get("email") == "" or _flag("email"):
            cmd_args.append("--email")

        out = io.StringIO()
        try:
            call_command("audit_recap_data_health", *cmd_args, stdout=out)
        except Exception as exc:
            logger.exception("recap-data-health cron failed")
            return JsonResponse(
                {"ok": False, "error": "command-failed",
                 "exception": _concise_exc(exc), "log": out.getvalue()},
                status=500,
            )
        return JsonResponse({"ok": True, "log": out.getvalue()})

    def post(self, request: HttpRequest) -> HttpResponse:
        return self._run(request)

    def get(self, request: HttpRequest) -> HttpResponse:
        return self._run(request)


@method_decorator(csrf_exempt, name="dispatch")
class StaffTenantEventsView(View):
    """POST `/internal/cron/staff-tenant-events`.

    Bulk-staff a tenant's imported events from a committed spec: ensure the
    BAs exist (NEW ones get the Welcome-to-Spark email with the app-store
    download buttons), create per-event Jobs at the spec's hourly rate, and
    book every group BA on every group event (AmbassadorJob + approved
    AmbassadorEvent — direct writes, no per-booking notification fan-out).
    Dry-run by default; pass execute=1 to write. Params: spec (default
    feel_free_staffing), owner_email, execute. See staff_tenant_events.
    """

    def _run(self, request: HttpRequest) -> HttpResponse:
        deny = _check_secret(request)
        if deny is not None:
            return deny

        def _get(name: str) -> str:
            return (request.GET.get(name) or request.POST.get(name) or "").strip()

        execute = _get("execute").lower() in ("1", "true", "yes", "on")
        cmd_args: list[str] = []
        for flag in ("spec", "owner_email"):
            val = _get(flag)
            if val:
                cmd_args += [f"--{flag.replace('_', '-')}", val]
        if execute:
            cmd_args.append("--apply")

        out = io.StringIO()
        try:
            call_command("staff_tenant_events", *cmd_args, stdout=out)
        except Exception as exc:
            logger.exception("staff-tenant-events cron failed")
            return JsonResponse(
                {"ok": False, "error": "command-failed",
                 "exception": _concise_exc(exc), "log": out.getvalue()},
                status=500,
            )
        return JsonResponse({"ok": True, "execute": execute, "log": out.getvalue()})

    def post(self, request: HttpRequest) -> HttpResponse:
        return self._run(request)

    def get(self, request: HttpRequest) -> HttpResponse:
        return self._run(request)


@method_decorator(csrf_exempt, name="dispatch")
class SetTenantMileageTrackingView(View):
    """POST `/internal/cron/set-tenant-mileage-tracking`.

    Bulk-toggle GPS mileage tracking (Event.track_mileage + mileage_rate)
    on a tenant's events — the per-gig admin panel doesn't scale to an
    imported season. Dry-run by default; pass execute=1 to write. Params:
    tenant_name (required), event_type, since_date, rate (default 0.725;
    'none' = miles only), disable, execute. See set_tenant_mileage_tracking.
    """

    def _run(self, request: HttpRequest) -> HttpResponse:
        deny = _check_secret(request)
        if deny is not None:
            return deny

        def _get(name: str) -> str:
            return (request.GET.get(name) or request.POST.get(name) or "").strip()

        def _flag(name: str) -> bool:
            return _get(name).lower() in ("1", "true", "yes", "on")

        execute = _flag("execute")
        cmd_args: list[str] = []
        for flag in ("tenant_name", "event_type", "since_date", "rate"):
            val = _get(flag)
            if val:
                cmd_args += [f"--{flag.replace('_', '-')}", val]
        if _flag("disable"):
            cmd_args.append("--off")
        if execute:
            cmd_args.append("--apply")

        out = io.StringIO()
        try:
            call_command("set_tenant_mileage_tracking", *cmd_args, stdout=out)
        except Exception as exc:
            logger.exception("set-tenant-mileage-tracking cron failed")
            return JsonResponse(
                {"ok": False, "error": "command-failed",
                 "exception": _concise_exc(exc), "log": out.getvalue()},
                status=500,
            )
        return JsonResponse({"ok": True, "execute": execute, "log": out.getvalue()})

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
        # Column layout: "ld_retail" = write only A–I in LD's client format
        # (the fix). The flag is persisted ONLY on execute, so a dry-run preview
        # never flips the live per-row mirror onto the LD path before review.
        layout = (_get("layout") or "ld_retail").strip()
        execute = _bool("execute", default=False)
        # Whether to ALSO write all existing requests now. Default off so
        # execute=1 just enables the layout going forward (the per-row mirror
        # picks it up on the next save/edit) without a historical bulk dump.
        backfill = _bool("backfill", default=False)
        # Optional scope for that backfill — e.g. only requests dated today
        # forward, or only a specific status (pending = submitted but not
        # yet approved), instead of the tenant's full history.
        since_date = _get("since_date")
        status_slug = _get("status_slug")
        # One-off pruning of a client's hand-entered duplicate rows (comma-
        # separated 1-based row numbers). Guarded in delete_ld_rows: never a
        # row carrying a Spark key, rows 2-40 only, ld_retail layout only.
        delete_rows = _get("delete_rows")

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
        # Persist the layout flag ONLY on execute — flipping it changes how the
        # live per-row mirror writes, so a dry-run stays a true no-op preview.
        if execute and layout and (tenant.master_tracker_layout or "") != layout:
            tenant.master_tracker_layout = layout
            changes["master_tracker_layout"] = layout
        if changes:
            tenant.save(update_fields=list(changes.keys()))

        # Going-forward only (execute, no backfill): the layout flag is now set,
        # so the live per-row mirror writes LD's A–I format on the next request
        # save/edit. Stop here — no historical bulk write.
        if execute and not backfill:
            return JsonResponse(
                {
                    "ok": True,
                    "tenant": tenant.slug,
                    "changes": changes,
                    "execute": True,
                    "backfill": False,
                    "master_tracker_layout": tenant.master_tracker_layout,
                    "master_tracker_tab_name": tenant.master_tracker_tab_name,
                    "note": (
                        "Layout enabled; new/edited requests now sync into "
                        "columns A–I. No historical backfill performed."
                    ),
                }
            )

        # Dry-run: build a read-only PREVIEW of the LD A–I rows for a sample of
        # this tenant's requests (no write, no flag change) so the mapping can
        # be eyeballed before going live.
        preview: list = []
        if not execute:
            try:
                from utils import sheets_mirror as _sm
                from events import models as _em

                sample = (
                    _em.Request.objects.filter(
                        tenant=tenant, deleted_at__isnull=True
                    )
                    .select_related("state", "retailer", "timezone")
                    .prefetch_related("request_product__product")
                    .order_by("-date")[:12]
                )
                header = [
                    "State", "Date(wkday)", "Date", "Store Name", "Start",
                    "End", "Address", "Notes", "SKUs to sample",
                ]
                preview.append(header)
                for r in sample:
                    row = _sm._ld_retail_row(r)
                    if row:
                        preview.append(row)
            except Exception as exc:
                preview = [["preview-failed", _concise_exc(exc)]]

        out = io.StringIO()
        cmd_args = ["--tenant-slug", tenant.slug]
        if execute and backfill:
            cmd_args.append("--apply")
            if since_date:
                cmd_args += ["--since-date", since_date]
            if status_slug:
                cmd_args += ["--status-slug", status_slug]
            if delete_rows:
                cmd_args += ["--delete-rows", delete_rows]
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
                "backfill": backfill,
                "since_date": since_date or None,
                "status_slug": status_slug or None,
                "delete_rows": delete_rows or None,
                "master_tracker_tab_name": tenant.master_tracker_tab_name,
                "master_tracker_insert_by_date": tenant.master_tracker_insert_by_date,
                "master_tracker_layout": tenant.master_tracker_layout,
                "preview": preview,
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


def _ld_rmm_unassigned_audit(tenant) -> dict:
    """Enumerate the demos that land in the LD Summary's 'Unassigned' RMM
    bucket and WHY. Mirrors recaps.ld_summary_export.compute_ld_program_breakdowns
    exactly (legacy Recap: assigned-RMM-then-state; custom: state-only). A demo
    is Unassigned when its event has no LD RMM assigned AND no state that maps to
    an RMM territory (routing.LIQUID_DEATH_TERRITORY). Read-only."""
    from collections import Counter

    from events.models import Event
    from recaps.ld_summary_export import (
        RMM_EMAIL_TO_NAME,
        STATE_TO_RMM,
        _event_rmm_name,
        _recap_state_code,
    )
    from recaps.models import CustomRecap, Recap

    ewr = (
        Event.objects.exclude(request__deleted_at__isnull=False)
        .filter(tenant=tenant, recaps__isnull=False)
        .distinct()
    )
    reasons: Counter = Counter()
    unmapped_states: Counter = Counter()
    nonld_emails: Counter = Counter()
    samples: list = []

    def _evd(ev) -> str:
        v = getattr(ev, "date", None)
        try:
            return v.strftime("%Y-%m-%d") if v else ""
        except Exception:
            return str(v)[:10] if v else ""

    def _email(ev) -> str:
        u = getattr(ev, "rmm_asigned", None)
        return (getattr(u, "email", "") or "").strip().lower()

    legacy_unassigned = 0
    for r in Recap.objects.filter(event__in=ewr).select_related(
        "state", "event", "event__state", "event__rmm_asigned"
    ):
        ev = getattr(r, "event", None)
        code = getattr(getattr(r, "state", None), "code", None) or getattr(
            getattr(ev, "state", None), "code", None
        )
        code = (str(code).strip().upper()[:2] or None) if code else None
        if _event_rmm_name(ev) or (STATE_TO_RMM.get(code) if code else None):
            continue
        legacy_unassigned += 1
        email = _email(ev)
        if email and email not in RMM_EMAIL_TO_NAME:
            nonld_emails[email] += 1
        if code:
            unmapped_states[code] += 1
        rkey = ("rmm_set_non_ld" if email else "no_rmm_on_event") + " + " + (
            "state_unmapped" if code else "no_state"
        )
        reasons["legacy: " + rkey] += 1
        if len(samples) < 60:
            samples.append({
                "src": "legacy", "event": (getattr(ev, "name", "") or "")[:70],
                "date": _evd(ev), "state": code or "—", "rmm_email": email or "—",
            })

    custom_unassigned = 0
    for cr in CustomRecap.objects.filter(tenant=tenant).select_related(
        "state", "event", "event__state", "event__rmm_asigned"
    ):
        state = _recap_state_code(cr)
        if state and STATE_TO_RMM.get(state):
            continue
        custom_unassigned += 1
        ev = getattr(cr, "event", None)
        email = _email(ev)
        if state:
            unmapped_states[state] += 1
        if email in RMM_EMAIL_TO_NAME:
            # would map if the custom path used the event's assigned RMM (it
            # only uses state) — a fixable attribution gap.
            reasons["custom: event_has_rmm_but_state_only_attribution"] += 1
        elif not state:
            reasons["custom: no_state"] += 1
        else:
            reasons["custom: state_unmapped"] += 1
        if len(samples) < 60:
            samples.append({
                "src": "custom", "event": (getattr(ev, "name", "") or "")[:70],
                "date": _evd(ev), "state": state or "—", "rmm_email": email or "—",
            })

    return {
        "total_unassigned": legacy_unassigned + custom_unassigned,
        "legacy_unassigned": legacy_unassigned,
        "custom_unassigned": custom_unassigned,
        "reasons": dict(reasons),
        "top_unmapped_states": unmapped_states.most_common(20),
        "non_ld_rmm_emails": nonld_emails.most_common(20),
        "samples": samples,
    }


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

        # Legacy Recap / ConsumerEngagements — the source the in-app dashboard
        # SUMS alongside CustomRecap. LD's bulk demo data lives here, not in
        # CustomRecap, which is why the CustomRecap-only counts above read tiny.
        from django.db.models import Sum

        legacy = {"error": None}
        legacy_recaps_by_year: Counter = Counter()
        ce_consumers_by_year: dict = {}
        try:
            from recaps.models import ConsumerEngagements, Recap

            for r in Recap.objects.filter(event__tenant=tenant).select_related("event"):
                ev = r.event
                legacy_recaps_by_year[ev.date.year if ev and ev.date else "no-date"] += 1
            ce_qs = ConsumerEngagements.objects.filter(recap__event__tenant=tenant)
            ce_agg = ce_qs.aggregate(
                c=Sum("total_consumer"),
                b=Sum("brand_aware_consumers"),
                w=Sum("willing_to_purchase_consumers"),
            )
            consumers_by_year: Counter = Counter()
            for ce in ce_qs.select_related("recap__event").only(
                "total_consumer", "recap__event__date"
            ):
                ev = getattr(getattr(ce, "recap", None), "event", None)
                yr = ev.date.year if ev and ev.date else "no-date"
                consumers_by_year[yr] += ce.total_consumer or 0
            ce_consumers_by_year = dict(consumers_by_year)
            legacy = {
                "error": None,
                "legacy_recap_total": Recap.objects.filter(event__tenant=tenant).count(),
                "consumer_engagements_rows": ce_qs.count(),
                "ce_total_consumers": ce_agg.get("c") or 0,
                "ce_total_brand_aware": ce_agg.get("b") or 0,
                "ce_total_willing": ce_agg.get("w") or 0,
            }
        except Exception as exc:  # noqa: BLE001
            legacy = {"error": _concise_exc(exc)}

        field_names = sorted(
            set(
                CustomField.objects.filter(
                    custom_recap_template__tenant=tenant
                ).values_list("name", flat=True)
            )
        )

        def _ord(d: dict) -> dict:
            return {str(k): v for k, v in sorted(d.items(), key=lambda kv: str(kv[0]))}

        # Audit the Summary's "Unassigned" RMM bucket (which demos don't map to
        # an RMM, and why). Always included — this is a manual, read-only endpoint.
        try:
            rmm_audit = _ld_rmm_unassigned_audit(tenant)
        except Exception as exc:  # noqa: BLE001
            rmm_audit = {"error": _concise_exc(exc)}

        return JsonResponse(
            {
                "ok": True,
                "tenant": tenant.slug,
                "rmm_audit": rmm_audit,
                "total_recaps": total_recaps,
                "recaps_by_year": _ord(recap_year),
                "recaps_by_month": _ord(recap_month),
                "events_by_year": _ord(event_year),
                "legacy_recaps_by_year": _ord(legacy_recaps_by_year),
                "ce_consumers_by_year": _ord(ce_consumers_by_year),
                "legacy": legacy,
                "recap_field_count": len(field_names),
                "recap_field_names": field_names,
            }
        )

    def post(self, request: HttpRequest) -> HttpResponse:
        return self._run(request)

    def get(self, request: HttpRequest) -> HttpResponse:
        return self._run(request)


@method_decorator(csrf_exempt, name="dispatch")
class BackfillEventStateView(View):
    """POST `/internal/cron/backfill-event-state`.

    Stamp `event.state` for a tenant's events that have a NULL state FK, parsed
    from the event's address (then its name) via events.routing.extract_state_code,
    with an optional Photon geocode fallback for addresses with no parseable
    code. Fixes demos that can't map to an RMM territory (the Summary's
    'Unassigned' bucket) because they have no state.

    Params: tenant_slug (default liquid-death), apply (write; default dry-run),
    geocode (add the slower geocode fallback), limit (cap events processed —
    use to batch geocode runs and avoid timeouts).
    """

    def _run(self, request: HttpRequest) -> HttpResponse:
        deny = _check_secret(request)
        if deny is not None:
            return deny

        from collections import Counter

        from django.utils import timezone as djtz

        from events.models import Event, State
        from events.routing import extract_state_code
        from tenants.models import Tenant

        def _get(n: str) -> str:
            return (request.GET.get(n) or request.POST.get(n) or "").strip()

        def _bool(n: str) -> bool:
            return _get(n).lower() in ("1", "true", "yes", "on")

        slug = _get("tenant_slug") or "liquid-death"
        apply = _bool("apply")
        geocode = _bool("geocode")
        try:
            limit = int(_get("limit") or 0)
        except ValueError:
            limit = 0

        tenant = (
            Tenant.objects.filter(slug=slug).first()
            or Tenant.objects.filter(name__icontains="liquid death").first()
        )
        if tenant is None:
            return JsonResponse({"ok": False, "error": "no-tenant", "slug": slug}, status=404)

        states_by_code: dict = {}
        states_by_name: dict = {}
        for s in State.objects.all():
            if getattr(s, "code", None):
                states_by_code[s.code.upper()] = s
            if getattr(s, "name", None):
                states_by_name[s.name.lower()] = s

        photon = None
        if geocode:
            try:
                from utils.geocoding import photon_state_for_address as photon
            except Exception:
                photon = None

        qs = Event.objects.filter(tenant=tenant, state__isnull=True).only(
            "id", "name", "address"
        ).order_by("id")
        if limit:
            qs = qs[:limit]

        total = resolved = geocoded = unresolved = written = 0
        by_state: Counter = Counter()
        samples: list = []
        unresolved_samples: list = []

        for ev in qs:
            total += 1
            code = extract_state_code(getattr(ev, "address", None)) or extract_state_code(
                getattr(ev, "name", None)
            )
            st = states_by_code.get(code.upper()) if code else None
            via_geocode = False
            if st is None and photon is not None:
                try:
                    nm = photon(getattr(ev, "address", None))
                    if nm and nm.lower() in states_by_name:
                        st = states_by_name[nm.lower()]
                        via_geocode = True
                except Exception:
                    st = None
            if st is not None:
                resolved += 1
                if via_geocode:
                    geocoded += 1
                by_state[st.code.upper()] += 1
                if len(samples) < 40:
                    samples.append({
                        "event": (ev.name or "")[:55], "addr": (ev.address or "")[:60],
                        "state": st.code.upper(), "via": "geocode" if via_geocode else "address",
                    })
                if apply:
                    try:
                        ev.state = st
                        ev.save(update_fields=["state", "updated_at"])
                        written += 1
                    except Exception:
                        pass
            else:
                unresolved += 1
                if len(unresolved_samples) < 40:
                    unresolved_samples.append({
                        "event": (ev.name or "")[:55], "addr": (ev.address or "")[:75],
                    })

        return JsonResponse({
            "ok": True, "tenant": tenant.slug, "apply": apply, "geocode": geocode,
            "null_state_events": total, "resolved": resolved,
            "resolved_via_geocode": geocoded, "unresolved": unresolved, "written": written,
            "by_state": dict(by_state.most_common()),
            "resolved_samples": samples, "unresolved_samples": unresolved_samples,
        })

    def post(self, request: HttpRequest) -> HttpResponse:
        return self._run(request)

    def get(self, request: HttpRequest) -> HttpResponse:
        return self._run(request)


@method_decorator(csrf_exempt, name="dispatch")
class BuildPoliTabView(View):
    """POST `/internal/cron/build-poli-tab`.

    On the LD RMM planning workbook: duplicate the per-person scorecard tab
    (default 'Pat') as a new tab (default 'Poli') with its data-entry rows
    cleared, and add a parallel column to the MASTER monthly grid that mirrors
    the source's column (the National FM / Pat column) but points at the new
    tab — so it rolls into the existing MONTHLY TOTAL + YTD logic.

    The new column is inserted immediately BEFORE the source column so the
    MONTHLY TOTAL `SUM(C:H)` ranges and the YTD references auto-extend to
    include it (Sheets shifts in-range refs on column insert). The column is
    then copy/pasted from the source column (formulas + formatting) and a
    column-scoped find/replace repoints '<source>!' → '<new>!' and the header.

    Params: sheet_url, source_tab (default Pat), new_tab (default Poli),
    master_tab (default MASTER), data_start_row (default 19), apply (dry-run
    default). Idempotent: skips the duplicate if the tab exists and skips the
    MASTER column if a column already references the new tab.
    """

    def _run(self, request: HttpRequest) -> HttpResponse:
        deny = _check_secret(request)
        if deny is not None:
            return deny

        from utils.sheets_mirror import _col_letter, _service, extract_sheet_id

        def _get(n: str) -> str:
            return (request.GET.get(n) or request.POST.get(n) or "").strip()

        def _bool(n: str) -> bool:
            return _get(n).lower() in ("1", "true", "yes", "on")

        sheet_url = _get("sheet_url") or (
            "https://docs.google.com/spreadsheets/d/"
            "1W4F7X_vdW7d0SmthUvdxujBH2CahG0DaB53xBVr5q04/edit"
        )
        source_tab = _get("source_tab") or "Pat"
        new_tab = _get("new_tab") or "Poli"
        master_tab = _get("master_tab") or "MASTER"
        try:
            data_start = int(_get("data_start_row") or 19)
        except ValueError:
            data_start = 19
        apply = _bool("apply")
        # The MASTER header text for the source column (Pat's column is labeled
        # "NATIONAL FM"); the new column's header.
        source_header = _get("source_header") or "NATIONAL FM"
        new_header = _get("new_header") or new_tab.upper()
        # MASTER wiring is OFF by default: the MASTER monthly grid shares its
        # columns with the YTD table stacked above it, so inserting a region
        # column needs a layout call. Build the tab first; wire MASTER only when
        # explicitly asked (wire_master=1).
        wire_master = _bool("wire_master")
        # The YTD summary table's header row (shares columns with the monthly
        # grid). After the column insert it gets a blank gap; we shift its
        # displaced metrics back. The value row is assumed to be the next row.
        try:
            ytd_header_row = int(_get("ytd_header_row") or 15)
        except ValueError:
            ytd_header_row = 15

        sheet_id = extract_sheet_id(sheet_url)
        if not sheet_id:
            return JsonResponse({"ok": False, "error": "bad-sheet-url"}, status=400)
        svc = _service()
        if svc is None:
            return JsonResponse({"ok": False, "error": "no-credentials"}, status=503)

        def _props():
            meta = (
                svc.spreadsheets()
                .get(spreadsheetId=sheet_id,
                     fields="sheets.properties(title,sheetId,gridProperties)")
                .execute()
            )
            return {
                s["properties"]["title"]: s["properties"]
                for s in meta.get("sheets", [])
            }

        try:
            props = _props()

            # Delete-tabs mode: remove named redundant tabs (e.g. the pre-wire
            # MASTER backup snapshot + the abandoned "WIP (Poli)" stub once the
            # real Poli tab is live). Guarded — refuses to delete the live
            # MASTER / source / new tabs even if named. Honors dry-run (apply=0).
            del_names_raw = _get("delete_tabs")
            if del_names_raw:
                requested = [t.strip() for t in del_names_raw.split(",") if t.strip()]
                protected = {master_tab, source_tab, new_tab}
                to_delete, skipped_protected, not_found = [], [], []
                for name in requested:
                    if name in protected:
                        skipped_protected.append(name)
                    elif name in props:
                        to_delete.append(name)
                    else:
                        not_found.append(name)
                del_plan = {"requested": requested, "to_delete": to_delete,
                            "skipped_protected": skipped_protected,
                            "not_found": not_found, "protected": sorted(protected)}
                if not apply:
                    return JsonResponse({"ok": True, "dry_run": True,
                                         "delete_plan": del_plan})
                if not to_delete:
                    return JsonResponse({"ok": True, "deleted": [], **del_plan})
                svc.spreadsheets().batchUpdate(
                    spreadsheetId=sheet_id,
                    body={"requests": [
                        {"deleteSheet": {"sheetId": props[name]["sheetId"]}}
                        for name in to_delete
                    ]},
                ).execute()
                return JsonResponse({"ok": True, "deleted": to_delete,
                                     "skipped_protected": skipped_protected,
                                     "not_found": not_found,
                                     "remaining_tabs": list(_props())})

            if source_tab not in props:
                return JsonResponse({"ok": False, "error": "source-tab-not-found",
                                     "source_tab": source_tab, "tabs": list(props)}, status=404)
            if master_tab not in props:
                return JsonResponse({"ok": False, "error": "master-tab-not-found",
                                     "master_tab": master_tab, "tabs": list(props)}, status=404)
            src_gid = props[source_tab]["sheetId"]
            master_gid = props[master_tab]["sheetId"]
            src_cols = props[source_tab]["gridProperties"].get("columnCount", 65)
            src_rows = props[source_tab]["gridProperties"].get("rowCount", 1011)

            # Locate the source column in MASTER (the one whose formulas
            # reference '<source_tab>!') by scanning the header band.
            band = (
                svc.spreadsheets().values()
                .get(spreadsheetId=sheet_id, range=f"'{master_tab}'!1:30",
                     valueRenderOption="FORMULA")
                .execute().get("values", [])
            )
            ref_token = f"{source_tab}!"
            src_col_idx = None
            ref_row_no = None
            for r_i, row in enumerate(band):
                for c_i, cell in enumerate(row):
                    if isinstance(cell, str) and ref_token in cell:
                        src_col_idx, ref_row_no = c_i, r_i + 1
                        break
                if src_col_idx is not None:
                    break

            poli_exists = new_tab in props
            already_wired = any(
                isinstance(c, str) and f"{new_tab}!" in c
                for row in band for c in row
            )

            plan = {
                "sheet_id": sheet_id, "source_tab": source_tab, "new_tab": new_tab,
                "master_tab": master_tab,
                "source_col_in_master": _col_letter(src_col_idx + 1) if src_col_idx is not None else None,
                "ref_row_sample": band[ref_row_no - 1] if ref_row_no else None,
                "poli_tab_exists": poli_exists, "master_already_wired": already_wired,
                "clear_range": f"'{new_tab}'!A{data_start}:{_col_letter(src_cols)}{src_rows}",
            }
            plan["wire_master"] = wire_master
            if wire_master and src_col_idx is None:
                return JsonResponse({"ok": False, "error": "source-col-not-found-in-master",
                                     "hint": f"no MASTER cell referenced '{ref_token}'", **plan}, status=422)

            if not apply:
                return JsonResponse({"ok": True, "dry_run": True, "plan": plan})

            # Revert: delete the MASTER column that holds the new tab's refs
            # (cleanly auto-reverts the SUM/YTD shifts + closes any gap). Used to
            # undo a partial wire. Does not touch the new tab.
            if _bool("revert"):
                del_idx = None
                for row in band:
                    for c_i, cell in enumerate(row):
                        if isinstance(cell, str) and f"{new_tab}!" in cell:
                            del_idx = c_i
                            break
                    if del_idx is not None:
                        break
                if del_idx is None:
                    return JsonResponse({"ok": True, "reverted": False,
                                         "note": "no MASTER column referenced the new tab"})
                svc.spreadsheets().batchUpdate(
                    spreadsheetId=sheet_id,
                    body={"requests": [{"deleteDimension": {
                        "range": {"sheetId": master_gid, "dimension": "COLUMNS",
                                  "startIndex": del_idx, "endIndex": del_idx + 1},
                    }}]},
                ).execute()
                return JsonResponse({"ok": True, "reverted": True,
                                     "deleted_col": _col_letter(del_idx + 1)})

            # Repair-only: close the blank gap a column insert opened in the YTD
            # summary rows (shares columns with the grid) WITHOUT re-inserting.
            # Finds the new-tab column (= the gap) and shifts the YTD metrics to
            # its right back by one. Self-detecting; idempotent (no-op if no gap).
            if _bool("repair_ytd"):
                gap_idx = None
                for row in band:
                    for c_i, cell in enumerate(row):
                        if isinstance(cell, str) and f"{new_tab}!" in cell:
                            gap_idx = c_i
                            break
                    if gap_idx is not None:
                        break
                if gap_idx is None:
                    return JsonResponse({"ok": True, "repaired": False,
                                         "note": "no new-tab column found"})
                hdr = (
                    svc.spreadsheets().values()
                    .get(spreadsheetId=sheet_id,
                         range=f"'{master_tab}'!{ytd_header_row}:{ytd_header_row}",
                         valueRenderOption="FORMULA")
                    .execute().get("values", [[]])
                )
                cells = hdr[0] if hdr else []
                last = max((i for i, c in enumerate(cells) if c not in ("", None)),
                           default=-1)
                gap_at = bool(gap_idx < len(cells) and cells[gap_idx] in ("", None))
                if last <= gap_idx or not gap_at:
                    return JsonResponse({"ok": True, "repaired": False,
                                         "note": "no gap at the new-tab column",
                                         "ytd_header": cells})
                for r in (ytd_header_row, ytd_header_row + 1):
                    rv = (
                        svc.spreadsheets().values()
                        .get(spreadsheetId=sheet_id,
                             range=f"'{master_tab}'!{_col_letter(gap_idx + 2)}{r}:"
                                   f"{_col_letter(last + 1)}{r}",
                             valueRenderOption="FORMULA")
                        .execute().get("values", [[]])
                    )
                    block = (rv[0] if rv else [])
                    block = block + [""] * ((last - gap_idx) - len(block))
                    svc.spreadsheets().values().update(
                        spreadsheetId=sheet_id,
                        range=f"'{master_tab}'!{_col_letter(gap_idx + 1)}{r}",
                        valueInputOption="USER_ENTERED", body={"values": [block]},
                    ).execute()
                    svc.spreadsheets().values().update(
                        spreadsheetId=sheet_id,
                        range=f"'{master_tab}'!{_col_letter(last + 1)}{r}",
                        valueInputOption="USER_ENTERED", body={"values": [[""]]},
                    ).execute()
                after = (
                    svc.spreadsheets().values()
                    .get(spreadsheetId=sheet_id,
                         range=f"'{master_tab}'!{ytd_header_row}:{ytd_header_row}",
                         valueRenderOption="FORMULA")
                    .execute().get("values", [[]])
                )
                return JsonResponse({"ok": True, "repaired": True,
                                     "gap_col": _col_letter(gap_idx + 1),
                                     "ytd_header_after": after[0] if after else None})

            actions = []
            # 1) Duplicate the source scorecard tab → new tab.
            if not poli_exists:
                svc.spreadsheets().batchUpdate(
                    spreadsheetId=sheet_id,
                    body={"requests": [{"duplicateSheet": {
                        "sourceSheetId": src_gid, "newSheetName": new_tab,
                    }}]},
                ).execute()
                actions.append(f"duplicated '{source_tab}' → '{new_tab}'")
                props = _props()
            new_gid = props[new_tab]["sheetId"]

            # 2) Clear the duplicated data-entry rows so it starts empty.
            svc.spreadsheets().values().clear(
                spreadsheetId=sheet_id,
                range=f"'{new_tab}'!A{data_start}:{_col_letter(src_cols)}{src_rows}",
            ).execute()
            actions.append(f"cleared data rows {data_start}+ in '{new_tab}'")

            # 3) Wire MASTER — insert a blank column immediately BEFORE the
            #    source column (so MONTHLY TOTAL SUM ranges + YTD refs auto-extend
            #    to include it), then fill it with the source column's formulas
            #    re-pointed to the new tab. We read+rewrite the formulas as TEXT
            #    (not copyPaste) so relative cell refs like F3 are NOT shifted —
            #    only the tab name + header are swapped.
            if wire_master and not already_wired:
                master_rows = props[master_tab]["gridProperties"].get("rowCount", 1036)
                # Safety net: snapshot MASTER before the structural surgery so a
                # mistake on the live dashboard is one-click recoverable.
                backup_name = f"{master_tab} (pre-{new_tab} backup)"
                if backup_name not in props:
                    svc.spreadsheets().batchUpdate(
                        spreadsheetId=sheet_id,
                        body={"requests": [{"duplicateSheet": {
                            "sourceSheetId": master_gid, "newSheetName": backup_name,
                        }}]},
                    ).execute()
                    actions.append(f"backed up '{master_tab}' → '{backup_name}'")
                svc.spreadsheets().batchUpdate(
                    spreadsheetId=sheet_id,
                    body={"requests": [{"insertDimension": {
                        "range": {"sheetId": master_gid, "dimension": "COLUMNS",
                                  "startIndex": src_col_idx, "endIndex": src_col_idx + 1},
                        "inheritFromBefore": True,
                    }}]},
                ).execute()
                new_col = src_col_idx          # the blank inserted column
                src_now = src_col_idx + 1       # source column shifted right by 1
                src_letter = _col_letter(src_now + 1)
                new_letter = _col_letter(new_col + 1)

                col_vals = (
                    svc.spreadsheets().values()
                    .get(spreadsheetId=sheet_id,
                         range=f"'{master_tab}'!{src_letter}1:{src_letter}{master_rows}",
                         valueRenderOption="FORMULA")
                    .execute().get("values", [])
                )
                # Only carry the region-grid cells into the new column: the
                # source-tab refs (=Pat!…) and the block header. Everything else
                # in that column (e.g. the YTD-section formulas that share the
                # column) is left blank so we don't duplicate them.
                out_vals = []
                refs = 0
                for row in col_vals:
                    v = row[0] if row else ""
                    if isinstance(v, str) and (
                        f"{source_tab}!" in v or f"'{source_tab}'!" in v
                    ):
                        out_vals.append([
                            v.replace(f"'{source_tab}'!", f"'{new_tab}'!").replace(
                                f"{source_tab}!", f"{new_tab}!")
                        ])
                        refs += 1
                    elif isinstance(v, str) and v.strip().upper() == source_header.upper():
                        out_vals.append([new_header])
                    else:
                        out_vals.append([""])
                svc.spreadsheets().values().update(
                    spreadsheetId=sheet_id,
                    range=f"'{master_tab}'!{new_letter}1",
                    valueInputOption="USER_ENTERED",
                    body={"values": out_vals},
                ).execute()
                # (The inserted column inherits formatting from its left neighbor
                # via inheritFromBefore; we skip a copyPaste PASTE_FORMAT because
                # it can't paste across the merged header cells in this sheet.)
                actions.append(
                    f"inserted MASTER col {new_letter} mirroring {src_letter} "
                    f"→ '{new_tab}' ({refs} refs repointed)"
                )

                # 4) Repair the YTD summary table: the insert opened a blank gap
                #    in its header/value rows (it shares columns with the grid).
                #    Determine the span from the HEADER row, then shift BOTH the
                #    header and value rows by the same amount to close the gap.
                hdr = (
                    svc.spreadsheets().values()
                    .get(spreadsheetId=sheet_id,
                         range=f"'{master_tab}'!{ytd_header_row}:{ytd_header_row}",
                         valueRenderOption="FORMULA")
                    .execute().get("values", [[]])
                )
                hdr_cells = hdr[0] if hdr else []
                last = max((i for i, c in enumerate(hdr_cells) if c not in ("", None)),
                           default=-1)
                if last > new_col:  # there is a gap at new_col to close
                    for r in (ytd_header_row, ytd_header_row + 1):
                        rv = (
                            svc.spreadsheets().values()
                            .get(spreadsheetId=sheet_id,
                                 range=f"'{master_tab}'!{_col_letter(new_col + 2)}{r}:"
                                       f"{_col_letter(last + 1)}{r}",
                                 valueRenderOption="FORMULA")
                            .execute().get("values", [[]])
                        )
                        block = rv[0] if rv else []
                        block = block + [""] * ((last - new_col) - len(block))
                        svc.spreadsheets().values().update(
                            spreadsheetId=sheet_id,
                            range=f"'{master_tab}'!{_col_letter(new_col + 1)}{r}",
                            valueInputOption="USER_ENTERED",
                            body={"values": [block]},
                        ).execute()
                        svc.spreadsheets().values().update(
                            spreadsheetId=sheet_id,
                            range=f"'{master_tab}'!{_col_letter(last + 1)}{r}",
                            valueInputOption="USER_ENTERED",
                            body={"values": [[""]]},
                        ).execute()
                    actions.append(
                        f"closed YTD gap: shifted cols {_col_letter(new_col + 2)}-"
                        f"{_col_letter(last + 1)} ← 1 on rows "
                        f"{ytd_header_row}-{ytd_header_row + 1}")

                # 5) Read back the key MASTER rows so the result is verifiable.
                verify = (
                    svc.spreadsheets().values()
                    .get(spreadsheetId=sheet_id,
                         range=f"'{master_tab}'!{ytd_header_row}:{ref_row_no}",
                         valueRenderOption="FORMULA")
                    .execute().get("values", [])
                )
                plan["verify_rows"] = {
                    "ytd_header": verify[0] if verify else None,
                    "ref_row": verify[-1] if verify else None,
                }

            return JsonResponse({"ok": True, "apply": True, "new_gid": new_gid,
                                 "actions": actions, "plan": plan})
        except Exception as exc:  # noqa: BLE001
            logger.exception("build-poli-tab failed")
            return JsonResponse({"ok": False, "error": "failed",
                                 "exception": _concise_exc(exc)}, status=500)

    def post(self, request: HttpRequest) -> HttpResponse:
        return self._run(request)

    def get(self, request: HttpRequest) -> HttpResponse:
        return self._run(request)


@method_decorator(csrf_exempt, name="dispatch")
class CheckCronHealthView(View):
    """POST `/internal/cron/check-cron-health`.

    Fires `check_cron_health` — the staleness watchdog that reads the
    digest.CronRun heartbeats and emails the Ignite team when a recurring
    cron has gone overdue (older than its cadence) or is erroring. This is
    the proactive alarm on top of the passive System Health page: a job
    that silently stops firing surfaces within a cadence window instead of
    weeks later. Read-only apart from the per-cron alert throttle stamp.

    Designed for a frequent-ish GHA cron (every ~6h). Note: the wrapper in
    digest.urls also records THIS endpoint's own heartbeat, so the watchdog
    is itself watched (add it to EXPECTED_MAX_HOURS to alert if it stops).

    Body / query params (all optional):
      - dry_run: "1" / "true" / "yes" — report the plan, send/stamp nothing.
      - throttle_hours: int (default 12) — don't re-alert the same cron
        within this many hours.
      - alert_never_seen: "1"/"true"/"yes" — also alert on watched crons
        with no heartbeat at all (off by default; cold-start noise).
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

        throttle_hours = _int("throttle_hours", 12)
        if throttle_hours is None:
            return JsonResponse(
                {"ok": False, "error": "throttle_hours must be an integer"},
                status=400,
            )
        dry_run = _bool("dry_run", default=False)
        alert_never_seen = _bool("alert_never_seen", default=False)

        cmd_args: list[str] = ["--throttle-hours", str(throttle_hours)]
        if dry_run:
            cmd_args.append("--dry-run")
        if alert_never_seen:
            cmd_args.append("--alert-never-seen")

        out = io.StringIO()
        try:
            call_command("check_cron_health", *cmd_args, stdout=out)
        except Exception as exc:  # noqa: BLE001 — surface to caller
            logger.exception("Cron health check failed")
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
            {"ok": True, "dry_run": dry_run, "log": out.getvalue()}
        )

    def get(self, request: HttpRequest) -> HttpResponse:
        deny = _check_secret(request)
        if deny is not None:
            return deny
        return JsonResponse({"ok": True, "endpoint": "check-cron-health"})


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
        "ambassador-job-reminders": SendAmbassadorJobRemindersView,
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
        "backfill-event-state": BackfillEventStateView,
        "build-poli-tab": BuildPoliTabView,
        "repair-request-activation-time": RepairRequestActivationTimeView,
        "backfill-girlbeer-receipts": BackfillGirlBeerReceiptsView,
        "audit-tenant-onboarding": AuditTenantOnboardingView,
        "audit-tenant-consumers": AuditTenantConsumersView,
        "dedupe-skills": DedupeSkillsView,
        "apply-girl-beer-branding": ApplyGirlBeerBrandingView,
        "shift-confirmations": SendShiftConfirmationsView,
        "provision-review-ambassador": ProvisionReviewAmbassadorView,
        "import-event-schedule": ImportEventScheduleView,
        "verify-user": VerifyUserView,
        "demote-admin": DemoteAdminView,
        "ensure-ambassador-profile": EnsureAmbassadorProfileView,
        "set-event-date": SetEventDateView,
        "book-ambassador-on-event": BookAmbassadorOnEventView,
        "set-tenant-event-types": SetTenantEventTypesView,
        "set-custom-recap-field": SetCustomRecapFieldView,
        "export-recaps-to-sheet": ExportRecapsToSheetView,
        "export-ld-summary": ExportLdSummaryView,
        "export-girlbeer-summary": ExportGirlbeerSummaryView,
        "backfill-recap-retailers": BackfillRecapRetailersView,
        "describe-sheet-tabs": DescribeSheetTabsView,
        "fix-ld-kpi-totals": FixLdKpiTotalsView,
        "set-tenant-mileage-tracking": SetTenantMileageTrackingView,
        "staff-tenant-events": StaffTenantEventsView,
        "weekly-mileage-report": WeeklyMileageReportView,
        "activation-autopilot": ActivationAutopilotView,
        "recap-data-health": RecapDataHealthView,
        "audit-ba-accounts": AuditBaAccountsView,
        "resend-ba-welcome": ResendBaWelcomeView,
        "retext-tenant-events": RetextTenantEventsView,
        "backfill-ld-master-tracker": BackfillLdMasterTrackerView,
        "ld-data-census": LdDataCensusView,
        "export-ld-recaps": ExportLdRecapsView,
        "check-cron-health": CheckCronHealthView,
    }
