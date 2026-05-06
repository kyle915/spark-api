import mimetypes
import datetime
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

    def _format_dt_no_tz(
        self, value: datetime.datetime | None, fmt: str, offset_minutes: int = 0
    ) -> str:
        if not value:
            return "-"
        value = value + datetime.timedelta(minutes=offset_minutes)
        formatted = value.replace(tzinfo=None).strftime(fmt)
        if fmt.startswith("%I"):
            return formatted.lstrip("0")
        return formatted

    def _build_job_invite_context(self) -> dict[str, str | bool]:
        invitation = self.invitation
        job = invitation.job
        event = getattr(job, "event", None)
        tenant = invitation.tenant
        ambassador = getattr(invitation, "ambassador", None)
        ambassador_user = getattr(ambassador, "user", None)
        retailer = getattr(event, "retailer", None)
        retailer_location = getattr(retailer, "location", None)
        retailer_state = getattr(retailer_location, "state", None)
        retailer_is_national = bool(getattr(retailer, "is_national", False))
        event_timezone = getattr(event, "timezone", None)
        offset_minutes = int(getattr(event_timezone, "offset", 0) or 0)

        request_id_value = getattr(event, "request_id", None)
        request_id = f"REQ-{request_id_value}" if request_id_value else f"JOB-{job.id}"
        campaign_name = (getattr(event, "name", None) or job.name or "-")
        event_address = job.address or getattr(event, "address", None) or "-"
        start_dt = (
            getattr(event, "start_time", None)
            or getattr(event, "date", None)
            or job.start_date
        )
        end_dt = (
            getattr(event, "end_time", None)
            or getattr(event, "date", None)
            or job.end_date
        )

        retailer_location_name = getattr(retailer_location, "name", None)
        retailer_state_code = getattr(retailer_state, "code", None)
        if retailer_location_name and retailer_state_code:
            location_name = f"{retailer_location_name} - {retailer_state_code}"
        else:
            location_name = retailer_location_name or event_address

        return {
            "recipient_first_name": (
                (getattr(ambassador_user, "first_name", None) or "").strip() or "there"
            ),
            "request_id": request_id,
            "brand_name": getattr(tenant, "name", None) or "-",
            "campaign_name": campaign_name,
            "market_name": getattr(retailer, "name", None) or "-",
            "show_location": not retailer_is_national,
            "location_name": location_name,
            "event_address": event_address,
            "activation_date": self._format_dt_no_tz(start_dt, "%m/%d/%Y", offset_minutes),
            "start_time": self._format_dt_no_tz(start_dt, "%I:%M %p", offset_minutes),
            "end_time": self._format_dt_no_tz(end_dt, "%I:%M %p", offset_minutes),
            "event_notes": getattr(event, "notes", None) or job.description or "-",
            "deep_link": f"spark://app/tabs/my-gigs/{job.id}",
        }

    def envelope(self) -> Envelope:
        return Envelope(
            subject="You have been invited to a job",
            template="jobs.templates.emails.ambassador_invited_to_job",
            to_emails=[self.invitation.email],
            context=self._build_job_invite_context(),
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
            subject="Welcome to Spark by Ignite",
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
