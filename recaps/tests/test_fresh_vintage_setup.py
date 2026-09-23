"""Fresh Vintage Farms Costco Roadshow: seeder shape + walk-up apply."""

from __future__ import annotations

import io

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import Client, override_settings

from recaps.fresh_vintage import (
    ACCOUNT_SPEND,
    CODE_PREFIX,
    CONSUMERS_SAMPLED,
    ESTIMATED_SALES,
    NOT_WILLING,
    PHOTO_BUCKETS,
    PROGRAM_NAME,
    SPEC,
    TEMPLATE_NAME,
    TENANT_NAME,
    TENANT_SLUG,
    TOTAL_UNITS,
    UNITS_SOLD_ALMOND,
    UNITS_SOLD_GARLIC,
    WILLING,
    roadshow_derived,
)
from recaps.spend_amount import is_spend_amount_field
from recaps.types import _consumers_sampled_from_fields, _sold_units_from_fields
from tenants.tests.base import BaseGraphQLTestCase

VALID_SECRET = "test-cron-secret-value-only-for-tests"
FVF_CRON_URL = "/internal/cron/setup-fresh-vintage-checkin"


class TestFreshVintageSpec:
    def test_section_order_and_field_names(self):
        assert [section for section, _ in SPEC] == [
            "Product Samples",
            "Sales Performance",
            "Sampling",
            "Account Feedback",
            "Visit Details",
            "Expenses",
            "Customer Feedback",
        ]
        names = [f[0] for _, fields in SPEC for f in fields]
        assert names == [
            "Expeller Pressed Almond Oil: bottles opened for sampling",
            "Expeller Pressed Garlic Almond Oil: bottles opened for sampling",
            "Costco retail price: Almond Oil",
            "Costco retail price: Garlic Almond Oil",
            "Starting inventory: Almond Oil",
            "Starting inventory: Garlic Almond Oil",
            "Ending inventory: Almond Oil",
            "Ending inventory: Garlic Almond Oil",
            "Units sold: Almond Oil",
            "Units sold: Garlic Almond Oil",
            "Total units sold today",
            "Estimated sales $",
            "Did either SKU sell out?",
            "Sell-out time",
            "Total number of consumers sampled",
            "# sampled Almond Oil",
            "# sampled Garlic Almond Oil",
            "# of Females Sampled",
            "# of Males Sampled",
            "How many were first-time Fresh Vintage Farms consumers?",
            "How many had heard of Fresh Vintage Farms before?",
            "How many already cook with almond oil?",
            "What oil do most members currently use?",
            "How many would be willing to purchase after tasting?",
            "How many would NOT be willing to purchase after tasting?",
            "Conversion rate",
            (
                "Costco contact spoken to (name + title, e.g. Roadshow "
                "Coordinator, Front End Manager)"
            ),
            "Any feedback from the warehouse / roadshow coordinator?",
            "Is there anything about the event you'd change next time?",
            "Any additional notes or observations?",
            "Booth location in warehouse",
            "Foot traffic level",
            "Busiest time window",
            "Other roadshows or demos nearby (brands/categories)?",
            "Any inventory, pallet, or pricing sign issues?",
            "Inventory, pallet, or pricing sign details",
            "Total spend on the card",
            "Which flavor did members prefer?",
            "A few comments you heard from members about the product?",
            "What were 2 positive stories or reactions from today?",
            "What were the top 2 reasons members declined to purchase?",
            "Other reason members declined",
            "How did members say they'd use it?",
            (
                "General demographics of members sampled "
                "(age range, gender, ethnicity)"
            ),
        ]

    def test_consumers_sampled_matches_only_total_number(self):
        sampled = [
            f[0]
            for _, fields in SPEC
            for f in fields
            if _consumers_sampled_from_fields([(f[0], "1")]) == 1
        ]
        assert sampled == [CONSUMERS_SAMPLED]

    def test_willing_to_purchase_fields_are_not_sold_units(self):
        assert _sold_units_from_fields([(WILLING, "20")]) is None
        assert _sold_units_from_fields([(NOT_WILLING, "3")]) is None

    def test_per_sku_units_sold_sum_without_double_counting_total(self):
        pairs = [
            (UNITS_SOLD_ALMOND, "12"),
            (UNITS_SOLD_GARLIC, "5"),
            (TOTAL_UNITS, "17"),
        ]
        assert _sold_units_from_fields(pairs) == 17

    def test_total_spend_on_card_is_spend_amount_not_receipts_bucket(self):
        assert is_spend_amount_field(ACCOUNT_SPEND, "number") is True
        assert is_spend_amount_field("Expense Receipts", "image") is False

    def test_roadshow_derived_inventory_sales_and_conversion(self):
        derived = roadshow_derived(
            {
                "Starting inventory: Almond Oil": "40",
                "Ending inventory: Almond Oil": "28",
                "Starting inventory: Garlic Almond Oil": "20",
                "Ending inventory: Garlic Almond Oil": "15",
                "Costco retail price: Almond Oil": "12.99",
                "Costco retail price: Garlic Almond Oil": "14.99",
                CONSUMERS_SAMPLED: "50",
            }
        )
        assert derived[UNITS_SOLD_ALMOND] == "12"
        assert derived[UNITS_SOLD_GARLIC] == "5"
        assert derived[TOTAL_UNITS] == "17"
        expected_sales = 12 * 12.99 + 5 * 14.99
        assert derived[ESTIMATED_SALES] == f"{expected_sales:.2f}"
        assert derived["Conversion rate"] == "34.0%"

    def test_photo_buckets_include_expense_receipts(self):
        names = [b["name"] for b in PHOTO_BUCKETS]
        assert names == [
            "Full booth setup, straight-on",
            "Product display / pallet with price sign visible",
            "Ambassador at booth in uniform",
            "Sampling in action with members",
            "End-of-day remaining inventory",
            "Expense Receipts",
        ]
        assert "Allergen signage" not in names
        assert CODE_PREFIX == "FVF-"
        assert TEMPLATE_NAME.startswith("Fresh Vintage Farms")
        assert PROGRAM_NAME == "Costco Roadshow"
        assert TENANT_SLUG == "fresh-vintage-farms"
        assert TENANT_NAME == "Fresh Vintage Farms"


