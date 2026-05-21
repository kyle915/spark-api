"""
One-shot backfill of a tenant's Requests into their linked Google Sheet.

Why a separate command vs. just relying on post_save signals: a fresh
tenant just shared their Sheet but has years of historical Requests
that never triggered a sync. This walks every Request for the
selected tenant and runs the same upsert path the live signal uses,
so the Sheet ends up in step with the database.

Usage:
    # Dry run, by tenant id
    python manage.py sync_tenant_to_sheet --tenant-id 9

    # Apply, by slug
    python manage.py sync_tenant_to_sheet --tenant-slug ighn-liquid-death --apply

    # All tenants that have a linked_sheet_url set
    python manage.py sync_tenant_to_sheet --all-linked --apply
"""
import time

from django.core.management.base import BaseCommand, CommandError

from events.models import Request
from tenants.models import Tenant
from utils.sheets_mirror import upsert_request_row


class Command(BaseCommand):
    help = (
        "Backfill a tenant's Requests into their linked Google Sheet. "
        "Idempotent — re-running just refreshes the rows."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--tenant-id",
            type=int,
            help="Single tenant by primary key.",
        )
        parser.add_argument(
            "--tenant-slug",
            type=str,
            help="Single tenant by slug (e.g. ighn-liquid-death).",
        )
        parser.add_argument(
            "--all-linked",
            action="store_true",
            help="Sync every tenant that has a non-empty linked_sheet_url.",
        )
        parser.add_argument(
            "--apply",
            action="store_true",
            help="Actually write. Without this, the command prints what "
            "it WOULD do but never touches the sheet.",
        )
        parser.add_argument(
            "--throttle-ms",
            type=int,
            default=120,
            help="Sleep N ms between row writes to stay under the "
            "Google Sheets API per-minute quota. Default 120ms "
            "(~500 writes/minute, well under the 60/min/user limit "
            "but adjustable for big tenants).",
        )
        parser.add_argument(
            "--limit",
            type=int,
            default=None,
            help="Cap the number of requests processed per tenant. "
            "Useful for smoke-testing the pipeline before a big run.",
        )

    def handle(self, *args, **opts):
        tenants = self._resolve_tenants(opts)
        if not tenants:
            raise CommandError(
                "No tenants matched the filters you passed. "
                "Use --tenant-id, --tenant-slug, or --all-linked."
            )

        apply = opts["apply"]
        throttle = max(0, int(opts["throttle_ms"])) / 1000.0
        limit = opts.get("limit")

        if not apply:
            self.stdout.write(
                self.style.WARNING(
                    "DRY RUN — pass --apply to actually write to the sheet.\n"
                )
            )

        grand_total_ok = 0
        grand_total = 0

        for tenant in tenants:
            sheet = (tenant.linked_sheet_url or "").strip()
            if not sheet:
                self.stdout.write(
                    f"  · skipping {tenant.name} ({tenant.id}) — no linked_sheet_url"
                )
                continue

            qs = Request.objects.filter(tenant_id=tenant.id).order_by("date", "id")
            if limit:
                qs = qs[:limit]

            count = qs.count()
            grand_total += count
            self.stdout.write("")
            self.stdout.write(
                self.style.NOTICE(
                    f"[{tenant.name}] {count} request(s) → {sheet}"
                )
            )

            if not apply:
                continue

            ok = 0
            failed: list[int] = []
            for i, req in enumerate(qs.iterator(chunk_size=200), start=1):
                if upsert_request_row(req):
                    ok += 1
                else:
                    failed.append(req.id)
                if throttle:
                    time.sleep(throttle)
                if i % 25 == 0:
                    self.stdout.write(f"  · {i}/{count} synced…")

            grand_total_ok += ok
            self.stdout.write(
                self.style.SUCCESS(f"  ✓ synced {ok}/{count}")
            )
            if failed:
                preview = ", ".join(str(x) for x in failed[:10])
                more = "" if len(failed) <= 10 else f" (+{len(failed)-10} more)"
                self.stdout.write(
                    self.style.WARNING(
                        f"  ! {len(failed)} failed — ids: {preview}{more}"
                    )
                )

        if apply:
            self.stdout.write("")
            self.stdout.write(
                self.style.SUCCESS(
                    f"Done. {grand_total_ok}/{grand_total} rows written."
                )
            )

    def _resolve_tenants(self, opts) -> list[Tenant]:
        if opts.get("tenant_id"):
            return list(Tenant.objects.filter(id=opts["tenant_id"]))
        if opts.get("tenant_slug"):
            return list(Tenant.objects.filter(slug=opts["tenant_slug"]))
        if opts.get("all_linked"):
            return list(
                Tenant.objects.exclude(linked_sheet_url__isnull=True)
                .exclude(linked_sheet_url__exact="")
                .order_by("name")
            )
        return []
