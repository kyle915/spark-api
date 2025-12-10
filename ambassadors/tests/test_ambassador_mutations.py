"""
Tests for ambassador mutations.

This module tests:
- create_public_ambassador (public mutation)
- create_ambassador_invitation (authenticated)
- accept_ambassador_invitation (public with token)
- approve_ambassador (authenticated)
"""
import pytest
import strawberry_django  # noqa: F401
import uuid
from asgiref.sync import sync_to_async
from django.contrib.auth import get_user_model
from django.utils import timezone
from datetime import timedelta

from ambassadors.models import Ambassador, AmbassadorInvitation
from ambassadors.tests.base import AmbassadorsGraphQLTestCase
from ambassadors.constants import INVITATION_EXPIRY_DAYS
from tenants.models import Tenant, TenantedUser
from utils.utils import ROLE_ID

User = get_user_model()


@pytest.mark.django_db(transaction=True)
class TestPublicAmbassadorCreation(AmbassadorsGraphQLTestCase):
    """Tests for create_public_ambassador mutation."""

    @pytest.fixture(autouse=True)
    def setup(self, db):
        """Set up test data."""
        from config.schema_ambassador import schema_ambassador
        self.roles = self.setup_default_roles()
        self.schema = schema_ambassador
        self.endpoint_path = "/api/v1/graphql/ambassadors"

        self.mutation = """
            mutation CreatePublicAmbassador($input: CreatePublicAmbassadorInput!) {
                createPublicAmbassador(input: $input) {
                    success
                    message
                    clientMutationId
                    ambassador {
                        id
                        isActive
                        address
                        coordinates
                    }
                    activationToken
                }
            }
        """

    @pytest.mark.asyncio
    async def test_create_public_ambassador_success(self):
        """Test successful public ambassador creation."""
        unique_id = str(uuid.uuid4())[:8]
        email = f"newambassador_{unique_id}@test.com"

        variables = {
            "input": {
                "firstName": "John",
                "email": email,
                "password1": "testpass123",
                "password2": "testpass123",
                "address": "123 Test St",
                "coordinates": [40.7128, -74.0060],
                "clientMutationId": "test-123",
            }
        }

        result = await self._execute_mutation(
            self.mutation, variables, self.endpoint_path
        )

        assert result.errors is None, f"Errors: {result.errors}"
        assert result.data is not None
        assert result.data["createPublicAmbassador"]["success"] is True, \
            f"Expected success but got: {result.data.get('createPublicAmbassador', {}).get('message', 'Unknown error')}"
        assert "created successfully" in result.data["createPublicAmbassador"]["message"].lower(
        )
        assert result.data["createPublicAmbassador"]["clientMutationId"] == "test-123"
        assert result.data["createPublicAmbassador"]["activationToken"] is not None

        # Verify user was created
        user = await sync_to_async(User.objects.get)(email=email)
        assert user.first_name == "John"
        assert user.role.id == ROLE_ID.Ambassadors
        assert user.is_active is False  # Inactive until email verification

        # Verify ambassador was created
        ambassador = await sync_to_async(Ambassador.objects.get)(user=user)
        assert ambassador.is_active is False  # Requires manual approval
        assert ambassador.address == "123 Test St"
        assert ambassador.coordinates == [40.7128, -74.0060]

    @pytest.mark.asyncio
    async def test_create_public_ambassador_password_mismatch(self):
        """Test public ambassador creation with mismatched passwords."""
        unique_id = str(uuid.uuid4())[:8]
        email = f"test2_{unique_id}@test.com"

        variables = {
            "input": {
                "firstName": "John",
                "email": email,
                "password1": "testpass123",
                "password2": "differentpass",
                "clientMutationId": "test-123",
            }
        }

        result = await self._execute_mutation(
            self.mutation, variables, self.endpoint_path
        )

        assert result.data is not None
        assert result.data["createPublicAmbassador"]["success"] is False
        assert "password" in result.data["createPublicAmbassador"]["message"].lower(
        )
        assert result.data["createPublicAmbassador"]["ambassador"] is None

    @pytest.mark.asyncio
    async def test_create_public_ambassador_duplicate_email(self):
        """Test public ambassador creation with existing email."""
        unique_id = str(uuid.uuid4())[:8]
        email = f"existing_public_{unique_id}@test.com"

        # Create existing user
        existing_user = await sync_to_async(self.create_user)(
            username=email,
            email=email,
            role=self.roles['ambassador']
        )

        variables = {
            "input": {
                "firstName": "John",
                "email": email,
                "password1": "testpass123",
                "password2": "testpass123",
                "clientMutationId": "test-123",
            }
        }

        result = await self._execute_mutation(
            self.mutation, variables, self.endpoint_path
        )

        assert result.data is not None
        assert result.data["createPublicAmbassador"]["success"] is False
        assert "email" in result.data["createPublicAmbassador"]["message"].lower(
        )

    @pytest.mark.asyncio
    async def test_create_public_ambassador_with_coordinates(self):
        """Test public ambassador creation with coordinates."""
        unique_id = str(uuid.uuid4())[:8]
        email = f"jane_{unique_id}@test.com"

        variables = {
            "input": {
                "firstName": "Jane",
                "email": email,
                "password1": "testpass123",
                "password2": "testpass123",
                "coordinates": [-33.8688, 151.2093],  # Sydney coordinates
                "clientMutationId": "test-coords",
            }
        }

        result = await self._execute_mutation(
            self.mutation, variables, self.endpoint_path
        )

        assert result.data is not None
        assert result.data["createPublicAmbassador"]["success"] is True, \
            f"Expected success but got: {result.data.get('createPublicAmbassador', {}).get('message', 'Unknown error')}"

        user = await sync_to_async(User.objects.get)(email=email)
        ambassador = await sync_to_async(Ambassador.objects.get)(user=user)
        assert ambassador.coordinates == [-33.8688, 151.2093]


