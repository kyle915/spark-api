"""Neutonic Event Activation: MAB/LD-mirrored template + two-program walk-up."""

from __future__ import annotations

import io

import pytest
from django.core.management import call_command
from django.test import Client, override_settings

from recaps.management.commands.seed_neutonic_recap_template import (
    EVENT_PROGRAM,
    EVENT_SPEC,
    EVENT_SPEC_FIELDS,
    EVENT_TEMPLATE_NAME,
    build_event_spec,
)
from recaps.management.commands.setup_neutonic_checkin import (
    ACTIVATION_BUCKETS,
    CODE_PREFIX,
    EVENT_LABEL,
    RETAIL_LABEL,
    WALKUP_PROGRAMS,
)
from tenants.tests.base import BaseGraphQLTestCase

VALID_SECRET = "test-cron-secret-value-only-for-tests"
NEUTONIC_SETUP_URL = "/internal/cron/setup-neutonic-checkin"
NEUTONIC_SEED_URL = "/internal/cron/seed-neutonic-recap-template"


class TestNeutonicRecapTemplateSpec:
    def test_event_spec_mirrors_mab_ld_event_activation(self):
        assert [section for section, _ in EVENT_SPEC_FIELDS] == [
            "Consumer Engagement",
            "Feedback & Account Notes",
        ]
        # 4 + 3 engagement/feedback + Products Sampled = 8 fields.
        full = build_event_spec(["Blue Raspberry — 6 pack"])
        assert [section for section, _ in full] == [
            "Consumer Engagement",
            "Feedback & Account Notes",
            "Products Sampled",
        ]
        assert sum(len(fields) for _, fields in full) == 8
        assert EVENT_TEMPLATE_NAME == "Neutonic-Event Activation"
        assert EVENT_PROGRAM == "Event Activation"

    def test_brand_copy_is_neutonic_not_white_claw(self):
        labels = [name for _, fields in EVENT_SPEC_FIELDS for name, *_ in fields]
        blob = " ".join(labels)
        assert "White Claw" not in blob
        assert "Mark Anthony" not in blob
        assert "Liquid Death" not in blob
        assert "Neutonic" in blob
        assert "tasing" in blob
        assert "TOTAL consumers" in blob

    def test_event_spec_has_ld_question_shapes(self):
        labels = [name for _, fields in EVENT_SPEC_FIELDS for name, *_ in fields]
        assert "How many TOTAL consumers did you sample?" in labels
        assert any("tried a Neutonic product before?" in n for n in labels)
        assert any("knew about Neutonic product/brand?" in n for n in labels)
        assert "Demographics" in labels
        assert any("top 5 frequently asked questions" in n for n in labels)
        assert "Helpful feedback" in labels

    def test_products_sampled_uses_catalog_options_not_hardcoded_sku_list(self):
        opts = ["Blue Raspberry — 6 pack", "Tropical Ice — 6 pack"]
        products = next(
            fields
            for section, fields in build_event_spec(opts)
            if section == "Products Sampled"
        )
        assert products == [
            ("Products Sampled", "multiselect", True, list(opts))
        ]
        # Default module EVENT_SPEC has empty options (catalog filled at seed).
        default_products = next(
            fields for section, fields in EVENT_SPEC if section == "Products Sampled"
        )
        assert default_products[0][3] == []

    def test_no_template_image_fields_photos_are_walkup_buckets(self):
        kinds = [
            kind
            for _, fields in build_event_spec(["x"])
            for _, kind, *_ in fields
        ]
        assert "image" not in kinds

    def test_template_name_buckets_as_event_for_recaps_filter(self):
        import re

        assert re.search(
            r"event|activation|festival|pop[-\s]?up",
            EVENT_TEMPLATE_NAME,
            re.I,
        )


class TestNeutonicPhotoBucketSpec:
    def test_activation_buckets_mirror_ld_mab(self):
        assert [b["name"] for b in ACTIVATION_BUCKETS] == [
            "Activation Set Up",
            "Consumer Sampling Pictures",
            "Expense Receipts (Parking)",
        ]
        assert ACTIVATION_BUCKETS[1].get("min") == 8
        assert CODE_PREFIX == "NEU-"
        assert EVENT_LABEL == "Event Activation"
        assert RETAIL_LABEL == "Retail Sampling"
        assert WALKUP_PROGRAMS == ("Retail Sampling", "Event Activation")


