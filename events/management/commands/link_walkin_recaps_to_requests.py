"""Attach filed walk-up Events onto Tracker Requests that still show DUE.

See ``events.walkin_link`` for match rules. Dry-run is the default.

Usage:
    python manage.py link_walkin_recaps_to_requests --tenant-slug famus-energy
    python manage.py link_walkin_recaps_to_requests --tenant-slug famus-energy --apply
"""

from __future__ import annotations

import json

from django.core.management.base import BaseCommand, CommandError

from events.walkin_link import link_walkin_recaps_for_tenant
from tenants.models import Tenant
from utils.cron_db import cron_db_session


class Command(BaseCommand):
    help = (
        "Link filed request-less walk-up Events onto Tracker Requests that "
        "still show DUE. High-confidence only. Dry-run default."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--tenant-slug",
            type=str,
            default="famus-energy",
            help="Exact tenant slug (default: famus-energy).",
        )
        parser.add_argument("--days-back", type=int, default=45)
        parser.add_argument(
            "--apply",
            action="store_true",
            help="Write request_id on matched orphans.",
        )
        parser.add_argument(
            "--all-brand-group",
            action="store_true",
            help="Run for every tenant that has a brand_group set.",
        )
        parser.add_argument(
            "--diagnose",
            action="store_true",
            help="Include unmatched orphan details in JSON_RESULT.",
        )

    def handle(self, *args, **opts):
        apply = bool(opts["apply"])
        days_back = max(1, int(opts["days_back"]))

        if not apply:
            self.stdout.write(
                self.style.WARNING("DRY RUN — pass --apply to write links.\n")
            )

        tenants: list[Tenant] = []
        if opts.get("all_brand_group"):
            tenants = list(
                Tenant.objects.filter(brand_group__isnull=False).order_by("slug")
            )
            if not tenants:
                raise CommandError("No brand_group tenants found.")
        else:
            slug = (opts["tenant_slug"] or "").strip()
            if not slug:
                raise CommandError("--tenant-slug is required.")
            tenant = Tenant.objects.filter(slug=slug).first()
            if tenant is None:
                tenant = Tenant.objects.filter(request_url_name=slug).first()
            if tenant is None:
                raise CommandError(f"No tenant with slug/request_url_name={slug!r}.")
            tenants = [tenant]

        combined = []
        with cron_db_session():
            for tenant in tenants:
                result = link_walkin_recaps_for_tenant(
                    tenant,
                    days_back=days_back,
                    apply=apply,
                    diagnose=bool(opts.get("diagnose")),
                )
                for row in result.rows:
                    status = row.get("status")
                    if status in ("link", "would-link"):
                        self.stdout.write(
                            f"  {'+' if apply else '~'} REQ-{row.get('request_id')} "
                            f"← event {row.get('orphan_id')}"
                        )
                self.stdout.write(
                    f"\ntenant={result.tenant_slug} linked={result.linked} "
                    f"would_link={result.would_link} ambiguous={result.ambiguous} "
                    f"orphans_with_filed={result.orphans_with_filed}"
                )
                entry = {
                    "tenant": result.tenant_slug,
                    "apply": result.apply,
                    "linked": result.linked,
                    "would_link": result.would_link,
                    "ambiguous": result.ambiguous,
                    "orphans_with_filed": result.orphans_with_filed,
                    "rows": result.rows,
                }
                if result.orphan_details is not None:
                    entry["orphan_details"] = result.orphan_details
                combined.append(entry)

        self.stdout.write("JSON_RESULT:" + json.dumps({"tenants": combined}))
