"""Pivot Energy Drink Event Activation walk-up setup tests."""

from __future__ import annotations

import io

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import Client, override_settings

from events.event_confirmations import catalog_product_options
from events.models import EventType, Product, ProductType
from recaps.management.commands.setup_ld_retail_checkin import (
    PROGRAMS as LD_PROGRAMS,
)
from recaps.management.commands.setup_pivot_energy_checkin import (
    CODE_PREFIX,
    NEXT_DEMO_QUESTION,
    PHOTO_BUCKETS,
    PRODUCT_TYPE_NAME,
    PROGRAM_NAME,
    RETAIL_PHOTO_BUCKETS,
    RETAIL_PROGRAM,
    RETAIL_SPEC,
    RETAIL_TEMPLATE_NAME,
    SKU_NAMES,
    SPEC,
    STANDING_CODE,
    TEMPLATE_NAME,
    TENANT_NAME,
    TENANT_SLUG,
)
from recaps.models import (
    CustomField,
    CustomRecapFieldType,
    CustomRecapTemplate,
    FileRecapCategory,
    RecapSection,
)
from recaps.products_sampled import PRODUCTS_SAMPLED_FIELD
from tenants.models import Tenant
from tenants.management.commands.setup_brand_group import (
    BRANDS,
    NEXT_DEMO_QUESTION as FAMUS_NEXT_DEMO_QUESTION,
    PHOTO_BUCKETS_BY_PROGRAM as FAMUS_PHOTO_BUCKETS,
    retail_spec,
)
from tenants.tests.base import BaseGraphQLTestCase

VALID_SECRET = "test-cron-secret-value-only-for-tests"
PIVOT_CRON_URL = "/internal/cron/setup-pivot-energy-checkin"

FIELD_NAMES = [f[0] for _, fields in SPEC for f in fields]


