import mimetypes
from pathlib import Path

from django.conf import settings

from utils.mailer import Envelope, Mailer
from ambassadors.models import AmbassadorEvent, AmbassadorInvitation
from tenants.models import User


class AmbassadorEventApplicationMailer(Mailer):
    """
    The Ambassador Event Application Mailer.
    """

    def __init__(self, application: AmbassadorEvent):
        self.application = application

    def envelope(self) -> Envelope:
        return Envelope(
            subject="Your application has been received",
            template="ambassadors.templates.emails.event_application",
            to_emails=[self.application.ambassador.user.email],
            context={
                "application": self.application
            }
        )


class NotifyApplicationToClientMailer(Mailer):
    def __init__(self, application: AmbassadorEvent):
        self.application = application

    def envelope(self) -> Envelope:
        from tenants.models import TenantedUser, Role
        users = TenantedUser.objects.filter(
            tenant=self.application.tenant,
            user__role__slug=Role.CLIENT_SLUG
        ).select_related("user", "user__role")
        to_emails = [user.user.email for user in users]

        return Envelope(
            subject="New application has been received",
            template="ambassadors.templates.emails.notify_application_to_client",
            to_emails=to_emails,
            context={
                "application": self.application
            }
        )


class SendInvitationMailToAmbassadorMailer(Mailer):
    def __init__(self, invitation: AmbassadorInvitation):
        self.invitation = invitation

    def envelope(self) -> Envelope:
        return Envelope(
            subject="You have been invited to a job",
            template="ambassadors.templates.emails.send_invitation_to_ambassador",
            to_emails=[self.invitation.email],
            context={
                "invitation": self.invitation
            }
        )


class AmbassadorGeneratedPasswordMailer(Mailer):
    PLAY_STORE_BUTTON_CID = "spark-play-store-button"
    APP_STORE_BUTTON_CID = "spark-app-store-button"

    def __init__(self, user: User, password: str):
        self.user = user
        self.password = password

    def _build_inline_attachment(
        self,
        path: Path,
        content_id: str,
    ) -> dict | None:
        if not path.exists():
            return None

        try:
            raw = path.read_bytes()
        except OSError:
            return None

        return {
            "filename": path.name,
            "content": list(raw),
            "content_type": mimetypes.guess_type(path.name)[0] or "image/png",
            "content_id": content_id,
        }

    def envelope(self) -> Envelope:
        static_root = Path(settings.BASE_DIR) / "ambassadors" / "static"
        attachments = [
            attachment
            for attachment in [
                self._build_inline_attachment(
                    static_root / "play-store-button.png",
                    self.PLAY_STORE_BUTTON_CID,
                ),
                self._build_inline_attachment(
                    static_root / "app-store-button.png",
                    self.APP_STORE_BUTTON_CID,
                ),
            ]
            if attachment
        ]

        return Envelope(
            subject="Welcome to Spark",
            template="ambassadors.templates.emails.generated_password",
            to_emails=[self.user.email],
            context={
                "user": self.user,
                "password": self.password,
                "PLAY_STORE_BUTTON_CID": self.PLAY_STORE_BUTTON_CID,
                "APP_STORE_BUTTON_CID": self.APP_STORE_BUTTON_CID,
            },
            attachments=attachments,
        )
