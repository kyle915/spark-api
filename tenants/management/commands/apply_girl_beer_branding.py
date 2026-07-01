"""
One-off: apply Girl Beer's real brand identity to its public receipt
campaign page (/c/girlbeer) — logo, brand colors, and hero/product
photography, all scraped from the official girlbeer.com storefront.

Three writes, each independently idempotent:
  1. Tenant.image        — the circular purple "Hurray's Girl Beer" badge
                            (their favicon/social icon — colored, so it
                            reads on any background, unlike their header
                            wordmark which is pure white).
  2. TenantTheme(light)   — brand palette sampled from the live site's own
                            CSS: #830DFF is their dominant color by a wide
                            margin (121 occurrences on the homepage vs. the
                            next most common non-neutral color at 8), so
                            it's a verified brand color, not a guess.
  3. ReceiptCampaign      — hero_image (a real lifestyle photo from their
     (slug="girlbeer")      homepage) + product_image (one flavor can),
                            for the campaign page's hero banner.

Source URLs are Shopify CDN links scraped 2026-07-01; kept here (not in a
config file) since this is a single-tenant one-off, mirroring
attach_product_images.py's IMAGE_SOURCES convention for the same reason.

Usage:
    python manage.py apply_girl_beer_branding --dry-run   # preview, no writes
    python manage.py apply_girl_beer_branding              # apply
    python manage.py apply_girl_beer_branding --force       # re-download + overwrite
"""

from __future__ import annotations

import logging
from urllib.parse import urlparse

import httpx
from django.core.files.base import ContentFile
from django.core.management.base import BaseCommand
from django.db import transaction

from receipts.models import ReceiptCampaign
from tenants.models import Tenant, TenantTheme
from utils.gcs import upload_bytes

logger = logging.getLogger(__name__)

TENANT_REQUEST_URL_NAME = "girl-beer"
CAMPAIGN_SLUG = "girlbeer"

LOGO_URL = (
    "https://girlbeer.com/cdn/shop/files/Hurray_sGBSocialIcon-8.png"
    "?v=1733015688"
)
HERO_IMAGE_URL = (
    "https://girlbeer.com/cdn/shop/files/GIRLBEER_FINAL_20258_1.jpg"
    "?v=1764973141"
)
PRODUCT_IMAGE_URL = (
    "https://girlbeer.com/cdn/shop/files/Hurray_sGirlBeer_PYCan-1_1.png"
    "?v=1763059199"
)

# Sampled from girlbeer.com's own stylesheet/inline styles. #830DFF is their
# dominant brand purple (header background, buttons, accents — 121 hits on
# the homepage HTML alone). The rest are restrained near-white/near-black
# choices to stay premium rather than loud — no gradients, no glow.
# Only a subset of DaisyUI keys are set; any key left out falls through to
# CampaignUpload.tsx's own neutral literal default (see `pick()` there).
THEME_CSS_VARIABLES: dict[str, str] = {
    "--color-primary": "#830DFF",
    "--color-primary-content": "#FFFFFF",
    "--color-base-100": "#FFFFFF",
    "--color-base-200": "#F5EEFF",
    "--color-base-300": "#E4D9F5",
    "--color-base-content": "#1A1A1A",
    "--color-neutral": "#E4D9F5",
    "--color-neutral-content": "#1A1A1A",
}

REQUEST_TIMEOUT = 30.0
USER_AGENT = "Mozilla/5.0 (compatible; SparkBA-Onboarder/1.0)"


