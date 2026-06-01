"""GraphQL tests for Help-page support-ticket capture + Ignite-team notify.

Covers the clients-schema ``createSupportTicket`` mutation and the admin
``tenantSupportTickets`` query backed by :class:`tenants.models.SupportTicket`:

* create CAPTURES a row scoped to the authenticated user + their tenant,
* the Ignite team is notified by REUSING the request-approval recipient
  resolution (``IGNITE_REVIEW_CC`` + active spark-admins +
  ``REQUEST_REVIEW_COPY_EMAILS``) — asserted by mocking the mailer (no send),
* a mail failure NEVER fails the save (ticket still committed, success=True),
* category defaults / normalizes,
* a user without a bound tenant can still file,
* unauthenticated create is denied (StrictIsAuthenticated) and writes nothing,
* the admin query is tenant-scoped (clients pinned to their own tenant).

The mailer is always mocked — these tests never send real email.
"""

import pytest
from asgiref.sync import sync_to_async
from django.contrib.auth import get_user_model
from django.test import override_settings
from unittest.mock import patch

from config.schema_client import schema_clients
from tenants.models import SupportTicket
from tenants.tests.base import BaseGraphQLTestCase


User = get_user_model()

# Where the mailer class is looked up inside the mutation (imported lazily
# from tenants.envelopes), so patch it at its definition site.
MAILER_PATH = "tenants.envelopes.SupportTicketIgniteNotificationMailer"

CREATE_SUPPORT_TICKET_MUTATION = """
mutation CreateSupportTicket($input: CreateSupportTicketInput!) {
  createSupportTicket(input: $input) {
    success
    message
    ticket {
      id
      uuid
      subject
      body
      category
      status
    }
    clientMutationId
  }
}
"""

TENANT_SUPPORT_TICKETS_QUERY = """
query TenantSupportTickets($tenantId: ID!) {
  tenantSupportTickets(tenantId: $tenantId) {
    id
    subject
    category
    status
  }
}
"""


