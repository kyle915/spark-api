"""
Tests for EventGoogleCalendarJob class.

This module tests:
- EventGoogleCalendarJob initialization
- handle() method
- send_to_admins() method
- send_to_clients() method
- send_to_ambassadors() method
- send_to_user() method
- get_tenanted_users() helper method
- get_event_location() helper method
"""
import pytest
from unittest.mock import patch, MagicMock
from datetime import date, time, timedelta, datetime
from django.contrib.auth import get_user_model
from django.utils import timezone

from tenants.models import Role, Tenant, TenantedUser, GoogleCalendarConnection
from events.models import (
    Event, EventType, EventStatus, Request, RequestType, RequestStatus,
    Client, Distributor, Retailer, Location, State,
    NotificationGroup, NotificationGroupLocation, NotificationGroupUser
)
from ambassadors.models import Ambassador, AmbassadorEvent
from events.jobs.google_calendar_jobs import EventGoogleCalendarJob
from events.tasks import sync_event_to_google_calendar
from utils.utils import ROLE_ID

User = get_user_model()


@pytest.mark.django_db
class TestEventGoogleCalendarJob:
    """Tests for EventGoogleCalendarJob class."""

    def setup_method(self):
        """Set up test data."""
        # Create roles
        self.admin_role, _ = Role.objects.update_or_create(
            slug=Role.SPARK_ADMIN_SLUG,
            defaults={"name": "Spark Admin", "slug": Role.SPARK_ADMIN_SLUG}
        )
        self.client_role, _ = Role.objects.update_or_create(
            slug=Role.CLIENT_SLUG,
            defaults={"name": "Client", "slug": Role.CLIENT_SLUG}
        )
        self.ambassador_role, _ = Role.objects.update_or_create(
            pk=ROLE_ID.Ambassadors,
            defaults={"name": "Ambassador", "slug": Role.AMBASSADOR_SLUG}
        )

        # Create users
        self.admin_user = User.objects.create_user(
            username="admin",
            email="admin@test.com",
            password="testpass123",
            role=self.admin_role
        )
        self.admin_user2 = User.objects.create_user(
            username="admin2",
            email="admin2@test.com",
            password="testpass123",
            role=self.admin_role
        )
        self.client_user = User.objects.create_user(
            username="client",
            email="client@test.com",
            password="testpass123",
            role=self.client_role
        )
        self.client_user2 = User.objects.create_user(
            username="client2",
            email="client2@test.com",
            password="testpass123",
            role=self.client_role
        )
        self.ambassador_user = User.objects.create_user(
            username="ambassador",
            email="ambassador@test.com",
            password="testpass123",
            role=self.ambassador_role
        )
        self.ambassador_user2 = User.objects.create_user(
            username="ambassador2",
            email="ambassador2@test.com",
            password="testpass123",
            role=self.ambassador_role
        )

        # Create tenant
        self.tenant = Tenant.objects.create(
            name="Test Tenant",
            created_by=self.admin_user
        )

        # Create tenanted users
        TenantedUser.objects.create(
            user=self.admin_user,
            tenant=self.tenant,
            is_active=True,
            created_by=self.admin_user
        )
        TenantedUser.objects.create(
            user=self.admin_user2,
            tenant=self.tenant,
            is_active=True,
            created_by=self.admin_user
        )
        TenantedUser.objects.create(
            user=self.client_user,
            tenant=self.tenant,
            is_active=True,
            created_by=self.admin_user
        )
        TenantedUser.objects.create(
            user=self.client_user2,
            tenant=self.tenant,
            is_active=True,
            created_by=self.admin_user
        )
        TenantedUser.objects.create(
            user=self.ambassador_user,
            tenant=self.tenant,
            is_active=True,
            created_by=self.admin_user
        )
        TenantedUser.objects.create(
            user=self.ambassador_user2,
            tenant=self.tenant,
            is_active=True,
            created_by=self.admin_user
        )

        # Create Google Calendar connections
        self.admin_connection = GoogleCalendarConnection.objects.create(
            user=self.admin_user,
            created_by=self.admin_user,
            updated_by=self.admin_user,
            is_active=True,
            calendar_id="primary"
        )
        self.admin_connection.set_access_token("test_token")
        self.admin_connection.set_refresh_token("test_refresh_token")
        self.admin_connection.token_expiry = timezone.now() + timedelta(hours=1)
        self.admin_connection.save()

        self.admin_connection2 = GoogleCalendarConnection.objects.create(
            user=self.admin_user2,
            created_by=self.admin_user2,
            updated_by=self.admin_user2,
            is_active=True,
            calendar_id="primary"
        )
        self.admin_connection2.set_access_token("test_token")
        self.admin_connection2.set_refresh_token("test_refresh_token")
        self.admin_connection2.token_expiry = timezone.now() + timedelta(hours=1)
        self.admin_connection2.save()

        self.client_connection = GoogleCalendarConnection.objects.create(
            user=self.client_user,
            created_by=self.client_user,
            updated_by=self.client_user,
            is_active=True,
            calendar_id="primary"
        )
        self.client_connection.set_access_token("test_token")
        self.client_connection.set_refresh_token("test_refresh_token")
        self.client_connection.token_expiry = timezone.now() + timedelta(hours=1)
        self.client_connection.save()

        # Create state and location
        self.state = State.objects.create(
            name="Test State",
            code="TS",
            created_by=self.admin_user
        )
        self.location = Location.objects.create(
            name="Test Location",
            code="TEST",
            zip="12345",
            state=self.state,
            created_by=self.admin_user
        )

        # Create notification group and relationships
        self.notification_group = NotificationGroup.objects.create(
            name="Test Group",
            state=False,
            created_by=self.admin_user
        )
        NotificationGroupLocation.objects.create(
            location=self.location,
            notification_group=self.notification_group,
            state=self.state,
            created_by=self.admin_user
        )
        NotificationGroupUser.objects.create(
            user=self.client_user,
            notification_group=self.notification_group,
            created_by=self.admin_user
        )

        # Create client, distributor, retailer
        self.client = Client.objects.create(
            name="Test Client",
            email="client@test.com",
            tenant=self.tenant,
            created_by=self.admin_user
        )
        self.distributor = Distributor.objects.create(
            name="Test Distributor",
            email="dist@test.com",
            location=self.location,
            tenant=self.tenant,
            created_by=self.admin_user
        )
        self.retailer = Retailer.objects.create(
            name="Test Retailer",
            address="123 Test St",
            store_contact="Contact",
            location=self.location,
            tenant=self.tenant,
            created_by=self.admin_user
        )

        # Create request type and status
        self.request_type = RequestType.objects.create(
            name="Test Request Type",
            tenant=self.tenant,
            created_by=self.admin_user
        )
        self.request_status = RequestStatus.objects.create(
            name="Approved",
            tenant=self.tenant,
            created_by=self.admin_user
        )

        # Create request
        request_date = timezone.make_aware(
            datetime.combine(date.today(), time.min))
        start_datetime = timezone.make_aware(
            datetime.combine(date.today(), time(10, 0)))
        end_datetime = timezone.make_aware(
            datetime.combine(date.today(), time(12, 0)))

        self.request = Request.objects.create(
            name="Test Request",
            date=request_date,
            start_time=start_datetime,
            end_time=end_datetime,
            address="123 Test St",
            client=self.client,
            distributor=self.distributor,
            retailer=self.retailer,
            request_type=self.request_type,
            status=self.request_status,
            tenant=self.tenant,
            created_by=self.admin_user
        )

        # Create event type and status
        self.event_type = EventType.objects.create(
            name="Test Type",
            tenant=self.tenant,
            created_by=self.admin_user
        )
        self.event_status = EventStatus.objects.create(
            name="Approved",
            tenant=self.tenant,
            created_by=self.admin_user
        )

        # Create event
        self.event = Event.objects.create(
            name="Test Event",
            tenant=self.tenant,
            event_type=self.event_type,
            status=self.event_status,
            address="123 Test St",
            request=self.request,
            created_by=self.admin_user
        )

        # Create ambassadors
        self.ambassador = Ambassador.objects.create(
            user=self.ambassador_user,
            created_by=self.admin_user
        )
        self.ambassador2 = Ambassador.objects.create(
            user=self.ambassador_user2,
            created_by=self.admin_user
        )

        # Create ambassador events
        self.ambassador_event = AmbassadorEvent.objects.create(
            ambassador=self.ambassador,
            event=self.event,
            tenant=self.tenant,
            created_by=self.admin_user
        )
        self.ambassador_event2 = AmbassadorEvent.objects.create(
            ambassador=self.ambassador2,
            event=self.event,
            tenant=self.tenant,
            created_by=self.admin_user
        )

    def test_init_success(self):
        """Test that job initializes correctly with valid event_id."""
        job = EventGoogleCalendarJob(self.event.id)
        assert job.event.id == self.event.id
        assert job.tenant.id == self.tenant.id
        assert job.roles is not None
        assert job.queues is not None
        assert job.logger is not None

    def test_init_event_not_found(self):
        """Test that job raises Event.DoesNotExist for invalid event_id."""
        with pytest.raises(Event.DoesNotExist):
            EventGoogleCalendarJob(99999)

    @patch.object(EventGoogleCalendarJob, 'send_to_admins')
    @patch.object(EventGoogleCalendarJob, 'send_to_clients')
    @patch.object(EventGoogleCalendarJob, 'send_to_ambassadors')
    def test_handle_calls_all_send_methods(self, mock_send_ambassadors, mock_send_clients, mock_send_admins):
        """Test that handle() calls all three send methods."""
        job = EventGoogleCalendarJob(self.event.id)
        job.handle()

        mock_send_admins.assert_called_once()
        mock_send_clients.assert_called_once()
        mock_send_ambassadors.assert_called_once()

    @patch('events.jobs.google_calendar_jobs.Queues')
    def test_send_to_admins_success(self, mock_queues_class):
        """Test sending to admins with active connections."""
        mock_queues = MagicMock()
        mock_queues_class.return_value = mock_queues

        job = EventGoogleCalendarJob(self.event.id)
        job.send_to_admins()

        # Should queue for both admin users with connections
        assert mock_queues.default.add.call_count == 2
        calls = mock_queues.default.add.call_args_list
        assert all(
            call[0][0] == sync_event_to_google_calendar for call in calls)
        user_ids = [call[0][1] for call in calls]
        assert self.admin_user.id in user_ids
        assert self.admin_user2.id in user_ids

    @patch('events.jobs.google_calendar_jobs.Queues')
    def test_send_to_admins_no_connection(self, mock_queues_class):
        """Test that admins without connections are skipped."""
        mock_queues = MagicMock()
        mock_queues_class.return_value = mock_queues

        # Deactivate admin_user2's connection
        self.admin_connection2.is_active = False
        self.admin_connection2.save()

        job = EventGoogleCalendarJob(self.event.id)
        job.send_to_admins()

        # Should only queue for admin_user
        assert mock_queues.default.add.call_count == 1
        call = mock_queues.default.add.call_args_list[0]
        assert call[0][1] == self.admin_user.id

    @patch('events.jobs.google_calendar_jobs.Queues')
    def test_send_to_admins_no_admins(self, mock_queues_class):
        """Test handling when no admins exist."""
        mock_queues = MagicMock()
        mock_queues_class.return_value = mock_queues

        # Deactivate all admin tenanted users
        TenantedUser.objects.filter(
            tenant=self.tenant,
            user__role=self.admin_role
        ).update(is_active=False)

        job = EventGoogleCalendarJob(self.event.id)
        job.send_to_admins()

        # Should not queue anything
        mock_queues.default.add.assert_not_called()

    @patch('events.jobs.google_calendar_jobs.Queues')
    def test_send_to_clients_success(self, mock_queues_class):
        """Test sending to clients via notification groups."""
        mock_queues = MagicMock()
        mock_queues_class.return_value = mock_queues

        job = EventGoogleCalendarJob(self.event.id)
        job.send_to_clients()

        # Should queue for client_user who is in the notification group
        assert mock_queues.default.add.call_count == 1
        call = mock_queues.default.add.call_args_list[0]
        assert call[0][1] == self.client_user.id

    @patch('events.jobs.google_calendar_jobs.Queues')
    def test_send_to_clients_no_location(self, mock_queues_class):
        """Test that clients sync is skipped when event has no location."""
        mock_queues = MagicMock()
        mock_queues_class.return_value = mock_queues

        # Create event without location
        event_no_location = Event.objects.create(
            name="Event No Location",
            tenant=self.tenant,
            event_type=self.event_type,
            status=self.event_status,
            address="123 Test St",
            created_by=self.admin_user
        )

        job = EventGoogleCalendarJob(event_no_location.id)
        job.send_to_clients()

        # Should not queue anything
        mock_queues.default.add.assert_not_called()

    @patch('events.jobs.google_calendar_jobs.Queues')
    def test_send_to_clients_no_group_users(self, mock_queues_class):
        """Test that clients sync is skipped when no NotificationGroupUsers match."""
        mock_queues = MagicMock()
        mock_queues_class.return_value = mock_queues

        # Remove client_user from notification group
        NotificationGroupUser.objects.filter(user=self.client_user).delete()

        job = EventGoogleCalendarJob(self.event.id)
        job.send_to_clients()

        # Should not queue anything
        mock_queues.default.add.assert_not_called()

    @patch('events.jobs.google_calendar_jobs.Queues')
    def test_send_to_clients_location_from_request(self, mock_queues_class):
        """Test that location is retrieved from event.request.retailer.location."""
        mock_queues = MagicMock()
        mock_queues_class.return_value = mock_queues

        job = EventGoogleCalendarJob(self.event.id)
        location = job.get_event_location()

        assert location.id == self.location.id
        assert location == self.request.retailer.location

    @patch('events.jobs.google_calendar_jobs.Queues')
    def test_send_to_clients_location_from_retailer(self, mock_queues_class):
        """Test that location is retrieved from event.retailer.location."""
        mock_queues = MagicMock()
        mock_queues_class.return_value = mock_queues

        # Create event with retailer but no request
        event_with_retailer = Event.objects.create(
            name="Event With Retailer",
            tenant=self.tenant,
            event_type=self.event_type,
            status=self.event_status,
            address="123 Test St",
            retailer=self.retailer,
            created_by=self.admin_user
        )

        job = EventGoogleCalendarJob(event_with_retailer.id)
        location = job.get_event_location()

        assert location.id == self.location.id
        assert location == self.retailer.location

    @patch('events.jobs.google_calendar_jobs.Queues')
    def test_send_to_clients_location_from_distributor(self, mock_queues_class):
        """Test that location is retrieved from event.distributor.location."""
        mock_queues = MagicMock()
        mock_queues_class.return_value = mock_queues

        # Create event with distributor but no request or retailer
        event_with_distributor = Event.objects.create(
            name="Event With Distributor",
            tenant=self.tenant,
            event_type=self.event_type,
            status=self.event_status,
            address="123 Test St",
            distributor=self.distributor,
            created_by=self.admin_user
        )

        job = EventGoogleCalendarJob(event_with_distributor.id)
        location = job.get_event_location()

        assert location.id == self.location.id
        assert location == self.distributor.location

    @patch('events.jobs.google_calendar_jobs.Queues')
    def test_send_to_ambassadors_success(self, mock_queues_class):
        """Test sending to ambassadors via AmbassadorEvent relationships."""
        mock_queues = MagicMock()
        mock_queues_class.return_value = mock_queues

        # Create Google Calendar connections for ambassadors
        ambassador_connection = GoogleCalendarConnection.objects.create(
            user=self.ambassador_user,
            created_by=self.ambassador_user,
            updated_by=self.ambassador_user,
            is_active=True,
            calendar_id="primary"
        )
        ambassador_connection.set_access_token("test_token")
        ambassador_connection.set_refresh_token("test_refresh_token")
        ambassador_connection.token_expiry = timezone.now() + timedelta(hours=1)
        ambassador_connection.save()

        ambassador_connection2 = GoogleCalendarConnection.objects.create(
            user=self.ambassador_user2,
            created_by=self.ambassador_user2,
            updated_by=self.ambassador_user2,
            is_active=True,
            calendar_id="primary"
        )
        ambassador_connection2.set_access_token("test_token")
        ambassador_connection2.set_refresh_token("test_refresh_token")
        ambassador_connection2.token_expiry = timezone.now() + timedelta(hours=1)
        ambassador_connection2.save()

        job = EventGoogleCalendarJob(self.event.id)
        job.send_to_ambassadors()

        # Should queue for both ambassadors
        assert mock_queues.default.add.call_count == 2
        calls = mock_queues.default.add.call_args_list
        assert all(
            call[0][0] == sync_event_to_google_calendar for call in calls)
        user_ids = [call[0][1] for call in calls]
        assert self.ambassador_user.id in user_ids
        assert self.ambassador_user2.id in user_ids

    @patch('events.jobs.google_calendar_jobs.Queues')
    def test_send_to_ambassadors_no_ambassadors(self, mock_queues_class):
        """Test that ambassador sync is skipped when no AmbassadorEvents exist."""
        mock_queues = MagicMock()
        mock_queues_class.return_value = mock_queues

        # Create event without ambassador events
        event_no_ambassadors = Event.objects.create(
            name="Event No Ambassadors",
            tenant=self.tenant,
            event_type=self.event_type,
            status=self.event_status,
            address="123 Test St",
            request=self.request,
            created_by=self.admin_user
        )

        job = EventGoogleCalendarJob(event_no_ambassadors.id)
        job.send_to_ambassadors()

        # Should not queue anything
        mock_queues.default.add.assert_not_called()

    @patch('events.jobs.google_calendar_jobs.Queues')
    def test_send_to_user_with_connection(self, mock_queues_class):
        """Test that send_to_user queues task when user has active connection."""
        mock_queues = MagicMock()
        mock_queues_class.return_value = mock_queues

        job = EventGoogleCalendarJob(self.event.id)
        job.send_to_user(self.admin_user)

        mock_queues.default.add.assert_called_once_with(
            sync_event_to_google_calendar, self.admin_user.id, self.event.id
        )

    @patch('events.jobs.google_calendar_jobs.Queues')
    def test_send_to_user_no_connection(self, mock_queues_class):
        """Test that send_to_user skips when user has no connection."""
        mock_queues = MagicMock()
        mock_queues_class.return_value = mock_queues

        # Create user without connection
        user_no_connection = User.objects.create_user(
            username="noconnection",
            email="noconnection@test.com",
            password="testpass123",
            role=self.admin_role
        )

        job = EventGoogleCalendarJob(self.event.id)
        job.send_to_user(user_no_connection)

        # Should not queue anything
        mock_queues.default.add.assert_not_called()

    @patch('events.jobs.google_calendar_jobs.Queues')
    def test_send_to_user_inactive_connection(self, mock_queues_class):
        """Test that send_to_user skips when connection exists but is inactive."""
        mock_queues = MagicMock()
        mock_queues_class.return_value = mock_queues

        # Deactivate connection
        self.admin_connection.is_active = False
        self.admin_connection.save()

        job = EventGoogleCalendarJob(self.event.id)
        job.send_to_user(self.admin_user)

        # Should not queue anything
        mock_queues.default.add.assert_not_called()

    def test_get_tenanted_users(self):
        """Test that get_tenanted_users filters correctly."""
        job = EventGoogleCalendarJob(self.event.id)
        tenanted_users = job.get_tenanted_users(self.admin_role)

        user_ids = [tu.user.id for tu in tenanted_users]
        assert self.admin_user.id in user_ids
        assert self.admin_user2.id in user_ids
        assert self.client_user.id not in user_ids
        assert self.ambassador_user.id not in user_ids

        # Test with inactive user
        TenantedUser.objects.filter(
            user=self.admin_user2).update(is_active=False)
        tenanted_users = job.get_tenanted_users(self.admin_role)
        user_ids = [tu.user.id for tu in tenanted_users]
        assert self.admin_user.id in user_ids
        assert self.admin_user2.id not in user_ids

    def test_get_event_location_priority(self):
        """Test that get_event_location follows priority: request.retailer > request.distributor > retailer > distributor."""
        job = EventGoogleCalendarJob(self.event.id)

        # Event has request with retailer, should return request.retailer.location
        location = job.get_event_location()
        assert location == self.request.retailer.location

        # Remove retailer from request, should return request.distributor.location
        self.request.retailer = None
        self.request.save()
        self.event.refresh_from_db()

        job = EventGoogleCalendarJob(self.event.id)
        location = job.get_event_location()
        assert location == self.request.distributor.location

        # Remove request, should return retailer.location (if event has retailer)
        self.event.request = None
        self.event.retailer = self.retailer
        self.event.save()

        job = EventGoogleCalendarJob(self.event.id)
        location = job.get_event_location()
        assert location == self.retailer.location

        # Remove retailer, should return distributor.location
        self.event.retailer = None
        self.event.distributor = self.distributor
        self.event.save()

        job = EventGoogleCalendarJob(self.event.id)
        location = job.get_event_location()
        assert location == self.distributor.location

        # Remove all, should return None
        self.event.distributor = None
        self.event.save()

        job = EventGoogleCalendarJob(self.event.id)
        location = job.get_event_location()
        assert location is None
