"""Approve-notify PDF attachments must be JSON-safe and APPROVED-fresh."""

import base64
import json
from datetime import timedelta
from unittest.mock import MagicMock, patch

import pytest
from asgiref.sync import async_to_sync
from django.utils import timezone as django_timezone

from jobs.tests.base import JobsGraphQLTestCase
from events import models as event_models
from recaps import models as recap_models
from recaps.mutation_parts.pdf_helpers import (
    _find_existing_pdf_file,
    _pdf_matches_approval_status,
    _render_and_store_recap_pdf_sync,
    _resolve_recap_pdf_attachment,
)


@pytest.mark.django_db
class TestRecapPdfAttachment(JobsGraphQLTestCase):
    @pytest.fixture(autouse=True)
    def setup(self, db):
        self.roles = self.setup_default_roles()
        self.tenant = self.create_tenant(name="LD Attachment Tenant")
        self.spark_user = self.create_user(
            username="spark_attach@test.com",
            email="spark_attach@test.com",
            role=self.roles["spark_admin"],
            password="testpass123",
        )
        self.create_tenanted_user(user=self.spark_user, tenant=self.tenant)
        self.event = self.create_event(
            name="WI State Fair",
            tenant=self.tenant,
            address="West Allis, WI",
        )
        self.pdf_type = self.create_file_type(name="PDF", extension=".pdf")
        self.event_type = event_models.EventType.objects.create(
            name="Sampling",
            tenant=self.tenant,
            created_by=self.spark_user,
        )
        self.template = recap_models.CustomRecapTemplate.objects.create(
            name="Attachment Template",
            event_type=self.event_type,
            tenant=self.tenant,
            created_by=self.spark_user,
        )

    def test_legacy_recap_pdf_content_is_base64(self):
        recap = recap_models.Recap.objects.create(
            name="Recap 727",
            approved=True,
            event=self.event,
            created_by=self.spark_user,
            updated_by=self.spark_user,
        )
        recap_models.RecapFile.objects.create(
            name="Recap PDF - Recap 727",
            file="recaps/pdfs/recap-727.pdf",
            file_type=self.pdf_type,
            recap=recap,
            approved=False,
            created_by=self.spark_user,
        )
        pdf = b"%PDF-1.4 recap-727"
        with patch(
            "recaps.mutation_parts.pdf_helpers.download_blob_bytes",
            return_value=pdf,
        ):
            attachments = async_to_sync(_resolve_recap_pdf_attachment)(recap)

        assert attachments is not None
        content = attachments[0]["content"]
        assert isinstance(content, str)
        json.dumps(attachments)
        assert base64.b64decode(content) == pdf
        assert attachments[0]["filename"].endswith(".pdf")
        assert attachments[0]["content_type"] == "application/pdf"

    def test_resolve_ignores_non_spark_pdf_attachments(self):
        """Connecteam / upload PDFs must not be emailed as the FIELD RECAP."""
        recap = recap_models.CustomRecap.objects.create(
            name="Torch Big Bend",
            approved=True,
            approved_at=django_timezone.now(),
            event=self.event,
            tenant=self.tenant,
            custom_recap_template=self.template,
            created_by=self.spark_user,
            updated_by=self.spark_user,
        )
        recap_models.CustomRecapFile.objects.create(
            name="connecteam-source.pdf",
            url="uploads/connecteam-source.pdf",
            file_type=self.pdf_type,
            custom_recap=recap,
            approved=False,
            created_by=self.spark_user,
        )
        attachments = async_to_sync(_resolve_recap_pdf_attachment)(recap)
        assert attachments is None
        assert _find_existing_pdf_file(recap) is None


