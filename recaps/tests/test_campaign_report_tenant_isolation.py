"""
Cross-tenant READ isolation for the combined campaign-report PDF.

`generateCampaignReportPdf` and `emailCampaignReport` share one core
(`RecapMutationService.build_campaign_report_pdf_with_meta`) that loaded
`Recap` / `CustomRecap` rows by raw primary key behind only
`StrictIsAuthenticated`. Any authenticated user — a client of another brand,
or a brand ambassador — could pass guessed recap ids and receive another
tenant's photos, metrics and ambassador names as one PDF.

These run end to end against the real `schema_clients` GraphQL surface (only
WeasyPrint, GCS and the mailer are stubbed) and assert:

  * an Ignite admin can bundle recaps across tenants, with full BA names;
  * a client of tenant A can bundle tenant A's recaps (legacy + custom);
  * a client of tenant A asking for tenant B's recaps gets the SAME answer
    as for ids that don't exist — existence is never confirmed (#247);
  * a mixed selection silently drops tenant B's recaps;
  * a non-admin's PDF shows the ambassador as first name + last initial and
    carries no ambassador email;
  * the email path is gated identically.
"""

import base64
import sys
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone as _tz
from types import ModuleType
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from asgiref.sync import sync_to_async

from ambassadors.tests.base import AmbassadorsGraphQLTestCase
from recaps import models as recap_models


GENERATE_MUTATION = """
mutation GenerateCampaignReportPdf($recapIds: [ID!]!) {
  generateCampaignReportPdf(input: { recapIds: $recapIds, title: "Report" }) {
    success
    message
    fileUrl
  }
}
"""

EMAIL_MUTATION = """
mutation EmailCampaignReport($recapIds: [ID!]!) {
  emailCampaignReport(
    input: { recapIds: $recapIds, recipients: ["someone@example.com"] }
  ) {
    success
    message
  }
}
"""

NOT_FOUND = "None of the supplied recap ids were found."


def _gid(type_name: str, pk: int) -> str:
    return base64.b64encode(f"{type_name}:{pk}".encode()).decode()


class _Rendered(list):
    """HTML documents rendered during the call, plus the GCS upload mock."""

    upload: MagicMock


@contextmanager
def _capture_rendered_html():
    """Stub WeasyPrint + GCS; yield the list of HTML documents rendered.

    `build_campaign_report_pdf` imports weasyprint at call time, so a stub
    module in `sys.modules` intercepts it without loading Pango/cairo. The
    real HTML is still built, so assertions read what a viewer would see.
    """
    rendered = _Rendered()

    class _HTML:
        def __init__(self, string=None, **_kwargs):
            rendered.append(string or "")

        def write_pdf(self, *_args, **_kwargs):
            return b"%PDF-stub"

    module = ModuleType("weasyprint")
    module.HTML = _HTML
    module.CSS = MagicMock()
    with (
        patch.dict(sys.modules, {"weasyprint": module}),
        patch("recaps.mutations.upload_bytes") as upload,
        patch(
            "recaps.mutations.public_url",
            side_effect=lambda name: f"https://cdn.test/{name}",
        ),
    ):
        rendered.upload = upload
        yield rendered


