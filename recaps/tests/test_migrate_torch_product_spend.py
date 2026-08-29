"""Tests for Torch Product Spend migration + receipt-role keyword fallback."""

import pytest
from django.core.management import call_command

from ambassadors.tests.base import AmbassadorsGraphQLTestCase
from recaps import models as recap_models
from recaps.management.commands.migrate_torch_product_spend import (
    PRODUCT_SPEND_NAME,
    scrub_checkin_photo_buckets,
)
from recaps.mutation_parts.file_categories import _resolve_file_recap_category


@pytest.mark.django_db
class TestScrubCheckinPhotoBuckets:
    def test_list_replaces_receipts_with_product_spend(self):
        raw = [
            {"name": "Sampling photos", "min": 3},
            {"name": "Receipts", "helper": "upload spend"},
        ]
        new, changed = scrub_checkin_photo_buckets(raw)
        assert changed is True
        assert new == [
            {"name": "Sampling photos", "min": 3},
            {"name": PRODUCT_SPEND_NAME, "helper": "upload spend"},
        ]

    def test_dict_per_program_and_idempotent(self):
        raw = {
            "Retail Sampling": [{"name": "Product Spend"}, {"name": "Receipts"}],
            "default": [{"name": "Sampling photos"}],
        }
        new, changed = scrub_checkin_photo_buckets(raw)
        assert changed is True
        assert new["Retail Sampling"] == [{"name": PRODUCT_SPEND_NAME}]
        again, changed2 = scrub_checkin_photo_buckets(new)
        assert changed2 is False


@pytest.mark.django_db
class TestReceiptSentinelResolvesToProductSpend(AmbassadorsGraphQLTestCase):
    @pytest.fixture(autouse=True)
    def setup(self, db):
        self.tenant = self.create_tenant(name="Torch THC", slug="torch-thc")
        self.system_user = self.get_system_user()
        recap_models.FileRecapCategory.objects.create(
            name="Sampling photos", tenant=self.tenant, created_by=self.system_user
        )
        self.spend = recap_models.FileRecapCategory.objects.create(
            name=PRODUCT_SPEND_NAME, tenant=self.tenant, created_by=self.system_user
        )

    def test_receipts_sentinel_uses_spend_keyword(self):
        resolved = _resolve_file_recap_category("2", tenant_id=self.tenant.id)
        assert resolved is not None
        assert resolved.id == self.spend.id

    def test_product_spend_name_resolves(self):
        resolved = _resolve_file_recap_category("Product Spend", tenant_id=self.tenant.id)
        assert resolved is not None
        assert resolved.id == self.spend.id


@pytest.mark.django_db
class TestMigrateTorchProductSpendCommand(AmbassadorsGraphQLTestCase):
    @pytest.fixture(autouse=True)
    def setup(self, db):
        self.tenant = self.create_tenant(name="Torch THC", slug="torch-thc")
        self.system_user = self.get_system_user()
        self.receipts = recap_models.FileRecapCategory.objects.create(
            name="Receipts", tenant=self.tenant, created_by=self.system_user
        )
        self.spend = recap_models.FileRecapCategory.objects.create(
            name=PRODUCT_SPEND_NAME, tenant=self.tenant, created_by=self.system_user
        )
        self.tenant.checkin_photo_buckets = [
            {"name": "Sampling photos"},
            {"name": "Receipts"},
        ]
        self.tenant.save(update_fields=["checkin_photo_buckets"])
        et = self.create_event_type("Retail Sampling", self.tenant)
        event = self.create_event(name="Torch store", tenant=self.tenant, event_type=et)
        template = recap_models.CustomRecapTemplate.objects.create(
            name="Torch Recap", tenant=self.tenant, event_type=et, created_by=self.system_user
        )
        recap = recap_models.CustomRecap.objects.create(
            name="Torch recap",
            tenant=self.tenant,
            event=event,
            custom_recap_template=template,
            created_by=self.system_user,
            updated_by=self.system_user,
        )
        ft = recap_models.FileType.objects.create(
            name="Image", extension=".jpg", created_by=self.system_user
        )
        self.file = recap_models.CustomRecapFile.objects.create(
            name="receipt.jpg",
            url="recaps/receipts/x.jpg",
            file_type=ft,
            file_recap_category=self.receipts,
            custom_recap=recap,
            created_by=self.system_user,
        )

    def test_apply_moves_files_scrubs_buckets_deletes_receipts(self):
        call_command("migrate_torch_product_spend", apply=True)
        self.file.refresh_from_db()
        assert self.file.file_recap_category_id == self.spend.id
        assert not recap_models.FileRecapCategory.objects.filter(id=self.receipts.id).exists()
        self.tenant.refresh_from_db()
        names = [e["name"] for e in self.tenant.checkin_photo_buckets]
        assert "Receipts" not in names
        assert PRODUCT_SPEND_NAME in names

    def test_dry_run_writes_nothing(self):
        call_command("migrate_torch_product_spend")
        self.file.refresh_from_db()
        assert self.file.file_recap_category_id == self.receipts.id
