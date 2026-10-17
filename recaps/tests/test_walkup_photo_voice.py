"""Walk-up photo classification, photo reuse checks, and voice drafts.

The model calls are mocked. A missing key degrades to a clear unavailable
reason. Reuse is a hash/url check with no score and does not approve.
"""

from __future__ import annotations

from datetime import datetime, timezone as dt_timezone
from unittest.mock import patch

import pytest
from django.test import override_settings

from ambassadors.models import FileType
from ambassadors.tests.base import AmbassadorsGraphQLTestCase
from recaps import models as recap_models
from recaps.photo_check import (
    PHOTO_CHECK_UNAVAILABLE,
    PHOTO_SYSTEM,
    VISION_FEATURES,
    classify_photo,
    kind_from_labels,
)
from recaps.photo_reuse import normalize_content_sha256, photo_reuse_checks
from recaps.voice_draft import (
    VOICE_DRAFT_UNAVAILABLE,
    draft_slot,
    sanitize_fills,
    structure_transcript,
)

DIGEST = "a" * 64


def test_hash_normalizer_rejects_junk():
    assert normalize_content_sha256(DIGEST) == DIGEST
    assert normalize_content_sha256("  " + DIGEST.upper() + " ") == DIGEST
    assert normalize_content_sha256("not-a-hash") == ""
    assert normalize_content_sha256(None) == ""


def test_vision_features_do_not_detect_faces():
    assert "Do not identify" in PHOTO_SYSTEM
    blob = str(VISION_FEATURES).upper()
    assert "FACE" not in blob
    assert "LABEL_DETECTION" in blob


def test_shelf_labels_with_no_product():
    kind, product = kind_from_labels(["Shelf", "Refrigerator"])
    assert kind == "shelf"
    assert product is False


def test_draft_slot_skips_numbers_and_keeps_prose():
    assert draft_slot("Total engagements", "number") is None
    assert draft_slot("How many people did you survey", "text") is None
    assert draft_slot("Cans sampled", "text") is None
    assert draft_slot("Feedback & Account Notes", "long text") == "feedback"
    assert draft_slot("Account notes", "text") == "notes"
    assert draft_slot("What competitor products do consumers drink?", "text") == "competitor"
    assert draft_slot("Why they like it", "long text") == "competitor"
    assert draft_slot("Store name", "text") is None


def test_sanitize_drops_bare_numbers_and_unknown_fields():
    fills = sanitize_fills(
        [
            {"fieldId": "notes", "text": "Aisle was slow after lunch."},
            {"fieldId": "notes", "text": "second"},
            {"fieldId": "count", "text": "40"},
            {"fieldId": "spend", "text": "12.50"},
            {"fieldId": "nope", "text": "ignored"},
        ],
        {"notes", "count", "spend"},
    )
    assert fills == [{"fieldId": "notes", "text": "Aisle was slow after lunch."}]


@override_settings(OPENAI_API_KEY="")
def test_voice_draft_unavailable_without_a_key():
    result = structure_transcript(
        "People liked the peach. A couple asked about the other brand.",
        [
            {"id": "1", "name": "Account notes", "type": "text"},
            {"id": "2", "name": "Total engagements", "type": "number"},
        ],
    )
    assert result == {
        "available": False,
        "reason": VOICE_DRAFT_UNAVAILABLE,
        "fills": [],
    }


@override_settings(OPENAI_API_KEY="test-key")
def test_voice_draft_keeps_only_prose_fields():
    def _fake(system, user, *, schema, max_tokens):
        assert "Total engagements" not in user
        return {
            "fills": [
                {"fieldId": "1", "text": "Busy aisle, people asked about peach."},
                {"fieldId": "9", "text": "40"},
            ]
        }

    with patch("recaps.voice_draft.generate_json", side_effect=_fake):
        result = structure_transcript(
            "Busy aisle. I handed out 40.",
            [
                {"id": "1", "name": "Account notes", "type": "text"},
                {"id": "9", "name": "Total engagements", "type": "number"},
            ],
        )
    assert result["available"] is True
    assert result["fills"] == [
        {"fieldId": "1", "text": "Busy aisle, people asked about peach."}
    ]


