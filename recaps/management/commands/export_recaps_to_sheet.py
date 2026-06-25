"""Export a tenant's recap data to their linked Google Sheet (full refresh).

Writes one row per recap — event/BA metadata plus every custom-template field
value (including the demographic breakdowns) — into the tenant's
`recap_export_sheet_url`, clearing and rewriting the target tab each run.

Usage:
    python manage.py export_recaps_to_sheet --tenant-slug girl-beer --apply
    python manage.py export_recaps_to_sheet --tenant-slug girl-beer \
        --sheet-url "https://docs.google.com/spreadsheets/d/<id>/edit" --apply
    python manage.py export_recaps_to_sheet --all-linked --apply

Without --apply it's a dry run (reports row/column counts, writes nothing).
--sheet-url (single tenant only) is persisted to Tenant.recap_export_sheet_url
before syncing, so the daily workflow can seed the URL without DB access.
"""
from __future__ import annotations

from django.core.management.base import BaseCommand, CommandError

from recaps.recap_sheet_export import (
    DEFAULT_TAB,
    build_export_grid,
    export_tenant_recaps_to_sheet,
    refresh_recap_export,
)
from tenants.models import Tenant


class Command(BaseCommand):
    help = "Full-refresh a tenant's recap data into their recap_export_sheet_url."

    def add_arguments(self, parser):
        parser.add_argument("--tenant-id", type=int)
        parser.add_argument("--tenant-slug", type=str)
        parser.add_argument("--all-linked", action="store_true")
        parser.add_argument("--sheet-url", type=str, default=None)
        parser.add_argument("--tab", type=str, default=DEFAULT_TAB)
        parser.add_argument("--apply", action="store_true")

    def handle(self, *args, **opts):
        tenants = self._resolve(opts)
        if not tenants:
            raise CommandError(
                "No tenant matched. Use --tenant-slug / --tenant-id / --all-linked."
            )
        sheet_url = opts.get("sheet_url")
        if sheet_url and len(tenants) > 1:
            raise CommandError("--sheet-url is only valid with a single tenant.")
        tab = opts["tab"]
        apply = opts["apply"]

        for tenant in tenants:
            if sheet_url:
                tenant.recap_export_sheet_url = sheet_url
                tenant.save(update_fields=["recap_export_sheet_url"])
                self.stdout.write(f"Set recap_export_sheet_url for {tenant.slug}.")

            url = tenant.recap_export_sheet_url
            if not url:
                self.stdout.write(
                    self.style.WARNING(
                        f"{tenant.slug}: no recap_export_sheet_url set — skipping."
                    )
                )
                continue

            header, rows = build_export_grid(tenant)
            self.stdout.write(
                f"{tenant.slug}: {len(rows)} recap row(s) x {len(header)} column(s) -> {url}"
            )
            if not apply:
                self.stdout.write("  (dry run — pass --apply to write)")
                continue

            # Default tab → central dispatcher (raw-data export + optional
            # branded recaps tab + optional computed Summary dashboard). A
            # non-default --tab targets the raw export directly.
            if tab == DEFAULT_TAB:
                result = refresh_recap_export(tenant)
            else:
                result = export_tenant_recaps_to_sheet(tenant, tab=tab)
            if result.get("ok"):
                self.stdout.write(
                    self.style.SUCCESS(
                        f"  wrote {result['rows']} row(s) to tab '{result['tab']}'."
                    )
                )
                summary = result.get("summary")
                if summary is not None:
                    if summary.get("ok"):
                        self.stdout.write(
                            self.style.SUCCESS(
                                f"  Summary '{summary.get('tab')}' rebuilt: "
                                f"{summary.get('demos')} demos, "
                                f"{summary.get('ambassadors')} BAs, "
                                f"{summary.get('locations')} locations, "
                                f"{summary.get('dates')} dates."
                            )
                        )
                    else:
                        self.stdout.write(
                            self.style.WARNING(f"  Summary FAILED: {summary}")
                        )
            else:
                self.stdout.write(self.style.ERROR(f"  FAILED: {result}"))

    def _resolve(self, opts) -> list:
        if opts.get("tenant_id"):
            return list(Tenant.objects.filter(id=opts["tenant_id"]))
        if opts.get("tenant_slug"):
            return list(Tenant.objects.filter(slug=opts["tenant_slug"]))
        if opts.get("all_linked"):
            return list(
                Tenant.objects.exclude(recap_export_sheet_url__isnull=True)
                .exclude(recap_export_sheet_url__exact="")
                .order_by("name")
            )
        return []
