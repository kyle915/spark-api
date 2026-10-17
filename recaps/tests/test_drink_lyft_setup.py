"""LYT (drink-lyft) Event Activation walk-up setup tests."""

from __future__ import annotations

import io
from unittest.mock import patch

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import Client, override_settings

from recaps.management.commands.setup_drink_lyft_checkin import (
    CODE_PREFIX,
    EVENT_SPEC_FIELDS,
    PHOTO_BUCKETS,
    PRODUCTS,
    PROGRAM_NAME,
    TEMPLATE_NAME,
    TENANT_NAME,
    TENANT_SLUG,
    build_spec,
)
from tenants.tests.base import BaseGraphQLTestCase

VALID_SECRET = "test-cron-secret-value-only-for-tests"
CRON_URL = "/internal/cron/setup-drink-lyft-checkin"

# 1x1 PNG. ImageField validates through Pillow.
PNG_BYTES = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01"
    b"\x00\x00\x00\x01\x08\x06\x00\x00\x00\x1f\x15\xc4\x89"
    b"\x00\x00\x00\nIDATx\x9cc\x00\x01\x00\x00\x05\x00\x01\r\n-\xb4"
    b"\x00\x00\x00\x00IEND\xaeB`\x82"
)

FIELD_NAMES = [name for _, fields in build_spec([]) for name, *_ in fields]


def _fake_download(url: str):
    return ("lyt.png", PNG_BYTES)


class TestLytSpec:
    def test_mirrors_ld_event_questions_with_lyt_labels(self):
        assert FIELD_NAMES == [
            "How many consumers would be willing to purchase the product "
            "after tasting it?",
            "How many consumers that were engaged with knew about LYT product/brand?",
            "How many consumers had tried a LYT flavor before?",
            "How many TOTAL consumers did you sample?",
            "Demographics",
            "What were the top 5 frequently asked questions you received "
            "from consumers?",
            "Helpful feedback",
            "Products Sampled",
        ]

    def test_zero_liquid_death_or_other_brand_copy(self):
        blob = str(EVENT_SPEC_FIELDS) + str(PHOTO_BUCKETS) + TEMPLATE_NAME + TENANT_NAME
        for forbidden in (
            "Liquid Death",
            "White Claw",
            "Dude Wipes",
            "Kalshi",
            "DAOU",
            "tasing",
        ):
            assert forbidden.lower() not in blob.lower()

    def test_photo_buckets_match_ld_event(self):
        assert [bucket["name"] for bucket in PHOTO_BUCKETS] == [
            "Activation Set Up",
            "Consumer Sampling Pictures",
            "Expense Receipts (Parking)",
        ]
        sampling = PHOTO_BUCKETS[1]
        assert sampling["min"] == 8
        assert "capture" not in sampling
        assert "camera" not in str(sampling).lower()

    def test_catalog_is_the_six_storefront_12_packs(self):
        assert [row["name"] for row in PRODUCTS] == [
            "Island Flow 12-Pack",
            "Grape Boost 12-Pack",
            "Cherry Tempo 12-Pack",
            "Orange Blast 12-Pack",
            "Citrus Mist 12-Pack",
            "Cyclone Punch 12-Pack",
        ]
        assert [row["sku"] for row in PRODUCTS] == [
            "LYT22Z12PIFCHT",
            "LYT22Z12PGBCHT",
            "LYT22Z12PCYTCHT",
            "LYT22Z12POBCHT",
            "LYT22Z12PZCMCHT",
            "LYT22Z12PZCPCHT",
        ]
        assert [row["product_type"] for row in PRODUCTS] == [
            "LYT",
            "LYT",
            "LYT",
            "LYT",
            "LYT Zero",
            "LYT Zero",
        ]
        for row in PRODUCTS:
            assert len(row["name"]) <= 50
            assert row["image_url"].startswith("https://cdn.shopify.com/")
            assert "$" not in row["name"]
            assert "price" not in row

    def test_brand_constants(self):
        assert CODE_PREFIX == "LYT-"
        assert TEMPLATE_NAME == "LYT-Event Activation"
        assert PROGRAM_NAME == "Event Activation"
        assert TENANT_SLUG == "drink-lyft"
        assert TENANT_NAME == "LYT"

    def test_kinds_are_canonical_and_not_image(self):
        kinds = {
            kind
            for _, fields in build_spec(["LYT — Island Flow 12-Pack"])
            for _, kind, *_ in fields
        }
        assert kinds <= {"text", "number", "longtext", "select", "multiselect"}
        assert "image" not in kinds


