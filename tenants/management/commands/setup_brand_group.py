"""Set up the FAMUS / Alcohol Armor / Wook Water brand group.

For each brand (exact slug, then exact request_url_name; created with
createTenant-style seeds when missing):

* programs: Retail Sampling + Event Activation only (request types and event
  types; unused extras retired, used ones left and reported),
* Product catalog + SKU images (downloaded from the brand's own site),
* Retail Sampling + Event Activation recap templates with shared sections
  answered once and per-brand sections (``layout.coBrandSections``) so one
  BA form writes one recap per brand on a co-activation,
* photo buckets, standing walk-up programs, address location mode,
  "sales" Insights trend series, light theme + logo,
* one standing check-in code per brand — minted once, never rotated.

Then the three tenants join one :class:`BrandGroup`.

Recaps stay admin-approved (approved=False until review) like every tenant.
DRY-RUN by default; ``--apply`` writes. Prod: ``/internal/cron/setup-brand-group``
or the "Setup brand group" GitHub Action.
"""

from __future__ import annotations

import logging
import secrets
import time
from dataclasses import dataclass, field
from urllib.parse import urlparse

import httpx
from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.files.base import ContentFile
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils.text import slugify

from ambassadors.models import AttendanceStatus, Skill
from events.event_confirmations import catalog_product_options
from events.models import (
    Event,
    EventStatus,
    EventType,
    Product,
    ProductType,
    Request,
    RequestStatus,
    RequestType,
)
from jobs.models import RateType
from jobs.models import Status as JobStatus
from recaps.co_activation import CO_BRAND_SECTIONS_KEY
from recaps.management.commands.setup_total_wireless_checkin import _match_field_type
from recaps.models import (
    CustomField,
    CustomRecap,
    CustomRecapFieldType,
    CustomRecapTemplate,
    FileRecapCategory,
    RecapSection,
    TypeOfGood,
)
from recaps.products_sampled import PRODUCTS_SAMPLED_FIELD
from tenants.models import BrandGroup, Tenant, TenantTheme
from tenants.mutations import (
    DEFAULT_ATTENDANCE_STATUS_TEMPLATES,
    DEFAULT_FILE_RECAP_CATEGORIES,
    DEFAULT_JOB_STATUS_TEMPLATES,
    DEFAULT_RATE_TYPES,
    DEFAULT_SKILLS,
    DEFAULT_STATUS_TEMPLATES,
    DEFAULT_TYPES_OF_GOOD,
)
from utils.gcs import upload_bytes

logger = logging.getLogger(__name__)

GROUP_NAME = "FAMUS · Alcohol Armor · Wook Water"
GROUP_SLUG = "famus-alcohol-armor-wook-water"

RETAIL = "Retail Sampling"
EVENT = "Event Activation"
PROGRAMS = (RETAIL, EVENT)
# Legacy seed names folded into the two programs (old name -> program).
EVENT_TYPE_RENAMES = {"event": EVENT}

PER_BRAND_SECTIONS = ["Brand Results", "Brand Feedback"]

USER_AGENT = "Mozilla/5.0 (compatible; SparkCatalogBot/1.0; +https://igniteproductions.co)"
REQUEST_TIMEOUT = 30.0


@dataclass(frozen=True)
class Sku:
    name: str
    image_url: str


@dataclass(frozen=True)
class Brand:
    name: str
    slug: str
    code_prefix: str
    product_type: str
    primary_color: str
    logo_url: str
    skus: tuple[Sku, ...] = field(default_factory=tuple)


