import datetime
import logging
import mimetypes
import re
from html import unescape
from typing import Any
from pathlib import Path
from email import encoders
from email.mime.base import MIMEBase

import django_rq
import resend
from asgiref.sync import sync_to_async

from django.conf import settings
from django.template.loader import get_template as django_get_template
from django.template import Template
from django_rq import job
from django.core.mail import EmailMultiAlternatives
from rq import Retry

from utils.queues import Queues

resend.api_key = settings.RESEND_API_KEY

logger = logging.getLogger(__name__)


def html_to_text(html: str) -> str:
    """Best-effort plain-text rendering of an HTML email body.

    Mailbox providers (Gmail especially) treat HTML-only messages with no
    text/plain alternative as a spam signal. We don't ship hand-written
    text templates, so derive a readable text part from the rendered HTML:
    drop script/style, keep link URLs inline as "label (url)", turn block
    tags into newlines, strip the rest, and tidy whitespace. Dependency-free.
    """
    if not html:
        return ""
    text = html
    # Remove style/script blocks (incl. their contents).
    text = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", "", text)

    # Preserve link targets: <a href="X">label</a> -> "label (X)".
    def _link(match: "re.Match[str]") -> str:
        href = (match.group(1) or "").strip()
        label = re.sub(r"(?is)<[^>]+>", "", match.group(2) or "").strip()
        if not href or href.startswith("mailto:") or href == label:
            return label
        return f"{label} ({href})" if label else href

    text = re.sub(
        r'(?is)<a\b[^>]*\bhref=["\']([^"\']*)["\'][^>]*>(.*?)</a>', _link, text
    )
    # List items -> bulleted lines.
    text = re.sub(r"(?is)<li\b[^>]*>", "\n- ", text)
    # Block-level closers / breaks -> newlines.
    text = re.sub(r"(?is)<(br|/p|/div|/tr|/li|/h[1-6]|/table)\s*/?>", "\n", text)
    # Strip any remaining tags.
    text = re.sub(r"(?is)<[^>]+>", "", text)
    # Decode HTML entities.
    text = unescape(text)
    # Tidy: trim each line, collapse 3+ blank lines to one blank line.
    text = "\n".join(line.strip() for line in text.splitlines())
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    return text


class Envelope:
    subject: str = "Spark Notification"
    template: str = ""
    context: dict = {}
    from_email: str = settings.DEFAULT_FROM_EMAIL
    to_emails: list[str] = []
    cc_emails: list[str] = []
    headers: dict = {}
    html: str = ""
    attachments: list[dict[str, Any]] = []

    def __init__(self, **kwargs: Any) -> None:
        """
        Initialize the Envelope class.
        """
        self.subject = kwargs.get("subject", self.subject)
        self.template = kwargs.get("template", self.template)
        self.context = kwargs.get("context", self.context)
        self.from_email = kwargs.get("from_email", self.from_email)
        self.to_emails = kwargs.get("to_emails", self.to_emails)
        self.cc_emails = kwargs.get("cc_emails", self.cc_emails)
        self.headers = kwargs.get("headers", self.headers)
        self.html = kwargs.get("html", self.html)
        self.attachments = kwargs.get("attachments", self.attachments)

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
        if self.html:
            return self.html

        template = self.get_template()
        context = dict(self.context or {})
        context.setdefault("EMAIL_LOGO_CID", settings.EMAIL_LOGO_CID)
        context.setdefault("EMAIL_LOGO_URL", settings.EMAIL_LOGO_URL)
        return template.render(context)

    def render_text(self) -> str:
        """Plain-text alternative derived from the rendered HTML body."""
        return html_to_text(self.render_template())

    def delivery_headers(self) -> dict:
        """Headers plus deliverability defaults.

        Adds a List-Unsubscribe header (unless the caller already set one)
        so Gmail/Yahoo see a clean opt-out path — recipients use that
        instead of hitting "report spam", which protects sender reputation.
        Mailto-only (no RFC 8058 one-click) since there's no HTTPS
        unsubscribe endpoint to POST to yet.
        """
        headers = dict(self.headers or {})
        if not any(k.lower() == "list-unsubscribe" for k in headers):
            mailto = getattr(
                settings, "LIST_UNSUBSCRIBE_MAILTO", "events@igniteproductions.co"
            )
            headers["List-Unsubscribe"] = f"<mailto:{mailto}?subject=Unsubscribe>"
        return headers

    def compile(self) -> dict:
        """
        Compile the envelope to a dictionary.
        """
        payload = {
            "from": self.from_email,
            "to": self.to_emails,
            "subject": self.subject,
            "html": self.render_template(),
            "template": self.template,
            "headers": self.headers,
        }
        if self.cc_emails:
            payload["cc"] = self.cc_emails
        if self.attachments:
            payload["attachments"] = self.attachments
        return payload

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
            cc_emails=payload.get("cc", []),
            subject=payload.get("subject"),
            template=payload.get("template"),
            headers=payload.get("headers"),
            html=payload.get("html"),
            attachments=payload.get("attachments", []),
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
            # Plain-text alternative — multipart/alternative is friendlier to
            # spam filters than HTML-only.
            "text": envelope.render_text(),
            "headers": envelope.delivery_headers(),
        }
        if envelope.cc_emails:
            params["cc"] = envelope.cc_emails
        if envelope.attachments:
            params["attachments"] = envelope.attachments
        result = resend.Emails.send(params)
        # When the API key is invalid or the FROM domain isn't verified,
        # the SDK still returns a payload (no exception) but `id` is
        # missing — without this check that's a SILENT failure: the caller
        # believes the email went out (welcome emails, approvals) and
        # nobody ever learns otherwise. ERROR-level so the backend error
        # monitor alerts on it; deliberately not raised — most sends are
        # best-effort side effects of a mutation that already committed,
        # and failing the whole request over a lost email is worse than
        # the lost email.
        send_id = (
            result.get("id") if isinstance(result, dict)
            else getattr(result, "id", None)
        )
        if not send_id:
            logger.error(
                "Resend send FAILED (no id in response) to=%s subject=%r result=%s",
                envelope.to_emails, envelope.subject, result,
            )
            return
        logger.info(
            "Resend send to=%s subject=%r id=%s",
            envelope.to_emails, envelope.subject, send_id,
        )


