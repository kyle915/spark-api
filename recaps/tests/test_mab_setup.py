"""Mark Anthony Brands: LD-mirrored dual templates + Retail/Event check-in."""

from __future__ import annotations

import io

import pytest
from django.core.management import call_command
from django.test import Client, override_settings

from recaps.management.commands.mab_products import product_options
from recaps.management.commands.seed_mab_recap_template import (
    EVENT_SPEC,
    EVENT_TEMPLATE_NAME,
    PRODUCT_OPTIONS,
    RETAIL_SPEC,
    RETAIL_TEMPLATE_NAME,
)
from recaps.management.commands.setup_mab_checkin import (
    ACTIVATION_BUCKETS,
    CODE_PREFIX,
    RETAIL_BUCKETS,
)
from tenants.tests.base import BaseGraphQLTestCase

VALID_SECRET = "test-cron-secret-value-only-for-tests"
MAB_CHECKIN_URL = "/internal/cron/setup-mab-checkin"
MAB_SEED_URL = "/internal/cron/seed-mab-recap-template"


class TestMabRecapTemplateSpec:
    def test_spec_mirrors_ld_retail_section_layout(self):
        assert [section for section, _ in RETAIL_SPEC] == [
            "Consumer Engagement",
            "Feedback & Account Notes",
            "Additional Insights",
            "Products Sampled",
        ]

    def test_spec_field_count_matches_ld_retail(self):
        assert sum(len(fields) for _, fields in RETAIL_SPEC) == 16

    def test_event_spec_mirrors_ld_event_activation(self):
        assert [section for section, _ in EVENT_SPEC] == [
            "Consumer Engagement",
            "Feedback & Account Notes",
            "Products Sampled",
        ]
        assert sum(len(fields) for _, fields in EVENT_SPEC) == 8
        assert EVENT_TEMPLATE_NAME == "Mark Anthony Brands-Event Activation"

    def test_copy_is_brand_agnostic(self):
        for spec in (RETAIL_SPEC, EVENT_SPEC):
            labels = [name for _, fields in spec for name, *_ in fields]
            blob = " ".join(labels)
            assert "Liquid Death" not in blob
            assert "Mark Anthony Brands" not in blob
            assert "the product/brand" in blob or "Products Sampled" in blob

    def test_products_sampled_uses_full_catalog(self):
        opts = product_options()
        assert len(opts) == 141
        assert PRODUCT_OPTIONS == opts
        assert opts[0].startswith("White Claw — ")
        for spec in (RETAIL_SPEC, EVENT_SPEC):
            products = next(
                fields for section, fields in spec if section == "Products Sampled"
            )
            assert products == [
                ("Products Sampled", "multiselect", True, list(PRODUCT_OPTIONS))
            ]

    def test_no_template_image_fields(self):
        for spec in (RETAIL_SPEC, EVENT_SPEC):
            kinds = [kind for _, fields in spec for _, kind, *_ in fields]
            assert "image" not in kinds


class TestMabPhotoBucketSpec:
    def test_retail_buckets_mirror_ld(self):
        assert [b["name"] for b in RETAIL_BUCKETS] == [
            "Table Set Up",
            "Product Display",
            "Consumer Sampling Pictures",
            "Product Receipt",
        ]

    def test_activation_buckets_mirror_ld(self):
        assert [b["name"] for b in ACTIVATION_BUCKETS] == [
            "Activation Set Up",
            "Consumer Sampling Pictures",
            "Expense Receipts (Parking)",
        ]
        assert ACTIVATION_BUCKETS[1].get("min") == 8

    def test_code_prefix_is_brand_scoped(self):
        assert CODE_PREFIX == "MAB-"


