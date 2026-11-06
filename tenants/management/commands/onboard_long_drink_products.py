"""Seed The Finnish Long Drink SKUs + can artwork on Mark Anthony Brands.

Torch-style onboard: ProductType + Products, download artwork into
``Product.image`` (django-storages → GCS in prod). Idempotent. DRY-RUN
default. Existing MAB catalog rows for other brands are left alone.

Usage::

    python manage.py onboard_long_drink_products --owner-email kyle@…
    python manage.py onboard_long_drink_products --owner-email kyle@… --apply
"""

from __future__ import annotations

import logging
import time
from urllib.parse import urlparse

import httpx
from django.contrib.auth import get_user_model
from django.core.files.base import ContentFile
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.db.models import Q

from events.models import Product, ProductType
from recaps.management.commands.long_drink_products import (
    LONG_DRINK_PRODUCTS,
    PRODUCT_TYPE,
    long_drink_product_options,
)
from tenants.models import Tenant

logger = logging.getLogger(__name__)
User = get_user_model()

TENANT_SLUG = "mark-anthony-brands"
REQUEST_TIMEOUT = 30.0
USER_AGENT = "Mozilla/5.0 (compatible; SparkBA-Onboarder/1.0)"
DOWNLOAD_SPACING_SECONDS = 0.75
RATE_LIMIT_BACKOFF_SECONDS = 8.0
RATE_LIMIT_RETRIES = 2


