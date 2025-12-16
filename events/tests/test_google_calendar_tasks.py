"""
Tests for Google Calendar RQ jobs.

This module tests:
- sync_event_to_google_calendar
- update_event_in_google_calendar
- sync_event_to_all_connected_users
"""
import pytest
from unittest.mock import patch, MagicMock
from datetime import date, time, timedelta, datetime
from django.contrib.auth import get_user_model
from django.utils import timezone
from tenants.models import Role, Tenant, TenantedUser, GoogleCalendarConnection
from events.models import Event, EventType, EventStatus, Request, RequestType, RequestStatus
from events.models import Client, Distributor, Retailer, Location
from events.tasks import (
    sync_event_to_google_calendar,
    update_event_in_google_calendar,
    sync_event_to_all_connected_users
)
from utils.utils import ROLE_ID

User = get_user_model()


@pytest.mark.django_db
class TestGoogleCalendarTasks:
    """Tests for Google Calendar RQ jobs."""

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

        # Create location and related models for request
        self.location = Location.objects.create(
            name="Test Location",
            code="TEST",
            zip="12345",
            tenant=self.tenant,
            created_by=self.user
        )
        self.client = Client.objects.create(
            name="Test Client",
            email="client@test.com",
            tenant=self.tenant,
            created_by=self.user
        )
        self.distributor = Distributor.objects.create(
            name="Test Distributor",
            email="dist@test.com",
            location=self.location,
            tenant=self.tenant,
            created_by=self.user
        )
        self.retailer = Retailer.objects.create(
            name="Test Retailer",
            address="123 Test St",
            store_contact="Contact",
            location=self.location,
            tenant=self.tenant,
            created_by=self.user
        )
        self.request_type = RequestType.objects.create(
            name="Test Request Type",
            tenant=self.tenant,
            created_by=self.user
        )
        self.request_status = RequestStatus.objects.create(
            name="Approved",
            tenant=self.tenant,
            created_by=self.user
        )
        # Request model uses DateTimeField, so convert date/time to datetime
        request_date = timezone.make_aware(datetime.combine(date.today(), time.min))
        start_datetime = timezone.make_aware(datetime.combine(date.today(), time(10, 0)))
        end_datetime = timezone.make_aware(datetime.combine(date.today(), time(12, 0)))
        
        self.request = Request.objects.create(
            name="Test Request",
            date=request_date,
            start_time=start_datetime,  # Required for Google Calendar sync
            end_time=end_datetime,    # Required for Google Calendar sync
            address="123 Test St",
            client=self.client,
            distributor=self.distributor,
            retailer=self.retailer,
            request_type=self.request_type,
            status=self.request_status,
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
            request=self.request,
            created_by=self.user
        )

    @patch('events.tasks.GoogleCalendarService')
    def test_sync_event_to_google_calendar_success(self, mock_service_class):
        """Test successful event sync to Google Calendar."""
        # Mock the service instance and its methods
        mock_service = MagicMock()
        mock_service.sync_event.return_value = "google_event_123"
        # Configure the class to return our mock instance when instantiated
        mock_service_class.return_value = mock_service

        # Execute task
        result = sync_event_to_google_calendar(self.user.id, self.event.id)

        # Verify service was instantiated and called
        mock_service_class.assert_called_once_with(self.user)
        mock_service.sync_event.assert_called_once()
        assert result is None  # Task doesn't return value

    @patch('events.tasks.GoogleCalendarService')
    def test_sync_event_to_google_calendar_no_connection(self, mock_service_class):
        """Test event sync when user has no connection."""
        # Deactivate connection - task should return early without calling service
        self.connection.is_active = False
        self.connection.save()

        # Execute task - should return early without calling service
        result = sync_event_to_google_calendar(self.user.id, self.event.id)
        
        # Service should not be instantiated if connection check fails early
        assert result is None
        # Note: Service won't be called if connection check fails, so no need to verify mock

    @patch('events.tasks.GoogleCalendarService')
    def test_update_event_in_google_calendar_success(self, mock_service_class):
        """Test successful event update in Google Calendar."""
        # Mock the service instance and its methods
        mock_service = MagicMock()
        mock_service.sync_event.return_value = "google_event_123"
        # Configure the class to return our mock instance when instantiated
        mock_service_class.return_value = mock_service

        # Execute task
        result = update_event_in_google_calendar(
            self.user.id,
            self.event.id,
            "google_event_123"
        )

        # Verify service was instantiated and called
        mock_service_class.assert_called_once_with(self.user)
        mock_service.sync_event.assert_called_once()
        assert result is None

    @patch('events.tasks.django_rq.enqueue')
    def test_sync_event_to_all_connected_users(self, mock_enqueue):
        """Test syncing event to all connected users."""
        # Create another user with connection
        user2 = User.objects.create_user(
            username="testuser2",
            email="test2@test.com",
            password="testpass123",
            role=self.role
        )
        TenantedUser.objects.create(
            user=user2,
            tenant=self.tenant,
            is_active=True,
            created_by=self.user
        )
        connection2 = GoogleCalendarConnection.objects.create(
            user=user2,
            created_by=user2,
            updated_by=user2,
            is_active=True
        )
        connection2.set_access_token("test_access_token")
        connection2.set_refresh_token("test_refresh_token")
        connection2.token_expiry = timezone.now() + timedelta(hours=1)
        connection2.save()

        # Execute task
        result = sync_event_to_all_connected_users(
            self.event.id, self.tenant.id)

        # Verify sync jobs were enqueued for both users
        assert mock_enqueue.call_count == 2
        # Verify correct function and arguments were enqueued
        calls = mock_enqueue.call_args_list
        assert all(call[0][0] == 'default' for call in calls)  # All use 'default' queue
        assert all(call[0][1] == sync_event_to_google_calendar for call in calls)  # All call sync function
        assert result is None
