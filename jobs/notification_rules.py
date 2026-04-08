import datetime

from django.utils import timezone

from jobs.models import AmbassadorJob


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


def _event_start_datetime(ambassador_job: AmbassadorJob) -> datetime.datetime | None:
    event = ambassador_job.job.event
    return event.start_time or ambassador_job.job.start_date


def should_send_ambassador_event_email(
    ambassador_job: AmbassadorJob,
    *,
    now: datetime.datetime | None = None,
) -> bool:
    event_start_utc = _to_utc_aware(_event_start_datetime(ambassador_job))
    now_utc = _to_utc_aware(now or timezone.now())
    if event_start_utc is None or now_utc is None:
        return True

    timezone_offset = getattr(
        getattr(ambassador_job.job.event, "timezone", None),
        "offset",
        None,
    )
    event_local = _to_event_timezone_offset(event_start_utc, timezone_offset)
    now_local = _to_event_timezone_offset(now_utc, timezone_offset)
    if event_local is None or now_local is None:
        return True

    return event_local.date() >= now_local.date()
