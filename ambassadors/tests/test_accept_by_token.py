"""
Tests for accept_by_token mutation.

This module tests:
- accept_by_token (authenticated mutation for accepting invitations)
"""
import pytest
import strawberry_django  # noqa: F401
import uuid
import strawberry.relay
from asgiref.sync import sync_to_async
from django.utils import timezone
from datetime import timedelta

from ambassadors.models import Ambassador, AmbassadorInvitation
from ambassadors.tests.base import AmbassadorsGraphQLTestCase
from jobs import models as job_models


@pytest.mark.django_db(transaction=True)
class TestAcceptByToken(AmbassadorsGraphQLTestCase):
    """Tests for accept_by_token mutation."""

    @pytest.fixture(autouse=True)
    def setup(self, db):
        """Set up test data."""
        from config.schema_spark import schema_spark

        self.roles = self.setup_default_roles()
        self.tenant = self.create_tenant(name="Accept By Token Tenant")
        self.system_user = self.get_system_user()

        # Create client user (inviter)
        unique_id = str(uuid.uuid4())[:8]
        self.client_user = self.create_user(
            username=f"client_token_{unique_id}@test.com",
            email=f"client_token_{unique_id}@test.com",
            role=self.roles['client']
        )
        self.create_tenanted_user(self.client_user, self.tenant)

        # Create ambassador user (who will accept)
        unique_id2 = str(uuid.uuid4())[:8]
        self.ambassador_user = self.create_user(
            username=f"ambassador_token_{unique_id2}@test.com",
            email=f"ambassador_token_{unique_id2}@test.com",
            role=self.roles['ambassador']
        )
        self.create_tenanted_user(self.ambassador_user, self.tenant)

        self.schema = schema_spark
        self.endpoint_path = "/api/v1/graphql/spark"

        self.mutation = """
            mutation AcceptByToken($input: AcceptByTokenInput!) {
                acceptByToken(input: $input) {
                    success
                    message
                    clientMutationId
                    ambassador {
                        id
                        isActive
                    }
                }
            }
        """

    @pytest.mark.asyncio
    async def test_accept_by_token_success_user_without_ambassador(self):
        """Test successful invitation acceptance when user doesn't have an ambassador."""
        unique_id = str(uuid.uuid4())[:8]
        token = f"token-success-{unique_id}"

        # Create invitation
        invitation = await sync_to_async(AmbassadorInvitation.objects.create)(
            email=self.ambassador_user.email,
            token=token,
            expires_at=timezone.now() + timedelta(days=7),
            invited_by=self.client_user,
            tenant=self.tenant,
            created_by=self.client_user,
            updated_by=self.client_user,
        )

        variables = {
            "input": {
                "token": token,
                "clientMutationId": "test-123",
            }
        }

        result = await self._execute_mutation_authenticated(
            self.mutation, variables, self.ambassador_user, self.endpoint_path
        )

        assert result.errors is None
        assert result.data is not None
        assert result.data["acceptByToken"]["success"] is True
        assert "accepted successfully" in result.data["acceptByToken"]["message"].lower(
        )

        # Verify ambassador was created
        ambassador = await sync_to_async(Ambassador.objects.get)(user=self.ambassador_user)
        # Note: is_active defaults to False for new ambassadors

        # Verify invitation was marked as used
        @sync_to_async
        def refresh_invitation():
            invitation.refresh_from_db()
            return invitation.is_used, invitation.ambassador_id
        is_used, invitation_ambassador_id = await refresh_invitation()
        assert is_used is True
        assert invitation_ambassador_id == ambassador.id

        # Verify ambassador ID matches
        ambassador_id = strawberry.relay.to_base64("Ambassador", ambassador.id)
        assert result.data["acceptByToken"]["ambassador"]["id"] == ambassador_id

    @pytest.mark.asyncio
    async def test_accept_by_token_success_user_with_ambassador(self):
        """Test successful invitation acceptance when user already has an ambassador."""
        unique_id = str(uuid.uuid4())[:8]
        token = f"token-existing-{unique_id}"

        # Create existing ambassador
        existing_ambassador = await sync_to_async(self.create_ambassador)(
            self.ambassador_user,
            address="123 Existing St",
            is_active=True,
        )

        # Create invitation
        invitation = await sync_to_async(AmbassadorInvitation.objects.create)(
            email=self.ambassador_user.email,
            token=token,
            expires_at=timezone.now() + timedelta(days=7),
            invited_by=self.client_user,
            tenant=self.tenant,
            created_by=self.client_user,
            updated_by=self.client_user,
        )

        variables = {
            "input": {
                "token": token,
                "clientMutationId": "test-456",
            }
        }

        result = await self._execute_mutation_authenticated(
            self.mutation, variables, self.ambassador_user, self.endpoint_path
        )

        assert result.errors is None
        assert result.data is not None
        assert result.data["acceptByToken"]["success"] is True

        # Verify existing ambassador was used (not a new one created)
        ambassador = await sync_to_async(Ambassador.objects.get)(user=self.ambassador_user)
        assert ambassador.id == existing_ambassador.id

        # Verify invitation was marked as used
        @sync_to_async
        def refresh_invitation():
            invitation.refresh_from_db()
            return invitation.is_used, invitation.ambassador_id
        is_used, invitation_ambassador_id = await refresh_invitation()
        assert is_used is True
        assert invitation_ambassador_id == ambassador.id

    @pytest.mark.asyncio
    async def test_accept_by_token_with_job(self):
        """Test successful invitation acceptance with job invitation."""
        unique_id = str(uuid.uuid4())[:8]
        token = f"token-job-{unique_id}"

        # Create job-related data
        event = await sync_to_async(self.create_event)(
            name="Test Event",
            tenant=self.tenant,
            address="123 Test St"
        )
        job_title = await sync_to_async(job_models.JobTitle.objects.create)(
            name="Promoter",
            tenant=self.tenant,
            created_by=self.system_user
        )
        rate_type = await sync_to_async(job_models.RateType.objects.create)(
            name="Hourly",
            tenant=self.tenant,
            created_by=self.system_user
        )
        rate = await sync_to_async(job_models.Rate.objects.create)(
            amount=75.0,
            rate_type=rate_type,
            tenant=self.tenant,
            created_by=self.system_user
        )
        job = await sync_to_async(job_models.Job.objects.create)(
            name="Test Job",
            code="JOB-TOKEN-001",
            address="123 Test St",
            event=event,
            job_title=job_title,
            tenant=self.tenant,
            rate=rate,
            created_by=self.system_user
        )

        # Create AmbassadorJob with "invited" status
        invited_status = await sync_to_async(job_models.Status.objects.get_invited)(
            tenant_id=self.tenant.id,
            user=self.client_user
        )
        ambassador_for_job = await sync_to_async(self.create_ambassador)(
            self.ambassador_user,
            is_active=True,
        )
        ambassador_job = await sync_to_async(job_models.AmbassadorJob.objects.create)(
            ambassador=ambassador_for_job,
            job=job,
            tenant=self.tenant,
            status=invited_status,
            rate=rate,
            appear_as_rfp=True,
            created_by=self.client_user,
            updated_by=self.client_user,
        )

        # Create invitation with job
        invitation = await sync_to_async(AmbassadorInvitation.objects.create)(
            email=self.ambassador_user.email,
            token=token,
            expires_at=timezone.now() + timedelta(days=7),
            invited_by=self.client_user,
            tenant=self.tenant,
            job=job,
            ambassador=ambassador_for_job,
            created_by=self.client_user,
            updated_by=self.client_user,
        )

        variables = {
            "input": {
                "token": token,
                "clientMutationId": "test-job",
            }
        }

        result = await self._execute_mutation_authenticated(
            self.mutation, variables, self.ambassador_user, self.endpoint_path
        )

        assert result.errors is None
        assert result.data is not None
        assert result.data["acceptByToken"]["success"] is True

        # Verify AmbassadorJob status was updated to "accepted"
        @sync_to_async
        def refresh_objects():
            ambassador_job.refresh_from_db()
            invitation.refresh_from_db()
            # Access related fields while in sync context
            status_id = ambassador_job.status.id
            is_used = invitation.is_used
            return ambassador_job, status_id, is_used
        ambassador_job, status_id, is_used = await refresh_objects()
        accepted_status = await sync_to_async(job_models.Status.objects.get_accepted)(
            tenant_id=self.tenant.id,
            user=self.client_user
        )
        assert status_id == accepted_status.id

        # Verify invitation was marked as used
        assert is_used is True

    @pytest.mark.asyncio
    async def test_accept_by_token_invalid_token(self):
        """Test acceptance with invalid token."""
        variables = {
            "input": {
                "token": "invalid-token-12345",
                "clientMutationId": "test-invalid",
            }
        }

        result = await self._execute_mutation_authenticated(
            self.mutation, variables, self.ambassador_user, self.endpoint_path
        )

        assert result.data is not None
        assert result.data["acceptByToken"]["success"] is False
        message_lower = result.data["acceptByToken"]["message"].lower()
        assert "token" in message_lower or "not found" in message_lower or "matching query does not exist" in message_lower

    @pytest.mark.asyncio
    async def test_accept_by_token_expired_invitation(self):
        """Test acceptance with expired invitation."""
        unique_id = str(uuid.uuid4())[:8]
        token = f"token-expired-{unique_id}"

        # Create expired invitation
        await sync_to_async(AmbassadorInvitation.objects.create)(
            email=self.ambassador_user.email,
            token=token,
            expires_at=timezone.now() - timedelta(days=1),
            invited_by=self.client_user,
            tenant=self.tenant,
            created_by=self.client_user,
            updated_by=self.client_user,
        )

        variables = {
            "input": {
                "token": token,
                "clientMutationId": "test-expired",
            }
        }

        result = await self._execute_mutation_authenticated(
            self.mutation, variables, self.ambassador_user, self.endpoint_path
        )

        assert result.data is not None
        assert result.data["acceptByToken"]["success"] is False
        assert "expired" in result.data["acceptByToken"]["message"].lower()

    @pytest.mark.asyncio
    async def test_accept_by_token_already_used(self):
        """Test acceptance with already used invitation."""
        unique_id = str(uuid.uuid4())[:8]
        token = f"token-used-{unique_id}"

        # Create used invitation
        ambassador = await sync_to_async(self.create_ambassador)(
            self.ambassador_user,
            is_active=True,
        )
        await sync_to_async(AmbassadorInvitation.objects.create)(
            email=self.ambassador_user.email,
            token=token,
            expires_at=timezone.now() + timedelta(days=7),
            is_used=True,
            used_at=timezone.now(),
            ambassador=ambassador,
            invited_by=self.client_user,
            tenant=self.tenant,
            created_by=self.client_user,
            updated_by=self.client_user,
        )

        variables = {
            "input": {
                "token": token,
                "clientMutationId": "test-used",
            }
        }

        result = await self._execute_mutation_authenticated(
            self.mutation, variables, self.ambassador_user, self.endpoint_path
        )

        assert result.data is not None
        assert result.data["acceptByToken"]["success"] is False
        assert "already been used" in result.data["acceptByToken"]["message"].lower(
        )

    @pytest.mark.asyncio
    async def test_accept_by_token_unauthorized(self):
        """Test that unauthenticated users cannot accept invitations."""
        unique_id = str(uuid.uuid4())[:8]
        token = f"token-unauth-{unique_id}"

        variables = {
            "input": {
                "token": token,
                "clientMutationId": "test-unauth",
            }
        }

        result = await self._execute_mutation(
            self.mutation, variables, self.endpoint_path
        )

        assert result.errors is not None
        assert any("not authenticated" in str(error).lower() or "authentication" in str(
            error).lower() for error in result.errors)
