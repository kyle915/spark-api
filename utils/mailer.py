import resend
from typing import Any
from asgiref.sync import sync_to_async
import logging

from django.conf import settings
from django.template.loader import get_template as django_get_template
from django.template import Template
from django_rq import job
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
        }


@job('default', retry=Retry(max=3, interval=[60, 120, 240]))
def send_email_task(envelop: dict) -> None:
    """
    Background task to send an email using Resend.

    This function is enqueued by the Mailer.send() method to send emails
    asynchronously using RQ workers.

    Args:
        envelope: The envelope to send as a dictionary.
    """
    try:
        params: resend.Emails.SendParams = envelop
        resend.Emails.send(params)
        logger.info(
            f"Successfully sent email to {params['to']} with subject: {params['subject']}")

    except Exception as exc:
        logger.error(
            f"Error sending email to {params['to']} with subject '{params['subject']}': {exc}")
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

    def dispatch(self) -> None:
        envelope: Envelope = self.envelope()
        params: resend.Emails.SendParams = {
            "from": envelope.from_email,
            "to": envelope.to_emails,
            "subject": envelope.subject,
            "html": envelope.render_template(),
            "headers": envelope.headers,
        }
        resend.Emails.send(params)

    async def send_async(self) -> None:
        await sync_to_async(self.dispatch)()

    def send_now(self) -> None:
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
            envelope=envelope.compile(),
        )