@pytest.mark.django_db(transaction=True)
class TestCampaignReportTenantIsolation(AmbassadorsGraphQLTestCase):
    @pytest.fixture(autouse=True)
    def setup(self, db):
        from config.schema_client import schema_clients

        self.roles = self.setup_default_roles()
        self.schema = schema_clients
        self.endpoint_path = "/api/v1/graphql/clients"
        self.system_user = self.get_system_user()

        self.tenant = self.create_tenant(name="Girl Beer")
        self.other_tenant = self.create_tenant(name="Liquid Death")

        self.spark_admin = self.create_user(
            username="admin-campaign-iso",
            email="admin-campaign-iso@test.com",
            role=self.roles["spark_admin"],
        )
        # Client belongs to tenant A only.
        self.client_user = self.create_user(
            username="client-campaign-iso",
            email="client-campaign-iso@test.com",
            role=self.roles["client"],
        )
        self.create_tenanted_user(self.client_user, self.tenant)
        # A brand ambassador with no membership in tenant B.
        self.ba_user = self.create_user(
            username="ba-campaign-iso",
            email="ba-campaign-iso@test.com",
            role=self.roles["ambassador"],
        )
        self.create_tenanted_user(self.ba_user, self.tenant)

        # The ambassador credited on the recaps.
        self.credited_user = self.create_user(
            username="jordan-rivera",
            email="jordan.rivera@ba.test",
            role=self.roles["ambassador"],
        )
        self.credited_user.first_name = "Jordan"
        self.credited_user.last_name = "Rivera"
        self.credited_user.save()
        self.credited_ba = self.create_ambassador(self.credited_user)

        now = datetime.now(_tz.utc)
        self.event = self.create_event(
            name="Whole Foods Burbank",
            tenant=self.tenant,
            date=now,
            start_time=now,
            end_time=now + timedelta(hours=4),
        )
        self.other_event = self.create_event(
            name="Foreign event",
            tenant=self.other_tenant,
            date=now,
            start_time=now,
            end_time=now + timedelta(hours=4),
        )
        self.template = recap_models.CustomRecapTemplate.objects.create(
            name="GB Template",
            event_type=self.create_event_type(name="Sampling", tenant=self.tenant),
            tenant=self.tenant,
            created_by=self.system_user,
        )
        self.other_template = recap_models.CustomRecapTemplate.objects.create(
            name="LD Template",
            event_type=self.create_event_type(
                name="Sampling B", tenant=self.other_tenant
            ),
            tenant=self.other_tenant,
            created_by=self.system_user,
        )

        self.recap_a = self._make_recap("OWN-LEGACY-RECAP", self.event)
        self.custom_a = self._make_custom(
            "OWN-CUSTOM-RECAP", self.event, self.tenant, self.template
        )
        self.recap_b = self._make_recap("FOREIGN-LEGACY-RECAP", self.other_event)
        self.custom_b = self._make_custom(
            "FOREIGN-CUSTOM-RECAP",
            self.other_event,
            self.other_tenant,
            self.other_template,
        )

    def _make_recap(self, name, event):
        return recap_models.Recap.objects.create(
            name=name,
            approved=True,
            event=event,
            ambassador=self.credited_ba,
            created_by=self.system_user,
            updated_by=self.system_user,
        )

    def _make_custom(self, name, event, tenant, template):
        return recap_models.CustomRecap.objects.create(
            name=name,
            approved=True,
            event=event,
            tenant=tenant,
            custom_recap_template=template,
            ambassador=self.credited_ba,
            created_by=self.system_user,
            updated_by=self.system_user,
        )

    async def _generate(self, ids, user):
        with _capture_rendered_html() as rendered:
            result = await self._execute_mutation_authenticated(
                GENERATE_MUTATION, {"recapIds": ids}, user, self.endpoint_path
            )
        assert result.errors is None, f"errored: {result.errors}"
        return result.data["generateCampaignReportPdf"], rendered

    # ── admin ─────────────────────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_admin_can_bundle_across_tenants_with_full_names(self):
        payload, rendered = await self._generate(
            [
                _gid("RecapType", self.recap_a.id),
                _gid("CustomRecapType", self.custom_b.id),
                _gid("RecapType", self.recap_b.id),
            ],
            self.spark_admin,
        )
        assert payload["success"] is True, payload
        assert payload["fileUrl"], payload
        html = rendered[0]
        assert "OWN-LEGACY-RECAP" in html
        assert "FOREIGN-CUSTOM-RECAP" in html
        assert "FOREIGN-LEGACY-RECAP" in html
        # Ignite staff keep the full ambassador identity.
        assert "Jordan Rivera" in html
        assert "jordan.rivera@ba.test" in html

    # ── same-tenant client ────────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_client_can_bundle_own_tenant_recaps(self):
        payload, rendered = await self._generate(
            [
                _gid("RecapType", self.recap_a.id),
                _gid("CustomRecapType", self.custom_a.id),
            ],
            self.client_user,
        )
        assert payload["success"] is True, payload
        html = rendered[0]
        assert "OWN-LEGACY-RECAP" in html
        assert "OWN-CUSTOM-RECAP" in html

    @pytest.mark.asyncio
    async def test_client_pdf_masks_ambassador_name_and_email(self):
        payload, rendered = await self._generate(
            [
                _gid("RecapType", self.recap_a.id),
                _gid("CustomRecapType", self.custom_a.id),
            ],
            self.client_user,
        )
        assert payload["success"] is True, payload
        html = rendered[0]
        assert "Jordan R." in html
        assert "Rivera" not in html
        assert "jordan.rivera@ba.test" not in html
        assert "Ambassador Email" not in html

    @pytest.mark.asyncio
    async def test_file_url_is_not_guessable(self):
        ids = [_gid("RecapType", self.recap_a.id)]
        first, _ = await self._generate(ids, self.client_user)
        second, _ = await self._generate(ids, self.client_user)
        assert first["fileUrl"] != second["fileUrl"]

    # ── cross-tenant ──────────────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_client_gets_not_found_for_other_tenant_recaps(self):
        for ids in (
            [_gid("RecapType", self.recap_b.id)],
            [_gid("CustomRecapType", self.custom_b.id)],
            # Bare ints route to the legacy table — same gate.
            [str(self.recap_b.id)],
        ):
            payload, rendered = await self._generate(ids, self.client_user)
            assert payload["success"] is False, payload
            assert payload["fileUrl"] is None, payload
            assert payload["message"] == NOT_FOUND, payload
            assert "authorized" not in payload["message"].lower(), payload
            assert rendered == [], "nothing may be rendered for a foreign recap"
            rendered.upload.assert_not_called()

    @pytest.mark.asyncio
    async def test_foreign_id_is_indistinguishable_from_missing_id(self):
        foreign, _ = await self._generate(
            [_gid("RecapType", self.recap_b.id)], self.client_user
        )
        missing, _ = await self._generate(
            [_gid("RecapType", 987654321)], self.client_user
        )
        assert foreign == missing

    @pytest.mark.asyncio
    async def test_ambassador_gets_not_found_for_other_tenant_recaps(self):
        payload, rendered = await self._generate(
            [_gid("CustomRecapType", self.custom_b.id)], self.ba_user
        )
        assert payload["success"] is False, payload
        assert payload["message"] == NOT_FOUND, payload
        assert rendered == []

    @pytest.mark.asyncio
    async def test_inactive_membership_does_not_grant_access(self):
        await sync_to_async(
            lambda: self.client_user.tenanted_users.update(is_active=False)
        )()
        payload, rendered = await self._generate(
            [_gid("RecapType", self.recap_a.id)], self.client_user
        )
        assert payload["success"] is False, payload
        assert payload["message"] == NOT_FOUND, payload
        assert rendered == []

    @pytest.mark.asyncio
    async def test_mixed_selection_drops_other_tenant_recaps(self):
        payload, rendered = await self._generate(
            [
                _gid("RecapType", self.recap_b.id),
                _gid("RecapType", self.recap_a.id),
                _gid("CustomRecapType", self.custom_b.id),
                _gid("CustomRecapType", self.custom_a.id),
            ],
            self.client_user,
        )
        assert payload["success"] is True, payload
        html = rendered[0]
        assert "OWN-LEGACY-RECAP" in html
        assert "OWN-CUSTOM-RECAP" in html
        assert "FOREIGN" not in html
        assert "Foreign event" not in html
        assert "Liquid Death" not in html
        # Only the caller's two recaps were counted.
        assert "2 recaps" in html
        assert "-2-recaps-" in payload["fileUrl"]

    # ── email path shares the core ────────────────────────────────────

    async def _email(self, ids, user):
        mailer_cls = MagicMock()
        mailer_cls.return_value.send_async_now = AsyncMock()
        with (
            _capture_rendered_html() as rendered,
            patch("recaps.envelopes.CampaignReportMailer", mailer_cls),
        ):
            result = await self._execute_mutation_authenticated(
                EMAIL_MUTATION, {"recapIds": ids}, user, self.endpoint_path
            )
        assert result.errors is None, f"errored: {result.errors}"
        return result.data["emailCampaignReport"], rendered, mailer_cls

    @pytest.mark.asyncio
    async def test_email_path_denies_other_tenant_recaps(self):
        payload, rendered, mailer_cls = await self._email(
            [_gid("CustomRecapType", self.custom_b.id)], self.client_user
        )
        assert payload["success"] is False, payload
        assert payload["message"] == NOT_FOUND, payload
        assert rendered == []
        mailer_cls.assert_not_called()

    @pytest.mark.asyncio
    async def test_email_path_drops_foreign_recaps_from_mixed_selection(self):
        payload, rendered, mailer_cls = await self._email(
            [
                _gid("RecapType", self.recap_b.id),
                _gid("RecapType", self.recap_a.id),
            ],
            self.client_user,
        )
        assert payload["success"] is True, payload
        assert "FOREIGN" not in rendered[0]
        kwargs = mailer_cls.call_args.kwargs
        assert kwargs["recap_count"] == 1
        # Sender / metadata come from the caller's tenant, not tenant B.
        assert kwargs["sender_tenant_name"] == "Girl Beer"
        assert kwargs["event_meta"]["event_label"] == "Whole Foods Burbank"

    @pytest.mark.asyncio
    async def test_email_path_admin_can_bundle_across_tenants(self):
        payload, rendered, mailer_cls = await self._email(
            [
                _gid("RecapType", self.recap_a.id),
                _gid("RecapType", self.recap_b.id),
            ],
            self.spark_admin,
        )
        assert payload["success"] is True, payload
        assert mailer_cls.call_args.kwargs["recap_count"] == 2
