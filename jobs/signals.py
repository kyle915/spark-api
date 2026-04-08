from django.db.models.signals import post_save, pre_save
from django.dispatch import receiver

from events.models import Event
from jobs.models import AmbassadorJob
from jobs.tasks import (
    reschedule_event_24h_reminders,
    reschedule_event_3h_reminders,
    schedule_ambassador_job_24h_reminder,
    schedule_ambassador_job_3h_reminder,
)


def _event_schedule_signature(event: Event | None) -> tuple:
    if event is None:
        return (None, None, None)
    return (event.date, event.start_time, event.timezone_id)


@receiver(pre_save, sender=Event)
def store_previous_event_schedule(sender, instance: Event, **kwargs):
    if not instance.pk:
        instance._previous_schedule_signature = (None, None, None)
        return

    previous_event = (
        Event.objects.filter(pk=instance.pk)
        .only("date", "start_time", "timezone_id")
        .first()
    )
    instance._previous_schedule_signature = _event_schedule_signature(previous_event)


@receiver(post_save, sender=Event)
def reschedule_event_reminders_on_save(
    sender,
    instance: Event,
    created: bool,
    **kwargs,
):
    previous_signature = getattr(
        instance,
        "_previous_schedule_signature",
        (None, None, None),
    )
    current_signature = _event_schedule_signature(instance)
    if not created and previous_signature == current_signature:
        return

    reschedule_event_3h_reminders(
        instance.id,
        reset_sent_at=not created and previous_signature != current_signature,
    )
    reschedule_event_24h_reminders(
        instance.id,
        reset_sent_at=not created and previous_signature != current_signature,
    )


@receiver(post_save, sender=AmbassadorJob)
def reschedule_ambassador_job_reminder_on_save(
    sender,
    instance: AmbassadorJob,
    **kwargs,
):
    schedule_ambassador_job_24h_reminder(instance.id)
    schedule_ambassador_job_3h_reminder(instance.id)
