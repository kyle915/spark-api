"""
RQ jobs for ambassador event reminders.
"""
import datetime
import logging
from typing import Any

import django_rq
from asgiref.sync import async_to_sync
from django.db.models import Q
from django.utils import timezone
from django_rq import job
from rq import Retry

from jobs.envelopes import (
    AmbassadorEventReminder3HoursMailer,
    AmbassadorEventReminderMailer,
)
from jobs.models import AmbassadorJob
from jobs.notification_rules import (
    _event_end_datetime,
    _event_start_datetime,
    _to_utc_aware,
)
from utils.onesignal import OneSignalError, one_signal_client

logger = logging.getLogger(__name__)

LEGACY_REMINDER_24H_SCHEDULE_DESCRIPTION = "jobs.hourly.ambassador_event_reminders.24h"
LEGACY_REMINDER_3H_SCHEDULE_DESCRIPTION = "jobs.hourly.ambassador_event_reminders.3h"
REMINDER_24H_EXACT_SCHEDULE_PREFIX = "jobs.ambassador_event_reminders.24h"
REMINDER_3H_EXACT_SCHEDULE_PREFIX = "jobs.ambassador_event_reminders.3h"
REMINDER_15M_EXACT_SCHEDULE_PREFIX = "jobs.ambassador_event_reminders.15m"
END_REMINDER_15M_EXACT_SCHEDULE_PREFIX = "jobs.ambassador_event_end_reminders.15m"
REMINDER_ALLOWED_STATUS_SLUGS = {"approved", "accepted"}


def _ambassador_job_schedule_description(
    reminder_prefix: str,
    ambassador_job_id: int,
) -> str:
    return f"{reminder_prefix}.{ambassador_job_id}"


def _matches_ambassador_job_schedule(
    scheduled_job: Any,
    *,
    reminder_prefix: str,
    func_name_suffix: str,
    ambassador_job_id: int,
) -> bool:
    description = getattr(scheduled_job, "description", "") or ""
    if description == _ambassador_job_schedule_description(
        reminder_prefix,
        ambassador_job_id,
    ):
        return True

    args = list(getattr(scheduled_job, "args", []) or [])
    kwargs = dict(getattr(scheduled_job, "kwargs", {}) or {})
    return (
        getattr(scheduled_job, "func_name", "") or ""
    ).endswith(func_name_suffix) and (
        (args and args[0] == ambassador_job_id)
        or kwargs.get("ambassador_job_id") == ambassador_job_id
    )


def _get_ambassador_job_for_reminder(ambassador_job_id: int) -> AmbassadorJob | None:
    return (
        AmbassadorJob.objects.filter(id=ambassador_job_id)
        .exclude(
            Q(ambassador__user__email__isnull=True)
            | Q(ambassador__user__email="")
        )
        .select_related("status", "ambassador__user", "job__event", "job__event__timezone")
        .first()
    )


def _event_trigger_at_hours_before_utc(
    ambassador_job: AmbassadorJob,
    *,
    hours_before: int,
) -> datetime.datetime | None:
    event_start_utc = _to_utc_aware(_event_start_datetime(ambassador_job))
    if event_start_utc is None:
        return None
    return event_start_utc - datetime.timedelta(hours=hours_before)


def _is_redis_conn_error(exc: Exception) -> bool:
    """True when `exc` is (or reads as) a Redis connectivity failure.

    Prod (Cloud Run) has NO Redis — ambassador-job reminders are delivered by
    the cron endpoints (send_activation_reminders et al.), not RQ — so a
    missing Redis here is EXPECTED, not a bug. We log those at DEBUG so the
    backend error monitor never pages on them (they were flooding it: the
    schedule/cancel calls fire on every job save/status change).
    """
    try:
        import redis.exceptions as _rexc

        if isinstance(exc, (_rexc.ConnectionError, _rexc.TimeoutError)):
            return True
    except Exception:  # noqa: BLE001 — redis import should never break this
        pass
    name = type(exc).__name__.lower()
    return "connection" in name or "timeout" in name


