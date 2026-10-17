"""Set up LYT (Drink Lyft): tenant, catalog, Event recap, standing check-in.

Public brand on drinklytofficial.com is LYT. Slug stays ``drink-lyft``.
The recap mirrors Liquid Death Event Activation (LD walk-up Event path):
same sections, question shapes, field types, location mode (address), and
photo buckets, with LYT labels and the LYT Product catalog as the only SKU
list. No Liquid Death product names or brand copy on this path.

Catalog SKUs are the six 12-packs on
https://drinklytofficial.com/collections/flavors
(Shopify product titles + variant SKUs). Images are copied into
``Product.image`` (GCS in prod) rather than hotlinked.

Recaps stay human-reviewed (walk-up leaves approved=False). Requests stay
Pending (``auto_approve_requests`` stays false). Idempotent: an existing
check-in code is never rotated.

DRY-RUN by default. Run via ``/internal/cron/setup-drink-lyft-checkin``
(or the "Setup LYT check-in" GitHub Action) against prod.
"""

from __future__ import annotations

import secrets
from urllib.parse import urlparse

import httpx
from django.conf import settings
from django.core.files.base import ContentFile
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.db.models import Q
from django.utils.text import slugify

from recaps.management.commands.setup_total_wireless_checkin import (
    _match_field_type,
)

TENANT_NAME = "LYT"
TENANT_SLUG = "drink-lyft"
TEMPLATE_NAME = "LYT-Event Activation"
CODE_PREFIX = "LYT-"
PROGRAM_NAME = "Event Activation"

# Exact identities only. Substring matches must not attach another tenant.
ALIAS_SLUGS = frozenset({TENANT_SLUG, "lyt"})
ALIAS_NAMES = frozenset({"lyt", "drink lyft", "drink lyt"})

# Field-for-field off Liquid Death Event Activation (DAOU seed + the LD
# engagement questions in test_campaign_pdf), with LYT labels. "tasting" is
# the corrected question; the legacy LD row spelled it "tasing".
EVENT_SPEC_FIELDS: list[tuple[str, list[tuple[str, str, bool]]]] = [
    (
        "Consumer Engagement",
        [
            (
                "How many consumers would be willing to purchase the product "
                "after tasting it?",
                "number",
                True,
            ),
            (
                "How many consumers that were engaged with knew about "
                "LYT product/brand?",
                "number",
                True,
            ),
            (
                "How many consumers had tried a LYT flavor before?",
                "number",
                True,
            ),
            ("How many TOTAL consumers did you sample?", "number", True),
        ],
    ),
    (
        "Feedback & Account Notes",
        [
            ("Demographics", "longtext", True),
            (
                "What were the top 5 frequently asked questions you received "
                "from consumers?",
                "longtext",
                True,
            ),
            ("Helpful feedback", "longtext", True),
        ],
    ),
]

SECTION_ORDER = {
    "Consumer Engagement": 0,
    "Feedback & Account Notes": 1,
    "Products Sampled": 3,
}

# Liquid Death Event Activation dropzones (address location mode).
CONSUMER_SAMPLING: dict = {
    "name": "Consumer Sampling Pictures",
    "helper": "please try to upload 8+",
    "min": 8,
}
PHOTO_BUCKETS: list[dict] = [
    {"name": "Activation Set Up"},
    CONSUMER_SAMPLING,
    {"name": "Expense Receipts (Parking)"},
]
PHOTO_BUCKETS_BY_PROGRAM: dict[str, list[dict]] = {
    PROGRAM_NAME: list(PHOTO_BUCKETS),
}
ALL_BUCKETS: list[dict] = list(PHOTO_BUCKETS)

