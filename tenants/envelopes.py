from utils.mailer import Envelope, Mailer
from tenants.models import User


class EmailVerificationMailer(Mailer):
    """
    The Email Verification Mailer.

    usage example:
    mailer = EmailVerificationMailer(user, activation_token)
    mailer.send()
    """

    def __init__(self, user: User, activation_token: str):
        self.user = user
        self.activation_token = activation_token

    def envelope(self) -> Envelope:
        return Envelope(
            subject="Please verify your email",
            template="tenants.templates.emails.email_verification",
            to_emails=[self.user.email],
            context={
                "user": self.user,
                "activation_token": self.activation_token
            }
        )