@pytest.mark.django_db(transaction=True)
class TestMabSetupCommand(BaseGraphQLTestCase):
    @pytest.fixture(autouse=True)
    def setup(self, db):
        from events.models import EventType

        self.roles = self.setup_default_roles()
        self.user = self.get_system_user()
        self.user.is_superuser = True
        self.user.save(update_fields=["is_superuser"])
        self.tenant = self.create_tenant(
            name="Mark Anthony Brands", slug="mark-anthony-brands"
        )
        self.retail = EventType.objects.create(
            name="Retail Sampling", tenant=self.tenant, created_by=self.user
        )
        self.activation = EventType.objects.create(
            name="Event Activation", tenant=self.tenant, created_by=self.user
        )

    def _run(self, **kw):
        out = io.StringIO()
        call_command("setup_mab_checkin", stdout=out, **kw)
        return out.getvalue()

    def test_dry_run_creates_nothing(self):
        from recaps.models import FileRecapCategory

        log = self._run(tenant="mark anthony")
        assert "DRY-RUN" in log
        assert "Table Set Up" in log
        assert "Activation Set Up" in log
        assert not FileRecapCategory.objects.filter(tenant=self.tenant).exists()

    def test_apply_sets_keyed_buckets_and_both_programs(self):
        from ambassadors import checkin_web
        from recaps.models import FileRecapCategory

        self._run(tenant="mark anthony", apply=True)
        self.tenant.refresh_from_db()
        buckets = self.tenant.checkin_photo_buckets
        assert [b["name"] for b in buckets["Retail Sampling"]] == [
            b["name"] for b in RETAIL_BUCKETS
        ]
        assert [b["name"] for b in buckets["Event Activation"]] == [
            b["name"] for b in ACTIVATION_BUCKETS
        ]
        assert self.tenant.checkin_event_type_id == self.retail.id
        offered = list(
            self.tenant.checkin_event_types.order_by("id").values_list(
                "name", flat=True
            )
        )
        assert offered == ["Retail Sampling", "Event Activation"]
        assert set(checkin_web.selectable_event_types(self.tenant)) == {
            self.retail,
            self.activation,
        }
        assert self.tenant.checkin_code and self.tenant.checkin_code.startswith(
            "MAB-"
        )
        for name in (
            [b["name"] for b in RETAIL_BUCKETS]
            + [b["name"] for b in ACTIVATION_BUCKETS]
        ):
            assert FileRecapCategory.objects.filter(
                tenant=self.tenant, name=name
            ).exists()


@pytest.mark.django_db(transaction=True)
class TestMabRecapTemplateSeed(BaseGraphQLTestCase):
    @pytest.fixture(autouse=True)
    def setup(self, db):
        from events.models import EventType

        self.roles = self.setup_default_roles()
        self.user = self.get_system_user()
        self.user.is_superuser = True
        self.user.save(update_fields=["is_superuser"])
        self.tenant = self.create_tenant(
            name="Mark Anthony Brands", slug="mark-anthony-brands"
        )
        EventType.objects.create(
            name="Retail Sampling", tenant=self.tenant, created_by=self.user
        )

    def _run(self, **kw):
        out = io.StringIO()
        call_command("seed_mab_recap_template", stdout=out, **kw)
        return out.getvalue()

    def test_apply_seeds_both_templates(self):
        from recaps.models import CustomField, CustomRecapTemplate

        log = self._run(tenant="mark anthony", apply=True)
        assert "APPLIED" in log
        retail = CustomRecapTemplate.objects.get(
            tenant=self.tenant, name=RETAIL_TEMPLATE_NAME
        )
        event = CustomRecapTemplate.objects.get(
            tenant=self.tenant, name=EVENT_TEMPLATE_NAME
        )
        assert retail.event_type.name == "Retail Sampling"
        assert event.event_type.name == "Event Activation"
        for tpl in (retail, event):
            products = CustomField.objects.get(
                custom_recap_template=tpl, name="Products Sampled"
            )
            assert len(list(products.options)) == 141


@pytest.mark.django_db
class TestMabSetupCronView:
    def test_valid_secret_fires_checkin_command(self):
        client = Client()
        with override_settings(INTERNAL_CRON_SECRET=VALID_SECRET):
            from unittest.mock import patch

            with patch("digest.cron_views.call_command") as mock_call:
                resp = client.post(
                    MAB_CHECKIN_URL,
                    {"tenant": "mark anthony"},
                    HTTP_X_CRON_SECRET=VALID_SECRET,
                )
        assert resp.status_code == 200
        assert resp.json()["ok"] is True
        assert mock_call.call_args[0][0] == "setup_mab_checkin"

    def test_seed_cron_fires_template_command(self):
        client = Client()
        with override_settings(INTERNAL_CRON_SECRET=VALID_SECRET):
            from unittest.mock import patch

            with patch("digest.cron_views.call_command") as mock_call:
                resp = client.post(
                    MAB_SEED_URL,
                    {"tenant": "mark anthony"},
                    HTTP_X_CRON_SECRET=VALID_SECRET,
                )
        assert resp.status_code == 200
        assert resp.json()["ok"] is True
        assert mock_call.call_args[0][0] == "seed_mab_recap_template"
