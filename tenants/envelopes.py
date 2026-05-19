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
    ):
        self.user = user
        self.link = link
        # Optional `spark://magic/<token>` URL the mobile app catches
        # via expo-linking. Included as a secondary CTA in the email
        # so taps on a phone with the app installed open the app
        # directly. None falls back to web-only.
        self.mobile_link = mobile_link
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
                "expires_minutes": self.expires_minutes,
            },
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
