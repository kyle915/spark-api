"""Brand Revolution Event Activation walk-up setup."""

from __future__ import annotations

import io

import pytest
from django.core.management import call_command
from django.test import Client, override_settings

from recaps.management.commands.setup_brand_revolution_checkin import (
    CODE_PREFIX,
    PHOTO_BUCKETS,
    PROGRAM_NAME,
    SPEC,
    TEMPLATE_NAME,
    TENANT_NAME,
    TENANT_SLUG,
)
from tenants.tests.base import BaseGraphQLTestCase

VALID_SECRET = "test-cron-secret-value-only-for-tests"
BR_CRON_URL = "/internal/cron/setup-brand-revolution-checkin"


class TestBrandRevolutionSpec:
    def test_covers_requested_metrics_only(self):
        names = [f[0] for _, fields in SPEC for f in fields]
        assert names == [
            "How many people did you interact with?",
            "How many people did you survey?",
        ]

    def test_photo_bucket_is_recap_pictures(self):
        assert [b["name"] for b in PHOTO_BUCKETS] == ["Recap pictures"]
        assert PHOTO_BUCKETS[0].get("min") == 1

    def test_kinds_are_number_not_image(self):
        kinds = {f[1] for _, fields in SPEC for f in fields}
        assert kinds == {"number"}
        assert "image" not in kinds

    def test_brand_constants(self):
        assert CODE_PREFIX == "BR-"
        assert TEMPLATE_NAME == "Brand Revolution-Event Activation"
        assert PROGRAM_NAME == "Event Activation"
        assert TENANT_SLUG == "brand-revolution"
        assert TENANT_NAME == "Brand Revolution"


@pytest.mark.django_db(transaction=True)
class TestBrandRevolutionSetupCommand(BaseGraphQLTestCase):
    @pytest.fixture(autouse=True)
    def setup(self, db):
        self.roles = self.setup_default_roles()
        self.user = self.get_system_user()
        self.user.is_superuser = True
        self.user.save(update_fields=["is_superuser"])

    def _run(self, **kw):
        out = io.StringIO()
        call_command("setup_brand_revolution_checkin", stdout=out, **kw)
        return out.getvalue()

    def test_dry_run_creates_nothing(self):
        from recaps.models import CustomRecapTemplate
        from tenants.models import Tenant

        log = self._run(tenant="brand-revolution")
        assert "DRY-RUN" in log
        assert TENANT_NAME in log
        assert not Tenant.objects.filter(slug=TENANT_SLUG).exists()
        assert not CustomRecapTemplate.objects.filter(name=TEMPLATE_NAME).exists()

    def test_apply_creates_tenant_template_code_and_buckets(self):
        from events.models import EventType
        from recaps.models import CustomField, CustomRecapTemplate, FileRecapCategory
        from tenants.models import Tenant

        log = self._run(tenant="brand-revolution", apply=True)
        assert "APPLIED" in log
        tenant = Tenant.objects.get(slug=TENANT_SLUG)
        assert tenant.name == TENANT_NAME
        assert tenant.checkin_code and tenant.checkin_code.startswith("BR-")
        assert tenant.checkin_location_mode == Tenant.CHECKIN_LOCATION_ADDRESS
        assert tenant.checkin_photo_buckets == {PROGRAM_NAME: PHOTO_BUCKETS}
        assert tenant.checkin_event_type is not None
        assert tenant.checkin_event_type.name == PROGRAM_NAME
        assert list(
            tenant.checkin_event_types.values_list("name", flat=True)
        ) == [PROGRAM_NAME]
        assert list(
            EventType.objects.filter(tenant=tenant)
            .order_by("id")
            .values_list("name", flat=True)
        ) == [PROGRAM_NAME]
        assert FileRecapCategory.objects.filter(
            tenant=tenant, name="Recap pictures"
        ).exists()

        tpl = CustomRecapTemplate.objects.get(tenant=tenant, name=TEMPLATE_NAME)
        names = list(
            CustomField.objects.filter(custom_recap_template=tpl)
            .order_by("recap_section__order", "order", "id")
            .values_list("name", flat=True)
        )
        assert names == [
            "How many people did you interact with?",
            "How many people did you survey?",
        ]
        assert tpl.event_type.name == PROGRAM_NAME
        assert tpl.product_samples is False

        code = tenant.checkin_code
        log2 = self._run(tenant="brand-revolution", apply=True)
        tenant.refresh_from_db()
        assert tenant.checkin_code == code
        assert "already set" in log2


@pytest.mark.django_db
class TestBrandRevolutionSetupCronView:
    def test_valid_secret_fires_command(self):
        client = Client()
        with override_settings(INTERNAL_CRON_SECRET=VALID_SECRET):
            from unittest.mock import patch

            with patch("digest.cron_views.call_command") as mock_call:
                resp = client.post(
                    BR_CRON_URL,
                    {"tenant": "brand-revolution"},
                    HTTP_X_CRON_SECRET=VALID_SECRET,
                )
        assert resp.status_code == 200
        body = resp.json()
        assert body["ok"] is True
        assert body["apply"] is False
        mock_call.assert_called_once()
        assert mock_call.call_args[0][0] == "setup_brand_revolution_checkin"

    def test_bad_secret_returns_401(self):
        client = Client()
        with override_settings(INTERNAL_CRON_SECRET=VALID_SECRET):
            from unittest.mock import patch

            with patch("digest.cron_views.call_command") as mock_call:
                resp = client.post(
                    BR_CRON_URL,
                    HTTP_X_CRON_SECRET="wrong",
                )
        assert resp.status_code == 401
        mock_call.assert_not_called()