def _reminder_scheduler_or_none():
    """The RQ scheduler if Redis is reachable, else None (DEBUG-logged).

    Pings Redis up front so callers bail HERE quietly rather than deep inside a
    .schedule()/.cancel() where the failure would hit an ERROR-level log path
    and the backend error monitor.
    """
    try:
        scheduler = django_rq.get_scheduler("default")
        scheduler.connection.ping()
        return scheduler
    except Exception as exc:  # noqa: BLE001
        logger.debug("RQ scheduler unavailable — skipping reminder op: %s", exc)
        return None


def _log_scheduler_issue(exc: Exception, msg: str, *args) -> None:
    """DEBUG for expected Redis-down; exception (ERROR, paged) otherwise."""
    if _is_redis_conn_error(exc):
        logger.debug("Redis unavailable — " + msg, *args)
    else:
        logger.exception(msg, *args)


def _cancel_ambassador_job_reminder_schedule(
    ambassador_job_id: int,
    *,
    reminder_prefix: str,
    func_name_suffix: str,
    reminder_label: str,
) -> int:
    scheduler = _reminder_scheduler_or_none()
    if scheduler is None:
        return 0
    get_jobs = getattr(scheduler, "get_jobs", None)
    if not callable(get_jobs):
        return 0

    canceled = 0
    try:
        scheduled_jobs = list(get_jobs())
    except Exception as exc:
        _log_scheduler_issue(
            exc,
            "Failed to list scheduled jobs while canceling %s reminder for ambassador_job=%s",
            reminder_label,
            ambassador_job_id,
        )
        return 0

    for scheduled_job in scheduled_jobs:
        if not _matches_ambassador_job_schedule(
            scheduled_job,
            reminder_prefix=reminder_prefix,
            func_name_suffix=func_name_suffix,
            ambassador_job_id=ambassador_job_id,
        ):
            continue
        try:
            scheduler.cancel(scheduled_job)
            canceled += 1
        except Exception as exc:
            _log_scheduler_issue(
                exc,
                "Failed to cancel %s reminder for ambassador_job=%s",
                reminder_label,
                ambassador_job_id,
            )
    return canceled


def cancel_legacy_ambassador_event_reminder_schedules() -> int:
    scheduler = _reminder_scheduler_or_none()
    if scheduler is None:
        return 0
    get_jobs = getattr(scheduler, "get_jobs", None)
    if not callable(get_jobs):
        return 0

    canceled = 0
    try:
        scheduled_jobs = list(get_jobs())
    except Exception as exc:
        _log_scheduler_issue(
            exc, "Failed to list scheduled jobs while canceling legacy reminder schedules"
        )
        return 0

    for scheduled_job in scheduled_jobs:
        description = getattr(scheduled_job, "description", "") or ""
        func_name = getattr(scheduled_job, "func_name", "") or ""
        if description not in {
            LEGACY_REMINDER_24H_SCHEDULE_DESCRIPTION,
            LEGACY_REMINDER_3H_SCHEDULE_DESCRIPTION,
        } and not func_name.endswith(
            (
                ".send_upcoming_ambassador_event_reminders",
                ".send_upcoming_ambassador_event_3h_reminders",
            )
        ):
            continue
        try:
            scheduler.cancel(scheduled_job)
            canceled += 1
        except Exception as exc:
            _log_scheduler_issue(
                exc,
                "Failed to cancel legacy reminder schedule job=%s",
                getattr(scheduled_job, "id", None),
            )
    return canceled


def cancel_ambassador_job_24h_reminder_schedule(ambassador_job_id: int) -> int:
    return _cancel_ambassador_job_reminder_schedule(
        ambassador_job_id,
        reminder_prefix=REMINDER_24H_EXACT_SCHEDULE_PREFIX,
        func_name_suffix=".send_ambassador_job_24h_reminder",
        reminder_label="24h",
    )


def cancel_ambassador_job_3h_reminder_schedule(ambassador_job_id: int) -> int:
    return _cancel_ambassador_job_reminder_schedule(
        ambassador_job_id,
        reminder_prefix=REMINDER_3H_EXACT_SCHEDULE_PREFIX,
        func_name_suffix=".send_ambassador_job_3h_reminder",
        reminder_label="3h",
    )


