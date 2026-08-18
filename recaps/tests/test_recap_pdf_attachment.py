"""Approve-notify PDF attachments must be JSON-safe for Resend."""

import base64
import json
from unittest.mock import patch

import pytest
from asgiref.sync import async_to_sync

from jobs.tests.base import JobsGraphQLTestCase
from recaps import models as recap_models
from recaps.mutation_parts.pdf_helpers import _resolve_recap_pdf_attachment


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
