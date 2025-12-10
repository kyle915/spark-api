"""
Tests for ambassador models.

This module tests:
- Ambassador model (is_active field)
- AmbassadorInvitation model
"""
import pytest
import uuid
from asgiref.sync import sync_to_async
from django.utils import timezone
from datetime import timedelta

from ambassadors.models import Ambassador, AmbassadorInvitation
from ambassadors.tests.base import AmbassadorsGraphQLTestCase
from ambassadors.constants import INVITATION_EXPIRY_DAYS


@pytest.mark.django_db(transaction=True)
class TestAmbassadorModel(AmbassadorsGraphQLTestCase):
    """Tests for Ambassador model."""

    @pytest.fixture(autouse=True)
    def setup(self, db):
        """Set up test data."""
        self.roles = self.setup_default_roles()
        self.tenant = self.create_tenant(name="Ambassador Model Test Tenant")
        # Use UUID for user to ensure uniqueness across test runs
        unique_id = str(uuid.uuid4())[:8]
        self.ambassador_user = self.create_user(
            username=f"ambassador_model_{unique_id}@test.com",
            email=f"ambassador_model_{unique_id}@test.com",
            role=self.roles['ambassador']
        )

    @pytest.mark.asyncio
    async def test_ambassador_is_active_default_false(self):
        """Test that ambassador is_active defaults to False."""
        # Create ambassador without specifying is_active to test default
        # We need to bypass the helper's default and use the model's default
        from asgiref.sync import sync_to_async as stoa
        system_user = await stoa(self.get_system_user)()
        ambassador = await stoa(Ambassador.objects.create)(
            user=self.ambassador_user,
            created_by=system_user,
            updated_by=system_user,
            # Don't pass is_active to let model default handle it
        )
        # Refresh from DB
        ambassador = await sync_to_async(Ambassador.objects.get)(pk=ambassador.pk)
        assert ambassador.is_active is False

    @pytest.mark.asyncio
    async def test_ambassador_is_active_true(self):
        """Test creating ambassador with is_active=True."""
        ambassador = await sync_to_async(self.create_ambassador)(
            user=self.ambassador_user,
            is_active=True,
        )
        assert ambassador.is_active is True

    @pytest.mark.asyncio
    async def test_ambassador_coordinates_storage(self):
        """Test that coordinates are stored correctly."""
        coordinates = [40.7128, -74.0060]  # NYC coordinates
        ambassador = await sync_to_async(self.create_ambassador)(
            user=self.ambassador_user,
            coordinates=coordinates,
        )
        ambassador = await sync_to_async(Ambassador.objects.get)(pk=ambassador.pk)
        assert ambassador.coordinates == coordinates


@pytest.mark.django_db(transaction=True)
class TestAmbassadorInvitationModel(AmbassadorsGraphQLTestCase):
    """Tests for AmbassadorInvitation model."""

    @pytest.fixture(autouse=True)
    def setup(self, db):
        """Set up test data."""
        self.roles = self.setup_default_roles()
        self.tenant = self.create_tenant(name="Invitation Model Test Tenant")
        # Use UUID for user to ensure uniqueness across test runs
        unique_id = str(uuid.uuid4())[:8]
        self.client_user = self.create_user(
            username=f"client_model_{unique_id}@test.com",
            email=f"client_model_{unique_id}@test.com",
            role=self.roles['client']
        )

    @pytest.mark.asyncio
    async def test_invitation_creation(self):
        """Test creating an invitation."""
        unique_id = str(uuid.uuid4())[:8]
        email = f"invited_{unique_id}@test.com"
        token = f"test-token-{unique_id}"

        invitation = await sync_to_async(AmbassadorInvitation.objects.create)(
            email=email,
            token=token,
            expires_at=timezone.now() + timedelta(days=INVITATION_EXPIRY_DAYS),
            invited_by=self.client_user,
            tenant=self.tenant,
            created_by=self.client_user,
            updated_by=self.client_user,
        )
        assert invitation.email == email
        assert invitation.token == token
        assert invitation.is_used is False
        assert invitation.used_at is None
        assert invitation.ambassador is None

    @pytest.mark.asyncio
    async def test_invitation_token_uniqueness(self):
        """Test that invitation tokens must be unique."""
        unique_id = str(uuid.uuid4())[:8]
        token = f"unique-token-{unique_id}"

        await sync_to_async(AmbassadorInvitation.objects.create)(
            email=f"invited1_{unique_id}@test.com",
            token=token,
            expires_at=timezone.now() + timedelta(days=7),
            invited_by=self.client_user,
            tenant=self.tenant,
            created_by=self.client_user,
            updated_by=self.client_user,
        )

        # Try to create another invitation with the same token
        with pytest.raises(Exception):  # IntegrityError
            await sync_to_async(AmbassadorInvitation.objects.create)(
                email=f"invited2_{unique_id}@test.com",
                token=token,  # Duplicate token
                expires_at=timezone.now() + timedelta(days=7),
                invited_by=self.client_user,
                tenant=self.tenant,
                created_by=self.client_user,
                updated_by=self.client_user,
            )

    @pytest.mark.asyncio
    async def test_invitation_expiry(self):
        """Test invitation expiry check."""
        unique_id = str(uuid.uuid4())[:8]
        email = f"expired_{unique_id}@test.com"
        token = f"expired-token-{unique_id}"
        past_date = timezone.now() - timedelta(days=1)

        invitation = await sync_to_async(AmbassadorInvitation.objects.create)(
            email=email,
            token=token,
            expires_at=past_date,
            invited_by=self.client_user,
            tenant=self.tenant,
            created_by=self.client_user,
            updated_by=self.client_user,
        )
        assert invitation.expires_at < timezone.now()