@pytest.mark.django_db(transaction=True)
class TestFreshVintageSetupCommand(BaseGraphQLTestCase):
    @pytest.fixture(autouse=True)
    def setup(self, db):
        self.roles = self.setup_default_roles()
        self.user = self.get_system_user()
        self.user.is_superuser = True
        self.user.save(update_fields=["is_superuser"])

    def _run(self, **kw):
        out = io.StringIO()
        call_command("setup_fresh_vintage_checkin", stdout=out, **kw)
        return out.getvalue()

    def test_dry_run_creates_nothing(self):
        from recaps.models import CustomRecapTemplate
        from tenants.models import Tenant

        log = self._run(tenant=TENANT_SLUG)
        assert "DRY-RUN" in log
        assert TENANT_NAME in log
        assert not Tenant.objects.filter(slug=TENANT_SLUG).exists()
        assert not CustomRecapTemplate.objects.filter(name=TEMPLATE_NAME).exists()

    def test_non_exact_tenant_errors_no_substring_match(self):
        with pytest.raises(CommandError, match="exact slug or name"):
            self._run(tenant="vintage")
        with pytest.raises(CommandError, match="exact slug or name"):
            self._run(tenant="fresh", apply=True)

    def test_apply_creates_tenant_template_code_and_buckets(self):
        from events.models import EventType
        from recaps.models import CustomField, CustomRecapTemplate, FileRecapCategory
        from tenants.models import Tenant

        log = self._run(tenant=TENANT_SLUG, apply=True)
        assert "APPLIED" in log
        tenant = Tenant.objects.get(slug=TENANT_SLUG)
        assert tenant.name == TENANT_NAME
        assert tenant.checkin_code and tenant.checkin_code.startswith("FVF-")
        assert tenant.checkin_location_mode == Tenant.CHECKIN_LOCATION_ADDRESS
        assert tenant.checkin_photo_buckets == PHOTO_BUCKETS
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
            tenant=tenant, name="Expense Receipts"
        ).exists()
        for bucket in PHOTO_BUCKETS:
            assert FileRecapCategory.objects.filter(
                tenant=tenant, name=bucket["name"]
            ).exists()

        tpl = CustomRecapTemplate.objects.get(tenant=tenant, name=TEMPLATE_NAME)
        names = list(
            CustomField.objects.filter(custom_recap_template=tpl)
            .order_by("recap_section__order", "order", "id")
            .values_list("name", flat=True)
        )
        assert names == [f[0] for _, fields in SPEC for f in fields]
        assert tpl.event_type.name == PROGRAM_NAME

        code = tenant.checkin_code
        log2 = self._run(tenant=TENANT_SLUG, apply=True)
        tenant.refresh_from_db()
        assert tenant.checkin_code == code
        assert "already set" in log2

    def test_apply_updates_stale_photo_buckets_without_reminting_code(self):
        from tenants.models import Tenant

        self._run(tenant=TENANT_SLUG, apply=True)
        tenant = Tenant.objects.get(slug=TENANT_SLUG)
        code = tenant.checkin_code
        stale = PHOTO_BUCKETS + [{"name": "Allergen signage", "min": 1}]
        tenant.checkin_photo_buckets = stale
        tenant.save(update_fields=["checkin_photo_buckets"])

        log = self._run(tenant=TENANT_SLUG, apply=True)
        tenant.refresh_from_db()
        assert tenant.checkin_code == code
        assert tenant.checkin_photo_buckets == PHOTO_BUCKETS
        assert "checkin_photo_buckets set" in log
        assert "Allergen signage" not in [
            b["name"] for b in tenant.checkin_photo_buckets
        ]
        assert f"Check-in code already set: {code}" in log


@pytest.mark.django_db
class TestFreshVintageSetupCronView:
    def test_valid_secret_fires_command(self):
        client = Client()
        with override_settings(INTERNAL_CRON_SECRET=VALID_SECRET):
            from unittest.mock import patch

            with patch("digest.cron_views.call_command") as mock_call:
                resp = client.post(
                    FVF_CRON_URL,
                    {"tenant": TENANT_SLUG},
                    HTTP_X_CRON_SECRET=VALID_SECRET,
                )
        assert resp.status_code == 200
        body = resp.json()
        assert body["ok"] is True
        assert body["apply"] is False
        mock_call.assert_called_once()
        assert mock_call.call_args[0][0] == "setup_fresh_vintage_checkin"

    def test_bad_secret_returns_401(self):
        client = Client()
        with override_settings(INTERNAL_CRON_SECRET=VALID_SECRET):
            from unittest.mock import patch

            with patch("digest.cron_views.call_command") as mock_call:
                resp = client.post(
                    FVF_CRON_URL,
                    HTTP_X_CRON_SECRET="wrong",
                )
        assert resp.status_code == 401
        mock_call.assert_not_called()
