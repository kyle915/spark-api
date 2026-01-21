from utils.mailer import Envelope, Mailer
from events import models


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
