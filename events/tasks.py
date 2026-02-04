"""
RQ jobs for Google Calendar synchronization.
"""
import logging
from django_rq import job
from rq import Retry

from events.models import Event
from ambassadors.models import AmbassadorEvent
from tenants.models import User, GoogleCalendarConnection
from tenants.calendar.service import GoogleCalendarService
from utils.queues import Queues
from utils.utils import ROLE_ID

logger = logging.getLogger(__name__)


@job('default', retry=Retry(max=3, interval=[60, 120, 240]))
def sync_event_to_google_calendar(user_id: int, event_id: int):
    """
    Sync an event to a user's Google Calendar.

    Args:
        user_id: ID of the user whose calendar to sync to
        event_id: ID of the event to sync
    """
    try:
        user = User.objects.get(id=user_id)
        event = Event.objects.get(id=event_id)
        logger.info(
            f"Syncing event {event.name} to Google Calendar for user {user.pk}"
        )

        # Check if user has active Google Calendar connection
        try:
            GoogleCalendarConnection.objects.get(
                user=user,
                is_active=True
            )
        except GoogleCalendarConnection.DoesNotExist:
            logger.warning(
                f"User {user_id} does not have active Google Calendar connection")
            return

        event_type_name = event.event_type.name if event.event_type else None
        status_name = event.status.name if event.status else None
        service = GoogleCalendarService(user)
        google_event_id = service.sync_event(
            event,
            event_type_name=event_type_name,
            status_name=status_name
        )

        if google_event_id:
            logger.info(
                f"Successfully synced event {event_id} to Google Calendar for user {user_id} with google event id {google_event_id}")
        else:
            logger.error(
                f"Failed to sync event {event_id} to Google Calendar for user {user_id}")
            # RQ will automatically retry on exception (up to max retries)
            raise Exception("Failed to create Google Calendar event")

    except User.DoesNotExist:
        logger.error(f"User {user_id} not found")
        # Don't retry on missing user
        raise
    except Event.DoesNotExist:
        logger.error(f"Event {event_id} not found")
        # Don't retry on missing event
        raise
    except Exception as exc:
        logger.error(
            f"Error syncing event {event_id} to Google Calendar for user {user_id}: {exc}")
        # RQ will automatically retry on exception (up to max retries with exponential backoff)
        raise


@job('default', retry=Retry(max=3, interval=[60, 120, 240]))
def sync_event_to_all_connected_users(event_id: int, tenant_id: int = None):
    try:
        from events.jobs.google_calendar_jobs import EventGoogleCalendarJob
        job: EventGoogleCalendarJob = EventGoogleCalendarJob(event_id)
        job.handle()

    except Event.DoesNotExist:
        logger.error(f"Event {event_id} not found")
        # Don't retry on missing event
        raise
    except Exception as exc:
        logger.error(f"Error syncing event {event_id} to all users: {exc}")
        # RQ will automatically retry on exception (up to max retries with exponential backoff)
        raise
