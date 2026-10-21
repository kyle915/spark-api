"""Set up Pivot Energy Drink: tenant + walk-up recaps + standing check-in.

Slug is the existing ``pivot-energy-drink`` tenant (exact slug only).

Event Activation stays the festival form already live on this brand:
- Template: "Pivot Energy Drink · Event Activation Recap"
- Sections: Event Details, Sampling Counts, Expenses, Feedback
- Photo buckets: Consumer Sampling Pictures, Expense Receipts
- Pinned default program on the standing link

Retail Sampling is added beside it for a store demo. It keeps that same
field shape (samples, mileage, card spend, expense receipts) with store
wording, plus the tenant Product catalog as the only SKU list. Retail
copy says Pivot. No SKU images are downloaded.

Standing walk-up code is left alone. ``PE-Y7VPYC`` is never reminted.
Recaps stay human-reviewed (approved=False until admin approve).

DRY-RUN by default. Run via ``/internal/cron/setup-pivot-energy-checkin`` (or
the "Setup Pivot Energy check-in" GitHub Action) against prod with apply=true.
"""

from __future__ import annotations

import secrets

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils.text import slugify

from recaps.management.commands.setup_total_wireless_checkin import (
    _match_field_type,
)
from recaps.products_sampled import PRODUCTS_SAMPLED_FIELD

TENANT_NAME = "Pivot Energy Drink"
TENANT_SLUG = "pivot-energy-drink"
TEMPLATE_NAME = "Pivot Energy Drink · Event Activation Recap"
RETAIL_TEMPLATE_NAME = "Pivot · Retail Sampling Recap"
CODE_PREFIX = "PE-"
# Already shared with the field team. Never mint a replacement.
STANDING_CODE = "PE-Y7VPYC"
PROGRAM_NAME = "Event Activation"
RETAIL_PROGRAM = "Retail Sampling"
PRODUCT_TYPE_NAME = "Pivot"
# The only SKU list. Recap options are read back from these Product rows.
SKU_NAMES = (
    "Blue Razz",
    "Pink Lemonade",
    "Melon Rush",
    "Going Grape",
)

# White Claw Surge festival form with Pivot renames + mileage/receipts.
# No other-brand copy on this path.
SPEC: list[tuple[str, list[tuple[str, str, bool, list[str]]]]] = [
    (
        "Event Details",
        [
            ("Event Location", "text", True, []),
        ],
    ),
    (
        "Sampling Counts",
        [
            ("Total samples distributed", "number", True, []),
            ("Estimated foot traffic / impressions", "number", True, []),
        ],
    ),
    (
        "Expenses",
        [
            ("Mileage", "number", True, []),
            ("Total spend on the card", "number", False, []),
        ],
    ),
    (
        "Feedback",
        [
            (
                "Key highlight (1–2 sentences) / What worked well & What didn't?",
                "longtext",
                True,
                [],
            ),
            (
                "What were some consumer comments that you heard?",
                "longtext",
                True,
                [],
            ),
            (
                "What percent of consumer had heard of or tried Pivot Energy Drink before?",
                "text",
                True,
                [],
            ),
        ],
    ),
]

# Store-demo twin of SPEC. Event location becomes the store, foot traffic
# drops the impressions wording, and the awareness question says Pivot.
# Mileage, card spend, and sample counts stay. Products Sampled options stay
# empty here — apply fills them from Product rows.
RETAIL_SPEC: list[tuple[str, list[tuple[str, str, bool, list[str]]]]] = [
    (
        "Store Details",
        [
            ("Where are you working (store)?", "text", True, []),
        ],
    ),
    (
        "Demo Counts",
        [
            ("Total samples distributed", "number", True, []),
            ("Estimated store foot traffic", "number", True, []),
        ],
    ),
    (
        "Products",
        [
            (PRODUCTS_SAMPLED_FIELD, "multiselect", True, []),
        ],
    ),
    (
        "Card & Mileage",
        [
            ("Mileage", "number", True, []),
            ("Total spend on the card", "number", False, []),
        ],
    ),
    (
        "Demo Feedback",
        [
            (
                "Key highlight (1–2 sentences) / What worked well & What didn't?",
                "longtext",
                True,
                [],
            ),
            (
                "What were some consumer comments that you heard?",
                "longtext",
                True,
                [],
            ),
            (
                "What percent of consumers had heard of or tried Pivot before?",
                "text",
                True,
                [],
            ),
        ],
    ),
]

