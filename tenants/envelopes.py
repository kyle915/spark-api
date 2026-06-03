from html import escape

from django.conf import settings

from utils.mailer import Envelope, Mailer
from tenants.models import User


class EmailVerificationMailer(Mailer):
    """
    The Email Verification Mailer.

    usage example:
    mailer = EmailVerificationMailer(user, activation_url)
    mailer.send()
    """

    def __init__(self, user: User, activation_url: str):
        self.user = user
        self.activation_url = activation_url

    def envelope(self) -> Envelope:
        return Envelope(
            subject="Please verify your email",
            template="tenants.templates.emails.email_verification",
            to_emails=[self.user.email],
            context={
                "user": self.user,
                "activation_url": self.activation_url
            }
        )


class _NoAttachedLogoMixin:
    """
    The base Mailer._build_logo_attachment() inlines spark_logo.png as
    a CID attachment for legacy templates that referenced cid:spark-logo.
    The magic-link and password-reset templates use a hosted <img src>
    URL instead, so the attached PNG just shows up as a "spark_logo.png
    28.3KB" file in the recipient's inbox. Subclasses opt out by
    returning None for the logo attachment.
    """

    def _build_logo_attachment(self):
        return None


class PasswordResetLinkMailer(_NoAttachedLogoMixin, Mailer):
    """
    Branded password-reset email. Mirrors the MagicLinkMailer template
    but the CTA leads to /reset-password/<token> on the admin app where
    the BA picks a new password.
    """

    def __init__(self, user: User, link: str, expires_minutes: int):
        self.user = user
        self.link = link
        self.expires_minutes = expires_minutes

    def envelope(self) -> Envelope:
        first = (self.user.first_name or self.user.email.split("@")[0]).strip()
        return Envelope(
            subject="Reset your Spark password",
            template="tenants.templates.emails.password_reset",
            from_email="Spark by Ignite <no-reply@igniteproductions.co>",
            to_emails=[self.user.email],
            headers={"Reply-To": "staffing@igniteproductions.co"},
            context={
                "user": self.user,
                "first_name": first,
                "link": self.link,
                "expires_minutes": self.expires_minutes,
            },
        )


class MagicLinkMailer(_NoAttachedLogoMixin, Mailer):
    """
    One-click magic-link login. Sends an email with a tokenised URL
    that the front-end exchanges for a JWT via loginWithMagicToken.
    """

    def __init__(
        self,
        user: User,
        link: str,
        expires_minutes: int,
        mobile_link: str | None = None,
        app_primary: bool = False,
    ):
        self.user = user
        self.link = link
        # Optional `spark://magic/<token>` URL the mobile app catches
        # via expo-linking. Included as a CTA in the email so taps on a
        # phone with the app installed open the app directly. None falls
        # back to web-only.
        self.mobile_link = mobile_link
        # When True (set by the caller for ambassador/BA recipients), the
        # app deep-link becomes the PRIMARY CTA and the web link drops to a
        # smaller fallback — BAs live in the mobile app and there is no BA
        # home on the admin web, so the big button must open the app.
        # Admin/client recipients leave this False so the web link stays
        # primary. Only takes effect when a `mobile_link` is also present
        # (no app link → web stays primary, never strand the recipient).
        self.app_primary = bool(app_primary and mobile_link)
        self.expires_minutes = expires_minutes

    def envelope(self) -> Envelope:
        first = (self.user.first_name or self.user.email.split("@")[0]).strip()
        return Envelope(
            subject="Your Spark sign-in link",
            template="tenants.templates.emails.magic_link",
            from_email="Spark by Ignite <no-reply@igniteproductions.co>",
            to_emails=[self.user.email],
            # Replies route to the staffing inbox so humans can pick up.
            headers={"Reply-To": "staffing@igniteproductions.co"},
            context={
                "user": self.user,
                "first_name": first,
                "link": self.link,
                "mobile_link": self.mobile_link,
                "app_primary": self.app_primary,
                "expires_minutes": self.expires_minutes,
            },
        )