class TestPivotEnergySpec:
    def test_event_activation_field_names(self):
        assert FIELD_NAMES == [
            "Event Location",
            "Total samples distributed",
            "Estimated foot traffic / impressions",
            "Mileage",
            "Total spend on the card",
            "Key highlight (1–2 sentences) / What worked well & What didn't?",
            "What were some consumer comments that you heard?",
            "What percent of consumer had heard of or tried Pivot Energy Drink before?",
        ]

    def test_zero_references_to_other_brands(self):
        full_spec_text = str(SPEC) + str(PHOTO_BUCKETS) + TEMPLATE_NAME
        for forbidden in (
            "Festival Location",
            "White Claw",
            "Surge",
            "Hiyo",
            "Jimmy Johns",
            "Breakaway",
            "Dude Wipes",
            "Kalshi",
            "Tables Distributed",
        ):
            assert forbidden.lower() not in full_spec_text.lower()

    def test_photo_buckets(self):
        assert PHOTO_BUCKETS == [
            {"name": "Consumer Sampling Pictures"},
            {"name": "Expense Receipts"},
        ]

    def test_brand_constants(self):
        assert CODE_PREFIX == "PE-"
        assert STANDING_CODE == "PE-Y7VPYC"
        assert TEMPLATE_NAME == "Pivot Energy Drink · Event Activation Recap"
        assert RETAIL_TEMPLATE_NAME == "Pivot Energy Drink · Retail Sampling Recap"
        assert PROGRAM_NAME == "Event Activation"
        assert RETAIL_PROGRAM == "Retail Sampling"
        assert TENANT_SLUG == "pivot-energy-drink"
        assert TENANT_NAME == "Pivot Energy Drink"
        assert SKU_NAMES == (
            "Blue Razz",
            "Pink Lemonade",
            "Melon Rush",
            "Going Grape",
        )

    def test_retail_spec_matches_famus_except_the_brand_question(self):
        famus = next(brand for brand in BRANDS if brand.slug == "famus-energy")
        assert famus.sells_packs is True
        expected = []
        for section, fields in retail_spec(famus):
            expected.append(
                (
                    section,
                    [
                        (
                            name.replace(
                                "FAMUS Energy",
                                "Pivot Energy Drink",
                            ),
                            kind,
                            required,
                            options,
                        )
                        for name, kind, required, options in fields
                    ],
                )
            )
        assert RETAIL_SPEC == expected
        assert RETAIL_PHOTO_BUCKETS == FAMUS_PHOTO_BUCKETS["Retail Sampling"]
        assert NEXT_DEMO_QUESTION == FAMUS_NEXT_DEMO_QUESTION
        ld_retail = next(
            program for program in LD_PROGRAMS if program["event_type"] == "retail sampling"
        )
        famus_buckets = [bucket["name"] for bucket in FAMUS_PHOTO_BUCKETS["Retail Sampling"]]
        ld_only = [
            bucket["name"]
            for bucket in ld_retail["photos"]
            if bucket["name"] not in famus_buckets
        ]
        assert ld_only == []
        assert [bucket["name"] for bucket in RETAIL_PHOTO_BUCKETS] == famus_buckets
        retail_names = [name.lower() for _, fields in RETAIL_SPEC for name, *_ in fields]
        assert not any("mileage" in name for name in retail_names)
        assert not any("total spend" in name for name in retail_names)
        assert not any("event location" in name for name in retail_names)
        assert "expense receipt" not in " ".join(
            bucket["name"].lower() for bucket in RETAIL_PHOTO_BUCKETS
        )
        assert "Mileage" in FIELD_NAMES

        famus_names = [name for _, fields in retail_spec(famus) for name, *_ in fields]
        pivot_names = [name for _, fields in RETAIL_SPEC for name, *_ in fields]
        changed = [
            (before, after)
            for before, after in zip(famus_names, pivot_names)
            if before != after
        ]
        assert changed == [
            (
                "How many consumers that were engaged with knew about FAMUS Energy?",
                "How many consumers that were engaged with knew about Pivot Energy Drink?",
            )
        ]

    def test_retail_copy_has_no_other_brand_names(self):
        blob = " ".join(
            [
                RETAIL_TEMPLATE_NAME,
                str(RETAIL_SPEC),
                str(RETAIL_PHOTO_BUCKETS),
                " ".join(SKU_NAMES),
            ]
        ).lower()
        for forbidden in (
            "famus",
            "wook",
            "alcohol armor",
            "white claw",
            "surge",
            "og luxe",
            "havana",
        ):
            assert forbidden not in blob
        closer = RETAIL_SPEC[-1]
        assert closer[0] == "Follow-up"
        assert closer[1] == [(NEXT_DEMO_QUESTION, "longtext", False, [])]
        kinds = {field[1] for _, fields in RETAIL_SPEC for field in fields}
        assert "image" not in kinds
        assert PRODUCTS_SAMPLED_FIELD in {
            field[0] for _, fields in RETAIL_SPEC for field in fields
        }

    def test_kinds_are_canonical_and_not_image(self):
        kinds = {f[1] for _, fields in SPEC for f in fields}
        assert kinds <= {"text", "number", "longtext", "select", "multiselect"}
        assert "image" not in kinds


