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

# Batch size for processing connections to avoid memory issues
CONNECTION_BATCH_SIZE = 50


def _user_should_receive_event(user: User, event: Event) -> bool:
    """
    Determine whether a user should receive a calendar event for the given Event.

    Rules:
    1. Spark Admin or Client:
       - Event must be approved (status.slug == 'approved')
       - Event must have an associated request
    2. Ambassador:
       - There must be an approved AmbassadorEvent relationship for this
         user and this event in the current tenant (is_approved=True).
    """
    role = getattr(user, "role", None)
    if not role:
        logger.info(
            "Skipping Google Calendar sync for user %s and event %s: "
            "user has no role assigned.",
            user.id,
            event.id,
        )
        return False

    # Check if event has a request
    if not event.request:
        logger.info(
            "Skipping Google Calendar sync for user %s and event %s: "
            "event has no request.",
            user.id,
            event.id,
        )
        return False

    # Check if event has an approved status
    if not event.status or event.status.slug != "approved":
        logger.info(
            "Skipping Google Calendar sync for user %s and event %s: "
            "event is not approved.",
            user.id,
            event.id,
        )
        return False

    # Ambassador
    if role._is_ambassador:
        has_approved_link = AmbassadorEvent.objects.filter(
            ambassador__user=user,
            event_id=event.id,
            tenant_id=event.tenant_id,
            is_approved=True,
        ).exists()
        if not has_approved_link:
            logger.info(
                "Skipping Google Calendar sync for ambassador user %s and event %s: "
                "no approved AmbassadorEvent found for this tenant.",
                user.id,
                event.id,
            )
            return False

    return True


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

        # Role and business rules: decide if this user should receive this event
        if not _user_should_receive_event(user, event):
            return

        # Get event type and status names
        event_type_name = None
        status_name = None

        if event.event_type:
            event_type_name = event.event_type.name
        if event.status:
            status_name = event.status.name

        # Validate event has request (required for building calendar payload)
        if not event.request:
            logger.error(
                f"Event {event_id} must have a request to sync to Google Calendar")
            return

        # Create calendar service and sync event (creates or updates)
        service = GoogleCalendarService(user)
        google_event_id = service.sync_event(
            event,
            event_type_name=event_type_name,
            status_name=status_name
        )

        if google_event_id:
            logger.info(
                f"Successfully synced event {event_id} to Google Calendar for user {user_id}")
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
def update_event_in_google_calendar(user_id: int, event_id: int, google_event_id: str = None):
    """
    Update an event in a user's Google Calendar.
    Uses sync_event which will update if mapping exists, or create if it doesn't.

    Args:
        user_id: ID of the user whose calendar to update
        event_id: ID of the event to update
        google_event_id: Google Calendar event ID (optional, will use mapping if not provided)
    """
    try:
        user = User.objects.get(id=user_id)
        event = Event.objects.get(id=event_id)

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

        # Role and business rules: decide if this user should receive this event
        if not _user_should_receive_event(user, event):
            return

        # Validate event has request
        if not event.request:
            logger.error(
                f"Event {event_id} must have a request to sync to Google Calendar")
            return

        # Get event type and status names
        event_type_name = None
        status_name = None

        if event.event_type:
            event_type_name = event.event_type.name
        if event.status:
            status_name = event.status.name

        # Create calendar service and sync event (will update if mapping exists)
        service = GoogleCalendarService(user)
        google_event_id = service.sync_event(
            event,
            event_type_name=event_type_name,
            status_name=status_name
        )

        if google_event_id:
            logger.info(
                f"Successfully synced/updated event {event_id} in Google Calendar for user {user_id}")
        else:
            logger.error(
                f"Failed to sync/update event {event_id} in Google Calendar for user {user_id}")
            # RQ will automatically retry on exception (up to max retries)
            raise Exception("Failed to update Google Calendar event")

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
            f"Error updating event {event_id} in Google Calendar for user {user_id}: {exc}")
        # RQ will automatically retry on exception (up to max retries with exponential backoff)
        raise


@job('default', retry=Retry(max=3, interval=[60, 120, 240]))
def sync_event_to_all_connected_users(event_id: int, tenant_id: int = None):
    """
    Sync an event to all users with active Google Calendar connections in a tenant.

    Args:
        event_id: ID of the event to sync
        tenant_id: Optional tenant ID to filter users (if None, syncs to all users)
    """
    try:
        connections_qs = GoogleCalendarConnection.objects.filter(
            is_active=True
        ).values_list('user_id', flat=True)

        if tenant_id:
            connections_qs = connections_qs.filter(
                user__tenanted_users__tenant_id=tenant_id,
                user__tenanted_users__is_active=True
            ).distinct()

        queues: Queues = Queues()
        offset = 0
        total_queued = 0
        while True:
            user_ids_page = list(
                connections_qs[offset:offset + CONNECTION_BATCH_SIZE]
            )

            if not user_ids_page:
                break

            for user_id in user_ids_page:
                queues.default.add(
                    sync_event_to_google_calendar, user_id, event_id)

            total_queued += len(user_ids_page)
            offset += CONNECTION_BATCH_SIZE

            logger.debug(
                f"Queued batch of {len(user_ids_page)} users for event {event_id} "
                f"(total queued: {total_queued})")

        logger.info(
            f"Queued sync for event {event_id} to {total_queued} users with active Google Calendar connections")

    except Event.DoesNotExist:
        logger.error(f"Event {event_id} not found")
        # Don't retry on missing event
        raise
    except Exception as exc:
        logger.error(f"Error syncing event {event_id} to all users: {exc}")
        # RQ will automatically retry on exception (up to max retries with exponential backoff)
        raise