class SupportTicketIgniteNotificationMailer(_NoAttachedLogoMixin, Mailer):
    """Notifies the Ignite team that a support ticket was filed from the web
    Help page.

    ``to_emails`` is resolved by the caller (the ``createSupportTicket``
    mutation) by REUSING the same Ignite-team recipient resolution the
    request-approval email uses — see ``events/mutations.py``
    (``IGNITE_REVIEW_CC`` + active spark-admins + ``REQUEST_REVIEW_COPY_EMAILS``,
    deduped through ``suppress_cc``). We do NOT hardcode an address here.

    The body is built inline (the ``html`` kwarg on :class:`Envelope` bypasses
    the Django template loader) so this internal alert needs no new template
    file. All user-supplied fields are HTML-escaped to keep arbitrary subject /
    body text from injecting markup into the alert email.
    """

    def __init__(
        self,
        *,
        to_emails: list[str],
        subject: str,
        body: str,
        category: str,
        submitter_name: str,
        submitter_email: str,
        tenant_name: str | None,
        reply_to_email: str | None = None,
    ) -> None:
        self.to_emails = to_emails
        self.ticket_subject = subject
        self.ticket_body = body
        self.category = category or "other"
        self.submitter_name = submitter_name
        self.submitter_email = submitter_email
        self.tenant_name = tenant_name
        # Replies route back to the submitter so the Ignite team can answer
        # directly from the alert; falls back to the support inbox.
        self.reply_to_email = (
            (reply_to_email or "").strip() or "staffing@igniteproductions.co"
        )

    def _subject_line(self) -> str:
        tenant_suffix = f" — {self.tenant_name}" if self.tenant_name else ""
        return f"[Spark Support] {self.ticket_subject}{tenant_suffix}"

    def _html_body(self) -> str:
        # Preserve line breaks in the free-text body, escaped first.
        body_html = escape(self.ticket_body).replace("\n", "<br>")
        rows = [
            ("From", f"{escape(self.submitter_name)} ({escape(self.submitter_email)})"),
            ("Brand / tenant", escape(self.tenant_name) if self.tenant_name else "—"),
            ("Category", escape(self.category)),
            ("Subject", escape(self.ticket_subject)),
        ]
        rows_html = "".join(
            f'<tr><td style="padding:4px 12px 4px 0;color:#666;'
            f'white-space:nowrap;vertical-align:top">{label}</td>'
            f'<td style="padding:4px 0">{value}</td></tr>'
            for label, value in rows
        )
        return (
            '<div style="font-family:Arial,Helvetica,sans-serif;font-size:14px;'
            'color:#111;line-height:1.5">'
            "<p>A new support request was submitted from the Spark Help page.</p>"
            f'<table style="border-collapse:collapse;margin:12px 0">{rows_html}</table>'
            '<p style="color:#666;margin-bottom:4px">Message:</p>'
            '<div style="padding:12px;background:#f6f6f6;border-radius:6px;'
            f'white-space:normal">{body_html}</div>'
            "</div>"
        )

    def envelope(self) -> Envelope:
        return Envelope(
            subject=self._subject_line(),
            from_email=getattr(
                settings,
                "DEFAULT_FROM_EMAIL",
                "Spark by Ignite <no-reply@igniteproductions.co>",
            ),
            to_emails=self.to_emails,
            headers={"Reply-To": self.reply_to_email},
            html=self._html_body(),
        )


class ForgotPasswordCodeMailer(Mailer):
    """
    Mailer for forgot password verification code.
    """

    def __init__(self, user: User, code: str, expires_minutes: int):
        self.user = user
        self.code = code
        self.expires_minutes = expires_minutes

    def envelope(self) -> Envelope:
        return Envelope(
            subject="Your Spark password reset code",
            template="tenants.templates.emails.forgot_password_code",
            to_emails=[self.user.email],
            context={
                "user": self.user,
                "code": self.code,
                "expires_minutes": self.expires_minutes,
            },
        )
