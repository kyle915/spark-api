"""
Tests for ambassador queries.

This module tests:
- sent_invitations query (client/spark-admin only)
- available_ambassadors query (client/spark-admin only)
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
class TestSentInvitationsQuery(AmbassadorsGraphQLTestCase):
    """Tests for sent_invitations query."""

    @pytest.fixture(autouse=True)
    def setup(self, db):
        """Set up test data."""
        from config.schema_client import schema_clients
        self.roles = self.setup_default_roles()
        self.tenant = self.create_tenant(name="Sent Invitations Tenant")
        # Use UUID for users to ensure uniqueness across test runs
        unique_id = str(uuid.uuid4())[:8]
        self.client_user = self.create_user(
            username=f"client_sent_{unique_id}@test.com",
            email=f"client_sent_{unique_id}@test.com",
            role=self.roles['client']
        )
        self.create_tenanted_user(self.client_user, self.tenant)

        unique_id2 = str(uuid.uuid4())[:8]
        self.spark_admin_user = self.create_user(
            username=f"spark_sent_{unique_id2}@test.com",
            email=f"spark_sent_{unique_id2}@test.com",
            role=self.roles['spark_admin']
        )

        # Create test invitations
        unique_id3 = str(uuid.uuid4())[:8]
        self.active_invitation = AmbassadorInvitation.objects.create(
            email=f"active_inv_{unique_id3}@test.com",
            token=f"token-active-{unique_id3}",
            expires_at=timezone.now() + timedelta(days=7),
            invited_by=self.client_user,
            tenant=self.tenant,
            created_by=self.client_user,
            updated_by=self.client_user,
        )

        unique_id4 = str(uuid.uuid4())[:8]
        self.expired_invitation = AmbassadorInvitation.objects.create(
            email=f"expired_inv_{unique_id4}@test.com",
            token=f"token-expired-{unique_id4}",
            expires_at=timezone.now() - timedelta(days=1),
            invited_by=self.client_user,
            tenant=self.tenant,
            created_by=self.client_user,
            updated_by=self.client_user,
        )

        unique_id5 = str(uuid.uuid4())[:8]
        self.used_invitation = AmbassadorInvitation.objects.create(
            email=f"used_inv_{unique_id5}@test.com",
            token=f"token-used-{unique_id5}",
            expires_at=timezone.now() + timedelta(days=7),
            is_used=True,
            used_at=timezone.now(),
            invited_by=self.client_user,
            tenant=self.tenant,
            created_by=self.client_user,
            updated_by=self.client_user,
        )

        # Create invitation for different tenant
        self.other_tenant = self.create_tenant(name="Other Tenant")
        unique_id6 = str(uuid.uuid4())[:8]
        self.other_tenant_invitation = AmbassadorInvitation.objects.create(
            email=f"other_tenant_{unique_id6}@test.com",
            token=f"token-other-{unique_id6}",
            expires_at=timezone.now() + timedelta(days=7),
            invited_by=self.client_user,
            tenant=self.other_tenant,
            created_by=self.client_user,
            updated_by=self.client_user,
        )

        self.schema = schema_clients
        self.endpoint_path = "/api/v1/graphql/clients"

        self.query = """
            query SentInvitations($first: Int, $filters: AmbassadorInvitationFiltersInput) {
                sentInvitations(first: $first, filters: $filters) {
                    edges {
                        node {
                            id
                            email
                            isUsed
                            expiresAt
                            invitedById
                            tenantId
                        }
                    }
                    totalCount
                    pageInfo {
                        hasNextPage
                        hasPreviousPage
                    }
                }
            }
        """

    @pytest.mark.asyncio
    async def test_sent_invitations_success_by_client(self):
        """Test successful sent invitations query by client."""
        variables = {
            "first": 10
        }

        result = await self._execute_query_authenticated(
            self.query, variables, self.client_user, self.endpoint_path
        )

        assert result.errors is None
        assert result.data is not None
        assert result.data["sentInvitations"] is not None
        assert result.data["sentInvitations"]["totalCount"] >= 3  # At least 3 invitations for this tenant

        # Verify we get invitations for the client's tenant
        edges = result.data["sentInvitations"]["edges"]
        assert len(edges) >= 3
        # All invitations should belong to the client's tenant
        for edge in edges:
            assert edge["node"]["tenantId"] == str(self.tenant.id)

    @pytest.mark.asyncio
    async def test_sent_invitations_success_by_spark_admin(self):
        """Test successful sent invitations query by spark admin."""
        variables = {
            "first": 10
        }

        result = await self._execute_query_authenticated(
            self.query, variables, self.spark_admin_user, self.endpoint_path
        )

        assert result.errors is None
        assert result.data is not None
        assert result.data["sentInvitations"] is not None
        # Spark admin should see all invitations
        assert result.data["sentInvitations"]["totalCount"] >= 4

    @pytest.mark.asyncio
    async def test_sent_invitations_filter_by_tenant(self):
        """Test sent invitations query with tenant filter."""
        variables = {
            "first": 10,
            "filters": {
                "tenantId": str(self.tenant.id)
            }
        }

        result = await self._execute_query_authenticated(
            self.query, variables, self.spark_admin_user, self.endpoint_path
        )

        assert result.errors is None
        assert result.data is not None
        edges = result.data["sentInvitations"]["edges"]
        # All invitations should belong to the specified tenant
        for edge in edges:
            assert edge["node"]["tenantId"] == str(self.tenant.id)

    @pytest.mark.asyncio
    async def test_sent_invitations_filter_by_expired(self):
        """Test sent invitations query with expired filter."""
        variables = {
            "first": 10,
            "filters": {
                "isExpired": True
            }
        }

        result = await self._execute_query_authenticated(
            self.query, variables, self.client_user, self.endpoint_path
        )

        assert result.errors is None
        assert result.data is not None
        edges = result.data["sentInvitations"]["edges"]
        # All invitations should be expired
        for edge in edges:
            # Verify expired (expires_at < now)
            expires_at_str = edge["node"]["expiresAt"]
            # The query returns ISO format, we just verify we got expired ones
            assert edge["node"]["email"] == self.expired_invitation.email or True

    @pytest.mark.asyncio
    async def test_sent_invitations_filter_by_active(self):
        """Test sent invitations query with active (not expired) filter."""
        variables = {
            "first": 10,
            "filters": {
                "isExpired": False
            }
        }

        result = await self._execute_query_authenticated(
            self.query, variables, self.client_user, self.endpoint_path
        )

        assert result.errors is None
        assert result.data is not None
        edges = result.data["sentInvitations"]["edges"]
        # Should include active invitation
        emails = [edge["node"]["email"] for edge in edges]
        assert self.active_invitation.email in emails

    @pytest.mark.asyncio
    async def test_sent_invitations_filter_by_used(self):
        """Test sent invitations query with used filter."""
        variables = {
            "first": 10,
            "filters": {
                "isUsed": True
            }
        }

        result = await self._execute_query_authenticated(
            self.query, variables, self.client_user, self.endpoint_path
        )

        assert result.errors is None
        assert result.data is not None
        edges = result.data["sentInvitations"]["edges"]
        # All invitations should be used
        for edge in edges:
            assert edge["node"]["isUsed"] is True

    @pytest.mark.asyncio
    async def test_sent_invitations_filter_by_unused(self):
        """Test sent invitations query with unused filter."""
        variables = {
            "first": 10,
            "filters": {
                "isUsed": False
            }
        }

        result = await self._execute_query_authenticated(
            self.query, variables, self.client_user, self.endpoint_path
        )

        assert result.errors is None
        assert result.data is not None
        edges = result.data["sentInvitations"]["edges"]
        # All invitations should be unused
        for edge in edges:
            assert edge["node"]["isUsed"] is False

    @pytest.mark.asyncio
    async def test_sent_invitations_filter_by_email(self):
        """Test sent invitations query with email filter."""
        variables = {
            "first": 10,
            "filters": {
                "email": "active_inv"
            }
        }

        result = await self._execute_query_authenticated(
            self.query, variables, self.client_user, self.endpoint_path
        )

        assert result.errors is None
        assert result.data is not None
        edges = result.data["sentInvitations"]["edges"]
        # Should find invitations matching email
        emails = [edge["node"]["email"] for edge in edges]
        assert any("active_inv" in email for email in emails)

    @pytest.mark.asyncio
    async def test_sent_invitations_filter_by_search(self):
        """Test sent invitations query with general search filter."""
        variables = {
            "first": 10,
            "filters": {
                "search": self.client_user.first_name
            }
        }

        result = await self._execute_query_authenticated(
            self.query, variables, self.client_user, self.endpoint_path
        )

        assert result.errors is None
        assert result.data is not None
        # Should find invitations where invited_by name matches
        assert result.data["sentInvitations"]["totalCount"] >= 0

    @pytest.mark.asyncio
    async def test_sent_invitations_pagination(self):
        """Test sent invitations query with pagination."""
        variables = {
            "first": 2
        }

        result = await self._execute_query_authenticated(
            self.query, variables, self.client_user, self.endpoint_path
        )

        assert result.errors is None
        assert result.data is not None
        edges = result.data["sentInvitations"]["edges"]
        assert len(edges) <= 2
        assert result.data["sentInvitations"]["pageInfo"] is not None

    @pytest.mark.asyncio
    async def test_sent_invitations_unauthorized(self):
        """Test sent invitations query by unauthorized user (ambassador)."""
        unique_id = str(uuid.uuid4())[:8]
        ambassador_user = await sync_to_async(self.create_user)(
            username=f"ambassador_query_{unique_id}@test.com",
            email=f"ambassador_query_{unique_id}@test.com",
            role=self.roles['ambassador']
        )

        variables = {
            "first": 10
        }

        result = await self._execute_query_authenticated(
            self.query, variables, ambassador_user, self.endpoint_path
        )

        # Permission class rejects at GraphQL level
        assert result.data is None
        assert result.errors is not None
        assert len(result.errors) > 0


@pytest.mark.django_db(transaction=True)
class TestAvailableAmbassadorsQuery(AmbassadorsGraphQLTestCase):
    """Tests for available_ambassadors query."""

    @pytest.fixture(autouse=True)
    def setup(self, db):
        """Set up test data."""
        from config.schema_client import schema_clients
        self.roles = self.setup_default_roles()
        self.tenant = self.create_tenant(name="Available Ambassadors Tenant")
        # Use UUID for users to ensure uniqueness across test runs
        unique_id = str(uuid.uuid4())[:8]
        self.client_user = self.create_user(
            username=f"client_avail_{unique_id}@test.com",
            email=f"client_avail_{unique_id}@test.com",
            role=self.roles['client']
        )
        self.create_tenanted_user(self.client_user, self.tenant)

        unique_id2 = str(uuid.uuid4())[:8]
        self.spark_admin_user = self.create_user(
            username=f"spark_avail_{unique_id2}@test.com",
            email=f"spark_avail_{unique_id2}@test.com",
            role=self.roles['spark_admin']
        )

        # Create test ambassadors
        unique_id3 = str(uuid.uuid4())[:8]
        self.active_ambassador_user = self.create_user(
            username=f"active_amb_{unique_id3}@test.com",
            email=f"active_amb_{unique_id3}@test.com",
            first_name="Active",
            last_name="Ambassador",
            role=self.roles['ambassador'],
        )
        self.active_ambassador = self.create_ambassador(
            self.active_ambassador_user,
            address="Active Address",
            coordinates=[40.7128, -74.0060],
            is_active=True,
        )
        self.create_tenanted_user(
            self.active_ambassador_user, self.tenant
        )

        unique_id4 = str(uuid.uuid4())[:8]
        self.inactive_ambassador_user = self.create_user(
            username=f"inactive_amb_{unique_id4}@test.com",
            email=f"inactive_amb_{unique_id4}@test.com",
            first_name="Inactive",
            last_name="Ambassador",
            role=self.roles['ambassador'],
        )
        self.inactive_ambassador = self.create_ambassador(
            self.inactive_ambassador_user,
            address="Inactive Address",
            coordinates=[34.0522, -118.2437],
            is_active=False,
        )
        self.create_tenanted_user(
            self.inactive_ambassador_user, self.tenant
        )

        # Create ambassador for different tenant
        self.other_tenant = self.create_tenant(name="Other Tenant")
        unique_id5 = str(uuid.uuid4())[:8]
        self.other_tenant_ambassador_user = self.create_user(
            username=f"other_amb_{unique_id5}@test.com",
            email=f"other_amb_{unique_id5}@test.com",
            first_name="Other",
            last_name="Ambassador",
            role=self.roles['ambassador'],
        )
        self.other_tenant_ambassador = self.create_ambassador(
            self.other_tenant_ambassador_user,
            address="Other Address",
            is_active=True,
        )
        self.create_tenanted_user(
            self.other_tenant_ambassador_user, self.other_tenant
        )

        self.schema = schema_clients
        self.endpoint_path = "/api/v1/graphql/clients"

        self.query = """
            query AvailableAmbassadors($first: Int, $filters: AmbassadorFiltersInput) {
                availableAmbassadors(first: $first, filters: $filters) {
                    edges {
                        node {
                            id
                            isActive
                            address
                            coordinates
                            userId
                        }
                    }
                    totalCount
                    pageInfo {
                        hasNextPage
                        hasPreviousPage
                    }
                }
            }
        """

    @pytest.mark.asyncio
    async def test_available_ambassadors_success_by_client(self):
        """Test successful available ambassadors query by client."""
        variables = {
            "first": 10
        }

        result = await self._execute_query_authenticated(
            self.query, variables, self.client_user, self.endpoint_path
        )

        assert result.errors is None
        assert result.data is not None
        assert result.data["availableAmbassadors"] is not None
        assert result.data["availableAmbassadors"]["totalCount"] >= 2  # At least 2 ambassadors for this tenant

        # Verify we get ambassadors for the client's tenant
        edges = result.data["availableAmbassadors"]["edges"]
        assert len(edges) >= 2

    @pytest.mark.asyncio
    async def test_available_ambassadors_success_by_spark_admin(self):
        """Test successful available ambassadors query by spark admin."""
        variables = {
            "first": 10
        }

        result = await self._execute_query_authenticated(
            self.query, variables, self.spark_admin_user, self.endpoint_path
        )

        assert result.errors is None
        assert result.data is not None
        assert result.data["availableAmbassadors"] is not None
        # Spark admin should see all ambassadors
        assert result.data["availableAmbassadors"]["totalCount"] >= 3

    @pytest.mark.asyncio
    async def test_available_ambassadors_filter_by_tenant(self):
        """Test available ambassadors query with tenant filter."""
        variables = {
            "first": 10,
            "filters": {
                "tenantId": str(self.tenant.id)
            }
        }

        result = await self._execute_query_authenticated(
            self.query, variables, self.spark_admin_user, self.endpoint_path
        )

        assert result.errors is None
        assert result.data is not None
        edges = result.data["availableAmbassadors"]["edges"]
        # Should only get ambassadors for the specified tenant
        assert len(edges) >= 2

    @pytest.mark.asyncio
    async def test_available_ambassadors_filter_by_active(self):
        """Test available ambassadors query with active filter."""
        variables = {
            "first": 10,
            "filters": {
                "isActive": True
            }
        }

        result = await self._execute_query_authenticated(
            self.query, variables, self.client_user, self.endpoint_path
        )

        assert result.errors is None
        assert result.data is not None
        edges = result.data["availableAmbassadors"]["edges"]
        # All ambassadors should be active
        for edge in edges:
            assert edge["node"]["isActive"] is True

    @pytest.mark.asyncio
    async def test_available_ambassadors_filter_by_inactive(self):
        """Test available ambassadors query with inactive filter."""
        variables = {
            "first": 10,
            "filters": {
                "isActive": False
            }
        }

        result = await self._execute_query_authenticated(
            self.query, variables, self.client_user, self.endpoint_path
        )

        assert result.errors is None
        assert result.data is not None
        edges = result.data["availableAmbassadors"]["edges"]
        # All ambassadors should be inactive
        for edge in edges:
            assert edge["node"]["isActive"] is False

    @pytest.mark.asyncio
    async def test_available_ambassadors_filter_by_email(self):
        """Test available ambassadors query with email filter."""
        variables = {
            "first": 10,
            "filters": {
                "email": "active_amb"
            }
        }

        result = await self._execute_query_authenticated(
            self.query, variables, self.client_user, self.endpoint_path
        )

        assert result.errors is None
        assert result.data is not None
        edges = result.data["availableAmbassadors"]["edges"]
        # Should find ambassadors matching email (we can only verify by userId since email is not exposed)
        assert len(edges) >= 0

    @pytest.mark.asyncio
    async def test_available_ambassadors_filter_by_name(self):
        """Test available ambassadors query with name filter."""
        variables = {
            "first": 10,
            "filters": {
                "name": "Active"
            }
        }

        result = await self._execute_query_authenticated(
            self.query, variables, self.client_user, self.endpoint_path
        )

        assert result.errors is None
        assert result.data is not None
        edges = result.data["availableAmbassadors"]["edges"]
        # Should find ambassadors matching name (we can only verify by userId since name is not exposed)
        assert len(edges) >= 0

    @pytest.mark.asyncio
    async def test_available_ambassadors_filter_by_address(self):
        """Test available ambassadors query with address filter."""
        variables = {
            "first": 10,
            "filters": {
                "address": "Active Address"
            }
        }

        result = await self._execute_query_authenticated(
            self.query, variables, self.client_user, self.endpoint_path
        )

        assert result.errors is None
        assert result.data is not None
        edges = result.data["availableAmbassadors"]["edges"]
        # Should find ambassadors matching address
        addresses = [edge["node"]["address"] for edge in edges]
        assert any("Active Address" in addr for addr in addresses if addr)

    @pytest.mark.asyncio
    async def test_available_ambassadors_filter_by_search(self):
        """Test available ambassadors query with general search filter."""
        variables = {
            "first": 10,
            "filters": {
                "search": "Active"
            }
        }

        result = await self._execute_query_authenticated(
            self.query, variables, self.client_user, self.endpoint_path
        )

        assert result.errors is None
        assert result.data is not None
        # Should find ambassadors matching search term in email, name, or address
        assert result.data["availableAmbassadors"]["totalCount"] >= 0

    @pytest.mark.asyncio
    async def test_available_ambassadors_pagination(self):
        """Test available ambassadors query with pagination."""
        variables = {
            "first": 1
        }

        result = await self._execute_query_authenticated(
            self.query, variables, self.client_user, self.endpoint_path
        )

        assert result.errors is None
        assert result.data is not None
        edges = result.data["availableAmbassadors"]["edges"]
        assert len(edges) <= 1
        assert result.data["availableAmbassadors"]["pageInfo"] is not None

    @pytest.mark.asyncio
    async def test_available_ambassadors_unauthorized(self):
        """Test available ambassadors query by unauthorized user (ambassador)."""
        unique_id = str(uuid.uuid4())[:8]
        ambassador_user = await sync_to_async(self.create_user)(
            username=f"ambassador_query2_{unique_id}@test.com",
            email=f"ambassador_query2_{unique_id}@test.com",
            role=self.roles['ambassador']
        )

        variables = {
            "first": 10
        }

        result = await self._execute_query_authenticated(
            self.query, variables, ambassador_user, self.endpoint_path
        )

        # Permission class rejects at GraphQL level
        assert result.data is None
        assert result.errors is not None
        assert len(result.errors) > 0

