"""
Cross-tenant READ isolation for the single-recap accessors (clients schema).

Follow-up to the recap WRITE IDOR sweep (#708). #708 gated the mutation
cluster via
``RecapMutationService._assert_caller_authorized_for_recap_tenant`` but
explicitly flagged the READ side as still leaky: the single-recap accessors
loaded a recap (or recap file) by raw id/uuid gated only by
``StrictIsAuthenticated``, so any authenticated user (a client of another
brand, or a BA) could READ another tenant's recap data / file CONTENT by
guessing the id.

These run end to end against the real ``schema_clients`` GraphQL surface and
assert, for BOTH legacy ``Recap`` and ``CustomRecap``, that a tenant-A user
(client AND ambassador) CANNOT, for a tenant-B recap:

  * get a file download URL (``recapFileDownloadUrl``),
  * generate / export a PDF (``generateRecapPdf`` / ``generateCustomRecapPdf``),
  * export an XLSX (``exportRecapXlsx`` / ``exportCustomRecapXlsx``),
  * read the recap detail (``recap`` / ``customRecap`` queries),

while a same-tenant client AND a spark-admin still CAN.

The DENY paths short-circuit at the tenant gate before any GCS I/O, so they
need no patching. The ALLOW paths for the PDF/XLSX/URL accessors touch GCS
(upload/download/public_url); those calls are patched so the test asserts the
authorization outcome (got PAST the gate) without external infra.

Mirrors the fixture style of test_recap_mutation_tenant_isolation (#708).
"""

import pytest
from datetime import datetime, timedelta, timezone as _tz
from unittest.mock import patch

from asgiref.sync import sync_to_async

from ambassadors.models import FileType
from ambassadors.tests.base import AmbassadorsGraphQLTestCase
from recaps import models as recap_models


# ── Read accessors under test ─────────────────────────────────────────

RECAP_FILE_DOWNLOAD_URL_MUTATION = """
mutation RecapFileDownloadUrl($uuid: ID!) {
  recapFileDownloadUrl(input: { uuid: $uuid }) {
    success
    message
    fileUrl
  }
}
"""

GENERATE_RECAP_PDF_MUTATION = """
mutation GenerateRecapPdf($id: ID!) {
  generateRecapPdf(input: { id: $id }) {
    success
    message
    recapFile { uuid }
  }
}
"""

GENERATE_CUSTOM_RECAP_PDF_MUTATION = """
mutation GenerateCustomRecapPdf($id: ID!) {
  generateCustomRecapPdf(input: { id: $id }) {
    success
    message
    customRecapFile { uuid }
  }
}
"""

EXPORT_RECAP_XLSX_MUTATION = """
mutation ExportRecapXlsx($id: ID!) {
  exportRecapXlsx(input: { id: $id }) {
    success
    message
    fileUrl
  }
}
"""

EXPORT_CUSTOM_RECAP_XLSX_MUTATION = """
mutation ExportCustomRecapXlsx($id: ID!) {
  exportCustomRecapXlsx(input: { id: $id }) {
    success
    message
    fileUrl
  }
}
"""

RECAP_QUERY = """
query Recap($uuid: ID!) {
  recap(uuid: $uuid) {
    uuid
  }
}
"""

CUSTOM_RECAP_QUERY = """
query CustomRecap($uuid: ID!) {
  customRecap(uuid: $uuid) {
    uuid
  }
}
"""


