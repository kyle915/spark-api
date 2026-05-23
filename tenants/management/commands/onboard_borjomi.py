"""
Provision the Borjomi recap template (and any missing catalog rows).

Borjomi already has a Tenant (id=9) and active recap traffic, so this
command is mostly additive: ensure the product/request-type/event-type
rows exist and create a CustomRecapTemplate matching Crystal Vizcaino's
5/23 Connecteam recap PDF. Same field-matching as Girl Beer — the
Connecteam PDF importer (recaps/connecteam.py) maps PDF labels to
these CustomField names directly, so changing a name here will move
which template field a given PDF row imports into.

Idempotent — uses get_or_create on natural keys throughout. Re-running
the command on Borjomi only adds the missing rows.

Usage:
    python manage.py onboard_borjomi --owner-email kyle@igniteproductions.co
    python manage.py onboard_borjomi --owner-email kyle@igniteproductions.co --dry-run
"""

from __future__ import annotations

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from events.models import EventType, Product, ProductType, RequestType
from recaps.models import (
    CustomField,
    CustomRecapFieldType,
    CustomRecapTemplate,
    RecapSection,
)
from tenants.models import Tenant

User = get_user_model()

TENANT_NAME = "Borjomi"

PRODUCTS = [
    "Citrus Lemonade",
    "Pear Lemonade",
    "Tarkhun Lemonade",
    "Adjarian Mandarin Lemonade",
]

REQUEST_TYPES = [
    "Retail Sampling",
    "On-Premise Sampling",
]

FT_TEXT = "text"
FT_NUMBER = "number"
FT_IMAGE = "image"
FT_LONGTEXT = "longtext"

# (Section, [(field name, type, required)])
#
# Field names match Crystal's 5/23/26 Connecteam PDF verbatim — the
# Connecteam importer normalizes (lowercase, drop punctuation, collapse
# whitespace) before fuzzy-matching, but exact names are still the
# cleanest path to a 100% match. The one-letter typo in the source
# PDF ("tasing" instead of "tasting") is preserved so the importer
# can match the literal PDF label until Connecteam fixes it on their
# end.
SECTIONS: list[tuple[str, list[tuple[str, str, bool]]]] = [
    (
        "Account & Spend",
        [
            ("Account Spend Amount ($)", FT_NUMBER, False),
            ("Which products were sampled?", FT_LONGTEXT, False),
            (
                "Was an Ignite provided credit card used for product spend?",
                FT_TEXT,
                False,
            ),
            (
                "Any expenses / bill-backs outside of product. (E.g. Tolls, Parking, etc)",
                FT_LONGTEXT,
                False,
            ),
        ],
    ),
    (
        "Sampling Counts",
        [
            ("Total number of consumers sampled", FT_NUMBER, True),
            ("First Time consumers?", FT_NUMBER, False),
            (
                "How many consumers that were engaged with knew about Borjomi product/brand?",
                FT_NUMBER,
                False,
            ),
            (
                "How many consumers had tried a Limonati by Borjomi before?",
                FT_NUMBER,
                False,
            ),
            (
                "How many consumers would be willing to purchase the product after tasting it?",
                FT_NUMBER,
                False,
            ),
            (
                # Source PDF has the typo "tasing"; keep verbatim so
                # the Connecteam parser matches it.
                "How many consumers would NOT be willing to purchase the product after tasing it?",
                FT_NUMBER,
                False,
            ),
            ("How many single cans did consumers purchase?", FT_NUMBER, False),
            ("How many packs did consumers purchase?", FT_NUMBER, False),
        ],
    ),
    (
        "Customer Feedback",
        [
            ("What were 5 customer comments you heard?", FT_LONGTEXT, False),
            (
                "What were 2 reasons customers gave when they declined to purchase?",
                FT_LONGTEXT,
                False,
            ),
            (
                "Demographics (general age, sex, ethnicities of consumers)",
                FT_LONGTEXT,
                False,
            ),
        ],
    ),
    (
        "Event Feedback",
        [
            ("Anything about the event you'd change or do differently?", FT_LONGTEXT, False),
            ("Any feedback from the account?", FT_LONGTEXT, False),
        ],
    ),
    (
        "Photos",
        [
            ("Picture of all in-stock product", FT_IMAGE, True),
            ("Upload consumer sampling photos here", FT_IMAGE, True),
            ("Upload a picture of you behind the sampling table.", FT_IMAGE, True),
            ("Upload account spend receipt", FT_IMAGE, False),
        ],
    ),
]


