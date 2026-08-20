"""Sipli Event Sampling Recap: seeder shape + walk-up apply.

Pins the templates to the client's "Sipli // Event Sampling Recap" PDF
(Maria Vorheier, #13, 08/17/2026) so a future edit is deliberate, and
proves the standing-link command can create the tenant from scratch with
Retail Sampling vs Event Activation on one code.
"""

from __future__ import annotations

import io

import pytest
from django.core.management import call_command
from django.test import Client, override_settings

from recaps.management.commands.setup_sipli_checkin import (
    CODE_PREFIX,
    EVENT_PROGRAM,
    EVENT_SPEC,
    EVENT_TEMPLATE_NAME,
    PHOTO_BUCKETS,
    PHOTO_BUCKETS_BY_PROGRAM,
    PRODUCT_OPTIONS,
    RETAIL_EXTRA,
    RETAIL_PROGRAM,
    RETAIL_SPEC,
    RETAIL_TEMPLATE_NAME,
    TENANT_NAME,
    TENANT_SLUG,
)
from tenants.tests.base import BaseGraphQLTestCase

VALID_SECRET = "test-cron-secret-value-only-for-tests"
SIPLI_CRON_URL = "/internal/cron/setup-sipli-checkin"

EXCLUDED = (
    "date of sampling",
    "sampling location",
    "todays date",
    "date:",
)

EVENT_FIELD_NAMES = [f[0] for _, fields in EVENT_SPEC for f in fields]
RETAIL_EXTRA_NAMES = [f[0] for f in RETAIL_EXTRA[1]]


class TestSipliSpec:
    def test_covers_the_pdf_minus_date_and_location(self):
        assert EVENT_FIELD_NAMES == [
            "Event / Sampling Location Name",
            "Which products were sampled?",
            "How many Apple bottles were used for sampling?",
            "How many Cranberry bottles were used for sampling?",
            "How many Grape bottles were used for sampling?",
            "Number of coupons given out (BOGO coupons and free bottle coupons)",
            "Number of unique people served",
            "Number of samples served even if someone receives two or more",
            "How many consumers that were engaged with knew about Sipli product/brand?",
            "How many consumers would be willing to purchase the product after tasting it?",
            "How many consumers have tried Sipli flavors before?",
            "Demographics",
            "What were the top 5 frequently asked questions you received from consumers?",
            "Helpful feedback",
            "Any expenses / bill-backs outside of product. (E.g. Tolls, Parking, etc).",
        ]

    def test_date_and_address_are_not_on_the_form(self):
        lowered = [n.lower() for n in EVENT_FIELD_NAMES]
        for banned in EXCLUDED:
            assert not any(n == banned or n.startswith(banned + " ") for n in lowered)
        assert "sampling location" not in lowered

    def test_retail_adds_store_and_inventory_only(self):
        assert RETAIL_EXTRA_NAMES == [
            "Store Number",
            "Total Inventory Before Demo",
            "Total Inventory After Demo",
        ]
        retail_names = [f[0] for _, fields in RETAIL_SPEC for f in fields]
        assert retail_names[:3] == RETAIL_EXTRA_NAMES
        assert retail_names[3:] == EVENT_FIELD_NAMES
        assert RETAIL_EXTRA_NAMES[0] not in EVENT_FIELD_NAMES

    def test_products_are_the_three_pdf_juices(self):
        products_field = next(
            f for f in EVENT_SPEC[1][1] if f[0] == "Which products were sampled?"
        )
        assert products_field[1] == "multiselect"
        assert products_field[3] == PRODUCT_OPTIONS
        assert PRODUCT_OPTIONS == [
            "100% Apple Juice",
            "100% Grape Juice",
            "100% Cranberry Juice",
        ]

    def test_photo_buckets_match_the_pdf_labels(self):
        assert [b["name"] for b in PHOTO_BUCKETS] == [
            "Consumer Sampling Pictures",
            "Activation Set Up",
            "Expense Receipts",
        ]
        assert PHOTO_BUCKETS_BY_PROGRAM[EVENT_PROGRAM] == PHOTO_BUCKETS
        assert PHOTO_BUCKETS_BY_PROGRAM[RETAIL_PROGRAM] == PHOTO_BUCKETS

    def test_kinds_are_canonical_and_not_image(self):
        """Photos are buckets, not template image fields — two image
        fields would share one grid on the walk-up page."""
        kinds = {f[1] for spec in (EVENT_SPEC, RETAIL_SPEC) for _, fields in spec for f in fields}
        assert kinds <= {"text", "number", "longtext", "select", "multiselect"}
        assert "image" not in kinds

    def test_code_prefix_is_brand_scoped(self):
        assert CODE_PREFIX == "SIP-"
        assert EVENT_TEMPLATE_NAME.startswith("Sipli")
        assert RETAIL_TEMPLATE_NAME.startswith("Sipli")
        assert TENANT_SLUG == "sipli"
        assert TENANT_NAME == "Sipli"


