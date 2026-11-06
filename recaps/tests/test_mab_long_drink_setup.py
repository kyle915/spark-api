"""Mark Anthony Brands · Total Wine Long Drink check-in."""

from __future__ import annotations

import io

import pytest
from django.core.management import call_command
from django.test import Client, override_settings

from recaps.management.commands.long_drink_products import (
    LONG_DRINK_PRODUCTS,
    PRODUCT_TYPE,
    long_drink_product_options,
)
from recaps.management.commands.setup_mab_long_drink_checkin import (
    RETAIL_SPEC,
    RETAIL_TEMPLATE_NAME,
    STANDING_CODE,
)
from recaps.products_sampled import (
    PRODUCTS_SAMPLED_TYPES_KEY,
    resolve_products_sampled_options,
)
from tenants.tests.base import BaseGraphQLTestCase

VALID_SECRET = "test-cron-secret-value-only-for-tests"
SETUP_URL = "/internal/cron/setup-mab-long-drink-checkin"


class TestLongDrinkSpec:
    def test_nine_skus_match_mab_catalog_flavors(self):
        assert len(LONG_DRINK_PRODUCTS) == 9
        assert len(long_drink_product_options()) == 9
        assert all(
            opt.startswith(f"{PRODUCT_TYPE} — ")
            for opt in long_drink_product_options()
        )

    def test_retail_spec_is_torch_shaped_long_drink_copy(self):
        labels = [name for _, fields in RETAIL_SPEC for name, *_ in fields]
        blob = " ".join(labels)
        assert "Torch" not in blob
        assert "THC" not in blob
        assert "Long Drink" in blob
        assert "Total number of consumers sampled" in labels
        assert "People engaged" in labels
        assert "Dry demo? (no product tasted)" in labels
        assert "Sample format" in labels
        assert "Products Sampled" in labels
        assert RETAIL_TEMPLATE_NAME == "Mark Anthony Brands-Retail Sampling"
        assert STANDING_CODE == "MAB-V696HM"

    def test_no_template_image_fields(self):
        kinds = [kind for _, fields in RETAIL_SPEC for _, kind, *_ in fields]
        assert "image" not in kinds


@pytest.mark.django_db(transaction=True)
class TestProductsSampledTypeFilter(BaseGraphQLTestCase):
    @pytest.fixture(autouse=True)
    def setup(self, db):
        from events.models import Product, ProductType

        self.roles = self.setup_default_roles()
        self.user = self.get_system_user()
        self.tenant = self.create_tenant(
            name="Mark Anthony Brands", slug="mark-anthony-brands"
        )
        claw = ProductType.objects.create(
            tenant=self.tenant, name="White Claw", created_by=self.user
        )
        ld = ProductType.objects.create(
            tenant=self.tenant, name=PRODUCT_TYPE, created_by=self.user
        )
        Product.objects.create(
            tenant=self.tenant,
            product_type=claw,
            name="Black Cherry",
            created_by=self.user,
        )
        Product.objects.create(
            tenant=self.tenant,
            product_type=ld,
            name="Traditional Citrus",
            created_by=self.user,
        )
        Product.objects.create(
            tenant=self.tenant,
            product_type=ld,
            name="Cranberry",
            created_by=self.user,
        )

    def test_type_filter_returns_only_long_drink(self):
        all_opts = resolve_products_sampled_options(
            field_name="Products Sampled",
            tenant=self.tenant,
            stored=[],
        )
        assert any(o.startswith("White Claw") for o in all_opts)
        filtered = resolve_products_sampled_options(
            field_name="Products Sampled",
            tenant=self.tenant,
            stored=[],
            product_types=[PRODUCT_TYPE],
        )
        assert filtered == [
            f"{PRODUCT_TYPE} — Cranberry",
            f"{PRODUCT_TYPE} — Traditional Citrus",
        ]


@pytest.mark.django_db(transaction=True)
class TestMabLongDrinkSetupCommand(BaseGraphQLTestCase):
    @pytest.fixture(autouse=True)
    def setup(self, db):
        from events.models import EventType

        self.roles = self.setup_default_roles()
        self.user = self.get_system_user()
        self.user.email = "kyle@igniteproductions.co"
        self.user.is_superuser = True
        self.user.save(update_fields=["email", "is_superuser"])
        self.tenant = self.create_tenant(
            name="Mark Anthony Brands", slug="mark-anthony-brands"
        )
        EventType.objects.create(
            name="Retail Sampling", tenant=self.tenant, created_by=self.user
        )

    def test_dry_run_writes_nothing(self):
        from recaps.models import CustomRecapTemplate

        out = io.StringIO()
        call_command(
            "setup_mab_long_drink_checkin",
            tenant="mark anthony",
            owner_email=self.user.email,
            skip_images=True,
            stdout=out,
        )
        log = out.getvalue()
        assert "DRY-RUN" in log
        assert "Long Drink" in log
        assert not CustomRecapTemplate.objects.filter(
            tenant=self.tenant, name=RETAIL_TEMPLATE_NAME
        ).exists()

    def test_apply_rewrites_retail_and_keeps_standing_code(self):
        from events.models import Product
        from recaps.models import CustomField, CustomRecapTemplate

        out = io.StringIO()
        call_command(
            "setup_mab_long_drink_checkin",
            tenant="mark anthony",
            owner_email=self.user.email,
            code=STANDING_CODE,
            skip_images=True,
            apply=True,
            stdout=out,
        )
        self.tenant.refresh_from_db()
        assert self.tenant.checkin_code == STANDING_CODE
        tpl = CustomRecapTemplate.objects.get(
            tenant=self.tenant, name=RETAIL_TEMPLATE_NAME
        )
        assert tpl.layout.get(PRODUCTS_SAMPLED_TYPES_KEY) == [PRODUCT_TYPE]
        labels = list(
            CustomField.objects.filter(custom_recap_template=tpl).values_list(
                "name", flat=True
            )
        )
        assert any("knew about Long Drink" in n for n in labels)
        assert any("willing to purchase Long Drink" in n for n in labels)
        assert (
            Product.objects.filter(
                tenant=self.tenant, product_type__name=PRODUCT_TYPE
            ).count()
            == 9
        )


@pytest.mark.django_db
class TestMabLongDrinkCronView:
    def test_valid_secret_fires_setup_command(self):
        client = Client()
        with override_settings(INTERNAL_CRON_SECRET=VALID_SECRET):
            from unittest.mock import patch

            with patch("digest.cron_views.call_command") as mock_call:
                resp = client.post(
                    SETUP_URL,
                    {
                        "tenant": "mark anthony",
                        "owner_email": "kyle@igniteproductions.co",
                        "skip_images": "true",
                        "apply": "false",
                    },
                    HTTP_X_CRON_SECRET=VALID_SECRET,
                )
        assert resp.status_code == 200
        assert resp.json()["ok"] is True
        assert mock_call.call_args[0][0] == "setup_mab_long_drink_checkin"
