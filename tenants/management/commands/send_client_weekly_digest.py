"""Email each opted-in tenant a weekly digest of its field-marketing activity.

Once a week this command builds a per-tenant three-section rollup
(:func:`recaps.weekly_digest.build_weekly_digest`) and emails it to that
tenant's client contacts (:class:`recaps.envelopes.ClientWeeklyDigestMailer`):

  1. **This week at a glance** — activations run, recaps filed, and the headline
     KPIs over the trailing 7 days.
  2. **Coming up (next 7 days)** — upcoming activations.
  3. **Needs your approval** — requests still awaiting sign-off.

SAFE DEFAULT — OPT-IN OFF. The digest has its OWN per-tenant flag,
``client_weekly_digest_enabled`` (split from ``scheduled_report_enabled`` in
migration 0025, which copied the old shared value so nobody silently lost the
digest), so the weekly digest and the monthly report roll out independently.
The command only ever touches tenants with BOTH the flag ON AND a non-empty
recipient list (``Tenant.scheduled_report_recipients()`` — the same client
contacts the recap-approval emails reach).

Quiet weeks are skipped. A tenant whose week has nothing worth reporting
(nothing ran, nothing coming up, nothing pending — see
``WeeklyDigest.has_content``) is skipped rather than mailed a barren report.
Use ``--force`` to send anyway (handy with ``--tenant`` for a test run).

Resilience. Each tenant is wrapped in its own ``try / except`` +
``logger.exception`` so one tenant's failure never aborts the run for others.

Flags:
    --dry-run        Build the digest + log recipients, but send NOTHING.
    --tenant <id>    Only this tenant (for testing a single brand).
    --force          Send even if the week is quiet (skips the has_content gate).

Usage:
    python manage.py send_client_weekly_digest
    python manage.py send_client_weekly_digest --dry-run
    python manage.py send_client_weekly_digest --tenant 12 --force

Scheduling (infra — mirror the existing crons). Like the other scheduled jobs,
this runs via a GitHub Actions workflow that POSTs ``/internal/cron/<name>`` on
Cloud Run (secret-gated by ``X-Cron-Secret`` → ``settings.INTERNAL_CRON_SECRET``),
which calls this command through ``call_command``. See ``digest/cron_views.py``
(``SendClientWeeklyDigestView``, registered in ``_registered_views``) and
``.github/workflows/client-weekly-digest.yml`` (weekly, Monday AM). The cron
workflow must live on BOTH ``develop`` and ``main`` — GitHub only fires schedules
from the default branch (``main``), but the endpoint deploys from ``develop``.
"""

from __future__ import annotations

import logging

from django.core.management.base import BaseCommand
from django.utils import timezone

from recaps.envelopes import ClientWeeklyDigestMailer
from recaps.weekly_digest import build_weekly_digest
from tenants.models import Tenant

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = (
        "Build + email the weekly digest to each tenant that has opted in "
        "(client_weekly_digest_enabled=True) and has recipients. Quiet weeks "
        "are skipped unless --force. Opt-in OFF by default."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Build the digest + log recipients, but send NO email.",
        )
        parser.add_argument(
            "--tenant",
            type=int,
            default=None,
            help="Restrict to a single tenant id (for testing one brand).",
        )
        parser.add_argument(
            "--force",
            action="store_true",
            help="Send even if the week is quiet (skip the has_content gate).",
        )

    def handle(self, *args, **opts):
        dry_run = bool(opts["dry_run"])
        force = bool(opts["force"])
        only_tenant = opts.get("tenant")
        now = timezone.now()

        # Candidate tenants: opted-in + active only. --tenant narrows to one id
        # WITHOUT bypassing the opt-in gate, so testing still can't email a
        # brand that hasn't opted in. The digest has its OWN flag (split from
        # scheduled_report_enabled in migration 0025 so the weekly digest and
        # the monthly report roll out independently per tenant).
        tenants = Tenant.active().filter(client_weekly_digest_enabled=True)
        if only_tenant is not None:
            tenants = tenants.filter(id=only_tenant)

        mode = "DRY-RUN (no email will be sent)" if dry_run else "live"
        self.stdout.write(
            f"Client weekly digest — as of {now:%Y-%m-%d}, mode {mode}"
            f"{' (forced)' if force else ''}."
        )

        enabled = 0
        sent = 0
        skipped_no_recipients = 0
        skipped_quiet = 0
        failed = 0

        for tenant in tenants.iterator():
            enabled += 1
            try:
                recipients = tenant.scheduled_report_recipients()
                if not recipients:
                    skipped_no_recipients += 1
                    self.stdout.write(
                        f"  - {tenant.name} (id={tenant.id}): SKIP — no recipients."
                    )
                    continue

                digest = build_weekly_digest(tenant.id, now)

                if not digest.has_content and not force:
                    skipped_quiet += 1
                    self.stdout.write(
                        f"  - {tenant.name} (id={tenant.id}): SKIP — quiet week "
                        f"(use --force to send anyway)."
                    )
                    continue

                if dry_run:
                    self.stdout.write(
                        f"  - {tenant.name} (id={tenant.id}): would email "
                        f"{len(recipients)} recipient(s) {recipients} — "
                        f"{digest.completed_activations} ran, "
                        f"{digest.upcoming_total} coming up, "
                        f"{digest.pending_total} pending — NOT sent (dry-run)."
                    )
                    continue

                mailer = ClientWeeklyDigestMailer(
                    recipients=recipients,
                    tenant_name=tenant.name or "",
                    digest=digest,
                )
                mailer.send()
                sent += 1
                self.stdout.write(
                    f"  - {tenant.name} (id={tenant.id}): sent to "
                    f"{len(recipients)} recipient(s)."
                )
            except Exception:
                # One tenant's failure (data / mail / anything) must not abort
                # the whole run — log it and move on.
                failed += 1
                logger.exception(
                    "Weekly digest: unexpected failure for tenant=%s (%s).",
                    tenant.id,
                    getattr(tenant, "name", None),
                )
                self.stdout.write(
                    f"  - {getattr(tenant, 'name', '?')} (id={getattr(tenant, 'id', '?')}): "
                    f"FAILED — see logs."
                )

        self.stdout.write(
            self.style.SUCCESS(
                f"Done. enabled={enabled} sent={sent} "
                f"skipped_no_recipients={skipped_no_recipients} "
                f"skipped_quiet={skipped_quiet} failed={failed}."
            )
        )
