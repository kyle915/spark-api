"""Seed Tenant.retail_sheet_id from standing brand-group constants.

Writes sheet IDs only when empty. Never remints or overwrites checkin_code.
Standing codes stay in events.brand_group_sheet; this only parks the sheet
id on the tenant row so ops can override without a Python deploy.

    python manage.py seed_brand_group_sheet_config
    python manage.py seed_brand_group_sheet_config --apply
"""

from __future__ import annotations

from django.core.management.base import BaseCommand

from events.brand_group_sheet import (
    BRAND_GROUP_SHEET_ID,
    PIVOT_SHEET_ID,
    PIVOT_TENANT_SLUG,
)
from tenants.models import Tenant

_SHEET_BY_SLUG: dict[str, str] = {
    "famus-energy": BRAND_GROUP_SHEET_ID,
    "alcohol-armor": BRAND_GROUP_SHEET_ID,
    "wook-water": BRAND_GROUP_SHEET_ID,
    PIVOT_TENANT_SLUG: PIVOT_SHEET_ID,
}


class Command(BaseCommand):
    help = (
        "Seed empty Tenant.retail_sheet_id from brand-group constants "
        "(never remints check-in codes)."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--apply",
            action="store_true",
            help="Write empty retail_sheet_id values. Default is dry-run.",
        )

    def handle(self, *args, **opts):
        apply = bool(opts.get("apply"))
        w = self.stdout.write
        if not apply:
            w(self.style.WARNING("DRY RUN — pass --apply to write."))

        updated = 0
        for slug, sheet_id in _SHEET_BY_SLUG.items():
            tenant = (
                Tenant.objects.filter(slug=slug)
                .only("id", "slug", "retail_sheet_id", "checkin_code")
                .first()
            )
            if tenant is None:
                w(f"MISSING tenant slug={slug}")
                continue
            current = (tenant.retail_sheet_id or "").strip()
            if current:
                if current != sheet_id:
                    w(
                        f"KEEP slug={slug} retail_sheet_id={current!r} "
                        f"(differs from standing {sheet_id!r} — not overwritten)"
                    )
                else:
                    w(f"OK slug={slug} retail_sheet_id already set")
                continue
            w(f"SEED slug={slug} retail_sheet_id={sheet_id!r}")
            if apply:
                Tenant.objects.filter(pk=tenant.pk).update(
                    retail_sheet_id=sheet_id
                )
                updated += 1

        summary = (
            f"seed_brand_group_sheet_config: wrote {updated} row(s)"
            if apply
            else "seed_brand_group_sheet_config: dry-run complete"
        )
        w(self.style.SUCCESS(summary) if apply else summary)
