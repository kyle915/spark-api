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
        context = _build_ambassador_job_email_context(self.ambassador_job)
        context["bas_assigned"] = _get_bas_assigned_count(self.ambassador_job)

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
                **context,
            },
        )


class AmbassadorApprovedForJobMailer(Mailer):
    LIQUID_DEATH_TENANT_SLUG = "liquid-death"

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
        context = _build_ambassador_job_email_context(self.ambassador_job)
        tenant_slug = (getattr(self.ambassador_job.tenant, "slug", None) or "").strip().lower()
        template = (
            "jobs.templates.emails.ambassador_assigned_to_job"
            if tenant_slug == self.LIQUID_DEATH_TENANT_SLUG
            else "jobs.templates.emails.ambassador_approved_for_job"
        )

        return Envelope(
            subject="You have been approved for a job",
            template=template,
            to_emails=self.to_emails,
            headers={"Reply-To": self.reply_to_email},
            from_email=getattr(
                settings,
                "DEFAULT_FROM_EMAIL",
                "Spark by Ignite <no-reply@igniteproductions.co>",
            ),
            context={
                "recipient_first_name": self.recipient_first_name or "there",
                **context,
            },
        )


class AmbassadorAssignedToJobMailer(Mailer):
    LIQUID_DEATH_TENANT_SLUG = "liquid-death"

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
        context = _build_ambassador_job_email_context(self.ambassador_job)
        tenant_slug = (
            (getattr(self.ambassador_job.tenant, "slug", None) or "").strip().lower()
        )
        template = (
            "jobs.templates.emails.ambassador_assigned_to_job"
            if tenant_slug == self.LIQUID_DEATH_TENANT_SLUG
            else "jobs.templates.emails.ambassador_assigned_to_job_default"
        )

        return Envelope(
            subject="You have been assigned to a job",
            template=template,
            to_emails=self.to_emails,
            headers={"Reply-To": self.reply_to_email},
            from_email=getattr(
                settings,
                "DEFAULT_FROM_EMAIL",
                "Spark by Ignite <no-reply@igniteproductions.co>",
            ),
            context={
                "recipient_first_name": self.recipient_first_name or "there",
                **context,
            },
        )


class AmbassadorInvitedToJobMailer(Mailer):
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
        context = _build_ambassador_job_email_context(self.ambassador_job)

        return Envelope(
            subject="You have been invited to a job",
            template="jobs.templates.emails.ambassador_invited_to_job",
            to_emails=self.to_emails,
            headers={"Reply-To": self.reply_to_email},
            from_email=getattr(
                settings,
                "DEFAULT_FROM_EMAIL",
                "Spark by Ignite <no-reply@igniteproductions.co>",
            ),
            context={
                "recipient_first_name": self.recipient_first_name or "there",
                **context,
            },
        )


class AmbassadorJobUpdatedMailer(Mailer):
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
        context = _build_ambassador_job_email_context(self.ambassador_job)

        return Envelope(
            subject="Your job details have been updated",
            template="jobs.templates.emails.ambassador_job_updated",
            to_emails=self.to_emails,
            headers={"Reply-To": self.reply_to_email},
            from_email=getattr(
                settings,
                "DEFAULT_FROM_EMAIL",
                "Spark by Ignite <no-reply@igniteproductions.co>",
            ),
            context={
                "recipient_first_name": self.recipient_first_name or "there",
                **context,
            },
        )


class AmbassadorUnassignedFromJobMailer(Mailer):
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
        context = _build_ambassador_job_email_context(self.ambassador_job)

        return Envelope(
            subject="You have been unassigned from a job",
            template="jobs.templates.emails.ambassador_unassigned_from_job",
            to_emails=self.to_emails,
            headers={"Reply-To": self.reply_to_email},
            from_email=getattr(
                settings,
                "DEFAULT_FROM_EMAIL",
                "Spark by Ignite <no-reply@igniteproductions.co>",
            ),
            context={
                "recipient_first_name": self.recipient_first_name or "there",
                **context,
            },
        )