@pytest.mark.django_db(transaction=True)
class TestSipliSetupCommand(BaseGraphQLTestCase):
    @pytest.fixture(autouse=True)
    def setup(self, db):
        self.roles = self.setup_default_roles()
        self.user = self.get_system_user()
        self.user.is_superuser = True
        self.user.save(update_fields=["is_superuser"])

    def _run(self, **kw):
        out = io.StringIO()
        call_command("setup_sipli_checkin", stdout=out, **kw)
        return out.getvalue()

    def test_dry_run_creates_nothing(self):
        from recaps.models import CustomRecapTemplate
        from tenants.models import Tenant

        log = self._run(tenant="sipli")
        assert "DRY-RUN" in log
        assert TENANT_NAME in log
        assert not Tenant.objects.filter(slug=TENANT_SLUG).exists()
        assert not CustomRecapTemplate.objects.filter(
            name=EVENT_TEMPLATE_NAME
        ).exists()

    def test_apply_creates_tenant_templates_code_and_buckets(self):
        from events.models import EventType
        from recaps.models import CustomField, CustomRecapTemplate, FileRecapCategory
        from tenants.models import Tenant

        log = self._run(tenant="sipli", apply=True)
        assert "APPLIED" in log
        tenant = Tenant.objects.get(slug=TENANT_SLUG)
        assert tenant.name == TENANT_NAME
        assert tenant.checkin_code and tenant.checkin_code.startswith("SIP-")
        assert tenant.checkin_location_mode == Tenant.CHECKIN_LOCATION_ADDRESS
        assert tenant.checkin_photo_buckets == PHOTO_BUCKETS_BY_PROGRAM
        assert tenant.checkin_event_type is not None
        assert tenant.checkin_event_type.name == EVENT_PROGRAM
        assert set(tenant.checkin_event_types.values_list("name", flat=True)) == {
            EVENT_PROGRAM,
            RETAIL_PROGRAM,
        }
        assert set(
            EventType.objects.filter(tenant=tenant).values_list("name", flat=True)
        ) == {EVENT_PROGRAM, RETAIL_PROGRAM}
        assert FileRecapCategory.objects.filter(
            tenant=tenant, name="Consumer Sampling Pictures"
        ).exists()
        assert FileRecapCategory.objects.filter(
            tenant=tenant, name="Activation Set Up"
        ).exists()
        assert FileRecapCategory.objects.filter(
            tenant=tenant, name="Expense Receipts"
        ).exists()
        assert FileRecapCategory.objects.filter(
            tenant=tenant, name="Sampling photos"
        ).exists()

        event_tpl = CustomRecapTemplate.objects.get(
            tenant=tenant, name=EVENT_TEMPLATE_NAME
        )
        retail_tpl = CustomRecapTemplate.objects.get(
            tenant=tenant, name=RETAIL_TEMPLATE_NAME
        )
        event_names = list(
            CustomField.objects.filter(custom_recap_template=event_tpl)
            .order_by("recap_section__order", "order", "id")
            .values_list("name", flat=True)
        )
        retail_names = list(
            CustomField.objects.filter(custom_recap_template=retail_tpl)
            .order_by("recap_section__order", "order", "id")
            .values_list("name", flat=True)
        )
        assert event_names == EVENT_FIELD_NAMES
        assert retail_names == RETAIL_EXTRA_NAMES + EVENT_FIELD_NAMES
        assert "Store Number" not in event_names
        assert "Total Inventory Before Demo" not in event_names
        assert event_tpl.event_type.name == EVENT_PROGRAM
        assert retail_tpl.event_type.name == RETAIL_PROGRAM
        products = CustomField.objects.get(
            custom_recap_template=event_tpl, name="Which products were sampled?"
        )
        assert list(products.options) == PRODUCT_OPTIONS

        code = tenant.checkin_code
        log2 = self._run(tenant="sipli", apply=True)
        tenant.refresh_from_db()
        assert tenant.checkin_code == code
        assert "already set" in log2

    def test_reapply_keeps_both_programs_and_the_code(self):
        from events.models import EventType
        from recaps.models import CustomRecapTemplate
        from tenants.models import Tenant

        self._run(tenant="sipli", apply=True)
        tenant = Tenant.objects.get(slug=TENANT_SLUG)
        code = tenant.checkin_code
        EventType.objects.create(
            name="On-Premise Sampling",
            slug="on-premise-sampling-again",
            tenant=tenant,
            created_by=self.user,
            is_default=False,
        )

        log = self._run(tenant="sipli", apply=True)
        tenant.refresh_from_db()
        assert tenant.checkin_code == code
        assert "already set" in log
        assert tenant.checkin_event_type.name == EVENT_PROGRAM
        assert set(
            EventType.objects.filter(tenant=tenant).values_list("name", flat=True)
        ) == {EVENT_PROGRAM, RETAIL_PROGRAM}
        assert CustomRecapTemplate.objects.filter(tenant=tenant).count() == 2


@pytest.mark.django_db
class TestSipliSetupCronView:
    def test_valid_secret_fires_command(self):
        client = Client()
        with override_settings(INTERNAL_CRON_SECRET=VALID_SECRET):
            from unittest.mock import patch

            with patch("digest.cron_views.call_command") as mock_call:
                resp = client.post(
                    SIPLI_CRON_URL,
                    {"tenant": "sipli"},
                    HTTP_X_CRON_SECRET=VALID_SECRET,
                )
        assert resp.status_code == 200
        body = resp.json()
        assert body["ok"] is True
        assert body["apply"] is False
        mock_call.assert_called_once()
        assert mock_call.call_args[0][0] == "setup_sipli_checkin"

    def test_bad_secret_returns_401(self):
        client = Client()
        with override_settings(INTERNAL_CRON_SECRET=VALID_SECRET):
            from unittest.mock import patch

            with patch("digest.cron_views.call_command") as mock_call:
                resp = client.post(
                    SIPLI_CRON_URL,
                    HTTP_X_CRON_SECRET="wrong",
                )
        assert resp.status_code == 401
        mock_call.assert_not_called()
