"""DAOU / Treasury Wine Estates Event Activation walk-up setup."""

from __future__ import annotations

import io

import pytest
from django.core.management import call_command
from django.test import Client, override_settings

from recaps.management.commands.seed_daou_recap_template import (
    EVENT_PROGRAM,
    EVENT_SPEC,
    EVENT_SPEC_FIELDS,
    EVENT_TEMPLATE_NAME,
    build_event_spec,
)
from recaps.management.commands.setup_daou_checkin import (
    ACTIVATION_BUCKETS,
    CODE_PREFIX,
    EVENT_LABEL,
)
from tenants.management.commands.onboard_daou_products import (
    DAOU_PRODUCTS,
    TENANT_NAME,
    TENANT_SLUG,
)
from tenants.tests.base import BaseGraphQLTestCase

VALID_SECRET = "test-cron-secret-value-only-for-tests"
DAOU_SETUP_URL = "/internal/cron/setup-daou-checkin"
DAOU_SEED_URL = "/internal/cron/seed-daou-recap-template"
DAOU_ONBOARD_URL = "/internal/cron/onboard-daou-products"


class TestDaouRecapTemplateSpec:
    def test_event_spec_mirrors_ld_event_activation(self):
        assert [section for section, _ in EVENT_SPEC_FIELDS] == [
            "Consumer Engagement",
            "Feedback & Account Notes",
        ]
        full = build_event_spec(["DAOU Discovery Rosé 2025"])
        assert [section for section, _ in full] == [
            "Consumer Engagement",
            "Feedback & Account Notes",
            "Products Sampled",
        ]
        assert sum(len(fields) for _, fields in full) == 8
        assert EVENT_TEMPLATE_NAME == "DAOU-Event Activation"
        assert EVENT_PROGRAM == "Event Activation"

    def test_brand_copy_is_daou_not_liquid_death(self):
        labels = [name for _, fields in EVENT_SPEC_FIELDS for name, *_ in fields]
        blob = " ".join(labels)
        assert "Liquid Death" not in blob
        assert "Neutonic" not in blob
        assert "DAOU" in blob
        assert "tasing" in blob
        assert "TOTAL consumers" in blob

    def test_products_sampled_uses_catalog_options(self):
        opts = list(DAOU_PRODUCTS)
        products = next(
            fields
            for section, fields in build_event_spec(opts)
            if section == "Products Sampled"
        )
        assert products == [
            ("Products Sampled", "multiselect", True, list(opts))
        ]
        default_products = next(
            fields for section, fields in EVENT_SPEC if section == "Products Sampled"
        )
        assert default_products[0][3] == []

    def test_no_template_image_fields(self):
        kinds = [
            kind
            for _, fields in build_event_spec(["x"])
            for _, kind, *_ in fields
        ]
        assert "image" not in kinds


class TestDaouPhotoBucketSpec:
    def test_activation_buckets_mirror_ld(self):
        assert [b["name"] for b in ACTIVATION_BUCKETS] == [
            "Activation Set Up",
            "Consumer Sampling Pictures",
            "Expense Receipts (Parking)",
        ]
        assert ACTIVATION_BUCKETS[1].get("min") == 8
        assert CODE_PREFIX == "DAOU-"
        assert EVENT_LABEL == "Event Activation"
        assert TENANT_SLUG == "treasury-wine-estates"
        assert TENANT_NAME == "Treasury Wine Estates"
        assert len(DAOU_PRODUCTS) == 4


@pytest.mark.django_db(transaction=True)
class TestDaouSetupCommand(BaseGraphQLTestCase):
    @pytest.fixture(autouse=True)
    def setup(self, db):
        self.roles = self.setup_default_roles()
        self.user = self.get_system_user()
        self.user.is_superuser = True
        self.user.save(update_fields=["is_superuser"])
        self.tenant = self.create_tenant(
            name=TENANT_NAME, slug=TENANT_SLUG
        )

    def _run(self, **kw):
        out = io.StringIO()
        call_command("setup_daou_checkin", stdout=out, **kw)
        return out.getvalue()

    def test_dry_run_creates_nothing(self):
        from events.models import EventType
        from recaps.models import FileRecapCategory

        log = self._run(tenant="treasury")
        assert "DRY-RUN" in log
        assert "Activation Set Up" in log
        assert not EventType.objects.filter(
            tenant=self.tenant, name="Event Activation"
        ).exists()
        assert not FileRecapCategory.objects.filter(
            tenant=self.tenant, name="Activation Set Up"
        ).exists()
        self.tenant.refresh_from_db()
        assert not (self.tenant.checkin_code or "").strip()

    def test_apply_mints_code_once_and_wires_event_activation(self):
        from ambassadors import checkin_web
        from events.models import EventType
        from recaps.models import FileRecapCategory

        log = self._run(tenant="treasury", apply=True)
        assert "APPLIED" in log
        self.tenant.refresh_from_db()
        code = self.tenant.checkin_code
        assert code and code.startswith("DAOU-")
        assert self.tenant.checkin_event_type.name == EVENT_LABEL
        offered = list(
            self.tenant.checkin_event_types.order_by("id").values_list(
                "name", flat=True
            )
        )
        assert offered == [EVENT_LABEL]
        buckets = self.tenant.checkin_photo_buckets
        assert [b["name"] for b in buckets["Event Activation"]] == [
            b["name"] for b in ACTIVATION_BUCKETS
        ]
        for name in [b["name"] for b in ACTIVATION_BUCKETS]:
            assert FileRecapCategory.objects.filter(
                tenant=self.tenant, name=name
            ).exists()
        activation = EventType.objects.get(
            tenant=self.tenant, name="Event Activation"
        )
        selectable = checkin_web.selectable_event_types(self.tenant)
        assert [et.name for et in selectable] == [EVENT_LABEL]
        assert activation in selectable

        # Never remint.
        self._run(tenant="treasury", apply=True)
        self.tenant.refresh_from_db()
        assert self.tenant.checkin_code == code


