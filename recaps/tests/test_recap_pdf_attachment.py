"""Approve-notify PDF attachments must be JSON-safe for Resend."""

import base64
import datetime
import json
from unittest.mock import patch

import pytest
from asgiref.sync import async_to_sync
from django.utils import timezone as django_timezone

from jobs.tests.base import JobsGraphQLTestCase
from recaps import models as recap_models
from recaps.mutation_parts.pdf_helpers import (
    _find_existing_pdf_file,
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


@pytest.mark.django_db
class TestExistingPdfStaleness(JobsGraphQLTestCase):
    """A cached PDF bakes in the DRAFT/APPROVED chip and field values, so it
    must NOT be reused once the recap has changed (e.g. an admin approved it)."""

    @pytest.fixture(autouse=True)
    def setup(self, db):
        self.roles = self.setup_default_roles()
        self.tenant = self.create_tenant(name="Stale PDF Tenant")
        self.user = self.create_user(
            username="stale_pdf@test.com",
            email="stale_pdf@test.com",
            role=self.roles["spark_admin"],
            password="testpass123",
        )
        self.create_tenanted_user(user=self.user, tenant=self.tenant)
        self.event = self.create_event(
            name="Total Wireless San Juan",
            tenant=self.tenant,
            address="31921 Camino Capistrano, San Juan Capistrano, CA",
        )
        self.pdf_type = self.create_file_type(name="PDF", extension=".pdf")
        self.recap = recap_models.Recap.objects.create(
            name="Damien Ford recap",
            approved=True,
            event=self.event,
            created_by=self.user,
            updated_by=self.user,
        )
        self.pdf_file = recap_models.RecapFile.objects.create(
            name="Recap PDF",
            file="recaps/pdfs/recap-damien.pdf",
            file_type=self.pdf_type,
            recap=self.recap,
            approved=False,
            created_by=self.user,
        )

    def _stamp(self, *, pdf_at, recap_updated_at):
        # .update() bypasses auto_now / auto_now_add, so we can pin both clocks.
        recap_models.RecapFile.objects.filter(id=self.pdf_file.id).update(
            created_at=pdf_at
        )
        recap_models.Recap.objects.filter(id=self.recap.id).update(
            updated_at=recap_updated_at
        )
        self.recap.refresh_from_db()

    def test_pdf_built_before_recap_changed_is_stale(self):
        base = django_timezone.now()
        # PDF generated while DRAFT, THEN the recap was approved (updated later).
        self._stamp(
            pdf_at=base,
            recap_updated_at=base + datetime.timedelta(minutes=5),
        )
        assert _find_existing_pdf_file(self.recap) is None

    def test_pdf_built_after_last_change_is_reused(self):
        base = django_timezone.now()
        # PDF generated AFTER the last recap change → still fresh, reuse it.
        self._stamp(
            pdf_at=base + datetime.timedelta(minutes=5),
            recap_updated_at=base,
        )
        found = _find_existing_pdf_file(self.recap)
        assert found is not None
        assert found.id == self.pdf_file.id
