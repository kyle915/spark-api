"""
Django signals for Google Calendar synchronization and mobile push.

Calendar sync (existing) keeps Google Calendar in step with Event /
AmbassadorEvent rows. Push notifications (new) fire across three
moments BAs care about:

  - shift-offer: BA was invited to an Event (AmbassadorEvent created)
  - activation reminder: 15 min before Event.start_time, once the
    invite is accepted (is_approved=True)
  - recap nudge: 4 hours after Event.end_time, if the BA hasn't filed
    a Recap yet (worker re-checks state at fire time)

All push paths are best-effort — the queue layer can fail (no Redis on
Cloud Run by default) without aborting Event/AmbassadorEvent saves.
"""
import datetime
import logging

from django.db.models.signals import post_save
from django.dispatch import receiver

from events.models import Event
from ambassadors.models import AmbassadorEvent
from events.tasks import sync_event_to_all_connected_users
from utils.queues import Queues

logger = logging.getLogger(__name__)
queues: Queues = Queues()

# How far before start_time we ping the BA. Keep aligned with what the
# mobile app's location tracker considers "activation window."
ACTIVATION_REMINDER_LEAD = datetime.timedelta(minutes=15)

# How long after end_time we wait before nudging an unfiled recap.
# Long enough that BAs who file from the parking lot don't get pinged;
# short enough that the nudge still feels relevant.
RECAP_NUDGE_DELAY = datetime.timedelta(hours=4)


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


@receiver(post_save, sender=AmbassadorEvent)
def push_on_ambassador_event_change(
    sender, instance: AmbassadorEvent, created: bool, **kwargs
):
    """Fan out push notifications for shift offers + activation + recap.

    Best-effort — wrapped so any failure (no Redis, no devices, no
    user, missing event start_time) is logged and dropped.
    """
    try:
        from ambassadors.push import (
            enqueue_push,
            schedule_push_at,
            schedule_recap_nudge_at,
        )

        ambassador = getattr(instance, "ambassador", None)
        user = getattr(ambassador, "user", None) if ambassador else None
        if not user:
            return

        event = getattr(instance, "event", None)
        if not event:
            return

        event_name = (event.name or "your upcoming shift")[:80]
        deep_link_data = {
            "screen": "shifts",
            "eventUuid": str(event.uuid),
            "ambassadorEventUuid": str(instance.uuid),
        }

        if created:
            # Shift offer — invited but not yet approved.
            enqueue_push(
                user.id,
                title="New shift offered",
                body=event_name,
                data=deep_link_data,
            )

        # If the invite has been approved AND the event has a start_time,
        # schedule the activation reminder + recap nudge. update_or_create
        # paths hit post_save with created=False, so we wire from both.
        if instance.is_approved and event.start_time:
            schedule_push_at(
                event.start_time - ACTIVATION_REMINDER_LEAD,
                user.id,
                title="Your shift starts in 15 minutes",
                body=event_name,
                data=deep_link_data,
            )
            if event.end_time:
                schedule_recap_nudge_at(
                    event.end_time + RECAP_NUDGE_DELAY,
                    user.id,
                    ambassador.id,
                    event.id,
                    title="Don't forget your recap",
                    body=f"Submit your recap for {event_name}",
                    data={
                        "screen": "recap",
                        "eventUuid": str(event.uuid),
                    },
                )
    except Exception as exc:
        logger.warning(
            "push wiring failed for ambassador_event=%s: %s", instance.id, exc
        )
