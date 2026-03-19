"""
RQ jobs for ambassador event reminders.
"""
import datetime
import logging

import django_rq
from django.db.models import Q
from django.utils import timezone
from django_rq import job
from rq import Retry

from jobs.envelopes import (
    AmbassadorEventReminder3HoursMailer,
    AmbassadorEventReminderMailer,
)
from jobs.models import AmbassadorJob

logger = logging.getLogger(__name__)

REMINDER_24H_SCHEDULE_DESCRIPTION = "jobs.hourly.ambassador_event_reminders.24h"
REMINDER_3H_SCHEDULE_DESCRIPTION = "jobs.hourly.ambassador_event_reminders.3h"
REMINDER_INTERVAL_SECONDS = 60 * 60
REMINDER_ALLOWED_STATUS_SLUGS = {"approved", "accepted"}


def _normalize_offset_minutes(offset_value: int | None) -> int:
    if offset_value is None:
        return 0
    value = int(offset_value)
    if abs(value) > 24:
        return value
    return value * 60


def _to_utc_aware(value: datetime.datetime | None) -> datetime.datetime | None:
    if value is None:
        return None
    if timezone.is_aware(value):
        return value.astimezone(datetime.timezone.utc)
    return value.replace(tzinfo=datetime.timezone.utc)


def _to_event_timezone_offset(
    value: datetime.datetime | None, timezone_offset: int | None
) -> datetime.datetime | None:
    if value is None:
        return None
    offset_minutes = _normalize_offset_minutes(timezone_offset)
    return value + datetime.timedelta(minutes=offset_minutes)


def _next_top_of_hour_utc_naive() -> datetime.datetime:
    """
    Return the next HH:00 execution time in UTC (naive datetime for rq-scheduler).
    """
    now_local = timezone.localtime(timezone.now())
    next_hour_local = (now_local + datetime.timedelta(hours=1)).replace(
        minute=0, second=0, microsecond=0
    )
    next_hour_utc = next_hour_local.astimezone(datetime.timezone.utc)
    return next_hour_utc.replace(tzinfo=None)


def _is_within_hours_window(
    *,
    event_start_utc: datetime.datetime,
    now_utc: datetime.datetime,
    timezone_offset: int | None,
    window_hours: int,
    min_exclusive_hours: int = 0,
) -> bool:
    event_local = _to_event_timezone_offset(event_start_utc, timezone_offset)
    now_local = _to_event_timezone_offset(now_utc, timezone_offset)
    if event_local is None or now_local is None:
        return False

    delta = event_local - now_local
    return (
        datetime.timedelta(hours=min_exclusive_hours) < delta
        <= datetime.timedelta(hours=window_hours)
    )


def _event_start_datetime(ambassador_job: AmbassadorJob) -> datetime.datetime | None:
    event = ambassador_job.job.event
    return event.start_time or event.date or ambassador_job.job.start_date


def _register_hourly_schedule(
    *,
    scheduler,
    description: str,
    func,
    func_name_suffix: str,
) -> str:
    get_jobs = getattr(scheduler, "get_jobs", None)
    if callable(get_jobs):
        for scheduled_job in get_jobs():
            scheduled_func_name = getattr(scheduled_job, "func_name", "") or ""
            scheduled_description = getattr(scheduled_job, "description", "") or ""
            if (
                scheduled_description == description
                or scheduled_func_name.endswith(func_name_suffix)
            ):
                scheduler.cancel(scheduled_job)

    scheduled_job = scheduler.schedule(
        scheduled_time=_next_top_of_hour_utc_naive(),
        func=func,
        interval=REMINDER_INTERVAL_SECONDS,
        repeat=None,
        description=description,
    )
    return scheduled_job.id