def cancel_ambassador_job_15m_reminder_schedule(ambassador_job_id: int) -> int:
    return _cancel_ambassador_job_reminder_schedule(
        ambassador_job_id,
        reminder_prefix=REMINDER_15M_EXACT_SCHEDULE_PREFIX,
        func_name_suffix=".send_ambassador_job_15m_reminder_push",
        reminder_label="15m",
    )


def cancel_ambassador_job_end_15m_reminder_schedule(ambassador_job_id: int) -> int:
    return _cancel_ambassador_job_reminder_schedule(
        ambassador_job_id,
        reminder_prefix=END_REMINDER_15M_EXACT_SCHEDULE_PREFIX,
        func_name_suffix=".send_ambassador_job_end_15m_reminder_push",
        reminder_label="end-15m",
    )


def _schedule_ambassador_job_exact_reminder(
    ambassador_job_id: int,
    *,
    reminder_prefix: str,
    hours_before: int,
    reminder_field: str,
    job_func,
    cancel_func,
    reminder_label: str,
) -> str | None:
    ambassador_job = _get_ambassador_job_for_reminder(ambassador_job_id)
    cancel_func(ambassador_job_id)

    if ambassador_job is None:
        return None

    status_slug = (getattr(ambassador_job.status, "slug", None) or "").strip().lower()
    if status_slug not in REMINDER_ALLOWED_STATUS_SLUGS:
        return None
    if getattr(ambassador_job, reminder_field) is not None:
        return None

    trigger_at_utc = _event_trigger_at_hours_before_utc(
        ambassador_job,
        hours_before=hours_before,
    )
    now_utc = _to_utc_aware(timezone.now())
    if trigger_at_utc is None or trigger_at_utc <= now_utc:
        return None

    scheduler = _reminder_scheduler_or_none()
    if scheduler is None:
        return None
    try:
        scheduled_job = scheduler.schedule(
            scheduled_time=trigger_at_utc.astimezone(datetime.timezone.utc).replace(
                tzinfo=None
            ),
            func=job_func,
            args=[ambassador_job_id, trigger_at_utc.isoformat()],
            interval=None,
            repeat=None,
            description=_ambassador_job_schedule_description(
                reminder_prefix,
                ambassador_job_id,
            ),
        )
    except Exception as exc:
        _log_scheduler_issue(
            exc,
            "Failed to schedule exact %s reminder for ambassador_job=%s",
            reminder_label,
            ambassador_job_id,
        )
        return None
    return scheduled_job.id


def schedule_ambassador_job_24h_reminder(ambassador_job_id: int) -> str | None:
    return _schedule_ambassador_job_exact_reminder(
        ambassador_job_id,
        reminder_prefix=REMINDER_24H_EXACT_SCHEDULE_PREFIX,
        hours_before=24,
        reminder_field="reminder_sent_at",
        job_func=send_ambassador_job_24h_reminder,
        cancel_func=cancel_ambassador_job_24h_reminder_schedule,
        reminder_label="24h",
    )


def schedule_ambassador_job_3h_reminder(ambassador_job_id: int) -> str | None:
    return _schedule_ambassador_job_exact_reminder(
        ambassador_job_id,
        reminder_prefix=REMINDER_3H_EXACT_SCHEDULE_PREFIX,
        hours_before=3,
        reminder_field="reminder_3h_sent_at",
        job_func=send_ambassador_job_3h_reminder,
        cancel_func=cancel_ambassador_job_3h_reminder_schedule,
        reminder_label="3h",
    )


