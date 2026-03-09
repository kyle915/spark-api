import datetime

from django.conf import settings

from jobs import models
from utils.mailer import Envelope, Mailer


def _apply_offset(
    value: datetime.datetime | None, offset_minutes: int
) -> datetime.datetime | None:
    if not value:
        return None
    return value + datetime.timedelta(minutes=offset_minutes)


def _format_dt_no_tz(
    value: datetime.datetime | None, fmt: str, offset_minutes: int = 0
) -> str:
    if not value:
        return "-"
    value = _apply_offset(value, offset_minutes) or value
    formatted = value.replace(tzinfo=None).strftime(fmt)
    if fmt.startswith("%I"):
        return formatted.lstrip("0")
    return formatted


class AmbassadorJobApprovedNotificationMailer(Mailer):
    def __init__(
        self,
        ambassador_job: models.AmbassadorJob,
        to_emails: list[str],
        recipient_first_name: str | None = None,
        reply_to_email: str | None = None,
    ) -> None:
        self.ambassador_job = ambassador_job
        self.to_emails = to_emails
        self.recipient_first_name = recipient_first_name
        self.reply_to_email = reply_to_email or "events@igniteproductions.co"

    def envelope(self) -> Envelope:
        job = self.ambassador_job.job
        event = job.event
        tenant = self.ambassador_job.tenant
        event_timezone = getattr(event, "timezone", None)
        offset_minutes = int(getattr(event_timezone, "offset", 0) or 0)

        request_id = (
            f"REQ-{event.request_id}" if getattr(event, "request_id", None) else f"JOB-{job.id}"
        )
        brand_name = tenant.name or "-"
        campaign_name = event.name or job.name or "-"
        location_name = job.address or event.address or "-"
        activation_date = _format_dt_no_tz(job.start_date, "%m/%d/%Y", offset_minutes)
        start_time = _format_dt_no_tz(job.start_date, "%I:%M %p", offset_minutes)
        end_time = _format_dt_no_tz(job.end_date, "%I:%M %p", offset_minutes)

        bas_assigned = (
            models.AmbassadorJob.objects.filter(
                job_id=job.id,
                tenant_id=self.ambassador_job.tenant_id,
                status__slug="approved",
            )
            .select_related("status")
            .count()
        )

        return Envelope(
            subject="You're all set - your activation is staffed and ready",
            template="jobs.templates.emails.ambassador_job_approved_notification",
            to_emails=self.to_emails,
            headers={"Reply-To": self.reply_to_email},
            from_email=getattr(
                settings,
                "DEFAULT_FROM_EMAIL",
                "Spark by Ignite <no-reply@igniteproductions.co>",
            ),
            context={
                "recipient_first_name": self.recipient_first_name or "there",
                "request_id": request_id,
                "brand_name": brand_name,
                "campaign_name": campaign_name,
                "location_name": location_name,
                "activation_date": activation_date,
                "start_time": start_time,
                "end_time": end_time,
                "bas_assigned": bas_assigned,
            },
        )
