"""Brew Dr. Kombucha product catalog + Products Sampled rewrite."""

from __future__ import annotations

import io
import json

import pytest
from django.core.management import call_command
from django.test import Client, override_settings

from events.models import Product, ProductType
from tenants.management.commands.onboard_brew_dr_products import (
    BREW_DR_PRODUCTS,
    LEGACY_CANS,
    PRODUCT_TYPE_NAME,
    _has_full_new_set,
    _parse_sampled_list,
    _sku_key,
)
from tenants.tests.base import BaseGraphQLTestCase

VALID_SECRET = "test-cron-secret-value-only-for-tests"
BREW_DR_PRODUCTS_URL = "/internal/cron/onboard-brew-dr-products"


class TestBrewDrSkuHelpers:
    def test_sku_key_strips_type_prefix(self):
        assert _sku_key("Kombucha — Unsweetened") == "unsweetened"
        assert _sku_key("Unsweetened") == "unsweetened"

    def test_has_full_new_set(self):
        labels = [f"Kombucha — {n}" for n in BREW_DR_PRODUCTS]
        assert _has_full_new_set(labels, BREW_DR_PRODUCTS)
        assert not _has_full_new_set(list(LEGACY_CANS), BREW_DR_PRODUCTS)
        assert not _has_full_new_set([], BREW_DR_PRODUCTS)

    def test_parse_sampled_list(self):
        assert _parse_sampled_list(json.dumps(["Clear Mind", "Love"])) == [
            "Clear Mind",
            "Love",
        ]
        assert _parse_sampled_list("") == []
        assert _parse_sampled_list("Clear Mind") == ["Clear Mind"]


@pytest.mark.django_db(transaction=True)
class TestOnboardBrewDrProducts(BaseGraphQLTestCase):
    @pytest.fixture(autouse=True)
    def setup(self, db):
        from events.models import EventType

        self.roles = self.setup_default_roles()
        self.owner = self.get_system_user()
        self.owner.email = "kyle@igniteproductions.co"
        self.owner.save(update_fields=["email"])
        self.tenant = self.create_tenant(
            name="Brew Dr. Kombucha", slug="brew-dr-kombucha"
        )
        self._EventType = EventType

    def test_apply_creates_exact_catalog(self):
        out = io.StringIO()
        call_command(
            "onboard_brew_dr_products",
            owner_email=self.owner.email,
            apply=True,
            skip_migrate=True,
            stdout=out,
        )
        names = list(
            Product.objects.filter(tenant=self.tenant)
            .order_by("id")
            .values_list("name", flat=True)
        )
        assert names == BREW_DR_PRODUCTS
        assert ProductType.objects.filter(
            tenant=self.tenant, name=PRODUCT_TYPE_NAME
        ).exists()
        assert "Applied" in out.getvalue() or "APPLIED" in out.getvalue().upper()

    def test_apply_retires_legacy_products(self):
        ptype = ProductType.objects.create(
            name=PRODUCT_TYPE_NAME,
            tenant=self.tenant,
            created_by=self.owner,
        )
        Product.objects.create(
            name="Clear Mind",
            product_type=ptype,
            tenant=self.tenant,
            created_by=self.owner,
        )
        call_command(
            "onboard_brew_dr_products",
            owner_email=self.owner.email,
            apply=True,
            skip_migrate=True,
        )
        names = set(
            Product.objects.filter(tenant=self.tenant).values_list("name", flat=True)
        )
        assert names == set(BREW_DR_PRODUCTS)
        assert "Clear Mind" not in names

    def test_rewrites_legacy_sampled_multiselect(self):
        from django.utils import timezone

        from events.models import Event
        from recaps.models import (
            CustomField,
            CustomFieldValue,
            CustomRecap,
            CustomRecapFieldType,
            CustomRecapTemplate,
            RecapSection,
        )

        ptype = ProductType.objects.create(
            name=PRODUCT_TYPE_NAME,
            tenant=self.tenant,
            created_by=self.owner,
        )
        for name in BREW_DR_PRODUCTS:
            Product.objects.create(
                name=name,
                product_type=ptype,
                tenant=self.tenant,
                created_by=self.owner,
            )

        et = self._EventType.objects.create(
            name="Retail Sampling", tenant=self.tenant, created_by=self.owner
        )
        event = Event.objects.create(
            name="Brew Dr rewrite fixture",
            tenant=self.tenant,
            address="1 Test St",
            event_type=et,
            date=timezone.now(),
            created_by=self.owner,
        )
        tpl = CustomRecapTemplate.objects.create(
            tenant=self.tenant,
            name="Brew Dr. Kombucha-Retail Sampling",
            event_type=et,
            product_samples=True,
            created_by=self.owner,
        )
        section = RecapSection.objects.create(
            tenant=self.tenant, name="Products Sampled", created_by=self.owner
        )
        ft = CustomRecapFieldType.objects.create(
            name="multiselect", created_by=self.owner
        )
        field = CustomField.objects.create(
            custom_recap_template=tpl,
            recap_section=section,
            custom_field_type=ft,
            name="Products Sampled",
            required=True,
            options=list(LEGACY_CANS),
            created_by=self.owner,
        )
        recap = CustomRecap.objects.create(
            tenant=self.tenant,
            custom_recap_template=tpl,
            event=event,
            created_by=self.owner,
            name="legacy sampled",
        )
        cfv = CustomFieldValue.objects.create(
            custom_recap=recap,
            custom_field=field,
            value=json.dumps(["Clear Mind", "Love"]),
            created_by=self.owner,
        )

        call_command(
            "onboard_brew_dr_products",
            owner_email=self.owner.email,
            apply=True,
        )
        cfv.refresh_from_db()
        parsed = json.loads(cfv.value)
        assert _has_full_new_set(parsed, BREW_DR_PRODUCTS)
        field.refresh_from_db()
        assert len(field.options) == 6


@pytest.mark.django_db(transaction=True)
class TestOnboardBrewDrProductsCron(BaseGraphQLTestCase):
    @pytest.fixture(autouse=True)
    def setup(self, db):
        self.roles = self.setup_default_roles()
        self.owner = self.get_system_user()
        self.owner.email = "kyle@igniteproductions.co"
        self.owner.save(update_fields=["email"])
        self.tenant = self.create_tenant(
            name="Brew Dr. Kombucha", slug="brew-dr-kombucha"
        )
        self.client = Client()

    def test_command_dry_run_creates_nothing(self):
        out = io.StringIO()
        call_command(
            "onboard_brew_dr_products",
            owner_email=self.owner.email,
            tenant="brew-dr-kombucha",
            skip_migrate=True,
            stdout=out,
        )
        assert "DRY-RUN" in out.getvalue()
        assert Product.objects.filter(tenant=self.tenant).count() == 0

    @override_settings(INTERNAL_CRON_SECRET=VALID_SECRET)
    def test_requires_owner_email(self):
        resp = self.client.post(
            BREW_DR_PRODUCTS_URL,
            {"apply": "false"},
            HTTP_X_CRON_SECRET=VALID_SECRET,
        )
        assert resp.status_code == 400
        assert resp.json().get("error") == "missing-owner-email"