class AmbassadorEventSuspendedMailer(Mailer):
    LIQUID_DEATH_TENANT_SLUG = "liquid-death"

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
        context = _build_ambassador_job_email_context(self.ambassador_job)
        tenant_slug = (getattr(self.ambassador_job.tenant, "slug", None) or "").strip().lower()
        template = (
            "jobs.templates.emails.ambassador_event_suspended_liquid_death"
            if tenant_slug == self.LIQUID_DEATH_TENANT_SLUG
            else "jobs.templates.emails.ambassador_event_suspended"
        )

        return Envelope(
            subject="Your event has been suspended",
            template=template,
            to_emails=self.to_emails,
            headers={"Reply-To": self.reply_to_email},
            from_email=getattr(
                settings,
                "DEFAULT_FROM_EMAIL",
                "Spark by Ignite <no-reply@igniteproductions.co>",
            ),
            context={
                "recipient_first_name": self.recipient_first_name or "there",
                **context,
            },
        )


def _build_ambassador_job_email_context(
    ambassador_job: models.AmbassadorJob,
) -> dict[str, str | int]:
    from events.models import RequestProduct

    job = ambassador_job.job
    event = job.event
    retailer = getattr(event, "retailer", None)
    retailer_location = getattr(retailer, "location", None)
    retailer_state = getattr(retailer_location, "state", None)
    retailer_is_national = bool(getattr(retailer, "is_national", False))
    tenant = ambassador_job.tenant
    event_timezone = getattr(event, "timezone", None)
    offset_minutes = int(getattr(event_timezone, "offset", 0) or 0)

    request_id = (
        f"REQ-{event.request_id}"
        if getattr(event, "request_id", None)
        else f"JOB-{job.id}"
    )
    brand_name = tenant.name or "-"
    campaign_name = event.name or job.name or "-"
    event_address = job.address or event.address or "-"
    start_dt = event.start_time or event.date or job.start_date
    end_dt = event.end_time or event.date or job.end_date
    retailer_location_name = getattr(retailer_location, "name", None)
    retailer_state_code = getattr(retailer_state, "code", None)
    if retailer_location_name and retailer_state_code:
        location_name = f"{retailer_location_name} - {retailer_state_code}"
    else:
        location_name = retailer_location_name or event_address
    market_name = getattr(retailer, "name", None) or "-"
    sku_names = list(
        RequestProduct.objects.filter(request_id=event.request_id)
        .select_related("product")
        .exclude(product__name__isnull=True)
        .exclude(product__name="")
        .values_list("product__name", flat=True)
    ) if event.request_id else []
    skus = ", ".join(dict.fromkeys(sku_names)) if sku_names else "-"
    activation_date = _format_dt_no_tz(start_dt, "%m/%d/%Y", offset_minutes)
    start_time = _format_dt_no_tz(start_dt, "%I:%M %p", offset_minutes)
    end_time = _format_dt_no_tz(end_dt, "%I:%M %p", offset_minutes)
    deep_link = f"spark://app/tabs/my-gigs/{job.id}"

    return {
        "request_id": request_id,
        "brand_name": brand_name,
        "campaign_name": campaign_name,
        "location_name": location_name,
        "show_location": not retailer_is_national,
        "market_name": market_name,
        "skus": skus,
        "event_address": event_address,
        "activation_date": activation_date,
        "start_time": start_time,
        "end_time": end_time,
        "deep_link": deep_link,
        "event_notes": event.notes or job.description or "-",
    }


def _get_bas_assigned_count(ambassador_job: models.AmbassadorJob) -> int:
    return (
        models.AmbassadorJob.objects.filter(
            job_id=ambassador_job.job_id,
            tenant_id=ambassador_job.tenant_id,
            status__slug="approved",
        )
        .select_related("status")
        .count()
    )
