"""Email each opted-in tenant its monthly client performance report.

Once a month this command generates a per-tenant program-summary PDF
(:func:`recaps.client_report.build_client_monthly_report_pdf`) and emails it to
that tenant's client contacts (:class:`recaps.envelopes.ClientMonthlyReportMailer`).

SAFE DEFAULT — OPT-IN OFF. The command only ever touches tenants that have
BOTH ``scheduled_report_enabled=True`` AND a non-empty recipient list
(``Tenant.scheduled_report_recipients()`` — which reuses
``recap_recipient_emails``). ``scheduled_report_enabled`` defaults to ``False``
on the model, so until Ignite explicitly flips a tenant on, this command emails
NOBODY — deploying it is inert. With no tenants enabled, a normal run sends
nothing and reports "0 enabled".

Period selection — the most recent COMPLETE month. By default the report
covers the most recent FULLY-ELAPSED calendar month (never the in-progress
current month — reporting a partial month would understate the program, the
same partial-period distortion :func:`recaps.tenant_overview.tenant_kpi_comparison`
guards against). For a "now" anywhere in June 2026 the default period is May
2026. Override with ``--month YYYY-MM``.

Resilience. Each tenant is wrapped in its own ``try / except`` +
``logger.exception`` so one tenant's failure (bad data, a render error, a mail
hiccup) is logged and skipped — it never aborts the run for the others.

Flags:
    --dry-run            Generate the PDF + log the recipients, but send NOTHING.
    --tenant <id>        Only this tenant (for testing a single brand).
    --month YYYY-MM      Override the reporting month (default: prior complete month).

Usage:
    python manage.py send_scheduled_client_reports
    python manage.py send_scheduled_client_reports --dry-run
    python manage.py send_scheduled_client_reports --tenant 12 --month 2026-05

Scheduling (infra — NOT wired here, mirror the existing crons). The other
scheduled jobs run via a GitHub Actions workflow that POSTs an
``/internal/cron/<name>`` endpoint on Cloud Run (secret-gated by
``X-Cron-Secret`` → ``settings.INTERNAL_CRON_SECRET``), which calls this command
through ``django.core.management.call_command`` — see ``digest/cron_views.py`` +
``digest/urls.py`` and ``.github/workflows/daily-recap-reminders.yml`` for the
pattern. To schedule this MONTHLY: add a ``SendScheduledClientReportsView`` to
``digest/cron_views.py`` (register it in ``_registered_views`` so ``digest/urls.py``
mounts it) that ``call_command("send_scheduled_client_reports", ...)``, then add
a workflow with a monthly ``cron`` (e.g. ``"0 14 1 * *"`` — 1st of the month)
that curls the endpoint. Running it monthly is correct: the default period is
the prior complete month, so a 1st-of-month run reports the month that just
ended.
"""

from __future__ import annotations

import logging

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from recaps.client_report import (
    ClientMonthlyReportError,
    build_client_monthly_report_pdf,
)
from recaps.envelopes import ClientMonthlyReportMailer
from recaps.tenant_overview import _MONTH_ABBR, _add_months
from tenants.models import Tenant

logger = logging.getLogger(__name__)


def _prior_complete_month(now=None) -> tuple[int, int]:
    """The (year, month) of the most recent FULLY-ELAPSED calendar month.

    One month before the month ``now`` falls in (the current calendar month is
    still in progress, so it is never the reporting period) — the same
    "current = most recent complete month" rule
    :func:`recaps.tenant_overview._month_comparison_windows` uses, via the same
    :func:`_add_months` helper. For a June-2026 ``now`` this returns
    ``(2026, 5)``.
    """
    now = now or timezone.now()
    return _add_months(now.year, now.month, -1)


def _parse_month_arg(value: str) -> tuple[int, int]:
    """Parse a ``YYYY-MM`` override into ``(year, month)``.

    Raises :class:`CommandError` on a malformed value or an out-of-range month
    so a typo fails loudly at invocation rather than silently picking a wrong
    window.
    """
    try:
        year_s, month_s = value.split("-")
        year, month = int(year_s), int(month_s)
    except (ValueError, AttributeError) as exc:
        raise CommandError(
            f"--month must be YYYY-MM (e.g. 2026-05); got {value!r}."
        ) from exc
    if not (1 <= month <= 12):
        raise CommandError(f"--month month must be 1-12; got {month}.")
    if year < 2000 or year > 9999:
        raise CommandError(f"--month year looks wrong; got {year}.")
    return year, month


