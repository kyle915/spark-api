import resend
from typing import Any
from asgiref.sync import sync_to_async
import logging

from django.conf import settings
from django.template.loader import get_template as django_get_template
from django.template import Template
from django_rq import job
from django.core.mail import EmailMultiAlternatives
from rq import Retry

from utils.queues import Queues

resend.api_key = settings.RESEND_API_KEY

logger = logging.getLogger(__name__)


class Envelope:
    subject: str = "Spark Notification"
    template: str = ""
    context: dict = {}
    from_email: str = settings.DEFAULT_FROM_EMAIL
    to_emails: list[str] = []
    headers: dict = {}

    def __init__(self, **kwargs: Any) -> None:
        """
        Initialize the Envelope class.
        """
        self.subject = kwargs.get("subject", self.subject)
        self.template = kwargs.get("template", self.template)
        self.context = kwargs.get("context", self.context)
        self.from_email = kwargs.get("from_email", self.from_email)
        self.to_emails = kwargs.get("to_emails", self.to_emails)
        self.headers = kwargs.get("headers", self.headers)

    def get_template(self) -> Template:
        """
        Get the Django template object (not rendered).
        """
        if not self.template:
            raise ValueError("Template is required")

        template_path = self.template.split(".")

        if len(template_path) < 3 or template_path[1] != "templates":
            raise ValueError(
                f"Invalid template path format: {self.template}. Expected format: 'app.templates.path.to.template'")

        template_parts = template_path[2:]
        template_name = "/".join(template_parts) + ".html"

        return django_get_template(template_name)

    def render_template(self) -> str:
        """
        Render the template with the context.
        """
        template = self.get_template()
        return template.render(self.context or {})

    def compile(self) -> dict:
        """
        Compile the envelope to a dictionary.
        """
        return {
            "from": self.from_email,
            "to": self.to_emails,
            "subject": self.subject,
            "html": self.render_template(),
            "template": self.template,
            "headers": self.headers,
        }

    @staticmethod
    def from_dict(payload: dict) -> "Envelope":
        """
        Create an Envelope from a dictionary.
        """
        available_keys = ["from", "to", "subject", "html", "headers"]
        for key in available_keys:
            if key not in payload:
                raise ValueError(
                    f"Key {key} is required in the payload at Envelope.from_dict")

        return Envelope(
            from_email=payload.get("from"),
            to_emails=payload.get("to"),
            subject=payload.get("subject"),
            template=payload.get("template"),
            headers=payload.get("headers"),
        )


class MailDriver:
    """
    The Mail Driver class.
    """

    def send(self, envelope: Envelope) -> None:
        raise NotImplementedError("Subclasses must implement this method")


class ResendMailDriver(MailDriver):
    """
    The Resend Mail Driver.
    """

    def send(self, envelope: Envelope) -> None:
        params: resend.Emails.SendParams = {
            "from": envelope.from_email,
            "to": envelope.to_emails,
            "subject": envelope.subject,
            "html": envelope.render_template(),
            "headers": envelope.headers,
        }
        resend.Emails.send(params)


class MailpitMailDriver(MailDriver):
    """
    The Mailpit Mail Driver.
    """

    def send(self, envelope: Envelope) -> None:
        html_content = envelope.render_template()
        email = EmailMultiAlternatives(
            subject=envelope.subject,
            body=html_content,
            from_email=envelope.from_email,
            to=envelope.to_emails,
            headers=envelope.headers,
        )
        email.attach_alternative(html_content, "text/html")
        email.send()


class MailDrivers:
    """
    The Mail Drivers class.
    """

    def __init__(self):
        self.driver = settings.MAIL_DRIVER or "mailpit"
        self.drivers = {
            "resend": ResendMailDriver(),
            "mailpit": MailpitMailDriver(),
        }

    def send(self, envelope: Envelope) -> None:
        self.drivers[self.driver].send(envelope)


@job('default', retry=Retry(max=3, interval=[60, 120, 240]))
def send_email_task(payload: dict) -> None:
    """
    Background task to send an email using Resend.

    This function is enqueued by the Mailer.send() method to send emails
    asynchronously using RQ workers.

    Args:
        envelope: The envelope to send as a dictionary.
    """
    try:
        envelope = Envelope.from_dict(payload)
        driver = MailDrivers()
        driver.send(envelope)
        logger.info(
            f"Successfully sent email to {envelope.to_emails} with subject: {envelope.subject}")

    except Exception as exc:
        logger.error(
            f"Error sending email to {payload['to']} with subject '{payload['subject']}': {exc}")
        # RQ will automatically retry on exception (up to max retries with exponential backoff)
        raise


class Mailer:
    r"""
    THe Mailer class.

    The goal is that we can have re-usable classes to send emails. So, every class extends from this Mailer class
    and then we can override the properties to send emails.

    class EmailVerificationMailer(Mailer):

        def __init__(self, user: User):
            self.user = user

        def envelope(self) -> Envelope:
            return Envelope(
                subject="Email Verification",
                template="tenants.templates.emails.email_verification",
                context={
                    "user": self.user
                },
                from_email: "Spark <no-reply@spark.com>", // This is the default from email
                to_emails: [self.user.email],
                headers: {
                    "X-Tenant-ID": self.user.tenant.id
                }
            )        
    """

    driver: MailDrivers | None = None

    def envelope(self) -> Envelope:
        raise NotImplementedError(
            "Subclasses must implement this method. Please implement the envelope() method.")

    def get_driver(self) -> MailDrivers:
        if self.driver is None:
            self.driver = MailDrivers()
        return self.driver

    def dispatch(self) -> None:
        self.get_driver().send(self.envelope())

    async def send_async(self) -> None:
        """Send the email asynchronously. (background processing)
        """
        await sync_to_async(self.send)()

    async def send_async_now(self) -> None:
        """Send the email asynchronously now. (no background processing)
        """
        await sync_to_async(self.send_now)()

    def send_now(self) -> None:
        """Send the email now. (no background processing)
        """
        self.dispatch()

    def send(self) -> None:
        """
        It sends the email using rq workers so we send in the background.

        This method enqueues the email sending task to be processed
        asynchronously by RQ workers. The email will be sent in the background
        without blocking the current request.
        """
        envelope: Envelope = self.envelope()

        queues = Queues()
        queues.default.add(
            send_email_task,
            payload=envelope.compile(),
        )
