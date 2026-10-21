"""Pivot Energy Drink walk-up setup tests.

Event Activation stays. Retail Sampling is the store-demo twin, with SKU
choices read from the tenant Product catalog.
"""

from __future__ import annotations

import io

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import Client, override_settings

from events.event_confirmations import PRODUCT_CATEGORY_SEPARATOR
from events.models import Product, ProductType
from recaps.management.commands.setup_pivot_energy_checkin import (
    CODE_PREFIX,
    PHOTO_BUCKETS,
    PHOTO_BUCKETS_BY_PROGRAM,
    PRODUCT_TYPE_NAME,
    PROGRAM_NAME,
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
from recaps.products_sampled import PRODUCTS_SAMPLED_FIELD
from tenants.tests.base import BaseGraphQLTestCase

VALID_SECRET = "test-cron-secret-value-only-for-tests"
PIVOT_CRON_URL = "/internal/cron/setup-pivot-energy-checkin"

FIELD_NAMES = [f[0] for _, fields in SPEC for f in fields]
RETAIL_FIELD_NAMES = [f[0] for _, fields in RETAIL_SPEC for f in fields]


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

    def test_retail_is_the_store_demo_twin(self):
        assert RETAIL_FIELD_NAMES == [
            "Where are you working (store)?",
            "Total samples distributed",
            "Estimated store foot traffic",
            "Products Sampled",
            "Mileage",
            "Total spend on the card",
            "Key highlight (1–2 sentences) / What worked well & What didn't?",
            "What were some consumer comments that you heard?",
            "What percent of consumers had heard of or tried Pivot before?",
        ]
        sku_field = next(
            field
            for _, fields in RETAIL_SPEC
            for field in fields
            if field[0] == PRODUCTS_SAMPLED_FIELD
        )
        assert sku_field[1:] == ("multiselect", True, [])
        assert "Event Location" not in RETAIL_FIELD_NAMES
        assert "Estimated foot traffic / impressions" not in RETAIL_FIELD_NAMES

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

    def test_retail_copy_names_pivot_and_not_other_brands(self):
        blob = " ".join(
            [RETAIL_TEMPLATE_NAME, str(RETAIL_SPEC), str(PHOTO_BUCKETS)]
        ).lower()
        for forbidden in (
            "pivot energy drink",
            "white claw",
            "surge",
            "hiyo",
            "famus",
            "wook",
            "alcohol armor",
            "event location",
            "festival",
        ):
            assert forbidden not in blob
        kinds = {field[1] for _, fields in RETAIL_SPEC for field in fields}
        assert kinds <= {"text", "number", "longtext", "select", "multiselect"}
        assert "image" not in kinds

    def test_photo_buckets(self):
        assert PHOTO_BUCKETS == [
            {"name": "Consumer Sampling Pictures"},
            {"name": "Expense Receipts"},
        ]
        assert PHOTO_BUCKETS_BY_PROGRAM == {
            PROGRAM_NAME: PHOTO_BUCKETS,
            RETAIL_PROGRAM: PHOTO_BUCKETS,
        }

    def test_brand_constants(self):
        assert CODE_PREFIX == "PE-"
        assert STANDING_CODE == "PE-Y7VPYC"
        assert TEMPLATE_NAME == "Pivot Energy Drink · Event Activation Recap"
        assert RETAIL_TEMPLATE_NAME == "Pivot · Retail Sampling Recap"
        assert PROGRAM_NAME == "Event Activation"
        assert RETAIL_PROGRAM == "Retail Sampling"
        assert TENANT_SLUG == "pivot-energy-drink"
        assert TENANT_NAME == "Pivot Energy Drink"
        assert PRODUCT_TYPE_NAME == "Pivot"
        assert SKU_NAMES == (
            "Blue Razz",
            "Pink Lemonade",
            "Melon Rush",
            "Going Grape",
        )

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
        from tenants.models import Tenant

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
            RETAIL_PROGRAM: PHOTO_BUCKETS,
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

    def test_apply_adds_retail_recap_and_only_the_four_skus(self):
        from recaps.models import CustomField, CustomRecapTemplate, FileRecapCategory
        from tenants.models import Tenant

        tenant = self.create_tenant(
            name=TENANT_NAME,
            slug=TENANT_SLUG,
            request_url_name=TENANT_SLUG,
            checkin_code=STANDING_CODE,
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

        log = self._run(tenant="pivot-energy-drink", apply=True)
        assert "APPLIED" in log
        tenant.refresh_from_db()
        assert tenant.checkin_code == STANDING_CODE
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
        assert [field.name for field in retail_fields] == RETAIL_FIELD_NAMES
        assert retail.event_type.name == RETAIL_PROGRAM
        assert retail.product_samples is False
        blob = " ".join(field.name for field in retail_fields).lower()
        for forbidden in ("white claw", "surge", "hiyo", "pivot energy drink", "famus"):
            assert forbidden not in blob
        assert retail_fields[0].name == "Where are you working (store)?"
        assert (
            retail_fields[-1].name
            == "What percent of consumers had heard of or tried Pivot before?"
        )

        names = list(
            Product.objects.filter(tenant=tenant)
            .order_by("name")
            .values_list("name", flat=True)
        )
        assert names == [
            "Blue Razz",
            "Going Grape",
            "Melon Rush",
            "Pink Lemonade",
        ]
        sampled = next(
            field for field in retail_fields if field.name == PRODUCTS_SAMPLED_FIELD
        )
        assert list(sampled.options) == [
            f"{PRODUCT_TYPE_NAME}{PRODUCT_CATEGORY_SEPARATOR}{name}" for name in SKU_NAMES
        ]
        assert "House Blend" not in names

        for bucket in ("Consumer Sampling Pictures", "Expense Receipts"):
            assert FileRecapCategory.objects.filter(tenant=tenant, name=bucket).exists()
        assert tenant.checkin_photo_buckets[RETAIL_PROGRAM] == PHOTO_BUCKETS
        assert not Tenant.objects.filter(slug="pivot").exists()


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
