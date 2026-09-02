"""Seed the Mark Anthony Brands product catalog (ProductTypes + Products).

Simpler Torch-style onboard without artwork downloads. Categories from
``mab_products.PRODUCTS`` become ProductTypes; each SKU flavor is a Product
named with the flavor only (category lives on ProductType).

Idempotent. DRY-RUN by default; --apply writes. Under --apply, creates the
tenant ``Mark Anthony Brands`` / ``mark-anthony-brands`` when missing.

Usage::

    python manage.py onboard_mab_products --owner-email kyle@igniteproductions.co
    python manage.py onboard_mab_products --owner-email kyle@... --apply
    python manage.py onboard_mab_products --owner-email kyle@... --tenant mab --apply
"""

from __future__ import annotations

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.db.models import Q

from events.models import Product, ProductType
from recaps.management.commands.mab_products import (
    flat_product_rows,
    mab_product_options,
    product_options,
)
from tenants.models import Tenant

User = get_user_model()

TENANT_NAME = "Mark Anthony Brands"
TENANT_SLUG = "mark-anthony-brands"

# Re-export for event-confirmation / tests.
__all__ = [
    "Command",
    "TENANT_NAME",
    "TENANT_SLUG",
    "mab_product_options",
    "product_options",
]


class Command(BaseCommand):
    help = (
        "Seed Mark Anthony Brands product types + products (no artwork). "
        "Idempotent; dry-run by default, --apply writes."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--owner-email",
            dest="owner_email",
            required=True,
            help="Spark admin who owns these rows (Product.created_by).",
        )
        parser.add_argument(
            "--tenant",
            default="",
            help=(
                "Optional tenant name/slug substring. Default: resolve "
                f"{TENANT_SLUG!r} / name containing 'mark anthony' / 'mab'."
            ),
        )
        parser.add_argument(
            "--apply",
            action="store_true",
            help="Actually write (omit for a dry run that changes nothing).",
        )
        parser.add_argument(
            "--create-tenant",
            dest="create_tenant",
            action="store_true",
            help=(
                "When the tenant is missing, create Mark Anthony Brands "
                "(slug mark-anthony-brands). Implied by --apply if you pass "
                "this flag; without it a missing tenant is an error even "
                "under --apply."
            ),
        )

    def _needle(self, raw: str) -> str:
        search = (raw or "").strip()
        if search.lower() == "mab":
            return "mark anthony"
        return search

    def _resolve_tenant(
        self, needle: str, owner, apply: bool, create_tenant: bool
    ) -> Tenant:
        search = self._needle(needle)

        # Prefer exact slug first.
        exact = list(
            Tenant.objects.filter(slug=TENANT_SLUG).order_by("id")
        )
        if len(exact) == 1:
            return exact[0]
        if len(exact) > 1:
            ids = ", ".join(f"[{t.id}] {t.name!r}" for t in exact)
            raise CommandError(
                f"{len(exact)} tenants share slug {TENANT_SLUG!r} ({ids}). "
                "Resolve the duplicate before seeding products."
            )

        qs = Tenant.objects.all()
        if search:
            matches = list(
                qs.filter(
                    Q(name__icontains=search) | Q(slug__icontains=search)
                )
                .distinct()
                .order_by("id")
            )
        else:
            matches = list(
                qs.filter(
                    Q(name__icontains="mark anthony")
                    | Q(slug__icontains="mark-anthony")
                    | Q(slug__icontains="mab")
                )
                .distinct()
                .order_by("id")
            )

        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            raise CommandError(
                f"Matched {len(matches)} tenants "
                f"({', '.join(repr(t.slug) for t in matches)}) — narrow --tenant."
            )

        if not (apply and create_tenant):
            raise CommandError(
                f"No Mark Anthony Brands tenant found. Re-run with "
                f"--apply --create-tenant to create {TENANT_NAME!r} "
                f"(slug {TENANT_SLUG!r}), or ensure_mab_tenant first."
            )

        tenant = Tenant.objects.create(
            name=TENANT_NAME,
            slug=TENANT_SLUG,
            created_by=owner,
        )
        self.stdout.write(
            f"  + created tenant [{tenant.id}] {tenant.name!r} "
            f"slug={tenant.slug!r}"
        )
        return tenant

    def handle(self, *args, **opts):
        apply = bool(opts["apply"])
        create_tenant = bool(opts.get("create_tenant"))

        try:
            owner = User.objects.get(email__iexact=opts["owner_email"])
        except User.DoesNotExist:
            raise CommandError(f"No user with email {opts['owner_email']!r}.")

        tenant = self._resolve_tenant(
            opts.get("tenant") or "", owner, apply, create_tenant
        )
        rows = flat_product_rows()
        type_names = sorted({cat for cat, _ in rows})

        self.stdout.write("=" * 72)
        self.stdout.write(
            f"TARGET: [{tenant.id}] {tenant.name!r} slug={tenant.slug!r}"
        )
        self.stdout.write(f"OWNER : {owner.email} (id={owner.id})")
        self.stdout.write(
            f"MODE  : {'APPLY (writing)' if apply else 'DRY-RUN (no writes)'}"
        )
        self.stdout.write(
            f"EXISTING: {ProductType.objects.filter(tenant=tenant).count()} "
            f"product type(s), {Product.objects.filter(tenant=tenant).count()} "
            "product(s)"
        )
        self.stdout.write(
            f"CATALOG: {len(type_names)} type(s), {len(rows)} SKU(s); "
            f"picker has {len(product_options())} Category — Name options"
        )
        self.stdout.write("=" * 72)

        stats = {
            "types_created": 0,
            "types_found": 0,
            "products_created": 0,
            "products_found": 0,
        }

        if not apply:
            self.stdout.write("\nWould create/confirm product types:")
            for name in type_names:
                n = sum(1 for cat, _ in rows if cat == name)
                self.stdout.write(f"  {name:<32} {n:>3} product(s)")
            self.stdout.write(
                f"\nDRY-RUN — would upsert {len(type_names)} product type(s) and "
                f"{len(rows)} product(s). Re-run with --apply to write."
            )
            return

        types: dict[str, ProductType] = {}
        for name in type_names:
            pt, created = ProductType.objects.get_or_create(
                tenant=tenant,
                name=name,
                defaults={"created_by": owner},
            )
            types[name] = pt
            stats["types_created" if created else "types_found"] += 1
            self.stdout.write(
                f"  {'+' if created else '='} ProductType id={pt.id} {name!r}"
            )

        self.stdout.write("")
        for type_name, product_name in rows:
            # Lookup includes product_type so shared flavor names across brand
            # lines (e.g. Black Cherry on White Claw + Mike's) stay distinct.
            product, created = Product.objects.get_or_create(
                tenant=tenant,
                product_type=types[type_name],
                name=product_name,
                defaults={"created_by": owner},
            )
            stats["products_created" if created else "products_found"] += 1
            mark = "+" if created else "="
            self.stdout.write(
                f"  {mark} id={product.id:<6} [{type_name}] {product_name}"
            )

        self._sync_products_sampled_options(tenant)

        self.stdout.write("")
        self.stdout.write("=" * 72)
        self.stdout.write(
            self.style.SUCCESS(
                f"Types: {stats['types_created']} created, "
                f"{stats['types_found']} already present.  "
                f"Products: {stats['products_created']} created, "
                f"{stats['products_found']} already present."
            )
        )
        self.stdout.write(
            f"Tenant [{tenant.id}] now has "
            f"{ProductType.objects.filter(tenant=tenant).count()} product type(s) "
            f"and {Product.objects.filter(tenant=tenant).count()} product(s)."
        )
        self.stdout.write("=" * 72)

    def _sync_products_sampled_options(self, tenant: Tenant) -> None:
        """Refresh Products Sampled multiselects from the live catalog."""
        from events.event_confirmations import catalog_product_options
        from recaps.models import CustomField
        from recaps.products_sampled import PRODUCTS_SAMPLED_FIELD

        options = catalog_product_options(tenant) or list(mab_product_options())
        if not options:
            self.stdout.write(
                self.style.WARNING(
                    f"  no options to sync onto '{PRODUCTS_SAMPLED_FIELD}'"
                )
            )
            return

        fields = list(
            CustomField.objects.filter(
                custom_recap_template__tenant_id=tenant.id,
                name__iexact=PRODUCTS_SAMPLED_FIELD,
            ).select_related("custom_recap_template")
        )
        if not fields:
            self.stdout.write(
                f"  No '{PRODUCTS_SAMPLED_FIELD}' choice field on MAB "
                "templates — nothing to refresh (pills still resolve from "
                "catalog at GraphQL read time once a field exists)."
            )
            return

        for field in fields:
            field.options = list(options)
            field.save(update_fields=["options"])
            tpl_name = getattr(field.custom_recap_template, "name", "?")
            self.stdout.write(
                f"  refreshed Products Sampled on {tpl_name!r} "
                f"→ {len(options)} catalog options"
            )