@pytest.mark.django_db(transaction=True)
class TestSupportTicketsGraphQL(BaseGraphQLTestCase):
    """GraphQL tests for support-ticket capture + Ignite notify."""

    @pytest.fixture(autouse=True)
    def setup(self, db):
        self.roles = self.setup_default_roles()
        self.schema = schema_clients
        self.endpoint_path = "/api/v1/graphql/clients"
        self.tenant = self.create_tenant(name="Support Tenant")
        self.other_tenant = self.create_tenant(name="Other Tenant")

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

    @pytest.mark.asyncio
    async def test_create_captures_row_scoped_to_user_and_tenant(self):
        """createSupportTicket saves a row with the auth user + their tenant."""
        user = await self._client_user_for("st-rt", "rt@test.com", self.tenant)

        with patch(MAILER_PATH) as MockMailer:
            MockMailer.return_value.send.return_value = None
            result = await self._execute_mutation(
                CREATE_SUPPORT_TICKET_MUTATION,
                {
                    "input": {
                        "subject": "Cannot invite a BA",
                        "body": "The INVITE BA button does nothing on Safari.",
                        "category": "bug",
                        "clientMutationId": "st-1",
                    }
                },
                self.endpoint_path,
                user=user,
            )

        assert result.errors is None
        payload = result.data["createSupportTicket"]
        assert payload["success"] is True
        assert payload["clientMutationId"] == "st-1"
        assert payload["ticket"]["subject"] == "Cannot invite a BA"
        assert payload["ticket"]["category"] == "bug"
        assert payload["ticket"]["status"] == "open"

        # Row persisted, scoped to user + tenant. (id is the relay global id;
        # look up by the uuid we also selected.)
        ticket = await sync_to_async(SupportTicket.objects.get)(
            uuid=payload["ticket"]["uuid"]
        )
        assert ticket.tenant_id == self.tenant.id
        assert ticket.created_by_id == user.id
        assert ticket.body == "The INVITE BA button does nothing on Safari."

    @pytest.mark.asyncio
    async def test_notify_uses_ignite_team_recipients(self):
        """The mailer is invoked with the REUSED Ignite-team recipient list:
        IGNITE_REVIEW_CC + active spark-admins + REQUEST_REVIEW_COPY_EMAILS."""
        user = await self._client_user_for("st-notif", "notif@test.com", self.tenant)
        # An active spark-admin → folded into recipients by _get_spark_admin_emails.
        await self._admin_user("st-admin", "ops-admin@igniteproductions.co")

        with (
            override_settings(REQUEST_REVIEW_COPY_EMAILS=["copylist@test.com"]),
            patch(MAILER_PATH) as MockMailer,
        ):
            MockMailer.return_value.send.return_value = None
            result = await self._execute_mutation(
                CREATE_SUPPORT_TICKET_MUTATION,
                {
                    "input": {
                        "subject": "Billing question",
                        "body": "Where do I see my invoices?",
                        "category": "billing",
                    }
                },
                self.endpoint_path,
                user=user,
            )

        assert result.errors is None
        assert result.data["createSupportTicket"]["success"] is True

        # Mailer constructed once and .send() called (no real email sent).
        assert MockMailer.call_count == 1
        MockMailer.return_value.send.assert_called_once()

        kwargs = MockMailer.call_args.kwargs
        recipients = [r.lower() for r in kwargs["to_emails"]]
        # Static Ignite ops team (IGNITE_REVIEW_CC).
        assert "events@igniteproductions.co" in recipients
        assert "kyle@igniteproductions.co" in recipients
        # Active spark-admin folded in.
        assert "ops-admin@igniteproductions.co" in recipients
        # Settings-configured review copy list.
        assert "copylist@test.com" in recipients
        # Subject + tenant name threaded through to the mailer.
        assert kwargs["subject"] == "Billing question"
        assert kwargs["tenant_name"] == "Support Tenant"
        assert kwargs["submitter_email"] == "notif@test.com"

    @pytest.mark.asyncio
    async def test_mail_failure_still_saves_and_returns_success(self):
        """A mailer exception never fails the ticket save (mirrors the recaps
        approval-notify error handling): row committed, success=True."""
        user = await self._client_user_for("st-fail", "fail@test.com", self.tenant)

        with patch(MAILER_PATH) as MockMailer:
            MockMailer.return_value.send.side_effect = RuntimeError("Resend down")
            result = await self._execute_mutation(
                CREATE_SUPPORT_TICKET_MUTATION,
                {
                    "input": {
                        "subject": "Mail hiccup",
                        "body": "Notify should not fail the save.",
                    }
                },
                self.endpoint_path,
                user=user,
            )

        assert result.errors is None
        payload = result.data["createSupportTicket"]
        # Save succeeded despite the mail failure.
        assert payload["success"] is True
        assert payload["ticket"] is not None

        ticket = await sync_to_async(SupportTicket.objects.get)(
            uuid=payload["ticket"]["uuid"]
        )
        assert ticket.subject == "Mail hiccup"
        assert ticket.created_by_id == user.id

    @pytest.mark.asyncio
    async def test_category_defaults_and_normalizes_to_other(self):
        """Omitted / unknown category collapses to 'other'."""
        user = await self._client_user_for("st-cat", "cat@test.com", self.tenant)

        with patch(MAILER_PATH) as MockMailer:
            MockMailer.return_value.send.return_value = None
            # No category supplied.
            r1 = await self._execute_mutation(
                CREATE_SUPPORT_TICKET_MUTATION,
                {"input": {"subject": "No category", "body": "x"}},
                self.endpoint_path,
                user=user,
            )
            # Garbage category.
            r2 = await self._execute_mutation(
                CREATE_SUPPORT_TICKET_MUTATION,
                {
                    "input": {
                        "subject": "Bad category",
                        "body": "y",
                        "category": "not-a-real-bucket",
                    }
                },
                self.endpoint_path,
                user=user,
            )

        assert r1.data["createSupportTicket"]["ticket"]["category"] == "other"
        assert r2.data["createSupportTicket"]["ticket"]["category"] == "other"

    @pytest.mark.asyncio
    async def test_blank_subject_or_body_is_rejected(self):
        """Empty subject/body returns success=False and writes nothing."""
        user = await self._client_user_for("st-blank", "blank@test.com", self.tenant)

        with patch(MAILER_PATH) as MockMailer:
            result = await self._execute_mutation(
                CREATE_SUPPORT_TICKET_MUTATION,
                {"input": {"subject": "   ", "body": ""}},
                self.endpoint_path,
                user=user,
            )

        assert result.errors is None
        assert result.data["createSupportTicket"]["success"] is False
        assert result.data["createSupportTicket"]["ticket"] is None
        # No mailer, no row.
        assert not MockMailer.called
        count = await sync_to_async(SupportTicket.objects.count)()
        assert count == 0

    @pytest.mark.asyncio
    async def test_user_without_tenant_can_still_file(self):
        """A signed-in user lacking a tenant still files (tenant=None)."""
        user = await sync_to_async(self.create_user)(
            username="st-notenant",
            email="notenant@test.com",
            role=self.roles["client"],
            password="password123",
        )  # no create_tenanted_user → get_tenant() returns None

        with patch(MAILER_PATH) as MockMailer:
            MockMailer.return_value.send.return_value = None
            result = await self._execute_mutation(
                CREATE_SUPPORT_TICKET_MUTATION,
                {"input": {"subject": "No tenant", "body": "still works"}},
                self.endpoint_path,
                user=user,
            )

        assert result.errors is None
        payload = result.data["createSupportTicket"]
        assert payload["success"] is True
        ticket = await sync_to_async(SupportTicket.objects.get)(
            uuid=payload["ticket"]["uuid"]
        )
        assert ticket.tenant_id is None
        assert ticket.created_by_id == user.id
        # Mailer still invoked (IGNITE_REVIEW_CC is always present) with no
        # tenant name.
        assert MockMailer.called
        assert MockMailer.call_args.kwargs["tenant_name"] is None

    @pytest.mark.asyncio
    async def test_unauthenticated_create_is_denied_not_crashed(self):
        """StrictIsAuthenticated denies an anonymous create; nothing written."""
        with patch(MAILER_PATH) as MockMailer:
            result = await self._execute_mutation(
                CREATE_SUPPORT_TICKET_MUTATION,
                {"input": {"subject": "Nope", "body": "should not save"}},
                self.endpoint_path,
            )

        # createSupportTicket is non-null, so the denied field null-propagates
        # the whole payload — clean auth error, nothing written.
        assert result.errors is not None
        assert "authentication required" in str(result.errors[0]).lower()
        assert result.data is None
        assert not MockMailer.called
        wrote = await sync_to_async(
            SupportTicket.objects.filter(subject="Nope").exists
        )()
        assert wrote is False

    @pytest.mark.asyncio
    async def test_admin_query_lists_tenant_tickets(self):
        """tenantSupportTickets returns a tenant's tickets for an admin."""
        admin = await self._admin_user("st-q-admin", "qadmin@test.com")
        await sync_to_async(SupportTicket.objects.create)(
            tenant=self.tenant, subject="One", body="b", category="bug"
        )
        await sync_to_async(SupportTicket.objects.create)(
            tenant=self.other_tenant, subject="Elsewhere", body="b"
        )

        result = await self._execute_mutation(
            TENANT_SUPPORT_TICKETS_QUERY,
            {"tenantId": str(self.tenant.id)},
            self.endpoint_path,
            user=admin,
        )
        assert result.errors is None
        tickets = result.data["tenantSupportTickets"]
        assert [t["subject"] for t in tickets] == ["One"]

    @pytest.mark.asyncio
    async def test_client_query_pinned_to_own_tenant(self):
        """A client passing another tenant's id is pinned to their own tenant."""
        user = await self._client_user_for("st-q-cli", "qcli@test.com", self.tenant)
        await sync_to_async(SupportTicket.objects.create)(
            tenant=self.tenant, subject="Mine", body="b", created_by=user
        )
        await sync_to_async(SupportTicket.objects.create)(
            tenant=self.other_tenant, subject="Theirs", body="b"
        )

        # Ask for the OTHER tenant; scoping overrides to own tenant.
        result = await self._execute_mutation(
            TENANT_SUPPORT_TICKETS_QUERY,
            {"tenantId": str(self.other_tenant.id)},
            self.endpoint_path,
            user=user,
        )
        assert result.errors is None
        tickets = result.data["tenantSupportTickets"]
        assert [t["subject"] for t in tickets] == ["Mine"]
