"""Seed the Drekker Brewing product catalog (ProductTypes + Products).

Idempotent. DRY-RUN by default; --apply writes. GraphQL "Products Sampled"
resolves from live Product rows, so this catalog is the single source of
truth — no hardcoded second SKU list on the recap template beyond the
cached options written at seed time.

Usage::

    python manage.py onboard_drekker_products --owner-email kyle@igniteproductions.co
    python manage.py onboard_drekker_products --owner-email kyle@... --apply
"""

from __future__ import annotations

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.db.models import Q

from events.models import Product, ProductType
from tenants.models import Tenant

User = get_user_model()

TENANT_NAME = "Drekker Brewing"
TENANT_SLUG = "drekker-brewing"
PRODUCT_TYPE_NAME = "Beer / Seltzer"

# Braaaaaaaaains = Br + 10 a's + ins (Kyle's spelling).
BRAINS = "Br" + ("a" * 10) + "ins Variety 6-Pack"

DREKKER_PRODUCTS: list[str] = [
    "Saved by the Buoyancy of Citrus",
    "Ectogasm",
    "Brunch Bubbz",
    "SMoL Blue Razz",
    "SMoL POG",
    "SMoL Party Pack",
    "CHONK Mix Pack",
    BRAINS,
]


class Command(BaseCommand):
    help = (
        "Seed Drekker Brewing product type + SKUs (no artwork). "
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
                f"{TENANT_SLUG!r}."
            ),
        )
        parser.add_argument(
            "--apply",
            action="store_true",
            help="Actually write (omit for a dry run that changes nothing).",
        )

    def _resolve_owner(self, email: str):
        owner = User.objects.filter(email__iexact=email.strip()).first()
        if owner is None:
            raise CommandError(f"No user with email {email!r}.")
        return owner

    def _resolve_tenant(self, needle: str) -> Tenant:
        search = (needle or "").strip() or "drekker"
        exact = list(Tenant.objects.filter(slug=TENANT_SLUG).order_by("id"))
        if len(exact) == 1:
            return exact[0]
        matches = list(
            Tenant.objects.filter(
                Q(name__icontains=search) | Q(slug__icontains=search)
            )
            .distinct()
            .order_by("id")
        )
        if len(matches) > 1:
            prefer = [t for t in matches if (t.slug or "").lower() == TENANT_SLUG]
            if len(prefer) == 1:
                return prefer[0]
        if len(matches) == 1:
            return matches[0]
        self.stdout.write(self.style.WARNING("Tenants in this database:"))
        for t in Tenant.objects.order_by("id"):
            self.stdout.write(f"  [{t.id}] name={t.name!r} slug={t.slug!r}")
        if not matches:
            raise CommandError(
                f"No tenant matches {search!r}. Onboard Drekker Brewing first."
            )
        raise CommandError(
            f"{search!r} matched {len(matches)} tenants "
            f"({', '.join(repr(t.slug) for t in matches)}) — narrow --tenant."
        )

    def handle(self, *args, **opts):
        apply = bool(opts["apply"])
        owner = self._resolve_owner(opts["owner_email"])
        tenant = self._resolve_tenant(opts.get("tenant") or "")

        self.stdout.write("=" * 68)
        self.stdout.write(
            f"Tenant     : [{tenant.id}] {tenant.name!r} (slug {tenant.slug!r})"
        )
        self.stdout.write(f"Owner      : {owner.email!r}")
        self.stdout.write(
            f"Mode       : {'APPLY (writing)' if apply else 'DRY-RUN (no writes)'}"
        )
        self.stdout.write("=" * 68)

        ptype = ProductType.objects.filter(
            tenant_id=tenant.id, name__iexact=PRODUCT_TYPE_NAME
        ).first()
        if ptype:
            self.stdout.write(f"ProductType: [{ptype.id}] {ptype.name!r} (exists)")
        elif apply:
            ptype = ProductType.objects.create(
                name=PRODUCT_TYPE_NAME,
                tenant=tenant,
                created_by=owner,
            )
            self.stdout.write(f"ProductType: + [{ptype.id}] {ptype.name!r}")
        else:
            self.stdout.write(f"ProductType: would create {PRODUCT_TYPE_NAME!r}")

        created = 0
        skipped = 0
        for name in DREKKER_PRODUCTS:
            existing = Product.objects.filter(
                tenant_id=tenant.id, name__iexact=name
            ).first()
            if existing:
                skipped += 1
                self.stdout.write(f"  = {name!r}")
                continue
            if not apply:
                created += 1
                self.stdout.write(f"  + would create {name!r}")
                continue
            if ptype is None:
                raise CommandError("Product type missing under --apply.")
            Product.objects.create(
                name=name,
                product_type=ptype,
                tenant=tenant,
                created_by=owner,
            )
            created += 1
            self.stdout.write(f"  + {name!r}")

        self.stdout.write("")
        self.stdout.write(
            self.style.SUCCESS(
                f"{'Applied' if apply else 'Dry-run'}: "
                f"+{created} create, ={skipped} exist "
                f"(catalog size target {len(DREKKER_PRODUCTS)})."
            )
        )
