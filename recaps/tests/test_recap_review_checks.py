"""Rule-based recap review checks (roadmap #3): deterministic, no AI/score."""

from __future__ import annotations

from datetime import timedelta

import pytest
from django.utils import timezone

from ambassadors.models import Attendance, FileType, Source
from ambassadors.tests.base import AmbassadorsGraphQLTestCase
from recaps import models as recap_models
from recaps.recap_review_checks import recap_review_checks


def _by_code(result):
    return {c["code"]: c for c in result}


@pytest.mark.django_db(transaction=True)
class TestRecapReviewChecks(AmbassadorsGraphQLTestCase):
    @pytest.fixture(autouse=True)
    def setup(self, db):
        self.roles = self.setup_default_roles()
        self.system_user = self.get_system_user()
        self.tenant = self.create_tenant(name="Review Brand")
        self.tenant.checkin_photo_buckets = [
            {"name": "Sampling Photos"},
            {"name": "Product Spend", "min": 1},
            {"name": "Optional Extra", "min": 0},
        ]
        self.tenant.save(update_fields=["checkin_photo_buckets"])
        start = timezone.now().replace(hour=10, minute=0, second=0, microsecond=0)
        self.event = self.create_event(
            name="Store Demo", tenant=self.tenant,
            start_time=start, end_time=start + timedelta(hours=2),
        )
        # create_event may not accept times; set explicitly.
        self.event.start_time = start
        self.event.end_time = start + timedelta(hours=2)
        self.event.coordinates = [40.0, -74.0]
        self.event.save()
        self.file_type = FileType.objects.create(name="image", created_by=self.system_user)
        self.sampling_cat = recap_models.FileRecapCategory.objects.create(
            name="Sampling Photos", tenant=self.tenant, created_by=self.system_user)
        self.spend_cat = recap_models.FileRecapCategory.objects.create(
            name="Product Spend", tenant=self.tenant, created_by=self.system_user)
        self.event_type = self.create_event_type(name="Retail Sampling", tenant=self.tenant)
        self.template = recap_models.CustomRecapTemplate.objects.create(
            name="Review Template", event_type=self.event_type, tenant=self.tenant,
            created_by=self.system_user)
        self.field_type = recap_models.CustomRecapFieldType.objects.create(
            name="text", created_by=self.system_user)
        self.section = recap_models.RecapSection.objects.create(
            name="S", tenant=self.tenant, created_by=self.system_user)

    def _recap(self, **kw):
        defaults = dict(name="custom", event=self.event, tenant=self.tenant,
                        custom_recap_template=self.template,
                        created_by=self.system_user, updated_by=self.system_user,
                        total_engagements=100)
        defaults.update(kw)
        return recap_models.CustomRecap.objects.create(**defaults)

    def _file(self, recap, blob, cat=None):
        return recap_models.CustomRecapFile.objects.create(
            name=blob, url=blob, file_type=self.file_type, custom_recap=recap,
            file_recap_category=cat, created_by=self.system_user)

    def _value(self, recap, name, value):
        field = recap_models.CustomField.objects.create(
            name=name, custom_recap_template=self.template,
            custom_field_type=self.field_type, recap_section=self.section,
            created_by=self.system_user)
        return recap_models.CustomFieldValue.objects.create(
            custom_recap=recap, custom_field=field, value=value,
            created_by=self.system_user)

    def _attendance(self, recap, coords, when):
        source, _ = Source.objects.get_or_create(name="clock_in")
        return Attendance.objects.create(
            clock_time=when, coordinates=coords, event=self.event,
            ambassador=recap.ambassador, source=source)

    def test_clean_recap_passes_and_optional_bucket_skipped(self):
        recap = self._recap()
        self._file(recap, "recap_files/a.jpg", self.sampling_cat)
        self._file(recap, "recap_files/b.jpg", self.spend_cat)
        checks = _by_code(recap_review_checks(recap.id, is_custom=True))
        assert checks["photo_buckets"]["status"] == "pass"
        assert checks["duplicate_photos"]["status"] == "pass"
        assert checks["engagements_rate"]["status"] == "pass"
        assert checks["gps_distance"]["status"] == "na"  # no clock fix yet

    def test_engagements_rate_flags_only_absurd(self):
        recap = self._recap(total_engagements=5000)  # 2500/hr over 2h
        checks = _by_code(recap_review_checks(recap.id, is_custom=True))
        assert checks["engagements_rate"]["status"] == "flag"
        assert "5000" in checks["engagements_rate"]["label"]

    def test_engagements_rate_busy_but_plausible_passes(self):
        recap = self._recap(total_engagements=500)  # 250/hr over 2h
        checks = _by_code(recap_review_checks(recap.id, is_custom=True))
        assert checks["engagements_rate"]["status"] == "pass"

    def test_gps_far_flags_and_shows_distance(self):
        recap = self._recap()
        self._attendance(recap, [41.0, -74.0], timezone.now())  # ~69 mi north
        checks = _by_code(recap_review_checks(recap.id, is_custom=True))
        assert checks["gps_distance"]["status"] == "flag"
        assert "mi" in checks["gps_distance"]["label"]

    def test_gps_near_passes(self):
        recap = self._recap()
        self._attendance(recap, [40.001, -74.001], timezone.now())
        checks = _by_code(recap_review_checks(recap.id, is_custom=True))
        assert checks["gps_distance"]["status"] == "pass"

    def test_missing_bucket_named(self):
        recap = self._recap()
        self._file(recap, "recap_files/a.jpg", self.spend_cat)
        checks = _by_code(recap_review_checks(recap.id, is_custom=True))
        assert checks["photo_buckets"]["status"] == "flag"
        assert "Sampling Photos" in checks["photo_buckets"]["label"]
        assert "Optional Extra" not in checks["photo_buckets"]["label"]

    def test_duplicate_photo_flagged(self):
        recap = self._recap()
        self._file(recap, "recap_files/same.jpg", self.sampling_cat)
        self._file(recap, "recap_files/same.jpg", self.sampling_cat)
        checks = _by_code(recap_review_checks(recap.id, is_custom=True))
        assert checks["duplicate_photos"]["status"] == "flag"
        assert "same.jpg" in checks["duplicate_photos"]["label"]

    def test_spend_without_receipt_flagged(self):
        recap = self._recap()
        self._file(recap, "recap_files/a.jpg", self.sampling_cat)
        self._value(recap, "Total spend", "42.50")
        checks = _by_code(recap_review_checks(recap.id, is_custom=True))
        assert checks["spend_without_receipt"]["status"] == "flag"
        assert "42.50" in checks["spend_without_receipt"]["label"]

    def test_spend_with_product_spend_photo_passes(self):
        recap = self._recap()
        self._file(recap, "recap_files/a.jpg", self.spend_cat)
        self._value(recap, "Product Spend", "42.50")
        checks = _by_code(recap_review_checks(recap.id, is_custom=True))
        assert checks["spend_without_receipt"]["status"] == "pass"

    def test_no_spend_is_na_and_missing_recap_all_na(self):
        recap = self._recap()
        checks = _by_code(recap_review_checks(recap.id, is_custom=True))
        assert checks["spend_without_receipt"]["status"] == "na"
        missing = recap_review_checks(999_999_999, is_custom=True)
        assert all(c["status"] == "na" for c in missing)
        assert recap_review_checks("nope") == missing or all(
            c["status"] == "na" for c in recap_review_checks("nope"))

    def test_legacy_recap_shape(self):
        recap = recap_models.Recap.objects.create(
            name="legacy", event=self.event, total_engagements=10,
            account_spend_amount="25.00",
            created_by=self.system_user, updated_by=self.system_user)
        recap_models.RecapFile.objects.create(
            name="x.jpg", file="recap_files/x.jpg", file_type=self.file_type,
            recap=recap, file_recap_category=self.sampling_cat,
            created_by=self.system_user)
        checks = _by_code(recap_review_checks(recap.id, is_custom=False))
        assert checks["engagements_rate"]["status"] == "pass"
        assert checks["spend_without_receipt"]["status"] == "flag"


