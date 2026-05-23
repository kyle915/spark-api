"""
One-shot onboarding for the Girl Beer tenant.

Provisions everything Spark needs to start booking, executing, and
recapping Girl Beer retail sampling activations:

  1. Tenant row (idempotent — get_or_create on `slug`)
  2. ProductType "Beer" under the tenant
  3. Seven products (Purple Variety, Red Variety, Blueberry Lavender,
     Pineapple Yuzu, Grapefruit Guava, Peach, Tangerine — all 6-packs
     except the variety packs)
  4. EventType "Retail Sampling" under the tenant
  5. CustomRecapTemplate matching the PDF recap that Michelle Yeh
     filled out — Visit Details, Sales Figures, Customer Interaction,
     Demographics, Common Questions, Customer Feedback, Staff/Demo,
     Photos.
  6. RecapSection rows for each section above
  7. CustomField rows linking template → section → field type, with
     "Table setup pictures" and "Sampling pictures" added under Photos
     per Kyle's note.

Idempotent — re-running won't duplicate anything. Client user creation
is deferred until Kyle has the contact email.

Usage:
    python manage.py onboard_girl_beer --owner-email kyle@igniteproductions.co
"""

from __future__ import annotations

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from events.models import EventType, Product, ProductType
from recaps.models import (
    CustomField,
    CustomRecapFieldType,
    CustomRecapTemplate,
    RecapSection,
)
from tenants.models import Tenant

User = get_user_model()

TENANT_NAME = "Girl Beer"
TENANT_SLUG = "girl-beer"
TENANT_REQUEST_URL_NAME = "girl-beer"

PRODUCTS = [
    "Purple Variety Pack",
    "Red Variety Pack",
    "Blueberry Lavender 6-Pack",
    "Pineapple Yuzu 6-Pack",
    "Grapefruit Guava 6-Pack",
    "Peach 6-Pack",
    "Tangerine 6-Pack",
]

# Field-type names. The frontend renders inputs based on these strings;
# the existing tenants (Borjomi, etc.) use "text" / "number" / "image"
# in lowercase. get_or_create against the global CustomRecapFieldType
# table — these are shared across all tenants.
FT_TEXT = "text"
FT_NUMBER = "number"
FT_IMAGE = "image"
FT_LONGTEXT = "longtext"  # falls back to "text" if not registered yet

# (Section label, [ (field name, field type, required) ])
SECTIONS: list[tuple[str, list[tuple[str, str, bool]]]] = [
    (
        "Visit Details",
        [
            ("Store Associate Spoken To", FT_TEXT, False),
            ("What flavors were available to taste?", FT_TEXT, False),
        ],
    ),
    (
        "Sales Figures",
        [
            ("# of PURPLE Variety Packs sold", FT_NUMBER, False),
            ("# of RED Variety Packs sold", FT_NUMBER, False),
            ("Blueberry Lavender 6-packs Sold", FT_NUMBER, False),
            ("Pineapple Yuzu 6-packs Sold", FT_NUMBER, False),
            ("Grapefruit Guava 6-packs Sold", FT_NUMBER, False),
            ("Peach 6-packs Sold", FT_NUMBER, False),
            ("Tangerine 6-packs Sold", FT_NUMBER, False),
            ("Total Samples Given Out", FT_NUMBER, True),
        ],
    ),
    (
        "Customer Interaction",
        [
            ("Foot Traffic (people walking by per hour)", FT_NUMBER, False),
            ("Number of Customers Engaged", FT_NUMBER, True),
        ],
    ),
    (
        "Demographics — Bought",
        [
            ("Men who bought (21-29)", FT_NUMBER, False),
            ("Men who bought (30-39)", FT_NUMBER, False),
            ("Men who bought (40+)", FT_NUMBER, False),
            ("Women who bought (21-29)", FT_NUMBER, False),
            ("Women who bought (30-39)", FT_NUMBER, False),
            ("Women who bought (40+)", FT_NUMBER, False),
        ],
    ),
    (
        "Demographics — Sampled",
        [
            ("Men who sampled (21-29)", FT_NUMBER, False),
            ("Men who sampled (30-39)", FT_NUMBER, False),
            ("Men who sampled (40+)", FT_NUMBER, False),
            ("Women who sampled (21-29)", FT_NUMBER, False),
            ("Women who sampled (30-39)", FT_NUMBER, False),
            ("Women who sampled (40+)", FT_NUMBER, False),
        ],
    ),
    (
        "Common Questions & Comments",
        [
            ("Most Common Question / Comment 1", FT_LONGTEXT, False),
            ("Most Common Question / Comment 2", FT_LONGTEXT, False),
            ("Most Common Question / Comment 3", FT_LONGTEXT, False),
            ("Most Common Question / Comment 4", FT_LONGTEXT, False),
        ],
    ),
    (
        "Customer Feedback",
        [
            ("Positive Feedback From Customers", FT_LONGTEXT, False),
            ("Negative Feedback / Concerns From Customers", FT_LONGTEXT, False),
        ],
    ),
    (
        "Staff & Demo Experience",
        [
            ("How was the setup?", FT_LONGTEXT, False),
            ("Did the demo influence the store to place a reorder?", FT_LONGTEXT, False),
            ("Anything that could make future demos better?", FT_LONGTEXT, False),
            ("Account Spend Amount ($)", FT_NUMBER, False),
            ("Product purchase receipt", FT_IMAGE, False),
        ],
    ),
    (
        "Photos",
        [
            # Per Kyle: add table setup + sampling photos.
            ("Table setup pictures", FT_IMAGE, True),
            ("Sampling pictures", FT_IMAGE, True),
        ],
    ),
]


