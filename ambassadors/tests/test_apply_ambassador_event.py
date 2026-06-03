"""
Tests for apply_ambassador_event mutation and envelope mailers.

This module tests:
- apply_ambassador_event (authenticated mutation)
- MailChain email sending functionality
- SendInvitationMailToAmbassadorMailer envelope mailer
"""
import pytest
import strawberry_django  # noqa: F401
import uuid
from unittest.mock import patch, MagicMock
from asgiref.sync import sync_to_async
from events.models import Event, Request
from ambassadors.models import AmbassadorEvent
from ambassadors.tests.base import AmbassadorsGraphQLTestCase
from config.schema_spark import schema_spark
from ambassadors.envelopes import AmbassadorEventApplicationMailer, NotifyApplicationToClientMailer
from utils.mailer import MailChain


@pytest.mark.django_db(transaction=True)
class TestApplyAmbassadorEvent(AmbassadorsGraphQLTestCase):
    """Tests for apply_ambassador_event mutation."""

    @pytest.fixture(autouse=True)
    def setup(self, db):
        """Set up test data."""
        self.schema = schema_spark
        self.endpoint_path = "/api/v1/graphql/spark"
        self.system_user = self.get_system_user()

        self.roles = self.setup_default_roles()
        self.tenant = self.create_tenant(name="Apply Event Test Tenant")
        # Use UUID for user to ensure uniqueness across test runs
        unique_id = str(uuid.uuid4())[:8]
        self.user = self.create_user(
            username=f"ambassador_apply_{unique_id}@spark.local",
            email=f"ambassador_apply_{unique_id}@spark.local",
            role=self.roles['ambassador']
        )
        self.create_tenanted_user(self.user, self.tenant)

        # Create Ambassador profile
        self.ambassador = self.create_ambassador(
            user=self.user,
            created_by=self.system_user,
        )

        self.request_status = self.create_request_status(
            "Pending", self.tenant)
        self.request_type = self.create_request_type("Demo", self.tenant)

        # Create Request manually (helper requires client/distributor/retailer we don't need)
        self.request = Request.objects.create(
            name="Test Request",
            date="2023-10-27",
            address="123 Test St",
            tenant=self.tenant,
            status=self.request_status,
            request_type=self.request_type,
            created_by=self.system_user,
        )

        # Create Event manually
        self.event = Event.objects.create(
            name="Test Event",
            tenant=self.tenant,
            request=self.request,
            created_by=self.system_user,
            address="123 Test St"
        )

        # Create a client user to receive notification emails
        self.client_user = self.create_user(
            username=f"client_apply_{unique_id}@spark.local",
            email=f"client_apply_{unique_id}@spark.local",
            role=self.roles['client']
        )
        self.create_tenanted_user(self.client_user, self.tenant)

        self.mutation = """
            mutation ApplyAmbassadorEvent($eventId: ID!) {
                applyAmbassadorEvent(eventId: $eventId) {
                    success
                    message
                    application {
                        id
                        isApproved
                    }
                }
            }
        """

    @pytest.mark.asyncio
    async def test_apply_ambassador_event_success(self):
        """Test successful ambassador event application."""
        variables = {"eventId": str(self.event.id)}
        response = await self._execute_mutation_authenticated(
            self.mutation, variables=variables, user=self.user, endpoint_path=self.endpoint_path
        )

        assert response.errors is None
        assert response.data["applyAmbassadorEvent"]["success"] is True
        assert response.data["applyAmbassadorEvent"]["message"] == "Application successful"
        assert response.data["applyAmbassadorEvent"]["application"]["id"] is not None
        assert response.data["applyAmbassadorEvent"]["application"]["isApproved"] is False

        # Verify DB
        exists = await sync_to_async(
            AmbassadorEvent.objects.filter(
                ambassador=self.ambassador, event=self.event
            ).exists
        )()
        assert exists is True

    @pytest.mark.asyncio
    @patch('ambassadors.mutations.MailChain.send_chain_async')
    async def test_apply_ambassador_event_sends_emails(self, mock_send_chain_async):
        """Test that apply_ambassador_event sends emails via MailChain."""
        variables = {"eventId": str(self.event.id)}

        # Mock the async method to return a chain
        mock_chain = MagicMock()
        mock_send_chain_async.return_value = mock_chain

        response = await self._execute_mutation_authenticated(
            self.mutation, variables=variables, user=self.user, endpoint_path=self.endpoint_path
        )

        assert response.errors is None
        assert response.data["applyAmbassadorEvent"]["success"] is True

        # Verify MailChain.send_chain_async was called
        assert mock_send_chain_async.called is True

        # Get the mailers that were passed to send_chain_async
        call_args = mock_send_chain_async.call_args
        # First positional argument is the list of mailers
        mailers = call_args[0][0]

        # Verify two mailers were passed
        assert len(mailers) == 2

        # Verify the mailers are of the correct types
        assert isinstance(mailers[0], AmbassadorEventApplicationMailer)
        assert isinstance(mailers[1], NotifyApplicationToClientMailer)

        # Verify both mailers have the same application
        assert mailers[0].application == mailers[1].application

    @pytest.mark.asyncio
    @patch('utils.mailer.Queues')
    async def test_mail_chain_enqueues_emails(self, mock_queues_class):
        """Test that MailChain properly enqueues emails to RQ workers."""
        # Create an application for testing
        application = await sync_to_async(AmbassadorEvent.objects.create)(
            ambassador=self.ambassador,
            event=self.event,
            tenant=self.tenant,
            created_by=self.user,
        )

        # Mock the Queues
        mock_queues = MagicMock()
        mock_queue = MagicMock()
        mock_queues.default = mock_queue
        mock_queues_class.return_value = mock_queues

        # Create mailers and send via chain
        mailers = [
            AmbassadorEventApplicationMailer(application),
            NotifyApplicationToClientMailer(application),
        ]

        # Send the chain (this will enqueue to RQ)
        await MailChain.send_chain_async(mailers)

        # Verify that queue.add was called twice (once for each mailer)
        assert mock_queue.add.call_count == 2

        # Verify the calls were made with send_email_task
        from utils.mailer import send_email_task
        calls = mock_queue.add.call_args_list
        for call_args in calls:
            # First argument should be send_email_task function
            assert call_args[0][0] == send_email_task
            # Second argument should be payload (envelope.compile())
            assert 'payload' in call_args[1] or len(call_args[0]) > 1

    @pytest.mark.asyncio
    async def test_notify_client_mailer_gets_client_emails(self):
        """Test that NotifyApplicationToClientMailer correctly identifies client emails."""
        # Create an application
        application = await sync_to_async(AmbassadorEvent.objects.create)(
            ambassador=self.ambassador,
            event=self.event,
            tenant=self.tenant,
            created_by=self.user,
        )

        # Create the mailer
        mailer = NotifyApplicationToClientMailer(application)

        # Get the envelope (wrap in sync_to_async since envelope() does database queries)
        envelope = await sync_to_async(mailer.envelope)()

        # Verify the envelope has the client user's email
        assert self.client_user.email in envelope.to_emails
        assert envelope.subject == "New application has been received"
        assert envelope.template == "ambassadors.templates.emails.notify_application_to_client"
        assert envelope.context["application"] == application

    @pytest.mark.asyncio
    async def test_ambassador_event_application_mailer_envelope(self):
        """Test that AmbassadorEventApplicationMailer creates correct envelope."""
        # Create an application
        application = await sync_to_async(AmbassadorEvent.objects.create)(
            ambassador=self.ambassador,
            event=self.event,
            tenant=self.tenant,
            created_by=self.user,
        )

        # Create the mailer
        mailer = AmbassadorEventApplicationMailer(application)

        # Get the envelope
        envelope = mailer.envelope()

        # Verify envelope properties
        assert envelope.subject == "Your application has been received"
        assert envelope.template == "ambassadors.templates.emails.event_application"
        assert envelope.to_emails == [self.ambassador.user.email]
        assert envelope.context["application"] == application

    @pytest.mark.asyncio
    async def test_apply_ambassador_event_already_applied(self):
        """Test applying to an event that was already applied to."""
        # Create existing application
        await sync_to_async(AmbassadorEvent.objects.create)(
            ambassador=self.ambassador,
            event=self.event,
            tenant=self.tenant,
            created_by=self.user,
        )

        variables = {"eventId": str(self.event.id)}
        response = await self._execute_mutation_authenticated(
            self.mutation, variables=variables, user=self.user, endpoint_path=self.endpoint_path
        )

        assert response.data["applyAmbassadorEvent"]["success"] is False
        assert response.data["applyAmbassadorEvent"]["message"] == "Already applied to this event"