@pytest.mark.django_db(transaction=True)
class TestRecapReviewChecksResolver(AmbassadorsGraphQLTestCase):
    @pytest.fixture(autouse=True)
    def setup(self, db):
        from config.schema_client import schema_clients

        self.schema = schema_clients
        self.endpoint_path = "/api/v1/graphql/clients"
        self.roles = self.setup_default_roles()
        self.system_user = self.get_system_user()
        self.tenant = self.create_tenant(name="Resolver Brand")
        start = timezone.now().replace(hour=10, minute=0, second=0, microsecond=0)
        self.event = self.create_event(name="Demo", tenant=self.tenant)
        self.event.start_time = start
        self.event.end_time = start + timedelta(hours=2)
        self.event.save()
        self.admin = self.create_user(
            username="review-admin", email="review-admin@test.com",
            role=self.roles["spark_admin"])
        self.event_type = self.create_event_type(name="Retail", tenant=self.tenant)
        self.template = recap_models.CustomRecapTemplate.objects.create(
            name="T", event_type=self.event_type, tenant=self.tenant,
            created_by=self.system_user)
        self.recap = recap_models.CustomRecap.objects.create(
            name="c", event=self.event, tenant=self.tenant,
            custom_recap_template=self.template, total_engagements=5000,
            created_by=self.system_user, updated_by=self.system_user)

    @pytest.mark.asyncio
    async def test_resolver_returns_checks_and_missing_is_na(self):
        result = await self._execute_query_authenticated(
            """
            query Q($recapId: ID!, $isCustom: Boolean) {
              recapReviewChecks(recapId: $recapId, isCustom: $isCustom) {
                code label status detail
              }
            }
            """,
            {"recapId": str(self.recap.id), "isCustom": True},
            self.admin, self.endpoint_path)
        assert result.errors is None, f"errored: {result.errors}"
        checks = {c["code"]: c for c in result.data["recapReviewChecks"]}
        assert checks["engagements_rate"]["status"] == "flag"
        missing = await self._execute_query_authenticated(
            """
            query Q($recapId: ID!) {
              recapReviewChecks(recapId: $recapId) { code status }
            }
            """,
            {"recapId": "987654321"}, self.admin, self.endpoint_path)
        assert missing.errors is None
        assert all(c["status"] == "na" for c in missing.data["recapReviewChecks"])
