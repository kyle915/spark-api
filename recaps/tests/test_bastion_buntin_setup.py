"""Bastion Buntin Future Golf / Avocados from Mexico walk-up setup tests."""

from __future__ import annotations

import io

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import Client, override_settings

from recaps.management.commands.setup_bastion_buntin_checkin import (
    AFM_PROGRAM,
    AFM_SPEC,
    AFM_TEMPLATE_NAME,
    CODE_PREFIX,
    FG_PROGRAM,
    FG_SPEC,
    FG_TEMPLATE_NAME,
    PHOTO_BUCKETS,
    PHOTO_BUCKETS_BY_PROGRAM,
    TENANT_NAME,
    TENANT_SLUG,
)
from tenants.tests.base import BaseGraphQLTestCase

VALID_SECRET = "test-cron-secret-value-only-for-tests"
CRON_URL = "/internal/cron/setup-bastion-buntin-checkin"

FG_FIELD_NAMES = [f[0] for _, fields in FG_SPEC for f in fields]
AFM_FIELD_NAMES = [f[0] for _, fields in AFM_SPEC for f in fields]


class TestBastionBuntinSpec:
    def test_drops_samples_given_and_keeps_engagement(self):
        assert FG_FIELD_NAMES == [
            "How many consumers would be willing to purchase the product "
            "after tasting it?",
            "How many consumers that were engaged with knew about Future Golf?",
            "How many consumers had tried Future Golf before?",
            "How many people did you interact with?",
            "Demographics",
            "What were the top 5 frequently asked questions you received "
            "from consumers?",
            "Helpful feedback",
        ]
        assert AFM_FIELD_NAMES == [
            "How many consumers would be willing to purchase the product "
            "after tasting it?",
            "How many consumers that were engaged with knew about "
            "Avocados from Mexico?",
            "How many consumers had tried Avocados from Mexico before?",
            "How many people did you interact with?",
            "Demographics",
            "What were the top 5 frequently asked questions you received "
            "from consumers?",
            "Helpful feedback",
        ]
        blob = str(FG_SPEC) + str(AFM_SPEC)
        for forbidden in (
            "TOTAL consumers did you sample",
            "samples distributed",
            "samples given",
            "samples handed",
            "Products Sampled",
        ):
            assert forbidden.lower() not in blob.lower()

    def test_spelling_is_avocados_from_mexico(self):
        assert AFM_PROGRAM == "Avocados from Mexico"
        assert "Avacados" not in AFM_PROGRAM
        assert "Avacados" not in AFM_TEMPLATE_NAME

    def test_photo_buckets_match_ld_event(self):
        assert [bucket["name"] for bucket in PHOTO_BUCKETS] == [
            "Activation Set Up",
            "Consumer Sampling Pictures",
            "Expense Receipts (Parking)",
        ]
        assert PHOTO_BUCKETS_BY_PROGRAM[FG_PROGRAM] == PHOTO_BUCKETS
        assert PHOTO_BUCKETS_BY_PROGRAM[AFM_PROGRAM] == PHOTO_BUCKETS

    def test_brand_constants(self):
        assert CODE_PREFIX == "BB-"
        assert FG_TEMPLATE_NAME == "Bastion Buntin · Future Golf Event Recap"
        assert AFM_TEMPLATE_NAME == (
            "Bastion Buntin · Avocados from Mexico Event Recap"
        )
        assert TENANT_SLUG == "bastion-buntin"
        assert TENANT_NAME == "Bastion Buntin"

    def test_kinds_are_canonical_and_not_image(self):
        kinds = {f[1] for _, fields in FG_SPEC + AFM_SPEC for f in fields}
        assert kinds <= {"text", "number", "longtext", "select", "multiselect"}
        assert "image" not in kinds