@override_settings(OPENAI_API_KEY="")
def test_photo_check_unavailable_when_nothing_can_see():
    with patch("recaps.photo_check._vision_label_names", return_value=None):
        result = classify_photo(
            b"jpeg-bytes",
            content_type="image/jpeg",
            bucket_names=["Before & After Shelf / Stock"],
            chosen_bucket_name="Before & After Shelf / Stock",
        )
    assert result["available"] is False
    assert result["reason"] == PHOTO_CHECK_UNAVAILABLE
    assert result["message"] == PHOTO_CHECK_UNAVAILABLE


@override_settings(OPENAI_API_KEY="test-key")
def test_photo_check_warns_on_empty_shelf_and_does_not_reject():
    with patch(
        "recaps.photo_check.generate_vision_json",
        return_value={"bucket": "shelf", "productVisible": False},
    ):
        result = classify_photo(
            b"jpeg-bytes",
            content_type="image/jpeg",
            bucket_names=["Before & After Shelf / Stock", "Table Set Up"],
            chosen_bucket_name="Before & After Shelf / Stock",
        )
    assert result["available"] is True
    assert result["emptyShelf"] is True
    assert result["bucket"] == "shelf"
    assert "keep it" in result["message"].lower()


@override_settings(OPENAI_API_KEY="")
def test_photo_check_uses_vision_labels_when_openai_is_unset():
    with patch(
        "recaps.photo_check._vision_label_names",
        return_value=["Table", "Tablecloth"],
    ):
        result = classify_photo(
            b"jpeg-bytes",
            content_type="image/jpeg",
            bucket_names=["Table Set Up", "Sampling photos"],
            chosen_bucket_name="Sampling photos",
        )
    assert result["available"] is True
    assert result["bucket"] == "table_setup"
    assert result["suggestedBucketName"] == "Table Set Up"
    assert "Retake" in result["message"]


@pytest.mark.django_db(transaction=True)
class TestPhotoReuseChecks(AmbassadorsGraphQLTestCase):
    @pytest.fixture(autouse=True)
    def setup(self, db):
        self.roles = self.setup_default_roles()
        self.system_user = self.get_system_user()
        self.tenant = self.create_tenant(name="Brew Dr")
        self.file_type = FileType.objects.create(name="image", created_by=self.system_user)
        self.event = self.create_event(name="Tuesday", tenant=self.tenant)
        self.event.start_time = datetime(2026, 3, 3, 18, 0, tzinfo=dt_timezone.utc)
        self.event.save(update_fields=["start_time"])
        self.other_event = self.create_event(name="Thursday", tenant=self.tenant)
        self.other_event.start_time = datetime(2026, 3, 5, 18, 0, tzinfo=dt_timezone.utc)
        self.other_event.save(update_fields=["start_time"])
        self.event_type = self.create_event_type(name="Retail Sampling", tenant=self.tenant)
        self.template = recap_models.CustomRecapTemplate.objects.create(
            name="Retail",
            event_type=self.event_type,
            tenant=self.tenant,
            created_by=self.system_user,
        )

    def _recap(self, event, **kwargs):
        return recap_models.CustomRecap.objects.create(
            name="recap",
            event=event,
            tenant=self.tenant,
            custom_recap_template=self.template,
            created_by=self.system_user,
            approved=False,
            **kwargs,
        )

    def _file(self, recap, path, digest=""):
        return recap_models.CustomRecapFile.objects.create(
            name=path,
            url=path,
            file_type=self.file_type,
            custom_recap=recap,
            approved=False,
            content_sha256=digest,
            created_by=self.system_user,
        )

    def test_same_hash_on_another_date_is_a_check_not_a_score(self):
        current = self._recap(self.event)
        other = self._recap(self.other_event)
        self._file(current, "recap_files/checkin/a.jpg", DIGEST)
        self._file(other, "recap_files/checkin/b.jpg", DIGEST)

        result = photo_reuse_checks(current.id, is_custom=True)

        assert "score" not in result
        assert result["items"][0]["status"] == "reused"
        assert "2026-03-05" in result["items"][0]["label"]
        current.refresh_from_db()
        assert current.approved is False

    def test_same_recap_is_not_flagged(self):
        current = self._recap(self.event)
        self._file(current, "recap_files/checkin/only.jpg", DIGEST)
        result = photo_reuse_checks(current.id, is_custom=True)
        assert result["items"][0]["status"] == "clear"

    def test_co_activation_copies_are_not_reuse(self):
        host = self._recap(self.event, co_activation_key="11111111-1111-1111-1111-111111111111")
        slice_recap = self._recap(
            self.event,
            co_activation_key=host.co_activation_key,
            co_activation_parent=host,
        )
        self._file(host, "recap_files/checkin/shared.jpg", DIGEST)
        self._file(slice_recap, "recap_files/checkin/shared.jpg", DIGEST)
        result = photo_reuse_checks(host.id, is_custom=True)
        assert result["items"][0]["status"] == "clear"

    def test_same_stored_path_on_another_recap(self):
        current = self._recap(self.event)
        other = self._recap(self.other_event)
        self._file(current, "recap_files/checkin/same.jpg")
        self._file(other, "recap_files/checkin/same.jpg")
        result = photo_reuse_checks(current.id, is_custom=True)
        assert result["items"][0]["status"] == "reused"