@pytest.mark.django_db(transaction=True)
class TestNeutonicSetupCommand(BaseGraphQLTestCase):
    @pytest.fixture(autouse=True)
    def setup(self, db):
        from events.models import EventType

        self.roles = self.setup_default_roles()
        self.user = self.get_system_user()
        self.user.is_superuser = True
        self.user.save(update_fields=["is_superuser"])
        self.tenant = self.create_tenant(name="Neutonic", slug="neutonic")
        self.tenant.checkin_code = "NEU-EXIST1"
        self.tenant.save(update_fields=["checkin_code"])
        self.retail = EventType.objects.create(
            name="Retail Sampling", tenant=self.tenant, created_by=self.user
        )
        self.event = EventType.objects.create(
            name="Event", tenant=self.tenant, created_by=self.user
        )
        self.onprem = EventType.objects.create(
            name="On-Premise Sampling", tenant=self.tenant, created_by=self.user
        )
        self.tenant.checkin_event_type = self.retail
        # Start with the old 4-style picker (minus Activation) so apply must trim.
        self.tenant.checkin_event_types.set([self.retail, self.event, self.onprem])
        self.tenant.checkin_photo_buckets = {
            "Retail Sampling": [{"name": "Table Set Up"}, {"name": "Product Display"}],
        }
        self.tenant.save(
            update_fields=["checkin_event_type", "checkin_photo_buckets"]
        )

    def _run(self, **kw):
        out = io.StringIO()
        call_command("setup_neutonic_checkin", stdout=out, **kw)
        return out.getvalue()

    def test_dry_run_creates_nothing(self):
        from events.models import EventType
        from recaps.models import FileRecapCategory

        log = self._run(tenant="neutonic")
        assert "DRY-RUN" in log
        assert "Activation Set Up" in log
        assert "NEU-EXIST1" in log
        assert not EventType.objects.filter(
            tenant=self.tenant, name="Event Activation"
        ).exists()
        assert not FileRecapCategory.objects.filter(
            tenant=self.tenant, name="Activation Set Up"
        ).exists()
        self.tenant.refresh_from_db()
        assert self.tenant.checkin_code == "NEU-EXIST1"
        assert "Event Activation" not in (self.tenant.checkin_photo_buckets or {})

    def test_apply_trims_walkup_to_retail_and_event_activation(self):
        from ambassadors import checkin_web
        from events.models import EventType
        from recaps.models import FileRecapCategory

        log = self._run(tenant="neutonic", apply=True)
        assert "APPLIED" in log
        self.tenant.refresh_from_db()
        assert self.tenant.checkin_code == "NEU-EXIST1"
        assert self.tenant.checkin_event_type_id == self.retail.id
        offered = list(
            self.tenant.checkin_event_types.order_by("id").values_list(
                "name", flat=True
            )
        )
        assert offered == list(WALKUP_PROGRAMS)
        assert "Event" not in offered
        assert "On-Premise Sampling" not in offered
        # Unused EventType rows remain in the tenant; only the picker M2M is trimmed.
        assert EventType.objects.filter(
            tenant=self.tenant, name="Event"
        ).exists()
        assert EventType.objects.filter(
            tenant=self.tenant, name="On-Premise Sampling"
        ).exists()
        buckets = self.tenant.checkin_photo_buckets
        assert [b["name"] for b in buckets["Retail Sampling"]] == [
            "Table Set Up",
            "Product Display",
        ]
        assert [b["name"] for b in buckets["Event Activation"]] == [
            b["name"] for b in ACTIVATION_BUCKETS
        ]
        activation = EventType.objects.get(
            tenant=self.tenant, name="Event Activation"
        )
        selectable = checkin_web.selectable_event_types(self.tenant)
        assert [et.name for et in selectable] == list(WALKUP_PROGRAMS)
        assert activation in selectable
        for name in [b["name"] for b in ACTIVATION_BUCKETS]:
            assert FileRecapCategory.objects.filter(
                tenant=self.tenant, name=name
            ).exists()

    def test_apply_is_idempotent_and_keeps_code(self):
        self._run(tenant="neutonic", apply=True)
        self._run(tenant="neutonic", apply=True)
        self.tenant.refresh_from_db()
        assert self.tenant.checkin_code == "NEU-EXIST1"
        from events.models import EventType

        assert (
            EventType.objects.filter(
                tenant=self.tenant, name="Event Activation"
            ).count()
            == 1
        )
        offered = set(
            self.tenant.checkin_event_types.values_list("name", flat=True)
        )
        assert offered == set(WALKUP_PROGRAMS)


