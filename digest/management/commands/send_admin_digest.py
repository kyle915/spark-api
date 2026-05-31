"""
Cron entrypoint for the admin digest email.

Designed to be invoked by Cloud Scheduler → Cloud Run job (or `manage.py`
on a host with a working email config). Walks every active tenant, builds
its TenantDigest, and sends the email to that tenant's admin + spark-admin
users.

Usage:
    python manage.py send_admin_digest                       # daily window
    python manage.py send_admin_digest --window weekly       # 30-day upcoming
    python manage.py send_admin_digest --tenant-id 12        # one tenant
    python manage.py send_admin_digest --skip-empty          # don't email
                                                             # "all clear"
    python manage.py send_admin_digest --dry-run             # log, don't send
    python manage.py send_admin_digest --to ops@example.com  # override recipients
"""

from __future__ import annotations

from django.core.management.base import BaseCommand

from digest.envelopes import AdminDigestMailer
from digest.services import (
    active_tenants,
    admin_recipients_for_tenant,
    build_tenant_digest,
)
from tenants.models import Tenant
from utils.mailer import MailChain


class Command(BaseCommand):
    help = (
        "Send the admin digest email to admins of every active tenant. "
        "Intended to run from Cloud Scheduler daily (or weekly)."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--window",
            choices=["daily", "weekly"],
            default="daily",
            help=(
                "Daily uses a 7-day upcoming window; weekly uses 30. "
                "Both run the same aggregator otherwise."
            ),
        )
        parser.add_argument(
            "--tenant-id",
            type=int,
            default=None,
            help="Only build/send the digest for this tenant.",
        )
        parser.add_argument(
            "--skip-empty",
            action="store_true",
            help=(
                "Don't send when there are no pending approvals + no "
                "unfiled recaps + no upcoming shifts."
            ),
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Build the digest + log the summary but skip the email send.",
        )
        parser.add_argument(
            "--to",
            type=str,
            default=None,
            help=(
                "Override recipients. Comma-separated emails. Useful for "
                "testing — fires to this address regardless of tenant admins."
            ),
        )

    def handle(self, *args, **opts):
        window = opts["window"]
        window_label = "Daily" if window == "daily" else "Weekly"
        upcoming_days = 7 if window == "daily" else 30
        tenant_id = opts.get("tenant_id")
        skip_empty = opts.get("skip_empty", False)
        dry_run = opts.get("dry_run", False)
        to_override = opts.get("to")

        if tenant_id:
            # Explicit single-tenant run targets any tenant, even archived.
            tenants_qs = Tenant.objects.filter(id=tenant_id)
        else:
            # Scheduled run: never email tenants archived by rename.
            tenants_qs = active_tenants()

        mailers = []
        sent = 0
        skipped = 0
        for tenant in tenants_qs:
            digest = build_tenant_digest(
                tenant,
                window_label=window_label,
                upcoming_days=upcoming_days,
            )

            if skip_empty and digest.is_empty:
                self.stdout.write(
                    f"  · skip empty digest for tenant {tenant.id} ({tenant.name})"
                )
                skipped += 1
                continue

            recipients = (
                [r.strip() for r in to_override.split(",") if r.strip()]
                if to_override
                else admin_recipients_for_tenant(tenant)
            )
            if not recipients:
                self.stdout.write(
                    f"  · skip tenant {tenant.id} ({tenant.name}) — no admin recipients"
                )
                skipped += 1
                continue

            self.stdout.write(
                f"  · tenant {tenant.id} ({tenant.name}) — "
                f"pending {digest.pending_approvals.count}, "
                f"unfiled {digest.unfiled_recaps.count}, "
                f"upcoming {digest.upcoming_shifts.count} → {len(recipients)} recipients"
            )

            if dry_run:
                continue

            mailers.append(
                AdminDigestMailer(digest=digest, to_emails=recipients)
            )
            sent += 1

        if mailers and not dry_run:
            # Fire one chain so RQ enqueues sequentially. Falls back to
            # inline send on Cloud Run where Redis isn't reachable.
            MailChain(mailers).send()

        self.stdout.write(
            self.style.SUCCESS(
                f"send_admin_digest complete: {sent} sent, {skipped} skipped, "
                f"window={window}"
            )
        )