@pytest.mark.django_db(transaction=True)
class TestAmbassadorInvitationCreation(AmbassadorsGraphQLTestCase):
    """Tests for create_ambassador_invitation mutation."""

    @pytest.fixture(autouse=True)
    def setup(self, db):
        """Set up test data."""
        from config.schema_spark import schema_spark
        self.roles = self.setup_default_roles()
        self.tenant = self.create_tenant(name="Invitation Test Tenant")
        # Use UUID for client user to ensure uniqueness across test runs
        unique_id = str(uuid.uuid4())[:8]
        self.client_user = self.create_user(
            username=f"client_inv_{unique_id}@test.com",
            email=f"client_inv_{unique_id}@test.com",
            role=self.roles['client']
        )
        self.create_tenanted_user(self.client_user, self.tenant)
        self.schema = schema_spark
        self.endpoint_path = "/api/v1/graphql/spark"

        self.mutation = """
            mutation CreateAmbassadorInvitation($input: CreateAmbassadorInvitationInput!) {
                createAmbassadorInvitation(input: $input) {
                    success
                    message
                    clientMutationId
                    invitation {
                        id
                        email
                        token
                        expiresAt
                        isUsed
                    }
                }
            }
        """

    @pytest.mark.asyncio
    async def test_create_invitation_success(self):
        """Test successful invitation creation."""
        unique_id = str(uuid.uuid4())[:8]
        email = f"invited_{unique_id}@test.com"

        variables = {
            "input": {
                "email": email,
                "tenantId": str(self.tenant.id),
                "clientMutationId": "test-123",
            }
        }

        result = await self._execute_mutation_authenticated(
            self.mutation, variables, self.client_user, self.endpoint_path
        )

        assert result.errors is None
        assert result.data is not None
        assert result.data["createAmbassadorInvitation"]["success"] is True
        assert "created successfully" in result.data["createAmbassadorInvitation"]["message"].lower(
        )
        assert result.data["createAmbassadorInvitation"]["invitation"]["email"] == email
        assert result.data["createAmbassadorInvitation"]["invitation"]["token"] is not None
        assert result.data["createAmbassadorInvitation"]["invitation"]["isUsed"] is False

        # Verify invitation in DB
        @sync_to_async
        def get_invitation():
            return AmbassadorInvitation.objects.select_related("invited_by", "tenant").get(
                email=email
            )
        invitation = await get_invitation()
        assert invitation.invited_by == self.client_user
        assert invitation.tenant == self.tenant
        assert invitation.is_used is False

    @pytest.mark.asyncio
    async def test_create_invitation_duplicate_email(self):
        """Test invitation creation with existing user email."""
        unique_id = str(uuid.uuid4())[:8]
        email = f"existing_inv_{unique_id}@test.com"

        # Create existing user
        await sync_to_async(self.create_user)(
            username=email,
            email=email,
            role=self.roles['ambassador']
        )

        variables = {
            "input": {
                "email": email,
                "tenantId": str(self.tenant.id),
                "clientMutationId": "test-123",
            }
        }

        result = await self._execute_mutation_authenticated(
            self.mutation, variables, self.client_user, self.endpoint_path
        )

        assert result.data is not None
        assert result.data["createAmbassadorInvitation"]["success"] is False
        assert "email" in result.data["createAmbassadorInvitation"]["message"].lower(
        )

    @pytest.mark.asyncio
    async def test_create_invitation_active_invitation_exists(self):
        """Test invitation creation when active invitation already exists."""
        unique_id = str(uuid.uuid4())[:8]
        email = f"invited_active_{unique_id}@test.com"

        # Create existing active invitation
        await sync_to_async(AmbassadorInvitation.objects.create)(
            email=email,
            token=f"existing-token-{unique_id}",
            expires_at=timezone.now() + timedelta(days=7),
            invited_by=self.client_user,
            tenant=self.tenant,
            created_by=self.client_user,
            updated_by=self.client_user,
        )

        variables = {
            "input": {
                "email": email,
                "tenantId": str(self.tenant.id),
                "clientMutationId": "test-123",
            }
        }

        result = await self._execute_mutation_authenticated(
            self.mutation, variables, self.client_user, self.endpoint_path
        )

        assert result.data is not None
        assert result.data["createAmbassadorInvitation"]["success"] is False
        assert "active invitation" in result.data["createAmbassadorInvitation"]["message"].lower(
        )

    @pytest.mark.asyncio
    async def test_create_invitation_expired_invitation_allowed(self):
        """Test that creating invitation with expired one existing is allowed."""
        unique_id = str(uuid.uuid4())[:8]
        email = f"invited_expired_{unique_id}@test.com"

        # Create expired invitation
        await sync_to_async(AmbassadorInvitation.objects.create)(
            email=email,
            token=f"expired-token-{unique_id}",
            expires_at=timezone.now() - timedelta(days=1),
            is_used=False,
            invited_by=self.client_user,
            tenant=self.tenant,
            created_by=self.client_user,
            updated_by=self.client_user,
        )

        variables = {
            "input": {
                "email": email,
                "tenantId": str(self.tenant.id),
                "clientMutationId": "test-123",
            }
        }

        result = await self._execute_mutation_authenticated(
            self.mutation, variables, self.client_user, self.endpoint_path
        )

        assert result.data is not None
        assert result.data["createAmbassadorInvitation"]["success"] is True

    @pytest.mark.asyncio
    async def test_create_invitation_invalid_tenant(self):
        """Test invitation creation with invalid tenant ID."""
        unique_id = str(uuid.uuid4())[:8]
        email = f"invited_invalid_{unique_id}@test.com"

        variables = {
            "input": {
                "email": email,
                "tenantId": "99999",  # Non-existent tenant
                "clientMutationId": "test-123",
            }
        }

        result = await self._execute_mutation_authenticated(
            self.mutation, variables, self.client_user, self.endpoint_path
        )

        assert result.data is not None
        assert result.data["createAmbassadorInvitation"]["success"] is False
        assert "tenant" in result.data["createAmbassadorInvitation"]["message"].lower(
        )