@pytest.mark.django_db(transaction=True)
class TestSendInvitationMailToAmbassadorMailer(AmbassadorsGraphQLTestCase):
    """Tests for SendInvitationMailToAmbassadorMailer envelope mailer."""

    @pytest.fixture(autouse=True)
    def setup(self, db):
        """Set up test data."""
        from jobs import models as job_models
        self.system_user = self.get_system_user()
        self.roles = self.setup_default_roles()
        self.tenant = self.create_tenant(name="Invitation Mailer Test Tenant")

        # Use UUID for user to ensure uniqueness across test runs
        unique_id = str(uuid.uuid4())[:8]
        self.client_user = self.create_user(
            username=f"client_mailer_{unique_id}@test.com",
            email=f"client_mailer_{unique_id}@test.com",
            role=self.roles['client']
        )
        self.create_tenanted_user(self.client_user, self.tenant)

        # Create ambassador user and ambassador
        self.ambassador_user = self.create_user(
            username=f"ambassador_mailer_{unique_id}@test.com",
            email=f"ambassador_mailer_{unique_id}@test.com",
            role=self.roles['ambassador']
        )
        self.create_tenanted_user(self.ambassador_user, self.tenant)
        self.ambassador = self.create_ambassador(
            user=self.ambassador_user,
            created_by=self.system_user,
        )

        # Create job-related test data
        self.event = self.create_event(
            name="Test Event",
            tenant=self.tenant,
            address="123 Test St"
        )
        self.job_title = job_models.JobTitle.objects.create(
            name="Promoter",
            tenant=self.tenant,
            created_by=self.system_user
        )
        self.rate_type = job_models.RateType.objects.create(
            name="Hourly",
            tenant=self.tenant,
            created_by=self.system_user
        )
        self.rate = job_models.Rate.objects.create(
            amount=75.0,
            rate_type=self.rate_type,
            tenant=self.tenant,
            created_by=self.system_user
        )
        self.job = job_models.Job.objects.create(
            name="Test Job",
            code="JOB-MAILER-001",
            address="123 Test St",
            event=self.event,
            job_title=self.job_title,
            tenant=self.tenant,
            rate=self.rate,
            created_by=self.system_user
        )

    @pytest.mark.asyncio
    async def test_send_invitation_mail_to_ambassador_mailer_envelope(self):
        """Test that SendInvitationMailToAmbassadorMailer creates correct envelope."""
        from django.utils import timezone
        from datetime import timedelta
        from ambassadors.models import AmbassadorInvitation
        from ambassadors.envelopes import SendInvitationMailToAmbassadorMailer
        from ambassadors.constants import INVITATION_EXPIRY_DAYS

        # Create an invitation with job and ambassador
        invitation = await sync_to_async(AmbassadorInvitation.objects.create)(
            email=self.ambassador_user.email,
            token="test-token-123",
            expires_at=timezone.now() + timedelta(days=INVITATION_EXPIRY_DAYS),
            invited_by=self.client_user,
            tenant=self.tenant,
            job=self.job,
            ambassador=self.ambassador,
            created_by=self.client_user,
            updated_by=self.client_user,
        )

        # Create the mailer
        mailer = SendInvitationMailToAmbassadorMailer(invitation)

        # Get the envelope. envelope() runs ORM queries (it builds the
        # deep-link invite context), so it must be wrapped in sync_to_async
        # when called from an async test — same as the other envelope tests.
        envelope = await sync_to_async(mailer.envelope)()

        # Verify envelope properties. As of the "unify invitation flows to
        # deep-link template" refactor the mailer renders the shared job
        # invite template with a flattened context (no raw `invitation`
        # object), so assert against that contract.
        assert envelope.subject == "You have been invited to a job"
        assert envelope.template == "jobs.templates.emails.ambassador_invited_to_job"
        assert envelope.to_emails == [invitation.email]
        assert envelope.context["brand_name"] == self.tenant.name
        assert envelope.context["recipient_first_name"] == "there"
        assert envelope.context["deep_link"].startswith("spark://my-gigs/")

    @pytest.mark.asyncio
    async def test_send_invitation_mail_to_ambassador_mailer_template_renders(self):
        """Test that the template renders correctly without syntax errors."""
        from django.utils import timezone
        from datetime import timedelta
        from ambassadors.models import AmbassadorInvitation
        from ambassadors.envelopes import SendInvitationMailToAmbassadorMailer
        from ambassadors.constants import INVITATION_EXPIRY_DAYS

        # Create an invitation with job and ambassador
        invitation = await sync_to_async(AmbassadorInvitation.objects.create)(
            email=self.ambassador_user.email,
            token="test-token-render-123",
            expires_at=timezone.now() + timedelta(days=INVITATION_EXPIRY_DAYS),
            invited_by=self.client_user,
            tenant=self.tenant,
            job=self.job,
            ambassador=self.ambassador,
            created_by=self.client_user,
            updated_by=self.client_user,
        )

        # Create the mailer
        mailer = SendInvitationMailToAmbassadorMailer(invitation)

        # Build the envelope and render it. envelope() runs ORM queries to
        # assemble the invite context, so wrap the whole thing in
        # sync_to_async (mirrors the other envelope tests).
        def _render():
            return mailer.envelope().render_template()

        rendered_html = await sync_to_async(_render)()
        assert rendered_html is not None
        assert len(rendered_html) > 0
        # Verify the template contains expected content
        assert "invited" in rendered_html.lower() or "invitation" in rendered_html.lower()

    @pytest.mark.asyncio
    async def test_send_invitation_mail_to_ambassador_mailer_sends(self):
        """Test that the mailer can send (with mocked dispatch)."""
        from django.utils import timezone
        from datetime import timedelta
        from ambassadors.models import AmbassadorInvitation
        from ambassadors.envelopes import SendInvitationMailToAmbassadorMailer
        from ambassadors.constants import INVITATION_EXPIRY_DAYS

        # Create an invitation
        invitation = await sync_to_async(AmbassadorInvitation.objects.create)(
            email=self.ambassador_user.email,
            token="test-token-send-123",
            expires_at=timezone.now() + timedelta(days=INVITATION_EXPIRY_DAYS),
            invited_by=self.client_user,
            tenant=self.tenant,
            job=self.job,
            ambassador=self.ambassador,
            created_by=self.client_user,
            updated_by=self.client_user,
        )

        # Create the mailer
        mailer = SendInvitationMailToAmbassadorMailer(invitation)

        # Test that mailer can send (with mocked dispatch)
        with patch.object(mailer, 'dispatch') as mock_dispatch:
            mailer.send_now()
            mock_dispatch.assert_called_once()
