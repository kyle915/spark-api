"""GraphQL tests for the scheduled monthly client-report CONTROL mutations.

Covers the two clients-schema mutations the #698 "Scheduled Reports" admin
panel needs (defined in :mod:`recaps.report_types`):

* ``setScheduledReportEnabled`` — flips a tenant's ``scheduled_report_enabled``
  opt-in switch and persists it. Tenant-scoped: a client may only toggle their
  OWN tenant (passing another tenant's id is a no-op); an admin may toggle any.
* ``sendTestClientReport`` — a SAFE PREVIEW that generates a tenant's monthly
  report PDF and emails it to ONLY the requesting user's own address — NEVER the
  tenant's configured client recipients. The single most important guarantee
  here: the mailer recipient is the REQUESTER, not
  ``tenant.scheduled_report_recipients()``.

The PDF builder + mailer are ALWAYS mocked — these tests never render a real
PDF (WeasyPrint's native deps aren't in CI) and never send real email. Scoping
matches the read side + ``TenantGoalsMutations``
(:meth:`recaps.report_types._CampaignReportService.resolve_target_tenant_id`).
"""

from __future__ import annotations

from unittest import mock

import pytest
from asgiref.sync import sync_to_async
from django.contrib.auth import get_user_model

from config.schema_client import schema_clients
from tenants.models import Tenant
from tenants.tests.base import BaseGraphQLTestCase

User = get_user_model()

# build_client_monthly_report_pdf + ClientMonthlyReportMailer are imported
# LAZILY inside the resolver (from their definition modules), so patch them
# there — that's the binding the resolver actually picks up.
PDF_BUILDER_PATH = "recaps.client_report.build_client_monthly_report_pdf"
MAILER_PATH = "recaps.envelopes.ClientMonthlyReportMailer"

SET_ENABLED_MUTATION = """
mutation SetScheduledReportEnabled($input: SetScheduledReportEnabledInput!) {
  setScheduledReportEnabled(input: $input) {
    success
    message
    enabled
    clientMutationId
  }
}
"""

SEND_TEST_MUTATION = """
mutation SendTestClientReport($input: SendTestClientReportInput!) {
  sendTestClientReport(input: $input) {
    success
    message
    clientMutationId
  }
}
"""