@pytest.mark.django_db(transaction=True)
class TestPivotEnergySetupCommand(BaseGraphQLTestCase):
    @pytest.fixture(autouse=True)
    def setup(self, db):
        self.roles = self.setup_default_roles()
        self.user = self.get_system_user()
        self.user.is_superuser = True
        self.user.save(update_fields=["is_superuser"])

    def _run(self, **kw):
        out = io.StringIO()
        call_command("setup_pivot_energy_checkin", stdout=out, **kw)
        return out.getvalue()

    def test_dry_run_creates_nothing(self):
        from recaps.models import CustomRecapTemplate
        from tenants.models import Tenant

        log = self._run(tenant="pivot-energy-drink")
        assert "DRY-RUN" in log
        assert TENANT_NAME in log
        assert not Tenant.objects.filter(slug=TENANT_SLUG).exists()
        assert not CustomRecapTemplate.objects.filter(name=TEMPLATE_NAME).exists()
        assert not CustomRecapTemplate.objects.filter(
            name=RETAIL_TEMPLATE_NAME
        ).exists()

    def test_substring_slug_is_rejected(self):
        with pytest.raises(CommandError, match="exact slug"):
            self._run(tenant="pivot")
        assert not Tenant.objects.filter(slug="pivot").exists()
        assert not Tenant.objects.filter(slug=TENANT_SLUG).exists()

    def test_apply_creates_tenant_template_code_and_buckets(self):
        from recaps.models import CustomField, CustomRecapTemplate, FileRecapCategory
        from tenants.models import Tenant

        log = self._run(tenant="pivot-energy-drink", apply=True)
        assert "APPLIED" in log
        tenant = Tenant.objects.get(slug=TENANT_SLUG)
        assert tenant.name == TENANT_NAME
        assert tenant.checkin_code == STANDING_CODE
        assert tenant.checkin_location_mode == Tenant.CHECKIN_LOCATION_ADDRESS
        assert tenant.checkin_photo_buckets == {
            PROGRAM_NAME: PHOTO_BUCKETS,
            RETAIL_PROGRAM: RETAIL_PHOTO_BUCKETS,
        }
        assert tenant.checkin_event_type is not None
        assert tenant.checkin_event_type.name == PROGRAM_NAME
        assert set(tenant.checkin_event_types.values_list("name", flat=True)) == {
            PROGRAM_NAME,
            RETAIL_PROGRAM,
        }
        assert FileRecapCategory.objects.filter(
            tenant=tenant, name="Consumer Sampling Pictures"
        ).exists()
        assert FileRecapCategory.objects.filter(
            tenant=tenant, name="Expense Receipts"
        ).exists()

        tpl = CustomRecapTemplate.objects.get(tenant=tenant, name=TEMPLATE_NAME)
        names = list(
            CustomField.objects.filter(custom_recap_template=tpl)
            .order_by("recap_section__order", "order", "id")
            .values_list("name", flat=True)
        )
        assert names == FIELD_NAMES
        assert tpl.event_type.name == PROGRAM_NAME
        assert tpl.product_samples is False

        code = tenant.checkin_code
        log2 = self._run(tenant="pivot-energy-drink", apply=True)
        tenant.refresh_from_db()
        assert tenant.checkin_code == code == STANDING_CODE
        assert "already set" in log2

    def test_apply_adds_retail_recap_skus_and_keeps_event_activation(self):
        tenant = self.create_tenant(
            name=TENANT_NAME,
            slug=TENANT_SLUG,
            request_url_name=TENANT_SLUG,
            checkin_code=STANDING_CODE,
            checkin_resources=[
                {
                    "label": "BA Guide",
                    "kind": "pdf",
                    "url": (
                        "https://client.igniteproductions.co/training/"
                        "pivot-energy-drink/ba-guide.pdf"
                    ),
                }
            ],
        )
        legacy_type = ProductType.objects.create(
            name="Legacy", tenant=tenant, created_by=self.user
        )
        Product.objects.create(
            name="House Blend",
            product_type=legacy_type,
            tenant=tenant,
            created_by=self.user,
        )

        famus = self.create_tenant(name="FAMUS Energy", slug="famus-energy")
        famus_type = self._event_type(famus, "Retail Sampling")
        famus_template = self._template(
            famus,
            famus_type,
            "FAMUS Energy · Retail Sampling Recap",
        )
        famus_label = (
            "How many consumers that were engaged with knew about FAMUS Energy?"
        )
        famus_field = self._field(famus_template, "Brand Results", famus_label)

        log = self._run(tenant="pivot-energy-drink", apply=True)
        assert "APPLIED" in log

        tenant.refresh_from_db()
        assert tenant.slug == TENANT_SLUG
        assert tenant.checkin_code == STANDING_CODE
        assert [row["label"] for row in tenant.checkin_resources] == [
            "BA Guide",
            "Retail Sampling FAQ",
            "Product Comparison",
        ]
        assert tenant.checkin_resources[1]["kind"] == "pdf"
        assert tenant.checkin_resources[1]["url"] == (
            "https://client.igniteproductions.co/training/"
            "pivot-energy-drink/retail-sampling-faq.pdf"
        )
        assert tenant.checkin_resources[2]["label"] == "Product Comparison"
        assert tenant.checkin_resources[2]["url"] == (
            "https://client.igniteproductions.co/training/"
            "pivot-energy-drink/product-comparison.pdf"
        )
        assert "hideOnRecapOnly" not in tenant.checkin_resources[1]
        assert "hideOnRecapOnly" not in tenant.checkin_resources[2]
        assert tenant.checkin_training_url.endswith(
            "/training/pivot-energy-drink/ba-guide.pdf"
        )
        famus.refresh_from_db()
        assert famus.checkin_resources is None
        assert tenant.checkin_event_type.name == PROGRAM_NAME
        assert set(tenant.checkin_event_types.values_list("name", flat=True)) == {
            PROGRAM_NAME,
            RETAIL_PROGRAM,
        }

        event = CustomRecapTemplate.objects.get(tenant=tenant, name=TEMPLATE_NAME)
        event_names = list(
            CustomField.objects.filter(custom_recap_template=event)
            .order_by("recap_section__order", "order", "id")
            .values_list("name", flat=True)
        )
        assert event_names == FIELD_NAMES
        assert event.event_type.name == PROGRAM_NAME

        retail = CustomRecapTemplate.objects.get(
            tenant=tenant, name=RETAIL_TEMPLATE_NAME
        )
        retail_fields = list(
            CustomField.objects.filter(custom_recap_template=retail)
            .select_related("recap_section", "custom_field_type")
            .order_by("recap_section__order", "order", "id")
        )
        assert [field.name for field in retail_fields] == [
            name for _, fields in RETAIL_SPEC for name, *_ in fields
        ]
        assert retail.event_type.name == RETAIL_PROGRAM
        blob = " ".join(
            f"{field.name} {field.placeholder or ''} {field.options}"
            for field in retail_fields
        ).lower()
        for forbidden in (
            "famus",
            "wook",
            "alcohol armor",
            "white claw",
            "surge",
            "og luxe",
            "havana",
        ):
            assert forbidden not in blob
        closer = retail_fields[-1]
        assert closer.name == NEXT_DEMO_QUESTION
        assert closer.required is False
        assert closer.recap_section.name == "Follow-up"
        assert "long" in (closer.custom_field_type.name or "").lower()
        feedback = next(
            field.recap_section
            for field in retail_fields
            if field.recap_section.name == "Brand Feedback"
        )
        assert closer.recap_section.order > feedback.order
        assert not any(
            "image" in (field.custom_field_type.name or "").lower()
            or "photo" in (field.custom_field_type.name or "").lower()
            for field in retail_fields
        )

        names = set(
            Product.objects.filter(tenant=tenant).values_list("name", flat=True)
        )
        assert set(SKU_NAMES) <= names
        assert "House Blend" in names
        assert "OG Luxe (Tropical Ice) 16oz" not in names
        catalog = catalog_product_options(tenant)
        for sku in SKU_NAMES:
            assert f"{PRODUCT_TYPE_NAME} — {sku}" in catalog
        assert "Legacy — House Blend" in catalog
        sampled = next(
            field for field in retail_fields if field.name == PRODUCTS_SAMPLED_FIELD
        )
        assert list(sampled.options) == catalog

        for bucket in (
            "Table Set Up",
            "Product Display",
            "Product Receipt",
            "Expense Receipts",
        ):
            assert FileRecapCategory.objects.filter(tenant=tenant, name=bucket).exists()

        famus_field.refresh_from_db()
        famus_template.refresh_from_db()
        assert famus_field.name == famus_label
        assert famus_template.name == "FAMUS Energy · Retail Sampling Recap"
        assert famus_template.tenant_id == famus.id
        assert not CustomField.objects.filter(
            custom_recap_template__tenant=famus,
            name__icontains="Pivot",
        ).exists()

    def test_apply_strips_mileage_from_the_live_retail_template(self):
        tenant = self.create_tenant(
            name=TENANT_NAME,
            slug=TENANT_SLUG,
            request_url_name=TENANT_SLUG,
            checkin_code=STANDING_CODE,
        )
        event_type = self._event_type(tenant, PROGRAM_NAME)
        event_template = self._template(tenant, event_type, TEMPLATE_NAME)
        event_mileage = self._field(event_template, "Expenses", "Mileage")
        retail_type = self._event_type(tenant, RETAIL_PROGRAM)
        legacy = self._template(tenant, retail_type, "Pivot · Retail Sampling Recap")
        self._field(legacy, "Card & Mileage", "Mileage")
        self._field(legacy, "Card & Mileage", "Total spend on the card")
        self._field(legacy, "Store Details", "Where are you working (store)?")
        product_type = ProductType.objects.create(
            name="Pivot", tenant=tenant, created_by=self.user
        )
        for sku in SKU_NAMES:
            Product.objects.create(
                name=sku,
                product_type=product_type,
                tenant=tenant,
                created_by=self.user,
            )

        famus = self.create_tenant(name="FAMUS Energy", slug="famus-energy")
        famus_type = self._event_type(famus, "Retail Sampling")
        famus_template = self._template(
            famus, famus_type, "FAMUS Energy · Retail Sampling Recap"
        )
        famus_label = (
            "How many consumers that were engaged with knew about FAMUS Energy?"
        )
        famus_field = self._field(famus_template, "Brand Results", famus_label)

        ld = self.create_tenant(name="Liquid Death", slug="liquid-death")
        ld_type = self._event_type(ld, "Retail Sampling")
        ld_template = self._template(ld, ld_type, "Liquid Death-Retail Sampling")
        ld_field = self._field(ld_template, "Additional Insights", "Account Spend Amount")

        log = self._run(tenant="pivot-energy-drink", apply=True)
        assert "dropped 'Mileage'" in log
        assert "PE-Y7VPYC" in log

        retail = CustomRecapTemplate.objects.get(
            tenant=tenant, name=RETAIL_TEMPLATE_NAME
        )
        retail_names = list(
            CustomField.objects.filter(custom_recap_template=retail)
            .order_by("recap_section__order", "order", "id")
            .values_list("name", flat=True)
        )
        assert retail_names == [name for _, fields in RETAIL_SPEC for name, *_ in fields]
        assert not any("mileage" in name.lower() for name in retail_names)
        assert CustomRecapTemplate.objects.filter(
            tenant=tenant, name="Pivot · Retail Sampling Recap"
        ).count() == 0
        assert (
            CustomRecapTemplate.objects.filter(
                tenant=tenant, event_type=retail_type
            ).count()
            == 1
        )

        event_mileage.refresh_from_db()
        assert event_mileage.name == "Mileage"
        assert event_mileage.custom_recap_template_id == event_template.id
        tenant.refresh_from_db()
        assert tenant.checkin_code == STANDING_CODE
        assert set(
            Product.objects.filter(tenant=tenant).values_list("name", flat=True)
        ) == set(SKU_NAMES)

        famus_field.refresh_from_db()
        famus_template.refresh_from_db()
        assert famus_field.name == famus_label
        assert famus_template.name == "FAMUS Energy · Retail Sampling Recap"
        ld_field.refresh_from_db()
        ld_template.refresh_from_db()
        assert ld_field.name == "Account Spend Amount"
        assert ld_template.name == "Liquid Death-Retail Sampling"
        assert ld_template.tenant_id == ld.id

    def _event_type(self, tenant, name):
        return EventType.objects.create(
            name=name,
            slug=name.lower().replace(" ", "-"),
            tenant=tenant,
            created_by=self.user,
        )

    def _template(self, tenant, event_type, name):
        return CustomRecapTemplate.objects.create(
            name=name,
            tenant=tenant,
            event_type=event_type,
            created_by=self.user,
        )

    def _field(self, template, section_name, name):
        section = RecapSection.objects.create(
            name=section_name,
            tenant=template.tenant,
            order=1,
            created_by=self.user,
        )
        field_type = CustomRecapFieldType.objects.create(
            name="number", created_by=self.user
        )
        return CustomField.objects.create(
            custom_recap_template=template,
            recap_section=section,
            custom_field_type=field_type,
            name=name,
            required=True,
            order=0,
            created_by=self.user,
        )


@pytest.mark.django_db
class TestPivotEnergySetupCronView:
    def test_valid_secret_fires_command(self):
        client = Client()
        with override_settings(INTERNAL_CRON_SECRET=VALID_SECRET):
            from unittest.mock import patch

            with patch("digest.cron_views.call_command") as mock_call:
                resp = client.post(
                    PIVOT_CRON_URL,
                    {"tenant": "pivot-energy-drink"},
                    HTTP_X_CRON_SECRET=VALID_SECRET,
                )
        assert resp.status_code == 200
        body = resp.json()
        assert body["ok"] is True
        assert body["apply"] is False
        mock_call.assert_called_once()
        assert mock_call.call_args[0][0] == "setup_pivot_energy_checkin"

    def test_bad_secret_returns_401(self):
        client = Client()
        with override_settings(INTERNAL_CRON_SECRET=VALID_SECRET):
            from unittest.mock import patch

            with patch("digest.cron_views.call_command") as mock_call:
                resp = client.post(
                    PIVOT_CRON_URL,
                    HTTP_X_CRON_SECRET="wrong",
                )
        assert resp.status_code == 401
        mock_call.assert_not_called()