class Command(BaseCommand):
    help = (
        "Seed Long Drink product type + SKUs (+ artwork) on Mark Anthony Brands. "
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
            default=TENANT_SLUG,
            help=f"Tenant slug/name substring. Default: {TENANT_SLUG!r}.",
        )
        parser.add_argument(
            "--apply",
            action="store_true",
            help="Actually write (omit for a dry run that changes nothing).",
        )
        parser.add_argument(
            "--skip-images",
            dest="skip_images",
            action="store_true",
            help="Create product rows but download no artwork.",
        )
        parser.add_argument(
            "--force-images",
            dest="force_images",
            action="store_true",
            help="Re-download artwork for products that already have an image.",
        )

    def handle(self, *args, **opts):
        apply = bool(opts["apply"])
        skip_images = bool(opts["skip_images"])
        force_images = bool(opts["force_images"])

        try:
            owner = User.objects.get(email__iexact=opts["owner_email"])
        except User.DoesNotExist as exc:
            raise CommandError(
                f"No user with email {opts['owner_email']!r}."
            ) from exc

        tenant = self._resolve_tenant(opts["tenant"])
        self.stdout.write("=" * 72)
        self.stdout.write(
            f"TARGET: [{tenant.id}] {tenant.name!r} slug={tenant.slug!r}"
        )
        self.stdout.write(f"OWNER : {owner.email} (id={owner.id})")
        self.stdout.write(
            f"MODE  : {'APPLY (writing)' if apply else 'DRY-RUN (no writes)'}"
            f"{'  [skip-images]' if skip_images else ''}"
            f"{'  [force-images]' if force_images else ''}"
        )
        self.stdout.write(
            f"SKUs  : {len(LONG_DRINK_PRODUCTS)} Long Drink flavor(s)"
        )
        self.stdout.write("=" * 72)

        if not apply:
            for name, url in LONG_DRINK_PRODUCTS:
                art = "artwork" if url else "no artwork URL"
                self.stdout.write(f"  {PRODUCT_TYPE} — {name}  ({art})")
            self.stdout.write(
                self.style.WARNING(
                    f"\nDRY-RUN — would upsert ProductType {PRODUCT_TYPE!r} "
                    f"+ {len(LONG_DRINK_PRODUCTS)} product(s). "
                    "Re-run with --apply to write."
                )
            )
            return

        stats = {
            "types_created": 0,
            "types_found": 0,
            "products_created": 0,
            "products_found": 0,
            "images_saved": 0,
            "images_skipped": 0,
            "images_failed": 0,
        }

        pt, created = ProductType.objects.get_or_create(
            tenant=tenant,
            name=PRODUCT_TYPE,
            defaults={"created_by": owner},
        )
        stats["types_created" if created else "types_found"] += 1
        self.stdout.write(
            f"  {'+' if created else '='} ProductType id={pt.id} {PRODUCT_TYPE!r}"
        )

        for product_name, image_url in LONG_DRINK_PRODUCTS:
            # Include product_type so shared flavor names across MAB lines
            # (e.g. Peach on White Claw + Long Drink) stay distinct.
            product, created = Product.objects.get_or_create(
                tenant=tenant,
                product_type=pt,
                name=product_name,
                defaults={"created_by": owner},
            )
            stats["products_created" if created else "products_found"] += 1
            mark = "+" if created else "="
            note = ""

            if skip_images or not image_url:
                stats["images_skipped"] += 1
                if not image_url and not skip_images:
                    note = "  (no artwork URL)"
            elif product.image and not force_images:
                stats["images_skipped"] += 1
                note = "  (image already set)"
            else:
                ok, detail = self._attach_image(product, image_url)
                stats["images_saved" if ok else "images_failed"] += 1
                note = f"  {detail}"

            self.stdout.write(
                f"  {mark} id={product.id:<6} {product_name:<28}{note}"
            )

        self._sync_products_sampled_options(tenant)

        self.stdout.write("")
        self.stdout.write(
            self.style.SUCCESS(
                f"Types: {stats['types_created']} created, "
                f"{stats['types_found']} already present.  "
                f"Products: {stats['products_created']} created, "
                f"{stats['products_found']} already present."
            )
        )
        msg = (
            f"Artwork: {stats['images_saved']} saved, "
            f"{stats['images_skipped']} skipped, "
            f"{stats['images_failed']} failed."
        )
        self.stdout.write(
            self.style.WARNING(msg)
            if stats["images_failed"]
            else self.style.SUCCESS(msg)
        )

    def _resolve_tenant(self, needle: str) -> Tenant:
        search = (needle or "").strip()
        if search.lower() in {"mab", "mark anthony"}:
            search = "mark anthony"
        exact = list(Tenant.objects.filter(slug=TENANT_SLUG).order_by("id"))
        if len(exact) == 1:
            return exact[0]
        if len(exact) > 1:
            raise CommandError(
                f"{len(exact)} tenants share slug {TENANT_SLUG!r}."
            )
        matches = list(
            Tenant.objects.filter(
                Q(name__icontains=search) | Q(slug__icontains=search)
            )
            .distinct()
            .order_by("id")
        )
        if len(matches) == 1:
            return matches[0]
        if not matches:
            raise CommandError(
                f"No tenant matches {needle!r}. Create Mark Anthony Brands first."
            )
        raise CommandError(
            f"{needle!r} matched {len(matches)} tenants "
            f"({', '.join(repr(t.slug) for t in matches)})."
        )

    def _sync_products_sampled_options(self, tenant: Tenant) -> None:
        """Refresh Long Drink–filtered Products Sampled caches when present."""
        from recaps.models import CustomField
        from recaps.products_sampled import PRODUCTS_SAMPLED_FIELD

        options = long_drink_product_options()
        fields = list(
            CustomField.objects.filter(
                custom_recap_template__tenant_id=tenant.id,
                name__iexact=PRODUCTS_SAMPLED_FIELD,
            ).select_related("custom_recap_template")
        )
        for field in fields:
            tpl = field.custom_recap_template
            layout = dict(tpl.layout) if isinstance(tpl.layout, dict) else {}
            types = layout.get("productsSampledProductTypes") or []
            if PRODUCT_TYPE not in types and "Long Drink" not in " ".join(
                str(t) for t in types
            ):
                # Only rewrite caches on templates that opted into Long Drink.
                continue
            field.options = list(options)
            field.save(update_fields=["options"])
            self.stdout.write(
                f"  refreshed Products Sampled on {tpl.name!r} "
                f"→ {len(options)} Long Drink option(s)"
            )

    def _attach_image(self, product: Product, url: str) -> tuple[bool, str]:
        blob = None
        last_error = None
        try:
            with httpx.Client(
                timeout=REQUEST_TIMEOUT,
                follow_redirects=True,
                headers={"User-Agent": USER_AGENT},
            ) as client:
                for attempt in range(RATE_LIMIT_RETRIES + 1):
                    if attempt:
                        time.sleep(RATE_LIMIT_BACKOFF_SECONDS)
                    response = client.get(url)
                    if response.status_code == 429:
                        last_error = "429 Too Many Requests"
                        continue
                    response.raise_for_status()
                    blob = response.content
                    break
        except Exception as exc:  # noqa: BLE001
            logger.warning("Long Drink artwork download failed for %s: %s", url, exc)
            return False, f"IMAGE DOWNLOAD FAILED: {exc}"

        if blob is None:
            return False, f"IMAGE DOWNLOAD FAILED: {last_error} after retries"

        time.sleep(DOWNLOAD_SPACING_SECONDS)
        if not blob:
            return False, "IMAGE EMPTY (0 bytes)"

        filename = f"{product.uuid}{self._extension_for_url(url)}"
        try:
            with transaction.atomic():
                product.image.save(filename, ContentFile(blob), save=False)
                product.save(update_fields=["image", "updated_at"])
        except Exception as exc:  # noqa: BLE001
            logger.warning("Long Drink artwork save failed for %s: %s", filename, exc)
            return False, f"IMAGE SAVE FAILED: {exc}"

        return True, f"image {len(blob) // 1024}KB"

    @staticmethod
    def _extension_for_url(url: str) -> str:
        path = urlparse(url).path.lower()
        for ext in (".png", ".jpg", ".jpeg", ".webp", ".gif"):
            if path.endswith(ext):
                return ".jpg" if ext == ".jpeg" else ext
        return ".png"
