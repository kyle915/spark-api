import datetime
from html import escape

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
        attachments: list[dict] | None = None,
    ) -> None:
        self.recap = recap
        self.to_emails = to_emails
        self.recipient_first_name = recipient_first_name
        self.reply_to_email = reply_to_email or "events@igniteproductions.co"
        # PDF (or other) attachments — passed through to Envelope so
        # the recipient gets the recap document inline with the
        # approval notification.
        self.attachments = attachments or []

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
        frontend_base_url = str(
            getattr(
                settings,
                "CLIENT_FRONTEND_URL",
                "https://spark.igniteproductions.co",
            )
        ).rstrip("/")
        is_custom_recap = isinstance(self.recap, models.CustomRecap)
        recap_link = (
            f"{frontend_base_url}/recap/view-custom/{self.recap.uuid}"
            if is_custom_recap
            else "https://spark.igniteproductions.co/"
        )
        template = (
            "recaps.templates.emails.custom_recap_approved_notification"
            if is_custom_recap
            else "recaps.templates.emails.recap_approved_notification"
        )

        return Envelope(
            subject="Your activation recap is ready",
            template=template,
            to_emails=self.to_emails,
            headers={"Reply-To": self.reply_to_email},
            attachments=self.attachments,
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
                "recap_link": recap_link,
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


# ─── Campaign Report email ──────────────────────────────────────
#
# Wraps a generated campaign-report PDF (recaps/pdf.py
# build_campaign_report_pdf) in a client-facing email. Sent from the
# `emailCampaignReport` mutation when the admin picks "Email to
# client@…" instead of "Download" on the Recaps multi-select bar.
#
# Keeping render + send separate means the same PDF artifact is
# reusable: future "download + email" combo doesn't render twice.

import base64 as _base64
from typing import Iterable as _Iterable


class CampaignReportMailer(Mailer):
    """Send a campaign-report PDF as an email attachment."""

    def __init__(
        self,
        *,
        recipients: _Iterable[str],
        campaign_title: str,
        campaign_subtitle: str,
        cover_message: str | None,
        recap_count: int,
        total_consumers: int | None,
        sender_tenant_name: str | None,
        pdf_bytes: bytes,
        pdf_filename: str,
        event_meta: dict | None = None,
    ) -> None:
        # De-dup recipients case-insensitively + strip whitespace.
        # Single-address case works trivially; multi-recipient
        # typo-protection is cheap insurance.
        seen: set[str] = set()
        clean: list[str] = []
        for r in recipients:
            norm = (r or "").strip()
            if not norm:
                continue
            key = norm.lower()
            if key in seen:
                continue
            seen.add(key)
            clean.append(norm)
        self.recipients = clean
        self.campaign_title = campaign_title
        self.campaign_subtitle = campaign_subtitle
        self.cover_message = (cover_message or "").strip() or None
        self.recap_count = recap_count
        self.total_consumers = total_consumers
        self.sender_tenant_name = (sender_tenant_name or "").strip() or None
        self.event_meta = event_meta or {}
        self.pdf_bytes = pdf_bytes
        self.pdf_filename = pdf_filename

    def envelope(self) -> Envelope:
        action_chip = (
            f"{self.recap_count} recap{'s' if self.recap_count != 1 else ''}"
        )
        subject = f"{self.campaign_title} — {action_chip}"
        # Resend accepts base64-encoded content directly. Matches the
        # codebase's existing attachment pattern (utils/gcs inline
        # base64). content_type kept explicit for the EmailMulti-
        # Alternatives fallback path.
        encoded = _base64.b64encode(self.pdf_bytes).decode("ascii")
        return Envelope(
            subject=subject,
            template="recaps.templates.emails.campaign_report",
            to_emails=self.recipients,
            context={
                "campaign_title": self.campaign_title,
                "campaign_subtitle": self.campaign_subtitle,
                "cover_message": self.cover_message,
                "recap_count": self.recap_count,
                "total_consumers": self.total_consumers,
                "sender_tenant_name": self.sender_tenant_name,
                "client_name": self.event_meta.get("client_name")
                or self.sender_tenant_name,
                "event_count": self.event_meta.get("event_count") or 0,
                "event_label": self.event_meta.get("event_label"),
                "date_label": self.event_meta.get("date_label"),
                "state_label": self.event_meta.get("state_label"),
                "location_label": self.event_meta.get("location_label"),
            },
            attachments=[
                {
                    "filename": self.pdf_filename,
                    "content": encoded,
                    "content_type": "application/pdf",
                }
            ],
        )


# ─── Scheduled monthly client-report email ──────────────────────
#
# Wraps a generated monthly performance-report PDF
# (recaps/client_report.py build_client_monthly_report_pdf) in a
# client-facing email. Sent by the `send_scheduled_client_reports`
# cron once per opted-in tenant per month — NOT a user-triggered
# mutation. Mirrors CampaignReportMailer above (same base64 +
# application/pdf attachment shape Resend / the Mailpit fallback
# expect) but with an inline HTML body (the `html` kwarg bypasses
# the Django template loader) so this scheduled report needs no new
# template file.


class ClientMonthlyReportMailer(Mailer):
    """Email a tenant's monthly performance-report PDF to its client contacts."""

    def __init__(
        self,
        *,
        recipients: _Iterable[str],
        tenant_name: str,
        period_label: str,
        pdf_bytes: bytes,
        pdf_filename: str,
        reply_to_email: str | None = None,
    ) -> None:
        # De-dup recipients case-insensitively + strip whitespace (the cron
        # already resolves these from Tenant.scheduled_report_recipients(),
        # but de-duping here is cheap typo-protection — same posture as
        # CampaignReportMailer).
        seen: set[str] = set()
        clean: list[str] = []
        for r in recipients:
            norm = (r or "").strip()
            if not norm:
                continue
            key = norm.lower()
            if key in seen:
                continue
            seen.add(key)
            clean.append(norm)
        self.recipients = clean
        self.tenant_name = (tenant_name or "").strip() or "Your brand"
        self.period_label = (period_label or "").strip() or "Monthly"
        self.pdf_bytes = pdf_bytes
        self.pdf_filename = pdf_filename
        # Replies route to the events inbox so a client can respond to the
        # report directly — same default the recap-approval email uses.
        self.reply_to_email = (
            (reply_to_email or "").strip() or "events@igniteproductions.co"
        )

    def _subject_line(self) -> str:
        return f"{self.tenant_name} — {self.period_label} performance report"

    def _html_body(self) -> str:
        # Short, plain body built inline (escaped) — the numbers live in the
        # attached PDF, so the email itself is just a friendly cover note.
        tenant = escape(self.tenant_name)
        period = escape(self.period_label)
        return (
            '<div style="font-family:Arial,Helvetica,sans-serif;font-size:14px;'
            'color:#111;line-height:1.5">'
            f"<p>Hi there,</p>"
            f"<p>Attached is the {period} performance report for "
            f"<strong>{tenant}</strong> — a summary of the month's reach, "
            f"sampling, sales, and highlights from the field.</p>"
            "<p>If you have any questions, just reply to this email.</p>"
            '<p style="color:#666">— The Ignite team</p>'
            "</div>"
        )

    def envelope(self) -> Envelope:
        # Resend accepts base64-encoded content directly; content_type kept
        # explicit for the Mailpit EmailMultiAlternatives fallback path.
        encoded = _base64.b64encode(self.pdf_bytes).decode("ascii")
        return Envelope(
            subject=self._subject_line(),
            from_email=getattr(
                settings,
                "DEFAULT_FROM_EMAIL",
                "Spark by Ignite <no-reply@igniteproductions.co>",
            ),
            to_emails=self.recipients,
            headers={"Reply-To": self.reply_to_email},
            html=self._html_body(),
            attachments=[
                {
                    "filename": self.pdf_filename,
                    "content": encoded,
                    "content_type": "application/pdf",
                }
            ],
        )


def _client_base_url() -> str:
    """Client-app base URL for digest deep-links (no trailing slash)."""
    base = getattr(settings, "CLIENT_FRONTEND_URL", "") or ""
    return base.rstrip("/")


def _fmt_when(value: "datetime.datetime | None", *, with_time: bool = True) -> str:
    """Readable date (+ optional time) for the digest, displayed as-stored.

    Mirrors :func:`_format_dt_no_tz`: we strip tzinfo and format the wall-clock
    value the event was saved with, rather than converting between zones. The
    rest of the app's emails read times this way, so the digest stays
    consistent and doesn't reintroduce the DST-conversion drift we already
    fought elsewhere.
    """
    if not value:
        return "TBD"
    naive = value.replace(tzinfo=None)
    day = naive.strftime("%a, %b %d").replace(" 0", " ")
    if not with_time:
        return day
    clock = naive.strftime("%I:%M %p").lstrip("0")
    return f"{day} · {clock}"


class ClientWeeklyDigestMailer(Mailer):
    """Email a client a once-a-week per-tenant rollup.

    Three sections, all gated by the caller on ``Tenant.scheduled_report_enabled``:
      * **This week at a glance** — what ran + the headline KPIs (last 7 days).
      * **Coming up** — activations in the next 7 days.
      * **Needs your approval** — requests still awaiting sign-off.

    Pre-formats every value into plain strings here so the template is a dumb
    renderer (no tz math, no model access at render time).
    """

    def __init__(
        self,
        *,
        recipients: "_Iterable[str]",
        tenant_name: str,
        digest: "WeeklyDigest",
        reply_to_email: str | None = None,
    ) -> None:
        # De-dup recipients case-insensitively (same posture as the monthly
        # report mailer) — cheap typo protection on top of the cron's resolve.
        seen: set[str] = set()
        clean: list[str] = []
        for r in recipients:
            norm = (r or "").strip()
            if not norm:
                continue
            key = norm.lower()
            if key in seen:
                continue
            seen.add(key)
            clean.append(norm)
        self.recipients = clean
        self.tenant_name = (tenant_name or "").strip() or "Your brand"
        self.digest = digest
        self.reply_to_email = (
            (reply_to_email or "").strip() or "events@igniteproductions.co"
        )

    def _period_label(self) -> str:
        start = (
            self.digest.start.replace(tzinfo=None).strftime("%b %d").replace(" 0", " ")
        )
        end = (
            self.digest.end.replace(tzinfo=None).strftime("%b %d").replace(" 0", " ")
        )
        return f"{start} – {end}"

    def _subject_line(self) -> str:
        d = self.digest
        # Lead the subject with the single most action-worthy fact so it reads
        # well in a crowded inbox: pending approvals first, else what's coming.
        if d.pending_total:
            tail = (
                f"{d.pending_total} awaiting approval"
                if d.pending_total != 1
                else "1 awaiting approval"
            )
        elif d.upcoming_total:
            tail = f"{d.upcoming_total} coming up"
        else:
            tail = "your weekly recap"
        return f"{self.tenant_name}: {tail} — week of {self._period_label()}"

    def _context(self) -> dict:
        d = self.digest
        base = _client_base_url()
        kpis = d.kpis
        return {
            "tenant_name": self.tenant_name,
            "period_label": self._period_label(),
            "glance": {
                "completed_activations": d.completed_activations,
                "recaps_filed": d.recaps_filed,
                "total_engagements": kpis.total_engagements,
                "samples_distributed": kpis.samples_distributed,
                "products_sold": kpis.products_sold,
            },
            "upcoming": [
                {
                    "name": u.name,
                    "when": _fmt_when(u.when),
                    "address": u.address,
                }
                for u in d.upcoming
            ],
            "upcoming_total": d.upcoming_total,
            "upcoming_overflow": d.upcoming_overflow,
            "pending": [
                {
                    "name": p.name,
                    "when": _fmt_when(p.when, with_time=False),
                    "url": f"{base}/request/view/{p.uuid}" if base else "",
                }
                for p in d.pending
            ],
            "pending_total": d.pending_total,
            "pending_overflow": d.pending_overflow,
            "links": {
                "dashboard": f"{base}/" if base else "",
                "tracker": f"{base}/master-tracker" if base else "",
                "approvals": f"{base}/approvals" if base else "",
            },
        }

    def envelope(self) -> Envelope:
        return Envelope(
            subject=self._subject_line(),
            template="events.templates.emails.client_weekly_digest",
            from_email=getattr(
                settings,
                "DEFAULT_FROM_EMAIL",
                "Spark by Ignite <no-reply@igniteproductions.co>",
            ),
            to_emails=self.recipients,
            headers={"Reply-To": self.reply_to_email},
            context=self._context(),
        )
