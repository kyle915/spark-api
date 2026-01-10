"""
Tests for Google Calendar synchronization signals.

This module tests:
- Event post_save signal (for non-ambassadors)
- AmbassadorEvent post_save signal (for ambassadors)
- Signal triggers RQ jobs correctly
"""
import pytest
from unittest.mock import patch, MagicMock
from datetime import date, time, datetime
from django.contrib.auth import get_user_model
from django.utils import timezone
from tenants.models import Role, Tenant, TenantedUser, GoogleCalendarConnection
from events.models import Event, EventType, EventStatus, Request, RequestType, RequestStatus
from events.models import Client, Distributor, Retailer, Location
from ambassadors.models import Ambassador, AmbassadorEvent
from events.tasks import sync_event_to_google_calendar, sync_event_to_all_connected_users
from utils.utils import ROLE_ID

User = get_user_model()


@pytest.mark.django_db
class TestGoogleCalendarSignals:
    """Tests for Google Calendar synchronization signals."""

    def setup_method(self):
        """Set up test data."""
        # Create roles
        self.ambassador_role = Role.objects.create(
            name="Ambassador", slug="ambassador")
        self.client_role = Role.objects.create(name="Client", slug="client")

        # Create users
        self.ambassador_user = User.objects.create_user(
            username="ambassador",
            email="ambassador@test.com",
            password="testpass123",
            role=self.ambassador_role
        )
        self.client_user = User.objects.create_user(
            username="client",
            email="client@test.com",
            password="testpass123",
            role=self.client_role
        )

        # Create tenant
        self.tenant = Tenant.objects.create(
            name="Test Tenant",
            created_by=self.client_user
        )

        # Create tenanted users
        TenantedUser.objects.create(
            user=self.ambassador_user,
            tenant=self.tenant,
            is_active=True,
            created_by=self.client_user
        )
        TenantedUser.objects.create(
            user=self.client_user,
            tenant=self.tenant,
            is_active=True,
            created_by=self.client_user
        )

        # Create Google Calendar connections
        self.ambassador_connection = GoogleCalendarConnection.objects.create(
            user=self.ambassador_user,
            created_by=self.ambassador_user,
            updated_by=self.ambassador_user,
            is_active=True
        )
        self.ambassador_connection.set_access_token("test_token")

        self.client_connection = GoogleCalendarConnection.objects.create(
            user=self.client_user,
            created_by=self.client_user,
            updated_by=self.client_user,
            is_active=True
        )
        self.client_connection.set_access_token("test_token")

        # Create ambassador
        self.ambassador = Ambassador.objects.create(
            user=self.ambassador_user,
            created_by=self.client_user
        )

        # Create event type and status
        self.event_type = EventType.objects.create(
            name="Test Type",
            tenant=self.tenant,
            created_by=self.client_user
        )
        self.event_status = EventStatus.objects.create(
            name="Approved",
            tenant=self.tenant,
            created_by=self.client_user
        )

        # Create location and related models for request
        self.location = Location.objects.create(
            name="Test Location",
            code="TEST",
            zip="12345",
            tenant=self.tenant,
            created_by=self.client_user
        )
        self.client = Client.objects.create(
            name="Test Client",
            email="client@test.com",
            tenant=self.tenant,
            created_by=self.client_user
        )
        self.distributor = Distributor.objects.create(
            name="Test Distributor",
            email="dist@test.com",
            location=self.location,
            tenant=self.tenant,
            created_by=self.client_user
        )
        self.retailer = Retailer.objects.create(
            name="Test Retailer",
            address="123 Test St",
            store_contact="Contact",
            location=self.location,
            tenant=self.tenant,
            created_by=self.client_user
        )
        self.request_type = RequestType.objects.create(
            name="Test Request Type",
            tenant=self.tenant,
            created_by=self.client_user
        )
        self.request_status = RequestStatus.objects.create(
            name="Approved",
            tenant=self.tenant,
            created_by=self.client_user
        )
        # Request model uses DateTimeField, so convert date to datetime
        request_date = timezone.make_aware(
            datetime.combine(date.today(), time.min))

        self.request = Request.objects.create(
            name="Test Request",
            date=request_date,
            address="123 Test St",
            client=self.client,
            distributor=self.distributor,
            retailer=self.retailer,
            request_type=self.request_type,
            status=self.request_status,
            tenant=self.tenant,
            created_by=self.client_user
        )

    @patch('events.signals.queues.default.add')
    def test_event_created_by_non_ambassador_triggers_sync(self, mock_add):
        """Test that Event created by non-ambassador triggers sync to all users."""
        from events.tasks import sync_event_to_all_connected_users

        event = Event.objects.create(
            name="Test Event",
            tenant=self.tenant,
            event_type=self.event_type,
            status=self.event_status,
            address="123 Test St",
            request=self.request,
            created_by=self.client_user  # Non-ambassador
        )

        # Signal should trigger sync job
        mock_add.assert_called_once_with(
            sync_event_to_all_connected_users, event.id, self.tenant.id)

    @patch('events.signals.queues.default.add')
    def test_event_created_by_ambassador_skips_sync(self, mock_add):
        """Test that Event created by ambassador skips sync (handled by AmbassadorEvent)."""
        event = Event.objects.create(
            name="Test Event",
            tenant=self.tenant,
            event_type=self.event_type,
            status=self.event_status,
            address="123 Test St",
            request=self.request,
            created_by=self.ambassador_user  # Ambassador
        )

        # Signal should not trigger sync (will be handled by AmbassadorEvent signal)
        mock_add.assert_not_called()

    @patch('events.signals.queues.default.add')
    def test_ambassador_event_created_triggers_sync(self, mock_add):
        """Test that AmbassadorEvent creation triggers sync for ambassador."""
        from events.tasks import sync_event_to_google_calendar, sync_event_to_all_connected_users

        event = Event.objects.create(
            name="Test Event",
            tenant=self.tenant,
            event_type=self.event_type,
            status=self.event_status,
            address="123 Test St",
            request=self.request,
            created_by=self.client_user
        )

        # Event creation triggers sync to all connected users (first call)
        assert mock_add.call_count == 1
        mock_add.assert_called_with(
            sync_event_to_all_connected_users, event.id, self.tenant.id)

        ambassador_event = AmbassadorEvent.objects.create(
            ambassador=self.ambassador,
            event=event,
            tenant=self.tenant,
            created_by=self.ambassador_user  # Ambassador creates the AmbassadorEvent
        )

        # AmbassadorEvent creation should trigger sync job for the ambassador (second call)
        assert mock_add.call_count == 2
        # Check the last call was for the ambassador sync
        mock_add.assert_any_call(
            sync_event_to_google_calendar,
            self.ambassador_user.id,
            event.id
        )

    @patch('events.signals.queues.default.add')
    def test_ambassador_event_created_by_non_ambassador_skips_sync(self, mock_add):
        """Test that AmbassadorEvent created by non-ambassador doesn't trigger sync."""
        from events.tasks import sync_event_to_all_connected_users

        event = Event.objects.create(
            name="Test Event",
            tenant=self.tenant,
            event_type=self.event_type,
            status=self.event_status,
            address="123 Test St",
            request=self.request,
            created_by=self.client_user
        )

        # Event creation triggers sync to all connected users (first call)
        assert mock_add.call_count == 1
        mock_add.assert_called_with(
            sync_event_to_all_connected_users, event.id, self.tenant.id)

        ambassador_event = AmbassadorEvent.objects.create(
            ambassador=self.ambassador,
            event=event,
            tenant=self.tenant,
            created_by=self.client_user  # Non-ambassador creates the AmbassadorEvent
        )

        # AmbassadorEvent creation by non-ambassador should NOT trigger additional sync
        # Only the Event signal should have been called (still 1 call)
        assert mock_add.call_count == 1