class MailpitMailDriver(MailDriver):
    """
    The Mailpit Mail Driver.
    """

    def send(self, envelope: Envelope) -> None:
        html_content = envelope.render_template()
        email = EmailMultiAlternatives(
            subject=envelope.subject,
            # body is the text/plain part; HTML is attached as the alternative.
            body=envelope.render_text(),
            from_email=envelope.from_email,
            to=envelope.to_emails,
            cc=envelope.cc_emails,
            headers=envelope.delivery_headers(),
        )
        email.attach_alternative(html_content, "text/html")
        for attachment in envelope.attachments or []:
            filename = attachment.get("filename") or "attachment"
            content = attachment.get("content")
            if content is None:
                continue
            mimetype = attachment.get("content_type") or mimetypes.guess_type(
                filename
            )[0] or "application/octet-stream"
            maintype, subtype = mimetype.split("/", 1)
            payload = bytes(content) if isinstance(content, list) else content
            if isinstance(payload, str):
                payload = payload.encode("utf-8")

            part = MIMEBase(maintype, subtype)
            part.set_payload(payload)
            encoders.encode_base64(part)
            part.add_header("Content-Type", mimetype, name=filename)
            content_id = attachment.get("content_id") or attachment.get(
                "inline_content_id"
            )
            if content_id:
                part.add_header("Content-Disposition", "inline", filename=filename)
                part.add_header("Content-ID", f"<{content_id}>")
            else:
                part.add_header("Content-Disposition", "attachment", filename=filename)
            email.attach(part)
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

    def _resolve_logo_path(self) -> Path | None:
        logo_path = getattr(settings, "EMAIL_LOGO_PATH", "")
        if not logo_path:
            return None
        path = Path(logo_path)
        if not path.is_absolute():
            path = Path(settings.BASE_DIR) / path
        return path

    def _build_logo_attachment(self) -> dict[str, Any] | None:
        logo_path = self._resolve_logo_path()
        if logo_path is None or not logo_path.exists():
            return None
        try:
            raw = logo_path.read_bytes()
        except OSError:
            return None

        mime_type = mimetypes.guess_type(logo_path.name)[0] or "image/png"
        return {
            "filename": logo_path.name,
            "content": list(raw),
            "content_type": mime_type,
            "content_id": settings.EMAIL_LOGO_CID,
        }

    def _prepare_envelope(self, envelope: Envelope) -> Envelope:
        context = dict(envelope.context or {})
        context.setdefault("EMAIL_LOGO_CID", settings.EMAIL_LOGO_CID)
        context.setdefault("EMAIL_LOGO_URL", settings.EMAIL_LOGO_URL)
        envelope.context = context

        attachments = list(envelope.attachments or [])
        logo_cid = settings.EMAIL_LOGO_CID
        already_has_logo = any(
            (a.get("content_id") or a.get("inline_content_id")) == logo_cid
            for a in attachments
            if isinstance(a, dict)
        )
        if not already_has_logo:
            logo_attachment = self._build_logo_attachment()
            if logo_attachment:
                attachments.append(logo_attachment)
        envelope.attachments = attachments
        return envelope

    def dispatch(self) -> None:
        envelope = self._prepare_envelope(self.envelope())
        self.get_driver().send(envelope)

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

    def send(self, delay_seconds: int | float | None = None) -> None:
        """
        It sends the email using rq workers so we send in the background.

        This method enqueues the email sending task to be processed
        asynchronously by RQ workers. The email will be sent in the background
        without blocking the current request.

        If Redis is unreachable (Cloud Run doesn't provision it by default),
        we fall back to sending the email inline via the driver so the
        calling mutation doesn't 500 on the whole request.
        """
        envelope: Envelope = self.envelope()
        envelope = self._prepare_envelope(envelope)
        try:
            queues = Queues()
            if delay_seconds and delay_seconds > 0:
                scheduler = django_rq.get_scheduler("default")
                scheduler.enqueue_in(
                    datetime.timedelta(seconds=delay_seconds),
                    send_email_task,
                    payload=envelope.compile(),
                )
                return
            queues.default.add(send_email_task, payload=envelope.compile())
        except Exception as exc:
            # Redis ConnectionRefusedError / TimeoutError / any other
            # queue-layer failure → swallow and dispatch inline. Worst
            # case is the response takes ~200ms longer; best case is
            # we don't 500 the user-facing mutation.
            logger.warning(
                "Mail queue unreachable (%s); falling back to inline send "
                "for subject=%r to=%s",
                exc, envelope.subject, envelope.to_emails,
            )
            try:
                self.get_driver().send(envelope)
            except Exception as inline_exc:
                logger.exception(
                    "Inline mail fallback failed too: %s", inline_exc,
                )