@pytest.mark.django_db(transaction=True)
class TestRecapReadTenantIsolation(AmbassadorsGraphQLTestCase):
    @pytest.fixture(autouse=True)
    def setup(self, db):
        from config.schema_client import schema_clients

        self.roles = self.setup_default_roles()
        self.schema = schema_clients
        self.endpoint_path = "/api/v1/graphql/clients"
        self.system_user = self.get_system_user()

        # Tenant A (the caller's tenant) and tenant B (the victim).
        self.tenant = self.create_tenant(name="Girl Beer")
        self.other_tenant = self.create_tenant(name="Liquid Death")

        self.spark_admin = self.create_user(
            username="admin-recap-read-iso",
            email="admin-recap-read-iso@test.com",
            role=self.roles["spark_admin"],
        )
        # Client belongs to tenant A only.
        self.client_user = self.create_user(
            username="client-recap-read-iso",
            email="client-recap-read-iso@test.com",
            role=self.roles["client"],
        )
        self.create_tenanted_user(self.client_user, self.tenant)
        # Ambassador (role 1) — the read side previously never tenant-checked
        # for non-client roles, so a BA could read any tenant's recap.
        self.ba_user = self.create_user(
            username="ba-recap-read-iso",
            email="ba-recap-read-iso@test.com",
            role=self.roles["ambassador"],
        )

        now = datetime.now(_tz.utc)
        # Tenant A event + supporting rows.
        self.event = self.create_event(
            name="Whole Foods Burbank",
            tenant=self.tenant,
            date=now,
            start_time=now,
            end_time=now + timedelta(hours=4),
        )
        self.event_type = self.create_event_type(
            name="Sampling", tenant=self.tenant
        )
        self.template = recap_models.CustomRecapTemplate.objects.create(
            name="GB Template",
            event_type=self.event_type,
            tenant=self.tenant,
            created_by=self.system_user,
        )
        # Tenant B event + supporting rows (the foreign recaps live here).
        self.other_event = self.create_event(
            name="Foreign event",
            tenant=self.other_tenant,
            date=now,
            start_time=now,
            end_time=now + timedelta(hours=4),
        )
        self.other_event_type = self.create_event_type(
            name="Sampling B", tenant=self.other_tenant
        )
        self.other_template = recap_models.CustomRecapTemplate.objects.create(
            name="LD Template",
            event_type=self.other_event_type,
            tenant=self.other_tenant,
            created_by=self.system_user,
        )
        self.file_type = FileType.objects.create(
            name="image", created_by=self.system_user
        )
        # PDF FileType so the generate*Pdf ALLOW paths (which look up a
        # ".pdf" FileType to attach the rendered file) can complete past the
        # authorization gate without external infra.
        self.pdf_file_type = FileType.objects.create(
            name="pdf", extension=".pdf", created_by=self.system_user
        )

    # ── builders ──────────────────────────────────────────────────────

    def _make_recap(self, event, approved=True):
        # approved=True so the client-visibility (approved-only) gate never
        # masks a tenant-authorization PASS — we want to isolate the tenant
        # check, not the draft-hiding behavior.
        return recap_models.Recap.objects.create(
            name="Legacy recap",
            approved=approved,
            event=event,
            created_by=self.system_user,
            updated_by=self.system_user,
        )

    def _make_custom_recap(self, event, tenant, template, approved=True):
        return recap_models.CustomRecap.objects.create(
            name="Custom recap",
            approved=approved,
            event=event,
            tenant=tenant,
            custom_recap_template=template,
            created_by=self.system_user,
            updated_by=self.system_user,
        )

    def _make_recap_file(self, recap, name="photo.jpg"):
        return recap_models.RecapFile.objects.create(
            name=name,
            file="recaps/abc/123-photo.jpg",
            file_type=self.file_type,
            recap=recap,
            approved=False,
            created_by=self.system_user,
        )

    def _make_custom_recap_file(self, custom_recap, name="photo.jpg"):
        return recap_models.CustomRecapFile.objects.create(
            name=name,
            url="recaps/abc/456-photo.jpg",
            file_type=self.file_type,
            custom_recap=custom_recap,
            approved=False,
            created_by=self.system_user,
        )

    @staticmethod
    def _denied(message: str) -> bool:
        # A denial reads either as a role limit ("not authorized…") or, for a
        # cross-tenant / foreign-id probe, as non-existence ("… not found.")
        # so we never confirm another brand's record exists (#247). Both mean
        # "denied" here.
        m = (message or "").lower()
        return "authorized" in m or "not found" in m

    # ════════════════════════════════════════════════════════════════
    # recapFileDownloadUrl — leaks file CONTENT (RecapFile via parent recap)
    # ════════════════════════════════════════════════════════════════

    @pytest.mark.asyncio
    async def test_client_cannot_download_other_tenant_recap_file(self):
        recap = await sync_to_async(self._make_recap)(self.other_event)
        rf = await sync_to_async(self._make_recap_file)(recap)

        result = await self._execute_mutation_authenticated(
            RECAP_FILE_DOWNLOAD_URL_MUTATION,
            {"uuid": str(rf.uuid)},
            self.client_user,
            self.endpoint_path,
        )
        assert result.errors is None, f"errored: {result.errors}"
        payload = result.data["recapFileDownloadUrl"]
        assert payload["success"] is False, payload
        assert self._denied(payload["message"]), payload
        assert payload["fileUrl"] is None, payload

    @pytest.mark.asyncio
    async def test_ambassador_cannot_download_other_tenant_recap_file(self):
        recap = await sync_to_async(self._make_recap)(self.other_event)
        rf = await sync_to_async(self._make_recap_file)(recap)

        result = await self._execute_mutation_authenticated(
            RECAP_FILE_DOWNLOAD_URL_MUTATION,
            {"uuid": str(rf.uuid)},
            self.ba_user,
            self.endpoint_path,
        )
        assert result.errors is None, f"errored: {result.errors}"
        payload = result.data["recapFileDownloadUrl"]
        assert payload["success"] is False, payload
        assert self._denied(payload["message"]), payload

    @pytest.mark.asyncio
    async def test_client_cannot_download_other_tenant_custom_recap_file(self):
        recap = await sync_to_async(self._make_custom_recap)(
            self.other_event, self.other_tenant, self.other_template
        )
        crf = await sync_to_async(self._make_custom_recap_file)(recap)

        result = await self._execute_mutation_authenticated(
            RECAP_FILE_DOWNLOAD_URL_MUTATION,
            {"uuid": str(crf.uuid)},
            self.client_user,
            self.endpoint_path,
        )
        assert result.errors is None, f"errored: {result.errors}"
        payload = result.data["recapFileDownloadUrl"]
        assert payload["success"] is False, payload
        assert self._denied(payload["message"]), payload

    @pytest.mark.asyncio
    async def test_same_tenant_client_can_download_recap_file(self):
        recap = await sync_to_async(self._make_recap)(self.event)
        rf = await sync_to_async(self._make_recap_file)(recap)

        with patch(
            "recaps.mutations.public_url",
            return_value="https://example.test/blob",
        ):
            result = await self._execute_mutation_authenticated(
                RECAP_FILE_DOWNLOAD_URL_MUTATION,
                {"uuid": str(rf.uuid)},
                self.client_user,
                self.endpoint_path,
            )
        assert result.errors is None, f"errored: {result.errors}"
        payload = result.data["recapFileDownloadUrl"]
        assert payload["success"] is True, payload
        assert payload["fileUrl"] == "https://example.test/blob", payload

    @pytest.mark.asyncio
    async def test_spark_admin_can_download_other_tenant_recap_file(self):
        recap = await sync_to_async(self._make_recap)(self.other_event)
        rf = await sync_to_async(self._make_recap_file)(recap)

        with patch(
            "recaps.mutations.public_url",
            return_value="https://example.test/blob",
        ):
            result = await self._execute_mutation_authenticated(
                RECAP_FILE_DOWNLOAD_URL_MUTATION,
                {"uuid": str(rf.uuid)},
                self.spark_admin,
                self.endpoint_path,
            )
        assert result.errors is None, f"errored: {result.errors}"
        payload = result.data["recapFileDownloadUrl"]
        assert payload["success"] is True, payload
        assert payload["fileUrl"] == "https://example.test/blob", payload

    @pytest.mark.asyncio
    async def test_spark_admin_can_download_other_tenant_custom_recap_file(self):
        recap = await sync_to_async(self._make_custom_recap)(
            self.other_event, self.other_tenant, self.other_template
        )
        crf = await sync_to_async(self._make_custom_recap_file)(recap)

        with patch(
            "recaps.mutations.public_url",
            return_value="https://example.test/blob",
        ):
            result = await self._execute_mutation_authenticated(
                RECAP_FILE_DOWNLOAD_URL_MUTATION,
                {"uuid": str(crf.uuid)},
                self.spark_admin,
                self.endpoint_path,
            )
        assert result.errors is None, f"errored: {result.errors}"
        payload = result.data["recapFileDownloadUrl"]
        assert payload["success"] is True, payload
        assert payload["fileUrl"] == "https://example.test/blob", payload

    # ════════════════════════════════════════════════════════════════
    # generateRecapPdf / generateCustomRecapPdf (single-recap PDF export)
    # ════════════════════════════════════════════════════════════════

    @pytest.mark.asyncio
    async def test_client_cannot_generate_other_tenant_recap_pdf(self):
        recap = await sync_to_async(self._make_recap)(self.other_event)

        result = await self._execute_mutation_authenticated(
            GENERATE_RECAP_PDF_MUTATION,
            {"id": str(recap.id)},
            self.client_user,
            self.endpoint_path,
        )
        assert result.errors is None, f"errored: {result.errors}"
        payload = result.data["generateRecapPdf"]
        assert payload["success"] is False, payload
        assert self._denied(payload["message"]), payload
        # Nothing persisted — no PDF RecapFile created for the foreign recap.
        count = await sync_to_async(
            recap_models.RecapFile.objects.filter(recap=recap).count
        )()
        assert count == 0, "a RecapFile was created for a denied PDF export"

    @pytest.mark.asyncio
    async def test_ambassador_cannot_generate_other_tenant_recap_pdf(self):
        recap = await sync_to_async(self._make_recap)(self.other_event)

        result = await self._execute_mutation_authenticated(
            GENERATE_RECAP_PDF_MUTATION,
            {"id": str(recap.id)},
            self.ba_user,
            self.endpoint_path,
        )
        assert result.errors is None, f"errored: {result.errors}"
        payload = result.data["generateRecapPdf"]
        assert payload["success"] is False, payload
        assert self._denied(payload["message"]), payload

    @pytest.mark.asyncio
    async def test_client_cannot_generate_other_tenant_custom_recap_pdf(self):
        recap = await sync_to_async(self._make_custom_recap)(
            self.other_event, self.other_tenant, self.other_template
        )

        result = await self._execute_mutation_authenticated(
            GENERATE_CUSTOM_RECAP_PDF_MUTATION,
            {"id": str(recap.id)},
            self.client_user,
            self.endpoint_path,
        )
        assert result.errors is None, f"errored: {result.errors}"
        payload = result.data["generateCustomRecapPdf"]
        assert payload["success"] is False, payload
        assert self._denied(payload["message"]), payload
        count = await sync_to_async(
            recap_models.CustomRecapFile.objects.filter(custom_recap=recap).count
        )()
        assert count == 0, "a CustomRecapFile was created for a denied export"

    @pytest.mark.asyncio
    async def test_same_tenant_client_can_generate_recap_pdf(self):
        recap = await sync_to_async(self._make_recap)(self.event)

        # Patch the GCS + render boundary so the ALLOW path completes without
        # external infra; the assertion is purely that we got PAST the gate.
        with patch("recaps.mutations.build_recap_pdf", return_value=b"%PDF-1.4"), \
                patch("recaps.mutations.upload_bytes", return_value=None), \
                patch("recaps.mutations.delete_blob", return_value=True):
            result = await self._execute_mutation_authenticated(
                GENERATE_RECAP_PDF_MUTATION,
                {"id": str(recap.id)},
                self.client_user,
                self.endpoint_path,
            )
        assert result.errors is None, f"errored: {result.errors}"
        payload = result.data["generateRecapPdf"]
        assert payload["success"] is True, payload
        assert not self._denied(payload["message"]), payload

    @pytest.mark.asyncio
    async def test_spark_admin_can_generate_other_tenant_custom_recap_pdf(self):
        recap = await sync_to_async(self._make_custom_recap)(
            self.other_event, self.other_tenant, self.other_template
        )

        with patch("recaps.mutations.build_recap_pdf", return_value=b"%PDF-1.4"), \
                patch("recaps.mutations.upload_bytes", return_value=None), \
                patch("recaps.mutations.delete_blob", return_value=True):
            result = await self._execute_mutation_authenticated(
                GENERATE_CUSTOM_RECAP_PDF_MUTATION,
                {"id": str(recap.id)},
                self.spark_admin,
                self.endpoint_path,
            )
        assert result.errors is None, f"errored: {result.errors}"
        payload = result.data["generateCustomRecapPdf"]
        assert payload["success"] is True, payload
        assert not self._denied(payload["message"]), payload

    # ════════════════════════════════════════════════════════════════
    # exportRecapXlsx / exportCustomRecapXlsx (single-recap XLSX export)
    # ════════════════════════════════════════════════════════════════

    @pytest.mark.asyncio
    async def test_client_cannot_export_other_tenant_recap_xlsx(self):
        recap = await sync_to_async(self._make_recap)(self.other_event)

        result = await self._execute_mutation_authenticated(
            EXPORT_RECAP_XLSX_MUTATION,
            {"id": str(recap.id)},
            self.client_user,
            self.endpoint_path,
        )
        assert result.errors is None, f"errored: {result.errors}"
        payload = result.data["exportRecapXlsx"]
        assert payload["success"] is False, payload
        assert self._denied(payload["message"]), payload
        assert payload["fileUrl"] is None, payload

    @pytest.mark.asyncio
    async def test_client_cannot_export_other_tenant_custom_recap_xlsx(self):
        recap = await sync_to_async(self._make_custom_recap)(
            self.other_event, self.other_tenant, self.other_template
        )

        result = await self._execute_mutation_authenticated(
            EXPORT_CUSTOM_RECAP_XLSX_MUTATION,
            {"id": str(recap.id)},
            self.client_user,
            self.endpoint_path,
        )
        assert result.errors is None, f"errored: {result.errors}"
        payload = result.data["exportCustomRecapXlsx"]
        assert payload["success"] is False, payload
        assert self._denied(payload["message"]), payload

    @pytest.mark.asyncio
    async def test_ambassador_cannot_export_other_tenant_custom_recap_xlsx(self):
        recap = await sync_to_async(self._make_custom_recap)(
            self.other_event, self.other_tenant, self.other_template
        )

        result = await self._execute_mutation_authenticated(
            EXPORT_CUSTOM_RECAP_XLSX_MUTATION,
            {"id": str(recap.id)},
            self.ba_user,
            self.endpoint_path,
        )
        assert result.errors is None, f"errored: {result.errors}"
        payload = result.data["exportCustomRecapXlsx"]
        assert payload["success"] is False, payload
        assert self._denied(payload["message"]), payload

    @pytest.mark.asyncio
    async def test_same_tenant_client_can_export_recap_xlsx(self):
        recap = await sync_to_async(self._make_recap)(self.event)

        with patch(
            "recaps.mutations.build_recaps_xlsx", return_value=b"PK\x03\x04"
        ), patch(
            "recaps.mutations.upload_bytes", return_value=None
        ), patch(
            "recaps.mutations.public_url",
            return_value="https://example.test/x.xlsx",
        ), patch(
            "recaps.mutations.get_gcs_client"
        ) as gcs:
            gcs.return_value.bucket.return_value.list_blobs.return_value = []
            result = await self._execute_mutation_authenticated(
                EXPORT_RECAP_XLSX_MUTATION,
                {"id": str(recap.id)},
                self.client_user,
                self.endpoint_path,
            )
        assert result.errors is None, f"errored: {result.errors}"
        payload = result.data["exportRecapXlsx"]
        assert payload["success"] is True, payload
        assert not self._denied(payload["message"]), payload

    @pytest.mark.asyncio
    async def test_spark_admin_can_export_other_tenant_custom_recap_xlsx(self):
        recap = await sync_to_async(self._make_custom_recap)(
            self.other_event, self.other_tenant, self.other_template
        )

        with patch(
            "recaps.mutations.build_recaps_xlsx", return_value=b"PK\x03\x04"
        ), patch(
            "recaps.mutations.upload_bytes", return_value=None
        ), patch(
            "recaps.mutations.public_url",
            return_value="https://example.test/x.xlsx",
        ), patch(
            "recaps.mutations.get_gcs_client"
        ) as gcs:
            gcs.return_value.bucket.return_value.list_blobs.return_value = []
            result = await self._execute_mutation_authenticated(
                EXPORT_CUSTOM_RECAP_XLSX_MUTATION,
                {"id": str(recap.id)},
                self.spark_admin,
                self.endpoint_path,
            )
        assert result.errors is None, f"errored: {result.errors}"
        payload = result.data["exportCustomRecapXlsx"]
        assert payload["success"] is True, payload
        assert not self._denied(payload["message"]), payload

    # ════════════════════════════════════════════════════════════════
    # recap / customRecap detail-by-uuid queries (deny => None)
    # ════════════════════════════════════════════════════════════════

    @pytest.mark.asyncio
    async def test_client_cannot_read_other_tenant_recap_query(self):
        recap = await sync_to_async(self._make_recap)(self.other_event)

        result = await self._execute_mutation_authenticated(
            RECAP_QUERY,
            {"uuid": str(recap.uuid)},
            self.client_user,
            self.endpoint_path,
        )
        assert result.errors is None, f"errored: {result.errors}"
        # Cross-tenant lookup is indistinguishable from "not found".
        assert result.data["recap"] is None, result.data

    @pytest.mark.asyncio
    async def test_ambassador_cannot_read_other_tenant_recap_query(self):
        recap = await sync_to_async(self._make_recap)(self.other_event)

        result = await self._execute_mutation_authenticated(
            RECAP_QUERY,
            {"uuid": str(recap.uuid)},
            self.ba_user,
            self.endpoint_path,
        )
        assert result.errors is None, f"errored: {result.errors}"
        assert result.data["recap"] is None, result.data

    @pytest.mark.asyncio
    async def test_ambassador_cannot_read_other_tenant_custom_recap_query(self):
        recap = await sync_to_async(self._make_custom_recap)(
            self.other_event, self.other_tenant, self.other_template
        )

        result = await self._execute_mutation_authenticated(
            CUSTOM_RECAP_QUERY,
            {"uuid": str(recap.uuid)},
            self.ba_user,
            self.endpoint_path,
        )
        assert result.errors is None, f"errored: {result.errors}"
        assert result.data["customRecap"] is None, result.data

    @pytest.mark.asyncio
    async def test_same_tenant_client_can_read_recap_query(self):
        recap = await sync_to_async(self._make_recap)(self.event)

        result = await self._execute_mutation_authenticated(
            RECAP_QUERY,
            {"uuid": str(recap.uuid)},
            self.client_user,
            self.endpoint_path,
        )
        assert result.errors is None, f"errored: {result.errors}"
        assert result.data["recap"] is not None, result.data
        assert result.data["recap"]["uuid"] == str(recap.uuid)

    @pytest.mark.asyncio
    async def test_same_tenant_client_can_read_custom_recap_query(self):
        recap = await sync_to_async(self._make_custom_recap)(
            self.event, self.tenant, self.template
        )

        result = await self._execute_mutation_authenticated(
            CUSTOM_RECAP_QUERY,
            {"uuid": str(recap.uuid)},
            self.client_user,
            self.endpoint_path,
        )
        assert result.errors is None, f"errored: {result.errors}"
        assert result.data["customRecap"] is not None, result.data
        assert result.data["customRecap"]["uuid"] == str(recap.uuid)

    @pytest.mark.asyncio
    async def test_spark_admin_can_read_other_tenant_recap_query(self):
        recap = await sync_to_async(self._make_recap)(self.other_event)

        result = await self._execute_mutation_authenticated(
            RECAP_QUERY,
            {"uuid": str(recap.uuid)},
            self.spark_admin,
            self.endpoint_path,
        )
        assert result.errors is None, f"errored: {result.errors}"
        assert result.data["recap"] is not None, result.data
        assert result.data["recap"]["uuid"] == str(recap.uuid)

    @pytest.mark.asyncio
    async def test_spark_admin_can_read_other_tenant_custom_recap_query(self):
        recap = await sync_to_async(self._make_custom_recap)(
            self.other_event, self.other_tenant, self.other_template
        )

        result = await self._execute_mutation_authenticated(
            CUSTOM_RECAP_QUERY,
            {"uuid": str(recap.uuid)},
            self.spark_admin,
            self.endpoint_path,
        )
        assert result.errors is None, f"errored: {result.errors}"
        assert result.data["customRecap"] is not None, result.data
        assert result.data["customRecap"]["uuid"] == str(recap.uuid)