BRANDS: tuple[Brand, ...] = (
    Brand(
        name="FAMUS Energy",
        slug="famus-energy",
        code_prefix="FM",
        product_type="Energy Drink",
        primary_color="#C8A058",
        logo_url="https://famusenergy.com/famus-logo.png",
        skus=(
            Sku("Havana Nights (Mint & Lime) 16oz", "https://famusenergy.com/bottle-havana.png"),
            Sku("OG Luxe (Tropical Ice) 16oz", "https://famusenergy.com/bottle-ogluxe.png"),
        ),
    ),
    Brand(
        name="Alcohol Armor",
        slug="alcohol-armor",
        code_prefix="AA",
        product_type="Functional Shot",
        primary_color="#2FA8E1",
        logo_url=(
            "https://alcoholarmor.com/cdn/shop/t/19/assets/aa-wordmark.png"
            "?v=30063181739944285441787610059"
        ),
        skus=(
            Sku(
                "Boujee Berry 2oz Shot",
                "https://cdn.shopify.com/s/files/1/0696/3639/1234/files/AlcoholArmor-BB-Shot-Front_ef3af8c7-b2ca-4fee-ae48-470dcbc68f0d.png?v=1761266458&width=1200",
            ),
            Sku(
                "Slayberry 2oz Shot",
                "https://cdn.shopify.com/s/files/1/0696/3639/1234/files/AlcoholArmor-SB-Shot-Front_f2272953-e391-486d-8b20-8cedc4426a46.png?v=1761267306&width=1200",
            ),
            Sku(
                "Boujee Berry 12-Pack",
                "https://cdn.shopify.com/s/files/1/0696/3639/1234/files/AlcoholArmor-BoujeeBerry-Shipper2_cbf43402-e0c2-4642-90ec-293d986ba73a.png?v=1761266458&width=1200",
            ),
            Sku(
                "Slayberry 12-Pack",
                "https://cdn.shopify.com/s/files/1/0696/3639/1234/files/AlcoholArmor-Slayberry-Shipper2_f7510a12-e1b8-42a6-ad40-e203ca02d054.png?v=1761267048&width=1200",
            ),
        ),
    ),
    Brand(
        name="Wook Water",
        slug="wook-water",
        code_prefix="WW",
        product_type="Functional Hydration",
        primary_color="#EA3295",
        logo_url=(
            "https://cdn.shopify.com/s/files/1/0929/3771/6083/files/"
            "62EF072A-276C-47F5-95EC-83266BC25781.png?v=1784839331"
        ),
        skus=(
            Sku(
                "Berry Groovy 12oz Can",
                "https://cdn.shopify.com/s/files/1/0929/3771/6083/files/BG-Single_02_1.png?v=1774203856&width=1200",
            ),
            Sku(
                "Laser Lime 12oz Can",
                "https://cdn.shopify.com/s/files/1/0929/3771/6083/files/LL-Single_02_549b0293-38c3-45de-82da-b1b910f1a548.png?v=1774203838&width=1200",
            ),
            Sku(
                "Pineapple Peak 12oz Can",
                "https://cdn.shopify.com/s/files/1/0929/3771/6083/files/pineapple_hero_single.png?v=1785249962&width=1200",
            ),
            Sku(
                "Berry Groovy 4-Pack",
                "https://cdn.shopify.com/s/files/1/0929/3771/6083/files/4pk_berry_-_website_df61984d-defc-4315-bc3f-3860ab33cf42.png?v=1783535527&width=1080",
            ),
            Sku(
                "Laser Lime 4-Pack",
                "https://cdn.shopify.com/s/files/1/0929/3771/6083/files/4pk_lime_-_website_e43cd5cb-220f-4e23-853c-ae8497908688.png?v=1783535528&width=1080",
            ),
            Sku(
                "Pineapple Peak 4-Pack",
                "https://cdn.shopify.com/s/files/1/0929/3771/6083/files/pineapple4pk.jpg?v=1785249819&width=1080",
            ),
        ),
    ),
)

# (section, [(field name, kind, required, options)])
FieldSpec = tuple[str, str, bool, list[str]]
SectionSpec = tuple[str, list[FieldSpec]]


