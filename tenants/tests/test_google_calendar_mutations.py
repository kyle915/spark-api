"""
Tests for Google Calendar OAuth mutations.

This module tests:
- connectGoogleCalendar
- googleCalendarCallback
- disconnectGoogleCalendar

Test scenarios include:
- Successful OAuth flow
- Invalid state parameter
- Missing Google OAuth credentials
- Connection already exists
"""
import pytest
from unittest.mock import patch, MagicMock
from asgiref.sync import sync_to_async
from django.contrib.auth import get_user_model
from django.core.cache import cache
from tenants.models import GoogleCalendarConnection, Role, Tenant, TenantedUser
from tenants.tests.base import BaseGraphQLTestCase
from utils.utils import ROLE_ID

User = get_user_model()


@pytest.mark.django_db(transaction=True)
class TestGoogleCalendarMutations(BaseGraphQLTestCase):
    """Tests for Google Calendar OAuth mutations."""

    @pytest.fixture(autouse=True)
    def setup(self, db):
        """Set up test data before each test."""
        from config.schema_spark import schema_spark
        self.roles = self.setup_default_roles()
        self.tenant = self.create_tenant(name="Test Company")
        self.user = self.create_user(
            username="testuser",
            email="test@test.com",
            role=self.roles['client'],
            password="testpass123"
        )
        self.create_tenanted_user(user=self.user, tenant=self.tenant)
        self.schema = schema_spark
        self.endpoint_path = "/api/v1/graphql/spark"

    @pytest.mark.asyncio
    @patch('tenants.calendar.utils.Flow')
    async def test_connect_google_calendar_success(self, mock_flow_class):
        """Test successful Google Calendar connection initiation."""
        # Mock the OAuth flow
        mock_flow = MagicMock()
        mock_flow.authorization_url.return_value = (
            "https://accounts.google.com/o/oauth2/auth?client_id=test",
            "state123"
        )
        mock_flow_class.from_client_config.return_value = mock_flow

        mutation = """
        mutation ConnectGoogleCalendar($input: ConnectGoogleCalendarInput!) {
            connectGoogleCalendar(input: $input) {
                success
                message
                authorizationUrl
                state
                clientMutationId
            }
        }
        """

        variables = {
            "input": {
                "clientMutationId": "test-123"
            }
        }

        result = await self._execute_mutation(
            mutation, variables, self.endpoint_path, user=self.user
        )

        assert result.data is not None
        assert result.data["connectGoogleCalendar"]["success"] is True
        assert result.data["connectGoogleCalendar"]["authorizationUrl"] is not None
        assert result.data["connectGoogleCalendar"]["state"] is not None

    @pytest.mark.asyncio
    async def test_connect_google_calendar_already_connected(self):
        """Test connecting when already connected."""
        # Create existing connection
        connection = await sync_to_async(GoogleCalendarConnection.objects.create)(
            user=self.user,
            created_by=self.user,
            updated_by=self.user,
            access_token="encrypted_token",
            is_active=True
        )
        connection.set_access_token("test_token")
        await sync_to_async(connection.save)()

        mutation = """
        mutation ConnectGoogleCalendar($input: ConnectGoogleCalendarInput!) {
            connectGoogleCalendar(input: $input) {
                success
                message
            }
        }
        """

        variables = {"input": {}}

        result = await self._execute_mutation(
            mutation, variables, self.endpoint_path, user=self.user
        )

        assert result.data is not None
        assert result.data["connectGoogleCalendar"]["success"] is False
        assert "already" in result.data["connectGoogleCalendar"]["message"].lower(
        )

    @pytest.mark.asyncio
    @patch('tenants.calendar.utils.Flow')
    async def test_google_calendar_callback_success(self, mock_flow_class):
        """Test successful Google Calendar OAuth callback."""
        # Set up state in cache
        state = "test_state_123"
        cache_key = f"google_calendar_oauth_state_{state}"
        cache.set(cache_key, self.user.id, timeout=600)

        # Mock the OAuth flow
        mock_flow = MagicMock()
        mock_credentials = MagicMock()
        mock_credentials.token = "access_token_123"
        mock_credentials.refresh_token = "refresh_token_123"
        mock_credentials.expiry = None
        mock_flow.credentials = mock_credentials
        mock_flow_class.from_client_config.return_value = mock_flow

        mutation = """
        mutation GoogleCalendarCallback($input: GoogleCalendarCallbackInput!) {
            googleCalendarCallback(input: $input) {
                success
                message
                clientMutationId
            }
        }
        """

        variables = {
            "input": {
                "code": "auth_code_123",
                "state": state,
                "clientMutationId": "test-123"
            }
        }

        result = await self._execute_mutation(
            mutation, variables, self.endpoint_path, user=self.user
        )

        assert result.data is not None
        assert result.data["googleCalendarCallback"]["success"] is True

        # Verify connection was created
        connection = await sync_to_async(
            GoogleCalendarConnection.objects.get
        )(user=self.user, is_active=True)
        assert connection is not None
        assert connection.get_access_token() == "access_token_123"

        # Clean up
        cache.delete(cache_key)

    @pytest.mark.asyncio
    async def test_google_calendar_callback_invalid_state(self):
        """Test callback with invalid state parameter."""
        mutation = """
        mutation GoogleCalendarCallback($input: GoogleCalendarCallbackInput!) {
            googleCalendarCallback(input: $input) {
                success
                message
            }
        }
        """

        variables = {
            "input": {
                "code": "auth_code_123",
                "state": "invalid_state"
            }
        }

        result = await self._execute_mutation(
            mutation, variables, self.endpoint_path, user=self.user
        )

        assert result.data is not None
        assert result.data["googleCalendarCallback"]["success"] is False
        assert "invalid state" in result.data["googleCalendarCallback"]["message"].lower(
        )

    @pytest.mark.asyncio
    async def test_disconnect_google_calendar_success(self):
        """Test successful Google Calendar disconnection."""
        # Create connection
        connection = await sync_to_async(GoogleCalendarConnection.objects.create)(
            user=self.user,
            created_by=self.user,
            updated_by=self.user,
            access_token="encrypted_token",
            is_active=True
        )
        connection.set_access_token("test_token")
        await sync_to_async(connection.save)()

        mutation = """
        mutation DisconnectGoogleCalendar($input: DisconnectGoogleCalendarInput!) {
            disconnectGoogleCalendar(input: $input) {
                success
                message
                clientMutationId
            }
        }
        """

        variables = {
            "input": {
                "clientMutationId": "test-123"
            }
        }

        result = await self._execute_mutation(
            mutation, variables, self.endpoint_path, user=self.user
        )

        assert result.data is not None
        assert result.data["disconnectGoogleCalendar"]["success"] is True

        # Verify connection was deactivated
        await sync_to_async(connection.refresh_from_db)()
        assert connection.is_active is False

    @pytest.mark.asyncio
    async def test_disconnect_google_calendar_not_connected(self):
        """Test disconnecting when not connected."""
        mutation = """
        mutation DisconnectGoogleCalendar($input: DisconnectGoogleCalendarInput!) {
            disconnectGoogleCalendar(input: $input) {
                success
                message
            }
        }
        """

        variables = {"input": {}}

        result = await self._execute_mutation(
            mutation, variables, self.endpoint_path, user=self.user
        )

        assert result.data is not None
        assert result.data["disconnectGoogleCalendar"]["success"] is False
        assert "no active" in result.data["disconnectGoogleCalendar"]["message"].lower(
        )
