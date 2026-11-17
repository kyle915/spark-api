"""
Export scope for recaps: the combined campaign-report PDF (generate + email)
and every export in recaps/mutation_parts/exports.py (tenant-wide XLSX,
single-recap XLSX, recap file download URL).

Rule (``RecapExportMixin._export_scope``, DB-backed):
  * Ignite admins: any recap, any tenant, drafts included.
  * Clients: only APPROVED, un-archived recaps in a tenant they're an active
    member of (what their recap lists show).
  * Brand ambassadors (and a removed admin): refused.
Out-of-scope ids answer exactly like missing ids ("... not found."), so
neither another brand's recap nor a draft is confirmed to exist.

Before this change: the campaign report and the single-recap exports only
checked tenant membership (so clients got drafts and a BA got their
tenant's recaps), and the tenant-wide exports used the sync
``is_spark_schema_request`` (removed admin with a stale role passed; a
staff/domain admin was refused outside their tenants) and never filtered
drafts for clients. Run through the real clients schema.
"""

import base64
import sys
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone as _tz
from types import ModuleType
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from asgiref.sync import sync_to_async
from django.utils import timezone

from ambassadors.tests.base import AmbassadorsGraphQLTestCase
from recaps import models as recap_models

GENERATE = """
mutation G($recapIds: [ID!]!) {
  generateCampaignReportPdf(input: { recapIds: $recapIds, title: "Report" }) {
    success message fileUrl
  }
}
"""

EMAIL = """
mutation E($recapIds: [ID!]!) {
  emailCampaignReport(
    input: { recapIds: $recapIds, recipients: ["someone@example.com"] }
  ) { success message }
}
"""

EXPORT_RECAP = """
mutation X($input: ExportRecapXlsxInput!) {
  exportRecapXlsx(input: $input) { success message fileUrl }
}
"""

EXPORT_CUSTOM_RECAP = """
mutation X($input: ExportCustomRecapXlsxInput!) {
  exportCustomRecapXlsx(input: $input) { success message fileUrl }
}
"""

EXPORT_RECAPS = """
mutation X($input: ExportRecapsXlsxInput!) {
  exportRecapsXlsx(input: $input) { success message fileUrl }
}
"""

EXPORT_CUSTOM_RECAPS = """
mutation X($input: ExportCustomRecapsXlsxInput!) {
  exportCustomRecapsXlsx(input: $input) { success message fileUrl }
}
"""

FILE_URL = """
mutation F($input: RecapFileDownloadUrlInput!) {
  recapFileDownloadUrl(input: $input) { success message fileUrl }
}
"""

NOT_FOUND = "None of the supplied recap ids were found."


def _gid(type_name, pk):
    return base64.b64encode(f"{type_name}:{pk}".encode()).decode()


@contextmanager
def _excluded(email):
    from utils.graphql.permissions import IGNITE_ADMIN_EXCLUDE

    IGNITE_ADMIN_EXCLUDE.add(email)
    try:
        yield
    finally:
        IGNITE_ADMIN_EXCLUDE.discard(email)


@contextmanager
def _stub_pdf():
    rendered = []

    class _HTML:
        def __init__(self, string=None, **_kw):
            rendered.append(string or "")

        def write_pdf(self, *_a, **_kw):
            return b"%PDF-stub"

    module = ModuleType("weasyprint")
    module.HTML = _HTML
    module.CSS = MagicMock()
    with (
        patch.dict(sys.modules, {"weasyprint": module}),
        patch("recaps.mutations.upload_bytes"),
        patch(
            "recaps.mutations.public_url",
            side_effect=lambda name: f"https://cdn.test/{name}",
        ),
    ):
        yield rendered


@contextmanager
def _stub_xlsx():
    """Capture the recaps handed to the XLSX builder; stub GCS."""
    exported = []

    def _build(recaps, **_kw):
        exported.append(sorted(r.name for r in recaps))
        return b"xlsx"

    with (
        patch("recaps.mutation_parts.exports.build_recaps_xlsx", side_effect=_build),
        patch("recaps.mutation_parts.exports.upload_bytes"),
        patch("recaps.mutation_parts.exports.get_gcs_client"),
        patch(
            "recaps.mutation_parts.exports.public_url",
            side_effect=lambda name: f"https://cdn.test/{name}",
        ),
    ):
        yield exported


