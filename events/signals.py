"""
Django signals for Google Calendar synchronization.
"""
import logging
from django.db.models.signals import post_save
from django.dispatch import receiver
from asgiref.sync import sync_to_async

from events.models import Event
from ambassadors.models import AmbassadorEvent
from events.tasks import sync_event_to_google_calendar, sync_event_to_all_connected_users
from utils.queues import Queues

logger = logging.getLogger(__name__)
queues: Queues = Queues()


@receiver(post_save, sender=Event)
def sync_event_on_create_or_update(sender, instance: Event, created: bool, **kwargs):
    """
    Signal handler for Event model post_save.

    For new events:
    - If user is NOT ambassador: sync to all connected users in tenant
    - If user IS ambassador: skip (handled by AmbassadorEvent signal)

    For updates:
    - Sync updates to all connected calendars (if we had stored Google event IDs)
    - For now, we'll just log that an update occurred
    """
    if created:
        # New event created
        try:
            # Check if the creator is an ambassador
            user = instance.created_by
            if user and user.role:
                # Check if role slug is 'ambassador'
                # Note: is_ambassador is an async property, so we need to handle it differently
                role_slug = user.role.slug
                is_ambassador = role_slug == 'ambassador'

                if not is_ambassador:
                    # Not an ambassador, sync to all connected users in tenant
                    logger.info(
                        f"Event {instance.id} created by non-ambassador, syncing to all connected users")
                    queues.default.add(
                        sync_event_to_all_connected_users, instance.id, instance.tenant_id)
                else:
                    # Ambassador - will be handled by AmbassadorEvent signal
                    logger.debug(
                        f"Event {instance.id} created by ambassador, skipping sync (will be handled by AmbassadorEvent)")
            else:
                # No user or role, sync to all connected users as fallback
                logger.warning(
                    f"Event {instance.id} created without user/role, syncing to all connected users")
                queues.default.add(
                    sync_event_to_all_connected_users, instance.id, instance.tenant_id)

        except Exception as e:
            logger.error(
                f"Error in sync_event_on_create_or_update for event {instance.id}: {e}")
    else:
        # Event updated - sync to all users who have this event in their calendar
        try:
            from events.models import GoogleCalendarEvent

            # Get all users who have this event synced to their calendar
            mappings = GoogleCalendarEvent.objects.filter(event=instance)

            if mappings.exists():
                logger.info(
                    f"Event {instance.id} updated, syncing to {mappings.count()} users with this event in their calendar")

                # Sync update to each user who has this event
                for mapping in mappings:
                    queues.default.add(
                        sync_event_to_google_calendar, mapping.user_id, instance.id)

            else:
                logger.debug(
                    f"Event {instance.id} updated but no users have it synced to their calendar")
        except Exception as e:
            logger.error(
                f"Error syncing event update for event {instance.id}: {e}")


@receiver(post_save, sender=AmbassadorEvent)
def sync_event_for_ambassador(sender, instance: AmbassadorEvent, created: bool, **kwargs):
    """
    Signal handler for AmbassadorEvent model post_save.

    When an AmbassadorEvent is created:
    - Check if created_by user is ambassador
    - If yes, sync the event to that ambassador's Google Calendar
    """
    if created:
        try:
            user = instance.created_by
            if user and user.role:
                # Check if role slug is 'ambassador'
                role_slug = user.role.slug
                is_ambassador = role_slug == 'ambassador'

                if is_ambassador:
                    # Sync event to this ambassador's Google Calendar
                    logger.info(
                        f"AmbassadorEvent {instance.id} created for ambassador {user.id}, syncing event {instance.event_id}")
                    queues.default.add(
                        sync_event_to_google_calendar, user.id, instance.event_id)
                else:
                    logger.debug(
                        f"AmbassadorEvent {instance.id} created by non-ambassador user {user.id}, skipping sync")
            else:
                logger.warning(
                    f"AmbassadorEvent {instance.id} created without user/role")
        except Exception as e:
            logger.error(
                f"Error in sync_event_for_ambassador for AmbassadorEvent {instance.id}: {e}")