# Collection order from /collections/flavors/products.json. Names are the
# storefront titles. No prices. No flavors that are not on that collection.
PRODUCTS: list[dict] = [
    {
        "name": "Island Flow 12-Pack",
        "product_type": "LYT",
        "sku": "LYT22Z12PIFCHT",
        "image_url": (
            "https://cdn.shopify.com/s/files/1/0696/6653/7519/files/"
            "LYT22ozIslandFlowBottleMock_PDP_072826sb-HR.jpg?v=1790616266"
        ),
    },
    {
        "name": "Grape Boost 12-Pack",
        "product_type": "LYT",
        "sku": "LYT22Z12PGBCHT",
        "image_url": (
            "https://cdn.shopify.com/s/files/1/0696/6653/7519/files/"
            "LYT22ozGrapeBoostBottleMock_PDP_072826sb-HR.jpg?v=1790616860"
        ),
    },
    {
        "name": "Cherry Tempo 12-Pack",
        "product_type": "LYT",
        "sku": "LYT22Z12PCYTCHT",
        "image_url": (
            "https://cdn.shopify.com/s/files/1/0696/6653/7519/files/"
            "LYT22ozCherryTempoBottleMock_PDP_072826sb-HR.png?v=1790616884"
        ),
    },
    {
        "name": "Orange Blast 12-Pack",
        "product_type": "LYT",
        "sku": "LYT22Z12POBCHT",
        "image_url": (
            "https://cdn.shopify.com/s/files/1/0696/6653/7519/files/"
            "LYT_22oz_Orange_Blast_Bottle_Mock_PDP_072826sb-HR.png?v=1790616770"
        ),
    },
    {
        "name": "Citrus Mist 12-Pack",
        "product_type": "LYT Zero",
        "sku": "LYT22Z12PZCMCHT",
        "image_url": (
            "https://cdn.shopify.com/s/files/1/0696/6653/7519/files/"
            "LYT_ZERO_22oz_Citrus_Mist_Bottle_Mock_PDP_072826sb-HR.jpg?v=1790616237"
        ),
    },
    {
        "name": "Cyclone Punch 12-Pack",
        "product_type": "LYT Zero",
        "sku": "LYT22Z12PZCPCHT",
        "image_url": (
            "https://cdn.shopify.com/s/files/1/0696/6653/7519/files/"
            "LYT_ZERO_22oz_Cyclone_Punch_Bottle_Mock_PDP_072826sb-HR.jpg"
            "?v=1790616213"
        ),
    },
]

REQUEST_TIMEOUT = 30.0
USER_AGENT = "Mozilla/5.0 (compatible; SparkBA-Onboarder/1.0)"


def build_spec(
    product_opts: list[str],
) -> list[tuple[str, list[tuple[str, str, bool, list[str]]]]]:
    """Assemble the event spec with catalog-backed Products Sampled options."""
    sections: list[tuple[str, list[tuple[str, str, bool, list[str]]]]] = []
    for section_name, fields in EVENT_SPEC_FIELDS:
        sections.append(
            (
                section_name,
                [(name, kind, req, []) for name, kind, req in fields],
            )
        )
    sections.append(
        (
            "Products Sampled",
            [("Products Sampled", "multiselect", True, list(product_opts))],
        )
    )
    return sections


def download_product_image(url: str) -> tuple[str, bytes]:
    """Fetch a storefront image. Returns ``(filename, bytes)``."""
    try:
        response = httpx.get(
            url,
            timeout=REQUEST_TIMEOUT,
            follow_redirects=True,
            headers={"User-Agent": USER_AGENT},
        )
        response.raise_for_status()
    except httpx.HTTPError as exc:
        raise CommandError(f"Could not download product image {url}: {exc}") from exc
    path = urlparse(url).path
    filename = (path.rsplit("/", 1)[-1] or "product.jpg").strip() or "product.jpg"
    if not response.content:
        raise CommandError(f"Product image {url} was empty.")
    return filename, response.content


def _needle_is_lyt(needle: str) -> bool:
    raw = (needle or "").strip().lower()
    if not raw:
        return True
    if raw in ALIAS_NAMES or raw in ALIAS_SLUGS:
        return True
    return slugify(raw) in ALIAS_SLUGS


