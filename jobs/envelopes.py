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
        tenant_slug = (
            (getattr(self.ambassador_job.tenant, "slug", None) or "").strip().lower()
        )
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


class JobApplicationReceivedMailer(Mailer):
    """Internal staffing alert: a BA just applied to a posted gig.

    Goes to the *staffing* side only — the event's assigned RMM, the admin
    who posted the job, and the Ignite events inbox — never the brand client
    (who applied is an internal staffing concern, not something to forward to
    the client). Self-contained inline HTML so it needs no template file.
    """

    def __init__(
        self,
        *,
        to_emails: list[str],
        applicant_name: str,
        job_name: str,
        event_name: str | None = None,
        when_label: str | None = None,
        location_label: str | None = None,
        tenant_name: str | None = None,
        note: str | None = None,
        applicants_url: str | None = None,
        reply_to_email: str | None = None,
    ) -> None:
        self.to_emails = to_emails
        self.applicant_name = (applicant_name or "").strip() or "A brand ambassador"
        self.job_name = (job_name or "").strip() or "a gig"
        self.event_name = event_name
        self.when_label = when_label
        self.location_label = location_label
        self.tenant_name = tenant_name
        self.note = note
        self.applicants_url = applicants_url
        self.reply_to_email = reply_to_email or "events@igniteproductions.co"

    def envelope(self) -> Envelope:
        from html import escape

        def esc(value: object) -> str:
            return escape(str(value)) if value not in (None, "") else ""

        # Detail rows render inside a light card; each gets a hairline divider
        # except the last (added after the loop so it's positional-agnostic).
        pairs: list[tuple[str, object]] = [
            ("Gig", self.job_name),
            ("When", self.when_label),
            ("Location", self.location_label),
            ("Brand", self.tenant_name),
        ]
        if self.event_name and self.event_name != self.job_name:
            pairs.insert(1, ("Event", self.event_name))
        visible = [(k, v) for k, v in pairs if v not in (None, "")]

        rows = ""
        for i, (label, value) in enumerate(visible):
            border = (
                "border-bottom:1px solid #eef0f2;" if i < len(visible) - 1 else ""
            )
            rows += (
                '<tr>'
                '<td style="padding:10px 16px;color:#6b7280;font-size:12px;'
                "letter-spacing:0.04em;text-transform:uppercase;white-space:nowrap;"
                'vertical-align:top;' + border + '">' + esc(label) + '</td>'
                '<td style="padding:10px 16px;color:#111827;font-size:14px;'
                'font-weight:600;text-align:right;' + border + '">'
                + esc(value) + '</td>'
                '</tr>'
            )

        note_block = ""
        if self.note not in (None, ""):
            note_block = (
                '<div style="margin:18px 28px 0;padding:14px 16px;'
                'background:#f9fafb;border:1px solid #eef0f2;border-radius:12px">'
                '<p style="margin:0 0 4px;font-size:11px;letter-spacing:0.08em;'
                'text-transform:uppercase;color:#9ca3af">Note from applicant</p>'
                '<p style="margin:0;font-size:14px;color:#374151;'
                'line-height:1.5">' + esc(self.note) + '</p></div>'
            )

        button = ""
        if self.applicants_url:
            button = (
                '<tr><td style="padding:24px 28px 4px">'
                '<a href="' + esc(self.applicants_url) + '" '
                'style="display:inline-block;background:#111827;color:#ffffff;'
                'text-decoration:none;border-radius:10px;padding:12px 22px;'
                'font-size:14px;font-weight:600">Review applicant &rarr;</a>'
                '</td></tr>'
            )

        gig_label = esc(self.event_name or self.job_name)
        html_body = (
            '<div style="background:#f4f5f7;padding:28px 12px;'
            'font-family:-apple-system,BlinkMacSystemFont,Segoe UI,Roboto,'
            'Helvetica,Arial,sans-serif">'
            '<table role="presentation" align="center" cellpadding="0" '
            'cellspacing="0" style="width:560px;max-width:100%;background:#ffffff;'
            'border:1px solid #e5e7eb;border-radius:16px;overflow:hidden">'
            # lime accent bar (brand)
            '<tr><td style="height:4px;background:#c4d82e;font-size:0;'
            'line-height:0">&nbsp;</td></tr>'
            # header
            '<tr><td style="padding:26px 28px 0">'
            '<p style="margin:0;font-size:11px;letter-spacing:0.16em;'
            'text-transform:uppercase;color:#9ca3af">'
            'Spark by Ignite &middot; New applicant</p>'
            '<p style="margin:12px 0 2px;font-size:22px;font-weight:700;'
            'color:#111827">' + esc(self.applicant_name) + '</p>'
            '<p style="margin:0;font-size:14px;color:#6b7280">applied to '
            '<strong style="color:#111827">' + gig_label + '</strong></p>'
            '</td></tr>'
            # detail card
            '<tr><td style="padding:20px 28px 0">'
            '<table role="presentation" cellpadding="0" cellspacing="0" '
            'style="width:100%;border-collapse:separate;background:#f9fafb;'
            'border:1px solid #eef0f2;border-radius:12px">' + rows + '</table>'
            '</td></tr>'
            # optional note
            + ('<tr><td>' + note_block + '</td></tr>' if note_block else '')
            # CTA
            + button +
            # footer
            '<tr><td style="padding:22px 28px;margin-top:8px;'
            'border-top:1px solid #eef0f2;background:#fafafa">'
            '<p style="margin:0;font-size:12px;color:#9ca3af">'
            "You're receiving this because you manage staffing for this gig."
            '</p></td></tr>'
            '</table></div>'
        )

        return Envelope(
            subject=(
                "New applicant: " + self.applicant_name + " → " + self.job_name
            ),
            to_emails=self.to_emails,
            html=html_body,
            headers={"Reply-To": self.reply_to_email},
            from_email=getattr(
                settings,
                "DEFAULT_FROM_EMAIL",
                "Spark by Ignite <no-reply@igniteproductions.co>",
            ),
        )


