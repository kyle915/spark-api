"""Drekker Brewing Retail Sampling template + walk-up wiring."""

from __future__ import annotations

import io

import pytest
from django.core.management import call_command
from django.test import Client, override_settings

from events.models import EventType, Product, ProductType
from recaps.management.commands.seed_drekker_recap_template import (
    RETAIL_PROGRAM,
    RETAIL_SPEC,
    RETAIL_SPEC_FIELDS,
    RETAIL_TEMPLATE_NAME,
    build_retail_spec,
)
from recaps.management.commands.setup_drekker_checkin import (
    CODE_PREFIX,
    RETAIL_BUCKETS,
    RETAIL_LABEL,
)
from tenants.management.commands.onboard_drekker_products import (
    BRAINS,
    DREKKER_PRODUCTS,
)
from tenants.tests.base import BaseGraphQLTestCase

VALID_SECRET = "test-cron-secret-value-only-for-tests"
DREKKER_SETUP_URL = "/internal/cron/setup-drekker-checkin"
DREKKER_SEED_URL = "/internal/cron/seed-drekker-recap-template"
DREKKER_PRODUCTS_URL = "/internal/cron/onboard-drekker-products"


class TestDrekkerRecapTemplateSpec:
    def test_retail_spec_mirrors_ld_retail_shape(self):
        assert [section for section, _ in RETAIL_SPEC_FIELDS] == [
            "Consumer Engagement",
            "Feedback & Account Notes",
            "Additional Insights",
        ]
        full = build_retail_spec(["Beer / Seltzer — Ectogasm"])
        assert [section for section, _ in full] == [
            "Consumer Engagement",
            "Feedback & Account Notes",
            "Additional Insights",
            "Products Sampled",
        ]
        assert sum(len(fields) for _, fields in full) == 16
        assert RETAIL_TEMPLATE_NAME == "Drekker Brewing-Retail Sampling"
        assert RETAIL_PROGRAM == "Retail Sampling"

    def test_brand_copy_is_drekker_not_liquid_death(self):
        labels = [name for _, fields in RETAIL_SPEC_FIELDS for name, *_ in fields]
        blob = " ".join(labels)
        assert "Liquid Death" not in blob
        assert "Brew Dr" not in blob
        assert "Drekker Brewing" in blob
        assert "tasing" in blob
        assert "Account Spend Amount" in labels

    def test_products_sampled_uses_catalog_options_not_hardcoded_sku_list(self):
        opts = ["Beer / Seltzer — Ectogasm", "Beer / Seltzer — Brunch Bubbz"]
        products = next(
            fields
            for section, fields in build_retail_spec(opts)
            if section == "Products Sampled"
        )
        assert products == [
            ("Products Sampled", "multiselect", True, list(opts))
        ]
        default_products = next(
            fields for section, fields in RETAIL_SPEC if section == "Products Sampled"
        )
        assert default_products[0][3] == []

    def test_no_template_image_fields_photos_are_walkup_buckets(self):
        kinds = [
            kind
            for _, fields in build_retail_spec(["x"])
            for _, kind, *_ in fields
        ]
        assert "image" not in kinds

    def test_brains_sku_has_ten_as(self):
        word = BRAINS.split()[0]
        assert word.count("a") == 10
        assert len(DREKKER_PRODUCTS) == 8


class TestDrekkerCheckinConstants:
    def test_retail_only_ld_buckets(self):
        assert CODE_PREFIX == "DR-"
        assert RETAIL_LABEL == "Retail Sampling"
        names = [b["name"] for b in RETAIL_BUCKETS]
        assert names == [
            "Table Set Up",
            "Product Display",
            "Consumer Sampling Pictures",
            "Product Receipt",
        ]