def schedule_ambassador_job_15m_reminder(ambassador_job_id: int) -> str | None:
    ambassador_job = _get_ambassador_job_for_reminder(ambassador_job_id)
    cancel_ambassador_job_15m_reminder_schedule(ambassador_job_id)

    if ambassador_job is None:
        return None

    status_slug = (getattr(ambassador_job.status, "slug", None) or "").strip().lower()
    if status_slug not in REMINDER_ALLOWED_STATUS_SLUGS:
        return None
    if getattr(ambassador_job, "reminder_15m_sent_at", None) is not None:
        return None

    event_start_utc = _to_utc_aware(_event_start_datetime(ambassador_job))
    now_utc = _to_utc_aware(timezone.now())
    if event_start_utc is None or now_utc is None:
        return None
    trigger_at_utc = event_start_utc - datetime.timedelta(minutes=15)
    if trigger_at_utc <= now_utc:
        return None

    scheduler = _reminder_scheduler_or_none()
    if scheduler is None:
        return None
    try:
        scheduled_job = scheduler.schedule(
            scheduled_time=trigger_at_utc.astimezone(datetime.timezone.utc).replace(
                tzinfo=None
            ),
            func=send_ambassador_job_15m_reminder_push,
            args=[ambassador_job_id, trigger_at_utc.isoformat()],
            interval=None,
            repeat=None,
            description=_ambassador_job_schedule_description(
                REMINDER_15M_EXACT_SCHEDULE_PREFIX,
                ambassador_job_id,
            ),
        )
    except Exception as exc:
        _log_scheduler_issue(
            exc,
            "Failed to schedule exact 15m reminder for ambassador_job=%s",
            ambassador_job_id,
        )
        return None
    return scheduled_job.id


def schedule_ambassador_job_end_15m_reminder(ambassador_job_id: int) -> str | None:
    ambassador_job = _get_ambassador_job_for_reminder(ambassador_job_id)
    cancel_ambassador_job_end_15m_reminder_schedule(ambassador_job_id)

    if ambassador_job is None:
        return None

    status_slug = (getattr(ambassador_job.status, "slug", None) or "").strip().lower()
    if status_slug not in REMINDER_ALLOWED_STATUS_SLUGS:
        return None
    if getattr(ambassador_job, "reminder_end_15m_sent_at", None) is not None:
        return None

    event_end_utc = _to_utc_aware(_event_end_datetime(ambassador_job))
    now_utc = _to_utc_aware(timezone.now())
    if event_end_utc is None or now_utc is None:
        return None
    trigger_at_utc = event_end_utc + datetime.timedelta(minutes=15)
    if trigger_at_utc <= now_utc:
        return None

    scheduler = _reminder_scheduler_or_none()
    if scheduler is None:
        return None
    try:
        scheduled_job = scheduler.schedule(
            scheduled_time=trigger_at_utc.astimezone(datetime.timezone.utc).replace(
                tzinfo=None
            ),
            func=send_ambassador_job_end_15m_reminder_push,
            args=[ambassador_job_id, trigger_at_utc.isoformat()],
            interval=None,
            repeat=None,
            description=_ambassador_job_schedule_description(
                END_REMINDER_15M_EXACT_SCHEDULE_PREFIX,
                ambassador_job_id,
            ),
        )
    except Exception as exc:
        _log_scheduler_issue(
            exc,
            "Failed to schedule exact end+15m reminder for ambassador_job=%s",
            ambassador_job_id,
        )
        return None
    return scheduled_job.id


def _reschedule_event_exact_reminders(
    event_id: int,
    *,
    reminder_field: str,
    schedule_func,
    reset_sent_at: bool = False,
) -> int:
    ambassador_jobs = list(
        AmbassadorJob.objects.filter(job__event_id=event_id).only("id", reminder_field)
    )
    if reset_sent_at:
        AmbassadorJob.objects.filter(
            id__in=[ambassador_job.id for ambassador_job in ambassador_jobs],
            **{f"{reminder_field}__isnull": False},
        ).update(**{reminder_field: None})

    scheduled_count = 0
    for ambassador_job in ambassador_jobs:
        if schedule_func(ambassador_job.id):
            scheduled_count += 1
    return scheduled_count


def reschedule_event_24h_reminders(
    event_id: int,
    *,
    reset_sent_at: bool = False,
) -> int:
    return _reschedule_event_exact_reminders(
        event_id,
        reminder_field="reminder_sent_at",
        schedule_func=schedule_ambassador_job_24h_reminder,
        reset_sent_at=reset_sent_at,
    )


