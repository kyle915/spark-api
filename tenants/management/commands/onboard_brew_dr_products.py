"""Seed Brew Dr. Kombucha Product catalog + rewrite Products Sampled.

Catalog is the single source of truth for walk-up / ops Products Sampled
selectors (GraphQL resolves live Product rows). This command:

1. Ensures ProductType ``Kombucha`` + exactly these six SKUs:
   Unsweetened, Classic, Tea & Lemonade, Peach, Raspberry,
   Mango Passionfruit.
2. Removes other Brew Dr Product rows once FK sample/sales refs are cleared
   (no soft-retire column on Product — delete after clearing so selectors
   only show the six).
3. Refreshes stored ``Products Sampled`` CustomField.options from the catalog.
4. For custom recaps whose sampled selection is empty OR does not already
   list all six new SKUs: clear old multiselect / structured sample rows,
   then set the multiselect to the six catalog labels. Does **not** invent
   per-SKU can quantities (CustomRecapProductSample qty stays empty unless
   the recap already correctly had the new set).

Idempotent. DRY-RUN by default; ``--apply`` writes.

Usage::

    python manage.py onboard_brew_dr_products --owner-email kyle@igniteproductions.co
    python manage.py onboard_brew_dr_products --owner-email kyle@... --apply
"""

from __future__ import annotations

import json
import re

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.db.models import Q

from events.models import Product, ProductType
from tenants.models import Tenant

User = get_user_model()

TENANT_NAME = "Brew Dr. Kombucha"
TENANT_SLUGS = ("brew-dr-kombucha", "brew-dr")
PRODUCT_TYPE_NAME = "Kombucha"

# Prior hardcoded Products Sampled options (seed_brew_dr_recap_template).
LEGACY_CANS: tuple[str, ...] = (
    "Clear Mind",
    "Island Mango",
    "Superberry",
    "Love",
    "Pineapple Paradise",
)

BREW_DR_PRODUCTS: list[str] = [
    "Unsweetened",
    "Classic",
    "Tea & Lemonade",
    "Peach",
    "Raspberry",
    "Mango Passionfruit",
]


def _sku_key(label: str | None) -> str:
    """Normalize a pill / option to a bare SKU name for set comparison."""
    raw = (label or "").strip()
    if not raw:
        return ""
    parts = re.split(r"\s+[—–-]\s+", raw, maxsplit=1)
    if len(parts) == 2:
        raw = parts[1].strip()
    return re.sub(r"\s+", " ", raw.lower())


def _parse_sampled_list(raw: str | None) -> list[str]:
    if raw is None:
        return []
    text = str(raw).strip()
    if not text:
        return []
    try:
        parsed = json.loads(text)
    except Exception:
        return [text] if text else []
    if isinstance(parsed, list):
        return [str(x).strip() for x in parsed if str(x).strip()]
    if isinstance(parsed, str) and parsed.strip():
        return [parsed.strip()]
    return []


def _has_full_new_set(selected: list[str], wanted: list[str]) -> bool:
    """True when every wanted SKU appears in selected (bare or Type — Name)."""
    have = {_sku_key(s) for s in selected if _sku_key(s)}
    need = {_sku_key(w) for w in wanted if _sku_key(w)}
    return bool(need) and need.issubset(have)