@pytest.mark.django_db(transaction=True)
class TestDrekkerSetupCommand(BaseGraphQLTestCase):
    @pytest.fixture(autouse=True)
    def setup(self, db):
        self.roles = self.setup_default_roles()
        self.user = self.get_system_user()
        self.user.is_superuser = True
        self.user.save(update_fields=["is_superuser"])
        self.tenant = self.create_tenant(
            name="Drekker Brewing", slug="drekker-brewing"
        )
        self.retail = EventType.objects.create(
            name="Retail Sampling", tenant=self.tenant, created_by=self.user
        )

    def test_apply_mints_dr_code_once(self):
        out = io.StringIO()
        call_command(
            "setup_drekker_checkin",
            stdout=out,
            tenant="drekker",
            apply=True,
        )
        log = out.getvalue()
        self.tenant.refresh_from_db()
        assert self.tenant.checkin_code
        assert self.tenant.checkin_code.startswith("DR-")
        assert "client.igniteproductions.co/checkin/" in log
        first = self.tenant.checkin_code
        call_command(
            "setup_drekker_checkin",
            stdout=io.StringIO(),
            tenant="drekker",
            apply=True,
        )
        self.tenant.refresh_from_db()
        assert self.tenant.checkin_code == first


@pytest.mark.django_db(transaction=True)
class TestDrekkerSeedCommand(BaseGraphQLTestCase):
    @pytest.fixture(autouse=True)
    def setup(self, db):
        self.roles = self.setup_default_roles()
        self.user = self.get_system_user()
        self.user.is_superuser = True
        self.user.save(update_fields=["is_superuser"])
        self.tenant = self.create_tenant(
            name="Drekker Brewing", slug="drekker-brewing"
        )
        EventType.objects.create(
            name="Retail Sampling", tenant=self.tenant, created_by=self.user
        )
        ptype = ProductType.objects.create(
            name="Beer / Seltzer", tenant=self.tenant, created_by=self.user
        )
        Product.objects.create(
            name="Ectogasm",
            product_type=ptype,
            tenant=self.tenant,
            created_by=self.user,
        )

    def test_apply_seeds_retail_from_catalog(self):
        from recaps.models import CustomField, CustomRecapTemplate

        out = io.StringIO()
        call_command(
            "seed_drekker_recap_template",
            stdout=out,
            tenant="drekker",
            apply=True,
        )
        assert "APPLIED" in out.getvalue()
        tpl = CustomRecapTemplate.objects.get(
            tenant=self.tenant, name=RETAIL_TEMPLATE_NAME
        )
        assert tpl.event_type.name == "Retail Sampling"
        assert tpl.product_samples is True
        products = CustomField.objects.get(
            custom_recap_template=tpl, name="Products Sampled"
        )
        opts = list(products.options or [])
        assert any("Ectogasm" in o for o in opts)


@pytest.mark.django_db(transaction=True)
class TestDrekkerCronEndpoints(BaseGraphQLTestCase):
    @pytest.fixture(autouse=True)
    def setup(self, db):
        self.roles = self.setup_default_roles()
        self.user = self.get_system_user()
        self.user.is_superuser = True
        self.user.save(update_fields=["is_superuser"])
        self.tenant = self.create_tenant(
            name="Drekker Brewing", slug="drekker-brewing"
        )
        self.tenant.checkin_code = "DR-CRON01"
        self.tenant.save(update_fields=["checkin_code"])
        self.client = Client()

    @override_settings(INTERNAL_CRON_SECRET=VALID_SECRET)
    def test_setup_cron_dry_run(self):
        resp = self.client.post(
            DREKKER_SETUP_URL,
            {"tenant": "drekker"},
            HTTP_X_CRON_SECRET=VALID_SECRET,
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["ok"] is True
        assert body["apply"] is False
        assert "DRY-RUN" in body["log"]
        assert "DR-CRON01" in body["log"]

    @override_settings(INTERNAL_CRON_SECRET=VALID_SECRET)
    def test_seed_cron_dry_run(self):
        resp = self.client.post(
            DREKKER_SEED_URL,
            {"tenant": "drekker"},
            HTTP_X_CRON_SECRET=VALID_SECRET,
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["ok"] is True
        assert body["applied"] is False

    @override_settings(INTERNAL_CRON_SECRET=VALID_SECRET)
    def test_products_requires_owner_email(self):
        resp = self.client.post(
            DREKKER_PRODUCTS_URL,
            {"apply": "false"},
            HTTP_X_CRON_SECRET=VALID_SECRET,
        )
        assert resp.status_code == 400
        assert resp.json().get("error") == "missing-owner-email"
