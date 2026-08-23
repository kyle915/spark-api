"""Luckin Coffee Sampling Recap: seeder shape + walk-up apply."""

from __future__ import annotations

import io

import pytest
from django.core.management import call_command
from django.test import Client, override_settings

from recaps.management.commands.setup_luckin_checkin import (
    CODE_PREFIX,
    PHOTO_BUCKETS,
    SPEC,
    TEMPLATE_NAME,
    TENANT_NAME,
    TENANT_SLUG,
)
from tenants.tests.base import BaseGraphQLTestCase

VALID_SECRET = "test-cron-secret-value-only-for-tests"
LUCKIN_CRON_URL = "/internal/cron/setup-luckin-checkin"


class TestLuckinSpec:
    def test_covers_the_sampling_recap_pdf_fields(self):
        names = [f[0] for _, fields in SPEC for f in fields]
        assert names == [
            "Walk-by traffic",
            "Number of people approached and engaged",
            "Number of App download QR scans",
            "Number of store visits",
            "Number of merch distributed",
            "Remaining merch",
            "Best-performing time window",
            "Key customer feedback/issues",
            "Customer profile",
        ]

    def test_photo_bucket_for_future_sampling_shots(self):
        assert [b["name"] for b in PHOTO_BUCKETS] == ["Sampling Pictures"]

    def test_code_prefix_is_brand_scoped(self):
        assert CODE_PREFIX == "LC-"
        assert TEMPLATE_NAME == "Luckin Coffee · Sampling Recap"
        assert TENANT_SLUG == "luckin-coffee"


@pytest.mark.django_db(transaction=True)
class TestLuckinSetupCommand(BaseGraphQLTestCase):
    @pytest.fixture(autouse=True)
    def setup(self, db):
        self.roles = self.setup_default_roles()
        self.user = self.get_system_user()
        self.user.is_superuser = True
        self.user.save(update_fields=["is_superuser"])

    def _run(self, **kw):
        out = io.StringIO()
        call_command("setup_luckin_checkin", stdout=out, **kw)
        return out.getvalue()

    def test_apply_creates_tenant_template_code_and_buckets(self):
        from recaps.models import CustomField, CustomRecapTemplate
        from tenants.models import Tenant

        log = self._run(tenant="luckin", apply=True)
        assert "APPLIED" in log
        tenant = Tenant.objects.get(slug=TENANT_SLUG)
        assert tenant.name == TENANT_NAME
        assert tenant.checkin_code and tenant.checkin_code.startswith("LC-")

        tpl = CustomRecapTemplate.objects.get(tenant=tenant, name=TEMPLATE_NAME)
        names = list(
            CustomField.objects.filter(custom_recap_template=tpl)
            .order_by("recap_section__order", "order", "id")
            .values_list("name", flat=True)
        )
        assert len(names) == 9


@pytest.mark.django_db
class TestLuckinSetupCronView:
    def test_valid_secret_fires_command(self):
        client = Client()
        with override_settings(INTERNAL_CRON_SECRET=VALID_SECRET):
            from unittest.mock import patch

            with patch("digest.cron_views.call_command") as mock_call:
                resp = client.post(
                    LUCKIN_CRON_URL,
                    {"tenant": "luckin"},
                    HTTP_X_CRON_SECRET=VALID_SECRET,
                )
        assert resp.status_code == 200
        mock_call.assert_called_once()
        assert mock_call.call_args[0][0] == "setup_luckin_checkin"
