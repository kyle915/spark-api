"""
Celery tasks for Google Calendar synchronization.
"""
import logging
from celery import shared_task
from celery.exceptions import Retry
from django.utils import timezone
from asgiref.sync import sync_to_async

from events.models import Event
from ambassadors.models import AmbassadorEvent
from tenants.models import User, GoogleCalendarConnection
from utils.google_calendar import GoogleCalendarService

logger = logging.getLogger(__name__)


@shared_task(bind=True, max_retries=3, default_retry_delay=60)
def sync_event_to_google_calendar(self, user_id: int, event_id: int):
    """
    Sync an event to a user's Google Calendar.
    
    Args:
        user_id: ID of the user whose calendar to sync to
        event_id: ID of the event to sync
    """
    try:
        user = User.objects.get(id=user_id)
        event = Event.objects.get(id=event_id)
        
        # Check if user has active Google Calendar connection
        try:
            connection = GoogleCalendarConnection.objects.get(
                user=user,
                is_active=True
            )
        except GoogleCalendarConnection.DoesNotExist:
            logger.warning(f"User {user_id} does not have active Google Calendar connection")
            return
        
        # Get event type and status names
        event_type_name = None
        status_name = None
        
        if event.event_type:
            event_type_name = event.event_type.name
        if event.status:
            status_name = event.status.name
        
        # Create calendar service and sync event
        service = GoogleCalendarService(user)
        google_event_id = service.create_event(
            event,
            event_type_name=event_type_name,
            status_name=status_name
        )
        
        if google_event_id:
            logger.info(f"Successfully synced event {event_id} to Google Calendar for user {user_id}")
        else:
            logger.error(f"Failed to sync event {event_id} to Google Calendar for user {user_id}")
            # Retry the task
            raise self.retry(exc=Exception("Failed to create Google Calendar event"))
            
    except User.DoesNotExist:
        logger.error(f"User {user_id} not found")
    except Event.DoesNotExist:
        logger.error(f"Event {event_id} not found")
    except Exception as exc:
        logger.error(f"Error syncing event {event_id} to Google Calendar for user {user_id}: {exc}")
        # Retry with exponential backoff
        raise self.retry(exc=exc, countdown=60 * (2 ** self.request.retries))


@shared_task(bind=True, max_retries=3, default_retry_delay=60)
def update_event_in_google_calendar(self, user_id: int, event_id: int, google_event_id: str):
    """
    Update an event in a user's Google Calendar.
    
    Note: This task requires the Google Calendar event ID, which we're not storing.
    For now, this is a placeholder. In production, you might want to:
    1. Store the Google Calendar event ID when creating events
    2. Or search for the event by summary/description and update it
    
    Args:
        user_id: ID of the user whose calendar to update
        event_id: ID of the event to update
        google_event_id: Google Calendar event ID (optional, will search if not provided)
    """
    try:
        user = User.objects.get(id=user_id)
        event = Event.objects.get(id=event_id)
        
        # Check if user has active Google Calendar connection
        try:
            connection = GoogleCalendarConnection.objects.get(
                user=user,
                is_active=True
            )
        except GoogleCalendarConnection.DoesNotExist:
            logger.warning(f"User {user_id} does not have active Google Calendar connection")
            return
        
        # Get event type and status names
        event_type_name = None
        status_name = None
        
        if event.event_type:
            event_type_name = event.event_type.name
        if event.status:
            status_name = event.status.name
        
        # Create calendar service and update event
        service = GoogleCalendarService(user)
        
        # If google_event_id is not provided, we can't update
        # In a production system, you'd want to store this mapping
        if not google_event_id:
            logger.warning(f"Cannot update event {event_id} without Google Calendar event ID")
            return
        
        success = service.update_event(
            google_event_id,
            event,
            event_type_name=event_type_name,
            status_name=status_name
        )
        
        if success:
            logger.info(f"Successfully updated event {event_id} in Google Calendar for user {user_id}")
        else:
            logger.error(f"Failed to update event {event_id} in Google Calendar for user {user_id}")
            # Retry the task
            raise self.retry(exc=Exception("Failed to update Google Calendar event"))
            
    except User.DoesNotExist:
        logger.error(f"User {user_id} not found")
    except Event.DoesNotExist:
        logger.error(f"Event {event_id} not found")
    except Exception as exc:
        logger.error(f"Error updating event {event_id} in Google Calendar for user {user_id}: {exc}")
        # Retry with exponential backoff
        raise self.retry(exc=exc, countdown=60 * (2 ** self.request.retries))


@shared_task(bind=True, max_retries=3, default_retry_delay=60)
def sync_event_to_all_connected_users(self, event_id: int, tenant_id: int = None):
    """
    Sync an event to all users with active Google Calendar connections in a tenant.
    
    Args:
        event_id: ID of the event to sync
        tenant_id: Optional tenant ID to filter users (if None, syncs to all users)
    """
    try:
        event = Event.objects.get(id=event_id)
        
        # Get all users with active Google Calendar connections
        connections = GoogleCalendarConnection.objects.filter(is_active=True)
        
        if tenant_id:
            # Filter by tenant if provided
            from tenants.models import TenantedUser
            tenant_user_ids = TenantedUser.objects.filter(
                tenant_id=tenant_id,
                is_active=True
            ).values_list('user_id', flat=True)
            connections = connections.filter(user_id__in=tenant_user_ids)
        
        # Sync to each connected user
        for connection in connections:
            sync_event_to_google_calendar.delay(connection.user_id, event_id)
        
        logger.info(f"Queued sync for event {event_id} to {connections.count()} users")
        
    except Event.DoesNotExist:
        logger.error(f"Event {event_id} not found")
    except Exception as exc:
        logger.error(f"Error syncing event {event_id} to all users: {exc}")
        raise self.retry(exc=exc, countdown=60 * (2 ** self.request.retries))