@pytest.mark.django_db(transaction=True)
class TestExportScopeGates(AmbassadorsGraphQLTestCase):
    @pytest.fixture(autouse=True)
    def setup(self, db):
        from config.schema_client import schema_clients

        self.schema = schema_clients
        self.endpoint_path = "/api/v1/graphql/clients"
        self.roles = self.setup_default_roles()
        self.sys = self.get_system_user()
        self.tenant = self.create_tenant(name="Export Brand A")
        self.other_tenant = self.create_tenant(name="Export Brand B")

        self.admin = self.create_user(
            username="ex-admin", email="ex-admin@test.com",
            role=self.roles["spark_admin"],
        )
        self.staff = self.create_user(
            username="ex-staff", email="ex-staff@test.com",
            role=self.roles["client"], is_staff=True,
        )
        self.removed_email = "ex-removed@igniteproductions.co"
        self.removed = self.create_user(
            username="ex-removed", email=self.removed_email,
            role=self.roles["spark_admin"],
        )
        self.client_user = self.create_user(
            username="ex-client", email="ex-client@test.com",
            role=self.roles["client"],
        )
        self.create_tenanted_user(self.client_user, self.tenant)
        self.ba_user = self.create_user(
            username="ex-ba", email="ex-ba@test.com",
            role=self.roles["ambassador"],
        )
        self.create_tenanted_user(self.ba_user, self.tenant)
        self.credited = self.create_ambassador(self.ba_user)

        now = datetime.now(_tz.utc)
        self.event = self.create_event(
            name="A store", tenant=self.tenant,
            date=now, start_time=now, end_time=now + timedelta(hours=4),
        )
        self.other_event = self.create_event(
            name="B store", tenant=self.other_tenant,
            date=now, start_time=now, end_time=now + timedelta(hours=4),
        )
        self.template = recap_models.CustomRecapTemplate.objects.create(
            name="A template",
            event_type=self.create_event_type(name="Sampling", tenant=self.tenant),
            tenant=self.tenant, created_by=self.sys,
        )

        self.approved = self._recap("APPROVED-RECAP", approved=True)
        self.draft = self._recap("DRAFT-RECAP", approved=False)
        self.archived = self._recap(
            "ARCHIVED-RECAP", approved=True, archived_at=timezone.now()
        )
        self.foreign = self._recap(
            "FOREIGN-RECAP", approved=True, event=self.other_event
        )
        self.custom_approved = self._custom("APPROVED-CUSTOM", approved=True)
        self.custom_draft = self._custom("DRAFT-CUSTOM", approved=False)

        file_type = self.create_file_type("Image", "jpg")
        self.approved_file = recap_models.RecapFile.objects.create(
            name="ok.jpg", file="recap_files/ok.jpg", recap=self.approved,
            file_type=file_type, created_by=self.sys,
        )
        self.draft_file = recap_models.RecapFile.objects.create(
            name="draft.jpg", file="recap_files/draft.jpg", recap=self.draft,
            file_type=file_type, created_by=self.sys,
        )

    def _recap(self, name, *, approved, event=None, **extra):
        return recap_models.Recap.objects.create(
            name=name, approved=approved, event=event or self.event,
            ambassador=self.credited, created_by=self.sys, updated_by=self.sys,
            **extra,
        )

    def _custom(self, name, *, approved):
        return recap_models.CustomRecap.objects.create(
            name=name, approved=approved, event=self.event, tenant=self.tenant,
            custom_recap_template=self.template, ambassador=self.credited,
            created_by=self.sys, updated_by=self.sys,
        )

    async def _run(self, query, variables, user):
        result = await self._execute_mutation(
            query, variables, self.endpoint_path, user=user
        )
        assert result.errors is None, result.errors
        return next(iter(result.data.values()))

    async def _generate(self, ids, user):
        with _stub_pdf() as rendered:
            payload = await self._run(GENERATE, {"recapIds": ids}, user)
        return payload, rendered

    # ── campaign report (combined PDF) ───────────────────────────────

    @pytest.mark.asyncio
    async def test_campaign_client_draft_reads_as_not_found(self):
        for recap in (self.draft, self.archived):
            payload, rendered = await self._generate(
                [_gid("RecapType", recap.id)], self.client_user
            )
            assert payload == {
                "success": False, "message": NOT_FOUND, "fileUrl": None,
            }, payload
            assert rendered == []

    @pytest.mark.asyncio
    async def test_campaign_client_custom_draft_reads_as_not_found(self):
        payload, rendered = await self._generate(
            [_gid("CustomRecapType", self.custom_draft.id)], self.client_user
        )
        assert payload["message"] == NOT_FOUND, payload
        assert rendered == []

    @pytest.mark.asyncio
    async def test_campaign_client_mixed_selection_keeps_only_approved(self):
        payload, rendered = await self._generate(
            [
                _gid("RecapType", self.approved.id),
                _gid("RecapType", self.draft.id),
                _gid("RecapType", self.archived.id),
                _gid("CustomRecapType", self.custom_approved.id),
                _gid("CustomRecapType", self.custom_draft.id),
            ],
            self.client_user,
        )
        assert payload["success"] is True, payload
        html = rendered[0]
        assert "APPROVED-RECAP" in html and "APPROVED-CUSTOM" in html
        assert "DRAFT" not in html and "ARCHIVED" not in html
        assert "-2-recaps-" in payload["fileUrl"]

    @pytest.mark.asyncio
    async def test_campaign_ambassador_refused_even_in_own_tenant(self):
        payload, rendered = await self._generate(
            [_gid("RecapType", self.approved.id)], self.ba_user
        )
        assert payload["success"] is False, payload
        assert payload["message"] == NOT_FOUND
        assert rendered == []

    @pytest.mark.asyncio
    async def test_campaign_admin_bundles_drafts_and_other_tenants(self):
        payload, rendered = await self._generate(
            [
                _gid("RecapType", self.draft.id),
                _gid("RecapType", self.foreign.id),
            ],
            self.admin,
        )
        assert payload["success"] is True, payload
        assert "DRAFT-RECAP" in rendered[0] and "FOREIGN-RECAP" in rendered[0]

    @pytest.mark.asyncio
    async def test_campaign_email_ambassador_refused_and_nothing_sent(self):
        mailer = MagicMock()
        mailer.return_value.send_async_now = AsyncMock()
        with _stub_pdf() as rendered, patch(
            "recaps.envelopes.CampaignReportMailer", mailer
        ):
            payload = await self._run(
                EMAIL, {"recapIds": [_gid("RecapType", self.approved.id)]},
                self.ba_user,
            )
        assert payload["success"] is False, payload
        assert payload["message"] == NOT_FOUND
        assert rendered == []
        mailer.assert_not_called()

    @pytest.mark.asyncio
    async def test_campaign_email_client_draft_refused(self):
        mailer = MagicMock()
        mailer.return_value.send_async_now = AsyncMock()
        with _stub_pdf(), patch("recaps.envelopes.CampaignReportMailer", mailer):
            payload = await self._run(
                EMAIL, {"recapIds": [_gid("RecapType", self.draft.id)]},
                self.client_user,
            )
        assert payload["message"] == NOT_FOUND, payload
        mailer.assert_not_called()

    # ── exportRecapXlsx / exportCustomRecapXlsx (single id) ──────────

    async def _export_one(self, query, pk, user):
        with _stub_xlsx() as exported:
            payload = await self._run(query, {"input": {"id": str(pk)}}, user)
        return payload, exported

    @pytest.mark.asyncio
    async def test_export_recap_client_approved_ok(self):
        payload, exported = await self._export_one(
            EXPORT_RECAP, self.approved.id, self.client_user
        )
        assert payload["success"] is True, payload
        assert exported == [["APPROVED-RECAP"]]

    @pytest.mark.asyncio
    async def test_export_recap_client_draft_or_foreign_not_found(self):
        for recap in (self.draft, self.archived, self.foreign):
            payload, exported = await self._export_one(
                EXPORT_RECAP, recap.id, self.client_user
            )
            assert payload == {
                "success": False, "message": "Recap not found.", "fileUrl": None,
            }, (recap.name, payload)
            assert exported == []

    @pytest.mark.asyncio
    async def test_export_recap_ambassador_not_found(self):
        payload, exported = await self._export_one(
            EXPORT_RECAP, self.approved.id, self.ba_user
        )
        assert payload["message"] == "Recap not found.", payload
        assert exported == []

    @pytest.mark.asyncio
    async def test_export_recap_admin_gets_draft(self):
        payload, exported = await self._export_one(
            EXPORT_RECAP, self.draft.id, self.admin
        )
        assert payload["success"] is True, payload
        assert exported == [["DRAFT-RECAP"]]

    @pytest.mark.asyncio
    async def test_export_custom_recap_scoped(self):
        payload, _ = await self._export_one(
            EXPORT_CUSTOM_RECAP, self.custom_approved.id, self.client_user
        )
        assert payload["success"] is True, payload
        for pk, user in (
            (self.custom_draft.id, self.client_user),
            (self.custom_approved.id, self.ba_user),
        ):
            payload, exported = await self._export_one(
                EXPORT_CUSTOM_RECAP, pk, user
            )
            assert payload["message"] == "Custom recap not found.", payload
            assert exported == []
        payload, _ = await self._export_one(
            EXPORT_CUSTOM_RECAP, self.custom_draft.id, self.admin
        )
        assert payload["success"] is True, payload

    # ── exportRecapsXlsx / exportCustomRecapsXlsx (tenant-wide) ──────

    async def _export_all(self, query, user, tenant):
        with _stub_xlsx() as exported:
            payload = await self._run(
                query, {"input": {"tenantId": str(tenant.id)}}, user
            )
        return payload, exported

    @pytest.mark.asyncio
    async def test_export_recaps_client_gets_only_approved(self):
        payload, exported = await self._export_all(
            EXPORT_RECAPS, self.client_user, self.tenant
        )
        assert payload["success"] is True, payload
        assert exported == [["APPROVED-RECAP"]]

    @pytest.mark.asyncio
    async def test_export_custom_recaps_client_gets_only_approved(self):
        payload, exported = await self._export_all(
            EXPORT_CUSTOM_RECAPS, self.client_user, self.tenant
        )
        assert payload["success"] is True, payload
        assert exported == [["APPROVED-CUSTOM"]]

    @pytest.mark.asyncio
    async def test_export_recaps_ambassador_refused(self):
        for query in (EXPORT_RECAPS, EXPORT_CUSTOM_RECAPS):
            payload, exported = await self._export_all(
                query, self.ba_user, self.tenant
            )
            assert payload["success"] is False, payload
            assert exported == []

    @pytest.mark.asyncio
    async def test_export_recaps_client_other_tenant_refused(self):
        payload, exported = await self._export_all(
            EXPORT_RECAPS, self.client_user, self.other_tenant
        )
        assert payload["success"] is False, payload
        assert exported == []

    @pytest.mark.asyncio
    async def test_export_recaps_admin_gets_everything(self):
        payload, exported = await self._export_all(
            EXPORT_RECAPS, self.admin, self.tenant
        )
        assert payload["success"] is True, payload
        assert exported == [["APPROVED-RECAP", "ARCHIVED-RECAP", "DRAFT-RECAP"]]

    @pytest.mark.asyncio
    async def test_export_recaps_staff_admin_any_tenant(self):
        payload, exported = await self._export_all(
            EXPORT_RECAPS, self.staff, self.other_tenant
        )
        assert payload["success"] is True, payload
        assert exported == [["FOREIGN-RECAP"]]

    @pytest.mark.asyncio
    async def test_export_recaps_removed_admin_refused(self):
        with _excluded(self.removed_email):
            payload, exported = await self._export_all(
                EXPORT_RECAPS, self.removed, self.other_tenant
            )
        assert payload["success"] is False, payload
        assert exported == []

    # ── recapFileDownloadUrl ─────────────────────────────────────────

    async def _file_url(self, recap_file, user):
        with _stub_xlsx():
            return await self._run(
                FILE_URL, {"input": {"uuid": str(recap_file.uuid)}}, user
            )

    @pytest.mark.asyncio
    async def test_file_url_client_approved_ok(self):
        payload = await self._file_url(self.approved_file, self.client_user)
        assert payload["success"] is True, payload
        assert payload["fileUrl"]

    @pytest.mark.asyncio
    async def test_file_url_client_draft_and_ambassador_not_found(self):
        for recap_file, user in (
            (self.draft_file, self.client_user),
            (self.approved_file, self.ba_user),
        ):
            payload = await self._file_url(recap_file, user)
            assert payload == {
                "success": False, "message": "Recap not found.", "fileUrl": None,
            }, payload

    @pytest.mark.asyncio
    async def test_file_url_admin_gets_draft_file(self):
        payload = await self._file_url(self.draft_file, self.admin)
        assert payload["success"] is True, payload