class Command(BaseCommand):
    help = "Provision the Girl Beer tenant + products + custom recap template."

    def add_arguments(self, parser):
        parser.add_argument(
            "--owner-email",
            required=True,
            help="Email of the Spark admin who owns this onboarding (used for created_by).",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Print what would happen without writing to the DB.",
        )

    def handle(self, *args, **opts):
        owner_email: str = opts["owner_email"]
        dry_run: bool = opts["dry_run"]

        try:
            owner = User.objects.get(email__iexact=owner_email)
        except User.DoesNotExist:
            raise CommandError(f"No user with email {owner_email}")

        self.stdout.write(self.style.MIGRATE_HEADING(
            f"\nOnboarding Girl Beer · owner={owner.email} (id={owner.id})"
        ))
        if dry_run:
            self.stdout.write(self.style.NOTICE("DRY RUN — no DB writes.\n"))

        if dry_run:
            # Just narrate what would happen.
            self._narrate()
            return

        with transaction.atomic():
            tenant = self._upsert_tenant(owner)
            product_type = self._upsert_product_type(tenant, owner)
            self._upsert_products(tenant, product_type, owner)
            event_type = self._upsert_event_type(tenant, owner)
            field_types = self._upsert_field_types(owner)
            template = self._upsert_template(tenant, event_type, owner)
            self._upsert_fields(tenant, template, field_types, owner)

        self.stdout.write(self.style.SUCCESS(
            "\nGirl Beer onboarding complete."
        ))
        self.stdout.write(self.style.SUCCESS(
            f"  External request form: client.igniteproductions.co/spark-form/{TENANT_SLUG}"
        ))
        self.stdout.write(self.style.WARNING(
            "  Client user NOT created (deferred per Kyle — contact email "
            "TBD). Once Kyle has the email, invite via the standard "
            "tenant invite flow."
        ))

    # ─── Helpers ────────────────────────────────────────────────────

    def _narrate(self) -> None:
        self.stdout.write("Would create / verify:")
        self.stdout.write(f"  Tenant: name='{TENANT_NAME}' slug='{TENANT_SLUG}'")
        self.stdout.write(f"  ProductType: 'Beer' under {TENANT_NAME}")
        for p in PRODUCTS:
            self.stdout.write(f"    - Product '{p}'")
        self.stdout.write(f"  EventType: 'Retail Sampling' under {TENANT_NAME}")
        for section_name, fields in SECTIONS:
            self.stdout.write(f"  RecapSection: '{section_name}'")
            for fname, ftype, required in fields:
                req = " *" if required else ""
                self.stdout.write(f"    - {fname} [{ftype}]{req}")

    def _upsert_tenant(self, owner) -> Tenant:
        tenant, created = Tenant.objects.get_or_create(
            slug=TENANT_SLUG,
            defaults={
                "name": TENANT_NAME,
                "request_url_name": TENANT_REQUEST_URL_NAME,
                "created_by": owner,
            },
        )
        verb = "Created" if created else "Found existing"
        self.stdout.write(f"  {verb} Tenant id={tenant.id} '{tenant.name}'")
        # Backfill request_url_name on an old tenant that may have been
        # created before we added that field.
        if not tenant.request_url_name:
            tenant.request_url_name = TENANT_REQUEST_URL_NAME
            tenant.updated_by = owner
            tenant.save(update_fields=["request_url_name", "updated_by"])
        return tenant

    def _upsert_product_type(self, tenant: Tenant, owner) -> ProductType:
        pt, created = ProductType.objects.get_or_create(
            tenant=tenant,
            name="Beer",
            defaults={"created_by": owner},
        )
        self.stdout.write(
            f"  {'Created' if created else 'Found'} ProductType id={pt.id} 'Beer'"
        )
        return pt

    def _upsert_products(
        self, tenant: Tenant, product_type: ProductType, owner
    ) -> None:
        for name in PRODUCTS:
            product, created = Product.objects.get_or_create(
                tenant=tenant,
                name=name,
                defaults={
                    "product_type": product_type,
                    "created_by": owner,
                },
            )
            self.stdout.write(
                f"    {'Created' if created else 'Found'} Product id={product.id} '{name}'"
            )

    def _upsert_event_type(self, tenant: Tenant, owner) -> EventType:
        et, created = EventType.objects.get_or_create(
            tenant=tenant,
            name="Retail Sampling",
            defaults={
                "slug": "retail-sampling",
                "is_default": True,
                "created_by": owner,
            },
        )
        self.stdout.write(
            f"  {'Created' if created else 'Found'} EventType id={et.id} 'Retail Sampling'"
        )
        return et

    def _upsert_field_types(self, owner) -> dict[str, CustomRecapFieldType]:
        # Shared across tenants. Lower-case names by convention; the
        # frontend matches case-insensitive when rendering inputs.
        wanted = [FT_TEXT, FT_NUMBER, FT_IMAGE, FT_LONGTEXT]
        out: dict[str, CustomRecapFieldType] = {}
        for name in wanted:
            ft, created = CustomRecapFieldType.objects.get_or_create(
                name=name,
                defaults={"created_by": owner},
            )
            verb = "Created" if created else "Found"
            self.stdout.write(f"  {verb} CustomRecapFieldType id={ft.id} '{name}'")
            out[name] = ft
        return out

    def _upsert_template(
        self, tenant: Tenant, event_type: EventType, owner
    ) -> CustomRecapTemplate:
        # Multiple templates per tenant are allowed, so we match by
        # (tenant, event_type, name) to keep this idempotent.
        template, created = CustomRecapTemplate.objects.get_or_create(
            tenant=tenant,
            event_type=event_type,
            name="Girl Beer · Retail Sampling Recap",
            defaults={
                "product_samples": True,
                "sales_performance": True,
                "layout": {
                    # Layout JSON is a hint to the BA mobile renderer
                    # about section ordering. The CustomField rows are
                    # the canonical source of truth; the layout block
                    # gives an explicit order so a missing field never
                    # silently disappears between sections.
                    "sections": [s for s, _ in SECTIONS],
                    "version": 1,
                },
                "created_by": owner,
            },
        )
        verb = "Created" if created else "Found"
        self.stdout.write(
            f"  {verb} CustomRecapTemplate id={template.id} "
            f"'{template.name}'"
        )
        return template

    def _upsert_fields(
        self,
        tenant: Tenant,
        template: CustomRecapTemplate,
        field_types: dict[str, CustomRecapFieldType],
        owner,
    ) -> None:
        for section_name, fields in SECTIONS:
            section, created = RecapSection.objects.get_or_create(
                tenant=tenant,
                name=section_name,
                defaults={"created_by": owner},
            )
            self.stdout.write(
                f"  {'Created' if created else 'Found'} RecapSection "
                f"id={section.id} '{section_name}'"
            )
            for field_name, type_name, required in fields:
                # Some installations may not have 'longtext' yet —
                # fall back to 'text'.
                ftype = field_types.get(type_name) or field_types[FT_TEXT]
                field, created_field = CustomField.objects.get_or_create(
                    custom_recap_template=template,
                    recap_section=section,
                    name=field_name,
                    defaults={
                        "custom_field_type": ftype,
                        "required": required,
                        "created_by": owner,
                    },
                )
                # Backfill required-flag on an old row if it diverged.
                if not created_field and field.required != required:
                    field.required = required
                    field.updated_by = owner
                    field.save(update_fields=["required", "updated_by"])
                self.stdout.write(
                    f"    {'+' if created_field else '·'} {field_name} "
                    f"[{ftype.name}]{' *' if required else ''}"
                )
