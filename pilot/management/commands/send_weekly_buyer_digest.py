"""Email each opted-in workspace the weekly buyer store-report digest.

SAFE DEFAULT — OPT-IN OFF. Only ``WorkspaceProfile.buyer_weekly_digest_enabled``
tenants receive mail. Quiet weeks (no visits / rows) are skipped unless
``--force``. Distinct from ``send_client_weekly_digest`` (field-marketing
activity rollup).

Usage:
    python manage.py send_weekly_buyer_digest
    python manage.py send_weekly_buyer_digest --dry-run
    python manage.py send_weekly_buyer_digest --tenant 12 --force
"""

from __future__ import annotations

import logging
from datetime import date

from django.core.management.base import BaseCommand, CommandError

from pilot.buyer_digest import send_buyer_digest_for_tenant
from pilot.models import WorkspaceProfile
from tenants.models import Tenant

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = (
        "Email the weekly buyer store report (CSV attached) to each workspace "
        "with buyer_weekly_digest_enabled=True. Quiet weeks skipped unless "
        "--force. Opt-in OFF by default."
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
            help="Restrict to a single tenant id (does not bypass opt-in).",
        )
        parser.add_argument(
            "--force",
            action="store_true",
            help="Send even if the week has no visits / store rows.",
        )
        parser.add_argument(
            "--only-email",
            default="",
            help="Send to this one address only. Requires --tenant.",
        )
        parser.add_argument(
            "--period-start",
            default="",
            help="YYYY-MM-DD start of report window (optional).",
        )
        parser.add_argument(
            "--period-end",
            default="",
            help="YYYY-MM-DD end of report window (optional).",
        )

    def handle(self, *args, **opts):
        dry_run = bool(opts["dry_run"])
        force = bool(opts["force"])
        only_tenant = opts.get("tenant")
        only_email = (opts.get("only_email") or "").strip()
        if only_email and only_tenant is None:
            raise CommandError("--only-email requires --tenant")

        def _parse_day(raw: str, label: str) -> date | None:
            raw = (raw or "").strip()
            if not raw:
                return None
            try:
                return date.fromisoformat(raw)
            except ValueError as exc:
                raise CommandError(f"Bad {label}: {raw!r}") from exc

        period_start = _parse_day(opts.get("period_start") or "", "--period-start")
        period_end = _parse_day(opts.get("period_end") or "", "--period-end")

        profiles = WorkspaceProfile.objects.select_related("tenant").filter(
            buyer_weekly_digest_enabled=True,
            tenant__in=Tenant.active(),
        )
        if only_tenant is not None:
            profiles = profiles.filter(tenant_id=only_tenant)

        mode = "DRY-RUN (no email will be sent)" if dry_run else "live"
        self.stdout.write(
            f"Weekly buyer digest — mode {mode}"
            f"{' (forced)' if force else ''}"
            f"{f' (only {only_email})' if only_email else ''}."
        )

        enabled = 0
        sent = 0
        skipped = 0
        failed = 0

        for profile in profiles.iterator():
            enabled += 1
            tenant = profile.tenant
            try:
                result = send_buyer_digest_for_tenant(
                    tenant,
                    dry_run=dry_run,
                    force=force,
                    only_email=only_email,
                    period_start=period_start,
                    period_end=period_end,
                )
                status = result.get("status")
                if status in {"sent", "dry_run"}:
                    sent += 1
                    self.stdout.write(
                        f"  - {tenant.name} (id={tenant.id}): {status} → "
                        f"{result.get('recipients')} "
                        f"rows={result.get('row_count')} "
                        f"visits={result.get('total_visits', '?')}."
                    )
                elif status == "failed_send":
                    failed += 1
                    self.stdout.write(
                        self.style.ERROR(
                            f"  - {tenant.name} (id={tenant.id}): FAIL — "
                            f"mail provider rejected digest "
                            f"(recipients={result.get('recipients')})."
                        )
                    )
                else:
                    skipped += 1
                    self.stdout.write(
                        f"  - {tenant.name} (id={tenant.id}): SKIP — {status}."
                    )
            except Exception:
                failed += 1
                logger.exception(
                    "Weekly buyer digest failed for tenant=%s (%s).",
                    tenant.id,
                    getattr(tenant, "name", None),
                )
                self.stdout.write(
                    f"  - {getattr(tenant, 'name', '?')} "
                    f"(id={getattr(tenant, 'id', '?')}): FAILED — see logs."
                )

        self.stdout.write(
            self.style.SUCCESS(
                f"Done. enabled={enabled} sent={sent} "
                f"skipped={skipped} failed={failed}."
            )
        )