class Command(BaseCommand):
    help = (
        "Seed Brew Dr. Kombucha catalog (6 SKUs) and rewrite Products Sampled "
        "on recaps that lack the new set. Idempotent; dry-run by default."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--owner-email",
            dest="owner_email",
            required=True,
            help="Spark admin who owns created Product rows (created_by).",
        )
        parser.add_argument(
            "--tenant",
            default="",
            help=(
                "Optional tenant name/slug substring. Default: resolve "
                f"{TENANT_SLUGS[0]!r} / brew."
            ),
        )
        parser.add_argument(
            "--apply",
            action="store_true",
            help="Actually write (omit for a dry run that changes nothing).",
        )
        parser.add_argument(
            "--skip-migrate",
            dest="skip_migrate",
            action="store_true",
            help="Only sync the Product catalog + field options (no recap rewrite).",
        )

    def _resolve_owner(self, email: str):
        owner = User.objects.filter(email__iexact=email.strip()).first()
        if owner is None:
            raise CommandError(f"No user with email {email!r}.")
        return owner

    def _resolve_tenant(self, needle: str) -> Tenant:
        search = (needle or "").strip()
        for slug in TENANT_SLUGS:
            exact = list(Tenant.objects.filter(slug=slug).order_by("id"))
            if len(exact) == 1:
                return exact[0]
            if len(exact) > 1:
                ids = ", ".join(f"[{t.id}] {t.name!r}" for t in exact)
                raise CommandError(
                    f"{len(exact)} tenants share slug {slug!r} ({ids})."
                )

        q = Q(name__icontains="brew dr") | Q(slug__icontains="brew-dr") | Q(
            slug__icontains="brew"
        )
        if search:
            q = Q(name__icontains=search) | Q(slug__icontains=search)
        matches = list(Tenant.objects.filter(q).distinct().order_by("id"))
        if len(matches) > 1:
            prefer = [
                t
                for t in matches
                if (t.slug or "").lower() in TENANT_SLUGS
                or "brew dr" in (t.name or "").lower()
            ]
            if len(prefer) == 1:
                return prefer[0]
        if len(matches) == 1:
            return matches[0]
        self.stdout.write(self.style.WARNING("Tenants in this database:"))
        for t in Tenant.objects.order_by("id"):
            self.stdout.write(f"  [{t.id}] name={t.name!r} slug={t.slug!r}")
        if not matches:
            raise CommandError(
                f"No tenant matches {search or 'brew'!r}. Onboard Brew Dr first."
            )
        raise CommandError(
            f"{search or 'brew'!r} matched {len(matches)} tenants "
            f"({', '.join(repr(t.slug) for t in matches)}) — narrow --tenant."
        )

    def handle(self, *args, **opts):
        apply = bool(opts["apply"])
        skip_migrate = bool(opts["skip_migrate"])
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
        self.stdout.write(
            f"Migrate    : {'skip' if skip_migrate else 'rewrite Products Sampled'}"
        )
        self.stdout.write("=" * 68)
        self.stdout.write(
            "Old cans → new SKUs:\n"
            f"  {', '.join(LEGACY_CANS)}\n"
            f"  → {', '.join(BREW_DR_PRODUCTS)}"
        )

        def _run():
            ptype = self._ensure_product_type(tenant, owner, apply)
            keep_ids = self._ensure_products(tenant, ptype, owner, apply)
            self._retire_extra_products(tenant, keep_ids, apply)
            self._sync_products_sampled_options(tenant, apply)
            if not skip_migrate:
                self._migrate_sampled_on_recaps(tenant, owner, apply)

        if apply:
            with transaction.atomic():
                _run()
        else:
            _run()

        live = Product.objects.filter(tenant_id=tenant.id).order_by("name")
        self.stdout.write("")
        self.stdout.write(
            self.style.SUCCESS(
                f"{'Applied' if apply else 'Dry-run'}: catalog now "
                f"{live.count()} product(s) — "
                + ", ".join(p.name for p in live)
            )
        )

    def _ensure_product_type(self, tenant, owner, apply: bool):
        ptype = ProductType.objects.filter(
            tenant_id=tenant.id, name__iexact=PRODUCT_TYPE_NAME
        ).first()
        if ptype:
            self.stdout.write(f"ProductType: [{ptype.id}] {ptype.name!r} (exists)")
            return ptype
        if not apply:
            self.stdout.write(f"ProductType: would create {PRODUCT_TYPE_NAME!r}")
            return None
        ptype = ProductType.objects.create(
            name=PRODUCT_TYPE_NAME,
            tenant=tenant,
            created_by=owner,
        )
        self.stdout.write(f"ProductType: + [{ptype.id}] {ptype.name!r}")
        return ptype

    def _ensure_products(self, tenant, ptype, owner, apply: bool) -> set[int]:
        keep: set[int] = set()
        for name in BREW_DR_PRODUCTS:
            existing = Product.objects.filter(
                tenant_id=tenant.id, name__iexact=name
            ).first()
            if existing:
                keep.add(existing.id)
                if (
                    ptype is not None
                    and existing.product_type_id != ptype.id
                    and apply
                ):
                    existing.product_type = ptype
                    existing.updated_by = owner
                    existing.save(
                        update_fields=["product_type", "updated_by", "updated_at"]
                    )
                    self.stdout.write(
                        f"  ~ retyped {existing.name!r} → {PRODUCT_TYPE_NAME}"
                    )
                else:
                    self.stdout.write(f"  = {existing.name!r}")
                continue
            if not apply:
                self.stdout.write(f"  + would create {name!r}")
                continue
            if ptype is None:
                raise CommandError("Product type missing under --apply.")
            row = Product.objects.create(
                name=name,
                product_type=ptype,
                tenant=tenant,
                created_by=owner,
            )
            keep.add(row.id)
            self.stdout.write(f"  + {name!r}")
        return keep

    def _clear_product_fks(self, product: Product, apply: bool) -> int:
        """Drop sample/sales rows that RESTRICT-block Product delete."""
        from recaps.models import (
            CustomRecapProductSample,
            CustomRecapSalePerformance,
            ProductSamples,
            SalesPerformance,
        )

        cleared = 0
        for model in (
            CustomRecapProductSample,
            ProductSamples,
            CustomRecapSalePerformance,
            SalesPerformance,
        ):
            qs = model.objects.filter(product_id=product.id)
            n = qs.count()
            if not n:
                continue
            cleared += n
            label = model.__name__
            if apply:
                qs.delete()
                self.stdout.write(
                    f"    - cleared {n} {label} row(s) on {product.name!r}"
                )
            else:
                self.stdout.write(
                    f"    - would clear {n} {label} row(s) on {product.name!r}"
                )
        return cleared

    def _retire_extra_products(self, tenant, keep_ids: set[int], apply: bool) -> None:
        extras = list(
            Product.objects.filter(tenant_id=tenant.id)
            .exclude(id__in=keep_ids)
            .order_by("id")
        )
        if not extras:
            self.stdout.write("Extras    : none (catalog already exact)")
            return
        self.stdout.write(f"Extras    : {len(extras)} to remove from selectors")
        for product in extras:
            self._clear_product_fks(product, apply)
            if apply:
                name = product.name
                product.delete()
                self.stdout.write(f"  - deleted Product {name!r}")
            else:
                self.stdout.write(f"  - would delete Product {product.name!r}")

    def _sync_products_sampled_options(self, tenant: Tenant, apply: bool) -> None:
        from events.event_confirmations import catalog_product_options
        from recaps.models import CustomField
        from recaps.products_sampled import PRODUCTS_SAMPLED_FIELD

        options = catalog_product_options(tenant)
        if not options and not apply:
            options = [f"{PRODUCT_TYPE_NAME} — {n}" for n in BREW_DR_PRODUCTS]
            self.stdout.write(
                f"Options   : would sync {len(options)} preview labels onto "
                f"{PRODUCTS_SAMPLED_FIELD!r}"
            )
        elif not options:
            self.stdout.write(
                self.style.WARNING(
                    f"  no Product rows to sync onto '{PRODUCTS_SAMPLED_FIELD}'"
                )
            )
            return
        else:
            self.stdout.write(
                f"Options   : {len(options)} catalog label(s) for "
                f"{PRODUCTS_SAMPLED_FIELD!r}"
            )

        fields = list(
            CustomField.objects.filter(
                custom_recap_template__tenant_id=tenant.id,
                name__iexact=PRODUCTS_SAMPLED_FIELD,
            ).select_related("custom_recap_template")
        )
        if not fields:
            self.stdout.write(
                f"  No '{PRODUCTS_SAMPLED_FIELD}' field on Brew Dr templates yet."
            )
            return

        for field in fields:
            tpl_name = getattr(field.custom_recap_template, "name", "?")
            if list(field.options or []) == list(options):
                self.stdout.write(f"  = options on {tpl_name!r}")
                continue
            if apply:
                field.options = list(options)
                field.save(update_fields=["options"])
                self.stdout.write(
                    f"  ~ refreshed options on {tpl_name!r} → {len(options)}"
                )
            else:
                self.stdout.write(
                    f"  ~ would refresh options on {tpl_name!r} → {len(options)}"
                )

    def _migrate_sampled_on_recaps(self, tenant, owner, apply: bool) -> None:
        from events.event_confirmations import catalog_product_options
        from recaps.models import (
            CustomField,
            CustomFieldValue,
            CustomRecap,
            CustomRecapProductSample,
            ProductSamples,
            Recap,
        )
        from recaps.products_sampled import PRODUCTS_SAMPLED_FIELD

        wanted_labels = catalog_product_options(tenant)
        if not wanted_labels:
            wanted_labels = [f"{PRODUCT_TYPE_NAME} — {n}" for n in BREW_DR_PRODUCTS]

        fields = list(
            CustomField.objects.filter(
                custom_recap_template__tenant_id=tenant.id,
                name__iexact=PRODUCTS_SAMPLED_FIELD,
            )
        )
        field_ids = [f.id for f in fields]

        rewritten = 0
        skipped = 0
        created_cfv = 0

        if field_ids:
            values = list(
                CustomFieldValue.objects.filter(custom_field_id__in=field_ids)
                .select_related("custom_recap", "custom_field")
                .order_by("id")
            )
        else:
            values = []

        seen_recap_ids: set[int] = set()
        for cfv in values:
            recap = cfv.custom_recap
            seen_recap_ids.add(recap.id)
            selected = _parse_sampled_list(cfv.value)
            if _has_full_new_set(selected, BREW_DR_PRODUCTS):
                skipped += 1
                continue
            rewritten += 1
            before = selected or ["(empty)"]
            self.stdout.write(
                f"  rewrite custom_recap id={recap.id}: "
                f"{before} → {wanted_labels}"
            )
            if not apply:
                continue
            cfv.value = json.dumps(wanted_labels)
            cfv.updated_by = owner
            cfv.save(update_fields=["value", "updated_by", "updated_at"])
            n_samples = CustomRecapProductSample.objects.filter(
                custom_recap_id=recap.id
            ).count()
            if n_samples:
                CustomRecapProductSample.objects.filter(
                    custom_recap_id=recap.id
                ).delete()
                self.stdout.write(
                    f"    cleared {n_samples} CustomRecapProductSample "
                    "(no invented qty)"
                )

        if field_ids:
            missing = (
                CustomRecap.objects.filter(tenant_id=tenant.id)
                .exclude(id__in=seen_recap_ids)
                .order_by("id")
            )
            primary_field = fields[0]
            for recap in missing.iterator():
                other = CustomFieldValue.objects.filter(
                    custom_recap_id=recap.id
                ).count()
                samples = CustomRecapProductSample.objects.filter(
                    custom_recap_id=recap.id
                ).count()
                if other == 0 and samples == 0:
                    continue
                rewritten += 1
                created_cfv += 1
                self.stdout.write(
                    f"  + Products Sampled on custom_recap id={recap.id} → "
                    f"{wanted_labels}"
                )
                if not apply:
                    continue
                CustomFieldValue.objects.create(
                    custom_recap=recap,
                    custom_field=primary_field,
                    value=json.dumps(wanted_labels),
                    created_by=owner,
                )
                if samples:
                    CustomRecapProductSample.objects.filter(
                        custom_recap_id=recap.id
                    ).delete()

        std = list(
            Recap.objects.filter(event__tenant_id=tenant.id)
            .prefetch_related("product_samples__product")
            .order_by("id")
        )
        for recap in std:
            rows = list(recap.product_samples.all())
            if not rows:
                continue
            names = [r.product.name for r in rows if r.product_id]
            if _has_full_new_set(names, BREW_DR_PRODUCTS):
                skipped += 1
                continue
            rewritten += 1
            self.stdout.write(
                f"  rewrite standard recap id={recap.id} ProductSamples: "
                f"{names} → clear (no invented qty; custom path owns pills)"
            )
            if apply:
                ProductSamples.objects.filter(recap_id=recap.id).delete()

        self.stdout.write("")
        self.stdout.write(
            self.style.SUCCESS(
                f"Sampled migrate: rewrite={rewritten} "
                f"(new CFV={created_cfv}) leave-alone={skipped}"
            )
        )
