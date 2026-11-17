"""
Cron entrypoint for the weekly executive summary email.

Run from the GHA cron at `daily-admin-digest.yml`'s sibling
`weekly-executive-summary.yml`. Walks every active tenant, builds
its ExecutiveSummary, sends to that tenant's admin recipients.

Usage:
    python manage.py send_executive_summary                # last 7 days
    python manage.py send_executive_summary --days 14      # custom window
    python manage.py send_executive_summary --tenant-id 12 # one tenant
    python manage.py send_executive_summary --skip-empty   # don't email
                                                           # tenants with 0 recaps
    python manage.py send_executive_summary --dry-run      # log only
    python manage.py send_executive_summary --to me@x.com  # override
"""

from __future__ import annotations

from django.core.management.base import BaseCommand
from django.db.models.functions import Lower

from digest.envelopes import ExecutiveSummaryMailer
from digest.exec_services import build_executive_summary
from digest.services import active_tenants, admin_recipients_for_tenant
from tenants.models import Tenant, User
from utils.graphql.permissions import _is_admin_access


def split_staff_recipients(emails: list[str]) -> tuple[list[str], list[str]]:
    """Split recipients into (Ignite staff, everyone else).

    Staff = the platform-admin test the schema uses (``_is_admin_access``:
    is_staff / superuser / spark-admin role / Ignite domain, minus
    IGNITE_ADMIN_EXCLUDE). An address with no user row is judged on its
    domain alone, so an unknown ``--to`` address lands in "everyone else".
    Only the staff list may see ambassador names.
    """
    users = {
        (u.email or "").lower(): u
        for u in User.objects.annotate(email_lower=Lower("email"))
        .filter(email_lower__in=[e.lower() for e in emails])
        .select_related("role")
    }
    staff: list[str] = []
    others: list[str] = []
    for email in emails:
        u = users.get(email.lower())
        is_staff = _is_admin_access(
            getattr(getattr(u, "role", None), "slug", None) if u else None,
            bool(u and u.is_staff),
            bool(u and u.is_superuser),
            email,
        )
        (staff if is_staff else others).append(email)
    return staff, others


class Command(BaseCommand):
    help = (
        "Send the weekly executive summary email to admins of every "
        "active tenant."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--days",
            type=int,
            default=7,
            help="Window size in days. Default 7 (rolling Monday-to-Monday).",
        )
        parser.add_argument(
            "--tenant-id",
            type=int,
            default=None,
            help="Only build/send the summary for this tenant.",
        )
        parser.add_argument(
            "--skip-empty",
            action="store_true",
            help="Don't send when the tenant filed 0 recaps in the window.",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Build the summary + log the headline but skip the send.",
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
        days = max(1, min(int(opts["days"] or 7), 365))
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

        sent = 0
        skipped = 0
        for tenant in tenants_qs:
            summary = build_executive_summary(tenant, window_days=days)

            self.stdout.write(
                f"  · tenant {tenant.id} ({tenant.name}) — "
                f"{summary.recap_count} recaps, "
                f"{summary.consumer_reach:,} consumers, "
                f"{summary.samples_distributed} samples"
            )

            if skip_empty and summary.is_empty:
                self.stdout.write(
                    f"    ↳ skip (empty window)"
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
                    f"    ↳ skip (no admin recipients)"
                )
                skipped += 1
                continue

            # Ignite staff get the ambassador leaderboard; anyone else (a
            # client admin, an unknown --to address) gets the same email
            # without ambassador names.
            staff, others = split_staff_recipients(recipients)

            if dry_run:
                self.stdout.write(
                    f"    ↳ would email {len(staff)} staff (with BA names) + "
                    f"{len(others)} other (no BA names) recipients (dry-run)"
                )
                continue

            for group, include_ba_names in ((staff, True), (others, False)):
                if not group:
                    continue
                ExecutiveSummaryMailer(
                    summary=summary,
                    to_emails=group,
                    include_ba_names=include_ba_names,
                ).send_now()
                sent += 1

        self.stdout.write(
            self.style.SUCCESS(
                f"Done. Sent: {sent} · Skipped: {skipped}"
            )
        )