def reschedule_event_3h_reminders(
    event_id: int,
    *,
    reset_sent_at: bool = False,
) -> int:
    return _reschedule_event_exact_reminders(
        event_id,
        reminder_field="reminder_3h_sent_at",
        schedule_func=schedule_ambassador_job_3h_reminder,
        reset_sent_at=reset_sent_at,
    )


def reschedule_event_15m_reminders(
    event_id: int,
    *,
    reset_sent_at: bool = False,
) -> int:
    return _reschedule_event_exact_reminders(
        event_id,
        reminder_field="reminder_15m_sent_at",
        schedule_func=schedule_ambassador_job_15m_reminder,
        reset_sent_at=reset_sent_at,
    )


def reschedule_event_end_15m_reminders(
    event_id: int,
    *,
    reset_sent_at: bool = False,
) -> int:
    return _reschedule_event_exact_reminders(
        event_id,
        reminder_field="reminder_end_15m_sent_at",
        schedule_func=schedule_ambassador_job_end_15m_reminder,
        reset_sent_at=reset_sent_at,
    )


def backfill_ambassador_job_reminders() -> dict[str, int]:
    now_utc = _to_utc_aware(timezone.now())
    if now_utc is None:
        return {
            "eligible": 0,
            "scheduled_24h": 0,
            "scheduled_3h": 0,
            "scheduled_15m": 0,
            "scheduled_end_15m": 0,
        }

    candidate_ids = list(
        AmbassadorJob.objects.filter(
            status__slug__in=REMINDER_ALLOWED_STATUS_SLUGS,
            job__event__start_time__gt=now_utc,
        )
        .exclude(
            Q(ambassador__user__email__isnull=True)
            | Q(ambassador__user__email="")
        )
        .values_list("id", flat=True)
    )

    scheduled_24h = 0
    scheduled_3h = 0
    scheduled_15m = 0
    scheduled_end_15m = 0
    for ambassador_job_id in candidate_ids:
        if schedule_ambassador_job_24h_reminder(ambassador_job_id):
            scheduled_24h += 1
        if schedule_ambassador_job_3h_reminder(ambassador_job_id):
            scheduled_3h += 1
        if schedule_ambassador_job_15m_reminder(ambassador_job_id):
            scheduled_15m += 1

    candidate_end_ids = list(
        AmbassadorJob.objects.filter(
            status__slug__in=REMINDER_ALLOWED_STATUS_SLUGS,
        )
        .filter(Q(job__event__end_time__gt=now_utc) | Q(job__end_date__gt=now_utc))
        .exclude(
            Q(ambassador__user__email__isnull=True)
            | Q(ambassador__user__email="")
        )
        .values_list("id", flat=True)
    )
    for ambassador_job_id in candidate_end_ids:
        if schedule_ambassador_job_end_15m_reminder(ambassador_job_id):
            scheduled_end_15m += 1

    return {
        "eligible": len(candidate_ids),
        "scheduled_24h": scheduled_24h,
        "scheduled_3h": scheduled_3h,
        "scheduled_15m": scheduled_15m,
        "scheduled_end_15m": scheduled_end_15m,
    }