def retail_spec(brand_name: str) -> list[SectionSpec]:
    return [
        (
            "Shift Overview",
            [
                ("Estimated store foot traffic", "number", True, []),
                ("Sample format", "select", True, ["Full can / bottle", "Sample cup / pour"]),
                ("Account Feedback", "longtext", True, []),
                ("Anything you'd change or do differently?", "longtext", False, []),
                ("Demographics (general age, sex, ethnicities of consumers)", "longtext", True, []),
            ],
        ),
        (
            "Brand Results",
            [
                ("Total number of consumers sampled", "number", True, []),
                ("First Time consumers?", "number", True, []),
                (
                    f"How many consumers that were engaged with knew about {brand_name}?",
                    "number",
                    True,
                    [],
                ),
                (
                    "How many consumers would be willing to purchase the product after tasting it?",
                    "number",
                    True,
                    [],
                ),
                (
                    "How many consumers would NOT be willing to purchase the product after tasting it?",
                    "number",
                    True,
                    [],
                ),
                (
                    "How many single cans / bottles / shots did consumers purchase?",
                    "number",
                    True,
                    [],
                ),
                ("How many packs did consumers purchase?", "number", True, []),
                (PRODUCTS_SAMPLED_FIELD, "multiselect", True, []),
            ],
        ),
        (
            "Brand Feedback",
            [
                ("Consumer Feedback", "longtext", True, []),
                ("Quotes from Consumers", "longtext", False, []),
                ("Reasons to decline to purchase?", "longtext", False, []),
            ],
        ),
    ]


def event_spec(brand_name: str) -> list[SectionSpec]:
    return [
        (
            "Event Details",
            [
                ("Event name / venue", "text", True, []),
                ("Estimated attendance / foot traffic", "number", True, []),
                ("Sample format", "select", True, ["Full can / bottle", "Sample cup / pour"]),
                (
                    "Key highlight (1–2 sentences) / What worked well & What didn't?",
                    "longtext",
                    True,
                    [],
                ),
                ("Demographics (general age, sex, ethnicities of consumers)", "longtext", True, []),
            ],
        ),
        (
            "Brand Results",
            [
                ("Total number of consumers sampled", "number", True, []),
                (
                    f"How many consumers that were engaged with knew about {brand_name}?",
                    "number",
                    True,
                    [],
                ),
                (PRODUCTS_SAMPLED_FIELD, "multiselect", True, []),
            ],
        ),
        (
            "Brand Feedback",
            [
                ("Consumer Feedback", "longtext", True, []),
                ("Quotes from Consumers", "longtext", False, []),
            ],
        ),
    ]


SECTION_ORDER = {
    "Shift Overview": 0,
    "Event Details": 0,
    "Brand Results": 1,
    "Brand Feedback": 2,
}

PHOTO_BUCKETS_BY_PROGRAM: dict[str, list[dict]] = {
    RETAIL: [
        {"name": "Table Set Up"},
        {"name": "Product Display"},
        {"name": "Consumer Sampling Pictures"},
        {"name": "Product Receipt"},
    ],
    EVENT: [
        {"name": "Activation Setup"},
        {"name": "Consumer Sampling Pictures"},
        {"name": "Expense Receipts"},
    ],
}

CODE_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"


def template_name(brand: Brand, program: str) -> str:
    return f"{brand.name} · {program} Recap"


