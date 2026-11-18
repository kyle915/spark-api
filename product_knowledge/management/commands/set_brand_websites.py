"""Set brands' website URLs in bulk from a small JSON map. DRY RUN by default.

    manage.py set_brand_websites --json '{"17": "https://torchthc.com", "liquid-death": "liquiddeath.com"}'
    manage.py set_brand_websites --map-file sites.json --apply

Keys are a tenant id, slug, or exact name (case-insensitive). A value of ""
clears the site. Nothing is crawled here — the next refresh picks it up.
"""

from __future__ import annotations

import json

from django.core.management.base import BaseCommand, CommandError

from product_knowledge.crawler import normalize_site_url, site_host


def resolve_tenant(key: str):
    from tenants.models import Tenant

    key = str(key).strip()
    if key.isdigit():
        return Tenant.objects.filter(pk=int(key)).first()
    match = Tenant.objects.filter(slug__iexact=key).first()
    if match:
        return match
    named = list(Tenant.objects.filter(name__iexact=key)[:2])
    return named[0] if len(named) == 1 else None


def apply_map(mapping: dict, *, apply: bool) -> list[dict]:
    from product_knowledge.models import KnowledgeSource

    rows = []
    for key, raw in mapping.items():
        tenant = resolve_tenant(key)
        url = normalize_site_url(str(raw or ""))
        row = {"key": key, "tenant": None, "from": "", "to": url, "status": ""}
        if tenant is None:
            row["status"] = "tenant-not-found"
        elif str(raw or "").strip() and not url:
            row.update(tenant=tenant.id, status="invalid-url")
        else:
            row.update(
                tenant=tenant.id, name=tenant.name, **{"from": tenant.brand_website_url}
            )
            if tenant.brand_website_url == url:
                row["status"] = "unchanged"
            elif not apply:
                row["status"] = "would-set"
            else:
                old = normalize_site_url(tenant.brand_website_url)
                if old and (not url or site_host(old) != site_host(url)):
                    KnowledgeSource.objects.filter(
                        tenant_id=tenant.id, kind__in=KnowledgeSource.WEBSITE_KINDS
                    ).delete()
                tenant.brand_website_url = url
                tenant.save(update_fields=["brand_website_url", "updated_at"])
                row["status"] = "set"
        rows.append(row)
    return rows


class Command(BaseCommand):
    help = (
        "Set Tenant.brand_website_url in bulk from a JSON map (dry run unless --apply)."
    )

    def add_arguments(self, parser):
        parser.add_argument("--json", default="")
        parser.add_argument("--map-file", default="")
        parser.add_argument("--apply", action="store_true")

    def handle(self, *args, **opts):
        raw = opts["json"]
        if opts["map_file"]:
            with open(opts["map_file"], encoding="utf-8") as fh:
                raw = fh.read()
        try:
            mapping = json.loads(raw or "{}")
        except ValueError as exc:
            raise CommandError(f"Not valid JSON: {exc}")
        if not isinstance(mapping, dict) or not mapping:
            raise CommandError('Pass a JSON object like {"17": "https://brand.com"}.')
        if len(mapping) > 200:
            raise CommandError("At most 200 brands per run.")
        rows = apply_map(mapping, apply=opts["apply"])
        mode = "APPLIED" if opts["apply"] else "DRY RUN"
        self.stdout.write(f"{mode}: {len(rows)} row(s)")
        for r in rows:
            self.stdout.write(json.dumps(r))
