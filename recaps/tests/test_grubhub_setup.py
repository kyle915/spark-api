"""Grub Hub BA Event Recap: seeder shape + walk-up apply.

Pins the template to the client's "BA EVENT RECAP" PDF (Leah Love, #1446)
plus Kyle's two campus GrubHub account-linking counts, and proves the
standing-link command can create the tenant from scratch.
"""

from __future__ import annotations

import io

import pytest
from django.core.management import call_command
from django.test import Client, override_settings

from recaps.management.commands.setup_grubhub_checkin import (
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
GRUBHUB_CRON_URL = "/internal/cron/setup-grubhub-checkin"

EXCLUDED = (
    "date:",
    "event location",
    "todays date",
)


class TestGrubhubSpec:
    def test_covers_the_pdf_plus_campus_linking_fields(self):
        names = [f[0] for _, fields in SPEC for f in fields]
        assert names == [
            "Event Name",
            "How many consumers did you interact with?",
            (
                "How many students knew they could link their GrubHub "
                "account on-campus?"
            ),
            "How many students did you help link their account?",
            "Consumer Feedback/Quotes",
            "Anything you'd improve or change?",
        ]

    def test_date_and_location_are_not_on_the_form(self):
        lowered = [f[0].lower() for _, fields in SPEC for f in fields]
        for banned in EXCLUDED:
            assert not any(n == banned or n.startswith(banned + " ") for n in lowered)

    def test_the_interact_count_field_is_matchable_exactly_once(self):
        sampled = [
            f[0]
            for _, fields in SPEC
            for f in fields
            if _consumers_sampled_from_fields([(f[0], "1")]) == 1
        ]
        assert sampled == ["How many consumers did you interact with?"]

    def test_photo_buckets_match_the_pdf_labels(self):
        assert [b["name"] for b in PHOTO_BUCKETS] == [
            "Event Pictures",
            "Expense Receipts",
        ]

    def test_kinds_are_canonical_and_not_image(self):
        kinds = {f[1] for _, fields in SPEC for f in fields}
        assert kinds <= {"text", "number", "longtext", "select", "multiselect"}
        assert "image" not in kinds

    def test_code_prefix_is_brand_scoped(self):
        assert CODE_PREFIX == "GH-"
        assert TEMPLATE_NAME.startswith("Grub Hub")
        assert TENANT_SLUG == "grub-hub"
        assert TENANT_NAME == "Grub Hub"


@pytest.mark.django_db(transaction=True)
class TestGrubhubSetupCommand(BaseGraphQLTestCase):
    @pytest.fixture(autouse=True)
    def setup(self, db):
        self.roles = self.setup_default_roles()
        self.user = self.get_system_user()
        self.user.is_superuser = True
        self.user.save(update_fields=["is_superuser"])

    def _run(self, **kw):
        out = io.StringIO()
        call_command("setup_grubhub_checkin", stdout=out, **kw)
        return out.getvalue()

    def test_dry_run_creates_nothing(self):
        from recaps.models import CustomRecapTemplate
        from tenants.models import Tenant

        log = self._run(tenant="grub")
        assert "DRY-RUN" in log
        assert TENANT_NAME in log
        assert not Tenant.objects.filter(slug=TENANT_SLUG).exists()
        assert not CustomRecapTemplate.objects.filter(name=TEMPLATE_NAME).exists()

    def test_apply_creates_tenant_template_code_and_buckets(self):
        from events.models import EventType
        from recaps.models import CustomField, CustomRecapTemplate, FileRecapCategory
        from tenants.models import Tenant

        log = self._run(tenant="grub", apply=True)
        assert "APPLIED" in log
        tenant = Tenant.objects.get(slug=TENANT_SLUG)
        assert tenant.name == TENANT_NAME
        assert tenant.checkin_code and tenant.checkin_code.startswith("GH-")
        assert tenant.checkin_location_mode == Tenant.CHECKIN_LOCATION_ADDRESS
        assert tenant.checkin_photo_buckets == PHOTO_BUCKETS
        assert tenant.checkin_event_type is not None
        assert tenant.checkin_event_type.name == "Event"
        assert list(
            tenant.checkin_event_types.values_list("name", flat=True)
        ) == ["Event"]
        assert list(
            EventType.objects.filter(tenant=tenant)
            .order_by("id")
            .values_list("name", flat=True)
        ) == ["Event"]
        assert FileRecapCategory.objects.filter(tenant=tenant).count() >= 2
        assert FileRecapCategory.objects.filter(
            tenant=tenant, name="Consumer Sampling Pictures"
        ).exists()
        assert FileRecapCategory.objects.filter(
            tenant=tenant, name="Expense Receipts"
        ).exists()

        tpl = CustomRecapTemplate.objects.get(tenant=tenant, name=TEMPLATE_NAME)
        names = list(
            CustomField.objects.filter(custom_recap_template=tpl)
            .order_by("recap_section__order", "order", "id")
            .values_list("name", flat=True)
        )
        assert names == [
            "Event Name",
            "How many consumers did you interact with?",
            (
                "How many students knew they could link their GrubHub "
                "account on-campus?"
            ),
            "How many students did you help link their account?",
            "Consumer Feedback/Quotes",
            "Anything you'd improve or change?",
        ]
        assert tpl.event_type.name == "Event"

        code = tenant.checkin_code
        log2 = self._run(tenant="grub", apply=True)
        tenant.refresh_from_db()
        assert tenant.checkin_code == code
        assert "already set" in log2


@pytest.mark.django_db
class TestGrubhubSetupCronView:
    def test_valid_secret_fires_command(self):
        client = Client()
        with override_settings(INTERNAL_CRON_SECRET=VALID_SECRET):
            from unittest.mock import patch

            with patch("digest.cron_views.call_command") as mock_call:
                resp = client.post(
                    GRUBHUB_CRON_URL,
                    {"tenant": "grub"},
                    HTTP_X_CRON_SECRET=VALID_SECRET,
                )
        assert resp.status_code == 200
        body = resp.json()
        assert body["ok"] is True
        assert body["apply"] is False
        mock_call.assert_called_once()
        assert mock_call.call_args[0][0] == "setup_grubhub_checkin"

    def test_bad_secret_returns_401(self):
        client = Client()
        with override_settings(INTERNAL_CRON_SECRET=VALID_SECRET):
            from unittest.mock import patch

            with patch("digest.cron_views.call_command") as mock_call:
                resp = client.post(
                    GRUBHUB_CRON_URL,
                    HTTP_X_CRON_SECRET="wrong",
                )
        assert resp.status_code == 401
        mock_call.assert_not_called()