class Command(BaseCommand):
    help = "Provision Borjomi's products + custom recap template."

    def add_arguments(self, parser):
        parser.add_argument(
            "--owner-email",
            required=True,
            help="Email of the Spark admin who owns this onboarding (created_by).",
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

        # Tenant must already exist — Borjomi is established. If it
        # doesn't, that's a serious data problem we shouldn't paper
        # over by creating a new one.
        try:
            tenant = Tenant.objects.get(name__iexact=TENANT_NAME)
        except Tenant.DoesNotExist:
            raise CommandError(
                f"No Tenant with name '{TENANT_NAME}'. "
                "Refusing to create — the existing Borjomi data is keyed "
                "to the existing tenant id; making a new one would orphan it."
            )

        self.stdout.write(self.style.MIGRATE_HEADING(
            f"\nOnboarding Borjomi · tenant id={tenant.id} · owner={owner.email}"
        ))
        if dry_run:
            self.stdout.write(self.style.NOTICE("DRY RUN — no DB writes.\n"))
            self._narrate(tenant)
            return

        with transaction.atomic():
            product_type = self._upsert_product_type(tenant, owner)
            self._upsert_products(tenant, product_type, owner)
            self._upsert_request_types(tenant, owner)
            event_type = self._upsert_event_type(tenant, owner)
            field_types = self._upsert_field_types(owner)
            template = self._upsert_template(tenant, event_type, owner)
            self._upsert_fields(tenant, template, field_types, owner)

        self.stdout.write(self.style.SUCCESS(
            "\nBorjomi onboarding complete."
        ))
        self.stdout.write(self.style.SUCCESS(
            "  Connecteam PDF importer will match against this template "
            "via the recap list page's 'Import Connecteam PDF' button."
        ))

    # ─── Helpers ────────────────────────────────────────────────────

    def _narrate(self, tenant) -> None:
        self.stdout.write("Would create / verify:")
        self.stdout.write(f"  Tenant: id={tenant.id} name='{tenant.name}' (existing)")
        self.stdout.write(f"  ProductType: 'Sparkling Lemonade' under {TENANT_NAME}")
        for p in PRODUCTS:
            self.stdout.write(f"    - Product '{p}'")
        for rt in REQUEST_TYPES:
            self.stdout.write(f"  RequestType: '{rt}'")
        self.stdout.write(f"  EventType: 'Retail Sampling'")
        for section_name, fields in SECTIONS:
            self.stdout.write(f"  RecapSection: '{section_name}'")
            for fname, ftype, required in fields:
                req = " *" if required else ""
                self.stdout.write(f"    - {fname} [{ftype}]{req}")

    def _upsert_product_type(self, tenant: Tenant, owner) -> ProductType:
        pt, created = ProductType.objects.get_or_create(
            tenant=tenant,
            name="Sparkling Lemonade",
            defaults={"created_by": owner},
        )
        self.stdout.write(
            f"  {'Created' if created else 'Found'} ProductType id={pt.id} '{pt.name}'"
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

    def _upsert_request_types(self, tenant: Tenant, owner) -> None:
        for name in REQUEST_TYPES:
            rt, created = RequestType.objects.get_or_create(
                tenant=tenant,
                name=name,
                defaults={"created_by": owner},
            )
            self.stdout.write(
                f"  {'Created' if created else 'Found'} RequestType "
                f"id={rt.id} '{name}'"
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
        template, created = CustomRecapTemplate.objects.get_or_create(
            tenant=tenant,
            event_type=event_type,
            name="Borjomi · Retail Sampling Recap",
            defaults={
                "product_samples": True,
                "sales_performance": True,
                "layout": {
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
                if not created_field and field.required != required:
                    field.required = required
                    field.updated_by = owner
                    field.save(update_fields=["required", "updated_by"])
                self.stdout.write(
                    f"    {'+' if created_field else '·'} {field_name} "
                    f"[{ftype.name}]{' *' if required else ''}"
                )
