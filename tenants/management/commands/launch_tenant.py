"""Launch a tenant without engineering: the wizard, as a command.

Same shared service (tenants.launch) the admin launch wizard calls.
Dry-run by default; --apply writes. An existing exact slug resumes that
tenant and never remints its standing code.

Usage:
    python manage.py launch_tenant --slug acme-drinks --name "Acme Drinks"
    python manage.py launch_tenant --slug acme-drinks --name "Acme Drinks" \
        --donor-slug liquid-death --donor-template-id 9 \
        --catalog-csv catalog.csv --code-prefix ACME --apply
"""

from __future__ import annotations

from pathlib import Path

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError

from recaps.models import CustomRecapTemplate
from tenants.launch import (
    LaunchError,
    build_preview,
    launch_tenant,
    parse_catalog_input,
    parse_label_swaps,
    resolve_tenant_by_exact_slug,
)

User = get_user_model()


class Command(BaseCommand):
    help = "Launch/resume a tenant (dry-run default; --apply writes)."

    def add_arguments(self, parser):
        parser.add_argument("--name", required=True)
        parser.add_argument("--slug", required=True, help="Exact slug, e.g. drink-lyft.")
        parser.add_argument("--brand-color", default=None)
        parser.add_argument("--logo-url", default=None)
        parser.add_argument("--donor-slug", default=None, help="Exact donor tenant slug.")
        parser.add_argument("--donor-template-id", type=int, default=None)
        parser.add_argument("--donor-template-name", default=None)
        parser.add_argument("--new-template-name", default=None)
        parser.add_argument("--replace-text", action="append", default=[])
        parser.add_argument("--catalog-csv", default=None, help="Path to CSV (name,sku,image_url).")
        parser.add_argument("--shopify-json", default=None, help="Path to Shopify products.json.")
        parser.add_argument("--code-prefix", default=None)
        parser.add_argument("--download-images", action="store_true")
        parser.add_argument("--apply", action="store_true")

    def handle(self, *args, **opts):
        apply = bool(opts["apply"])
        creator = (
            User.objects.filter(is_superuser=True).order_by("id").first()
            or User.objects.order_by("id").first()
        )
        if creator is None:
            raise CommandError("No user available to own the created rows.")

        csv_text = Path(opts["catalog_csv"]).read_text() if opts["catalog_csv"] else ""
        shopify_text = (
            Path(opts["shopify_json"]).read_text() if opts["shopify_json"] else ""
        )
        rows, errors = parse_catalog_input(csv_text=csv_text, shopify_json=shopify_text)
        if errors:
            raise CommandError("; ".join(errors))
        try:
            swaps = parse_label_swaps(opts["replace_text"])
        except LaunchError as exc:
            raise CommandError(str(exc)) from exc

        donor = self._resolve_donor(opts)
        preview = build_preview(
            name=opts["name"],
            slug=opts["slug"],
            donor_template=donor,
            catalog_rows=rows,
            code_prefix=opts["code_prefix"],
            swaps=swaps,
            new_template_name=opts["new_template_name"],
        )
        self.stdout.write(f"Tenant  : {preview['name']!r} slug={preview['slug']!r}")
        self.stdout.write(f"Existing: {preview['existing_tenant']}")
        self.stdout.write(f"Check-in: {preview['checkin_url']}")
        self.stdout.write(f"Requests: {preview['spark_form_url']}")
        self.stdout.write(f"Catalog : {preview['catalog_count']} row(s)")
        if preview["donor"]:
            self.stdout.write(
                f"Donor   : {preview['donor']['template_name']!r} -> "
                f"{preview['donor']['new_template_name']!r}"
            )
        if not apply:
            self.stdout.write(self.style.WARNING("DRY-RUN — no writes. Re-run with --apply."))
            return
        try:
            result = launch_tenant(
                creator=creator,
                name=opts["name"],
                slug=opts["slug"],
                brand_color=opts["brand_color"],
                logo_url=opts["logo_url"],
                donor_template_id=donor.id if donor else None,
                new_template_name=opts["new_template_name"],
                label_swaps=swaps,
                catalog_rows=rows,
                code_prefix=opts["code_prefix"],
                download_images=bool(opts["download_images"]),
            )
        except LaunchError as exc:
            raise CommandError(str(exc)) from exc
        self.stdout.write(self.style.SUCCESS(result["checkin_url"]))
        self.stdout.write(self.style.SUCCESS(result["spark_form_url"]))

    def _resolve_donor(self, opts):
        if opts["donor_template_id"]:
            donor = CustomRecapTemplate.objects.filter(
                id=opts["donor_template_id"]
            ).first()
            if donor is None:
                raise CommandError(f"No template with id {opts['donor_template_id']}.")
            return donor
        if not opts["donor_slug"]:
            return None
        tenant = resolve_tenant_by_exact_slug(opts["donor_slug"])
        if tenant is None:
            raise CommandError(f"No tenant with exact slug {opts['donor_slug']!r}.")
        qs = CustomRecapTemplate.objects.filter(tenant_id=tenant.id).order_by("id")
        if opts["donor_template_name"]:
            donor = qs.filter(name__iexact=opts["donor_template_name"]).first()
            if donor is None:
                raise CommandError(
                    f"Tenant {tenant.slug!r} has no template "
                    f"{opts['donor_template_name']!r}."
                )
            return donor
        rows = list(qs)
        if len(rows) != 1:
            raise CommandError(
                f"Tenant {tenant.slug!r} has {len(rows)} templates — "
                "pass --donor-template-id or --donor-template-name."
            )
        return rows[0]