@pytest.mark.django_db(transaction=True)
class TestAcceptAmbassadorInvitation(AmbassadorsGraphQLTestCase):
    """Tests for accept_ambassador_invitation mutation."""

    @pytest.fixture(autouse=True)
    def setup(self, db):
        """Set up test data."""
        from config.schema_ambassador import schema_ambassador
        self.roles = self.setup_default_roles()
        self.tenant = self.create_tenant(name="Accept Invitation Tenant")
        # Use UUID for client user to ensure uniqueness across test runs
        unique_id = str(uuid.uuid4())[:8]
        self.client_user = self.create_user(
            username=f"client_accept_{unique_id}@test.com",
            email=f"client_accept_{unique_id}@test.com",
            role=self.roles['client']
        )
        self.schema = schema_ambassador
        self.endpoint_path = "/api/v1/graphql/ambassadors"

        self.mutation = """
            mutation AcceptAmbassadorInvitation($input: AcceptAmbassadorInvitationInput!) {
                acceptAmbassadorInvitation(input: $input) {
                    success
                    message
                    clientMutationId
                    ambassador {
                        id
                        isActive
                        address
                        coordinates
                    }
                    activationToken
                }
            }
        """

    @pytest.mark.asyncio
    async def test_accept_invitation_success(self):
        """Test successful invitation acceptance."""
        unique_id = str(uuid.uuid4())[:8]
        email = f"invited_{unique_id}@test.com"
        token = f"valid-token-{unique_id}"

        # Create invitation
        invitation = await sync_to_async(AmbassadorInvitation.objects.create)(
            email=email,
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
                "firstName": "Invited",
                "password1": "testpass123",
                "password2": "testpass123",
                "address": "456 Invited St",
                "coordinates": [34.0522, -118.2437],  # LA coordinates
                "clientMutationId": "test-123",
            }
        }

        result = await self._execute_mutation(
            self.mutation, variables, self.endpoint_path
        )

        assert result.errors is None
        assert result.data is not None
        assert result.data["acceptAmbassadorInvitation"]["success"] is True
        assert "accepted successfully" in result.data["acceptAmbassadorInvitation"]["message"].lower(
        )
        assert result.data["acceptAmbassadorInvitation"]["activationToken"] is not None

        # Verify user was created
        user = await sync_to_async(User.objects.get)(email=email)
        assert user.first_name == "Invited"
        assert user.role.id == ROLE_ID.Ambassadors
        assert user.is_active is True  # Active since invited

        # Verify TenantedUser was created
        tenanted_user_exists = await sync_to_async(
            TenantedUser.objects.filter(user=user, tenant=self.tenant).exists
        )()
        assert tenanted_user_exists is True

        # Verify ambassador was created
        ambassador = await sync_to_async(Ambassador.objects.get)(user=user)
        assert ambassador.is_active is True  # Active since invited
        assert ambassador.address == "456 Invited St"
        assert ambassador.coordinates == [34.0522, -118.2437]

        # Verify invitation was marked as used
        @sync_to_async
        def get_invitation():
            return AmbassadorInvitation.objects.select_related("ambassador").get(
                token=token
            )
        invitation = await get_invitation()
        assert invitation.is_used is True
        assert invitation.used_at is not None
        assert invitation.ambassador == ambassador

    @pytest.mark.asyncio
    async def test_accept_invitation_password_mismatch(self):
        """Test invitation acceptance with mismatched passwords."""
        unique_id = str(uuid.uuid4())[:8]
        email = f"invited2_{unique_id}@test.com"
        token = f"token-{unique_id}"

        invitation = await sync_to_async(AmbassadorInvitation.objects.create)(
            email=email,
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
                "firstName": "Invited",
                "password1": "testpass123",
                "password2": "differentpass",
                "clientMutationId": "test-123",
            }
        }

        result = await self._execute_mutation(
            self.mutation, variables, self.endpoint_path
        )

        assert result.data is not None
        assert result.data["acceptAmbassadorInvitation"]["success"] is False
        assert "password" in result.data["acceptAmbassadorInvitation"]["message"].lower(
        )

    @pytest.mark.asyncio
    async def test_accept_invitation_invalid_token(self):
        """Test invitation acceptance with invalid token."""
        unique_id = str(uuid.uuid4())[:8]
        token = f"invalid-token-{unique_id}"

        variables = {
            "input": {
                "token": token,
                "firstName": "Invited",
                "password1": "testpass123",
                "password2": "testpass123",
                "clientMutationId": "test-123",
            }
        }

        result = await self._execute_mutation(
            self.mutation, variables, self.endpoint_path
        )

        assert result.data is not None
        assert result.data["acceptAmbassadorInvitation"]["success"] is False
        assert "token" in result.data["acceptAmbassadorInvitation"]["message"].lower(
        )

    @pytest.mark.asyncio
    async def test_accept_invitation_already_used(self):
        """Test accepting an already used invitation."""
        unique_id = str(uuid.uuid4())[:8]
        email = f"used_{unique_id}@test.com"
        token = f"used-token-{unique_id}"

        invitation = await sync_to_async(AmbassadorInvitation.objects.create)(
            email=email,
            token=token,
            expires_at=timezone.now() + timedelta(days=7),
            is_used=True,
            used_at=timezone.now(),
            invited_by=self.client_user,
            tenant=self.tenant,
            created_by=self.client_user,
            updated_by=self.client_user,
        )

        variables = {
            "input": {
                "token": token,
                "firstName": "Invited",
                "password1": "testpass123",
                "password2": "testpass123",
                "clientMutationId": "test-123",
            }
        }

        result = await self._execute_mutation(
            self.mutation, variables, self.endpoint_path
        )

        assert result.data is not None
        assert result.data["acceptAmbassadorInvitation"]["success"] is False
        assert "already been used" in result.data["acceptAmbassadorInvitation"]["message"].lower(
        )

    @pytest.mark.asyncio
    async def test_accept_invitation_expired(self):
        """Test accepting an expired invitation."""
        unique_id = str(uuid.uuid4())[:8]
        email = f"expired_{unique_id}@test.com"
        token = f"expired-token-{unique_id}"

        invitation = await sync_to_async(AmbassadorInvitation.objects.create)(
            email=email,
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
                "firstName": "Invited",
                "password1": "testpass123",
                "password2": "testpass123",
                "clientMutationId": "test-123",
            }
        }

        result = await self._execute_mutation(
            self.mutation, variables, self.endpoint_path
        )

        assert result.data is not None
        assert result.data["acceptAmbassadorInvitation"]["success"] is False
        assert "expired" in result.data["acceptAmbassadorInvitation"]["message"].lower(
        )


