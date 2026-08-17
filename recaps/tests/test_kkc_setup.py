"""Krispy Krunchy Chicken field-sampling recap: seeder shape + walk-up apply.

Pins the template to the client's PDF (Anaelsa Ortiz, 02/16/2025) so a future
edit is deliberate, and proves the standing-link command can create the
tenant from scratch without the Girl Beer seed-gap (no FileRecapCategory).
"""

from __future__ import annotations

import io

import pytest
from django.core.management import call_command
from django.test import Client, override_settings

from recaps.management.commands.setup_krispy_krunchy_checkin import (
    CODE_PREFIX,
    PHOTO_BUCKETS,
    SPEC,
    TEMPLATE_NAME,
    TENANT_NAME,
    TENANT_SLUG,
)
from recaps.types import _consumers_sampled_from_fields
from tenants.tests.base import BaseGraphQLTestCase

VALID_SECRET = "test-cron-secret-value-only-for-tests"
KKC_CRON_URL = "/internal/cron/setup-krispy-krunchy-checkin"

EXCLUDED = (
    "city",
    "sampling location #1",
    "what time did you sample?",
    "todays date",
    "date:",
)


class TestKkcSpec:
    def test_covers_the_pdf_minus_the_three_screenshot_fields(self):
        names = [f[0] for _, fields in SPEC for f in fields]
        assert names == [
            "How many TOTAL consumers did you sample?",
            "Sampling Location #2",
            "What time did you sample at location #2?",
            "How many did you sample at location #2?",
            "Sampling Location #3",
            "What time did you sample at location #3?",
            "How many did you sample at location #3?",
            "Consumer Feedback/Quotes",
            "How many consumers would you estimate had heard of "
            "Krispy Krunchy Chicken before?",
            "How many consumers mentioned they had tried Krispy "
            "Krunchy Chicken previously?",
            "Anything you'd improve or change?",
        ]

    def test_screenshot_and_event_fields_are_not_on_the_form(self):
        lowered = [f[0].lower() for _, fields in SPEC for f in fields]
        for banned in EXCLUDED:
            assert not any(n == banned or n.startswith(banned + " ") for n in lowered)
        assert not any(n == "city" or n.startswith("city ") for n in lowered)
        assert not any("sampling location #1" in n for n in lowered)
        # Location-#1 time used the bare PDF label; #2/#3 keep a suffix.
        assert "what time did you sample?" not in lowered

    def test_the_sampled_count_field_is_matchable_exactly_once(self):
        sampled = [
            f[0]
            for _, fields in SPEC
            for f in fields
            if _consumers_sampled_from_fields([(f[0], "1")]) == 1
        ]
        assert sampled == ["How many TOTAL consumers did you sample?"]

    def test_location_counts_do_not_steal_the_kpi(self):
        assert (
            _consumers_sampled_from_fields(
                [
                    ("How many TOTAL consumers did you sample?", "248"),
                    ("How many did you sample at location #2?", "294"),
                    ("How many did you sample at location #3?", "0"),
                ]
            )
            == 248
        )

    def test_photo_buckets_match_the_pdf_labels(self):
        assert [b["name"] for b in PHOTO_BUCKETS] == [
            "Consumer Sampling Pictures",
            "Expense Receipts",
        ]

    def test_kinds_are_canonical_and_not_image(self):
        """Photos are buckets, not template image fields — two image
        fields would share one grid on the walk-up page."""
        kinds = {f[1] for _, fields in SPEC for f in fields}
        assert kinds <= {"text", "number", "longtext", "select", "multiselect"}
        assert "image" not in kinds

    def test_code_prefix_is_brand_scoped(self):
        assert CODE_PREFIX == "KKC-"
        assert TEMPLATE_NAME.startswith("Krispy Krunchy Chicken")
        assert TENANT_SLUG == "krispy-krunchy-chicken"


