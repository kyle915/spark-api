import datetime

from django.conf import settings

from ambassadors.models import Attendance
from jobs.models import AmbassadorJob
from recaps import models
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


def _normalize_slug(slug: str | None) -> str:
    return (slug or "").strip().lower().replace("-", "_")


class RecapApprovedNotificationMailer(Mailer):
    def __init__(
        self,
        recap: models.Recap | models.CustomRecap,
        to_emails: list[str],
        recipient_first_name: str | None = None,
        reply_to_email: str | None = None,
    ) -> None:
        self.recap = recap
        self.to_emails = to_emails
        self.recipient_first_name = recipient_first_name
        self.reply_to_email = reply_to_email or "events@igniteproductions.co"

    def _location_name(self) -> str:
        if self.recap.retailer and self.recap.retailer.name:
            return self.recap.retailer.name
        if self.recap.job and self.recap.job.address:
            return self.recap.job.address
        if self.recap.event and self.recap.event.address:
            return self.recap.event.address
        return "-"

    def _attendance_window(self, offset_minutes: int) -> tuple[str, str]:
        if not self.recap.ambassador_id:
            return "-", "-"

        attendances = Attendance.objects.select_related("attendace_type").filter(
            event_id=self.recap.event_id,
            ambassador_id=self.recap.ambassador_id,
        )
        if self.recap.job_id:
            attendances = attendances.filter(job_id=self.recap.job_id)

        clock_in_times = []
        clock_out_times = []
        for record in attendances:
            slug = _normalize_slug(getattr(record.attendace_type, "slug", None))
            if slug == "clock_in":
                clock_in_times.append(record.clock_time)
            elif slug == "clock_out":
                clock_out_times.append(record.clock_time)

        actual_check_in = (
            _format_dt_no_tz(min(clock_in_times), "%I:%M %p", offset_minutes)
            if clock_in_times
            else "-"
        )
        actual_check_out = (
            _format_dt_no_tz(max(clock_out_times), "%I:%M %p", offset_minutes)
            if clock_out_times
            else "-"
        )
        return actual_check_in, actual_check_out

    def _ba_on_site_count(self) -> int:
        if self.recap.job_id:
            return (
                AmbassadorJob.objects.filter(
                    job_id=self.recap.job_id,
                    status__slug="approved",
                )
                .values("ambassador_id")
                .distinct()
                .count()
            )
        return (
            AmbassadorJob.objects.filter(
                job__event_id=self.recap.event_id,
                status__slug="approved",
            )
            .values("ambassador_id")
            .distinct()
            .count()
        )

    def _photos_count(self) -> int:
        recap_files = getattr(self.recap, "recap_files", None)
        if recap_files is not None:
            return recap_files.count()

        custom_recap_files = getattr(self.recap, "custom_recap_files", None)
        if custom_recap_files is not None:
            return custom_recap_files.count()

        return 0

    def envelope(self) -> Envelope:
        event = self.recap.event
        tenant = event.tenant
        job = self.recap.job
        timezone_obj = self.recap.timezone or event.timezone
        offset_minutes = int(getattr(timezone_obj, "offset", 0) or 0)

        start_dt = (
            job.start_date if job and job.start_date else None
        ) or event.start_time
        end_dt = (job.end_date if job and job.end_date else None) or event.end_time
        recap_date_source = start_dt or event.date

        request_id = (
            f"REQ-{event.request_id}"
            if getattr(event, "request_id", None)
            else f"RECAP-{self.recap.id}"
        )
        location_name = self._location_name()
        actual_check_in, actual_check_out = self._attendance_window(offset_minutes)
        ba_on_site = self._ba_on_site_count()
        photos_count = self._photos_count()
        client_metrics = []
        products_sold = getattr(self.recap, "products_sold", None)
        if products_sold is not None:
            client_metrics.append(f"products sold: {products_sold}")
        if self.recap.total_engagements is not None:
            client_metrics.append(f"engagements: {self.recap.total_engagements}")
        total_cans_sold = getattr(self.recap, "total_cans_sold", None)
        if total_cans_sold is not None:
            client_metrics.append(f"cans sold: {total_cans_sold}")
        total_packs_sold = getattr(self.recap, "total_packs_sold", None)
        if total_packs_sold is not None:
            client_metrics.append(f"packs sold: {total_packs_sold}")
        client_specific_metrics = (
            ", ".join(client_metrics)
            if client_metrics
            else "Samples distributed, leads captured, survey responses"
        )

        return Envelope(
            subject="Your activation recap is ready 📊",
            template="recaps.templates.emails.recap_approved_notification",
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
                "brand_name": tenant.name or "-",
                "campaign_name": event.name or "-",
                "location_name": location_name,
                "date_text": _format_dt_no_tz(
                    recap_date_source, "%m/%d/%Y", offset_minutes
                ),
                "scheduled_start_time": _format_dt_no_tz(
                    start_dt, "%I:%M %p", offset_minutes
                ),
                "scheduled_end_time": _format_dt_no_tz(
                    end_dt, "%I:%M %p", offset_minutes
                ),
                "actual_check_in": actual_check_in,
                "actual_check_out": actual_check_out,
                "ba_on_site": ba_on_site,
                "extensions_text": "None",
                "photos_count": photos_count,
                "client_specific_metrics": client_specific_metrics,
                "recap_link": "https://spark.igniteproductions.co/",
            },
        )


class RecapReadyForReviewAdminMailer(Mailer):
    def __init__(
        self,
        recap: models.Recap | models.CustomRecap,
        to_emails: list[str],
        ambassador_name: str | None = None,
    ) -> None:
        self.recap = recap
        self.to_emails = to_emails
        self.ambassador_name = ambassador_name

    def envelope(self) -> Envelope:
        tenant = self.recap.event.tenant
        ambassador_label = self.ambassador_name or "Ambassador"
        frontend_base_url = str(
            getattr(
                settings,
                "ADMIN_FRONTEND_URL",
                "https://spark-admin.igniteproductions.co",
            )
        ).rstrip("/")
        review_link = f"{frontend_base_url}/recap/view-custom/{self.recap.uuid}"

        return Envelope(
            subject="Recap ready for review",
            template="recaps.templates.emails.recap_ready_for_review_admin_notification",
            to_emails=self.to_emails,
            from_email=getattr(
                settings,
                "DEFAULT_FROM_EMAIL",
                "Spark by Ignite <no-reply@igniteproductions.co>",
            ),
            context={
                "ambassador_name": ambassador_label,
                "brand_name": tenant.name or "-",
                "review_link": review_link,
            },
        )