def _month_label(year: int, month: int) -> str:
    """Short "Mon YYYY" label for logs / the email subject (locale-independent)."""
    abbr = _MONTH_ABBR[month] if 1 <= month <= 12 else str(month)
    return f"{abbr} {year}"


class Command(BaseCommand):
    help = (
        "Generate + email the monthly performance-report PDF to each tenant "
        "that has opted in (scheduled_report_enabled=True) and has recipients. "
        "Defaults to the most recent COMPLETE month. Opt-in OFF by default."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Generate the PDF + log recipients, but send NO email.",
        )
        parser.add_argument(
            "--tenant",
            type=int,
            default=None,
            help="Restrict to a single tenant id (for testing one brand).",
        )
        parser.add_argument(
            "--month",
            type=str,
            default=None,
            help="Override reporting month as YYYY-MM (default: prior complete month).",
        )

    def handle(self, *args, **opts):
        dry_run = bool(opts["dry_run"])
        only_tenant = opts.get("tenant")

        if opts.get("month"):
            year, month = _parse_month_arg(opts["month"])
        else:
            year, month = _prior_complete_month()
        period_label = _month_label(year, month)

        # Candidate tenants: opted-in only. Skip archived clients via the
        # active() convention (an archived tenant should never get email even
        # if its flag was left on). --tenant narrows to one id WITHOUT
        # bypassing the opt-in gate, so testing still can't email a brand that
        # hasn't opted in.
        tenants = Tenant.active().filter(scheduled_report_enabled=True)
        if only_tenant is not None:
            tenants = tenants.filter(id=only_tenant)

        mode = "DRY-RUN (no email will be sent)" if dry_run else "live"
        self.stdout.write(
            f"Scheduled client reports — period {period_label}, mode {mode}."
        )

        enabled = 0
        sent = 0
        skipped_no_recipients = 0
        failed = 0

        for tenant in tenants.iterator():
            enabled += 1
            try:
                recipients = tenant.scheduled_report_recipients()
                if not recipients:
                    # Opted in but no recipients configured -> nothing to send.
                    skipped_no_recipients += 1
                    self.stdout.write(
                        f"  - {tenant.name} (id={tenant.id}): SKIP — no recipients."
                    )
                    continue

                pdf_bytes = build_client_monthly_report_pdf(tenant.id, year, month)
                filename = self._pdf_filename(tenant, year, month)

                if dry_run:
                    self.stdout.write(
                        f"  - {tenant.name} (id={tenant.id}): would email "
                        f"{len(recipients)} recipient(s) {recipients} "
                        f"({len(pdf_bytes):,} byte PDF) — NOT sent (dry-run)."
                    )
                    continue

                mailer = ClientMonthlyReportMailer(
                    recipients=recipients,
                    tenant_name=tenant.name or "",
                    period_label=period_label,
                    pdf_bytes=pdf_bytes,
                    pdf_filename=filename,
                )
                mailer.send()
                sent += 1
                self.stdout.write(
                    f"  - {tenant.name} (id={tenant.id}): sent to "
                    f"{len(recipients)} recipient(s)."
                )
            except ClientMonthlyReportError:
                failed += 1
                logger.exception(
                    "Scheduled report: PDF generation failed for tenant=%s (%s).",
                    tenant.id,
                    getattr(tenant, "name", None),
                )
            except Exception:
                # One tenant's failure (data / mail / anything) must not abort
                # the whole run — log it and move on.
                failed += 1
                logger.exception(
                    "Scheduled report: unexpected failure for tenant=%s (%s).",
                    tenant.id,
                    getattr(tenant, "name", None),
                )

        summary = (
            f"Done — period {period_label}: {enabled} enabled tenant(s), "
            f"{sent} sent, {skipped_no_recipients} skipped (no recipients), "
            f"{failed} failed."
        )
        if dry_run:
            summary = (
                f"Done (DRY-RUN, nothing sent) — period {period_label}: "
                f"{enabled} enabled tenant(s), "
                f"{skipped_no_recipients} would-skip (no recipients), "
                f"{failed} failed."
            )
        self.stdout.write(self.style.SUCCESS(summary))

    @staticmethod
    def _pdf_filename(tenant: Tenant, year: int, month: int) -> str:
        """A clean, filesystem-safe attachment name like ``girl-beer-2026-05.pdf``."""
        slug = (getattr(tenant, "slug", None) or "").strip()
        if not slug:
            # Fall back to a sanitized tenant name when no slug is set.
            base = "".join(
                ch if ch.isalnum() else "-" for ch in (tenant.name or "tenant").lower()
            ).strip("-") or "tenant"
            slug = base
        return f"{slug}-{year:04d}-{month:02d}-performance.pdf"