def _build_job_booking_email_context(job: "models.Job") -> dict[str, object]:
    """Booking-confirmation context built straight from a Job + its Event.

    The accept/assign flow (assign_ambassador_to_job) operates on a Job and
    a JobApplication — there is no AmbassadorJob row — so it can't use
    `_build_ambassador_job_email_context`. This mirrors the same keys the
    `ambassador_assigned_to_job_default.html` template renders, plus a `pay`
    line, so we can reuse that template for the confirmation.
    """
    from events.models import RequestProduct

    event = getattr(job, "event", None)
    retailer = getattr(event, "retailer", None)
    retailer_location = getattr(retailer, "location", None)
    retailer_state = getattr(retailer_location, "state", None)
    retailer_is_national = bool(getattr(retailer, "is_national", False))
    tenant = getattr(job, "tenant", None)
    event_timezone = getattr(event, "timezone", None)
    offset_minutes = int(getattr(event_timezone, "offset", 0) or 0)

    request_id = (
        f"REQ-{event.request_id}"
        if getattr(event, "request_id", None)
        else f"JOB-{job.id}"
    )
    brand_name = (getattr(tenant, "name", None) or "-")
    campaign_name = (getattr(event, "name", None) or job.name or "-")
    event_type = getattr(getattr(event, "event_type", None), "name", None) or ""
    event_address = job.address or getattr(event, "address", None) or "-"
    start_dt = getattr(event, "start_time", None) or getattr(
        event, "date", None
    ) or job.start_date
    end_dt = getattr(event, "end_time", None) or getattr(
        event, "date", None
    ) or job.end_date
    retailer_location_name = getattr(retailer_location, "name", None)
    retailer_state_code = getattr(retailer_state, "code", None)
    if retailer_location_name and retailer_state_code:
        location_name = f"{retailer_location_name} - {retailer_state_code}"
    else:
        location_name = retailer_location_name or event_address
    market_name = getattr(retailer, "name", None) or "-"
    request_pk = getattr(event, "request_id", None)
    sku_names = (
        list(
            RequestProduct.objects.filter(request_id=request_pk)
            .select_related("product")
            .exclude(product__name__isnull=True)
            .exclude(product__name="")
            .values_list("product__name", flat=True)
        )
        if request_pk
        else []
    )
    skus = ", ".join(dict.fromkeys(sku_names)) if sku_names else "-"
    activation_date = _format_dt_no_tz(start_dt, "%m/%d/%Y", offset_minutes)
    start_time = _format_dt_no_tz(start_dt, "%I:%M %p", offset_minutes)
    end_time = _format_dt_no_tz(end_dt, "%I:%M %p", offset_minutes)

    rate = getattr(job, "hourly_rate", None)
    hours = getattr(job, "total_hours", None)
    if rate is not None and hours is not None:
        pay = f"${rate}/hr x {hours} hrs"
    elif rate is not None:
        pay = f"${rate}/hr"
    else:
        pay = "-"

    deep_link = f"spark://my-gigs/{job.uuid}"

    return {
        "request_id": request_id,
        "brand_name": brand_name,
        "campaign_name": campaign_name,
        "event_type": event_type,
        "location_name": location_name,
        "show_location": not retailer_is_national,
        "market_name": market_name,
        "skus": skus,
        "event_address": event_address,
        "activation_date": activation_date,
        "start_time": start_time,
        "end_time": end_time,
        "pay": pay,
        "deep_link": deep_link,
        "event_notes": getattr(event, "notes", None) or job.description or "-",
    }


