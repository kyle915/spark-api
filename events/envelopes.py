import datetime

from utils.mailer import Envelope, Mailer
from events import models


def _format_dt_no_tz(value: datetime.datetime | None, fmt: str) -> str:
    if not value:
        return ""
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
        return Envelope(
            subject="Event approved",
            template="events.templates.emails.event_approved_notification",
            to_emails=self.to_emails,
            context={
                "event": self.event,
                "location": self.location,
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
        return Envelope(
            subject="Request approved",
            template="events.templates.emails.request_approved_notification",
            to_emails=self.to_emails,
            context={
                "request": self.request,
                "location": self.location,
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
        return Envelope(
            subject="New request created",
            template="events.templates.emails.request_created_notification",
            to_emails=self.to_emails,
            context={
                "request": self.request,
                "location": self.location,
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
        return Envelope(
            subject="New request created",
            template="events.templates.emails.request_created_admin_notification",
            to_emails=self.to_emails,
            context={
                "request": self.request,
                "location": self.location,
                "request_date": _format_dt_no_tz(self.request.date, "%B %d, %Y"),
                "request_start_time": _format_dt_no_tz(
                    self.request.start_time, "%I:%M %p"
                ),
                "request_end_time": _format_dt_no_tz(
                    self.request.end_time, "%I:%M %p"
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
        return Envelope(
            subject="We received your request",
            template="events.templates.emails.request_created_requestor_notification",
            to_emails=self.to_emails,
            context={
                "request": self.request,
                "location": self.location,
            },
        )