class Command(BaseCommand):
    help = (
        "Set up FAMUS Energy, Alcohol Armor and Wook Water as one brand group "
        "(dry-run by default; --apply to write)."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--tenant",
            default="",
            help="limit to one brand slug (default: all three).",
        )
        parser.add_argument(
            "--skip-images",
            action="store_true",
            help="don't download SKU images / logos.",
        )
        parser.add_argument(
            "--apply",
            action="store_true",
            help="actually write (omit for a dry-run that changes nothing).",
        )

    def handle(self, *args, **opts):
        self.apply = bool(opts["apply"])
        self.skip_images = bool(opts["skip_images"])
        only = (opts.get("tenant") or "").strip().lower()
        brands = [b for b in BRANDS if not only or b.slug == only]
        if not brands:
            raise CommandError(f"--tenant {only!r} isn't one of {[b.slug for b in BRANDS]}.")
        self.creator = self._resolve_creator()
        self.ft_cache: dict = {}
        self.stdout.write("=" * 68)
        self.stdout.write(f"Mode : {'APPLY (writing)' if self.apply else 'DRY-RUN (no writes)'}")
        self.stdout.write("=" * 68)

        tenants: list[Tenant] = []
        for brand in brands:
            tenant = self._setup_brand(brand)
            if tenant is not None:
                tenants.append(tenant)
        self._brand_group(tenants)
        self._summary(brands)

    # ------------------------------------------------------------------ setup

    def _setup_brand(self, brand: Brand) -> Tenant | None:
        self.stdout.write(f"\n### {brand.name} ({brand.slug})")
        tenant = self._resolve_tenant(brand)
        if tenant is None:
            if not self.apply:
                self.stdout.write(self.style.WARNING(f"  would create tenant {brand.name!r}"))
                self._print_plan(brand)
                return None
            tenant = self._create_tenant(brand)
        else:
            self.stdout.write(f"  tenant [{tenant.id}] {tenant.name!r} slug={tenant.slug!r}")

        event_types = self._programs(tenant)
        self._products(tenant, brand)
        for program, spec in ((RETAIL, retail_spec(brand.name)), (EVENT, event_spec(brand.name))):
            self._template(tenant, brand, program, spec, event_types.get(program))
        self._sync_products_sampled(tenant)
        self._photo_buckets(tenant)
        self._checkin_programs(tenant, event_types)
        self._tenant_settings(tenant)
        self._branding(tenant, brand)
        self._checkin_code(tenant, brand)
        return tenant

    def _print_plan(self, brand: Brand) -> None:
        self.stdout.write(f"  programs: {', '.join(PROGRAMS)}")
        self.stdout.write(f"  SKUs ({len(brand.skus)}):")
        for sku in brand.skus:
            self.stdout.write(f"    - {brand.product_type} — {sku.name}")
        for program, spec in ((RETAIL, retail_spec(brand.name)), (EVENT, event_spec(brand.name))):
            self.stdout.write(f"  template {template_name(brand, program)!r}")
            for section, fields in spec:
                tag = " (per brand)" if section in PER_BRAND_SECTIONS else " (shared)"
                self.stdout.write(f"    [{section}]{tag}: {len(fields)} field(s)")
        self.stdout.write(f"  standing code: would mint {brand.code_prefix}-XXXXXX")

    def _resolve_creator(self):
        User = get_user_model()
        creator = (
            User.objects.filter(is_superuser=True).order_by("id").first()
            or User.objects.order_by("id").first()
        )
        if creator is None:
            raise CommandError("No user available to own the created rows.")
        return creator

    def _resolve_tenant(self, brand: Brand) -> Tenant | None:
        for lookup in ("slug__iexact", "request_url_name__iexact"):
            rows = list(Tenant.objects.filter(**{lookup: brand.slug}).order_by("id")[:2])
            if len(rows) > 1:
                raise CommandError(f"{lookup}={brand.slug!r} matched several tenants.")
            if rows:
                return rows[0]
        return None

    def _create_tenant(self, brand: Brand) -> Tenant:
        creator = self.creator
        tenant = Tenant.objects.create(
            name=brand.name,
            slug=brand.slug,
            request_url_name=brand.slug,
            created_by=creator,
        )

        def _statuses(model_cls, templates, include_default: bool):
            for status in templates:
                payload = {
                    "name": status["name"],
                    "slug": status.get("slug") or slugify(status["name"]),
                    "tenant": tenant,
                    "created_by": creator,
                }
                if include_default:
                    payload["is_default"] = status["is_default"]
                model_cls.objects.create(**payload)

        _statuses(RequestStatus, DEFAULT_STATUS_TEMPLATES, True)
        _statuses(EventStatus, DEFAULT_STATUS_TEMPLATES, True)
        _statuses(JobStatus, DEFAULT_JOB_STATUS_TEMPLATES, False)
        _statuses(AttendanceStatus, DEFAULT_ATTENDANCE_STATUS_TEMPLATES, False)
        for rate_type in DEFAULT_RATE_TYPES:
            RateType.objects.create(name=rate_type, tenant=tenant, created_by=creator)
        for name in DEFAULT_FILE_RECAP_CATEGORIES:
            FileRecapCategory.objects.create(name=name, tenant_id=tenant.id, created_by=creator)
        for name in DEFAULT_TYPES_OF_GOOD:
            TypeOfGood.objects.create(name=name, tenant=tenant, created_by=creator)
        for skill in DEFAULT_SKILLS:
            if not Skill.objects.filter(name__iexact=skill).exists():
                Skill.objects.create(name=skill, created_by=creator)
        self.stdout.write(self.style.SUCCESS(f"  + created tenant [{tenant.id}] {tenant.name!r}"))
        return tenant

    # -------------------------------------------------------------- programs

    def _programs(self, tenant: Tenant) -> dict[str, EventType]:
        """Exactly Retail Sampling + Event Activation event & request types."""
        out: dict[str, EventType] = {}
        for old, new in EVENT_TYPE_RENAMES.items():
            legacy = EventType.objects.filter(tenant_id=tenant.id, name__iexact=old).first()
            if legacy and not EventType.objects.filter(tenant_id=tenant.id, name__iexact=new).exists():
                self.stdout.write(f"  event type {legacy.name!r} → {new!r}")
                if self.apply:
                    legacy.name = new
                    legacy.slug = slugify(new)
                    legacy.save(update_fields=["name", "slug"])

        for program in PROGRAMS:
            et = EventType.objects.filter(tenant_id=tenant.id, name__iexact=program).first()
            if et is None:
                self.stdout.write(f"  + event type {program!r}")
                if self.apply:
                    et = EventType.objects.create(
                        name=program,
                        slug=slugify(program),
                        tenant=tenant,
                        created_by=self.creator,
                        is_default=program == RETAIL,
                    )
            if et is not None:
                out[program] = et
            if not RequestType.objects.filter(tenant_id=tenant.id, name__iexact=program).exists():
                self.stdout.write(f"  + request type {program!r}")
                if self.apply:
                    RequestType.objects.create(name=program, tenant=tenant, created_by=self.creator)

        if self.apply and RETAIL in out:
            EventType.objects.filter(tenant_id=tenant.id, is_default=True).exclude(
                pk=out[RETAIL].pk
            ).update(is_default=False)
            if not out[RETAIL].is_default:
                out[RETAIL].is_default = True
                out[RETAIL].save(update_fields=["is_default"])

        wanted = {p.lower() for p in PROGRAMS}
        for et in EventType.objects.filter(tenant_id=tenant.id).order_by("id"):
            if (et.name or "").lower() in wanted:
                continue
            used = (
                Event.objects.filter(event_type=et).exists()
                or CustomRecapTemplate.objects.filter(event_type=et).exists()
            )
            self._retire(et, "event type", used)
        for rt in RequestType.objects.filter(tenant_id=tenant.id).order_by("id"):
            if (rt.name or "").lower() in wanted:
                continue
            self._retire(rt, "request type", Request.objects.filter(request_type=rt).exists())
        return out

    def _retire(self, row, label: str, used: bool) -> None:
        if used:
            self.stdout.write(self.style.WARNING(f"  kept {label} {row.name!r} (in use)"))
            return
        self.stdout.write(f"  - retire unused {label} {row.name!r}")
        if self.apply:
            row.delete()

    # -------------------------------------------------------------- catalog

    def _products(self, tenant: Tenant, brand: Brand) -> None:
        if not self.apply:
            existing = set(
                Product.objects.filter(tenant_id=tenant.id).values_list("name", flat=True)
            )
            for sku in brand.skus:
                mark = "=" if sku.name in existing else "+"
                self.stdout.write(f"  {mark} SKU {sku.name!r}")
            return
        product_type, _ = ProductType.objects.get_or_create(
            tenant=tenant, name=brand.product_type, defaults={"created_by": self.creator}
        )
        for sku in brand.skus:
            product = Product.objects.filter(tenant=tenant, name__iexact=sku.name).first()
            made = product is None
            if made:
                product = Product.objects.create(
                    tenant=tenant,
                    name=sku.name,
                    product_type=product_type,
                    created_by=self.creator,
                )
            elif product.product_type_id != product_type.id:
                product.product_type = product_type
                product.save(update_fields=["product_type", "updated_at"])
            note = ""
            if not product.image and not self.skip_images:
                note = self._attach_product_image(product, sku.image_url)
            self.stdout.write(
                f"  {'+' if made else '='} SKU [{product.id}] {sku.name!r}"
                f"{' · ' + note if note else ''}{'' if product.image else ' · NO IMAGE'}"
            )

    def _download(self, url: str) -> bytes | None:
        try:
            with httpx.Client(
                timeout=REQUEST_TIMEOUT,
                follow_redirects=True,
                headers={"User-Agent": USER_AGENT},
            ) as client:
                response = client.get(url)
                response.raise_for_status()
                time.sleep(0.5)
                return response.content or None
        except Exception as exc:  # noqa: BLE001
            logger.warning("brand group image download failed for %s: %s", url, exc)
            return None

    def _extension(self, url: str) -> str:
        path = urlparse(url).path
        if "." in path:
            ext = path.rsplit(".", 1)[-1].lower()
            if ext in {"png", "jpg", "jpeg", "webp", "gif"}:
                return f".{ext}"
        return ".png"

    def _attach_product_image(self, product: Product, url: str) -> str:
        blob = self._download(url)
        if not blob:
            return "IMAGE DOWNLOAD FAILED"
        product.image.save(f"{product.uuid}{self._extension(url)}", ContentFile(blob), save=False)
        product.save(update_fields=["image", "updated_at"])
        return f"image {len(blob) // 1024}KB"

    def _sync_products_sampled(self, tenant: Tenant) -> None:
        if not self.apply:
            return
        options = catalog_product_options(tenant)
        if options:
            CustomField.objects.filter(
                custom_recap_template__tenant_id=tenant.id,
                name__iexact=PRODUCTS_SAMPLED_FIELD,
            ).update(options=list(options))

    # ------------------------------------------------------------- templates

    def _field_type(self, kind: str):
        if kind in self.ft_cache:
            return self.ft_cache[kind]
        found = next(
            (
                ft
                for ft in CustomRecapFieldType.objects.all()
                if _match_field_type(kind, (ft.name or "").lower())
            ),
            None,
        )
        if found is None and self.apply:
            found = CustomRecapFieldType.objects.create(name=kind, created_by=self.creator)
        self.ft_cache[kind] = found
        return found

    def _template(
        self,
        tenant: Tenant,
        brand: Brand,
        program: str,
        spec: list[SectionSpec],
        event_type: EventType | None,
    ) -> None:
        name = template_name(brand, program)
        n_fields = sum(len(fields) for _, fields in spec)
        if not self.apply or event_type is None:
            self.stdout.write(f"  template {name!r}: {len(spec)} sections / {n_fields} fields")
            return
        with transaction.atomic():
            template, made = CustomRecapTemplate.objects.get_or_create(
                tenant_id=tenant.id,
                name=name,
                defaults={
                    "event_type": event_type,
                    "product_samples": False,
                    "sales_performance": False,
                    "layout": {},
                    "created_by": self.creator,
                },
            )
            layout = dict(template.layout or {})
            layout[CO_BRAND_SECTIONS_KEY] = list(PER_BRAND_SECTIONS)
            template.layout = layout
            template.event_type = event_type
            template.save(update_fields=["layout", "event_type"])

            keep: set[int] = set()
            for section_name, fields in spec:
                section, _ = RecapSection.objects.get_or_create(
                    tenant_id=tenant.id,
                    name=section_name,
                    defaults={"order": SECTION_ORDER[section_name], "created_by": self.creator},
                )
                for f_idx, (fname, kind, required, options) in enumerate(fields):
                    ft = self._field_type(kind)
                    row, created = CustomField.objects.get_or_create(
                        custom_recap_template=template,
                        recap_section=section,
                        name=fname,
                        defaults={
                            "custom_field_type": ft,
                            "required": required,
                            "options": list(options),
                            "order": f_idx,
                            "created_by": self.creator,
                        },
                    )
                    keep.add(row.id)
                    if created:
                        continue
                    row.custom_field_type = ft
                    row.required = required
                    row.order = f_idx
                    if fname != PRODUCTS_SAMPLED_FIELD:
                        row.options = list(options)
                    row.save()
            stale = CustomField.objects.filter(custom_recap_template=template).exclude(
                id__in=keep
            )
            n_stale = stale.count()
            if n_stale and not CustomRecap.objects.filter(custom_recap_template=template).exists():
                stale.delete()

            # resolve_template_for_event takes the lowest id on the event type.
            others = CustomRecapTemplate.objects.filter(
                tenant_id=tenant.id, event_type=event_type, id__lt=template.id
            )
            for other in others:
                if CustomRecap.objects.filter(custom_recap_template=other).exists():
                    self.stdout.write(
                        self.style.WARNING(
                            f"  older template {other.name!r} on {program} has recaps — left; "
                            "walk-up may resolve it first"
                        )
                    )
                else:
                    CustomField.objects.filter(custom_recap_template=other).delete()
                    other.delete()
        self.stdout.write(
            self.style.SUCCESS(
                f"  {'+' if made else '='} template [{template.id}] {name!r} "
                f"({len(spec)} sections / {n_fields} fields)"
            )
        )

    # ---------------------------------------------------------- walk-up/brand

    def _photo_buckets(self, tenant: Tenant) -> None:
        names = sorted({b["name"] for buckets in PHOTO_BUCKETS_BY_PROGRAM.values() for b in buckets})
        for name in names:
            if FileRecapCategory.objects.filter(tenant_id=tenant.id, name__iexact=name).exists():
                continue
            self.stdout.write(f"  + photo bucket {name!r}")
            if self.apply:
                FileRecapCategory.objects.create(name=name, tenant_id=tenant.id, created_by=self.creator)
        if tenant.checkin_photo_buckets != PHOTO_BUCKETS_BY_PROGRAM:
            self.stdout.write("  checkin_photo_buckets → per-program buckets")
            if self.apply:
                tenant.checkin_photo_buckets = PHOTO_BUCKETS_BY_PROGRAM
                tenant.save(update_fields=["checkin_photo_buckets"])

    def _checkin_programs(self, tenant: Tenant, event_types: dict[str, EventType]) -> None:
        if not self.apply or len(event_types) != len(PROGRAMS):
            self.stdout.write(f"  walk-up programs: {', '.join(PROGRAMS)}")
            return
        tenant.checkin_event_type = event_types[RETAIL]
        tenant.save(update_fields=["checkin_event_type"])
        tenant.checkin_event_types.set([event_types[p] for p in PROGRAMS])

    def _tenant_settings(self, tenant: Tenant) -> None:
        wanted = {
            "checkin_location_mode": Tenant.CHECKIN_LOCATION_ADDRESS,
            "insights_trend_series": Tenant.TREND_SERIES_SALES,
            "activation_mix_includes_walkups": True,
        }
        changed = [k for k, v in wanted.items() if getattr(tenant, k) != v]
        for key in changed:
            self.stdout.write(f"  {key}: {getattr(tenant, key)!r} → {wanted[key]!r}")
        if changed and self.apply:
            for key in changed:
                setattr(tenant, key, wanted[key])
            tenant.save(update_fields=changed)

    def _branding(self, tenant: Tenant, brand: Brand) -> None:
        variables = {"--color-primary": brand.primary_color}
        theme = TenantTheme.objects.filter(tenant=tenant, color_scheme="light").first()
        if theme is None or (theme.css_variables or {}).get("--color-primary") != brand.primary_color:
            self.stdout.write(f"  theme primary → {brand.primary_color}")
            if self.apply:
                current = (theme.css_variables if theme else None) or {}
                TenantTheme.objects.update_or_create(
                    tenant=tenant,
                    color_scheme="light",
                    defaults={"name": brand.slug, "css_variables": {**current, **variables}},
                )
        if tenant.image or self.skip_images:
            return
        self.stdout.write(f"  logo ← {urlparse(brand.logo_url).netloc}")
        if not self.apply:
            return
        blob = self._download(brand.logo_url)
        if not blob:
            self.stdout.write(self.style.WARNING("  LOGO DOWNLOAD FAILED"))
            return
        ext = self._extension(brand.logo_url)
        blob_name = f"tenants/images/{tenant.uuid}-logo{ext}"
        content_type = "image/jpeg" if ext in {".jpg", ".jpeg"} else f"image/{ext.lstrip('.')}"
        upload_bytes(blob_name, blob, content_type=content_type)
        tenant.image = blob_name
        tenant.save(update_fields=["image"])

    def _checkin_code(self, tenant: Tenant, brand: Brand) -> None:
        base = (
            getattr(settings, "PUBLIC_CHECKIN_BASE_URL", "") or "https://client.igniteproductions.co"
        ).rstrip("/")
        existing = (tenant.checkin_code or "").strip()
        if existing:
            self.stdout.write(f"  standing code {existing} (kept) → {base}/checkin/{existing}")
            return
        if not self.apply:
            self.stdout.write(f"  standing code: would mint {brand.code_prefix}-XXXXXX")
            return
        for _ in range(12):
            code = f"{brand.code_prefix}-" + "".join(secrets.choice(CODE_ALPHABET) for _ in range(6))
            if not Tenant.objects.filter(checkin_code__iexact=code).exists():
                tenant.checkin_code = code
                tenant.save(update_fields=["checkin_code"])
                self.stdout.write(self.style.SUCCESS(f"  + standing code {code} → {base}/checkin/{code}"))
                return
        raise CommandError("Couldn't mint a unique check-in code — try again.")

    # ------------------------------------------------------------------ group

    def _brand_group(self, tenants: list[Tenant]) -> None:
        group = BrandGroup.objects.filter(slug=GROUP_SLUG).first()
        self.stdout.write(f"\n### Brand group {GROUP_NAME!r}")
        if group is None:
            self.stdout.write(f"  + brand group {GROUP_SLUG!r}")
            if self.apply:
                group = BrandGroup.objects.create(name=GROUP_NAME, slug=GROUP_SLUG)
        for tenant in tenants:
            if group is not None and tenant.brand_group_id == group.id:
                self.stdout.write(f"  = {tenant.name}")
                continue
            if tenant.brand_group_id:
                self.stdout.write(
                    self.style.WARNING(f"  {tenant.name} is in another brand group — moving")
                )
            self.stdout.write(f"  + {tenant.name}")
            if self.apply and group is not None:
                tenant.brand_group = group
                tenant.save(update_fields=["brand_group"])

    def _summary(self, brands: list[Brand]) -> None:
        base = (
            getattr(settings, "PUBLIC_CHECKIN_BASE_URL", "") or "https://client.igniteproductions.co"
        ).rstrip("/")
        self.stdout.write("\n" + "=" * 68)
        for brand in brands:
            tenant = self._resolve_tenant(brand)
            if tenant is None:
                self.stdout.write(f"{brand.name}: (not created yet)")
                continue
            n_img = Product.objects.filter(tenant=tenant).exclude(image="").exclude(image__isnull=True).count()
            n_sku = Product.objects.filter(tenant=tenant).count()
            code = tenant.checkin_code or "(none)"
            self.stdout.write(
                f"{brand.name}: request {base}/spark-form/{tenant.request_url_name} · "
                f"walk-up {base}/checkin/{code} · SKUs {n_sku} ({n_img} with image)"
            )
        self.stdout.write("=" * 68)
        if not self.apply:
            self.stdout.write(self.style.WARNING("DRY-RUN — re-run with --apply to write."))
