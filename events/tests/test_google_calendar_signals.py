"""
Tests for Google Calendar synchronization signals.

This module tests:
- Event post_save signal (for non-ambassadors)
- AmbassadorEvent post_save signal (for ambassadors)
- Signal triggers RQ jobs correctly
"""
import pytest
import threading
from unittest.mock import patch, MagicMock
from datetime import date, time, datetime
from django.contrib.auth import get_user_model
from django.utils import timezone
from tenants.models import Role, Tenant, TenantedUser, GoogleCalendarConnection
from tenants.tests.base import ensure_role
from events.models import Event, EventType, EventStatus, Request, RequestType, RequestStatus
from events.models import Client, Distributor, Retailer, Location
from ambassadors.models import Ambassador, AmbassadorEvent
from utils.utils import ROLE_ID

User = get_user_model()


def _join_calendar_sync_threads(timeout: float = 5.0) -> None:
    """The AmbassadorEvent calendar sync runs in a daemon thread (so a slow
    Google API call can't freeze the save / invite mutation). Join those
    threads so assertions observe the completed call deterministically."""
    for t in list(threading.enumerate()):
        if t.name.startswith("cal-sync-ae-"):
            t.join(timeout=timeout)


@pytest.mark.django_db
class TestGoogleCalendarSignals:
    """Tests for Google Calendar synchronization signals."""

    @pytest.fixture(autouse=True)
    def _enable_calendar_sync(self, settings):
        # This suite exercises the calendar-sync daemon thread itself, so opt
        # back into it — it's off by default under DEBUG/CI (see
        # settings.CALENDAR_SYNC_ENABLED) precisely so incidental
        # AmbassadorEvent-creating tests don't spawn it and deadlock teardown.
        settings.CALENDAR_SYNC_ENABLED = True

    def setup_method(self):
        """Set up test data."""
        # Create roles (collision-proof against migration seeds / leaked rows)
        self.ambassador_role = ensure_role(
            "Ambassador", slug=Role.AMBASSADOR_SLUG, pk=ROLE_ID.Ambassadors)
        self.client_role = ensure_role(
            "Client", slug=Role.CLIENT_SLUG, pk=ROLE_ID.Client)

        # Create users (keyed on the unique username — adopt leaked rows)
        self.ambassador_user, _ = User.objects.update_or_create(
            username="ambassador",
            defaults={"email": "ambassador@test.com",
                      "role": self.ambassador_role, "is_active": True},
        )
        self.client_user, _ = User.objects.update_or_create(
            username="client",
            defaults={"email": "client@test.com",
                      "role": self.client_role, "is_active": True},
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

        # Create Google Calendar connections (OneToOne per user — adopt)
        self.ambassador_connection, _ = (
            GoogleCalendarConnection.objects.update_or_create(
                user=self.ambassador_user,
                defaults={"created_by": self.ambassador_user,
                          "updated_by": self.ambassador_user,
                          "is_active": True},
            )
        )
        self.ambassador_connection.set_access_token("test_token")

        self.client_connection, _ = (
            GoogleCalendarConnection.objects.update_or_create(
                user=self.client_user,
                defaults={"created_by": self.client_user,
                          "updated_by": self.client_user,
                          "is_active": True},
            )
        )
        self.client_connection.set_access_token("test_token")

        # Create ambassador (OneToOne per user — adopt)
        self.ambassador, _ = Ambassador.objects.update_or_create(
            user=self.ambassador_user,
            defaults={"created_by": self.client_user},
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

        Event.objects.create(
            name="Test Event",
            tenant=self.tenant,
            event_type=self.event_type,
            status=self.event_status,
            address="123 Test St",
            request=self.request,
            created_by=self.client_user  # Non-ambassador
        )

        # Signal should trigger sync job with only event.id (no tenant_id)
        mock_add.assert_called_once()
        call_args = mock_add.call_args[0]
        assert call_args[0] == sync_event_to_all_connected_users
        assert call_args[1] == Event.objects.latest('id').id

    @patch('events.signals.queues.default.add')
    def test_event_created_by_ambassador_skips_sync(self, mock_add):
        """Test that Event created by ambassador skips sync (handled by AmbassadorEvent)."""
        Event.objects.create(
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

    @patch('events.jobs.google_calendar_jobs.EventGoogleCalendarJob')
    def test_ambassador_event_created_triggers_sync(self, mock_job_class):
        """Test that AmbassadorEvent creation triggers sync via EventGoogleCalendarJob."""
        # Mock the job instance
        mock_job = MagicMock()
        mock_job_class.return_value = mock_job

        event = Event.objects.create(
            name="Test Event",
            tenant=self.tenant,
            event_type=self.event_type,
            status=self.event_status,
            address="123 Test St",
            request=self.request,
            created_by=self.client_user
        )

        ambassador_event = AmbassadorEvent.objects.create(
            ambassador=self.ambassador,
            event=event,
            tenant=self.tenant,
            created_by=self.ambassador_user  # Ambassador creates the AmbassadorEvent
        )

        # Calendar sync now runs in a daemon thread (so a slow Google call
        # can't freeze the save / invite). Wait for it before asserting.
        _join_calendar_sync_threads()

        # Verify job was instantiated with event_id from AmbassadorEvent
        mock_job_class.assert_called_once_with(ambassador_event.event_id)
        # Verify send_to_ambassadors() was called
        mock_job.send_to_ambassadors.assert_called_once()

    @patch('events.jobs.google_calendar_jobs.EventGoogleCalendarJob')
    def test_ambassador_event_signal_calls_job(self, mock_job_class):
        """Test that AmbassadorEvent signal properly instantiates and calls job."""
        # Mock the job instance
        mock_job = MagicMock()
        mock_job_class.return_value = mock_job

        event = Event.objects.create(
            name="Test Event",
            tenant=self.tenant,
            event_type=self.event_type,
            status=self.event_status,
            address="123 Test St",
            request=self.request,
            created_by=self.client_user
        )

        ambassador_event = AmbassadorEvent.objects.create(
            ambassador=self.ambassador,
            event=event,
            tenant=self.tenant,
            created_by=self.client_user
        )

        # Calendar sync now runs in a daemon thread (so a slow Google call
        # can't freeze the save / invite). Wait for it before asserting.
        _join_calendar_sync_threads()

        # Verify job was instantiated with event_id from AmbassadorEvent
        mock_job_class.assert_called_once_with(ambassador_event.event_id)
        # Verify send_to_ambassadors() was called
        mock_job.send_to_ambassadors.assert_called_once()
