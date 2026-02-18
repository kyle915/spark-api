import datetime

from utils.mailer import Envelope, Mailer
from events import models


def _apply_offset(
    value: datetime.datetime | None, offset_minutes: int
) -> datetime.datetime | None:
    if not value:
        return None
    return value + datetime.timedelta(minutes=offset_minutes)


def _get_timezone_offset_minutes(obj) -> int:
    """Return timezone offset (minutes) for event/request, default 0."""
    try:
        tz = getattr(obj, "timezone", None)
        if tz is not None and tz.offset is not None:
            return int(tz.offset)
    except Exception:
        pass

    tz_id = getattr(obj, "timezone_id", None)
    if tz_id:
        try:
            offset = (
                models.TimeZone.objects.filter(id=tz_id)
                .values_list("offset", flat=True)
                .first()
            )
            return int(offset) if offset is not None else 0
        except Exception:
            return 0
    return 0


def _format_dt_no_tz(
    value: datetime.datetime | None, fmt: str, offset_minutes: int = 0
) -> str:
    if not value:
        return ""
    value = _apply_offset(value, offset_minutes) or value
    formatted = value.replace(tzinfo=None).strftime(fmt)
    if fmt.startswith("%I"):
        return formatted.lstrip("0")
    return formatted


class EventApprovedNotificationMailer(Mailer):
    def __init__(
        self,
        event: models.Event,
        location: models.Location,
        to_emails: list[str],
    ) -> None:
        self.event = event
        self.location = location
        self.to_emails = to_emails

    def envelope(self) -> Envelope:
        offset = _get_timezone_offset_minutes(self.event)
        return Envelope(
            subject="Event approved",
            template="events.templates.emails.event_approved_notification",
            to_emails=self.to_emails,
            context={
                "event": self.event,
                "location": self.location,
                "event_date": _format_dt_no_tz(self.event.date, "%B %d, %Y", offset),
                "event_start_time": _format_dt_no_tz(
                    self.event.start_time, "%I:%M %p", offset
                ),
                "event_end_time": _format_dt_no_tz(
                    self.event.end_time, "%I:%M %p", offset
                ),
            },
        )


class RequestApprovedNotificationMailer(Mailer):
    def __init__(
        self,
        request: models.Request,
        location: models.Location,
        to_emails: list[str],
    ) -> None:
        self.request = request
        self.location = location
        self.to_emails = to_emails

    def envelope(self) -> Envelope:
        offset = _get_timezone_offset_minutes(self.request)
        return Envelope(
            subject="Request approved",
            template="events.templates.emails.request_approved_notification",
            to_emails=self.to_emails,
            context={
                "request": self.request,
                "location": self.location,
                "request_date": _format_dt_no_tz(
                    self.request.date, "%B %d, %Y", offset
                ),
                "request_start_time": _format_dt_no_tz(
                    self.request.start_time, "%I:%M %p", offset
                ),
                "request_end_time": _format_dt_no_tz(
                    self.request.end_time, "%I:%M %p", offset
                ),
            },
        )


class RequestCreatedNotificationMailer(Mailer):
    def __init__(
        self,
        request: models.Request,
        location: models.Location,
        to_emails: list[str],
    ) -> None:
        self.request = request
        self.location = location
        self.to_emails = to_emails

    def envelope(self) -> Envelope:
        offset = _get_timezone_offset_minutes(self.request)
        return Envelope(
            subject="New request created",
            template="events.templates.emails.request_created_notification",
            to_emails=self.to_emails,
            context={
                "request": self.request,
                "location": self.location,
                "request_date": _format_dt_no_tz(
                    self.request.date, "%B %d, %Y", offset
                ),
                "request_start_time": _format_dt_no_tz(
                    self.request.start_time, "%I:%M %p", offset
                ),
                "request_end_time": _format_dt_no_tz(
                    self.request.end_time, "%I:%M %p", offset
                ),
            },
        )


class ClientRequestCreatedNotificationMailer(Mailer):
    def __init__(
        self,
        request: models.Request,
        location: models.Location | None,
        to_emails: list[str],
    ) -> None:
        self.request = request
        self.location = location
        self.to_emails = to_emails

    def envelope(self) -> Envelope:
        offset = _get_timezone_offset_minutes(self.request)
        return Envelope(
            subject="New request created",
            template="events.templates.emails.request_created_admin_notification",
            to_emails=self.to_emails,
            context={
                "request": self.request,
                "location": self.location,
                "request_date": _format_dt_no_tz(
                    self.request.date, "%B %d, %Y", offset
                ),
                "request_start_time": _format_dt_no_tz(
                    self.request.start_time, "%I:%M %p", offset
                ),
                "request_end_time": _format_dt_no_tz(
                    self.request.end_time, "%I:%M %p", offset
                ),
            },
        )


class RequestorRequestCreatedMailer(Mailer):
    def __init__(
        self,
        request: models.Request,
        location: models.Location | None,
        to_emails: list[str],
    ) -> None:
        self.request = request
        self.location = location
        self.to_emails = to_emails

    def envelope(self) -> Envelope:
        offset = _get_timezone_offset_minutes(self.request)
        return Envelope(
            subject="We received your request",
            template="events.templates.emails.request_created_requestor_notification",
            to_emails=self.to_emails,
            context={
                "request": self.request,
                "location": self.location,
                "request_date": _format_dt_no_tz(
                    self.request.date, "%B %d, %Y", offset
                ),
                "request_start_time": _format_dt_no_tz(
                    self.request.start_time, "%I:%M %p", offset
                ),
                "request_end_time": _format_dt_no_tz(
                    self.request.end_time, "%I:%M %p", offset
                ),
            },
        )