@pytest.mark.django_db(transaction=True)
class TestNeutonicSeedCommand(BaseGraphQLTestCase):
    @pytest.fixture(autouse=True)
    def setup(self, db):
        from events.models import Product, ProductType

        self.roles = self.setup_default_roles()
        self.user = self.get_system_user()
        self.user.is_superuser = True
        self.user.save(update_fields=["is_superuser"])
        self.tenant = self.create_tenant(name="Neutonic", slug="neutonic")
        ptype = ProductType.objects.create(
            name="Cans", tenant=self.tenant, created_by=self.user
        )
        Product.objects.create(
            name="Blue Raspberry - 6 pack",
            product_type=ptype,
            tenant=self.tenant,
            created_by=self.user,
        )
        Product.objects.create(
            name="Tropical Ice - 6 pack",
            product_type=ptype,
            tenant=self.tenant,
            created_by=self.user,
        )

    def _run(self, **kw):
        out = io.StringIO()
        call_command("seed_neutonic_recap_template", stdout=out, **kw)
        return out.getvalue()

    def test_dry_run_creates_nothing(self):
        from recaps.models import CustomRecapTemplate

        log = self._run(tenant="neutonic")
        assert "DRY-RUN" in log
        assert EVENT_TEMPLATE_NAME in log
        assert not CustomRecapTemplate.objects.filter(tenant=self.tenant).exists()

    def test_apply_seeds_event_activation_from_catalog(self):
        from recaps.models import CustomField, CustomRecapTemplate

        log = self._run(tenant="neutonic", apply=True)
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
        assert any("Blue Raspberry" in o for o in opts)
        assert any("Tropical Ice" in o for o in opts)
        # Does not create a retail template.
        assert not CustomRecapTemplate.objects.filter(
            tenant=self.tenant, name__icontains="Retail"
        ).exists()


@pytest.mark.django_db(transaction=True)
class TestNeutonicCronEndpoints(BaseGraphQLTestCase):
    @pytest.fixture(autouse=True)
    def setup(self, db):
        self.roles = self.setup_default_roles()
        self.user = self.get_system_user()
        self.user.is_superuser = True
        self.user.save(update_fields=["is_superuser"])
        self.tenant = self.create_tenant(name="Neutonic", slug="neutonic")
        self.tenant.checkin_code = "NEU-CRON01"
        self.tenant.save(update_fields=["checkin_code"])
        self.client = Client()

    @override_settings(INTERNAL_CRON_SECRET=VALID_SECRET)
    def test_setup_cron_dry_run(self):
        resp = self.client.post(
            NEUTONIC_SETUP_URL,
            {"tenant": "neutonic"},
            HTTP_X_CRON_SECRET=VALID_SECRET,
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["ok"] is True
        assert body["apply"] is False
        assert "DRY-RUN" in body["log"]
        assert "NEU-CRON01" in body["log"]

    @override_settings(INTERNAL_CRON_SECRET=VALID_SECRET)
    def test_seed_cron_dry_run(self):
        resp = self.client.post(
            NEUTONIC_SEED_URL,
            {"tenant": "neutonic"},
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
        resp = self.client.post(NEUTONIC_SETUP_URL, {"tenant": "neutonic"})
        assert resp.status_code in (401, 403)
