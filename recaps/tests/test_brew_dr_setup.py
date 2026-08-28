"""Brew Dr. Kombucha: LD-mirrored dual templates + Retail/Event check-in dongle."""

from __future__ import annotations

import io

import pytest
from django.core.management import call_command
from django.test import Client, override_settings

from recaps.management.commands.seed_brew_dr_recap_template import (
    CANS,
    EVENT_SPEC,
    EVENT_TEMPLATE_NAME,
    LEGACY_TEMPLATE_NAMES,
    RETAIL_SPEC,
    RETAIL_TEMPLATE_NAME,
    SPEC,
    TEMPLATE_NAME,
)
from recaps.management.commands.setup_brew_dr_checkin import (
    ACTIVATION_BUCKETS,
    CODE_PREFIX,
    PHOTO_BUCKETS,
    RETAIL_BUCKETS,
)
from tenants.tests.base import BaseGraphQLTestCase

VALID_SECRET = "test-cron-secret-value-only-for-tests"
BREW_DR_CRON_URL = "/internal/cron/setup-brew-dr-checkin"
BREW_DR_SEED_URL = "/internal/cron/seed-brew-dr-recap-template"


class TestBrewDrRecapTemplateSpec:
    def test_spec_mirrors_ld_retail_section_layout(self):
        assert [section for section, _ in RETAIL_SPEC] == [
            "Consumer Engagement",
            "Feedback & Account Notes",
            "Additional Insights",
            "Products Sampled",
        ]
        assert SPEC is RETAIL_SPEC
        assert TEMPLATE_NAME == RETAIL_TEMPLATE_NAME

    def test_spec_field_count_matches_ld_retail(self):
        # LD Retail Sampling (prod id 9): 7 + 7 + 1 + 1 = 16 fields.
        assert sum(len(fields) for _, fields in RETAIL_SPEC) == 16

    def test_event_spec_mirrors_ld_event_activation(self):
        # LD Event Activation (prod id 3): 4 + 3 + 1 = 8 fields.
        assert [section for section, _ in EVENT_SPEC] == [
            "Consumer Engagement",
            "Feedback & Account Notes",
            "Products Sampled",
        ]
        assert sum(len(fields) for _, fields in EVENT_SPEC) == 8
        assert EVENT_TEMPLATE_NAME == "Brew Dr. Kombucha-Event Activation"

    def test_brand_copy_is_brew_dr_not_liquid_death(self):
        for spec in (RETAIL_SPEC, EVENT_SPEC):
            labels = [name for _, fields in spec for name, *_ in fields]
            blob = " ".join(labels)
            assert "Liquid Death" not in blob
            assert "Liquid Death" not in blob
            assert "Brew Dr. Kombucha" in blob or "Products Sampled" in blob
            assert "tasing" in blob or "TOTAL consumers" in blob

    def test_event_spec_has_ld_question_shapes(self):
        labels = [name for _, fields in EVENT_SPEC for name, *_ in fields]
        assert "How many TOTAL consumers did you sample?" in labels
        assert any("tried a Brew Dr. Kombucha flavor before?" in n for n in labels)
        assert "Demographics" in labels
        assert any("top 5 frequently asked questions" in n for n in labels)
        assert "Helpful feedback" in labels

    def test_products_sampled_uses_five_cans(self):
        for spec in (RETAIL_SPEC, EVENT_SPEC):
            products = next(
                fields for section, fields in spec if section == "Products Sampled"
            )
            assert products == [
                ("Products Sampled", "multiselect", True, list(CANS))
            ]
        assert CANS == [
            "Clear Mind",
            "Island Mango",
            "Superberry",
            "Love",
            "Pineapple Paradise",
        ]

    def test_no_template_image_fields_photos_are_walkup_buckets(self):
        for spec in (RETAIL_SPEC, EVENT_SPEC):
            kinds = [kind for _, fields in spec for _, kind, *_ in fields]
            assert "image" not in kinds

    def test_template_name_and_legacy_alias(self):
        assert RETAIL_TEMPLATE_NAME == "Brew Dr. Kombucha-Retail Sampling"
        assert "Brew Dr. Kombucha Recap" in LEGACY_TEMPLATE_NAMES


