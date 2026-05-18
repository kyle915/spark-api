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


class MagicLinkMailer(Mailer):
    """
    One-click magic-link login. Sends an email with a tokenised URL
    that the front-end exchanges for a JWT via loginWithMagicToken.
    """

    def __init__(self, user: User, link: str, expires_minutes: int):
        self.user = user
        self.link = link
        self.expires_minutes = expires_minutes

    def envelope(self) -> Envelope:
        first = (self.user.first_name or self.user.email.split("@")[0]).strip()
        return Envelope(
            subject="Your Spark sign-in link",
            template="tenants.templates.emails.magic_link",
            to_emails=[self.user.email],
            context={
                "user": self.user,
                "first_name": first,
                "link": self.link,
                "expires_minutes": self.expires_minutes,
            },
            # html fallback in case the template isn't packaged in the
            # container — keeps magic-link login working from day one.
            html=(
                f"<p>Hey {first},</p>"
                f"<p>Click this link to sign in to Spark. It expires in "
                f"{self.expires_minutes} minutes.</p>"
                f'<p><a href="{self.link}" '
                f'style="background:#c5f546;color:#0a0d09;padding:12px 20px;'
                f'border-radius:12px;text-decoration:none;font-weight:600;">'
                f"Sign in to Spark →</a></p>"
                f"<p>If the button doesn't work, paste this in your browser:<br/>"
                f'<a href="{self.link}">{self.link}</a></p>'
                f"<p>If you didn't request this, you can ignore the email.</p>"
                f"<p>— The Ignite team</p>"
            ),
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