def _send_ambassador_job_exact_reminder(
    ambassador_job_id: int,
    *,
    expected_trigger_at_iso: str | None,
    hours_before: int,
    reminder_field: str,
    mailer_class,
    reminder_label: str,
) -> int:
    ambassador_job = _get_ambassador_job_for_reminder(ambassador_job_id)
    if ambassador_job is None or getattr(ambassador_job, reminder_field) is not None:
        return 0

    status_slug = (getattr(ambassador_job.status, "slug", None) or "").strip().lower()
    if status_slug not in REMINDER_ALLOWED_STATUS_SLUGS:
        return 0

    current_trigger_at_utc = _event_trigger_at_hours_before_utc(
        ambassador_job,
        hours_before=hours_before,
    )
    if current_trigger_at_utc is None:
        return 0

    if expected_trigger_at_iso:
        expected_trigger_at_utc = datetime.datetime.fromisoformat(expected_trigger_at_iso)
        if current_trigger_at_utc != expected_trigger_at_utc:
            logger.info(
                "Skipping stale %s reminder for ambassador_job=%s: expected=%s current=%s",
                reminder_label,
                ambassador_job_id,
                expected_trigger_at_utc.isoformat(),
                current_trigger_at_utc.isoformat(),
            )
            return 0

    ambassador_user = ambassador_job.ambassador.user
    recipient_email = (ambassador_user.email or "").strip()
    if not recipient_email:
        return 0

    mailer = mailer_class(
        ambassador_job=ambassador_job,
        to_emails=[recipient_email],
        recipient_first_name=(ambassador_user.first_name or "").strip() or None,
    )
    mailer.send()

    setattr(ambassador_job, reminder_field, _to_utc_aware(timezone.now()))
    ambassador_job.save(update_fields=[reminder_field])
    return 1


@job("default", retry=Retry(max=3, interval=[60, 120, 240]))
def send_ambassador_job_24h_reminder(
    ambassador_job_id: int,
    expected_trigger_at_iso: str | None = None,
) -> int:
    return _send_ambassador_job_exact_reminder(
        ambassador_job_id,
        expected_trigger_at_iso=expected_trigger_at_iso,
        hours_before=24,
        reminder_field="reminder_sent_at",
        mailer_class=AmbassadorEventReminderMailer,
        reminder_label="24h",
    )


@job("default", retry=Retry(max=3, interval=[60, 120, 240]))
def send_ambassador_job_3h_reminder(
    ambassador_job_id: int,
    expected_trigger_at_iso: str | None = None,
) -> int:
    return _send_ambassador_job_exact_reminder(
        ambassador_job_id,
        expected_trigger_at_iso=expected_trigger_at_iso,
        hours_before=3,
        reminder_field="reminder_3h_sent_at",
        mailer_class=AmbassadorEventReminder3HoursMailer,
        reminder_label="3h",
    )


@job("default", retry=Retry(max=3, interval=[60, 120, 240]))
def send_ambassador_job_15m_reminder_push(
    ambassador_job_id: int,
    expected_trigger_at_iso: str | None = None,
) -> int:
    ambassador_job = _get_ambassador_job_for_reminder(ambassador_job_id)
    if ambassador_job is None or ambassador_job.reminder_15m_sent_at is not None:
        return 0

    status_slug = (getattr(ambassador_job.status, "slug", None) or "").strip().lower()
    if status_slug not in REMINDER_ALLOWED_STATUS_SLUGS:
        return 0

    current_trigger_at_utc = _event_trigger_at_hours_before_utc(
        ambassador_job,
        hours_before=0,
    )
    if current_trigger_at_utc is None:
        return 0
    current_trigger_at_utc = current_trigger_at_utc - datetime.timedelta(minutes=15)

    if expected_trigger_at_iso:
        expected_trigger_at_utc = datetime.datetime.fromisoformat(expected_trigger_at_iso)
        if current_trigger_at_utc != expected_trigger_at_utc:
            logger.info(
                "Skipping stale %s reminder for ambassador_job=%s: expected=%s current=%s",
                "15m",
                ambassador_job_id,
                expected_trigger_at_utc.isoformat(),
                current_trigger_at_utc.isoformat(),
            )
            return 0

    ambassador_user = ambassador_job.ambassador.user
    user_uuid = getattr(ambassador_user, "uuid", None)
    if not user_uuid:
        return 0

    deep_link = f"spark://my-gigs/{ambassador_job.id}"
    try:
        async_to_sync(one_signal_client.send_push)(
            external_ids=[str(user_uuid)],
            title="Your event starts in 15 minutes",
            message=f"{ambassador_job.job.name} starts soon. Please head to your location.",
            url=deep_link,
            data={
                "type": "event_starting_soon_15m",
                "job_id": str(ambassador_job.job.id),
                "ambassador_job_id": str(ambassador_job.id),
                "deep_link": deep_link,
            },
        )
    except OneSignalError:
        logger.exception(
            "Failed to send 15m reminder push for ambassador_job=%s",
            ambassador_job_id,
        )
        return 0

    ambassador_job.reminder_15m_sent_at = _to_utc_aware(timezone.now())
    ambassador_job.save(update_fields=["reminder_15m_sent_at"])
    return 1


