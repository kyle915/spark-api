"""
RQ jobs for Google Calendar synchronization.
"""
import logging
from django_rq import job
from rq import Retry

from events.models import Event
from tenants.models import User, GoogleCalendarConnection
from tenants.calendar.service import GoogleCalendarService

logger = logging.getLogger(__name__)


def _format_exc(exc: BaseException) -> str:
    """Exception label that stays useful when str(exc) is empty."""
    text = str(exc).strip()
    name = type(exc).__name__
    return f"{name}: {text}" if text else name


@job('default', retry=Retry(max=3, interval=[60, 120, 240]))
def sync_event_to_google_calendar(user_id: int, event_id: int):
    """
    Sync an event to a user's Google Calendar.

    Per-user Google failures (bad token, 403, timeout, missing start_time)
    are logged and swallowed. Cloud Run runs these jobs inline, so raising
    here used to abort the whole fan-out and spam Spark alerts (event 2073
    ×203 on 2026-08-17).
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

        has_start = event.start_time or (
            event.request.start_time if event.request else None
        )
        if not has_start:
            logger.info(
                "Skipping Google Calendar sync for event %s user %s — no start_time",
                event_id,
                user_id,
            )
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
            logger.warning(
                f"Failed to sync event {event_id} to Google Calendar for user {user_id}")

    except User.DoesNotExist:
        logger.warning(f"User {user_id} not found")
    except Event.DoesNotExist:
        logger.warning(f"Event {event_id} not found")
    except Exception as exc:
        logger.warning(
            "Error syncing event %s to Google Calendar for user %s: %s",
            event_id,
            user_id,
            _format_exc(exc),
            exc_info=True,
        )


@job('default', retry=Retry(max=3, interval=[60, 120, 240]))
def sync_event_to_all_connected_users(event_id: int, tenant_id: int = None):
    try:
        from events.jobs.google_calendar_jobs import EventGoogleCalendarJob
        job: EventGoogleCalendarJob = EventGoogleCalendarJob(event_id)
        job.handle()

    except Event.DoesNotExist:
        logger.warning(f"Event {event_id} not found")
    except Exception as exc:
        logger.warning(
            "Error syncing event %s to all users: %s",
            event_id,
            _format_exc(exc),
            exc_info=True,
        )
