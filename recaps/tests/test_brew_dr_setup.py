"""Brew Dr. Kombucha: LD-mirrored recap template + check-in photo buckets."""

from __future__ import annotations

import io

import pytest
from django.core.management import call_command
from django.test import Client, override_settings

from recaps.management.commands.seed_brew_dr_recap_template import (
    CANS,
    LEGACY_TEMPLATE_NAMES,
    SPEC,
    TEMPLATE_NAME,
)
from recaps.management.commands.setup_brew_dr_checkin import (
    CODE_PREFIX,
    PHOTO_BUCKETS,
)
from tenants.tests.base import BaseGraphQLTestCase

VALID_SECRET = "test-cron-secret-value-only-for-tests"
BREW_DR_CRON_URL = "/internal/cron/setup-brew-dr-checkin"
BREW_DR_SEED_URL = "/internal/cron/seed-brew-dr-recap-template"


class TestBrewDrRecapTemplateSpec:
    def test_spec_mirrors_ld_retail_section_layout(self):
        assert [section for section, _ in SPEC] == [
            "Consumer Engagement",
            "Feedback & Account Notes",
            "Additional Insights",
            "Products Sampled",
        ]

    def test_spec_field_count_matches_ld_retail(self):
        # LD Retail Sampling (prod id 9): 7 + 7 + 1 + 1 = 16 fields.
        assert sum(len(fields) for _, fields in SPEC) == 16

    def test_brand_copy_is_brew_dr_not_liquid_death(self):
        labels = [name for _, fields in SPEC for name, *_ in fields]
        blob = " ".join(labels)
        assert "Liquid Death" not in blob
        assert "tasing" not in blob
        assert "Brew Dr. Kombucha" in blob
        assert "after tasting it" in blob

    def test_products_sampled_uses_five_cans(self):
        products = next(
            fields for section, fields in SPEC if section == "Products Sampled"
        )
        assert products == [("Products Sampled", "multiselect", True, list(CANS))]
        assert CANS == [
            "Clear Mind",
            "Island Mango",
            "Superberry",
            "Love",
            "Pineapple Paradise",
        ]

    def test_no_template_image_fields_photos_are_walkup_buckets(self):
        kinds = [kind for _, fields in SPEC for _, kind, *_ in fields]
        assert "image" not in kinds

    def test_template_name_and_legacy_alias(self):
        assert TEMPLATE_NAME == "Brew Dr. Kombucha-Retail Sampling"
        assert "Brew Dr. Kombucha Recap" in LEGACY_TEMPLATE_NAMES


class TestBrewDrPhotoBucketSpec:
    def test_photo_buckets_match_kyles_retail_sampling_shot_list(self):
        assert [b["name"] for b in PHOTO_BUCKETS] == [
            "Set Before",
            "Set After",
            "Demo Table Before Demo (Far Back)",
            "Demo Table (Close Up)",
            "Demo Table Area",
            "Displays (if applicable)",
        ]

    def test_required_buckets_carry_min_one(self):
        required = PHOTO_BUCKETS[:-1]
        assert all(b.get("min") == 1 for b in required)
        assert "min" not in PHOTO_BUCKETS[-1]

    def test_code_prefix_is_brand_scoped(self):
        assert CODE_PREFIX == "BD-"