class JobBookingConfirmationMailer(Mailer):
    """Confirmation email sent to a BA when they're booked onto a Job via
    the marketplace accept/assign flow (assign_ambassador_to_job).

    Reuses the `ambassador_assigned_to_job_default` template — the context
    keys match — but is built from a Job (no AmbassadorJob row exists in
    that flow)."""

    def __init__(
        self,
        job: "models.Job",
        to_emails: list[str],
        recipient_first_name: str | None = None,
        reply_to_email: str | None = None,
    ) -> None:
        self.job = job
        self.to_emails = to_emails
        self.recipient_first_name = recipient_first_name
        self.reply_to_email = reply_to_email or "events@igniteproductions.co"

    def envelope(self) -> Envelope:
        context = _build_job_booking_email_context(self.job)

        return Envelope(
            subject="You're booked - here are your event details",
            template="jobs.templates.emails.ambassador_assigned_to_job_default",
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


class AmbassadorAppliedJobUpdatedMailer(Mailer):
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
            subject="Your applied job details have been updated",
            template="jobs.templates.emails.ambassador_applied_job_updated",
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


class AmbassadorInvitedJobUpdatedMailer(Mailer):
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
            subject="Your invited job details have been updated",
            template="jobs.templates.emails.ambassador_invited_job_updated",
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
        tenant_slug = (
            (getattr(self.ambassador_job.tenant, "slug", None) or "").strip().lower()
        )
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


class AmbassadorEventReminderMailer(Mailer):
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
            "jobs.templates.emails.ambassador_job_event_reminder_liquid_death"
            if tenant_slug == self.LIQUID_DEATH_TENANT_SLUG
            else "jobs.templates.emails.ambassador_job_event_reminder"
        )

        return Envelope(
            subject="Reminder: your event starts within 24 hours",
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


class AmbassadorEventReminder3HoursMailer(Mailer):
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
            "jobs.templates.emails.ambassador_job_event_reminder_3h_liquid_death"
            if tenant_slug == self.LIQUID_DEATH_TENANT_SLUG
            else "jobs.templates.emails.ambassador_job_event_reminder_3h"
        )

        return Envelope(
            subject="Reminder: your event starts within 3 hours",
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
    event_type = getattr(getattr(event, "event_type", None), "name", None) or ""
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
    sku_names = (
        list(
            RequestProduct.objects.filter(request_id=event.request_id)
            .select_related("product")
            .exclude(product__name__isnull=True)
            .exclude(product__name="")
            .values_list("product__name", flat=True)
        )
        if event.request_id
        else []
    )
    skus = ", ".join(dict.fromkeys(sku_names)) if sku_names else "-"
    activation_date = _format_dt_no_tz(start_dt, "%m/%d/%Y", offset_minutes)
    start_time = _format_dt_no_tz(start_dt, "%I:%M %p", offset_minutes)
    end_time = _format_dt_no_tz(end_dt, "%I:%M %p", offset_minutes)
    deep_link = f"spark://my-gigs/{ambassador_job.id}"

    return {
        "request_id": request_id,
        "brand_name": brand_name,
        "campaign_name": campaign_name,
        "event_type": event_type,
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