@pytest.mark.django_db(transaction=True)
class TestScheduledReportMutations(BaseGraphQLTestCase):
    """setScheduledReportEnabled + sendTestClientReport (clients schema)."""

    @pytest.fixture(autouse=True)
    def setup(self, db):
        self.roles = self.setup_default_roles()
        self.schema = schema_clients
        self.endpoint_path = "/api/v1/graphql/clients"
        # The tenant under test. Recipients are configured so we can prove the
        # preview does NOT email them.
        self.tenant = self.create_tenant(
            name="Girl Beer",
            scheduled_report_enabled=False,
            recap_recipient_emails="client@girlbeer.com, ops@girlbeer.com",
        )
        # A second brand the client must never be able to touch.
        self.other_tenant = self.create_tenant(
            name="Liquid Death",
            scheduled_report_enabled=False,
            recap_recipient_emails="brand@liquiddeath.com",
        )

    # -- user helpers ---------------------------------------------------

    async def _client_user_for(self, username, email, tenant) -> User:
        user = await sync_to_async(self.create_user)(
            username=username,
            email=email,
            role=self.roles["client"],
            password="password123",
            first_name="Cas",
            last_name="Client",
        )
        await sync_to_async(self.create_tenanted_user)(user=user, tenant=tenant)
        return user

    async def _admin_user(self, username, email) -> User:
        return await sync_to_async(self.create_user)(
            username=username,
            email=email,
            role=self.roles["spark_admin"],
            password="password123",
        )

    async def _reload_tenant(self, tenant_id: int) -> Tenant:
        return await sync_to_async(Tenant.objects.get)(id=tenant_id)

    # =================================================================
    # setScheduledReportEnabled — toggle flips + persists
    # =================================================================

    @pytest.mark.asyncio
    async def test_admin_can_enable_and_it_persists(self):
        """An admin flips the flag ON; the new state is returned AND persisted."""
        admin = await self._admin_user("sr-admin-on", "admin-on@test.com")

        result = await self._execute_mutation(
            SET_ENABLED_MUTATION,
            {
                "input": {
                    "tenantId": str(self.tenant.id),
                    "enabled": True,
                    "clientMutationId": "sr-1",
                }
            },
            self.endpoint_path,
            user=admin,
        )

        assert result.errors is None
        payload = result.data["setScheduledReportEnabled"]
        assert payload["success"] is True
        assert payload["enabled"] is True
        assert payload["clientMutationId"] == "sr-1"

        # Persisted on the row.
        tenant = await self._reload_tenant(self.tenant.id)
        assert tenant.scheduled_report_enabled is True

    @pytest.mark.asyncio
    async def test_admin_can_disable_and_it_persists(self):
        """Flipping OFF a currently-ON tenant persists the False state."""
        await sync_to_async(
            Tenant.objects.filter(id=self.tenant.id).update
        )(scheduled_report_enabled=True)
        admin = await self._admin_user("sr-admin-off", "admin-off@test.com")

        result = await self._execute_mutation(
            SET_ENABLED_MUTATION,
            {"input": {"tenantId": str(self.tenant.id), "enabled": False}},
            self.endpoint_path,
            user=admin,
        )

        assert result.errors is None
        payload = result.data["setScheduledReportEnabled"]
        assert payload["success"] is True
        assert payload["enabled"] is False

        tenant = await self._reload_tenant(self.tenant.id)
        assert tenant.scheduled_report_enabled is False

    @pytest.mark.asyncio
    async def test_client_can_toggle_own_tenant(self):
        """A client may toggle THEIR OWN tenant's flag."""
        user = await self._client_user_for("sr-cli-own", "own@test.com", self.tenant)

        result = await self._execute_mutation(
            SET_ENABLED_MUTATION,
            {"input": {"tenantId": str(self.tenant.id), "enabled": True}},
            self.endpoint_path,
            user=user,
        )

        assert result.errors is None
        payload = result.data["setScheduledReportEnabled"]
        assert payload["success"] is True
        assert payload["enabled"] is True

        tenant = await self._reload_tenant(self.tenant.id)
        assert tenant.scheduled_report_enabled is True

    @pytest.mark.asyncio
    async def test_client_cannot_toggle_another_tenant(self):
        """A client passing ANOTHER tenant's id can NEVER modify that tenant.

        resolve_target_tenant_id pins a client to their OWN tenant — the
        requested ``tenantId`` is ignored/overridden (same posture as
        TenantGoalsMutations + the support-ticket query). So the OTHER tenant
        is never touched; the toggle lands on the client's own tenant instead.
        This is the tenant-isolation guarantee."""
        user = await self._client_user_for("sr-cli-x", "x@test.com", self.tenant)

        result = await self._execute_mutation(
            SET_ENABLED_MUTATION,
            {"input": {"tenantId": str(self.other_tenant.id), "enabled": True}},
            self.endpoint_path,
            user=user,
        )

        assert result.errors is None
        payload = result.data["setScheduledReportEnabled"]
        # CRUCIAL: the OTHER tenant is NEVER enabled by a client who doesn't
        # own it — the requested id was overridden to the client's own tenant.
        other = await self._reload_tenant(self.other_tenant.id)
        assert other.scheduled_report_enabled is False
        # The toggle landed on the client's OWN tenant instead (override, not
        # a foreign write), so the mutation reports success for that tenant.
        assert payload["success"] is True
        own = await self._reload_tenant(self.tenant.id)
        assert own.scheduled_report_enabled is True

    # =================================================================
    # sendTestClientReport — SAFE PREVIEW to the REQUESTER only
    # =================================================================

    @pytest.mark.asyncio
    async def test_send_test_emails_only_the_requester_not_tenant_recipients(self):
        """The preview emails ONLY the requesting user's own email — NEVER the
        tenant's configured client recipients. This is the safety contract."""
        admin = await self._admin_user("sr-test-admin", "previewer@igniteproductions.co")

        with mock.patch(PDF_BUILDER_PATH, return_value=b"%PDF-1.7 fake") as mock_pdf, \
             mock.patch(MAILER_PATH) as MockMailer:
            MockMailer.return_value.send.return_value = None
            result = await self._execute_mutation(
                SEND_TEST_MUTATION,
                {
                    "input": {
                        "tenantId": str(self.tenant.id),
                        "clientMutationId": "test-1",
                    }
                },
                self.endpoint_path,
                user=admin,
            )

        assert result.errors is None
        payload = result.data["sendTestClientReport"]
        assert payload["success"] is True
        assert payload["clientMutationId"] == "test-1"

        # PDF generated once, with include_sentiment=False (no paid AI call).
        assert mock_pdf.call_count == 1
        assert mock_pdf.call_args.kwargs.get("include_sentiment") is False

        # Mailer constructed once and .send() called.
        assert MockMailer.call_count == 1
        MockMailer.return_value.send.assert_called_once()

        # THE key assertion: recipients == [the requester], NOT the tenant's
        # client recipients.
        kwargs = MockMailer.call_args.kwargs
        assert kwargs["recipients"] == ["previewer@igniteproductions.co"]
        tenant_recipients = await sync_to_async(
            self.tenant.scheduled_report_recipients
        )()
        assert tenant_recipients == ["client@girlbeer.com", "ops@girlbeer.com"]
        for client_addr in tenant_recipients:
            assert client_addr not in kwargs["recipients"]
        # Brand name threaded through (for the subject), but it's the requester
        # who receives it.
        assert kwargs["tenant_name"] == "Girl Beer"

    @pytest.mark.asyncio
    async def test_send_test_client_previews_own_tenant_to_self(self):
        """A client may preview THEIR OWN brand — and it still goes only to the
        client's own email, not the brand's recap recipients."""
        user = await self._client_user_for(
            "sr-test-cli", "me@girlbeer.com", self.tenant
        )

        with mock.patch(PDF_BUILDER_PATH, return_value=b"%PDF") as mock_pdf, \
             mock.patch(MAILER_PATH) as MockMailer:
            MockMailer.return_value.send.return_value = None
            result = await self._execute_mutation(
                SEND_TEST_MUTATION,
                {"input": {"tenantId": str(self.tenant.id)}},
                self.endpoint_path,
                user=user,
            )

        assert result.errors is None
        assert result.data["sendTestClientReport"]["success"] is True
        assert mock_pdf.call_count == 1
        assert MockMailer.call_args.kwargs["recipients"] == ["me@girlbeer.com"]

    @pytest.mark.asyncio
    async def test_send_test_client_cannot_preview_another_tenant(self):
        """A client passing ANOTHER tenant's id can NEVER preview that tenant.

        resolve_target_tenant_id pins the client to their OWN tenant, so the
        requested (foreign) id is overridden: the PDF is built for the client's
        OWN tenant, and the email still goes ONLY to the requester. The foreign
        tenant's data + recipients are never involved."""
        user = await self._client_user_for("sr-test-x", "cli-x@test.com", self.tenant)

        with mock.patch(PDF_BUILDER_PATH, return_value=b"%PDF") as mock_pdf, \
             mock.patch(MAILER_PATH) as MockMailer:
            MockMailer.return_value.send.return_value = None
            result = await self._execute_mutation(
                SEND_TEST_MUTATION,
                {"input": {"tenantId": str(self.other_tenant.id)}},
                self.endpoint_path,
                user=user,
            )

        assert result.errors is None
        # The PDF — if built at all — targets the client's OWN tenant, NEVER
        # the foreign tenant whose id was requested.
        if mock_pdf.call_count:
            assert mock_pdf.call_args.args[0] == self.tenant.id
            assert mock_pdf.call_args.args[0] != self.other_tenant.id
        # The email goes ONLY to the requester; the foreign tenant's recipients
        # are never used.
        if MockMailer.call_count:
            recipients = MockMailer.call_args.kwargs["recipients"]
            assert recipients == ["cli-x@test.com"]
            assert "brand@liquiddeath.com" not in recipients

    @pytest.mark.asyncio
    async def test_send_test_respects_month_override(self):
        """An explicit month "YYYY-MM" is passed through to the PDF builder."""
        admin = await self._admin_user("sr-test-mo", "mo@igniteproductions.co")

        with mock.patch(PDF_BUILDER_PATH, return_value=b"%PDF") as mock_pdf, \
             mock.patch(MAILER_PATH) as MockMailer:
            MockMailer.return_value.send.return_value = None
            result = await self._execute_mutation(
                SEND_TEST_MUTATION,
                {"input": {"tenantId": str(self.tenant.id), "month": "2026-03"}},
                self.endpoint_path,
                user=admin,
            )

        assert result.errors is None
        assert result.data["sendTestClientReport"]["success"] is True
        # build_client_monthly_report_pdf(tenant_id, 2026, 3, ...)
        args = mock_pdf.call_args.args
        assert args[1] == 2026
        assert args[2] == 3

    @pytest.mark.asyncio
    async def test_send_test_bad_month_returns_failure_no_send(self):
        """A malformed month string returns success=False and sends nothing."""
        admin = await self._admin_user("sr-test-badmo", "badmo@igniteproductions.co")

        with mock.patch(PDF_BUILDER_PATH) as mock_pdf, \
             mock.patch(MAILER_PATH) as MockMailer:
            result = await self._execute_mutation(
                SEND_TEST_MUTATION,
                {"input": {"tenantId": str(self.tenant.id), "month": "nonsense"}},
                self.endpoint_path,
                user=admin,
            )

        assert result.errors is None
        payload = result.data["sendTestClientReport"]
        assert payload["success"] is False
        assert "YYYY-MM" in payload["message"]
        assert mock_pdf.call_count == 0
        assert MockMailer.call_count == 0

    @pytest.mark.asyncio
    async def test_send_test_user_without_email_returns_failure(self):
        """A requester with no email on file -> success=False, nothing built."""
        # Admin-by-role but with an EMPTY email, so there is nowhere safe to
        # send the preview.
        user = await sync_to_async(self.create_user)(
            username="sr-noemail",
            email="",
            role=self.roles["spark_admin"],
            password="password123",
        )

        with mock.patch(PDF_BUILDER_PATH) as mock_pdf, \
             mock.patch(MAILER_PATH) as MockMailer:
            result = await self._execute_mutation(
                SEND_TEST_MUTATION,
                {"input": {"tenantId": str(self.tenant.id)}},
                self.endpoint_path,
                user=user,
            )

        assert result.errors is None
        payload = result.data["sendTestClientReport"]
        assert payload["success"] is False
        assert "email" in payload["message"].lower()
        # Never built a PDF or constructed the mailer.
        assert mock_pdf.call_count == 0
        assert MockMailer.call_count == 0

    @pytest.mark.asyncio
    async def test_send_test_generation_failure_returns_failure(self):
        """A PDF/generation failure is swallowed -> success=False, never raised."""
        from recaps.client_report import ClientMonthlyReportError

        admin = await self._admin_user("sr-test-fail", "fail@igniteproductions.co")

        with mock.patch(
            PDF_BUILDER_PATH, side_effect=ClientMonthlyReportError("boom")
        ) as mock_pdf, mock.patch(MAILER_PATH) as MockMailer:
            result = await self._execute_mutation(
                SEND_TEST_MUTATION,
                {"input": {"tenantId": str(self.tenant.id)}},
                self.endpoint_path,
                user=admin,
            )

        assert result.errors is None
        payload = result.data["sendTestClientReport"]
        assert payload["success"] is False
        assert mock_pdf.call_count == 1
        # The build blew up before the mailer was constructed.
        assert MockMailer.call_count == 0

    @pytest.mark.asyncio
    async def test_send_failure_returns_failure(self):
        """A mailer .send() exception is swallowed -> success=False, not raised."""
        admin = await self._admin_user("sr-send-fail", "sendfail@igniteproductions.co")

        with mock.patch(PDF_BUILDER_PATH, return_value=b"%PDF"), \
             mock.patch(MAILER_PATH) as MockMailer:
            MockMailer.return_value.send.side_effect = RuntimeError("Resend down")
            result = await self._execute_mutation(
                SEND_TEST_MUTATION,
                {"input": {"tenantId": str(self.tenant.id)}},
                self.endpoint_path,
                user=admin,
            )

        assert result.errors is None
        assert result.data["sendTestClientReport"]["success"] is False

    # =================================================================
    # auth gate (StrictIsAuthenticated)
    # =================================================================

    @pytest.mark.asyncio
    async def test_set_enabled_unauthenticated_is_denied(self):
        """An anonymous setScheduledReportEnabled is denied; nothing changes."""
        result = await self._execute_mutation(
            SET_ENABLED_MUTATION,
            {"input": {"tenantId": str(self.tenant.id), "enabled": True}},
            self.endpoint_path,
        )
        # Non-null field -> the denied resolver null-propagates the payload.
        assert result.errors is not None
        assert "authentication required" in str(result.errors[0]).lower()
        tenant = await self._reload_tenant(self.tenant.id)
        assert tenant.scheduled_report_enabled is False

    @pytest.mark.asyncio
    async def test_send_test_unauthenticated_is_denied(self):
        """An anonymous sendTestClientReport is denied; no PDF, no email."""
        with mock.patch(PDF_BUILDER_PATH) as mock_pdf, \
             mock.patch(MAILER_PATH) as MockMailer:
            result = await self._execute_mutation(
                SEND_TEST_MUTATION,
                {"input": {"tenantId": str(self.tenant.id)}},
                self.endpoint_path,
            )
        assert result.errors is not None
        assert "authentication required" in str(result.errors[0]).lower()
        assert mock_pdf.call_count == 0
        assert MockMailer.call_count == 0