class MailChain:
    """
    The Mail Chain class.
    This class is used to send a chain of Mailers.

    For instance:
    mail_chain = MailChain()
    mail_chain.add(EmailVerificationMailer(user, activation_token))
    mail_chain.add(EventApplicationMailer(application))
    mail_chain.send()

    or 

    mail_chain = MailChain.from_mailers([EmailVerificationMailer(user, activation_token), EventApplicationMailer(application)])
    mail_chain.send()

    or 

    mail_chain = MailChain([EmailVerificationMailer(user, activation_token), EventApplicationMailer(application)])
    mail_chain.send()
    """

    def __init__(self, mailers: list[Mailer] | None = None):
        self.mailers: list[Mailer] = mailers or []

    @staticmethod
    def send_chain(mailers: list[Mailer]) -> "MailChain":
        chain = MailChain(mailers)
        chain.send()
        return chain

    @staticmethod
    def send_chain_now(mailers: list[Mailer]) -> "MailChain":
        chain = MailChain(mailers)
        chain.send_now()
        return chain

    @staticmethod
    async def send_chain_async(mailers: list[Mailer]) -> "MailChain":
        chain = MailChain(mailers)
        await chain.send_async()
        return chain

    @staticmethod
    async def send_chain_async_now(mailers: list[Mailer]) -> "MailChain":
        chain = MailChain(mailers)
        await chain.send_async_now()
        return chain

    def add(self, mailer: Mailer) -> None:
        self.mailers.append(mailer)

    def send(self) -> None:
        for mailer in self.mailers:
            mailer.send()

    def send_now(self) -> None:
        for mailer in self.mailers:
            mailer.send_now()

    async def send_async(self) -> None:
        """Send all emails in the chain asynchronously (background processing)."""
        for mailer in self.mailers:
            await mailer.send_async()

    async def send_async_now(self) -> None:
        """Send all emails in the chain asynchronously now (no background processing)."""
        for mailer in self.mailers:
            await mailer.send_async_now()
