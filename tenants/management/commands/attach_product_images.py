"""
Download product can images from the brand sites and attach them to
Product rows. Filling in `Product.image` makes the product picker on
the request form actually look like a product picker instead of a
bare-text list.

Sources:
  - Girl Beer cans: official Shopify CDN (girlbeer.com)
  - Borjomi Limonati cans: official static.borjomi.com
  - Variety packs aren't on the public Girl Beer storefront; skipped.

Each product is keyed by (tenant_name, product_name). Idempotent —
re-running skips products that already have an image unless --force.

Usage:
    python manage.py attach_product_images
    python manage.py attach_product_images --force          # re-download
    python manage.py attach_product_images --tenant Borjomi # one tenant
"""

from __future__ import annotations

import io
import logging
from urllib.parse import urlparse

import httpx
from django.core.files.base import ContentFile
from django.core.management.base import BaseCommand
from django.db import transaction

from events.models import Product
from tenants.models import Tenant

logger = logging.getLogger(__name__)

# Source-of-truth mapping for product images. Keyed by tenant name,
# then product name. Values are direct CDN URLs scraped from the
# brand's public storefront / news page. If a product is missing
# from this map, it's intentionally skipped — see module docstring.
IMAGE_SOURCES: dict[str, dict[str, str]] = {
    "Girl Beer": {
        # All five individual flavor 6-packs on the storefront. Purple
        # / Red Variety Packs aren't publicly listed (likely wholesale
        # SKUs) so we leave Product.image null and Kyle uploads them
        # by hand when he has them.
        "Pineapple Yuzu 6-Pack":
            "https://girlbeer.com/cdn/shop/files/Hurray_sGirlBeer_PYCan-1_1.png?v=1763059199",
        "Blueberry Lavender 6-Pack":
            "https://girlbeer.com/cdn/shop/files/Hurray_sGirlBeer_BLCan-1_1.png?v=1763058924",
        "Grapefruit Guava 6-Pack":
            "https://girlbeer.com/cdn/shop/files/Hurray_sGirlBeer_GrapefruitGuavaCan-4_1.png?v=1762983387",
        "Tangerine 6-Pack":
            "https://girlbeer.com/cdn/shop/files/Hurray_s_Girl_Beer_Tangerine_Can-1_1.png?v=1763056149",
        "Peach 6-Pack":
            "https://girlbeer.com/cdn/shop/files/Hurray_sGirlBeer_PeachCan-1_1.png?v=1763055214",
    },
    "Borjomi": {
        "Citrus Lemonade":
            "https://static.borjomi.com/uploads/screenshot-2024-03-26-at-14-31-40-11556b4c.png",
        "Pear Lemonade":
            "https://static.borjomi.com/uploads/screenshot-2024-03-26-at-14-35-55-c3b1b334.png",
        "Tarkhun Lemonade":
            "https://static.borjomi.com/uploads/screenshot-2024-03-26-at-14-35-21-d8c52291.png",
        "Adjarian Mandarin Lemonade":
            "https://static.borjomi.com/uploads/screenshot-2024-03-26-at-14-35-07-a2b1272b.png",
    },
}

REQUEST_TIMEOUT = 30.0
USER_AGENT = "Mozilla/5.0 (compatible; SparkBA-Onboarder/1.0)"


class Command(BaseCommand):
    help = "Download brand product images and attach to Product.image."

    def add_arguments(self, parser):
        parser.add_argument(
            "--tenant",
            default=None,
            help="If set, only update products for this tenant name.",
        )
        parser.add_argument(
            "--force",
            action="store_true",
            help="Re-download even if Product.image is already set.",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="List what would happen without writing.",
        )

    def handle(self, *args, **opts):
        tenant_filter: str | None = opts.get("tenant")
        force: bool = opts["force"]
        dry_run: bool = opts["dry_run"]

        for tenant_name, products in IMAGE_SOURCES.items():
            if tenant_filter and tenant_filter.lower() != tenant_name.lower():
                continue
            self._process_tenant(tenant_name, products, force, dry_run)

    def _process_tenant(
        self,
        tenant_name: str,
        products: dict[str, str],
        force: bool,
        dry_run: bool,
    ) -> None:
        self.stdout.write(self.style.MIGRATE_HEADING(
            f"\n=== {tenant_name} ==="
        ))
        try:
            tenant = Tenant.objects.get(name__iexact=tenant_name)
        except Tenant.DoesNotExist:
            self.stdout.write(self.style.WARNING(
                f"  No tenant '{tenant_name}'. Skipping."
            ))
            return

        for product_name, url in products.items():
            try:
                product = Product.objects.get(
                    tenant=tenant, name__iexact=product_name,
                )
            except Product.DoesNotExist:
                self.stdout.write(self.style.WARNING(
                    f"  - {product_name}: NO PRODUCT ROW (skip)"
                ))
                continue

            if product.image and not force:
                self.stdout.write(
                    f"  - {product_name}: already has image (skip; "
                    "use --force to re-download)"
                )
                continue

            if dry_run:
                self.stdout.write(
                    f"  - {product_name}: would download {url}"
                )
                continue

            try:
                blob = self._download(url)
            except Exception as e:
                self.stdout.write(self.style.ERROR(
                    f"  - {product_name}: download failed: {e}"
                ))
                continue

            ext = self._extension_for_url(url)
            filename = f"{product.uuid}{ext}"
            try:
                with transaction.atomic():
                    # Save through the ImageField — django-storages
                    # routes the write to the configured backend
                    # (GCS in prod) and stamps Product.image with the
                    # resulting public URL.
                    product.image.save(
                        filename, ContentFile(blob), save=False,
                    )
                    product.save(update_fields=["image", "updated_at"])
            except Exception as e:
                self.stdout.write(self.style.ERROR(
                    f"  - {product_name}: save failed: {e}"
                ))
                continue

            size_kb = len(blob) // 1024
            self.stdout.write(self.style.SUCCESS(
                f"  + {product_name}: saved {size_kb}KB → {filename}"
            ))

    def _download(self, url: str) -> bytes:
        headers = {"User-Agent": USER_AGENT}
        with httpx.Client(
            timeout=REQUEST_TIMEOUT,
            follow_redirects=True,
            headers=headers,
        ) as client:
            response = client.get(url)
            response.raise_for_status()
            return response.content

    def _extension_for_url(self, url: str) -> str:
        # Strip query string, take last segment, extract extension.
        path = urlparse(url).path
        if "." in path:
            ext = path.rsplit(".", 1)[-1].lower()
            # Filter exotic / wrong types — fall back to .png as the
            # safest browser-renderable default.
            if ext in {"png", "jpg", "jpeg", "webp", "gif"}:
                return f".{ext}"
        return ".png"