def schedule_hourly_ambassador_event_reminders() -> dict[str, str]:
    """
    Register a recurring rq-scheduler job that runs every hour.

    Returns:
        Scheduler job IDs for 24h and 3h reminder jobs.
    """
    scheduler = django_rq.get_scheduler("default")

    reminder_24h_job_id = _register_hourly_schedule(
        scheduler=scheduler,
        description=REMINDER_24H_SCHEDULE_DESCRIPTION,
        func=send_upcoming_ambassador_event_reminders,
        func_name_suffix=".send_upcoming_ambassador_event_reminders",
    )
    reminder_3h_job_id = _register_hourly_schedule(
        scheduler=scheduler,
        description=REMINDER_3H_SCHEDULE_DESCRIPTION,
        func=send_upcoming_ambassador_event_3h_reminders,
        func_name_suffix=".send_upcoming_ambassador_event_3h_reminders",
    )

    logger.info(
        "Registered hourly ambassador event reminder scheduler jobs: 24h=%s, 3h=%s",
        reminder_24h_job_id,
        reminder_3h_job_id,
    )
    return {
        "24h": reminder_24h_job_id,
        "3h": reminder_3h_job_id,
    }


def _send_ambassador_event_reminders(
    *,
    window_hours: int,
    min_exclusive_hours: int,
    reminder_field: str,
    mailer_class,
) -> int:
    now_utc = _to_utc_aware(timezone.now())
    if now_utc is None:
        return 0

    candidates = (
        AmbassadorJob.objects.filter(
            status__slug__in=REMINDER_ALLOWED_STATUS_SLUGS,
            **{f"{reminder_field}__isnull": True},
        )
        .exclude(
            Q(ambassador__user__email__isnull=True)
            | Q(ambassador__user__email="")
        )
        .select_related("status", "ambassador__user", "job__event", "job__event__timezone")
    )

    sent_count = 0
    for ambassador_job in candidates.iterator():
        event_start_utc = _to_utc_aware(_event_start_datetime(ambassador_job))
        if event_start_utc is None:
            continue

        timezone_offset = getattr(
            getattr(ambassador_job.job.event, "timezone", None),
            "offset",
            None,
        )
        if not _is_within_hours_window(
            event_start_utc=event_start_utc,
            now_utc=now_utc,
            timezone_offset=timezone_offset,
            window_hours=window_hours,
            min_exclusive_hours=min_exclusive_hours,
        ):
            continue

        ambassador_user = ambassador_job.ambassador.user
        recipient_email = (ambassador_user.email or "").strip()
        if not recipient_email:
            continue

        mailer = mailer_class(
            ambassador_job=ambassador_job,
            to_emails=[recipient_email],
            recipient_first_name=(ambassador_user.first_name or "").strip() or None,
        )
        mailer.send()

        setattr(ambassador_job, reminder_field, now_utc)
        ambassador_job.save(update_fields=[reminder_field])
        sent_count += 1

    return sent_count


@job("default", retry=Retry(max=3, interval=[60, 120, 240]))
def send_upcoming_ambassador_event_reminders() -> int:
    """
    Send one reminder email to each assigned ambassador when their event starts
    within the next 24 hours in the event's timezone offset.

    Returns:
        Number of reminder emails sent.
    """
    sent_count = _send_ambassador_event_reminders(
        window_hours=24,
        min_exclusive_hours=3,
        reminder_field="reminder_sent_at",
        mailer_class=AmbassadorEventReminderMailer,
    )

    logger.info("Sent %s ambassador 24h reminder email(s)", sent_count)
    return sent_count


@job("default", retry=Retry(max=3, interval=[60, 120, 240]))
def send_upcoming_ambassador_event_3h_reminders() -> int:
    """
    Send one reminder email to each assigned ambassador when their event starts
    within the next 3 hours in the event's timezone offset.

    Returns:
        Number of reminder emails sent.
    """
    sent_count = _send_ambassador_event_reminders(
        window_hours=3,
        min_exclusive_hours=0,
        reminder_field="reminder_3h_sent_at",
        mailer_class=AmbassadorEventReminder3HoursMailer,
    )

    logger.info("Sent %s ambassador 3h reminder email(s)", sent_count)
    return sent_count
