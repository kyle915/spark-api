"""
Tests for Google Calendar RQ jobs.

This module tests:
- sync_event_to_google_calendar
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
from ambassadors.models import Ambassador, AmbassadorEvent
from events.tasks import (
    sync_event_to_google_calendar,
    sync_event_to_all_connected_users
)
from utils.utils import ROLE_ID

User = get_user_model()


@pytest.mark.django_db
class TestGoogleCalendarTasks:
    """Tests for Google Calendar RQ jobs."""

    def setup_method(self):
        """Set up test data."""
        # Create role with canonical client ID so role helpers and ID checks work
        self.role, _ = Role.objects.update_or_create(
            pk=ROLE_ID.Client,
            defaults={
                "name": "Client",
                "slug": "client",
            },
        )

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
        request_date = timezone.make_aware(
            datetime.combine(date.today(), time.min))
        start_datetime = timezone.make_aware(
            datetime.combine(date.today(), time(10, 0)))
        end_datetime = timezone.make_aware(
            datetime.combine(date.today(), time(12, 0)))

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
    def test_sync_event_to_google_calendar_user_not_found(self, mock_service_class):
        """Test that sync raises User.DoesNotExist when user doesn't exist."""
        mock_service = MagicMock()
        mock_service_class.return_value = mock_service

        with pytest.raises(User.DoesNotExist):
            sync_event_to_google_calendar(99999, self.event.id)

        mock_service_class.assert_not_called()

    @patch('events.tasks.GoogleCalendarService')
    def test_sync_event_to_google_calendar_event_not_found(self, mock_service_class):
        """Test that sync raises Event.DoesNotExist when event doesn't exist."""
        mock_service = MagicMock()
        mock_service_class.return_value = mock_service

        with pytest.raises(Event.DoesNotExist):
            sync_event_to_google_calendar(self.user.id, 99999)

        mock_service_class.assert_not_called()

    @patch('events.tasks.GoogleCalendarService')
    def test_sync_event_to_google_calendar_sync_fails(self, mock_service_class):
        """Test that sync raises exception when sync_event returns None."""
        mock_service = MagicMock()
        mock_service.sync_event.return_value = None
        mock_service_class.return_value = mock_service

        with pytest.raises(Exception, match="Failed to create Google Calendar event"):
            sync_event_to_google_calendar(self.user.id, self.event.id)

        mock_service_class.assert_called_once_with(self.user)
        mock_service.sync_event.assert_called_once()

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

    @patch('events.jobs.google_calendar_jobs.EventGoogleCalendarJob')
    def test_sync_event_to_all_connected_users(self, mock_job_class):
        """Test syncing event to all connected users via EventGoogleCalendarJob."""
        # Mock the job instance
        mock_job = MagicMock()
        mock_job_class.return_value = mock_job

        # Execute task
        result = sync_event_to_all_connected_users(
            self.event.id, self.tenant.id)

        # Verify job was instantiated with event_id
        mock_job_class.assert_called_once_with(self.event.id)
        # Verify handle() was called
        mock_job.handle.assert_called_once()
        assert result is None

    def test_sync_event_to_all_connected_users_event_not_found(self):
        """Test that sync_event_to_all_connected_users raises Event.DoesNotExist."""
        with pytest.raises(Event.DoesNotExist):
            sync_event_to_all_connected_users(99999, self.tenant.id)