@pytest.mark.django_db
class TestRecapPdfApprovalFreshness(JobsGraphQLTestCase):
    @pytest.fixture(autouse=True)
    def setup(self, db):
        self.roles = self.setup_default_roles()
        self.tenant = self.create_tenant(name="Torch PDF Freshness")
        self.spark_user = self.create_user(
            username="spark_pdf_fresh@test.com",
            email="spark_pdf_fresh@test.com",
            role=self.roles["spark_admin"],
            password="testpass123",
        )
        self.create_tenanted_user(user=self.spark_user, tenant=self.tenant)
        self.event = self.create_event(
            name="Big Bend Liquor",
            tenant=self.tenant,
            address="Big Bend, WI",
        )
        self.pdf_type = self.create_file_type(name="PDF", extension=".pdf")
        self.event_type = event_models.EventType.objects.create(
            name="Torch Sampling",
            tenant=self.tenant,
            created_by=self.spark_user,
        )
        self.template = recap_models.CustomRecapTemplate.objects.create(
            name="Torch Template",
            event_type=self.event_type,
            tenant=self.tenant,
            created_by=self.spark_user,
        )

    def _custom_recap(self, *, approved: bool, approved_at=None):
        return recap_models.CustomRecap.objects.create(
            name="8/27/2026 - Big Bend Liquor",
            approved=approved,
            approved_at=approved_at,
            event=self.event,
            tenant=self.tenant,
            custom_recap_template=self.template,
            created_by=self.spark_user,
            updated_by=self.spark_user,
        )

    def test_draft_pdf_matches_unapproved_recap(self):
        recap = self._custom_recap(approved=False)
        pdf = MagicMock(created_at=django_timezone.now())
        assert _pdf_matches_approval_status(recap, pdf) is True

    def test_pre_approval_pdf_is_stale_after_approve(self):
        approved_at = django_timezone.now()
        recap = self._custom_recap(approved=True, approved_at=approved_at)
        pdf = MagicMock(created_at=approved_at - timedelta(minutes=5))
        assert _pdf_matches_approval_status(recap, pdf) is False

    def test_post_approval_pdf_is_current(self):
        approved_at = django_timezone.now()
        recap = self._custom_recap(approved=True, approved_at=approved_at)
        pdf = MagicMock(created_at=approved_at + timedelta(seconds=1))
        assert _pdf_matches_approval_status(recap, pdf) is True

    def test_approved_without_approved_at_is_stale(self):
        recap = self._custom_recap(approved=True, approved_at=None)
        pdf = MagicMock(created_at=django_timezone.now())
        assert _pdf_matches_approval_status(recap, pdf) is False

    def test_notify_regenerates_stale_draft_pdf(self):
        """Approve-notify must replace a pre-approval DRAFT snapshot."""
        approved_at = django_timezone.now()
        recap = self._custom_recap(approved=True, approved_at=approved_at)
        stale = recap_models.CustomRecapFile.objects.create(
            name=f"Custom Recap PDF - {recap.name}",
            url="recaps/pdfs/custom-stale-draft.pdf",
            file_type=self.pdf_type,
            custom_recap=recap,
            approved=False,
            created_by=self.spark_user,
        )
        # Force created_at before approval (auto_now_add ignores create kwargs).
        recap_models.CustomRecapFile.objects.filter(id=stale.id).update(
            created_at=approved_at - timedelta(hours=1)
        )
        stale.refresh_from_db()
        assert _pdf_matches_approval_status(recap, stale) is False

        with (
            patch(
                "recaps.mutation_parts.pdf_helpers.build_recap_pdf",
                return_value=b"%PDF-1.4 APPROVED",
            ) as mock_build,
            patch("recaps.mutation_parts.pdf_helpers.upload_bytes"),
            patch("recaps.mutation_parts.pdf_helpers.delete_blob") as mock_delete,
        ):
            fresh = _render_and_store_recap_pdf_sync(recap)

        assert fresh is not None
        assert fresh.id != stale.id
        assert fresh.name.startswith("Custom Recap PDF -")
        assert not recap_models.CustomRecapFile.objects.filter(id=stale.id).exists()
        mock_build.assert_called_once()
        # build_recap_pdf receives the approved recap → APPROVED badge.
        assert mock_build.call_args.args[0].approved is True
        mock_delete.assert_called()

    def test_notify_reuses_post_approval_pdf(self):
        approved_at = django_timezone.now() - timedelta(minutes=1)
        recap = self._custom_recap(approved=True, approved_at=approved_at)
        current = recap_models.CustomRecapFile.objects.create(
            name=f"Custom Recap PDF - {recap.name}",
            url="recaps/pdfs/custom-approved.pdf",
            file_type=self.pdf_type,
            custom_recap=recap,
            approved=False,
            created_by=self.spark_user,
        )
        with patch(
            "recaps.mutation_parts.pdf_helpers.build_recap_pdf"
        ) as mock_build:
            reused = _render_and_store_recap_pdf_sync(recap)

        assert reused.id == current.id
        mock_build.assert_not_called()

    def test_connecteam_pdf_does_not_block_spark_generation(self):
        recap = self._custom_recap(
            approved=True, approved_at=django_timezone.now()
        )
        recap_models.CustomRecapFile.objects.create(
            name="connecteam-source.pdf",
            url="uploads/connecteam.pdf",
            file_type=self.pdf_type,
            custom_recap=recap,
            approved=False,
            created_by=self.spark_user,
        )
        with (
            patch(
                "recaps.mutation_parts.pdf_helpers.build_recap_pdf",
                return_value=b"%PDF-1.4 spark",
            ),
            patch("recaps.mutation_parts.pdf_helpers.upload_bytes"),
            patch("recaps.mutation_parts.pdf_helpers.delete_blob"),
        ):
            created = _render_and_store_recap_pdf_sync(recap)

        assert created is not None
        assert created.name.startswith("Custom Recap PDF -")
        # Connecteam source PDF must still be present.
        assert recap.custom_recap_files.filter(
            name="connecteam-source.pdf"
        ).exists()
