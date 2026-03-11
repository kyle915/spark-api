import datetime

from django.conf import settings

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


class RequestorRequestApprovedMailer(Mailer):
    def __init__(
        self,
        request: models.Request,
        location: models.Location | None,
        to_emails: list[str],
        cc_emails: list[str] | None = None,
    ) -> None:
        self.request = request
        self.location = location
        self.to_emails = to_emails
        self.cc_emails = cc_emails or []

    def envelope(self) -> Envelope:
        offset = _get_timezone_offset_minutes(self.request)
        request_id = f"REQ-{self.request.id}" if self.request.id else "-"
        location_name = "-"
        if self.location and self.location.name:
            location_name = self.location.name
            if self.location.state:
                location_name = f"{location_name}, {self.location.state.code}"
        elif self.request.address:
            location_name = self.request.address

        submitted_name = ""
        if self.request.created_by:
            submitted_name = (
                self.request.created_by.get_full_name() or self.request.created_by.email
            )
        if not submitted_name:
            submitted_name = self.request.client_name or "Client user"

        approved_by_name = "-"
        approved_by_email = "-"
        if self.request.approved_by:
            approved_by_name = (
                self.request.approved_by.get_full_name()
                or self.request.approved_by.email
                or "-"
            )
            approved_by_email = self.request.approved_by.email or "-"

        bas_requested = self.request.request_details.count()
        return Envelope(
            subject="Great news - your activation request is approved",
            template="events.templates.emails.request_approved_requestor_notification",
            to_emails=self.to_emails,
            cc_emails=self.cc_emails,
            headers={"Reply-To": "events@igniteproductions.co"},
            from_email=getattr(
                settings,
                "DEFAULT_FROM_EMAIL",
                "Spark by Ignite <no-reply@igniteproductions.co>",
            ),
            context={
                "request": self.request,
                "location": self.location,
                "request_id": request_id,
                "location_name": location_name,
                "submitted_name": submitted_name,
                "approved_by_name": approved_by_name,
                "approved_by_email": approved_by_email,
                "bas_requested": bas_requested,
                "request_date": _format_dt_no_tz(
                    self.request.date, "%m/%d/%Y", offset
                ),
                "request_start_time": _format_dt_no_tz(
                    self.request.start_time, "%I:%M %p", offset
                ),
                "request_end_time": _format_dt_no_tz(
                    self.request.end_time, "%I:%M %p", offset
                ),
            },
        )


class RequestorRequestDeclinedMailer(Mailer):
    def __init__(
        self,
        request: models.Request,
        location: models.Location | None,
        to_emails: list[str],
        cc_emails: list[str] | None = None,
        reviewed_by_name: str | None = None,
        reviewed_by_email: str | None = None,
    ) -> None:
        self.request = request
        self.location = location
        self.to_emails = to_emails
        self.cc_emails = cc_emails or []
        self.reviewed_by_name = reviewed_by_name
        self.reviewed_by_email = reviewed_by_email

    def envelope(self) -> Envelope:
        offset = _get_timezone_offset_minutes(self.request)
        request_id = f"REQ-{self.request.id}" if self.request.id else "-"
        location_name = "-"
        if self.location and self.location.name:
            location_name = self.location.name
            if self.location.state:
                location_name = f"{location_name}, {self.location.state.code}"
        elif self.request.address:
            location_name = self.request.address

        submitted_name = self.request.name or "there"

        return Envelope(
            subject="Update on your activation request - revision needed",
            template="events.templates.emails.request_declined_requestor_notification",
            to_emails=self.to_emails,
            cc_emails=self.cc_emails,
            headers={"Reply-To": "events@igniteproductions.co"},
            from_email=getattr(
                settings,
                "DEFAULT_FROM_EMAIL",
                "Spark by Ignite <no-reply@igniteproductions.co>",
            ),
            context={
                "request": self.request,
                "location": self.location,
                "request_id": request_id,
                "location_name": location_name,
                "submitted_name": submitted_name,
                "reviewed_by_name": self.reviewed_by_name or "-",
                "reviewed_by_email": self.reviewed_by_email or "-",
                "decline_reason": self.request.decline_reason or "",
                "request_date": _format_dt_no_tz(
                    self.request.date, "%m/%d/%Y", offset
                ),
            },
        )


class RequestCreatedNotificationMailer(Mailer):
    def __init__(
        self,
        request: models.Request,
        location: models.Location,
        to_emails: list[str],
        recipient_name: str | None = None,
    ) -> None:
        self.request = request
        self.location = location
        self.to_emails = to_emails
        self.recipient_name = recipient_name

    def envelope(self) -> Envelope:
        offset = _get_timezone_offset_minutes(self.request)
        return Envelope(
            subject="New request created",
            template="events.templates.emails.request_created_notification",
            to_emails=self.to_emails,
            context={
                "request": self.request,
                "location": self.location,
                "recipient_name": self.recipient_name or "",
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


class RequestorRequestAutoApprovedMailer(Mailer):
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
        request_id = f"REQ-{self.request.id}" if self.request.id else "-"
        location_name = "-"
        if self.location and self.location.name:
            location_name = self.location.name
            if self.location.state:
                location_name = f"{location_name}, {self.location.state.code}"
        elif self.request.address:
            location_name = self.request.address

        submitted_name = ""
        if self.request.created_by:
            submitted_name = (
                self.request.created_by.get_full_name() or self.request.created_by.email
            )
        if not submitted_name:
            submitted_name = self.request.client_name or "Client user"
        submitted_email = (
            self.request.requestor_email
            or (self.request.created_by.email if self.request.created_by else "")
            or self.request.client_email
            or "-"
        )
        bas_requested = self.request.request_details.count()
        return Envelope(
            subject="Confirmed - your activation request is locked in",
            template="events.templates.emails.request_auto_approved_requestor_notification",
            to_emails=self.to_emails,
            headers={"Reply-To": "events@igniteproductions.co"},
            from_email=getattr(
                settings,
                "DEFAULT_FROM_EMAIL",
                "Spark by Ignite <no-reply@igniteproductions.co>",
            ),
            context={
                "request": self.request,
                "location": self.location,
                "request_id": request_id,
                "location_name": location_name,
                "submitted_name": submitted_name,
                "submitted_email": submitted_email,
                "bas_requested": bas_requested,
                "request_date": _format_dt_no_tz(
                    self.request.date, "%m/%d/%Y", offset
                ),
                "request_start_time": _format_dt_no_tz(
                    self.request.start_time, "%I:%M %p", offset
                ),
                "request_end_time": _format_dt_no_tz(
                    self.request.end_time, "%I:%M %p", offset
                ),
            },
        )
