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

from jobs.envelopes import AmbassadorEventReminderMailer
from jobs.models import AmbassadorJob

logger = logging.getLogger(__name__)

REMINDER_SCHEDULE_DESCRIPTION = "jobs.hourly.ambassador_event_reminders"
REMINDER_INTERVAL_SECONDS = 60 * 60
REMINDER_WINDOW_HOURS = 24


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


def _is_within_24_hours_window(
    *,
    event_start_utc: datetime.datetime,
    now_utc: datetime.datetime,
    timezone_offset: int | None,
) -> bool:
    event_local = _to_event_timezone_offset(event_start_utc, timezone_offset)
    now_local = _to_event_timezone_offset(now_utc, timezone_offset)
    if event_local is None or now_local is None:
        return False

    delta = event_local - now_local
    return datetime.timedelta(0) <= delta <= datetime.timedelta(hours=REMINDER_WINDOW_HOURS)


def _event_start_datetime(ambassador_job: AmbassadorJob) -> datetime.datetime | None:
    event = ambassador_job.job.event
    return event.start_time or event.date or ambassador_job.job.start_date


def schedule_hourly_ambassador_event_reminders() -> str:
    """
    Register a recurring rq-scheduler job that runs every hour.

    Returns:
        The scheduler job ID.
    """
    scheduler = django_rq.get_scheduler("default")

    get_jobs = getattr(scheduler, "get_jobs", None)
    if callable(get_jobs):
        for scheduled_job in get_jobs():
            func_name = getattr(scheduled_job, "func_name", "") or ""
            description = getattr(scheduled_job, "description", "") or ""
            if (
                description == REMINDER_SCHEDULE_DESCRIPTION
                or func_name.endswith(".send_upcoming_ambassador_event_reminders")
            ):
                scheduler.cancel(scheduled_job)

    scheduled_job = scheduler.schedule(
        scheduled_time=_next_top_of_hour_utc_naive(),
        func=send_upcoming_ambassador_event_reminders,
        interval=REMINDER_INTERVAL_SECONDS,
        repeat=None,
        description=REMINDER_SCHEDULE_DESCRIPTION,
    )

    logger.info(
        "Registered hourly ambassador event reminder scheduler job id=%s",
        scheduled_job.id,
    )
    return scheduled_job.id


@job("default", retry=Retry(max=3, interval=[60, 120, 240]))
def send_upcoming_ambassador_event_reminders() -> int:
    """
    Send one reminder email to each assigned ambassador when their event starts
    within the next 24 hours in the event's timezone offset.

    Returns:
        Number of reminder emails sent.
    """
    now_utc = _to_utc_aware(timezone.now())
    if now_utc is None:
        return 0

    candidates = (
        AmbassadorJob.objects.filter(
            reminder_sent_at__isnull=True,
            status__slug="approved",
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

        timezone_offset = getattr(getattr(ambassador_job.job.event, "timezone", None), "offset", None)
        if not _is_within_24_hours_window(
            event_start_utc=event_start_utc,
            now_utc=now_utc,
            timezone_offset=timezone_offset,
        ):
            continue

        ambassador_user = ambassador_job.ambassador.user
        recipient_email = (ambassador_user.email or "").strip()
        if not recipient_email:
            continue

        mailer = AmbassadorEventReminderMailer(
            ambassador_job=ambassador_job,
            to_emails=[recipient_email],
            recipient_first_name=(ambassador_user.first_name or "").strip() or None,
        )
        mailer.send()

        ambassador_job.reminder_sent_at = now_utc
        ambassador_job.save(update_fields=["reminder_sent_at"])
        sent_count += 1

    logger.info("Sent %s ambassador event reminder email(s)", sent_count)
    return sent_count
