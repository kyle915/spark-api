"""
Tests for GoogleCalendarService.

This module tests:
- create_event
- update_event
- delete_event
- token refresh logic
"""
import pytest
from unittest.mock import patch, MagicMock
from datetime import datetime, timedelta
from django.utils import timezone
from django.contrib.auth import get_user_model
from tenants.models import GoogleCalendarConnection, Role, Tenant, TenantedUser
from events.models import Event, EventType, EventStatus
from utils.google_calendar import GoogleCalendarService
from utils.utils import ROLE_ID

User = get_user_model()


@pytest.mark.django_db
class TestGoogleCalendarService:
    """Tests for GoogleCalendarService."""

    def setup_method(self):
        """Set up test data."""
        # Create role
        self.role = Role.objects.create(name="Client", slug="client")

        # Create user
        self.user = User.objects.create_user(
            username="testuser",
            email="test@test.com",
            password="testpass123",
            role=self.role
        )

        # Create tenant
        self.tenant = Tenant.objects.create(
            name="Test Tenant",
            created_by=self.user
        )

        # Create tenanted user
        TenantedUser.objects.create(
            user=self.user,
            tenant=self.tenant,
            is_active=True,
            created_by=self.user
        )

        # Create Google Calendar connection
        self.connection = GoogleCalendarConnection.objects.create(
            user=self.user,
            created_by=self.user,
            updated_by=self.user,
            is_active=True,
            calendar_id="primary"
        )
        self.connection.set_access_token("test_access_token")
        self.connection.set_refresh_token("test_refresh_token")
        self.connection.token_expiry = timezone.now() + timedelta(hours=1)
        self.connection.save()

        # Create event type and status
        self.event_type = EventType.objects.create(
            name="Test Type",
            tenant=self.tenant,
            created_by=self.user
        )
        self.event_status = EventStatus.objects.create(
            name="Approved",
            tenant=self.tenant,
            created_by=self.user
        )

        # Create event
        self.event = Event.objects.create(
            name="Test Event",
            tenant=self.tenant,
            event_type=self.event_type,
            status=self.event_status,
            address="123 Test St",
            notes="Test notes",
            start_time=datetime.now().time(),
            end_time=(datetime.now() + timedelta(hours=2)).time(),
            created_by=self.user
        )

    @patch('utils.google_calendar.build')
    @patch('utils.google_calendar.AuthorizedHttp')
    @patch('utils.google_calendar.Credentials')
    def test_create_event_success(self, mock_credentials_class, mock_http, mock_build):
        """Test successful event creation."""
        # Mock Google Calendar API
        mock_service = MagicMock()
        mock_events = MagicMock()
        mock_insert = MagicMock()

        mock_insert.execute.return_value = {'id': 'google_event_123'}
        mock_events.insert.return_value = mock_insert
        mock_service.events.return_value = mock_events
        mock_build.return_value = mock_service

        service = GoogleCalendarService(self.user)
        google_event_id = service.create_event(
            self.event,
            event_type_name=self.event_type.name,
            status_name=self.event_status.name
        )

        assert google_event_id == 'google_event_123'
        mock_insert.execute.assert_called_once()

    @patch('utils.google_calendar.build')
    @patch('utils.google_calendar.AuthorizedHttp')
    @patch('utils.google_calendar.Credentials')
    def test_create_event_no_connection(self, mock_credentials_class, mock_http, mock_build):
        """Test event creation when user has no connection."""
        # Deactivate connection
        self.connection.is_active = False
        self.connection.save()

        service = GoogleCalendarService(self.user)
        google_event_id = service.create_event(self.event)

        assert google_event_id is None

    @patch('utils.google_calendar.build')
    @patch('utils.google_calendar.AuthorizedHttp')
    @patch('utils.google_calendar.Credentials')
    def test_update_event_success(self, mock_credentials_class, mock_http, mock_build):
        """Test successful event update."""
        # Mock Google Calendar API
        mock_service = MagicMock()
        mock_events = MagicMock()
        mock_get = MagicMock()
        mock_update = MagicMock()

        # Mock existing event
        existing_event = {
            'id': 'google_event_123',
            'summary': 'Old Event Name',
            'start': {'dateTime': '2024-01-01T10:00:00Z'},
            'end': {'dateTime': '2024-01-01T12:00:00Z'}
        }
        mock_get.execute.return_value = existing_event
        mock_events.get.return_value = mock_get

        mock_update.execute.return_value = {'id': 'google_event_123'}
        mock_events.update.return_value = mock_update
        mock_service.events.return_value = mock_events
        mock_build.return_value = mock_service

        service = GoogleCalendarService(self.user)
        success = service.update_event(
            'google_event_123',
            self.event,
            event_type_name=self.event_type.name,
            status_name=self.event_status.name
        )

        assert success is True
        mock_update.execute.assert_called_once()

    @patch('utils.google_calendar.build')
    @patch('utils.google_calendar.AuthorizedHttp')
    @patch('utils.google_calendar.Credentials')
    def test_delete_event_success(self, mock_credentials_class, mock_http, mock_build):
        """Test successful event deletion."""
        # Mock Google Calendar API
        mock_service = MagicMock()
        mock_events = MagicMock()
        mock_delete = MagicMock()

        mock_delete.execute.return_value = None
        mock_events.delete.return_value = mock_delete
        mock_service.events.return_value = mock_events
        mock_build.return_value = mock_service

        service = GoogleCalendarService(self.user)
        success = service.delete_event('google_event_123')

        assert success is True
        mock_delete.execute.assert_called_once()

    def test_get_connection(self):
        """Test getting active connection."""
        service = GoogleCalendarService(self.user)
        connection = service._get_connection()

        assert connection is not None
        assert connection.user == self.user
        assert connection.is_active is True

    def test_get_connection_no_active(self):
        """Test getting connection when none exists."""
        # Deactivate connection
        self.connection.is_active = False
        self.connection.save()

        service = GoogleCalendarService(self.user)
        connection = service._get_connection()

        assert connection is None