@job("default", retry=Retry(max=3, interval=[60, 120, 240]))
def send_ambassador_job_end_15m_reminder_push(
    ambassador_job_id: int,
    expected_trigger_at_iso: str | None = None,
) -> int:
    from ambassadors.models import Attendance

    ambassador_job = _get_ambassador_job_for_reminder(ambassador_job_id)
    if ambassador_job is None or ambassador_job.reminder_end_15m_sent_at is not None:
        return 0

    status_slug = (getattr(ambassador_job.status, "slug", None) or "").strip().lower()
    if status_slug not in REMINDER_ALLOWED_STATUS_SLUGS:
        return 0

    current_event_end_utc = _to_utc_aware(_event_end_datetime(ambassador_job))
    if current_event_end_utc is None:
        return 0
    current_trigger_at_utc = current_event_end_utc + datetime.timedelta(minutes=15)

    if expected_trigger_at_iso:
        expected_trigger_at_utc = datetime.datetime.fromisoformat(expected_trigger_at_iso)
        if current_trigger_at_utc != expected_trigger_at_utc:
            logger.info(
                "Skipping stale %s reminder for ambassador_job=%s: expected=%s current=%s",
                "end-15m",
                ambassador_job_id,
                expected_trigger_at_utc.isoformat(),
                current_trigger_at_utc.isoformat(),
            )
            return 0

    ambassador_user = ambassador_job.ambassador.user
    user_uuid = getattr(ambassador_user, "uuid", None)
    if not user_uuid:
        return 0

    deep_link = f"spark://my-gigs/{ambassador_job.id}"
    has_clock_out = Attendance.objects.filter(
        ambassador_id=ambassador_job.ambassador_id,
        job_id=ambassador_job.job_id,
        attendace_type__slug="clock_out",
    ).exists()
    if has_clock_out:
        title = "Please upload your recap"
        message = (
            f"{ambassador_job.job.name} ended 15 minutes ago. "
            "Please create your recap."
        )
        push_type = "event_ended_recap_15m"
    else:
        title = "Please clock out and upload recap"
        message = (
            f"{ambassador_job.job.name} ended 15 minutes ago. "
            "Clock out now and create your recap."
        )
        push_type = "event_ended_clock_out_recap_15m"

    try:
        async_to_sync(one_signal_client.send_push)(
            external_ids=[str(user_uuid)],
            title=title,
            message=message,
            url=deep_link,
            data={
                "type": push_type,
                "job_id": str(ambassador_job.job.id),
                "ambassador_job_id": str(ambassador_job.id),
                "deep_link": deep_link,
            },
        )
    except OneSignalError:
        logger.exception(
            "Failed to send end+15m reminder push for ambassador_job=%s",
            ambassador_job_id,
        )
        return 0

    ambassador_job.reminder_end_15m_sent_at = _to_utc_aware(timezone.now())
    ambassador_job.save(update_fields=["reminder_end_15m_sent_at"])
    return 1


@job("default", retry=Retry(max=1, interval=[60]))
def send_upcoming_ambassador_event_reminders() -> int:
    logger.warning(
        "Legacy hourly 24h reminder job executed after exact reminder migration. "
        "Canceling legacy schedules and skipping execution."
    )
    cancel_legacy_ambassador_event_reminder_schedules()
    return 0


@job("default", retry=Retry(max=1, interval=[60]))
def send_upcoming_ambassador_event_3h_reminders() -> int:
    logger.warning(
        "Legacy hourly 3h reminder job executed after exact reminder migration. "
        "Canceling legacy schedules and skipping execution."
    )
    cancel_legacy_ambassador_event_reminder_schedules()
    return 0
