import logging

from django.db.models.signals import post_save, pre_save
from django.dispatch import receiver

from events.models import Event
from jobs.models import AmbassadorJob
from tenants.models import TenantedUser
from jobs.tasks import (
    reschedule_event_end_15m_reminders,
    reschedule_event_15m_reminders,
    reschedule_event_24h_reminders,
    reschedule_event_3h_reminders,
    schedule_ambassador_job_end_15m_reminder,
    schedule_ambassador_job_15m_reminder,
    schedule_ambassador_job_24h_reminder,
    schedule_ambassador_job_3h_reminder,
)

logger = logging.getLogger(__name__)


def _event_schedule_signature(event: Event | None) -> tuple:
    if event is None:
        return (None, None, None, None)
    return (event.date, event.start_time, event.end_time, event.timezone_id)


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
        (None, None, None, None),
    )
    current_signature = _event_schedule_signature(instance)
    if not created and previous_signature == current_signature:
        return

    reschedule_event_3h_reminders(
        instance.id,
        reset_sent_at=not created and previous_signature != current_signature,
    )
    reschedule_event_15m_reminders(
        instance.id,
        reset_sent_at=not created and previous_signature != current_signature,
    )
    reschedule_event_end_15m_reminders(
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
    schedule_ambassador_job_15m_reminder(instance.id)
    schedule_ambassador_job_end_15m_reminder(instance.id)


@receiver(post_save, sender=AmbassadorJob)
def ensure_tenant_membership_on_assignment(
    sender,
    instance: AmbassadorJob,
    created: bool,
    **kwargs,
):
    """Guarantee a BA assigned to a shift appears on that tenant's roster.

    The desktop talent roster is gated on an active TenantedUser
    membership. Historically a BA could be assigned to (and work) a
    tenant's shifts without ever getting that membership — e.g. when they
    onboard via the mobile app / social sign-in, which doesn't create one
    — leaving them invisible on the roster despite having shifts (this is
    how Rocio ended up with 51 Feel Free shifts but no roster entry).

    Creating an AmbassadorJob is the single point every assignment flows
    through, so ensuring the membership here closes the gap for all paths.
    Idempotent (get_or_create); only creates when absent, and never
    reactivates a membership that was deliberately turned off.
    """
    if not created:
        return
    tenant_id = instance.tenant_id
    user_id = getattr(instance.ambassador, "user_id", None)
    if not tenant_id or not user_id:
        return
    try:
        TenantedUser.objects.get_or_create(
            user_id=user_id,
            tenant_id=tenant_id,
            defaults={"is_active": True},
        )
    except Exception:
        # Never let roster bookkeeping break shift assignment.
        logger.exception(
            "Failed to ensure TenantedUser for user=%s tenant=%s from "
            "AmbassadorJob=%s",
            user_id,
            tenant_id,
            instance.id,
        )