@pytest.mark.django_db(transaction=True)
class TestLytSetupCommand(BaseGraphQLTestCase):
    @pytest.fixture(autouse=True)
    def setup(self, db):
        self.roles = self.setup_default_roles()
        self.user = self.get_system_user()
        self.user.is_superuser = True
        self.user.save(update_fields=["is_superuser"])

    def _run(self, **kw):
        out = io.StringIO()
        with patch(
            "recaps.management.commands.setup_drink_lyft_checkin.download_product_image",
            side_effect=_fake_download,
        ):
            call_command("setup_drink_lyft_checkin", stdout=out, **kw)
        return out.getvalue()

    def test_dry_run_creates_nothing(self):
        from events.models import Product
        from recaps.models import CustomRecapTemplate
        from tenants.models import Tenant

        log = self._run(tenant="drink-lyft")
        assert "DRY-RUN" in log
        assert TENANT_NAME in log
        assert "Island Flow 12-Pack" in log
        assert not Tenant.objects.filter(slug=TENANT_SLUG).exists()
        assert not CustomRecapTemplate.objects.filter(name=TEMPLATE_NAME).exists()
        assert not Product.objects.filter(name="Island Flow 12-Pack").exists()

    def test_substring_does_not_attach_another_tenant(self):
        from tenants.models import Tenant

        other = self.create_tenant(
            name="Not Drink Lyft Extra",
            slug="drink-lyft-extra",
            request_url_name="drink-lyft-extra",
        )
        log = self._run(tenant="drink-lyft")
        assert "DRY-RUN" in log
        other.refresh_from_db()
        assert other.checkin_code in (None, "")
        assert not Tenant.objects.filter(slug=TENANT_SLUG).exists()

    def test_unrelated_tenant_needle_is_rejected(self):
        with pytest.raises(CommandError, match="not the LYT tenant"):
            self._run(tenant="torch")

    def test_apply_creates_tenant_catalog_template_and_code(self):
        from events.models import Product, RequestStatus
        from recaps.models import CustomField, CustomRecapTemplate, FileRecapCategory
        from tenants.models import Tenant

        log = self._run(tenant="drink-lyft", apply=True)
        assert "APPLIED" in log
        tenant = Tenant.objects.get(slug=TENANT_SLUG)
        assert tenant.name == TENANT_NAME
        assert tenant.request_url_name == TENANT_SLUG
        assert tenant.auto_approve_requests is False
        assert tenant.checkin_code and tenant.checkin_code.startswith("LYT-")
        assert tenant.checkin_location_mode == Tenant.CHECKIN_LOCATION_ADDRESS
        assert tenant.checkin_photo_buckets == {PROGRAM_NAME: PHOTO_BUCKETS}
        assert tenant.checkin_event_type is not None
        assert tenant.checkin_event_type.name == PROGRAM_NAME
        assert list(tenant.checkin_event_types.values_list("name", flat=True)) == [
            PROGRAM_NAME
        ]
        pending = RequestStatus.objects.get(tenant=tenant, name="Pending")
        assert pending.is_default is True

        names = set(
            Product.objects.filter(tenant=tenant).values_list("name", flat=True)
        )
        assert names == {row["name"] for row in PRODUCTS}
        for product in Product.objects.filter(tenant=tenant):
            assert product.image
            assert product.description.startswith("Shopify SKU ")
            assert "$" not in (product.description or "")

        for bucket in PHOTO_BUCKETS:
            assert FileRecapCategory.objects.filter(
                tenant=tenant, name=bucket["name"]
            ).exists()

        tpl = CustomRecapTemplate.objects.get(tenant=tenant, name=TEMPLATE_NAME)
        assert tpl.product_samples is True
        assert tpl.event_type.name == PROGRAM_NAME
        field_names = list(
            CustomField.objects.filter(custom_recap_template=tpl)
            .order_by("recap_section__order", "order", "id")
            .values_list("name", flat=True)
        )
        assert field_names == FIELD_NAMES
        sampled = CustomField.objects.get(
            custom_recap_template=tpl, name="Products Sampled"
        )
        assert "Island Flow 12-Pack" in " ".join(sampled.options)
        assert "Liquid Death" not in " ".join(sampled.options)

        code = tenant.checkin_code
        product_count = Product.objects.filter(tenant=tenant).count()
        log2 = self._run(tenant="drink-lyft", apply=True)
        tenant.refresh_from_db()
        assert tenant.checkin_code == code
        assert "already set" in log2
        assert Product.objects.filter(tenant=tenant).count() == product_count

    def test_exact_request_url_name_extends_existing_and_does_not_remint(self):
        from tenants.models import Tenant

        existing = self.create_tenant(
            name="Drink Lyft",
            slug="lyt-legacy",
            request_url_name=TENANT_SLUG,
            checkin_code="LYT-KEEPME",
        )
        log = self._run(tenant="drink-lyft", apply=True)
        assert "APPLIED" in log
        existing.refresh_from_db()
        assert existing.checkin_code == "LYT-KEEPME"
        assert "already set" in log
        assert Tenant.objects.filter(slug=TENANT_SLUG).count() == 0
        assert Tenant.objects.filter(request_url_name=TENANT_SLUG).count() == 1


@pytest.mark.django_db
class TestLytSetupCronView:
    def test_valid_secret_fires_command_dry_run_by_default(self):
        client = Client()
        with override_settings(INTERNAL_CRON_SECRET=VALID_SECRET):
            with patch("digest.cron_views.call_command") as mock_call:
                resp = client.post(
                    CRON_URL,
                    {"tenant": "drink-lyft"},
                    HTTP_X_CRON_SECRET=VALID_SECRET,
                )
        assert resp.status_code == 200
        body = resp.json()
        assert body["ok"] is True
        assert body["apply"] is False
        mock_call.assert_called_once()
        assert mock_call.call_args[0][0] == "setup_drink_lyft_checkin"
        assert "apply" not in mock_call.call_args.kwargs

    def test_bad_secret_returns_401(self):
        client = Client()
        with override_settings(INTERNAL_CRON_SECRET=VALID_SECRET):
            with patch("digest.cron_views.call_command") as mock_call:
                resp = client.post(CRON_URL, HTTP_X_CRON_SECRET="wrong")
        assert resp.status_code == 401
        mock_call.assert_not_called()