@pytest.mark.django_db(transaction=True)
class TestBrewDrSetupCommand(BaseGraphQLTestCase):
    @pytest.fixture(autouse=True)
    def setup(self, db):
        from events.models import EventType

        self.roles = self.setup_default_roles()
        self.user = self.get_system_user()
        self.user.is_superuser = True
        self.user.save(update_fields=["is_superuser"])
        self.tenant = self.create_tenant(name="Brew Dr. Kombucha", slug="brew-dr")
        self.event_type = EventType.objects.create(
            name="Retail Sampling", tenant=self.tenant, created_by=self.user
        )

    def _run(self, **kw):
        out = io.StringIO()
        call_command("setup_brew_dr_checkin", stdout=out, **kw)
        return out.getvalue()

    def test_dry_run_creates_nothing(self):
        from recaps.models import FileRecapCategory

        log = self._run(tenant="brew")
        assert "DRY-RUN" in log
        assert "Set Before" in log
        assert not FileRecapCategory.objects.filter(tenant=self.tenant).exists()
        self.tenant.refresh_from_db()
        assert self.tenant.checkin_photo_buckets is None

    def test_apply_sets_buckets_and_pins_program(self):
        from recaps.models import FileRecapCategory

        log = self._run(tenant="brew", apply=True)
        assert "checkin_photo_buckets set" in log
        self.tenant.refresh_from_db()
        assert self.tenant.checkin_photo_buckets == PHOTO_BUCKETS
        assert self.tenant.checkin_event_type_id == self.event_type.id
        assert list(
            self.tenant.checkin_event_types.values_list("name", flat=True)
        ) == ["Retail Sampling"]
        for bucket in PHOTO_BUCKETS:
            assert FileRecapCategory.objects.filter(
                tenant=self.tenant, name=bucket["name"]
            ).exists()
        assert self.tenant.checkin_code and self.tenant.checkin_code.startswith("BD-")

    def test_apply_is_idempotent_when_code_already_set(self):
        self.tenant.checkin_code = "BD-AQRACD"
        self.tenant.save(update_fields=["checkin_code"])
        log = self._run(tenant="brew", apply=True)
        self.tenant.refresh_from_db()
        assert self.tenant.checkin_code == "BD-AQRACD"
        assert "already set" in log
        assert self.tenant.checkin_photo_buckets == PHOTO_BUCKETS

    def test_serialize_photo_buckets_for_event(self):
        from django.utils import timezone
        from events.models import Event

        from ambassadors import checkin_web

        self._run(tenant="brew", apply=True)
        self.tenant.refresh_from_db()
        event = Event.objects.create(
            name="HEB Demo",
            tenant=self.tenant,
            address="123 Main St",
            event_type=self.event_type,
            date=timezone.now(),
            created_by=self.user,
        )
        buckets = checkin_web.serialize_photo_buckets(event)
        assert [b["name"] for b in buckets] == [b["name"] for b in PHOTO_BUCKETS]
        mins = {b["name"]: b["min"] for b in buckets}
        assert mins["Set Before"] == 1
        assert mins["Displays (if applicable)"] == 0


