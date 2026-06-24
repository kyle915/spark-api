"""Write Liquid Death's raw recap data into a branded "Spark Recaps" tab.

Usage:
    # dry run (counts only, writes nothing):
    python manage.py export_ld_recaps_to_sheet --tenant-slug ighn-liquid-death
    # write + pin the LD recap-export config (tab, sheet, on-submit refresh):
    python manage.py export_ld_recaps_to_sheet \
        --sheet-url "https://docs.google.com/spreadsheets/d/<id>/edit" --apply

When --apply, pins Tenant.recap_export_tab_name / recap_export_sheet_url /
recap_export_on_submit so the daily cron + the on-save signal target this tab.
"""
from __future__ import annotations

from django.core.management.base import BaseCommand, CommandError

from recaps.ld_recaps_export import DEFAULT_RECAPS_TAB, write_ld_recaps
from tenants.models import Tenant


class Command(BaseCommand):
    help = "Write Liquid Death raw recap data into a branded Spark Recaps tab."

    def add_arguments(self, parser):
        parser.add_argument("--tenant-slug", type=str, default="ighn-liquid-death")
        parser.add_argument("--tenant-id", type=int)
        parser.add_argument("--sheet-url", type=str, default=None)
        parser.add_argument("--tab", type=str, default=DEFAULT_RECAPS_TAB)
        parser.add_argument("--year", type=int, default=None, help="Only this event-year (omit = all).")
        parser.add_argument(
            "--no-on-submit",
            action="store_true",
            help="Do NOT enable the on-save refresh (default enables it).",
        )
        parser.add_argument("--apply", action="store_true")

    def handle(self, *args, **opts):
        tenant = self._resolve(opts)
        if tenant is None:
            raise CommandError("No Liquid Death tenant matched.")
        self.stdout.write(f"Tenant: {tenant.name} (slug={tenant.slug})")

        tab = opts["tab"]
        if opts["apply"]:
            changed = []
            sheet_url = opts.get("sheet_url") or tenant.recap_export_sheet_url or tenant.linked_sheet_url
            if sheet_url and (tenant.recap_export_sheet_url or "") != sheet_url:
                tenant.recap_export_sheet_url = sheet_url
                changed.append("recap_export_sheet_url")
            if (tenant.recap_export_tab_name or "") != tab:
                tenant.recap_export_tab_name = tab
                changed.append("recap_export_tab_name")
            want_on_submit = not opts["no_on_submit"]
            if bool(tenant.recap_export_on_submit) != want_on_submit:
                tenant.recap_export_on_submit = want_on_submit
                changed.append("recap_export_on_submit")
            if changed:
                tenant.save(update_fields=changed)
                self.stdout.write(f"  pinned config: {changed}")

        result = write_ld_recaps(
            tenant,
            tab=tab,
            sheet_url=opts.get("sheet_url"),
            year=opts.get("year"),
            dry_run=not opts["apply"],
        )
        if not result.get("ok"):
            self.stdout.write(self.style.ERROR(f"FAILED: {result}"))
            return

        self.stdout.write(f"  rows={result['rows']} columns={result['columns']}")
        if result.get("dry_run"):
            self.stdout.write("  (dry run — pass --apply to write the Spark Recaps tab)")
        else:
            self.stdout.write(
                self.style.SUCCESS(
                    f"  wrote {result['rows']} recap rows to tab '{result['tab']}' "
                    f"(formatted={result.get('formatted')})."
                )
            )

    def _resolve(self, opts):
        if opts.get("tenant_id"):
            return Tenant.objects.filter(id=opts["tenant_id"]).first()
        slug = opts.get("tenant_slug")
        t = Tenant.objects.filter(slug=slug).first()
        if t is None:
            t = Tenant.objects.filter(name__icontains="liquid death").first()
        return t