SECTION_ORDER = {
    "Event Details": 0,
    "Sampling Counts": 1,
    "Expenses": 2,
    "Feedback": 3,
    "Store Details": 0,
    "Demo Counts": 1,
    "Products": 2,
    "Card & Mileage": 3,
    "Demo Feedback": 4,
}

PHOTO_BUCKETS: list[dict] = [
    {"name": "Consumer Sampling Pictures"},
    {"name": "Expense Receipts"},
]

# Same multi-upload buckets as Event Activation (library and camera).
PHOTO_BUCKETS_BY_PROGRAM: dict[str, list[dict]] = {
    PROGRAM_NAME: list(PHOTO_BUCKETS),
    RETAIL_PROGRAM: list(PHOTO_BUCKETS),
}

ALL_BUCKETS: list[dict] = list(PHOTO_BUCKETS)

KEEP_PROGRAMS = {PROGRAM_NAME.lower(), RETAIL_PROGRAM.lower()}


class Command(BaseCommand):
    help = (
        "Set up Pivot Energy Drink: tenant (if missing), Event Activation and "
        "Retail Sampling recap templates, photo buckets, catalog SKUs, and the "
        "standing check-in link (dry-run by default; --apply to write)."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--tenant",
            default="pivot-energy-drink",
            help=(
                "Exact tenant slug. Must be 'pivot-energy-drink'. "
                "A substring such as 'pivot' is rejected."
            ),
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
            help=f"event type name substring. Default: {PROGRAM_NAME!r}.",
        )
        parser.add_argument(
            "--prefix",
            dest="prefix",
            default="",
            help=(
                "brand prefix for a NEWLY minted code, e.g. 'PE' -> "
                f"PE-XXXXXX. Blank keeps {CODE_PREFIX!r}."
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
        self.stdout.write(f"Retail     : {RETAIL_TEMPLATE_NAME!r}")
        self.stdout.write(
            "SKUs       : " + ", ".join(SKU_NAMES) + " (no image download)"
        )
        self.stdout.write(f"Created by : {getattr(creator, 'email', creator)!r}")
        self.stdout.write(
            f"Mode       : {'APPLY (writing)' if apply else 'DRY-RUN (no writes)'}"
        )
        self.stdout.write("=" * 68)

        if tenant is None:
            self._print_spec()
            self.stdout.write("\n" + "=" * 68)
            self.stdout.write(
                self.style.WARNING(
                    f"DRY-RUN — would create tenant {TENANT_NAME!r}, "
                    f"Event Activation ({len(SPEC)} sections) and "
                    f"Retail Sampling ({len(RETAIL_SPEC)} sections), "
                    f"{len(SKU_NAMES)} SKUs, photo buckets, and a {CODE_PREFIX} "
                    "standing link. Re-run with --apply to write."
                )
            )
            return

        event_type = self._resolve_event_type(
            tenant, opts.get("event_type"), creator, apply
        )
        retail_type = self._resolve_retail_type(tenant, creator, apply)
        self.stdout.write(
            f"Event type : {getattr(event_type, 'name', None)!r} "
            f"(id {getattr(event_type, 'id', None)})"
        )
        self.stdout.write(
            f"Retail type: {getattr(retail_type, 'name', None)!r} "
            f"(id {getattr(retail_type, 'id', None)})"
        )
        if event_type is None or retail_type is None:
            raise CommandError(
                f"Tenant {tenant.slug!r} is missing a walk-up program — seed failed."
            )

        self._report_existing_templates(tenant, template_name)

        ft_cache: dict = {}
        for spec in (SPEC, RETAIL_SPEC):
            for _, fields in spec:
                for _, kind, _, _ in fields:
                    self._resolve_field_type(kind, creator, apply, ft_cache)

        if apply:
            with transaction.atomic():
                self._upsert_template(
                    tenant, template_name, event_type, SPEC, creator, apply, ft_cache
                )
                self._upsert_template(
                    tenant,
                    RETAIL_TEMPLATE_NAME,
                    retail_type,
                    RETAIL_SPEC,
                    creator,
                    apply,
                    ft_cache,
                )
                self._ensure_skus(tenant, creator, apply)
                self._sync_products_sampled(tenant, apply)
        else:
            self._upsert_template(
                tenant, template_name, event_type, SPEC, creator, apply, ft_cache
            )
            self._upsert_template(
                tenant,
                RETAIL_TEMPLATE_NAME,
                retail_type,
                RETAIL_SPEC,
                creator,
                apply,
                ft_cache,
            )
            self._ensure_skus(tenant, creator, apply)

        self._photo_buckets(tenant, creator, apply)
        self._pin_programs(tenant, event_type, retail_type, apply)
        self._retire_other_programs(tenant, event_type, retail_type, apply)
        self._fold_extra_templates(
            tenant, event_type, template_name, SPEC, creator, apply, ft_cache
        )
        if getattr(retail_type, "id", None):
            self._fold_extra_templates(
                tenant,
                retail_type,
                RETAIL_TEMPLATE_NAME,
                RETAIL_SPEC,
                creator,
                apply,
                ft_cache,
            )
        self._location_mode(tenant, apply)
        self._checkin_code(tenant, apply, opts.get("prefix"))

    def _print_spec(self) -> None:
        self.stdout.write(f"\nEvent Activation template {TEMPLATE_NAME!r}")
        self._print_fields(SPEC)
        self.stdout.write(f"\nRetail Sampling template {RETAIL_TEMPLATE_NAME!r}")
        self._print_fields(RETAIL_SPEC)
        self.stdout.write("\nPhoto buckets:")
        for program, buckets in PHOTO_BUCKETS_BY_PROGRAM.items():
            names = ", ".join(bucket["name"] for bucket in buckets)
            self.stdout.write(f"    {program}: {names}")
        self.stdout.write("\nSKUs (catalog only, no images):")
        for name in SKU_NAMES:
            self.stdout.write(f"    - {PRODUCT_TYPE_NAME} — {name}")

    def _print_fields(self, spec) -> None:
        for s_idx, (section_name, fields) in enumerate(spec):
            self.stdout.write(f"\n[{s_idx}] SECTION {section_name!r}")
            for fname, kind, required, options in fields:
                req = "REQUIRED" if required else "optional"
                extra = f" options={options!r}" if options else ""
                self.stdout.write(f"    - {fname!r}  [{kind}] {req}{extra}")

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
        from tenants.models import Tenant

        # Exact slug only. "pivot" must not select or create another tenant.
        needle_slug = slugify((needle or "").strip()) or TENANT_SLUG
        if needle_slug != TENANT_SLUG:
            raise CommandError(
                f"--tenant must be the exact slug {TENANT_SLUG!r} (got {needle!r})."
            )

        exact = list(Tenant.objects.filter(slug__iexact=TENANT_SLUG).order_by("id"))
        if len(exact) > 1:
            for t in exact:
                self.stdout.write(f"  [{t.id}] name={t.name!r} slug={t.slug!r}")
            raise CommandError(f"slug {TENANT_SLUG!r} matched {len(exact)} tenants.")
        if len(exact) == 1:
            return exact[0]

        self.stdout.write(
            self.style.WARNING(
                f"No tenant matches {needle!r} — "
                f"{'creating' if apply else 'would create'} "
                f"{TENANT_NAME!r} (slug {TENANT_SLUG!r})."
            )
        )
        if not apply:
            return None
        return self._create_tenant(creator)

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

    def _resolve_event_type(self, tenant, hint: str | None, creator, apply: bool):
        from events.models import EventType
        from tenants.mutations import DEFAULT_EVENT_TYPES

        qs = EventType.objects.filter(tenant_id=tenant.id).order_by("id")
        if not qs.exists() and apply:
            for event_type in DEFAULT_EVENT_TYPES:
                EventType.objects.create(
                    name=event_type["name"],
                    slug=event_type.get("slug") or slugify(event_type["name"]),
                    tenant=tenant,
                    created_by=creator,
                    is_default=event_type["is_default"],
                )
            qs = EventType.objects.filter(tenant_id=tenant.id).order_by("id")

        wanted = (hint or PROGRAM_NAME).strip()
        match = (
            qs.filter(name__iexact=wanted).first()
            or qs.filter(name__icontains=wanted).first()
        )
        if match:
            return match

        # DEFAULT_EVENT_TYPES has Event / Retail / On-Premise — not Event
        # Activation. Create the program when missing (Brand Revolution-style).
        if not apply:
            self.stdout.write(f"  would create event type {PROGRAM_NAME!r}")
            return EventType(name=PROGRAM_NAME, tenant=tenant, id=0)

        et = EventType.objects.create(
            name=PROGRAM_NAME,
            slug=slugify(PROGRAM_NAME),
            tenant=tenant,
            created_by=creator,
            is_default=True,
        )
        self.stdout.write(f"  + event type {PROGRAM_NAME!r} [{et.id}]")
        return et

    def _resolve_retail_type(self, tenant, creator, apply: bool):
        """Find Retail Sampling by exact name, or create it. Never remint Event."""
        from events.models import EventType

        match = (
            EventType.objects.filter(tenant_id=tenant.id, name__iexact=RETAIL_PROGRAM)
            .order_by("id")
            .first()
        )
        if match:
            return match
        if not apply:
            self.stdout.write(f"  would create event type {RETAIL_PROGRAM!r}")
            return EventType(name=RETAIL_PROGRAM, tenant=tenant, id=0)

        et = EventType.objects.create(
            name=RETAIL_PROGRAM,
            slug=slugify(RETAIL_PROGRAM),
            tenant=tenant,
            created_by=creator,
            is_default=False,
        )
        self.stdout.write(f"  + event type {RETAIL_PROGRAM!r} [{et.id}]")
        return et

    def _ensure_skus(self, tenant, creator, apply: bool) -> None:
        """Seed exactly the four Pivot flavors. Drop unused extras."""
        from events.models import Product, ProductType

        self.stdout.write(f"\nCatalog ({PRODUCT_TYPE_NAME}):")
        if not apply:
            existing = {
                (name or "").strip().lower()
                for name in Product.objects.filter(tenant_id=tenant.id).values_list(
                    "name", flat=True
                )
            }
            for name in SKU_NAMES:
                mark = "=" if name.lower() in existing else "+"
                self.stdout.write(f"  {mark} SKU {name!r} (no image)")
            extras = existing - {name.lower() for name in SKU_NAMES}
            for name in sorted(extras):
                self.stdout.write(f"  - would retire extra SKU {name!r}")
            return

        product_type, _ = ProductType.objects.get_or_create(
            tenant=tenant,
            name=PRODUCT_TYPE_NAME,
            defaults={"created_by": creator},
        )
        keep = {name.lower() for name in SKU_NAMES}
        for name in SKU_NAMES:
            product = Product.objects.filter(
                tenant_id=tenant.id, name__iexact=name
            ).first()
            if product is None:
                Product.objects.create(
                    tenant=tenant,
                    name=name,
                    product_type=product_type,
                    created_by=creator,
                )
                self.stdout.write(f"  + SKU {name!r}")
                continue
            if product.product_type_id != product_type.id:
                product.product_type = product_type
                product.save(update_fields=["product_type", "updated_at"])
            self.stdout.write(f"  = SKU [{product.id}] {product.name!r}")
        for extra in Product.objects.filter(tenant_id=tenant.id).order_by("id"):
            if (extra.name or "").strip().lower() in keep:
                continue
            if self._product_in_use(extra):
                self.stdout.write(
                    self.style.WARNING(
                        f"  kept extra SKU {extra.name!r} (referenced by recaps or requests)"
                    )
                )
                continue
            label = extra.name
            extra.delete()
            self.stdout.write(f"  - retired extra SKU {label!r}")

    def _product_in_use(self, product) -> bool:
        from events.models import RequestProduct
        from recaps.models import (
            CustomRecapProductSample,
            CustomRecapSalePerformance,
            ProductSamples,
            SalesPerformance,
        )

        return bool(
            RequestProduct.objects.filter(product=product).exists()
            or product.co_brand_requests.exists()
            or ProductSamples.objects.filter(product=product).exists()
            or CustomRecapProductSample.objects.filter(product=product).exists()
            or SalesPerformance.objects.filter(product=product).exists()
            or CustomRecapSalePerformance.objects.filter(product=product).exists()
        )

    def _sku_option_labels(self, tenant) -> list[str]:
        """Catalog labels for the four SKUs, in Kyle's order.

        Built from Product rows (same ``Type — Name`` shape as the live
        catalog picker). The spec does not carry a second copy of the names.
        """
        from events.event_confirmations import PRODUCT_CATEGORY_SEPARATOR
        from events.models import Product

        products = {
            (row.name or "").strip().lower(): row
            for row in Product.objects.filter(tenant_id=tenant.id).select_related(
                "product_type"
            )
        }
        labels: list[str] = []
        for name in SKU_NAMES:
            product = products.get(name.lower())
            if product is None:
                continue
            sku = (product.name or "").strip()
            cat = (getattr(product.product_type, "name", "") or "").strip()
            labels.append(f"{cat}{PRODUCT_CATEGORY_SEPARATOR}{sku}" if cat else sku)
        return labels

    def _sync_products_sampled(self, tenant, apply: bool) -> None:
        """Point the retail Products Sampled field at the live catalog."""
        options = self._sku_option_labels(tenant)
        self.stdout.write(
            "  Products Sampled ← "
            + (", ".join(options) if options else "(catalog empty)")
        )
        if not apply or not options:
            return
        from recaps.models import CustomField

        updated = CustomField.objects.filter(
            custom_recap_template__tenant_id=tenant.id,
            custom_recap_template__name=RETAIL_TEMPLATE_NAME,
            name__iexact=PRODUCTS_SAMPLED_FIELD,
        ).update(options=list(options))
        self.stdout.write(f"  wrote catalog options onto {updated} field(s)")

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
        for t in rows:
            n_fields = CustomField.objects.filter(custom_recap_template=t).count()
            same = " <-- SAME NAME, will be reused" if t.name == template_name else ""
            self.stdout.write(
                f"  [{t.id}] {t.name!r} — {n_fields} field(s), "
                f"event_type={getattr(t.event_type, 'name', None)!r}{same}"
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
                    "product_samples": False,
                    "sales_performance": False,
                    "layout": {},
                    "created_by": creator,
                },
            )
            if template.event_type_id != event_type.id:
                template.event_type = event_type
                template.save(update_fields=["event_type"])
                self.stdout.write(
                    f"  Re-pointed {template_name!r} event_type → {event_type.name!r}"
                )
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
                if (
                    fname != PRODUCTS_SAMPLED_FIELD
                    and list(field.options or []) != list(options)
                ):
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

    def _pin_programs(self, tenant, event_type, retail_type, apply: bool) -> None:
        """Keep Event Activation as the pinned default and offer both programs."""
        from events.models import EventType

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

        selectable = [event_type]
        if retail_type is not None and getattr(retail_type, "id", None):
            selectable.append(retail_type)
        offered = ", ".join(
            name
            for name in (
                getattr(event_type, "name", None),
                getattr(retail_type, "name", None) or RETAIL_PROGRAM,
            )
            if name
        )
        if not apply or len(selectable) < 2:
            self.stdout.write(
                self.style.WARNING(f"  DRY-RUN — would make selectable: {offered}")
            )
            return
        tenant.checkin_event_types.set(selectable)
        EventType.objects.filter(tenant_id=tenant.id, is_default=True).exclude(
            pk=event_type.pk
        ).update(is_default=False)
        if not event_type.is_default:
            event_type.is_default = True
            event_type.save(update_fields=["is_default"])
        self.stdout.write(
            "  selectable = " + ", ".join(f"[{t.id}] {t.name}" for t in selectable)
        )

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
        """Leave one named template on the event type so walk-up hits it.

        ``resolve_template_for_event`` does
        ``filter(event_type_id=...).order_by("id").first()``. A leftover on
        the same event type wins over the intended form if it has the lower
        id. Unused leftovers (0 recaps) are deleted. Leftovers with recaps
        keep their rows: drop the empty keeper, rename the leftover, and
        upsert fields onto it.
        """
        from recaps.models import CustomRecap, CustomRecapTemplate

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

    def _retire_other_programs(
        self, tenant, event_type, retail_type, apply: bool
    ) -> None:
        """Keep Event Activation and Retail Sampling. Retire the createTenant extras."""
        from events.models import Event, EventType
        from recaps.models import CustomRecapTemplate

        if event_type is None or not getattr(event_type, "id", None):
            return
        keep_ids = {event_type.id}
        if retail_type is not None and getattr(retail_type, "id", None):
            keep_ids.add(retail_type.id)
        others = list(
            EventType.objects.filter(tenant_id=tenant.id)
            .exclude(pk__in=keep_ids)
            .order_by("id")
        )
        if not others:
            self.stdout.write(
                "\nNo other event types — Event Activation and Retail Sampling "
                "are the walk-up programs."
            )
            return
        self.stdout.write("\nOther event types (will not be selectable):")
        for extra in others:
            n_events = Event.objects.filter(event_type=extra).count()
            n_tpl = CustomRecapTemplate.objects.filter(event_type=extra).count()
            refs = []
            if n_events:
                refs.append(f"{n_events} event(s)→{event_type.name}")
            if n_tpl:
                refs.append(f"{n_tpl} template(s)→{event_type.name}")
            ref_txt = f"  [{', '.join(refs)}]" if refs else "  [unused]"
            if (extra.name or "").lower() in KEEP_PROGRAMS:
                self.stdout.write(f"    keeping {extra.name!r}{ref_txt}")
                continue
            if not apply:
                self.stdout.write(
                    self.style.WARNING(f"    would retire {extra.name!r}{ref_txt}")
                )
                continue
            Event.objects.filter(event_type=extra).update(event_type=event_type)
            CustomRecapTemplate.objects.filter(event_type=extra).update(
                event_type=event_type
            )
            extra.delete()
            self.stdout.write(
                self.style.SUCCESS(f"    retired {extra.name!r}{ref_txt}")
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
            raise CommandError("--prefix should be 1-4 letters/digits, e.g. PE.")
        code_prefix = f"{cleaned}-" if cleaned else CODE_PREFIX

        ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"
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

        # Default new link is the standing code the field team already has.
        # An explicit --prefix still mints a fresh code; the default never does.
        if not cleaned:
            taken = (
                Tenant.objects.filter(checkin_code__iexact=STANDING_CODE)
                .exclude(pk=tenant.pk)
                .first()
            )
            if taken is not None:
                raise CommandError(
                    f"{STANDING_CODE} belongs to {taken.slug!r}. "
                    "Refusing to mint a different Pivot code."
                )
            code = STANDING_CODE
        else:
            code = None
            for _ in range(12):
                candidate = code_prefix + "".join(
                    secrets.choice(ALPHABET) for _ in range(6)
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
