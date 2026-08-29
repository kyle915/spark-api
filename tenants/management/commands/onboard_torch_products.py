"""Seed the Torch THC product catalog: ProductTypes + Products + images.

Torch's catalog is product LINES (drink potencies plus a 10G specialty line),
each with flavors in a 12oz single and a 4-pack (where applicable), plus one
variety pack. The line is the ProductType, so /products filters by line the
way the brand and the sales team already think.

WHY POTENCY IS IN EVERY PRODUCT NAME
    Ten flavor+size combos appear in more than one line — Black Cherry 12oz is
    both a 60mg High Potency and a 5mg Lite SKU, and there are nine more like
    it, twenty products in total. Recap exports and the sampled-products picker
    show `Product.name` alone, so without the potency those twenty rows are
    indistinguishable to a BA in the field and in the client's data afterwards.
    Names are therefore "<Flavor> <Potency> <Size>", unique across the tenant.

IMAGES
    Pulled from torchdrinks.com at run time rather than committed to this repo,
    which is how `attach_product_images` already does it. All 45 URLs were
    verified live (HTTP 200, image content-type) when this was written. A failed
    download is reported and skipped — the Product row is still created, so a
    site change degrades to "product without artwork", never to a missing SKU.

PRODUCTS SAMPLED PILLS
    GraphQL resolves "Products Sampled" options from live Product rows (same
    catalog as the upper Product Samples grid). This command also refreshes
    the stored CustomField.options cache from that catalog on --apply so dumps
    and catalog-empty fallbacks stay aligned — no second hardcoded SKU list.

Idempotent. Re-running creates nothing and (without --force-images) re-downloads
nothing. DRY-RUN by default; --apply writes.

Usage::

    python manage.py onboard_torch_products --owner-email kyle@igniteproductions.co
    python manage.py onboard_torch_products --owner-email kyle@... --apply
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

from events.models import Product, ProductType
from tenants.models import Tenant

logger = logging.getLogger(__name__)

User = get_user_model()

TENANT_SLUG = "torch-thc"
REQUEST_TIMEOUT = 30.0
USER_AGENT = "Mozilla/5.0 (compatible; SparkBA-Onboarder/1.0)"

# torchdrinks.com rate-limits a fast burst: seeding all drink SKUs back-to-back
# reliably 429s on the last handful. Space the requests out and retry a 429
# after a pause. ~45 x 1s is well inside the request timeout, and a partial
# failure is expensive here — a product whose column is already set gets
# SKIPPED on a normal re-run, so a 429 sticks until someone forces it.
DOWNLOAD_SPACING_SECONDS = 1.0
RATE_LIMIT_BACKOFF_SECONDS = 8.0
RATE_LIMIT_RETRIES = 2

# (product_type, product_name, image_url) — drink SKUs from Torch manifest.csv
# (hand-checked), plus client-requested 10G specialty labels (no artwork URL).
TORCH_PRODUCTS: list[tuple[str, str, str]] = [
    # ── Iced Tea 10mg ─────────────────────────────────
    ('Iced Tea 10mg', 'Iced Tea Lemonade 10mg 12oz',
     'https://torchdrinks.com/wp-content/uploads/2025/10/torch-10mg-12oz-thc-iced-tea-lemonade-800x.jpg'),
    ('Iced Tea 10mg', 'Iced Tea Lemonade 10mg 4-Pack',
     'https://torchdrinks.com/wp-content/uploads/2025/10/torch-10mg-12oz-4pk-thc-iced-tea-lemonade-800x.jpg'),
    ('Iced Tea 10mg', 'Mango Tea Lemonade 10mg 12oz',
     'https://torchdrinks.com/wp-content/uploads/2025/10/torch-10mg-12oz-thc-mango-tea-lemonade-800x.jpg'),
    ('Iced Tea 10mg', 'Mango Tea Lemonade 10mg 4-Pack',
     'https://torchdrinks.com/wp-content/uploads/2025/10/torch-10mg-12oz-4pk-thc-mango-tea-lemonade-800x.jpg'),
    ('Iced Tea 10mg', 'Peach Tea Lemonade 10mg 12oz',
     'https://torchdrinks.com/wp-content/uploads/2025/10/torch-10mg-12oz-thc-peach-tea-lemonade-800x.jpg'),
    ('Iced Tea 10mg', 'Peach Tea Lemonade 10mg 4-Pack',
     'https://torchdrinks.com/wp-content/uploads/2025/10/torch-10mg-12oz-4pk-thc-peach-tea-lemonade-800x.jpg'),
    ('Iced Tea 10mg', 'Raspberry 10mg 12oz',
     'https://torchdrinks.com/wp-content/uploads/2026/01/torch-10mg-12oz-thc-raspberry-iced-tea.jpg'),
    ('Iced Tea 10mg', 'Raspberry 10mg 4-Pack',
     'https://torchdrinks.com/wp-content/uploads/2026/01/torch-10mg-12oz-4pk-thc-raspberry-iced-tea.jpg'),
    # ── Iced Tea 5mg Lite ─────────────────────────────
    ('Iced Tea 5mg Lite', 'Iced Tea Lemonade 5mg 12oz',
     'https://torchdrinks.com/wp-content/uploads/2026/03/torch-lite-5mg-thc-12oz-Iced-Tea.jpg'),
    ('Iced Tea 5mg Lite', 'Iced Tea Lemonade 5mg 4-Pack',
     'https://torchdrinks.com/wp-content/uploads/2026/03/torch-lite-5mg-thc-12oz-4pk-Iced-Tea.jpg'),
    ('Iced Tea 5mg Lite', 'Peach Iced Tea 5mg 12oz',
     'https://torchdrinks.com/wp-content/uploads/2026/03/torch-lite-5mg-thc-12oz-Peach-Tea.jpg'),
    ('Iced Tea 5mg Lite', 'Peach Iced Tea 5mg 4-Pack',
     'https://torchdrinks.com/wp-content/uploads/2026/03/torch-lite-5mg-thc-12oz-4pk-Peach-Tea.jpg'),
    # ── Seltzer 25mg High Potency ─────────────────────
    ('Seltzer 25mg High Potency', 'Blue Razz 25mg 12oz',
     'https://torchdrinks.com/wp-content/uploads/2025/06/torch-drinks-25mg-thc-seltzer-blue-razz-front.png'),
    ('Seltzer 25mg High Potency', 'Blue Razz 25mg 4-Pack',
     'https://torchdrinks.com/wp-content/uploads/2025/06/torch-drinks-25mg-seltzers-blue-razz-4-pack.png'),
    ('Seltzer 25mg High Potency', 'Fruit Punch 25mg 12oz',
     'https://torchdrinks.com/wp-content/uploads/2025/06/torch-drinks-25mg-thc-seltzer-fruit-punch-front.png'),
    ('Seltzer 25mg High Potency', 'Fruit Punch 25mg 4-Pack',
     'https://torchdrinks.com/wp-content/uploads/2025/06/torch-drinks-25mg-seltzers-fruit-punch-4-pack.png'),
    ('Seltzer 25mg High Potency', 'Grapefruit Delight 25mg 12oz',
     'https://torchdrinks.com/wp-content/uploads/2025/06/torch-drinks-25mg-thc-seltzer-grapefruit-delight-front.png'),
    ('Seltzer 25mg High Potency', 'Grapefruit Delight 25mg 4-Pack',
     'https://torchdrinks.com/wp-content/uploads/2025/06/torch-drinks-25mg-seltzers-grapefruit-delight-4-pack.png'),
    ('Seltzer 25mg High Potency', 'Passionfruit 25mg 12oz',
     'https://torchdrinks.com/wp-content/uploads/2025/06/torch-drinks-25mg-thc-seltzer-passion-fruit-front.png'),
    ('Seltzer 25mg High Potency', 'Passionfruit 25mg 4-Pack',
     'https://torchdrinks.com/wp-content/uploads/2025/06/torch-drinks-25mg-seltzers-passion-fruit-4-pack.png'),
    ('Seltzer 25mg High Potency', 'Pink Lemonade 25mg 12oz',
     'https://torchdrinks.com/wp-content/uploads/2025/06/torch-drinks-25mg-thc-seltzer-pink-lemonade-front.png'),
    ('Seltzer 25mg High Potency', 'Pink Lemonade 25mg 4-Pack',
     'https://torchdrinks.com/wp-content/uploads/2025/06/torch-drinks-25mg-seltzers-pink-lemonade-4-pack.png'),
    ('Seltzer 25mg High Potency', 'Watermelon Squeeze 25mg 12oz',
     'https://torchdrinks.com/wp-content/uploads/2025/06/torch-drinks-25mg-thc-seltzer-watermelon-squeeze-front.png'),
    ('Seltzer 25mg High Potency', 'Watermelon Squeeze 25mg 4-Pack',
     'https://torchdrinks.com/wp-content/uploads/2025/06/torch-drinks-25mg-seltzers-watermelon-squeeze-4-pack.png'),
    # ── Seltzer 5mg Lite ──────────────────────────────
    ('Seltzer 5mg Lite', 'Black Cherry 5mg 12oz',
     'https://torchdrinks.com/wp-content/uploads/2026/03/torch-lite-5mg-thc-12oz-Black-Cherry.jpg'),
    ('Seltzer 5mg Lite', 'Black Cherry 5mg 4-Pack',
     'https://torchdrinks.com/wp-content/uploads/2026/03/torch-lite-5mg-thc-12oz-4pk-Black-Cherry.jpg'),
    ('Seltzer 5mg Lite', 'Blue Razz Lemonade 5mg 12oz',
     'https://torchdrinks.com/wp-content/uploads/2026/03/torch-lite-5mg-thc-12oz-Blue-Razz.jpg'),
    ('Seltzer 5mg Lite', 'Blue Razz Lemonade 5mg 4-Pack',
     'https://torchdrinks.com/wp-content/uploads/2026/03/torch-lite-5mg-thc-12oz-4pk-Blue-Razz-Lemonade.jpg'),
    ('Seltzer 5mg Lite', 'Strawberry Lemonade 5mg 12oz',
     'https://torchdrinks.com/wp-content/uploads/2026/03/torch-lite-5mg-thc-12oz-Strawberry-Lemonade.jpg'),
    ('Seltzer 5mg Lite', 'Strawberry Lemonade 5mg 4-Pack',
     'https://torchdrinks.com/wp-content/uploads/2026/03/torch-lite-5mg-thc-12oz-4pk-Strawberry-Lemonade.jpg'),
    ('Seltzer 5mg Lite', 'Watermelon Limeade 5mg 12oz',
     'https://torchdrinks.com/wp-content/uploads/2026/03/torch-lite-5mg-thc-12oz-Watemelon-Limeade.jpg'),
    ('Seltzer 5mg Lite', 'Watermelon Limeade 5mg 4-Pack',
     'https://torchdrinks.com/wp-content/uploads/2026/03/torch-lite-5mg-thc-12oz-4pk-Watemelon-Limeade.jpg'),
    # ── Seltzer 60mg High Potency ─────────────────────
    ('Seltzer 60mg High Potency', 'Black Cherry 60mg 12oz',
     'https://torchdrinks.com/wp-content/uploads/2025/01/3.Black-Cherry-Seltzer_Torch_12oz.png'),
    ('Seltzer 60mg High Potency', 'Black Cherry 60mg 4-Pack',
     'https://torchdrinks.com/wp-content/uploads/2025/01/3.2black-cherry_masterbox.png'),
    ('Seltzer 60mg High Potency', 'Blue Razz Lemonade 60mg 12oz',
     'https://torchdrinks.com/wp-content/uploads/2025/01/1.Torch_bluerazz_Torch_12oz.png'),
    ('Seltzer 60mg High Potency', 'Blue Razz Lemonade 60mg 4-Pack',
     'https://torchdrinks.com/wp-content/uploads/2025/01/1.2_blue-razz_masterbox.png'),
    ('Seltzer 60mg High Potency', 'Cherry Limeade 60mg 12oz',
     'https://torchdrinks.com/wp-content/uploads/2025/04/torch-drinks-60mg-cherry-limeade-single.jpg'),
    ('Seltzer 60mg High Potency', 'Cherry Limeade 60mg 4-Pack',
     'https://torchdrinks.com/wp-content/uploads/2025/04/torch-drinks-60mg-cherry-limeade-4-pack.jpg'),
    ('Seltzer 60mg High Potency', 'Melon Madness 60mg 12oz',
     'https://torchdrinks.com/wp-content/uploads/2026/01/torch-60mg-thc-seltzer-12oz-Melon-Madness-800x.jpg'),
    ('Seltzer 60mg High Potency', 'Melon Madness 60mg 4-Pack',
     'https://torchdrinks.com/wp-content/uploads/2026/01/torch-60mg-thc-seltzer-4pk-Melon-Madness-800x.jpg'),
    ('Seltzer 60mg High Potency', 'Strawberry Lemonade 60mg 12oz',
     'https://torchdrinks.com/wp-content/uploads/2026/01/torch-60mg-thc-seltzer-12oz-Strawberry-Lemonade-800x.jpg'),
    ('Seltzer 60mg High Potency', 'Strawberry Lemonade 60mg 4-Pack',
     'https://torchdrinks.com/wp-content/uploads/2026/01/torch-60mg-thc-seltzer-4pk-Strawberry-Lemonade-800x.jpg'),
    ('Seltzer 60mg High Potency', 'Watermelon Squeeze 60mg 12oz',
     'https://torchdrinks.com/wp-content/uploads/2026/01/torch-60mg-thc-seltzer-12oz-Watermelon-Squeeze-800x.jpg'),
    ('Seltzer 60mg High Potency', 'Watermelon Squeeze 60mg 4-Pack',
     'https://torchdrinks.com/wp-content/uploads/2026/01/torch-60mg-thc-seltzer-4pk-Watermelon-Squeeze-800x.jpg'),
    # ── Variety Pack ──────────────────────────────────
    ('Variety Pack', 'Variety Pack 10mg 6-Pack',
     'https://torchdrinks.com/wp-content/uploads/2025/05/torch-10mg-thc-drink-variety-sampler-2.jpg'),
    # ── 10G (client-requested specialty labels; empty URL = no artwork) ──
    # Exact picker strings from Torch — keep ALL CAPS / 10G vs 10MG as given.
    ('10G', 'TORCH STRAWBERRY LEMONADE 10G', ''),
    ('10G', 'TORCH BLACK CHERRY 10G', ''),
    ('10G', 'TORCH WATERMELON 10MG', ''),
]


def torch_product_options() -> list[str]:
    """Onboard SKUs as ``"Line — Name"`` choice values for Event Confirmation.

    Same shape as Liquid Death's picker (``Category — SKU``) so the admin tab
    and the email strip-prefix path stay one code path. Used when the live
    Product catalog is empty (tests, a tenant that hasn't been onboarded).
    """
    return [f"{cat} — {name}" for cat, name, _url in TORCH_PRODUCTS]


class Command(BaseCommand):
    help = (
        "Seed Torch THC product types + products (+ artwork). "
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
            "--apply",
            action="store_true",
            help="Actually write (omit for a dry run that changes nothing).",
        )
        parser.add_argument(
            "--skip-images",
            dest="skip_images",
            action="store_true",
            help="Create the product rows but download no artwork.",
        )
        parser.add_argument(
            "--report",
            action="store_true",
            help=(
                "READ-ONLY: list every Torch product with its resolved public "
                "image URL and whether that URL actually serves. Writes nothing."
            ),
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
        except User.DoesNotExist:
            raise CommandError(f"No user with email {opts['owner_email']!r}.")

        # Resolve by slug, and refuse on ambiguity rather than guessing. Slug has
        # no unique constraint on this model — the duplicate-Sipli cleanup was
        # exactly this failure, two rows sharing a name AND a slug.
        matches = list(Tenant.objects.filter(slug=TENANT_SLUG).order_by("id"))
        if not matches:
            raise CommandError(f"No tenant with slug {TENANT_SLUG!r}.")
        if len(matches) > 1:
            ids = ", ".join(f"[{t.id}] {t.name!r}" for t in matches)
            raise CommandError(
                f"{len(matches)} tenants share slug {TENANT_SLUG!r} ({ids}). "
                "Resolve the duplicate before seeding products into one of them."
            )
        tenant = matches[0]

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
            f"EXISTING: {ProductType.objects.filter(tenant=tenant).count()} "
            f"product type(s), {Product.objects.filter(tenant=tenant).count()} "
            "product(s)"
        )
        self.stdout.write("=" * 72)

        if opts["report"]:
            self._report(tenant)
            return

        type_names = sorted({row[0] for row in TORCH_PRODUCTS})
        stats = {
            "types_created": 0, "types_found": 0,
            "products_created": 0, "products_found": 0,
            "images_saved": 0, "images_skipped": 0, "images_failed": 0,
        }

        if not apply:
            self.stdout.write("\nWould create/confirm product types:")
            for name in type_names:
                n = sum(1 for r in TORCH_PRODUCTS if r[0] == name)
                self.stdout.write(f"  {name:<28} {n:>2} product(s)")
            self.stdout.write(
                f"\nDRY-RUN — would upsert {len(type_names)} product type(s) and "
                f"{len(TORCH_PRODUCTS)} product(s)"
                f"{'' if skip_images else ', downloading artwork for each'}. "
                "Re-run with --apply to write."
            )
            return

        types: dict[str, ProductType] = {}
        for name in type_names:
            pt, created = ProductType.objects.get_or_create(
                tenant=tenant, name=name, defaults={"created_by": owner},
            )
            types[name] = pt
            stats["types_created" if created else "types_found"] += 1
            self.stdout.write(
                f"  {'+' if created else '='} ProductType id={pt.id} {name!r}"
            )

        self.stdout.write("")
        for type_name, product_name, image_url in TORCH_PRODUCTS:
            product, created = Product.objects.get_or_create(
                tenant=tenant,
                name=product_name,
                defaults={
                    "product_type": types[type_name],
                    "created_by": owner,
                },
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
                f"  {mark} id={product.id:<6} {product_name:<32}{note}"
            )

        self._sync_products_sampled_options(tenant)

        self.stdout.write("")
        self.stdout.write("=" * 72)
        self.stdout.write(self.style.SUCCESS(
            f"Types: {stats['types_created']} created, "
            f"{stats['types_found']} already present.  "
            f"Products: {stats['products_created']} created, "
            f"{stats['products_found']} already present."
        ))
        msg = (
            f"Artwork: {stats['images_saved']} saved, "
            f"{stats['images_skipped']} skipped, {stats['images_failed']} failed."
        )
        self.stdout.write(
            self.style.WARNING(msg) if stats["images_failed"]
            else self.style.SUCCESS(msg)
        )
        self.stdout.write(
            f"Tenant [{tenant.id}] now has "
            f"{ProductType.objects.filter(tenant=tenant).count()} product type(s) "
            f"and {Product.objects.filter(tenant=tenant).count()} product(s)."
        )
        self.stdout.write("=" * 72)

    # ------------------------------------------------------------------

    def _sync_products_sampled_options(self, tenant: Tenant) -> None:
        """Refresh every Torch Products Sampled multiselect from the catalog.

        GraphQL already prefers live Product rows at read time; this keeps the
        stored JSON cache aligned after onboard so admin dumps and any
        catalog-empty fallback stay correct.
        """
        from events.event_confirmations import catalog_product_options
        from recaps.models import CustomField
        from recaps.products_sampled import PRODUCTS_SAMPLED_FIELD

        options = catalog_product_options(tenant)
        if not options:
            self.stdout.write(
                self.style.WARNING(
                    f"  no Product rows to sync onto '{PRODUCTS_SAMPLED_FIELD}'"
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
                f"  No '{PRODUCTS_SAMPLED_FIELD}' choice field on Torch "
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

    def _attach_image(self, product: Product, url: str) -> tuple[bool, str]:
        """Download `url` into Product.image. Never raises — a brand-site
        change should cost artwork, not the product row."""
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
            logger.warning("Torch artwork download failed for %s: %s", url, exc)
            return False, f"IMAGE DOWNLOAD FAILED: {exc}"

        if blob is None:
            logger.warning("Torch artwork rate-limited for %s: %s", url, last_error)
            return False, f"IMAGE DOWNLOAD FAILED: {last_error} after retries"

        # Space out the next request rather than the previous one, so a run
        # that skips most products doesn't pay the delay for them.
        time.sleep(DOWNLOAD_SPACING_SECONDS)

        if not blob:
            return False, "IMAGE EMPTY (0 bytes)"

        filename = f"{product.uuid}{self._extension_for_url(url)}"
        try:
            with transaction.atomic():
                # Save through the ImageField so django-storages routes the
                # write to the configured backend (GCS in prod).
                product.image.save(filename, ContentFile(blob), save=False)
                product.save(update_fields=["image", "updated_at"])
        except Exception as exc:  # noqa: BLE001
            logger.warning("Torch artwork save failed for %s: %s", filename, exc)
            return False, f"IMAGE SAVE FAILED: {exc}"

        return True, f"image {len(blob) // 1024}KB"

    def _report(self, tenant: Tenant) -> None:
        """Print each product's public image URL and whether it serves.

        The GraphQL `image` field hands the frontend `public_url(blob)` and the
        products grid uses it as a bare `<img src>`, so a row renders broken if
        EITHER the column is empty or the object is not publicly readable. Those
        look identical in the browser, and only this distinguishes them.
        """
        from utils.gcs import extract_blob_name_from_url, public_url

        products = list(
            Product.objects.filter(tenant=tenant)
            .select_related("product_type")
            .order_by("product_type__name", "name")
        )
        no_column, serves, fails = [], [], []

        with httpx.Client(
            timeout=REQUEST_TIMEOUT, follow_redirects=True,
            headers={"User-Agent": USER_AGENT},
        ) as client:
            for product in products:
                raw = product.image.name if product.image else None
                if not raw:
                    no_column.append(product.name)
                    self.stdout.write(f"  NO IMAGE COLUMN  {product.name}")
                    continue
                url = public_url(extract_blob_name_from_url(raw))
                try:
                    resp = client.head(url)
                    code = resp.status_code
                    ctype = resp.headers.get("Content-Type", "")
                except Exception as exc:  # noqa: BLE001
                    fails.append((product.name, f"fetch error: {exc}"))
                    self.stdout.write(f"  FETCH ERROR      {product.name}: {exc}")
                    continue
                if code == 200 and ctype.startswith("image"):
                    serves.append(product.name)
                    size = resp.headers.get("Content-Length", "?")
                    self.stdout.write(
                        f"  OK {code} {ctype:<11} {int(size) // 1024 if size.isdigit() else '?':>4}KB  "
                        f"{product.name}"
                    )
                else:
                    fails.append((product.name, f"HTTP {code} {ctype}"))
                    self.stdout.write(
                        f"  NOT SERVING {code} {ctype or 'no-type'}  {product.name}\n"
                        f"      {url}"
                    )

        self.stdout.write("")
        self.stdout.write("=" * 72)
        line = (
            f"{len(products)} product(s): {len(serves)} image serves OK, "
            f"{len(no_column)} with no image set, {len(fails)} set but NOT serving."
        )
        self.stdout.write(
            self.style.SUCCESS(line) if not (no_column or fails)
            else self.style.WARNING(line)
        )
        if fails:
            self.stdout.write(
                "\nSet but not serving means the bytes uploaded and the column is "
                "populated, but the object is not publicly readable — check that "
                "the bucket still grants allUsers:objectViewer."
            )
        self.stdout.write("=" * 72)

    def _extension_for_url(self, url: str) -> str:
        path = urlparse(url).path
        if "." in path:
            ext = path.rsplit(".", 1)[-1].lower()
            if ext in {"png", "jpg", "jpeg", "webp", "gif"}:
                return f".{ext}"
        return ".png"