class Command(BaseCommand):
    help = (
        "Set up LYT (drink-lyft): tenant, flavor catalog, Event Activation "
        "recap mirroring Liquid Death's event form, and a standing check-in "
        "(dry-run by default; --apply to write)."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--tenant",
            default=TENANT_SLUG,
            help=(f"Exact tenant slug or request_url_name. Default: {TENANT_SLUG!r}."),
        )
        parser.add_argument(
            "--template-name",
            dest="template_name",
            default=TEMPLATE_NAME,
            help=f"template name. Default: {TEMPLATE_NAME!r}.",
        )
        parser.add_argument(
            "--event-type",
            dest="event_type",
            default=None,
            help=f"event type name. Default: {PROGRAM_NAME!r}.",
        )
        parser.add_argument(
            "--prefix",
            dest="prefix",
            default="",
            help=(
                "brand prefix for a NEWLY minted code, e.g. 'LYT' -> "
                f"LYT-XXXXXX. Blank keeps {CODE_PREFIX!r}."
            ),
        )
        parser.add_argument(
            "--apply",
            action="store_true",
            help="actually write (omit for a dry-run that changes nothing).",
        )

    def handle(self, *args, **opts):
        apply = opts["apply"]
        creator = self._resolve_creator()
        tenant = self._resolve_or_create_tenant(opts["tenant"], creator, apply)
        template_name = opts["template_name"]

        self.stdout.write("=" * 68)
        if tenant is None:
            self.stdout.write(
                f"Tenant     : (would create {TENANT_NAME!r} / {TENANT_SLUG!r})"
            )
        else:
            self.stdout.write(
                f"Tenant     : [{tenant.id}] {tenant.name!r} (slug {tenant.slug!r})"
            )
        self.stdout.write(f"Template   : {template_name!r}")
        self.stdout.write(f"Created by : {getattr(creator, 'email', creator)!r}")
        self.stdout.write(
            f"Mode       : {'APPLY (writing)' if apply else 'DRY-RUN (no writes)'}"
        )
        self.stdout.write("=" * 68)
        self._print_catalog()

        if tenant is None:
            self._print_spec(build_spec([]))
            self.stdout.write("\n" + "=" * 68)
            self.stdout.write(
                self.style.WARNING(
                    f"DRY-RUN — would create tenant {TENANT_NAME!r}, "
                    f"{len(PRODUCTS)} catalog SKUs, "
                    f"the {TEMPLATE_NAME!r} recap, "
                    f"photo buckets, and a {CODE_PREFIX} standing link. "
                    "Re-run with --apply to write."
                )
            )
            return

        images = self._fetch_images(tenant, apply)
        if apply:
            with transaction.atomic():
                self._seed_products(tenant, creator, apply, images)
                spec = build_spec(self._product_options(tenant))
                self._seed_recap(tenant, template_name, spec, creator, apply, opts)
        else:
            self._seed_products(tenant, creator, apply, images)
            spec = build_spec(self._product_options(tenant))
            self._seed_recap(tenant, template_name, spec, creator, apply, opts)

    def _print_catalog(self) -> None:
        self.stdout.write("\nCatalog (storefront 12-packs):")
        for row in PRODUCTS:
            self.stdout.write(
                f"    - {row['product_type']}: {row['name']} sku={row['sku']}"
            )

    def _print_spec(self, spec) -> None:
        for section_name, fields in spec:
            s_idx = SECTION_ORDER[section_name]
            self.stdout.write(f"\n[{s_idx}] SECTION {section_name!r}")
            for fname, kind, required, options in fields:
                req = "REQUIRED" if required else "optional"
                extra = f" options={options!r}" if options else ""
                self.stdout.write(f"    - {fname!r}  [{kind}] {req}{extra}")
        self.stdout.write("\nPhoto buckets:")
        for bucket in PHOTO_BUCKETS:
            self.stdout.write(f"    - {bucket['name']!r}")
        self.stdout.write("\nLocation mode: 'address'")

    def _resolve_creator(self):
        from django.contrib.auth import get_user_model

        User = get_user_model()
        creator = (
            User.objects.filter(is_superuser=True).order_by("id").first()
            or User.objects.order_by("id").first()
        )
        if creator is None:
            raise CommandError("No user available to own the created rows.")
        return creator

    def _resolve_or_create_tenant(self, needle: str, creator, apply: bool):
        if not _needle_is_lyt(needle):
            raise CommandError(
                f"--tenant {needle!r} is not the LYT tenant. "
                f"Pass exact slug {TENANT_SLUG!r} "
                "(or exact request_url_name)."
            )

        found = self._find_existing_tenant()
        if found is not None:
            return found

        self.stdout.write(
            self.style.WARNING(
                f"No tenant matches {TENANT_SLUG!r} — "
                f"{'creating' if apply else 'would create'} "
                f"{TENANT_NAME!r} (slug {TENANT_SLUG!r})."
            )
        )
        if not apply:
            return None
        return self._create_tenant(creator)

    def _find_existing_tenant(self):
        """Exact slug, then exact request_url_name, then exact alias name.

        Never a substring. More than one distinct row is an error.
        """
        from tenants.models import Tenant

        by_id: dict[int, Tenant] = {}

        def _add(qs):
            for tenant in qs:
                by_id[tenant.id] = tenant

        _add(Tenant.objects.filter(slug__iexact=TENANT_SLUG).order_by("id"))
        if not by_id:
            _add(
                Tenant.objects.filter(request_url_name__iexact=TENANT_SLUG).order_by(
                    "id"
                )
            )
        if not by_id:
            _add(Tenant.objects.filter(slug__iexact="lyt").order_by("id"))
        if not by_id:
            name_q = Q()
            for alias in ALIAS_NAMES:
                name_q |= Q(name__iexact=alias)
            _add(Tenant.objects.filter(name_q).order_by("id"))

        if not by_id:
            return None
        rows = list(by_id.values())
        if len(rows) > 1:
            for tenant in rows:
                self.stdout.write(
                    f"  [{tenant.id}] name={tenant.name!r} "
                    f"slug={tenant.slug!r} "
                    f"request_url_name={tenant.request_url_name!r}"
                )
            raise CommandError(
                f"{len(rows)} tenants match LYT / {TENANT_SLUG!r}. "
                "Resolve the duplicate before setup."
            )
        return rows[0]

    def _create_tenant(self, creator):
        """Create the tenant the same way ``createTenant`` seeds a new brand."""
        from ambassadors.models import AttendanceStatus, Skill
        from events.models import EventStatus, EventType, RequestStatus, RequestType
        from jobs.models import RateType
        from jobs.models import Status as JobStatus
        from recaps.models import FileRecapCategory, TypeOfGood
        from tenants.models import Tenant
        from tenants.mutations import (
            DEFAULT_ATTENDANCE_STATUS_TEMPLATES,
            DEFAULT_EVENT_TYPES,
            DEFAULT_FILE_RECAP_CATEGORIES,
            DEFAULT_JOB_STATUS_TEMPLATES,
            DEFAULT_RATE_TYPES,
            DEFAULT_REQUEST_TYPES,
            DEFAULT_SKILLS,
            DEFAULT_STATUS_TEMPLATES,
            DEFAULT_TYPES_OF_GOOD,
        )

        tenant = Tenant.objects.create(
            name=TENANT_NAME,
            slug=TENANT_SLUG,
            request_url_name=TENANT_SLUG,
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

        for event_type in DEFAULT_EVENT_TYPES:
            EventType.objects.create(
                name=event_type["name"],
                slug=event_type.get("slug") or slugify(event_type["name"]),
                tenant=tenant,
                created_by=creator,
                is_default=event_type["is_default"],
            )
        for request_type in DEFAULT_REQUEST_TYPES:
            RequestType.objects.create(
                name=request_type, tenant=tenant, created_by=creator
            )
        for rate_type in DEFAULT_RATE_TYPES:
            RateType.objects.create(name=rate_type, tenant=tenant, created_by=creator)
        for recap_category in DEFAULT_FILE_RECAP_CATEGORIES:
            FileRecapCategory.objects.create(
                name=recap_category, tenant_id=tenant.id, created_by=creator
            )
        for type_of_good in DEFAULT_TYPES_OF_GOOD:
            TypeOfGood.objects.create(
                name=type_of_good, tenant=tenant, created_by=creator
            )
        for skill in DEFAULT_SKILLS:
            if not Skill.objects.filter(name__iexact=skill).exists():
                Skill.objects.create(name=skill, created_by=creator)

        self.stdout.write(
            self.style.SUCCESS(
                f"  Created tenant [{tenant.id}] {tenant.name!r} "
                f"with createTenant-style seeds."
            )
        )
        return tenant

    def _fetch_images(self, tenant, apply: bool) -> dict[str, tuple[str, bytes]]:
        if not apply:
            return {}
        from events.models import Product

        images: dict[str, tuple[str, bytes]] = {}
        for row in PRODUCTS:
            existing = Product.objects.filter(
                tenant_id=tenant.id, name__iexact=row["name"]
            ).first()
            if existing is not None and existing.image:
                self.stdout.write(f"  image exists for {row['name']!r} — skip download")
                continue
            filename, data = download_product_image(row["image_url"])
            images[row["name"]] = (filename, data)
        return images

    def _seed_products(self, tenant, creator, apply: bool, images: dict) -> None:
        from events.models import Product, ProductType

        self.stdout.write("\nProducts:")
        types: dict[str, object] = {}
        for row in PRODUCTS:
            type_name = row["product_type"]
            ptype = types.get(type_name)
            if ptype is None:
                ptype = ProductType.objects.filter(
                    tenant_id=tenant.id, name__iexact=type_name
                ).first()
                if ptype is None and apply:
                    ptype = ProductType.objects.create(
                        name=type_name, tenant=tenant, created_by=creator
                    )
                    self.stdout.write(f"  + product type {type_name!r}")
                elif ptype is None:
                    self.stdout.write(f"  + product type {type_name!r} — would create")
                types[type_name] = ptype

            existing = Product.objects.filter(
                tenant_id=tenant.id, name__iexact=row["name"]
            ).first()
            if existing:
                self.stdout.write(
                    f"    = {row['name']!r} sku={row['sku']} [{existing.id}]"
                )
                if apply and not existing.image and row["name"] in images:
                    self._save_image(existing, images[row["name"]])
                continue
            if not apply:
                self.stdout.write(
                    f"    + {row['name']!r} sku={row['sku']} — would create"
                )
                continue
            if ptype is None:
                raise CommandError(f"Missing product type {type_name!r}.")
            product = Product.objects.create(
                name=row["name"],
                description=f"Shopify SKU {row['sku']}",
                product_type=ptype,
                tenant=tenant,
                created_by=creator,
            )
            if row["name"] in images:
                self._save_image(product, images[row["name"]])
            self.stdout.write(f"    + {row['name']!r} sku={row['sku']} [{product.id}]")

        if apply and not (tenant.request_url_name or "").strip():
            tenant.request_url_name = TENANT_SLUG
            tenant.save(update_fields=["request_url_name"])
            self.stdout.write(f"  set request_url_name={TENANT_SLUG!r}")

    def _save_image(self, product, payload: tuple[str, bytes]) -> None:
        filename, data = payload
        product.image.save(filename, ContentFile(data), save=True)
        self.stdout.write(f"      image -> {product.image.name}")

    def _product_options(self, tenant) -> list[str]:
        from recaps.products_sampled import products_sampled_options_for_tenant

        opts = products_sampled_options_for_tenant(tenant)
        if opts:
            self.stdout.write(f"Products Sampled options: {len(opts)} from catalog")
        else:
            self.stdout.write(
                self.style.WARNING(
                    "Products Sampled options: catalog empty — "
                    "pills refresh from the catalog once products exist"
                )
            )
        return opts

    def _seed_recap(
        self, tenant, template_name, spec, creator, apply: bool, opts
    ) -> None:
        event_type = self._resolve_event_type(
            tenant, opts.get("event_type"), creator, apply
        )
        self.stdout.write(
            f"Event type : {getattr(event_type, 'name', None)!r} "
            f"(id {getattr(event_type, 'id', None)})"
        )
        if event_type is None:
            raise CommandError(
                f"Tenant {tenant.slug!r} has no event types — seed failed."
            )

        self._report_existing_templates(tenant, template_name)
        ft_cache: dict = {}
        for _, fields in spec:
            for _, kind, _, _ in fields:
                self._resolve_field_type(kind, creator, apply, ft_cache)

        self._upsert_template(
            tenant, template_name, event_type, spec, creator, apply, ft_cache
        )
        self._photo_buckets(tenant, creator, apply)
        self._pin_event_type(tenant, event_type, apply)
        self._fold_extra_templates(
            tenant, event_type, template_name, spec, creator, apply, ft_cache
        )
        self._location_mode(tenant, apply)
        self._checkin_code(tenant, apply, opts.get("prefix") or "")

    def _resolve_event_type(self, tenant, hint: str | None, creator, apply: bool):
        from events.models import EventType

        wanted = (hint or PROGRAM_NAME).strip()
        match = EventType.objects.filter(
            tenant_id=tenant.id, name__iexact=wanted
        ).first()
        if match:
            return match
        if not apply:
            self.stdout.write(f"  would create event type {wanted!r}")
            return EventType(name=wanted, tenant=tenant, id=0)
        et = EventType.objects.create(
            name=wanted,
            slug=slugify(wanted),
            tenant=tenant,
            created_by=creator,
            is_default=False,
        )
        self.stdout.write(f"  + event type {wanted!r} [{et.id}]")
        return et

    def _resolve_field_type(self, kind: str, creator, apply: bool, cache: dict):
        if kind in cache:
            return cache[kind]
        from recaps.models import CustomRecapFieldType

        existing = None
        for ft in CustomRecapFieldType.objects.all():
            if _match_field_type(kind, (ft.name or "").lower()):
                existing = ft
                break
        if existing is None:
            if apply:
                existing = CustomRecapFieldType.objects.create(
                    name=kind, created_by=creator
                )
                self.stdout.write(f"    (created field type {kind!r})")
            else:
                existing = f"<would-create '{kind}'>"
        cache[kind] = existing
        return existing

    def _report_existing_templates(self, tenant, template_name: str) -> None:
        from recaps.models import CustomField, CustomRecapTemplate

        rows = list(
            CustomRecapTemplate.objects.filter(tenant_id=tenant.id).order_by("id")
        )
        self.stdout.write("\nExisting templates on this tenant:")
        if not rows:
            self.stdout.write("  (none — this will be the first)")
            return
        for template in rows:
            n_fields = CustomField.objects.filter(
                custom_recap_template=template
            ).count()
            same = (
                " <-- SAME NAME, will be reused"
                if template.name == template_name
                else ""
            )
            self.stdout.write(
                f"  [{template.id}] {template.name!r} — {n_fields} field(s), "
                f"event_type={getattr(template.event_type, 'name', None)!r}{same}"
            )

    def _upsert_template(
        self,
        tenant,
        template_name: str,
        event_type,
        spec,
        creator,
        apply: bool,
        ft_cache: dict,
    ) -> None:
        from recaps.models import CustomField, CustomRecapTemplate, RecapSection

        created = {"sections": 0, "fields": 0}
        updated = {"sections": 0, "fields": 0}
        template = None
        if apply:
            template, made = CustomRecapTemplate.objects.get_or_create(
                tenant_id=tenant.id,
                name=template_name,
                defaults={
                    "event_type": event_type,
                    "product_samples": True,
                    "sales_performance": False,
                    "layout": {},
                    "created_by": creator,
                },
            )
            changed_tpl = []
            if template.event_type_id != event_type.id:
                template.event_type = event_type
                changed_tpl.append("event_type")
            if template.product_samples is not True:
                template.product_samples = True
                changed_tpl.append("product_samples")
            if changed_tpl:
                template.save(update_fields=changed_tpl)
            self.stdout.write(
                f"\nTemplate {'CREATED' if made else 'exists'} "
                f"{template_name!r} (id {template.id}, uuid {template.uuid}, "
                f"event_type={event_type.name!r})"
            )
        else:
            self.stdout.write(f"\nTemplate {template_name!r} ({event_type.name})")

        for section_name, fields in spec:
            s_idx = SECTION_ORDER[section_name]
            self.stdout.write(f"\n[{s_idx}] SECTION {section_name!r}")
            section = None
            if apply:
                section, made = RecapSection.objects.get_or_create(
                    tenant_id=tenant.id,
                    name=section_name,
                    defaults={"order": s_idx, "created_by": creator},
                )
                if made:
                    created["sections"] += 1
                elif section.order != s_idx:
                    section.order = s_idx
                    section.save(update_fields=["order", "updated_at"])
                    updated["sections"] += 1

            for f_idx, (fname, kind, required, options) in enumerate(fields):
                ft = ft_cache[kind]
                req = "REQUIRED" if required else "optional"
                extra = f" options={options!r}" if options else ""
                self.stdout.write(f"    - {fname!r}  [{kind}] {req}{extra}")
                if not apply:
                    continue
                field, made = CustomField.objects.get_or_create(
                    custom_recap_template=template,
                    recap_section=section,
                    name=fname,
                    defaults={
                        "custom_field_type": ft,
                        "required": required,
                        "options": list(options),
                        "order": f_idx,
                        "created_by": creator,
                    },
                )
                if made:
                    created["fields"] += 1
                    continue
                changed = []
                if field.custom_field_type_id != ft.id:
                    field.custom_field_type = ft
                    changed.append("custom_field_type")
                if field.required != required:
                    field.required = required
                    changed.append("required")
                if field.order != f_idx:
                    field.order = f_idx
                    changed.append("order")
                if list(field.options or []) != list(options):
                    field.options = list(options)
                    changed.append("options")
                if changed:
                    changed.append("updated_at")
                    field.save(update_fields=changed)
                    updated["fields"] += 1

        self.stdout.write("\n" + "=" * 68)
        if apply:
            self.stdout.write(
                self.style.SUCCESS(
                    f"APPLIED — {template_name!r} {template.uuid} · "
                    f"sections +{created['sections']}/~{updated['sections']} · "
                    f"fields +{created['fields']}/~{updated['fields']}."
                )
            )
        else:
            total_fields = sum(len(f) for _, f in spec)
            self.stdout.write(
                self.style.WARNING(
                    f"DRY-RUN — would create/reconcile {len(spec)} sections + "
                    f"{total_fields} fields on {template_name!r}."
                )
            )

    def _photo_buckets(self, tenant, creator, apply: bool) -> None:
        from recaps.models import FileRecapCategory

        self.stdout.write("\nPhoto buckets:")
        for spec in ALL_BUCKETS:
            name = spec["name"]
            existing = FileRecapCategory.objects.filter(
                tenant_id=tenant.id, name__iexact=name
            ).first()
            if existing:
                self.stdout.write(f"    = {name!r} — [{existing.id}] already present")
            elif apply:
                cat = FileRecapCategory.objects.create(
                    name=name, tenant_id=tenant.id, created_by=creator
                )
                self.stdout.write(f"    + {name!r} — created [{cat.id}]")
            else:
                self.stdout.write(f"    + {name!r} — would be created")

        wanted = PHOTO_BUCKETS_BY_PROGRAM
        current = getattr(tenant, "checkin_photo_buckets", None)
        if current == wanted:
            self.stdout.write("  checkin_photo_buckets already set — left as-is.")
            return
        if not apply:
            self.stdout.write(
                self.style.WARNING("  DRY-RUN — would set checkin_photo_buckets")
            )
            return
        tenant.checkin_photo_buckets = wanted
        tenant.save(update_fields=["checkin_photo_buckets"])
        self.stdout.write(self.style.SUCCESS("  checkin_photo_buckets set."))

    def _pin_event_type(self, tenant, event_type, apply: bool) -> None:
        if event_type is None or not getattr(event_type, "id", None):
            return
        current = getattr(tenant, "checkin_event_type_id", None)
        already = current == event_type.id
        if already:
            self.stdout.write(
                f"\ncheckin_event_type already pinned to {event_type.name!r}."
            )
        elif not apply:
            self.stdout.write(
                self.style.WARNING(
                    f"\nDRY-RUN — would pin checkin_event_type="
                    f"{event_type.name!r} (id {event_type.id})"
                )
            )
        else:
            tenant.checkin_event_type = event_type
            tenant.save(update_fields=["checkin_event_type"])
            self.stdout.write(
                self.style.SUCCESS(
                    f"\nPinned checkin_event_type={event_type.name!r} "
                    f"(id {event_type.id})"
                )
            )
        if apply:
            tenant.checkin_event_types.set([event_type])
            if not event_type.is_default:
                from events.models import EventType

                EventType.objects.filter(tenant_id=tenant.id, is_default=True).exclude(
                    pk=event_type.pk
                ).update(is_default=False)
                event_type.is_default = True
                event_type.save(update_fields=["is_default"])

    def _delete_template(self, template) -> None:
        from recaps.models import CustomField

        CustomField.objects.filter(custom_recap_template=template).delete()
        template.delete()

    def _fold_extra_templates(
        self,
        tenant,
        event_type,
        keep_name: str,
        spec,
        creator,
        apply: bool,
        ft_cache: dict,
    ) -> None:
        """Leave one named template on the event type so walk-up hits it."""
        from recaps.models import CustomRecap, CustomRecapTemplate

        if not getattr(event_type, "id", None):
            return
        tpls = list(
            CustomRecapTemplate.objects.filter(
                tenant_id=tenant.id, event_type=event_type
            ).order_by("id")
        )
        keepers = [t for t in tpls if t.name == keep_name]
        extras = [t for t in tpls if t.name != keep_name]
        if not extras:
            return

        self.stdout.write(f"\nExtra templates on {event_type.name!r}:")
        reupsert = False
        for extra in extras:
            n = CustomRecap.objects.filter(custom_recap_template=extra).count()
            keeper = keepers[0] if keepers else None
            n_keep = (
                CustomRecap.objects.filter(custom_recap_template=keeper).count()
                if keeper
                else 0
            )
            if n == 0:
                msg = f"    leftover {extra.name!r} (id {extra.id}, 0 recaps)"
                if not apply:
                    self.stdout.write(self.style.WARNING(f"{msg} — would delete"))
                    continue
                self._delete_template(extra)
                self.stdout.write(self.style.SUCCESS(f"{msg} — deleted"))
                continue
            if keeper and n_keep == 0:
                msg = (
                    f"    leftover {extra.name!r} (id {extra.id}, {n} recap(s)); "
                    f"empty {keep_name!r} would lose walk-up (order_by id)"
                )
                if not apply:
                    self.stdout.write(
                        self.style.WARNING(
                            f"{msg} — would drop empty {keep_name!r} and rename leftover"
                        )
                    )
                    continue
                self._delete_template(keeper)
                extra.name = keep_name
                extra.save(update_fields=["name"])
                keepers = [extra]
                reupsert = True
                self.stdout.write(
                    self.style.SUCCESS(f"{msg} — renamed leftover to {keep_name!r}")
                )
                continue
            self.stdout.write(
                self.style.WARNING(
                    f"    leftover {extra.name!r} (id {extra.id}, {n} recap(s)) AND "
                    f"{keep_name!r} also has recaps — left both"
                )
            )
        if apply and reupsert:
            self._upsert_template(
                tenant, keep_name, event_type, spec, creator, apply, ft_cache
            )

    def _location_mode(self, tenant, apply: bool) -> None:
        from tenants.models import Tenant

        wanted = Tenant.CHECKIN_LOCATION_ADDRESS
        current = tenant.checkin_location_mode
        self.stdout.write(f"\nLocation mode: {current!r}")
        if current == wanted:
            return
        if not apply:
            self.stdout.write(
                self.style.WARNING(f"DRY-RUN — would set location mode to {wanted!r}")
            )
            return
        tenant.checkin_location_mode = wanted
        tenant.save(update_fields=["checkin_location_mode"])
        self.stdout.write(self.style.SUCCESS(f"Location mode set to {wanted!r}"))

    def _checkin_code(self, tenant, apply: bool, prefix: str = "") -> None:
        from tenants.models import Tenant

        raw = (prefix or "").strip().upper().rstrip("-")
        cleaned = "".join(ch for ch in raw if ch.isalnum())
        if raw and not 1 <= len(cleaned) <= 4:
            raise CommandError("--prefix should be 1-4 letters/digits, e.g. LYT.")
        code_prefix = f"{cleaned}-" if cleaned else CODE_PREFIX

        alphabet = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"
        base = (
            getattr(settings, "PUBLIC_CHECKIN_BASE_URL", "")
            or "https://client.igniteproductions.co"
        ).rstrip("/")

        self.stdout.write("\n" + "=" * 68)
        existing = (getattr(tenant, "checkin_code", "") or "").strip()
        if existing:
            self.stdout.write(
                f"Check-in code already set: {existing}\n"
                f"  Link: {base}/checkin/{existing}\n"
                "  (left as-is — rotating it would break every copy already shared)"
            )
            return

        code = None
        for _ in range(12):
            candidate = code_prefix + "".join(
                secrets.choice(alphabet) for _ in range(6)
            )
            if not Tenant.objects.filter(checkin_code__iexact=candidate).exists():
                code = candidate
                break
        if code is None:
            raise CommandError("Couldn't mint a unique check-in code — try again.")

        if not apply:
            self.stdout.write(
                self.style.WARNING(
                    f"DRY-RUN — would set checkin_code={code}\n"
                    f"  Link would be: {base}/checkin/{code}"
                )
            )
            return

        tenant.checkin_code = code
        tenant.save(update_fields=["checkin_code"])
        self.stdout.write(
            self.style.SUCCESS(
                f"Check-in code set: {code}\n  Link: {base}/checkin/{code}"
            )
        )
        self.stdout.write(
            f"  Request form: {base}/spark-form/{tenant.request_url_name or TENANT_SLUG}"
        )
