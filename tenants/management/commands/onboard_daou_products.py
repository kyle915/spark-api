"""Seed Treasury Wine Estates / DAOU Discovery product catalog.

Creates the ``Treasury Wine Estates`` tenant when missing (``--create-tenant``),
then seeds ProductType ``Discovery Collection`` + the four Discoveries SKUs
from the DAOU tasting notes. Catalog is the single source of truth for
Products Sampled on the Event Activation recap template.

Idempotent. DRY-RUN by default; ``--apply`` writes.

Usage::

    python manage.py onboard_daou_products --owner-email kyle@igniteproductions.co
    python manage.py onboard_daou_products --owner-email kyle@... --apply --create-tenant
"""

from __future__ import annotations

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.db.models import Q

from events.models import Product, ProductType
from tenants.models import Tenant

User = get_user_model()

TENANT_NAME = "Treasury Wine Estates"
TENANT_SLUG = "treasury-wine-estates"
TENANT_REQUEST_URL_NAME = "treasury-wine-estates"
PRODUCT_TYPE_NAME = "Discovery Collection"

# Discoveries line wines from the attached DAOU tasting notes (vintages as filed).
DAOU_PRODUCTS: list[str] = [
    "DAOU Discovery Sauvignon Blanc 2025",
    "DAOU Discovery Rosé 2025",
    "DAOU Discovery Cabernet Sauvignon 2024",
    "DAOU Discovery Chardonnay 2025",
]


class Command(BaseCommand):
    help = (
        "Seed Treasury Wine Estates / DAOU Discovery product catalog. "
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
                f"{TENANT_SLUG!r} / treasury / daou / twe."
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
                "When the tenant is missing, create Treasury Wine Estates "
                f"(slug {TENANT_SLUG!r})."
            ),
        )

    def _resolve_owner(self, email: str):
        owner = User.objects.filter(email__iexact=email.strip()).first()
        if owner is None:
            raise CommandError(f"No user with email {email!r}.")
        return owner

    def _resolve_tenant(
        self, needle: str, owner, apply: bool, create_tenant: bool
    ) -> Tenant:
        search = (needle or "").strip()

        exact = list(Tenant.objects.filter(slug=TENANT_SLUG).order_by("id"))
        if len(exact) == 1:
            return exact[0]
        if len(exact) > 1:
            ids = ", ".join(f"[{t.id}] {t.name!r}" for t in exact)
            raise CommandError(
                f"{len(exact)} tenants share slug {TENANT_SLUG!r} ({ids}). "
                "Resolve the duplicate before seeding products."
            )

        if search:
            matches = list(
                Tenant.objects.filter(
                    Q(name__icontains=search) | Q(slug__icontains=search)
                )
                .distinct()
                .order_by("id")
            )
        else:
            matches = list(
                Tenant.objects.filter(
                    Q(name__icontains="treasury")
                    | Q(name__icontains="daou")
                    | Q(slug__icontains="treasury")
                    | Q(slug__icontains="daou")
                    | Q(slug__icontains="twe")
                )
                .distinct()
                .order_by("id")
            )

        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            prefer = [
                t
                for t in matches
                if (t.slug or "").lower() == TENANT_SLUG
                or (t.name or "").lower() == TENANT_NAME.lower()
            ]
            if len(prefer) == 1:
                return prefer[0]
            self.stdout.write(self.style.WARNING("Matched tenants:"))
            for t in matches:
                self.stdout.write(f"  [{t.id}] name={t.name!r} slug={t.slug!r}")
            raise CommandError(
                f"Matched {len(matches)} tenants — narrow --tenant."
            )

        if not (apply and create_tenant):
            raise CommandError(
                f"No Treasury Wine Estates / DAOU tenant found. Re-run with "
                f"--apply --create-tenant to create {TENANT_NAME!r} "
                f"(slug {TENANT_SLUG!r})."
            )

        tenant = Tenant.objects.create(
            name=TENANT_NAME,
            slug=TENANT_SLUG,
            request_url_name=TENANT_REQUEST_URL_NAME,
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
        owner = self._resolve_owner(opts["owner_email"])
        tenant = self._resolve_tenant(
            opts.get("tenant") or "", owner, apply, create_tenant
        )

        # Backfill public form slug if an older row is missing it.
        if apply and not (tenant.request_url_name or "").strip():
            tenant.request_url_name = TENANT_REQUEST_URL_NAME
            tenant.updated_by = owner
            tenant.save(update_fields=["request_url_name", "updated_by"])
            self.stdout.write(
                f"  ~ set request_url_name={TENANT_REQUEST_URL_NAME!r}"
            )

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
        for name in DAOU_PRODUCTS:
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
                raise CommandError("ProductType missing under --apply.")
            Product.objects.create(
                name=name,
                product_type=ptype,
                tenant=tenant,
                created_by=owner,
            )
            created += 1
            self.stdout.write(f"  + {name!r}")

        self.stdout.write("")
        if apply:
            self.stdout.write(
                self.style.SUCCESS(
                    f"APPLIED — created {created}, already present {skipped}."
                )
            )
        else:
            self.stdout.write(
                self.style.WARNING(
                    f"DRY-RUN — would create {created}, already present "
                    f"{skipped}. Re-run with --apply."
                )
            )
