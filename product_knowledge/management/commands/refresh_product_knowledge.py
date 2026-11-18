"""Refresh what the BA product-questions box can answer from.

    manage.py refresh_product_knowledge --list
    manage.py refresh_product_knowledge --tenant 17 [--scope website|documents|all]
    manage.py refresh_product_knowledge --tenant 17 --dry-run   # show what would be read

Without --tenant every brand with a website or documents is refreshed in
turn (the weekly workflow calls it one brand per request instead, so one
slow site can't time the whole run out).
"""

from __future__ import annotations

import json

from django.core.management.base import BaseCommand, CommandError

from product_knowledge.crawler import normalize_site_url
from product_knowledge.documents import discover
from product_knowledge.refresh import SCOPES, refresh_tenant, tenants_to_refresh


class Command(BaseCommand):
    help = "Re-read brand documents and websites for BA product answers."

    def add_arguments(self, parser):
        parser.add_argument("--tenant", type=int, action="append", default=[])
        parser.add_argument("--scope", default="all", choices=SCOPES)
        parser.add_argument(
            "--list", action="store_true", help="Print tenant ids to refresh."
        )
        parser.add_argument("--dry-run", action="store_true")

    def handle(self, *args, **opts):
        from tenants.models import Tenant

        ids = opts["tenant"] or tenants_to_refresh()
        if opts["list"]:
            self.stdout.write(json.dumps({"tenants": ids}))
            return
        for tid in ids:
            tenant = Tenant.objects.filter(pk=tid).first()
            if tenant is None:
                raise CommandError(f"Tenant {tid} not found.")
            if opts["dry_run"]:
                site = normalize_site_url(tenant.brand_website_url) or "(none)"
                docs = discover(tenant)
                self.stdout.write(
                    f"[{tid}] {tenant.name}: website {site}; {len(docs)} document(s)"
                )
                for d in docs:
                    self.stdout.write(f"    - {d.kind}: {d.title}")
                continue
            stats = refresh_tenant(tenant, scope=opts["scope"])
            self.stdout.write(
                f"[{tid}] {tenant.name}: {json.dumps(stats, default=str)}"
            )
