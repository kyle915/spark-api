"""G7 Entertainment Marketing BA Event Recap: seeder shape + walk-up apply.

Pins the template to the client's "BA EVENT RECAP" PDF (Leah Love, #1446)
so a future edit is deliberate, and proves the standing-link command can
create the tenant from scratch without the Girl Beer seed-gap.
"""

from __future__ import annotations

import io

import pytest
from django.core.management import call_command
from django.test import Client, override_settings

from recaps.management.commands.setup_g7_entertainment_checkin import (
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
G7_CRON_URL = "/internal/cron/setup-g7-entertainment-checkin"

EXCLUDED = (
    "date:",
    "event location",
    "todays date",
)


class TestG7Spec:
    def test_covers_the_pdf_minus_date_and_location(self):
        names = [f[0] for _, fields in SPEC for f in fields]
        assert names == [
            "Event Name",
            "How many consumers did you interact with?",
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
        assert CODE_PREFIX == "G7-"
        assert TEMPLATE_NAME.startswith("G7 Entertainment Marketing")
        assert TENANT_SLUG == "g7-entertainment"
        assert TENANT_NAME == "G7 Entertainment Marketing"


@pytest.mark.django_db(transaction=True)
class TestG7SetupCommand(BaseGraphQLTestCase):
    @pytest.fixture(autouse=True)
    def setup(self, db):
        self.roles = self.setup_default_roles()
        self.user = self.get_system_user()
        self.user.is_superuser = True
        self.user.save(update_fields=["is_superuser"])

    def _run(self, **kw):
        out = io.StringIO()
        call_command("setup_g7_entertainment_checkin", stdout=out, **kw)
        return out.getvalue()

    def test_dry_run_creates_nothing(self):
        from recaps.models import CustomRecapTemplate
        from tenants.models import Tenant

        log = self._run(tenant="g7")
        assert "DRY-RUN" in log
        assert TENANT_NAME in log
        assert not Tenant.objects.filter(slug=TENANT_SLUG).exists()
        assert not CustomRecapTemplate.objects.filter(name=TEMPLATE_NAME).exists()

    def test_apply_creates_tenant_template_code_and_buckets(self):
        from events.models import EventType
        from recaps.models import CustomField, CustomRecapTemplate, FileRecapCategory
        from tenants.models import Tenant

        log = self._run(tenant="g7", apply=True)
        assert "APPLIED" in log
        tenant = Tenant.objects.get(slug=TENANT_SLUG)
        assert tenant.name == TENANT_NAME
        assert tenant.checkin_code and tenant.checkin_code.startswith("G7-")
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
        assert FileRecapCategory.objects.filter(
            tenant=tenant, name="Sampling photos"
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
            "Consumer Feedback/Quotes",
            "Anything you'd improve or change?",
        ]
        assert not any(n.lower().startswith("date") for n in names)
        assert not any("location" in n.lower() for n in names)
        assert tpl.event_type.name == "Event"
        assert tpl.product_samples is False
        assert tpl.sales_performance is False

        code = tenant.checkin_code
        log2 = self._run(tenant="g7", apply=True)
        tenant.refresh_from_db()
        assert tenant.checkin_code == code
        assert "already set" in log2

    def test_reapply_moves_template_off_retail_and_retires_extras(self):
        from events.models import EventType
        from recaps.models import CustomRecapTemplate
        from tenants.models import Tenant

        self._run(tenant="g7", apply=True)
        tenant = Tenant.objects.get(slug=TENANT_SLUG)
        code = tenant.checkin_code
        event = tenant.checkin_event_type
        retail = EventType.objects.create(
            name="Retail Sampling",
            slug="retail-sampling",
            tenant=tenant,
            created_by=self.user,
            is_default=True,
        )
        EventType.objects.create(
            name="On-Premise Sampling",
            slug="on-premise-sampling",
            tenant=tenant,
            created_by=self.user,
            is_default=False,
        )
        tpl = CustomRecapTemplate.objects.get(tenant=tenant, name=TEMPLATE_NAME)
        tpl.event_type = retail
        tpl.save(update_fields=["event_type"])
        tenant.checkin_event_type = retail
        tenant.save(update_fields=["checkin_event_type"])
        tenant.checkin_event_types.set([retail, event])

        log = self._run(tenant="g7", apply=True)
        tenant.refresh_from_db()
        tpl.refresh_from_db()
        assert tenant.checkin_code == code
        assert "already set" in log
        assert tenant.checkin_event_type.name == "Event"
        assert tpl.event_type.name == "Event"
        assert list(
            EventType.objects.filter(tenant=tenant).values_list("name", flat=True)
        ) == ["Event"]
        assert list(tenant.checkin_event_types.values_list("name", flat=True)) == [
            "Event"
        ]


@pytest.mark.django_db
class TestG7SetupCronView:
    def test_valid_secret_fires_command(self):
        client = Client()
        with override_settings(INTERNAL_CRON_SECRET=VALID_SECRET):
            from unittest.mock import patch

            with patch("digest.cron_views.call_command") as mock_call:
                resp = client.post(
                    G7_CRON_URL,
                    {"tenant": "g7"},
                    HTTP_X_CRON_SECRET=VALID_SECRET,
                )
        assert resp.status_code == 200
        body = resp.json()
        assert body["ok"] is True
        assert body["apply"] is False
        mock_call.assert_called_once()
        assert mock_call.call_args[0][0] == "setup_g7_entertainment_checkin"

    def test_bad_secret_returns_401(self):
        client = Client()
        with override_settings(INTERNAL_CRON_SECRET=VALID_SECRET):
            from unittest.mock import patch

            with patch("digest.cron_views.call_command") as mock_call:
                resp = client.post(
                    G7_CRON_URL,
                    HTTP_X_CRON_SECRET="wrong",
                )
        assert resp.status_code == 401
        mock_call.assert_not_called()