@pytest.mark.django_db(transaction=True)
class TestApproveAmbassador(AmbassadorsGraphQLTestCase):
    """Tests for approve_ambassador mutation."""

    @pytest.fixture(autouse=True)
    def setup(self, db):
        """Set up test data."""
        from config.schema_spark import schema_spark
        self.roles = self.setup_default_roles()
        self.tenant = self.create_tenant(name="Approve Ambassador Tenant")
        # Use UUID for all users to ensure uniqueness across test runs
        unique_id = str(uuid.uuid4())[:8]
        self.client_user = self.create_user(
            username=f"client_approve_{unique_id}@test.com",
            email=f"client_approve_{unique_id}@test.com",
            role=self.roles['client']
        )
        self.create_tenanted_user(self.client_user, self.tenant)

        self.spark_admin_user = self.create_user(
            username=f"sparkadmin_approve_{unique_id}@test.com",
            email=f"sparkadmin_approve_{unique_id}@test.com",
            role=self.roles['spark_admin']
        )
        self.create_tenanted_user(self.spark_admin_user, self.tenant)

        # Create inactive ambassador
        self.ambassador_user = self.create_user(
            username=f"ambassador_approve_{unique_id}@test.com",
            email=f"ambassador_approve_{unique_id}@test.com",
            role=self.roles['ambassador'],
            is_active=True
        )
        self.ambassador = self.create_ambassador(
            user=self.ambassador_user,
            is_active=False,  # Inactive, needs approval
        )

        self.schema = schema_spark
        self.endpoint_path = "/api/v1/graphql/spark"

        self.mutation = """
            mutation ApproveAmbassador($input: ApproveAmbassadorInput!) {
                approveAmbassador(input: $input) {
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
    async def test_approve_ambassador_success_by_client(self):
        """Test successful ambassador approval by client."""
        variables = {
            "input": {
                "ambassadorId": str(self.ambassador.id),
                "tenantId": str(self.tenant.id),
                "clientMutationId": "test-123",
            }
        }

        result = await self._execute_mutation_authenticated(
            self.mutation, variables, self.client_user, self.endpoint_path
        )

        assert result.errors is None
        assert result.data is not None
        assert result.data["approveAmbassador"]["success"] is True
        assert "approved successfully" in result.data["approveAmbassador"]["message"].lower(
        )

        # Verify ambassador is now active
        ambassador = await sync_to_async(Ambassador.objects.get)(pk=self.ambassador.id)
        assert ambassador.is_active is True

        # Verify TenantedUser was created
        tenanted_user_exists = await sync_to_async(
            TenantedUser.objects.filter(
                user=self.ambassador_user, tenant=self.tenant
            ).exists
        )()
        assert tenanted_user_exists is True

    @pytest.mark.asyncio
    async def test_approve_ambassador_success_by_spark_admin(self):
        """Test successful ambassador approval by spark admin."""
        variables = {
            "input": {
                "ambassadorId": str(self.ambassador.id),
                "tenantId": str(self.tenant.id),
                "clientMutationId": "test-123",
            }
        }

        result = await self._execute_mutation_authenticated(
            self.mutation, variables, self.spark_admin_user, self.endpoint_path
        )

        assert result.data is not None
        assert result.data["approveAmbassador"]["success"] is True

    @pytest.mark.asyncio
    async def test_approve_ambassador_without_tenant(self):
        """Test ambassador approval without specifying tenant."""
        variables = {
            "input": {
                "ambassadorId": str(self.ambassador.id),
                "clientMutationId": "test-123",
            }
        }

        result = await self._execute_mutation_authenticated(
            self.mutation, variables, self.client_user, self.endpoint_path
        )

        assert result.data is not None
        assert result.data["approveAmbassador"]["success"] is True

        # Verify ambassador is active
        ambassador = await sync_to_async(Ambassador.objects.get)(pk=self.ambassador.id)
        assert ambassador.is_active is True

    @pytest.mark.asyncio
    async def test_approve_ambassador_unauthorized(self):
        """Test ambassador approval by unauthorized user (ambassador)."""
        unique_id = str(uuid.uuid4())[:8]
        ambassador_user2 = await sync_to_async(self.create_user)(
            username=f"ambassador2_approve_{unique_id}@test.com",
            email=f"ambassador2_approve_{unique_id}@test.com",
            role=self.roles['ambassador']
        )

        variables = {
            "input": {
                "ambassadorId": str(self.ambassador.id),
                "clientMutationId": "test-123",
            }
        }

        result = await self._execute_mutation_authenticated(
            self.mutation, variables, ambassador_user2, self.endpoint_path
        )

        assert result.data is not None
        assert result.data["approveAmbassador"]["success"] is False
        assert "permission" in result.data["approveAmbassador"]["message"].lower(
        )

    @pytest.mark.asyncio
    async def test_approve_ambassador_not_found(self):
        """Test approval of non-existent ambassador."""
        variables = {
            "input": {
                "ambassadorId": "99999",
                "clientMutationId": "test-123",
            }
        }

        result = await self._execute_mutation_authenticated(
            self.mutation, variables, self.client_user, self.endpoint_path
        )

        assert result.data is not None
        assert result.data["approveAmbassador"]["success"] is False
        assert "not found" in result.data["approveAmbassador"]["message"].lower(
        )

    @pytest.mark.asyncio
    async def test_approve_ambassador_invalid_tenant(self):
        """Test approval with invalid tenant ID."""
        variables = {
            "input": {
                "ambassadorId": str(self.ambassador.id),
                "tenantId": "99999",
                "clientMutationId": "test-123",
            }
        }

        result = await self._execute_mutation_authenticated(
            self.mutation, variables, self.client_user, self.endpoint_path
        )

        assert result.data is not None
        assert result.data["approveAmbassador"]["success"] is False
        assert "tenant" in result.data["approveAmbassador"]["message"].lower()

    @pytest.mark.asyncio
    async def test_approve_ambassador_existing_tenanted_user(self):
        """Test approval when TenantedUser already exists."""
        # Create existing TenantedUser
        await sync_to_async(self.create_tenanted_user)(
            self.ambassador_user, self.tenant
        )

        variables = {
            "input": {
                "ambassadorId": str(self.ambassador.id),
                "tenantId": str(self.tenant.id),
                "clientMutationId": "test-123",
            }
        }

        result = await self._execute_mutation_authenticated(
            self.mutation, variables, self.client_user, self.endpoint_path
        )

        assert result.data is not None
        assert result.data["approveAmbassador"]["success"] is True

        # Verify only one TenantedUser exists
        tenanted_user_count = await sync_to_async(
            TenantedUser.objects.filter(
                user=self.ambassador_user, tenant=self.tenant
            ).count
        )()
        assert tenanted_user_count == 1