@pytest.mark.django_db(transaction=True)
class TestBrewDrRecapTemplateSeed(BaseGraphQLTestCase):
    @pytest.fixture(autouse=True)
    def setup(self, db):
        from events.models import EventType

        self.roles = self.setup_default_roles()
        self.user = self.get_system_user()
        self.user.is_superuser = True
        self.user.save(update_fields=["is_superuser"])
        self.tenant = self.create_tenant(name="Brew Dr. Kombucha", slug="brew-dr")
        self.event_type = EventType.objects.create(
            name="Retail Sampling", tenant=self.tenant, created_by=self.user
        )

    def _run(self, **kw):
        out = io.StringIO()
        call_command("seed_brew_dr_recap_template", stdout=out, **kw)
        return out.getvalue()

    def test_dry_run_creates_nothing(self):
        from recaps.models import CustomField, CustomRecapTemplate

        log = self._run(tenant="brew")
        assert "DRY-RUN" in log
        assert "Consumer Engagement" in log
        assert "tasting" in log
        assert not CustomRecapTemplate.objects.filter(tenant=self.tenant).exists()
        assert not CustomField.objects.filter(
            custom_recap_template__tenant=self.tenant
        ).exists()

    def test_apply_seeds_ld_mirrored_fields(self):
        from recaps.models import CustomField, CustomRecapTemplate

        log = self._run(tenant="brew", apply=True)
        assert "APPLIED" in log
        tpl = CustomRecapTemplate.objects.get(tenant=self.tenant)
        assert tpl.name == TEMPLATE_NAME
        assert tpl.event_type_id == self.event_type.id
        assert tpl.product_samples is True
        names = list(
            CustomField.objects.filter(custom_recap_template=tpl)
            .order_by("recap_section__order", "order", "id")
            .values_list("name", flat=True)
        )
        assert names[0] == "Total number of consumers sampled"
        assert "Products Sampled" in names
        assert "BA Name" not in names
        products = CustomField.objects.get(
            custom_recap_template=tpl, name="Products Sampled"
        )
        assert list(products.options) == list(CANS)
        assert len(names) == 16

    def test_apply_renames_legacy_template_in_place(self):
        from recaps.models import CustomField, CustomRecapTemplate

        legacy = CustomRecapTemplate.objects.create(
            tenant=self.tenant,
            name="Brew Dr. Kombucha Recap",
            event_type=self.event_type,
            product_samples=False,
            sales_performance=False,
            layout={},
            created_by=self.user,
        )
        legacy_id = legacy.id
        log = self._run(tenant="brew", apply=True)
        assert "rename" in log.lower() or "RENAMED" in log
        legacy.refresh_from_db()
        assert legacy.id == legacy_id
        assert legacy.name == TEMPLATE_NAME
        assert legacy.product_samples is True
        assert CustomRecapTemplate.objects.filter(tenant=self.tenant).count() == 1
        assert (
            CustomField.objects.filter(custom_recap_template=legacy).count() == 16
        )

    def test_apply_prunes_obsolete_fields_without_values(self):
        from recaps.models import (
            CustomField,
            CustomRecapFieldType,
            CustomRecapTemplate,
            RecapSection,
        )

        tpl = CustomRecapTemplate.objects.create(
            tenant=self.tenant,
            name="Brew Dr. Kombucha Recap",
            event_type=self.event_type,
            product_samples=False,
            sales_performance=False,
            layout={},
            created_by=self.user,
        )
        section = RecapSection.objects.create(
            tenant=self.tenant, name="Event Details", order=0, created_by=self.user
        )
        ftype = CustomRecapFieldType.objects.create(
            name="text", created_by=self.user
        )
        CustomField.objects.create(
            name="BA Name",
            custom_recap_template=tpl,
            recap_section=section,
            custom_field_type=ftype,
            required=True,
            options=[],
            order=0,
            created_by=self.user,
        )
        self._run(tenant="brew", apply=True)
        assert not CustomField.objects.filter(
            custom_recap_template=tpl, name="BA Name"
        ).exists()
        assert (
            CustomField.objects.filter(custom_recap_template_id=tpl.id).count() == 16
        )

    def test_apply_is_idempotent(self):
        from recaps.models import CustomField, CustomRecapTemplate

        self._run(tenant="brew", apply=True)
        self._run(tenant="brew", apply=True)
        assert CustomRecapTemplate.objects.filter(tenant=self.tenant).count() == 1
        assert (
            CustomField.objects.filter(
                custom_recap_template__tenant=self.tenant
            ).count()
            == 16
        )


@pytest.mark.django_db
class TestBrewDrSetupCronView:
    def test_valid_secret_fires_command(self):
        client = Client()
        with override_settings(INTERNAL_CRON_SECRET=VALID_SECRET):
            from unittest.mock import patch

            with patch("digest.cron_views.call_command") as mock_call:
                resp = client.post(
                    BREW_DR_CRON_URL,
                    {"tenant": "brew"},
                    HTTP_X_CRON_SECRET=VALID_SECRET,
                )
        assert resp.status_code == 200
        body = resp.json()
        assert body["ok"] is True
        assert body["apply"] is False
        mock_call.assert_called_once()
        assert mock_call.call_args[0][0] == "setup_brew_dr_checkin"

    def test_seed_cron_fires_template_command(self):
        client = Client()
        with override_settings(INTERNAL_CRON_SECRET=VALID_SECRET):
            from unittest.mock import patch

            with patch("digest.cron_views.call_command") as mock_call:
                resp = client.post(
                    BREW_DR_SEED_URL,
                    {"tenant": "brew"},
                    HTTP_X_CRON_SECRET=VALID_SECRET,
                )
        assert resp.status_code == 200
        body = resp.json()
        assert body["ok"] is True
        assert body["applied"] is False
        mock_call.assert_called_once()
        assert mock_call.call_args[0][0] == "seed_brew_dr_recap_template"
