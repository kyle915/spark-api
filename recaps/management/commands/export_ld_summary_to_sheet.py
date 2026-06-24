"""Rebuild Liquid Death's Summary tab from Spark recap data (branded LD).

Usage:
    python manage.py export_ld_summary_to_sheet --tenant-slug ighn-liquid-death \
        --sheet-url "https://docs.google.com/spreadsheets/d/<id>/edit" --apply
    # stage to a scratch tab first, eyeball, then swap to the live Summary:
    python manage.py export_ld_summary_to_sheet --target-tab "Summary (staging)" --apply

Dry run by default (prints computed KPIs, writes nothing).
"""
from __future__ import annotations

from django.core.management.base import BaseCommand, CommandError

from recaps.ld_summary_export import DEFAULT_SUMMARY_TAB, write_ld_summary
from tenants.models import Tenant


class Command(BaseCommand):
    help = "Recompute + rebuild the Liquid Death Summary tab from Spark recaps."

    def add_arguments(self, parser):
        parser.add_argument("--tenant-slug", type=str, default="ighn-liquid-death")
        parser.add_argument("--tenant-id", type=int)
        parser.add_argument("--sheet-url", type=str, default=None)
        parser.add_argument("--tab", type=str, default=DEFAULT_SUMMARY_TAB)
        parser.add_argument("--target-tab", type=str, default=None)
        parser.add_argument("--apply", action="store_true")

    def handle(self, *args, **opts):
        tenant = self._resolve(opts)
        if tenant is None:
            raise CommandError("No Liquid Death tenant matched.")
        self.stdout.write(f"Tenant: {tenant.name} (slug={tenant.slug})")

        result = write_ld_summary(
            tenant,
            tab=opts["tab"],
            target_tab=opts.get("target_tab"),
            sheet_url=opts.get("sheet_url"),
            dry_run=not opts["apply"],
        )
        if not result.get("ok"):
            self.stdout.write(self.style.ERROR(f"FAILED: {result}"))
            return

        self.stdout.write(
            f"  events_run={result.get('events_run')} consumers={result.get('consumers')} "
            f"cans_sold={result.get('cans_sold')} multi_packs={result.get('multi_packs')} "
            f"brand_awareness={result.get('brand_awareness_pct')}% "
            f"purchase_intent={result.get('purchase_intent_pct')}%"
        )
        self.stdout.write(f"  app custom-recaps: {result.get('app_recaps')}")
        self.stdout.write(f"  by year: {result.get('by_year')}")
        if result.get("dry_run"):
            self.stdout.write("  (dry run — pass --apply to write the Summary tab)")
        else:
            self.stdout.write(
                self.style.SUCCESS(
                    f"  wrote {result['rows']} rows to tab '{result['tab']}'."
                )
            )

    def _resolve(self, opts):
        if opts.get("tenant_id"):
            return Tenant.objects.filter(id=opts["tenant_id"]).first()
        slug = opts.get("tenant_slug")
        t = Tenant.objects.filter(slug=slug).first()
        if t is None:
            # Slug ambiguity (ighn-liquid-death vs liquid-death) — fall back to name.
            t = Tenant.objects.filter(name__icontains="liquid death").first()
        return t
