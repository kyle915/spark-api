"""Clean vs Needs Eyes triage: rules over model calls, staff-only GraphQL."""

from __future__ import annotations

from datetime import timedelta

import pytest
from django.utils import timezone

from ambassadors.models import FileType
from ambassadors.tests.base import AmbassadorsGraphQLTestCase
from recaps import models as recap_models
from recaps.review_triage import (
    LABEL_CLEAN,
    LABEL_NEEDS_EYES,
    recap_review_triage,
    recap_review_triage_for_ids,
)


@pytest.mark.django_db(transaction=True)
class TestRecapReviewTriage(AmbassadorsGraphQLTestCase):
    @pytest.fixture(autouse=True)
    def setup(self, db):
        from config.schema_client import schema_clients

        self.schema = schema_clients
        self.endpoint_path = "/api/v1/graphql/clients"
        self.roles = self.setup_default_roles()
        self.system_user = self.get_system_user()
        self.tenant = self.create_tenant(name="Triage Brand")
        self.tenant.checkin_photo_buckets = [
            {"name": "Sampling Photos"},
            {"name": "Product Spend", "min": 1},
        ]
        self.tenant.save(update_fields=["checkin_photo_buckets"])
        self.admin = self.create_user(
            username="triage-admin",
            email="triage-admin@test.com",
            role=self.roles["spark_admin"],
        )
        self.client_user = self.create_user(
            username="triage-client",
            email="triage-client@test.com",
            role=self.roles["client"],
        )
        self.create_tenanted_user(self.client_user, self.tenant)
        start = (timezone.now() - timedelta(hours=3)).replace(
            minute=0, second=0, microsecond=0
        )
        self.event = self.create_event(
            name="Store Demo",
            tenant=self.tenant,
            start_time=start,
            end_time=start + timedelta(hours=2),
        )
        self.event.start_time = start
        self.event.end_time = start + timedelta(hours=2)
        self.event.coordinates = [40.0, -74.0]
        self.event.save()
        self.file_type = FileType.objects.create(
            name="image", created_by=self.system_user
        )
        self.sampling_cat = recap_models.FileRecapCategory.objects.create(
            name="Sampling Photos",
            tenant=self.tenant,
            created_by=self.system_user,
        )
        self.spend_cat = recap_models.FileRecapCategory.objects.create(
            name="Product Spend",
            tenant=self.tenant,
            created_by=self.system_user,
        )
        self.event_type = self.create_event_type(
            name="Retail Sampling", tenant=self.tenant
        )
        self.template = recap_models.CustomRecapTemplate.objects.create(
            name="Triage Template",
            event_type=self.event_type,
            tenant=self.tenant,
            created_by=self.system_user,
        )
        self.field_type = recap_models.CustomRecapFieldType.objects.create(
            name="text", created_by=self.system_user
        )
        self.section = recap_models.RecapSection.objects.create(
            name="Feedback", tenant=self.tenant, created_by=self.system_user
        )
        self.feedback_field = recap_models.CustomField.objects.create(
            name="Consumer Feedback",
            custom_recap_template=self.template,
            custom_field_type=self.field_type,
            recap_section=self.section,
            created_by=self.system_user,
        )

    def _recap(self, with_feedback: bool = True, **kw):
        defaults = dict(
            name="custom",
            event=self.event,
            tenant=self.tenant,
            custom_recap_template=self.template,
            created_by=self.system_user,
            updated_by=self.system_user,
            total_engagements=100,
        )
        defaults.update(kw)
        recap = recap_models.CustomRecap.objects.create(**defaults)
        if with_feedback:
            recap_models.CustomFieldValue.objects.create(
                custom_recap=recap,
                custom_field=self.feedback_field,
                value="Customers loved the crisp citrus taste and bought 2 packs.",
                created_by=self.system_user,
            )
        return recap

    def _file(self, recap, blob, cat=None):
        return recap_models.CustomRecapFile.objects.create(
            name=blob,
            url=blob,
            file_type=self.file_type,
            custom_recap=recap,
            file_recap_category=cat,
            created_by=self.system_user,
        )

    def test_clean_recap_with_photos_and_spend_proof(self):
        recap = self._recap()
        self._file(recap, "recap_files/a.jpg", self.sampling_cat)
        self._file(recap, "recap_files/b.jpg", self.spend_cat)
        result = recap_review_triage(recap.id, is_custom=True)
        assert result["label"] == LABEL_CLEAN
        assert result["reasons"] == []

    def test_absurd_engagements_needs_eyes(self):
        # 5000 over 2h → 2500/hr, past the 300/hr review-check line.
        recap = self._recap(total_engagements=5000)
        self._file(recap, "recap_files/a.jpg", self.sampling_cat)
        self._file(recap, "recap_files/b.jpg", self.spend_cat)
        result = recap_review_triage(recap.id, is_custom=True)
        assert result["label"] == LABEL_NEEDS_EYES
        codes = {r["code"] for r in result["reasons"]}
        assert "engagements_rate" in codes

    def test_missing_photos_quality_needs_eyes(self):
        recap = self._recap(total_engagements=40)
        result = recap_review_triage(recap.id, is_custom=True)
        assert result["label"] == LABEL_NEEDS_EYES
        sources = {r["source"] for r in result["reasons"]}
        assert sources & {"quality", "review_check"}

    def test_blurry_photo_score_needs_eyes(self):
        recap = self._recap(total_engagements=40)
        sampling = self._file(recap, "recap_files/a.jpg", self.sampling_cat)
        self._file(recap, "recap_files/b.jpg", self.spend_cat)
        recap_models.RecapPhotoScore.objects.create(
            custom_recap_file=sampling,
            status=recap_models.RecapPhotoScore.STATUS_SCORED,
            score=20,
            kind="sampling",
            caption="Blurry table",
        )
        result = recap_review_triage(recap.id, is_custom=True)
        assert result["label"] == LABEL_NEEDS_EYES
        assert any(r["code"] == "photo_low_quality" for r in result["reasons"])

    def test_shelf_oos_gap_needs_eyes(self):
        recap = self._recap(total_engagements=40)
        sampling = self._file(recap, "recap_files/shelf.jpg", self.sampling_cat)
        self._file(recap, "recap_files/spend.jpg", self.spend_cat)
        recap_models.ShelfReading.objects.create(
            custom_recap_file=sampling,
            status=recap_models.ShelfReading.STATUS_READ,
            product_on_shelf=recap_models.ShelfReading.YES,
            out_of_stock_gap=recap_models.ShelfReading.YES,
            price_tag_visible=recap_models.ShelfReading.YES,
        )
        result = recap_review_triage(recap.id, is_custom=True)
        assert result["label"] == LABEL_NEEDS_EYES
        assert any(r["code"] == "shelf_oos_gap" for r in result["reasons"])

    def test_shelf_staff_correction_clears_oos_needs_eyes(self):
        """Staff ShelfReadingCorrection wins over the raw model reading."""
        recap = self._recap(total_engagements=40)
        sampling = self._file(recap, "recap_files/shelf.jpg", self.sampling_cat)
        self._file(recap, "recap_files/spend.jpg", self.spend_cat)
        recap_models.ShelfReading.objects.create(
            custom_recap_file=sampling,
            status=recap_models.ShelfReading.STATUS_READ,
            product_on_shelf=recap_models.ShelfReading.YES,
            out_of_stock_gap=recap_models.ShelfReading.YES,
            price_tag_visible=recap_models.ShelfReading.YES,
        )
        # Model said OOS gap; staff corrected to no gap.
        recap_models.ShelfReadingCorrection.objects.create(
            custom_recap_file=sampling,
            product_on_shelf=recap_models.ShelfReading.YES,
            out_of_stock_gap=recap_models.ShelfReading.NO,
            price_tag_visible=recap_models.ShelfReading.YES,
            corrected_by=self.admin,
        )
        result = recap_review_triage(recap.id, is_custom=True)
        codes = {r["code"] for r in result["reasons"]}
        assert "shelf_oos_gap" not in codes
        assert result["label"] == LABEL_CLEAN

    def test_batch_helper_and_invalid_id(self):
        clean = self._recap(total_engagements=40)
        self._file(clean, "recap_files/a.jpg", self.sampling_cat)
        self._file(clean, "recap_files/b.jpg", self.spend_cat)
        batched = recap_review_triage_for_ids([clean.id], is_custom=True)
        assert batched[clean.id]["label"] == LABEL_CLEAN
        assert recap_review_triage("not-an-int") == {
            "label": LABEL_CLEAN,
            "reasons": [],
        }

    @pytest.mark.asyncio
    async def test_graphql_staff_only_and_labels(self):
        from asgiref.sync import sync_to_async

        clean = await sync_to_async(self._recap, thread_sensitive=True)(
            name="clean", total_engagements=40
        )
        await sync_to_async(self._file, thread_sensitive=True)(
            clean, "recap_files/a.jpg", self.sampling_cat
        )
        await sync_to_async(self._file, thread_sensitive=True)(
            clean, "recap_files/b.jpg", self.spend_cat
        )
        flagged = await sync_to_async(self._recap, thread_sensitive=True)(
            name="flagged", total_engagements=5000
        )
        await sync_to_async(self._file, thread_sensitive=True)(
            flagged, "recap_files/c.jpg", self.sampling_cat
        )
        await sync_to_async(self._file, thread_sensitive=True)(
            flagged, "recap_files/d.jpg", self.spend_cat
        )

        query = """
            query Q($ids: [ID!]!, $isCustom: Boolean!) {
              recapReviewTriage(recapIds: $ids, isCustom: $isCustom) {
                recapId
                label
                reasons { code label source }
              }
            }
        """
        vars = {
            "ids": [str(clean.id), str(flagged.id)],
            "isCustom": True,
        }
        admin_result = await self._execute_query_authenticated(
            query, vars, self.admin, self.endpoint_path
        )
        assert admin_result.errors is None, admin_result.errors
        by_id = {
            str(row["recapId"]): row
            for row in admin_result.data["recapReviewTriage"]
        }
        assert by_id[str(clean.id)]["label"] == LABEL_CLEAN
        assert by_id[str(flagged.id)]["label"] == LABEL_NEEDS_EYES
        assert any(
            r["code"] == "engagements_rate"
            for r in by_id[str(flagged.id)]["reasons"]
        )

        # Brand / client role must not receive the staff-only field.
        client_result = await self._execute_query_authenticated(
            query, vars, self.client_user, self.endpoint_path
        )
        assert client_result.errors
        assert "Only Ignite admins" in str(client_result.errors[0].message)
        assert client_result.data is None or client_result.data.get(
            "recapReviewTriage"
        ) is None
