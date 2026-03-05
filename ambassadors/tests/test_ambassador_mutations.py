"""
Tests for ambassador mutations.

This module tests:
- create_public_ambassador (public mutation)
- create_ambassador_invitation (authenticated)
- accept_ambassador_invitation (public with token)
- approve_ambassador (authenticated)
- update_ambassador (authenticated)
- delete_invitation (authenticated)
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
        email = f"mismatch_{unique_id}@test.com"

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

    @pytest.mark.asyncio
    async def test_create_public_ambassador_duplicate_email(self):
        """Test public ambassador creation with existing email."""
        unique_id = str(uuid.uuid4())[:8]
        email = f"duplicate_{unique_id}@test.com"

        # Create existing user
        await sync_to_async(self.create_user)(
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
        email = f"coord_{unique_id}@test.com"

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

        # Verify coordinates were saved
        user = await sync_to_async(User.objects.get)(email=email)
        ambassador = await sync_to_async(Ambassador.objects.get)(user=user)
        assert ambassador.coordinates == [40.7128, -74.0060]


@pytest.mark.django_db(transaction=True)
class TestAmbassadorInvitationCreation(AmbassadorsGraphQLTestCase):
    """Tests for create_ambassador_invitation mutation."""

    @pytest.fixture(autouse=True)
    def setup(self, db):
        """Set up test data."""
        from config.schema_spark import schema_spark
        self.roles = self.setup_default_roles()
        self.tenant = self.create_tenant(name="Invitation Tenant")
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

    @pytest.mark.asyncio
    async def test_accept_invitation_password_mismatch(self):
        """Test invitation acceptance with mismatched passwords."""
        unique_id = str(uuid.uuid4())[:8]
        email = f"invited_pwd_{unique_id}@test.com"
        token = f"token-pwd-{unique_id}"

        # Create invitation
        await sync_to_async(AmbassadorInvitation.objects.create)(
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
        variables = {
            "input": {
                "token": "invalid-token-12345",
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
        """Test invitation acceptance when invitation is already used."""
        unique_id = str(uuid.uuid4())[:8]
        email = f"invited_used_{unique_id}@test.com"
        token = f"token-used-{unique_id}"

        # Create used invitation
        await sync_to_async(AmbassadorInvitation.objects.create)(
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
        """Test invitation acceptance when invitation is expired."""
        unique_id = str(uuid.uuid4())[:8]
        email = f"invited_expired_{unique_id}@test.com"
        token = f"token-expired-{unique_id}"

        # Create expired invitation
        await sync_to_async(AmbassadorInvitation.objects.create)(
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
        self.tenant = self.create_tenant(name="Approve Tenant")
        # Use UUID for users to ensure uniqueness across test runs
        unique_id = str(uuid.uuid4())[:8]
        self.client_user = self.create_user(
            username=f"client_approve_{unique_id}@test.com",
            email=f"client_approve_{unique_id}@test.com",
            role=self.roles['client']
        )
        self.create_tenanted_user(self.client_user, self.tenant)

        unique_id2 = str(uuid.uuid4())[:8]
        self.spark_admin_user = self.create_user(
            username=f"spark_admin_{unique_id2}@test.com",
            email=f"spark_admin_{unique_id2}@test.com",
            role=self.roles['spark_admin']
        )

        unique_id3 = str(uuid.uuid4())[:8]
        self.ambassador_user = self.create_user(
            username=f"ambassador_approve_{unique_id3}@test.com",
            email=f"ambassador_approve_{unique_id3}@test.com",
            role=self.roles['ambassador'],
            is_active=False,
        )
        self.ambassador = self.create_ambassador(
            self.ambassador_user,
            is_active=False,  # Inactive until approved
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
                "clientMutationId": "test-123",
            }
        }

        result = await self._execute_mutation_authenticated(
            self.mutation, variables, self.client_user, self.endpoint_path
        )

        assert result.data is not None
        assert result.data["approveAmbassador"]["success"] is True
        assert "approved successfully" in result.data["approveAmbassador"]["message"].lower(
        )

        # Verify ambassador is now active
        ambassador = await sync_to_async(Ambassador.objects.get)(pk=self.ambassador.id)
        assert ambassador.is_active is True

    @pytest.mark.asyncio
    async def test_approve_ambassador_success_by_spark_admin(self):
        """Test successful ambassador approval by spark admin."""
        variables = {
            "input": {
                "ambassadorId": str(self.ambassador.id),
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

        # Permission class rejects at GraphQL level, so data is None and errors contain the rejection
        assert result.data is None
        assert result.errors is not None
        assert len(result.errors) > 0
        assert "permission" in str(result.errors[0].message).lower(
        ) or "client or spark admin" in str(result.errors[0].message).lower()

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


@pytest.mark.django_db(transaction=True)
class TestUpdateAmbassador(AmbassadorsGraphQLTestCase):
    """Tests for update_ambassador mutation."""

    @pytest.fixture(autouse=True)
    def setup(self, db):
        """Set up test data."""
        from config.schema_spark import schema_spark
        self.roles = self.setup_default_roles()
        self.tenant = self.create_tenant(name="Update Tenant")
        # Use UUID for users to ensure uniqueness across test runs
        unique_id = str(uuid.uuid4())[:8]
        self.client_user = self.create_user(
            username=f"client_update_{unique_id}@test.com",
            email=f"client_update_{unique_id}@test.com",
            role=self.roles['client']
        )
        self.create_tenanted_user(self.client_user, self.tenant)

        unique_id2 = str(uuid.uuid4())[:8]
        self.spark_admin_user = self.create_user(
            username=f"spark_update_{unique_id2}@test.com",
            email=f"spark_update_{unique_id2}@test.com",
            role=self.roles['spark_admin']
        )

        unique_id3 = str(uuid.uuid4())[:8]
        self.ambassador_user = self.create_user(
            username=f"ambassador_update_{unique_id3}@test.com",
            email=f"ambassador_update_{unique_id3}@test.com",
            role=self.roles['ambassador'],
        )
        self.ambassador = self.create_ambassador(
            self.ambassador_user,
            address="Original Address",
            coordinates=[40.7128, -74.0060],
            is_active=True,
        )

        self.schema = schema_spark
        self.endpoint_path = "/api/v1/graphql/spark"

        self.mutation = """
            mutation UpdateAmbassador($input: UpdateAmbassadorInput!) {
                updateAmbassador(input: $input) {
                    success
                    message
                    clientMutationId
                    ambassador {
                        id
                        isActive
                        address
                        coordinates
                    }
                }
            }
        """

    @pytest.mark.asyncio
    async def test_update_ambassador_success_by_client(self):
        """Test successful ambassador update by client."""
        variables = {
            "input": {
                "ambassadorId": str(self.ambassador.id),
                "address": "Updated Address",
                "coordinates": [34.0522, -118.2437],
                "clientMutationId": "test-123",
            }
        }

        result = await self._execute_mutation_authenticated(
            self.mutation, variables, self.client_user, self.endpoint_path
        )

        assert result.data is not None
        assert result.data["updateAmbassador"]["success"] is True
        assert "updated successfully" in result.data["updateAmbassador"]["message"].lower(
        )
        assert result.data["updateAmbassador"]["ambassador"]["address"] == "Updated Address"
        assert result.data["updateAmbassador"]["ambassador"]["coordinates"] == [
            34.0522, -118.2437]

        # Verify changes in DB
        ambassador = await sync_to_async(Ambassador.objects.get)(pk=self.ambassador.id)
        assert ambassador.address == "Updated Address"
        assert ambassador.coordinates == [34.0522, -118.2437]

    @pytest.mark.asyncio
    async def test_update_ambassador_success_by_spark_admin(self):
        """Test successful ambassador update by spark admin."""
        variables = {
            "input": {
                "ambassadorId": str(self.ambassador.id),
                "address": "Spark Updated Address",
                "clientMutationId": "test-123",
            }
        }

        result = await self._execute_mutation_authenticated(
            self.mutation, variables, self.spark_admin_user, self.endpoint_path
        )

        assert result.data is not None
        assert result.data["updateAmbassador"]["success"] is True

        # Verify changes in DB
        ambassador = await sync_to_async(Ambassador.objects.get)(pk=self.ambassador.id)
        assert ambassador.address == "Spark Updated Address"

    @pytest.mark.asyncio
    async def test_update_ambassador_partial_update(self):
        """Test updating only some fields."""
        variables = {
            "input": {
                "ambassadorId": str(self.ambassador.id),
                "address": "Partial Update Address",
                # coordinates and is_active not provided
                "clientMutationId": "test-123",
            }
        }

        result = await self._execute_mutation_authenticated(
            self.mutation, variables, self.client_user, self.endpoint_path
        )

        assert result.data is not None
        assert result.data["updateAmbassador"]["success"] is True

        # Verify only address changed, coordinates and is_active unchanged
        ambassador = await sync_to_async(Ambassador.objects.get)(pk=self.ambassador.id)
        assert ambassador.address == "Partial Update Address"
        assert ambassador.coordinates == [40.7128, -74.0060]  # Original value
        assert ambassador.is_active is True  # Original value

    @pytest.mark.asyncio
    async def test_update_ambassador_is_active(self):
        """Test updating is_active status."""
        variables = {
            "input": {
                "ambassadorId": str(self.ambassador.id),
                "isActive": False,
                "clientMutationId": "test-123",
            }
        }

        result = await self._execute_mutation_authenticated(
            self.mutation, variables, self.client_user, self.endpoint_path
        )

        assert result.data is not None
        assert result.data["updateAmbassador"]["success"] is True
        assert result.data["updateAmbassador"]["ambassador"]["isActive"] is False

        # Verify change in DB
        ambassador = await sync_to_async(Ambassador.objects.get)(pk=self.ambassador.id)
        assert ambassador.is_active is False

    @pytest.mark.asyncio
    async def test_update_ambassador_unauthorized(self):
        """Test ambassador update by unauthorized user (ambassador)."""
        unique_id = str(uuid.uuid4())[:8]
        ambassador_user2 = await sync_to_async(self.create_user)(
            username=f"ambassador2_update_{unique_id}@test.com",
            email=f"ambassador2_update_{unique_id}@test.com",
            role=self.roles['ambassador']
        )

        variables = {
            "input": {
                "ambassadorId": str(self.ambassador.id),
                "address": "Unauthorized Update",
                "clientMutationId": "test-123",
            }
        }

        result = await self._execute_mutation_authenticated(
            self.mutation, variables, ambassador_user2, self.endpoint_path
        )

        # Permission class rejects at GraphQL level
        assert result.data is None
        assert result.errors is not None
        assert len(result.errors) > 0

    @pytest.mark.asyncio
    async def test_update_ambassador_not_found(self):
        """Test update of non-existent ambassador."""
        variables = {
            "input": {
                "ambassadorId": "99999",
                "address": "New Address",
                "clientMutationId": "test-123",
            }
        }

        result = await self._execute_mutation_authenticated(
            self.mutation, variables, self.client_user, self.endpoint_path
        )

        assert result.data is not None
        assert result.data["updateAmbassador"]["success"] is False
        assert "not found" in result.data["updateAmbassador"]["message"].lower(
        )


@pytest.mark.django_db(transaction=True)
class TestDisableAmbassador(AmbassadorsGraphQLTestCase):
    """Tests for disable_ambassador mutation."""

    @pytest.fixture(autouse=True)
    def setup(self, db):
        """Set up test data."""
        from config.schema_spark import schema_spark

        self.roles = self.setup_default_roles()
        self.tenant = self.create_tenant(name="Disable Tenant")

        unique_id = str(uuid.uuid4())[:8]
        self.client_user = self.create_user(
            username=f"client_disable_{unique_id}@test.com",
            email=f"client_disable_{unique_id}@test.com",
            role=self.roles["client"],
        )
        self.create_tenanted_user(self.client_user, self.tenant)

        unique_id2 = str(uuid.uuid4())[:8]
        self.spark_admin_user = self.create_user(
            username=f"spark_disable_{unique_id2}@test.com",
            email=f"spark_disable_{unique_id2}@test.com",
            role=self.roles["spark_admin"],
        )

        unique_id3 = str(uuid.uuid4())[:8]
        self.ambassador_user = self.create_user(
            username=f"ambassador_disable_{unique_id3}@test.com",
            email=f"ambassador_disable_{unique_id3}@test.com",
            role=self.roles["ambassador"],
            is_active=True,
        )
        self.ambassador = self.create_ambassador(
            self.ambassador_user,
            is_active=True,
        )

        self.schema = schema_spark
        self.endpoint_path = "/api/v1/graphql/spark"
        self.mutation = """
            mutation DisableAmbassador($input: DisableAmbassadorInput!) {
                disableAmbassador(input: $input) {
                    success
                    message
                    ambassador {
                        id
                        isActive
                    }
                }
            }
        """

    @pytest.mark.asyncio
    async def test_disable_ambassador_success(self):
        """Test disabling ambassador and associated user."""
        variables = {"input": {"ambassadorId": str(self.ambassador.id)}}

        result = await self._execute_mutation_authenticated(
            self.mutation, variables, self.client_user, self.endpoint_path
        )

        assert result.errors is None
        assert result.data is not None
        assert result.data["disableAmbassador"]["success"] is True
        assert result.data["disableAmbassador"]["ambassador"]["isActive"] is False

        ambassador = await sync_to_async(Ambassador.objects.select_related("user").get)(
            pk=self.ambassador.id
        )
        assert ambassador.is_active is False
        assert ambassador.user.is_active is False

    @pytest.mark.asyncio
    async def test_disable_ambassador_success_by_spark_admin(self):
        """Test disabling by spark admin user."""
        variables = {"input": {"ambassadorId": str(self.ambassador.id)}}

        result = await self._execute_mutation_authenticated(
            self.mutation, variables, self.spark_admin_user, self.endpoint_path
        )

        assert result.errors is None
        assert result.data is not None
        assert result.data["disableAmbassador"]["success"] is True

    @pytest.mark.asyncio
    async def test_disable_ambassador_success_by_same_ambassador(self):
        """Test ambassador can disable their own account."""
        variables = {"input": {"ambassadorId": str(self.ambassador.id)}}

        result = await self._execute_mutation_authenticated(
            self.mutation, variables, self.ambassador_user, self.endpoint_path
        )

        assert result.errors is None
        assert result.data is not None
        assert result.data["disableAmbassador"]["success"] is True

        ambassador = await sync_to_async(Ambassador.objects.select_related("user").get)(
            pk=self.ambassador.id
        )
        assert ambassador.is_active is False
        assert ambassador.user.is_active is False

    @pytest.mark.asyncio
    async def test_disable_ambassador_not_found(self):
        """Test disabling a non-existent ambassador."""
        variables = {"input": {"ambassadorId": "99999"}}

        result = await self._execute_mutation_authenticated(
            self.mutation, variables, self.client_user, self.endpoint_path
        )

        assert result.errors is None
        assert result.data is not None
        assert result.data["disableAmbassador"]["success"] is False
        assert "not found" in result.data["disableAmbassador"]["message"].lower()

    @pytest.mark.asyncio
    async def test_disable_ambassador_other_ambassador_forbidden(self):
        """Test ambassador cannot disable another ambassador."""
        unique_id = str(uuid.uuid4())[:8]
        unauthorized_user = await sync_to_async(self.create_user)(
            username=f"ambassador_disable_unauth_{unique_id}@test.com",
            email=f"ambassador_disable_unauth_{unique_id}@test.com",
            role=self.roles["ambassador"],
        )
        other_ambassador = await sync_to_async(self.create_ambassador)(
            unauthorized_user, is_active=True
        )

        variables = {"input": {"ambassadorId": str(other_ambassador.id)}}
        result = await self._execute_mutation_authenticated(
            self.mutation, variables, self.ambassador_user, self.endpoint_path
        )

        assert result.errors is None
        assert result.data is not None
        assert result.data["disableAmbassador"]["success"] is False
        assert "permission" in result.data["disableAmbassador"]["message"].lower()


@pytest.mark.django_db(transaction=True)
class TestDisableAmbassadorMobile(AmbassadorsGraphQLTestCase):
    """Tests for disable_ambassador_mobile mutation."""

    @pytest.fixture(autouse=True)
    def setup(self, db):
        from config.schema_mobile import schema_mobile

        self.roles = self.setup_default_roles()
        self.tenant = self.create_tenant(name="Disable Mobile Tenant")

        unique_id = str(uuid.uuid4())[:8]
        self.ambassador_user = self.create_user(
            username=f"ambassador_disable_mobile_{unique_id}@test.com",
            email=f"ambassador_disable_mobile_{unique_id}@test.com",
            role=self.roles["ambassador"],
            is_active=True,
        )
        self.create_tenanted_user(self.ambassador_user, self.tenant)
        self.ambassador = self.create_ambassador(self.ambassador_user, is_active=True)

        unique_id2 = str(uuid.uuid4())[:8]
        self.client_user = self.create_user(
            username=f"client_disable_mobile_{unique_id2}@test.com",
            email=f"client_disable_mobile_{unique_id2}@test.com",
            role=self.roles["client"],
        )
        self.create_tenanted_user(self.client_user, self.tenant)

        self.schema = schema_mobile
        self.endpoint_path = "/api/v1/graphql/mobile"
        self.mutation = """
            mutation DisableAmbassadorMobile {
                disableAmbassadorMobile {
                    success
                    message
                    ambassador {
                        id
                        isActive
                    }
                }
            }
        """

    @pytest.mark.asyncio
    async def test_disable_ambassador_mobile_success(self):
        result = await self._execute_mutation_authenticated(
            self.mutation, {}, self.ambassador_user, self.endpoint_path
        )

        assert result.errors is None
        assert result.data is not None
        assert result.data["disableAmbassadorMobile"]["success"] is True
        assert result.data["disableAmbassadorMobile"]["ambassador"]["isActive"] is False

        ambassador = await sync_to_async(Ambassador.objects.select_related("user").get)(
            pk=self.ambassador.id
        )
        assert ambassador.is_active is False
        assert ambassador.user.is_active is False

    @pytest.mark.asyncio
    async def test_disable_ambassador_mobile_only_ambassador(self):
        result = await self._execute_mutation_authenticated(
            self.mutation, {}, self.client_user, self.endpoint_path
        )

        assert result.errors is None
        assert result.data is not None
        assert result.data["disableAmbassadorMobile"]["success"] is False
        assert "only ambassadors" in result.data["disableAmbassadorMobile"]["message"].lower()


@pytest.mark.django_db(transaction=True)
class TestDeleteInvitation(AmbassadorsGraphQLTestCase):
    """Tests for delete_invitation mutation."""

    @pytest.fixture(autouse=True)
    def setup(self, db):
        """Set up test data."""
        from config.schema_spark import schema_spark
        self.roles = self.setup_default_roles()
        self.tenant = self.create_tenant(name="Delete Invitation Tenant")
        # Use UUID for users to ensure uniqueness across test runs
        unique_id = str(uuid.uuid4())[:8]
        self.client_user = self.create_user(
            username=f"client_delete_{unique_id}@test.com",
            email=f"client_delete_{unique_id}@test.com",
            role=self.roles['client']
        )
        self.create_tenanted_user(self.client_user, self.tenant)

        unique_id2 = str(uuid.uuid4())[:8]
        self.spark_admin_user = self.create_user(
            username=f"spark_delete_{unique_id2}@test.com",
            email=f"spark_delete_{unique_id2}@test.com",
            role=self.roles['spark_admin']
        )

        # Create test invitations
        unique_id3 = str(uuid.uuid4())[:8]
        self.invitation = AmbassadorInvitation.objects.create(
            email=f"invite_delete_{unique_id3}@test.com",
            token=f"token-delete-{unique_id3}",
            expires_at=timezone.now() + timedelta(days=7),
            invited_by=self.client_user,
            tenant=self.tenant,
            created_by=self.client_user,
            updated_by=self.client_user,
        )

        unique_id4 = str(uuid.uuid4())[:8]
        self.used_invitation = AmbassadorInvitation.objects.create(
            email=f"invite_used_{unique_id4}@test.com",
            token=f"token-used-{unique_id4}",
            expires_at=timezone.now() + timedelta(days=7),
            is_used=True,
            used_at=timezone.now(),
            invited_by=self.client_user,
            tenant=self.tenant,
            created_by=self.client_user,
            updated_by=self.client_user,
        )

        self.schema = schema_spark
        self.endpoint_path = "/api/v1/graphql/spark"

        self.mutation = """
            mutation DeleteInvitation($input: DeleteInvitationInput!) {
                deleteInvitation(input: $input) {
                    success
                    message
                    clientMutationId
                }
            }
        """

    @pytest.mark.asyncio
    async def test_delete_invitation_success_by_client(self):
        """Test successful invitation deletion by client."""
        variables = {
            "input": {
                "invitationId": str(self.invitation.id),
                "clientMutationId": "test-123",
            }
        }

        result = await self._execute_mutation_authenticated(
            self.mutation, variables, self.client_user, self.endpoint_path
        )

        assert result.data is not None
        assert result.data["deleteInvitation"]["success"] is True
        assert "deleted successfully" in result.data["deleteInvitation"]["message"].lower(
        )

        # Verify invitation was deleted
        invitation_exists = await sync_to_async(
            AmbassadorInvitation.objects.filter(pk=self.invitation.id).exists
        )()
        assert invitation_exists is False

    @pytest.mark.asyncio
    async def test_delete_invitation_success_by_spark_admin(self):
        """Test successful invitation deletion by spark admin."""
        variables = {
            "input": {
                "invitationId": str(self.invitation.id),
                "clientMutationId": "test-123",
            }
        }

        result = await self._execute_mutation_authenticated(
            self.mutation, variables, self.spark_admin_user, self.endpoint_path
        )

        assert result.data is not None
        assert result.data["deleteInvitation"]["success"] is True

    @pytest.mark.asyncio
    async def test_delete_invitation_used_invitation(self):
        """Test deletion of used invitation (should still work)."""
        variables = {
            "input": {
                "invitationId": str(self.used_invitation.id),
                "clientMutationId": "test-123",
            }
        }

        result = await self._execute_mutation_authenticated(
            self.mutation, variables, self.client_user, self.endpoint_path
        )

        assert result.data is not None
        assert result.data["deleteInvitation"]["success"] is True

        # Verify invitation was deleted even though it was used
        invitation_exists = await sync_to_async(
            AmbassadorInvitation.objects.filter(
                pk=self.used_invitation.id).exists
        )()
        assert invitation_exists is False

    @pytest.mark.asyncio
    async def test_delete_invitation_unauthorized(self):
        """Test invitation deletion by unauthorized user (ambassador)."""
        unique_id = str(uuid.uuid4())[:8]
        ambassador_user = await sync_to_async(self.create_user)(
            username=f"ambassador_delete_{unique_id}@test.com",
            email=f"ambassador_delete_{unique_id}@test.com",
            role=self.roles['ambassador']
        )

        variables = {
            "input": {
                "invitationId": str(self.invitation.id),
                "clientMutationId": "test-123",
            }
        }

        result = await self._execute_mutation_authenticated(
            self.mutation, variables, ambassador_user, self.endpoint_path
        )

        # Permission class rejects at GraphQL level
        assert result.data is None
        assert result.errors is not None
        assert len(result.errors) > 0

    @pytest.mark.asyncio
    async def test_delete_invitation_not_found(self):
        """Test deletion of non-existent invitation."""
        variables = {
            "input": {
                "invitationId": "99999",
                "clientMutationId": "test-123",
            }
        }

        result = await self._execute_mutation_authenticated(
            self.mutation, variables, self.client_user, self.endpoint_path
        )

        assert result.data is not None
        assert result.data["deleteInvitation"]["success"] is False
        assert "not found" in result.data["deleteInvitation"]["message"].lower(
        )