@pytest.mark.django_db(transaction=True)
class TestKkcSetupCommand(BaseGraphQLTestCase):
    @pytest.fixture(autouse=True)
    def setup(self, db):
        self.roles = self.setup_default_roles()
        self.user = self.get_system_user()
        self.user.is_superuser = True
        self.user.save(update_fields=["is_superuser"])

    def _run(self, **kw):
        out = io.StringIO()
        call_command("setup_krispy_krunchy_checkin", stdout=out, **kw)
        return out.getvalue()

    def test_dry_run_creates_nothing(self):
        from recaps.models import CustomRecapTemplate
        from tenants.models import Tenant

        log = self._run(tenant="krispy")
        assert "DRY-RUN" in log
        assert TENANT_NAME in log
        assert not Tenant.objects.filter(slug=TENANT_SLUG).exists()
        assert not CustomRecapTemplate.objects.filter(name=TEMPLATE_NAME).exists()

    def test_apply_creates_tenant_template_code_and_buckets(self):
        from events.models import EventType
        from recaps.models import CustomField, CustomRecapTemplate, FileRecapCategory
        from tenants.models import Tenant

        log = self._run(tenant="krispy", apply=True)
        assert "APPLIED" in log
        tenant = Tenant.objects.get(slug=TENANT_SLUG)
        assert tenant.name == TENANT_NAME
        assert tenant.checkin_code and tenant.checkin_code.startswith("KKC-")
        assert tenant.checkin_location_mode == Tenant.CHECKIN_LOCATION_ADDRESS
        assert tenant.checkin_photo_buckets == PHOTO_BUCKETS
        assert tenant.checkin_event_type is not None
        assert "Retail Sampling" in tenant.checkin_event_type.name
        assert EventType.objects.filter(tenant=tenant).count() >= 3
        assert FileRecapCategory.objects.filter(tenant=tenant).count() >= 2
        assert FileRecapCategory.objects.filter(
            tenant=tenant, name="Consumer Sampling Pictures"
        ).exists()
        assert FileRecapCategory.objects.filter(
            tenant=tenant, name="Expense Receipts"
        ).exists()
        # Default seeds from createTenant — the Girl Beer leak was zero of these.
        assert FileRecapCategory.objects.filter(
            tenant=tenant, name="Sampling photos"
        ).exists()

        tpl = CustomRecapTemplate.objects.get(tenant=tenant, name=TEMPLATE_NAME)
        names = list(
            CustomField.objects.filter(custom_recap_template=tpl)
            .order_by("recap_section__order", "order", "id")
            .values_list("name", flat=True)
        )
        assert "How many TOTAL consumers did you sample?" in names
        assert "Consumer Feedback/Quotes" in names
        assert not any("City" == n for n in names)
        assert not any("Sampling Location #1" in n for n in names)

        # Re-apply keeps the minted code.
        code = tenant.checkin_code
        log2 = self._run(tenant="krispy", apply=True)
        tenant.refresh_from_db()
        assert tenant.checkin_code == code
        assert "already set" in log2


@pytest.mark.django_db
class TestKkcSetupCronView:
    def test_valid_secret_fires_command(self):
        client = Client()
        with override_settings(INTERNAL_CRON_SECRET=VALID_SECRET):
            from unittest.mock import patch

            with patch("digest.cron_views.call_command") as mock_call:
                resp = client.post(
                    KKC_CRON_URL,
                    {"tenant": "krispy"},
                    HTTP_X_CRON_SECRET=VALID_SECRET,
                )
        assert resp.status_code == 200
        body = resp.json()
        assert body["ok"] is True
        assert body["apply"] is False
        mock_call.assert_called_once()
        assert mock_call.call_args[0][0] == "setup_krispy_krunchy_checkin"

    def test_bad_secret_returns_401(self):
        client = Client()
        with override_settings(INTERNAL_CRON_SECRET=VALID_SECRET):
            from unittest.mock import patch

            with patch("digest.cron_views.call_command") as mock_call:
                resp = client.post(
                    KKC_CRON_URL,
                    HTTP_X_CRON_SECRET="wrong",
                )
        assert resp.status_code == 401
        mock_call.assert_not_called()
