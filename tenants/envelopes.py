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