@pytest.mark.django_db(transaction=True)
class TestDaouSeedCommand(BaseGraphQLTestCase):
    @pytest.fixture(autouse=True)
    def setup(self, db):
        from events.models import Product, ProductType

        self.roles = self.setup_default_roles()
        self.user = self.get_system_user()
        self.user.is_superuser = True
        self.user.save(update_fields=["is_superuser"])
        self.tenant = self.create_tenant(
            name=TENANT_NAME, slug=TENANT_SLUG
        )
        ptype = ProductType.objects.create(
            name="Discovery Collection",
            tenant=self.tenant,
            created_by=self.user,
        )
        for name in DAOU_PRODUCTS:
            Product.objects.create(
                name=name,
                product_type=ptype,
                tenant=self.tenant,
                created_by=self.user,
            )

    def _run(self, **kw):
        out = io.StringIO()
        call_command("seed_daou_recap_template", stdout=out, **kw)
        return out.getvalue()

    def test_apply_seeds_event_activation_from_catalog(self):
        from recaps.models import CustomField, CustomRecapTemplate

        log = self._run(tenant="treasury", apply=True)
        assert "APPLIED" in log
        tpl = CustomRecapTemplate.objects.get(
            tenant=self.tenant, name=EVENT_TEMPLATE_NAME
        )
        assert tpl.event_type.name == "Event Activation"
        assert tpl.product_samples is True
        fields = list(
            CustomField.objects.filter(custom_recap_template=tpl).order_by("order")
        )
        assert len(fields) == 8
        products = next(f for f in fields if f.name == "Products Sampled")
        opts = list(products.options or [])
        for sku in DAOU_PRODUCTS:
            assert any(sku in o for o in opts)


@pytest.mark.django_db(transaction=True)
class TestDaouOnboardProducts(BaseGraphQLTestCase):
    @pytest.fixture(autouse=True)
    def setup(self, db):
        self.roles = self.setup_default_roles()
        self.user = self.get_system_user()
        self.user.email = "kyle@igniteproductions.co"
        self.user.is_superuser = True
        self.user.save(update_fields=["email", "is_superuser"])

    def test_create_tenant_and_skus(self):
        from events.models import Product
        from tenants.models import Tenant

        out = io.StringIO()
        call_command(
            "onboard_daou_products",
            owner_email="kyle@igniteproductions.co",
            apply=True,
            create_tenant=True,
            stdout=out,
        )
        log = out.getvalue()
        assert "APPLIED" in log
        tenant = Tenant.objects.get(slug=TENANT_SLUG)
        assert tenant.name == TENANT_NAME
        assert Product.objects.filter(tenant=tenant).count() == len(DAOU_PRODUCTS)


@pytest.mark.django_db(transaction=True)
class TestDaouCronEndpoints(BaseGraphQLTestCase):
    @pytest.fixture(autouse=True)
    def setup(self, db):
        self.roles = self.setup_default_roles()
        self.user = self.get_system_user()
        self.user.is_superuser = True
        self.user.save(update_fields=["is_superuser"])
        self.tenant = self.create_tenant(
            name=TENANT_NAME, slug=TENANT_SLUG
        )
        self.tenant.checkin_code = "DAOU-EXIST1"
        self.tenant.save(update_fields=["checkin_code"])
        self.client = Client()

    @override_settings(INTERNAL_CRON_SECRET=VALID_SECRET)
    def test_setup_cron_dry_run(self):
        resp = self.client.post(
            DAOU_SETUP_URL,
            {"tenant": "treasury"},
            HTTP_X_CRON_SECRET=VALID_SECRET,
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["ok"] is True
        assert body["apply"] is False
        assert "DRY-RUN" in body["log"]
        assert "DAOU-EXIST1" in body["log"]

    @override_settings(INTERNAL_CRON_SECRET=VALID_SECRET)
    def test_seed_cron_dry_run(self):
        resp = self.client.post(
            DAOU_SEED_URL,
            {"tenant": "treasury"},
            HTTP_X_CRON_SECRET=VALID_SECRET,
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["ok"] is True
        assert body["applied"] is False
        assert "DRY-RUN" in body["report"]
        assert "Event Activation" in body["report"]

    @override_settings(INTERNAL_CRON_SECRET=VALID_SECRET)
    def test_setup_cron_requires_secret(self):
        resp = self.client.post(DAOU_SETUP_URL, {"tenant": "treasury"})
        assert resp.status_code in (401, 403)
