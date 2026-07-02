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

    # Scoped: only pending requests dated today or later (skip the
    # historical backlog when the client's sheet already has its own
    # history for everything before that)
    python manage.py sync_tenant_to_sheet --tenant-slug liquid-death \\
        --since-date 2026-07-01 --status-slug pending --apply
"""
from django.core.management.base import BaseCommand, CommandError

from events.models import Request
from tenants.models import Tenant
from utils.sheets_mirror import bulk_sync_requests, delete_ld_rows


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
        parser.add_argument(
            "--since-date",
            type=str,
            default=None,
            help="Only sync requests with date >= this (YYYY-MM-DD). "
            "Skips the historical backlog — useful when a tenant's sheet "
            "already carries its own history and only recent/future "
            "activity needs to land in Spark's tracked rows.",
        )
        parser.add_argument(
            "--status-slug",
            type=str,
            default=None,
            help="Only sync requests whose status slug matches exactly "
            "(e.g. 'pending' for submitted-but-not-yet-approved requests).",
        )
        parser.add_argument(
            "--delete-rows",
            type=str,
            default=None,
            help="Comma-separated 1-based sheet row numbers to delete BEFORE "
            "syncing — prunes a client's hand-entered duplicates once "
            "Spark's keyed rows for the same events exist. Guarded: "
            "ld_retail layout only, rows 2-40 only, never a row carrying "
            "a Spark key, and only with --apply.",
        )
        tenants = self._resolve_tenants(opts)
        if not tenants:
            raise CommandError(
                "No tenants matched the filters you passed. "
                "Use --tenant-id, --tenant-slug, or --all-linked."
            )

        apply = opts["apply"]
        limit = opts.get("limit")
        since_date = opts.get("since_date")
        status_slug = opts.get("status_slug")

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

            qs = Request.objects.filter(tenant_id=tenant.id)
            if since_date:
                qs = qs.filter(date__gte=since_date)
            if status_slug:
                qs = qs.filter(status__slug=status_slug)
            qs = qs.order_by("date", "id")
            if limit:
                qs = qs[:limit]

            count = qs.count()
            grand_total += count
            scope = []
            if since_date:
                scope.append(f"date>={since_date}")
            if status_slug:
                scope.append(f"status={status_slug}")
            scope_note = f" ({', '.join(scope)})" if scope else ""
            self.stdout.write("")
            self.stdout.write(
                self.style.NOTICE(
                    f"[{tenant.name}] {count} request(s){scope_note} → {sheet}"
                )
            )

            if not apply:
                continue

            delete_rows = opts.get("delete_rows")
            if delete_rows:
                try:
                    row_nums = [int(x) for x in delete_rows.split(",") if x.strip()]
                except ValueError:
                    raise CommandError(
                        f"--delete-rows must be comma-separated integers, got {delete_rows!r}"
                    )
                pruned, prune_notes = delete_ld_rows(tenant, row_nums)
                for note in prune_notes:
                    self.stdout.write(f"  · {note}")
                self.stdout.write(
                    self.style.SUCCESS(f"  ✓ pruned {pruned} duplicate row(s)")
                )

            # Batched write — a handful of API calls for the whole tenant,
            # vs. ~3/row, so we stay well under the Sheets 60-req/min quota.
            ok, error = bulk_sync_requests(list(qs))
            grand_total_ok += ok
            self.stdout.write(self.style.SUCCESS(f"  ✓ synced {ok}/{count}"))
            if ok < count:
                detail = f": {error}" if error else " — see logs."
                self.stdout.write(
                    self.style.WARNING(
                        f"  ! {count - ok} row(s) not written{detail}"
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