class Command(BaseCommand):
    help = "Apply Girl Beer's real logo, brand colors, and campaign imagery."

    def add_arguments(self, parser):
        parser.add_argument(
            "--force",
            action="store_true",
            help="Re-download and overwrite even if already set.",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Narrate what would happen without writing anything.",
        )

    def handle(self, *args, **opts):
        force: bool = opts["force"]
        dry_run: bool = opts["dry_run"]

        try:
            tenant = Tenant.objects.get(
                request_url_name=TENANT_REQUEST_URL_NAME
            )
        except Tenant.DoesNotExist:
            self.stdout.write(self.style.ERROR(
                f"No tenant with request_url_name={TENANT_REQUEST_URL_NAME!r}."
            ))
            return

        self.stdout.write(self.style.MIGRATE_HEADING(
            f"\n=== Girl Beer (tenant #{tenant.id}) ==="
        ))

        self._apply_logo(tenant, force, dry_run)
        self._apply_theme(tenant, force, dry_run)
        self._apply_campaign_images(tenant, force, dry_run)

    # ------------------------------------------------------------------
    def _apply_logo(self, tenant: Tenant, force: bool, dry_run: bool) -> None:
        if tenant.image and not force:
            self.stdout.write(
                "  - logo: already set (skip; use --force to re-download)"
            )
            return
        if dry_run:
            self.stdout.write(f"  - logo: would download {LOGO_URL}")
            return
        try:
            blob = self._download(LOGO_URL)
        except Exception as e:
            self.stdout.write(self.style.ERROR(f"  - logo: download failed: {e}"))
            return
        filename = f"{tenant.uuid}-logo{self._extension_for_url(LOGO_URL)}"
        try:
            with transaction.atomic():
                tenant.image.save(filename, ContentFile(blob), save=False)
                tenant.save(update_fields=["image", "updated_at"])
        except Exception as e:
            self.stdout.write(self.style.ERROR(f"  - logo: save failed: {e}"))
            return
        self.stdout.write(self.style.SUCCESS(
            f"  + logo: saved {len(blob) // 1024}KB -> {filename}"
        ))

    # ------------------------------------------------------------------
    def _apply_theme(self, tenant: Tenant, force: bool, dry_run: bool) -> None:
        existing = TenantTheme.objects.filter(
            tenant=tenant, color_scheme="light"
        ).first()
        if existing and not force:
            self.stdout.write(
                "  - theme (light): already set (skip; use --force to overwrite)"
            )
            return
        if dry_run:
            self.stdout.write(
                f"  - theme (light): would set {len(THEME_CSS_VARIABLES)} "
                "variables (primary #830DFF)"
            )
            return
        TenantTheme.objects.update_or_create(
            tenant=tenant,
            color_scheme="light",
            defaults={
                "name": "Girl Beer",
                "css_variables": THEME_CSS_VARIABLES,
            },
        )
        self.stdout.write(self.style.SUCCESS(
            "  + theme (light): saved (primary #830DFF)"
        ))

    # ------------------------------------------------------------------
    def _apply_campaign_images(
        self, tenant: Tenant, force: bool, dry_run: bool
    ) -> None:
        try:
            campaign = ReceiptCampaign.objects.get(
                tenant=tenant, slug=CAMPAIGN_SLUG
            )
        except ReceiptCampaign.DoesNotExist:
            self.stdout.write(self.style.WARNING(
                f"  - campaign: no campaign with slug={CAMPAIGN_SLUG!r} (skip)"
            ))
            return

        for field, url, label in (
            ("hero_image", HERO_IMAGE_URL, "hero image"),
            ("product_image", PRODUCT_IMAGE_URL, "product image"),
        ):
            if getattr(campaign, field) and not force:
                self.stdout.write(
                    f"  - {label}: already set (skip; use --force to re-download)"
                )
                continue
            if dry_run:
                self.stdout.write(f"  - {label}: would download {url}")
                continue
            try:
                blob = self._download(url)
            except Exception as e:
                self.stdout.write(self.style.ERROR(
                    f"  - {label}: download failed: {e}"
                ))
                continue
            ext = self._extension_for_url(url)
            blob_name = f"receipt-campaigns/{campaign.uuid}/{field}{ext}"
            try:
                content_type = "image/jpeg" if ext == ".jpg" else "image/png"
                upload_bytes(blob_name, blob, content_type=content_type)
                setattr(campaign, field, blob_name)
                campaign.save(update_fields=[field, "updated_at"])
            except Exception as e:
                self.stdout.write(self.style.ERROR(f"  - {label}: save failed: {e}"))
                continue
            self.stdout.write(self.style.SUCCESS(
                f"  + {label}: saved {len(blob) // 1024}KB -> {blob_name}"
            ))

    # ------------------------------------------------------------------
    def _download(self, url: str) -> bytes:
        headers = {"User-Agent": USER_AGENT}
        with httpx.Client(
            timeout=REQUEST_TIMEOUT, follow_redirects=True, headers=headers,
        ) as client:
            response = client.get(url)
            response.raise_for_status()
            return response.content

    def _extension_for_url(self, url: str) -> str:
        path = urlparse(url).path
        if "." in path:
            ext = path.rsplit(".", 1)[-1].lower()
            if ext in {"png", "jpg", "jpeg", "webp", "gif"}:
                return f".{ext}"
        return ".png"