REUSE_QUERY = """
query Reuse($recapId: ID!, $isCustom: Boolean!) {
  recapPhotoReuse(recapId: $recapId, isCustom: $isCustom) {
    items { fileId status label }
  }
}
"""


@pytest.mark.django_db(transaction=True)
class TestPhotoReuseResolver(AmbassadorsGraphQLTestCase):
    @pytest.fixture(autouse=True)
    def setup(self, db):
        from config.schema_client import schema_clients

        self.roles = self.setup_default_roles()
        self.schema = schema_clients
        self.endpoint_path = "/api/v1/graphql/clients"
        self.system_user = self.get_system_user()
        self.tenant = self.create_tenant(name="Brew Dr")
        self.other = self.create_tenant(name="Other")
        self.spark_admin = self.create_user(
            username="admin-reuse",
            email="admin-reuse@test.com",
            role=self.roles["spark_admin"],
        )
        self.client_user = self.create_user(
            username="client-reuse",
            email="client-reuse@test.com",
            role=self.roles["client"],
        )
        self.create_tenanted_user(self.client_user, self.tenant)
        self.file_type = FileType.objects.create(name="image", created_by=self.system_user)
        self.event = self.create_event(name="Demo", tenant=self.tenant)
        self.event_type = self.create_event_type(name="Retail Sampling", tenant=self.tenant)
        self.template = recap_models.CustomRecapTemplate.objects.create(
            name="Retail",
            event_type=self.event_type,
            tenant=self.tenant,
            created_by=self.system_user,
        )
        self.recap = recap_models.CustomRecap.objects.create(
            name="ours",
            event=self.event,
            tenant=self.tenant,
            custom_recap_template=self.template,
            created_by=self.system_user,
            approved=False,
        )
        recap_models.CustomRecapFile.objects.create(
            name="a.jpg",
            url="recap_files/a.jpg",
            file_type=self.file_type,
            custom_recap=self.recap,
            content_sha256=DIGEST,
            approved=False,
            created_by=self.system_user,
        )
        self.other_event = self.create_event(name="Theirs", tenant=self.other)
        self.other_type = self.create_event_type(name="Other Sampling", tenant=self.other)
        self.other_template = recap_models.CustomRecapTemplate.objects.create(
            name="Other",
            event_type=self.other_type,
            tenant=self.other,
            created_by=self.system_user,
        )
        self.other_recap = recap_models.CustomRecap.objects.create(
            name="theirs",
            event=self.other_event,
            tenant=self.other,
            custom_recap_template=self.other_template,
            created_by=self.system_user,
        )

    @pytest.mark.asyncio
    async def test_admin_check_has_no_score(self):
        result = await self._execute_query_authenticated(
            REUSE_QUERY,
            {"recapId": str(self.recap.id), "isCustom": True},
            self.spark_admin,
            self.endpoint_path,
        )
        assert result.errors is None, f"errored: {result.errors}"
        payload = result.data["recapPhotoReuse"]
        assert "score" not in payload
        assert payload["items"][0]["status"] == "clear"

    @pytest.mark.asyncio
    async def test_client_cannot_read_another_tenant(self):
        result = await self._execute_query_authenticated(
            REUSE_QUERY,
            {"recapId": str(self.other_recap.id), "isCustom": True},
            self.client_user,
            self.endpoint_path,
        )
        assert result.errors is None, f"errored: {result.errors}"
        assert result.data["recapPhotoReuse"] == {"items": []}