class TestBrewDrPhotoBucketSpec:
    def test_photo_buckets_match_kyles_retail_sampling_shot_list(self):
        assert [b["name"] for b in RETAIL_BUCKETS] == [
            "Set Before",
            "Set After",
            "Demo Table Before Demo (Far Back)",
            "Demo Table (Close Up)",
            "Demo Table Area",
            "Displays (if applicable)",
        ]
        assert PHOTO_BUCKETS is RETAIL_BUCKETS

    def test_activation_buckets_mirror_ld(self):
        assert [b["name"] for b in ACTIVATION_BUCKETS] == [
            "Activation Set Up",
            "Consumer Sampling Pictures",
            "Expense Receipts (Parking)",
        ]
        assert ACTIVATION_BUCKETS[1].get("min") == 8

    def test_required_buckets_carry_min_one(self):
        required = RETAIL_BUCKETS[:-1]
        assert all(b.get("min") == 1 for b in required)
        assert "min" not in RETAIL_BUCKETS[-1]

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
        self.retail = EventType.objects.create(
            name="Retail Sampling", tenant=self.tenant, created_by=self.user
        )
        self.activation = EventType.objects.create(
            name="Event Activation", tenant=self.tenant, created_by=self.user
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
        assert "Activation Set Up" in log
        assert not FileRecapCategory.objects.filter(tenant=self.tenant).exists()
        self.tenant.refresh_from_db()
        assert self.tenant.checkin_photo_buckets is None

    def test_apply_sets_keyed_buckets_and_both_programs(self):
        from ambassadors import checkin_web
        from recaps.models import FileRecapCategory

        log = self._run(tenant="brew", apply=True)
        assert "selectable" in log.lower() or "Check-in code" in log
        self.tenant.refresh_from_db()
        buckets = self.tenant.checkin_photo_buckets
        assert isinstance(buckets, dict)
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
        for name in (
            [b["name"] for b in RETAIL_BUCKETS]
            + [b["name"] for b in ACTIVATION_BUCKETS]
        ):
            assert FileRecapCategory.objects.filter(
                tenant=self.tenant, name=name
            ).exists()
        assert self.tenant.checkin_code and self.tenant.checkin_code.startswith("BD-")

    def test_apply_is_idempotent_when_code_already_set(self):
        self.tenant.checkin_code = "BD-AQRACD"
        self.tenant.save(update_fields=["checkin_code"])
        log = self._run(tenant="brew", apply=True)
        self.tenant.refresh_from_db()
        assert self.tenant.checkin_code == "BD-AQRACD"
        assert "left as-is" in log or "BD-AQRACD" in log
        assert isinstance(self.tenant.checkin_photo_buckets, dict)

    def test_serialize_photo_buckets_per_program(self):
        from django.utils import timezone
        from events.models import Event

        from ambassadors import checkin_web

        self._run(tenant="brew", apply=True)
        self.tenant.refresh_from_db()
        retail_event = Event.objects.create(
            name="HEB Demo",
            tenant=self.tenant,
            address="123 Main St",
            event_type=self.retail,
            date=timezone.now(),
            created_by=self.user,
        )
        activation_event = Event.objects.create(
            name="Fest Demo",
            tenant=self.tenant,
            address="456 Park",
            event_type=self.activation,
            date=timezone.now(),
            created_by=self.user,
        )
        retail_buckets = checkin_web.serialize_photo_buckets(retail_event)
        activation_buckets = checkin_web.serialize_photo_buckets(activation_event)
        assert [b["name"] for b in retail_buckets] == [
            b["name"] for b in RETAIL_BUCKETS
        ]
        assert [b["name"] for b in activation_buckets] == [
            b["name"] for b in ACTIVATION_BUCKETS
        ]
        mins = {b["name"]: b["min"] for b in retail_buckets}
        assert mins["Set Before"] == 1
        assert mins["Displays (if applicable)"] == 0
        act_mins = {b["name"]: b["min"] for b in activation_buckets}
        assert act_mins["Consumer Sampling Pictures"] == 8


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
        assert "tasing" in log
        assert "Event Activation" in log
        assert not CustomRecapTemplate.objects.filter(tenant=self.tenant).exists()
        assert not CustomField.objects.filter(
            custom_recap_template__tenant=self.tenant
        ).exists()

    def test_apply_seeds_both_ld_mirrored_templates(self):
        from events.models import EventType
        from recaps.models import CustomField, CustomRecapTemplate

        log = self._run(tenant="brew", apply=True)
        assert "APPLIED" in log
        retail = CustomRecapTemplate.objects.get(
            tenant=self.tenant, name=RETAIL_TEMPLATE_NAME
        )
        event = CustomRecapTemplate.objects.get(
            tenant=self.tenant, name=EVENT_TEMPLATE_NAME
        )
        assert retail.event_type.name == "Retail Sampling"
        assert event.event_type.name == "Event Activation"
        assert EventType.objects.filter(
            tenant=self.tenant, name="Event Activation"
        ).exists()

        retail_names = list(
            CustomField.objects.filter(custom_recap_template=retail)
            .order_by("recap_section__order", "order", "id")
            .values_list("name", flat=True)
        )
        assert retail_names[0] == "Total number of consumers sampled"
        assert len(retail_names) == 16

        event_names = list(
            CustomField.objects.filter(custom_recap_template=event)
            .order_by("recap_section__order", "order", "id")
            .values_list("name", flat=True)
        )
        assert "How many TOTAL consumers did you sample?" in event_names
        assert len(event_names) == 8
        for tpl in (retail, event):
            products = CustomField.objects.get(
                custom_recap_template=tpl, name="Products Sampled"
            )
            assert list(products.options) == list(CANS)

    def test_apply_renames_empty_legacy_template_in_place(self):
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
        assert legacy.name == RETAIL_TEMPLATE_NAME
        assert legacy.product_samples is True
        assert CustomRecapTemplate.objects.filter(
            tenant=self.tenant, name=RETAIL_TEMPLATE_NAME
        ).count() == 1
        assert (
            CustomField.objects.filter(custom_recap_template=legacy).count() == 16
        )
        assert CustomRecapTemplate.objects.filter(
            tenant=self.tenant, name=EVENT_TEMPLATE_NAME
        ).exists()

    def test_apply_archives_franken_form_and_seeds_clean_ld_template(self):
        """Prod stacked LD fields on the legacy form; seeder must split them."""
        from ambassadors.checkin_web import resolve_template_for_event
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

        from recaps.management.commands.seed_brew_dr_recap_template import (
            ARCHIVE_EVENT_TYPE,
            ARCHIVED_TEMPLATE_NAME,
        )

        franken = CustomRecapTemplate.objects.create(
            tenant=self.tenant,
            name=RETAIL_TEMPLATE_NAME,
            event_type=self.event_type,
            product_samples=True,
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
        ba = CustomField.objects.create(
            name="BA Name",
            custom_recap_template=franken,
            recap_section=section,
            custom_field_type=ftype,
            required=True,
            options=[],
            order=0,
            created_by=self.user,
        )
        CustomField.objects.create(
            name="Total number of consumers sampled",
            custom_recap_template=franken,
            recap_section=section,
            custom_field_type=ftype,
            required=True,
            options=[],
            order=1,
            created_by=self.user,
        )
        event = Event.objects.create(
            name="Legacy HEB",
            tenant=self.tenant,
            address="1 Main",
            event_type=self.event_type,
            date=timezone.now(),
            created_by=self.user,
        )
        recap = CustomRecap.objects.create(
            name="old brew dr recap",
            event=event,
            tenant=self.tenant,
            custom_recap_template=franken,
            created_by=self.user,
        )
        CustomFieldValue.objects.create(
            value="Alex",
            custom_recap=recap,
            custom_field=ba,
            created_by=self.user,
        )

        log = self._run(tenant="brew", apply=True)
        assert "archiving" in log.lower() or "archive" in log.lower()

        franken.refresh_from_db()
        assert franken.name == ARCHIVED_TEMPLATE_NAME
        assert franken.event_type.name == ARCHIVE_EVENT_TYPE
        assert CustomField.objects.filter(
            custom_recap_template=franken, name="BA Name"
        ).exists()
        assert not CustomField.objects.filter(
            custom_recap_template=franken,
            name="Total number of consumers sampled",
        ).exists()

        live = CustomRecapTemplate.objects.get(
            tenant=self.tenant, name=RETAIL_TEMPLATE_NAME, event_type=self.event_type
        )
        assert live.id != franken.id
        assert (
            CustomField.objects.filter(custom_recap_template=live).count() == 16
        )
        assert not CustomField.objects.filter(
            custom_recap_template=live, name="BA Name"
        ).exists()

        walkup = resolve_template_for_event(event)
        fresh = Event.objects.create(
            name="New HEB",
            tenant=self.tenant,
            address="2 Main",
            event_type=self.event_type,
            date=timezone.now(),
            created_by=self.user,
        )
        assert resolve_template_for_event(fresh).id == live.id
        assert walkup.id == franken.id

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
        assert CustomRecapTemplate.objects.filter(tenant=self.tenant).count() == 2
        retail = CustomRecapTemplate.objects.get(
            tenant=self.tenant, name=RETAIL_TEMPLATE_NAME
        )
        event = CustomRecapTemplate.objects.get(
            tenant=self.tenant, name=EVENT_TEMPLATE_NAME
        )
        assert (
            CustomField.objects.filter(custom_recap_template=retail).count() == 16
        )
        assert CustomField.objects.filter(custom_recap_template=event).count() == 8

    def test_resolve_template_by_event_type(self):
        from django.utils import timezone
        from events.models import Event, EventType

        from ambassadors.checkin_web import resolve_template_for_event

        self._run(tenant="brew", apply=True)
        activation = EventType.objects.get(
            tenant=self.tenant, name="Event Activation"
        )
        retail_event = Event.objects.create(
            name="Retail",
            tenant=self.tenant,
            address="1",
            event_type=self.event_type,
            date=timezone.now(),
            created_by=self.user,
        )
        activation_event = Event.objects.create(
            name="Activation",
            tenant=self.tenant,
            address="2",
            event_type=activation,
            date=timezone.now(),
            created_by=self.user,
        )
        assert (
            resolve_template_for_event(retail_event).name == RETAIL_TEMPLATE_NAME
        )
        assert (
            resolve_template_for_event(activation_event).name
            == EVENT_TEMPLATE_NAME
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
