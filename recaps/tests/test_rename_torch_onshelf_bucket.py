"""Tests for Torch On-Shelf → Before & After Shelf / Stock rename."""

import pytest
from django.core.management import call_command

from ambassadors.tests.base import AmbassadorsGraphQLTestCase
from recaps import models as recap_models
from recaps.management.commands.rename_torch_onshelf_bucket import (
    NEW_NAME,
    PHOTO_BUCKETS,
    scrub_checkin_photo_buckets,
)


@pytest.mark.django_db
class TestScrubCheckinPhotoBuckets:
    def test_list_renames_onshelf(self):
        raw = [
            {"name": "Sampling photos"},
            {"name": "Table Set Up"},
            {"name": "On-Shelf Product"},
            {"name": "Product Spend"},
        ]
        new, changed = scrub_checkin_photo_buckets(raw)
        assert changed is True
        assert new == [
            {"name": "Sampling photos"},
            {"name": "Table Set Up"},
            {"name": NEW_NAME},
            {"name": "Product Spend"},
        ]

    def test_dict_per_program_and_idempotent(self):
        raw = {
            "Retail Sampling": [
                {"name": "On Shelf Product"},
                {"name": NEW_NAME},
            ],
            "default": [{"name": "Sampling photos"}],
        }
        new, changed = scrub_checkin_photo_buckets(raw)
        assert changed is True
        assert new["Retail Sampling"] == [{"name": NEW_NAME}]
        again, changed2 = scrub_checkin_photo_buckets(new)
        assert changed2 is False

    def test_seed_buckets_include_new_label_and_product_spend(self):
        names = [b["name"] for b in PHOTO_BUCKETS]
        assert names == [
            "Sampling photos",
            "Table Set Up",
            NEW_NAME,
            "Product Spend",
        ]
        assert "Receipts" not in names
        assert "On-Shelf Product" not in names
        shelf = next(b for b in PHOTO_BUCKETS if b["name"] == NEW_NAME)
        assert "On-Shelf Product" in shelf["aliases"]


@pytest.mark.django_db
class TestRenameTorchOnshelfBucketCommand(AmbassadorsGraphQLTestCase):
    @pytest.fixture(autouse=True)
    def setup(self, db):
        self.tenant = self.create_tenant(name="Torch THC", slug="torch-thc")
        self.tenant.checkin_code = "TH-2HRV3D"
        self.system_user = self.get_system_user()
        self.onshelf = recap_models.FileRecapCategory.objects.create(
            name="On-Shelf Product",
            tenant=self.tenant,
            created_by=self.system_user,
        )
        self.tenant.checkin_photo_buckets = [
            {"name": "Sampling photos"},
            {"name": "Table Set Up"},
            {"name": "On-Shelf Product"},
            {"name": "Product Spend"},
        ]
        self.tenant.save(update_fields=["checkin_code", "checkin_photo_buckets"])

    def test_apply_renames_category_and_buckets(self):
        call_command("rename_torch_onshelf_bucket", apply=True)
        self.onshelf.refresh_from_db()
        assert self.onshelf.name == NEW_NAME
        self.tenant.refresh_from_db()
        names = [e["name"] for e in self.tenant.checkin_photo_buckets]
        assert names == [
            "Sampling photos",
            "Table Set Up",
            NEW_NAME,
            "Product Spend",
        ]
        assert self.tenant.checkin_code == "TH-2HRV3D"

    def test_dry_run_writes_nothing(self):
        call_command("rename_torch_onshelf_bucket")
        self.onshelf.refresh_from_db()
        assert self.onshelf.name == "On-Shelf Product"
        self.tenant.refresh_from_db()
        assert self.tenant.checkin_photo_buckets[2]["name"] == "On-Shelf Product"

    def test_idempotent_when_already_renamed(self):
        self.onshelf.name = NEW_NAME
        self.onshelf.save(update_fields=["name", "updated_at"])
        self.tenant.checkin_photo_buckets = [
            {"name": "Sampling photos"},
            {"name": NEW_NAME},
            {"name": "Product Spend"},
        ]
        self.tenant.save(update_fields=["checkin_photo_buckets"])
        call_command("rename_torch_onshelf_bucket", apply=True)
        self.onshelf.refresh_from_db()
        assert self.onshelf.name == NEW_NAME
        assert recap_models.FileRecapCategory.objects.filter(
            tenant=self.tenant, name=NEW_NAME
        ).count() == 1
