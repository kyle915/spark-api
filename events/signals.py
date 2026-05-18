"""
Django signals for Google Calendar synchronization.
"""
import logging
from django.db.models.signals import post_save
from django.dispatch import receiver

from events.models import Event
from ambassadors.models import AmbassadorEvent
from events.tasks import sync_event_to_all_connected_users
from utils.queues import Queues

logger = logging.getLogger(__name__)
queues: Queues = Queues()


@receiver(post_save, sender=Event)
def sync_event_on_create_or_update(sender, instance: Event, created: bool, **kwargs):
    user = instance.created_by
    # If the user is an ambassador, skip the sync
    if user and user.role and user.role._is_ambassador:
        logger.info(
            f"Event {instance.id} created by ambassador, skipping sync (will be handled by AmbassadorEvent)")
        return

    # Calendar sync is best-effort. Cloud Run has no Redis, so RQ enqueue
    # will raise — we don't want that to bubble up and abort Event.save()
    # (which would break approve_request, create_event, etc).
    try:
        queues.default.add(sync_event_to_all_connected_users, instance.id)
    except Exception as exc:
        logger.warning(
            f"Skipping calendar sync for event {instance.id} — queue unavailable: {exc}"
        )


@receiver(post_save, sender=AmbassadorEvent)
def sync_event_for_ambassador(sender, instance: AmbassadorEvent, created: bool, **kwargs):
    # Same best-effort posture — don't let calendar sync abort the
    # ambassador-event save path.
    try:
        from events.jobs.google_calendar_jobs import EventGoogleCalendarJob
        job: EventGoogleCalendarJob = EventGoogleCalendarJob(instance.event_id)
        job.send_to_ambassadors()
    except Exception as exc:
        logger.warning(
            f"Skipping ambassador calendar sync for event {instance.event_id}: {exc}"
        )