@pytest.mark.django_db(transaction=True)
class TestBastionBuntinSetupCommand(BaseGraphQLTestCase):
    @pytest.fixture(autouse=True)
    def setup(self, db):
        self.roles = self.setup_default_roles()
        self.user = self.get_system_user()
        self.user.is_superuser = True
        self.user.save(update_fields=["is_superuser"])

    def _run(self, **kw):
        out = io.StringIO()
        call_command("setup_bastion_buntin_checkin", stdout=out, **kw)
        return out.getvalue()

    def test_dry_run_creates_nothing(self):
        from recaps.models import CustomRecapTemplate
        from tenants.models import Tenant

        log = self._run(tenant="bastion-buntin")
        assert "DRY-RUN" in log
        assert TENANT_NAME in log
        assert FG_PROGRAM in log
        assert AFM_PROGRAM in log
        assert not Tenant.objects.filter(slug=TENANT_SLUG).exists()
        assert not CustomRecapTemplate.objects.filter(
            name=FG_TEMPLATE_NAME
        ).exists()

    def test_unrelated_tenant_needle_is_rejected(self):
        with pytest.raises(CommandError, match="not Bastion Buntin"):
            self._run(tenant="torch")

    def test_apply_creates_tenant_programs_templates_code_and_buckets(self):
        from events.models import EventType
        from recaps.models import CustomField, CustomRecapTemplate, FileRecapCategory
        from tenants.models import Tenant

        log = self._run(tenant="bastion-buntin", apply=True)
        assert "APPLIED" in log
        tenant = Tenant.objects.get(slug=TENANT_SLUG)
        assert tenant.name == TENANT_NAME
        assert tenant.request_url_name == TENANT_SLUG
        assert tenant.checkin_code and tenant.checkin_code.startswith("BB-")
        assert tenant.checkin_location_mode == Tenant.CHECKIN_LOCATION_ADDRESS
        assert tenant.checkin_photo_buckets == PHOTO_BUCKETS_BY_PROGRAM
        assert tenant.checkin_event_type is not None
        assert tenant.checkin_event_type.name == FG_PROGRAM
        assert set(
            tenant.checkin_event_types.values_list("name", flat=True)
        ) == {FG_PROGRAM, AFM_PROGRAM}
        assert FileRecapCategory.objects.filter(
            tenant=tenant, name="Activation Set Up"
        ).exists()
        assert FileRecapCategory.objects.filter(
            tenant=tenant, name="Consumer Sampling Pictures"
        ).exists()
        assert FileRecapCategory.objects.filter(
            tenant=tenant, name="Expense Receipts (Parking)"
        ).exists()

        for tpl_name, fields, program in (
            (FG_TEMPLATE_NAME, FG_FIELD_NAMES, FG_PROGRAM),
            (AFM_TEMPLATE_NAME, AFM_FIELD_NAMES, AFM_PROGRAM),
        ):
            tpl = CustomRecapTemplate.objects.get(tenant=tenant, name=tpl_name)
            names = list(
                CustomField.objects.filter(custom_recap_template=tpl)
                .order_by("recap_section__order", "order", "id")
                .values_list("name", flat=True)
            )
            assert names == fields
            assert tpl.event_type.name == program
            assert tpl.product_samples is False

        assert EventType.objects.filter(
            tenant=tenant, name=FG_PROGRAM
        ).exists()
        assert EventType.objects.filter(
            tenant=tenant, name=AFM_PROGRAM
        ).exists()

        code = tenant.checkin_code
        log2 = self._run(tenant="bastion-buntin", apply=True)
        tenant.refresh_from_db()
        assert tenant.checkin_code == code
        assert "already set" in log2


@pytest.mark.django_db
class TestBastionBuntinSetupCronView:
    def test_valid_secret_fires_command(self):
        client = Client()
        with override_settings(INTERNAL_CRON_SECRET=VALID_SECRET):
            from unittest.mock import patch

            with patch("digest.cron_views.call_command") as mock_call:
                resp = client.post(
                    CRON_URL,
                    {"tenant": "bastion-buntin"},
                    HTTP_X_CRON_SECRET=VALID_SECRET,
                )
        assert resp.status_code == 200
        body = resp.json()
        assert body["ok"] is True
        assert body["apply"] is False
        mock_call.assert_called_once()
        assert mock_call.call_args[0][0] == "setup_bastion_buntin_checkin"

    def test_bad_secret_returns_401(self):
        client = Client()
        with override_settings(INTERNAL_CRON_SECRET=VALID_SECRET):
            from unittest.mock import patch

            with patch("digest.cron_views.call_command") as mock_call:
                resp = client.post(
                    CRON_URL,
                    HTTP_X_CRON_SECRET="wrong",
                )
        assert resp.status_code == 401
        mock_call.assert_not_called()
