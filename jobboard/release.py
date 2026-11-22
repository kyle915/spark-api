"""Signed self-serve BA shift release & <36h staffing escalation.

Rules (Section 2.4):
* ``> 36 hours`` before ``starts_at``: 1-tap release with reason removes the
  ambassador's shift assignment, re-opens the slot on ``/jobs/<token>``,
  creates an ``OpenShift`` row, triggers ``send_open_shift_alerts``, emails
  the BA confirmation, and notifies Ignite staffing (internal only — never
  the client).
* ``<= 36 hours`` before ``starts_at``: blocks silent auto-drop so the shift is
  never left uncovered without human eyes, and immediately routes an urgent
  escalation alert (high-priority email + Spark-admin push) to Ignite staffing
  while prompting the BA to text/call staffing directly.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from functools import partial
from urllib.parse import quote

from asgiref.sync import async_to_sync
from django.core import signing
from django.core.cache import cache
from django.core.management import call_command
from django.db import transaction
from django.utils import timezone
from django.utils.html import escape

from ambassadors.models import AmbassadorEvent, Attendance, OpenShift
from events.envelopes import _admin_request_url
from events.event_confirmations import (
    CONFIRMATION_FROM_EMAIL,
    CONFIRMATION_REPLY_TO,
    SUPPORT_PHONE,
    SUPPORT_PHONE_HREF,
    build_cancellation_context,
    build_cancellation_subject,
    format_event_date,
    format_time_range,
    public_page_base,
)
from events.field_marketing_mail import FROM_EMAIL, SPARK_LIME, admin_plan_url
from events.models import Event, EventConfirmation
from jobboard.config import job_board_config_for
from jobboard.gigs import STAFFING_EMAIL, date_label, list_gigs, time_label
from jobboard.models import JobBoard, JobBoardBooking
from jobs.models import AmbassadorJob
from utils.mailer import Envelope, Mailer

logger = logging.getLogger(__name__)

RELEASE_TOKEN_SALT = "jobboard.shift_release"
RELEASE_TOKEN_MAX_AGE_SECONDS = 90 * 24 * 3600
SELF_RELEASE_MIN_HOURS = 36
SELF_RELEASE_MIN_LEAD = timedelta(hours=SELF_RELEASE_MIN_HOURS)
SPARK_ORANGE = "#ef5a2a"


class ReleaseError(Exception):
    def __init__(self, code: str, message: str, *, status: int = 400):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status


@dataclass
class ReleaseTarget:
    booking: JobBoardBooking | None
    confirmation: EventConfirmation | None
    tenant: object
    event: Event | None
    board_token: str
    brand_label: str
    type_label: str
    venue: str
    address: str
    starts_at: datetime
    date_label: str
    time_label: str
    ba_name: str
    ba_first_name: str
    ba_email: str
    ba_phone: str

    @property
    def cache_id(self) -> str:
        if self.booking is not None:
            return f"b:{self.booking.uuid}"
        if self.confirmation is not None:
            return f"c:{self.confirmation.uuid}"
        return "unknown"

    @property
    def ambassador_id(self) -> int | None:
        if self.booking and self.booking.ambassador_id:
            return self.booking.ambassador_id
        if (
            self.confirmation
            and self.confirmation.ambassador_event_id
            and self.confirmation.ambassador_event
        ):
            return self.confirmation.ambassador_event.ambassador_id
        return None


def make_shift_release_token(
    *,
    booking: JobBoardBooking | None = None,
    confirmation: EventConfirmation | None = None,
) -> str:
    payload: dict[str, str] = {}
    if booking is not None and getattr(booking, "uuid", None):
        payload["b"] = str(booking.uuid)
    if confirmation is not None and getattr(confirmation, "uuid", None):
        payload["c"] = str(confirmation.uuid)
    if not payload:
        return ""
    return signing.dumps(payload, salt=RELEASE_TOKEN_SALT)


def _board_token_for_tenant(tenant) -> str:
    if tenant is None or not getattr(tenant, "pk", None):
        return ""
    if job_board_config_for(tenant) is None:
        return ""
    token = (
        JobBoard.objects.filter(tenant=tenant)
        .values_list("token", flat=True)
        .first()
    )
    if token:
        return token
    board, _ = JobBoard.objects.get_or_create(tenant=tenant)
    return board.token


def shift_release_url_for(
    *,
    booking: JobBoardBooking | None = None,
    confirmation: EventConfirmation | None = None,
    board_token: str = "",
) -> str:
    if booking is None and confirmation is not None and getattr(confirmation, "pk", None):
        booking = (
            JobBoardBooking.objects.filter(confirmation=confirmation)
            .select_related("tenant")
            .first()
        )
    token = make_shift_release_token(booking=booking, confirmation=confirmation)
    if not token:
        return ""
    tenant = (
        getattr(booking, "tenant", None)
        or getattr(confirmation, "tenant", None)
    )
    resolved_board = (board_token or "").strip() or _board_token_for_tenant(tenant)
    base = public_page_base().rstrip("/")
    encoded = quote(token, safe="")
    if resolved_board:
        return f"{base}/jobs/{quote(resolved_board, safe='')}?release={encoded}"
    return f"{base}/jobs/release/{encoded}"


def _format_us_phone(digits: str) -> str:
    clean = "".join(ch for ch in (digits or "") if ch.isdigit())
    if len(clean) == 10:
        return f"({clean[:3]}) {clean[3:6]}-{clean[6:]}"
    return (digits or "").strip()


def resolve_release_target(release_token: str) -> ReleaseTarget | None:
    raw = (release_token or "").strip()
    if not raw or len(raw) > 512:
        return None
    try:
        data = signing.loads(
            raw,
            salt=RELEASE_TOKEN_SALT,
            max_age=RELEASE_TOKEN_MAX_AGE_SECONDS,
        )
    except signing.BadSignature:
        return None
    if not isinstance(data, dict):
        return None

    booking: JobBoardBooking | None = None
    confirmation: EventConfirmation | None = None

    b_uuid = data.get("b")
    c_uuid = data.get("c")

    if b_uuid:
        booking = (
            JobBoardBooking.objects.select_related(
                "tenant",
                "event",
                "event__request",
                "ambassador",
                "ambassador__user",
                "confirmation",
                "confirmation__tenant",
            )
            .filter(uuid=b_uuid)
            .first()
        )
        if booking and booking.confirmation_id and confirmation is None:
            confirmation = booking.confirmation

    if c_uuid and confirmation is None:
        confirmation = (
            EventConfirmation.objects.select_related(
                "tenant",
                "event",
                "event__request",
                "ambassador_event",
                "ambassador_event__ambassador",
                "ambassador_event__ambassador__user",
            )
            .filter(uuid=c_uuid)
            .first()
        )
        if confirmation is not None and booking is None:
            booking = (
                JobBoardBooking.objects.select_related(
                    "tenant",
                    "event",
                    "event__request",
                    "ambassador",
                    "ambassador__user",
                )
                .filter(confirmation=confirmation)
                .first()
            )

    if booking is None and confirmation is None:
        return None

    tenant = booking.tenant if booking is not None else confirmation.tenant
    event = (
        booking.event
        if booking is not None
        else (confirmation.event if confirmation is not None else None)
    )
    config = job_board_config_for(tenant)
    brand_label = config.brand_label if config else (getattr(tenant, "name", "") or "").strip()
    board_token = _board_token_for_tenant(tenant)

    gig = None
    if event is not None:
        gigs = list_gigs(tenant, now=datetime(2000, 1, 1, tzinfo=UTC), event_ids=[event.id])
        gig = gigs[0] if gigs else None

    if gig is not None:
        starts_at = gig.starts_at
        venue = gig.venue
        type_label = gig.type_label
        d_label = date_label(gig)
        t_label = time_label(gig)
        address = (event.address or "").strip() if event else ""
    elif confirmation is not None:
        starts_at = confirmation.starts_at
        venue = (confirmation.store_name or (event.name if event else "") or "").strip()
        type_label = (confirmation.event_type_label or "Shift").strip()
        d_label = format_event_date(confirmation.local_start())
        t_label = format_time_range(confirmation.local_start(), confirmation.local_end())
        address = (confirmation.address or "").strip()
    else:
        starts_at = (event.start_time or event.date) if event else timezone.now()
        venue = (event.name if event else "Shift").strip()
        type_label = "Shift"
        d_label = starts_at.strftime("%a, %b %-d")
        t_label = ""
        address = (event.address or "").strip() if event else ""

    ba_name = (
        (booking.name if booking else "")
        or (confirmation.ba_name if confirmation else "")
        or "Ambassador"
    ).strip()
    ba_first_name = ba_name.split(" ")[0] if ba_name else "there"
    ba_email = (
        (booking.email if booking else "")
        or (confirmation.ba_email if confirmation else "")
    ).strip()
    ba_phone = ""
    if booking and booking.phone:
        ba_phone = booking.phone
    elif (
        confirmation
        and confirmation.ambassador_event
        and confirmation.ambassador_event.ambassador
        and confirmation.ambassador_event.ambassador.phone
    ):
        ba_phone = confirmation.ambassador_event.ambassador.phone

    return ReleaseTarget(
        booking=booking,
        confirmation=confirmation,
        tenant=tenant,
        event=event,
        board_token=board_token,
        brand_label=brand_label,
        type_label=type_label,
        venue=venue,
        address=address,
        starts_at=starts_at,
        date_label=d_label,
        time_label=t_label,
        ba_name=ba_name,
        ba_first_name=ba_first_name,
        ba_email=ba_email,
        ba_phone=ba_phone,
    )


def _is_already_cancelled(target: ReleaseTarget) -> bool:
    if target.booking is not None and target.booking.cancelled_at is not None:
        return True
    if target.confirmation is not None and target.confirmation.cancelled_at is not None:
        return True
    if (
        target.booking is not None
        and target.event is not None
        and target.booking.ambassador_id
        and not AmbassadorEvent.objects.filter(
            event_id=target.event.id,
            ambassador_id=target.booking.ambassador_id,
            is_approved=True,
        ).exists()
    ):
        return True
    return False


def _is_already_checked_in(target: ReleaseTarget) -> bool:
    amb_id = target.ambassador_id
    if not amb_id or target.event is None:
        return False
    return Attendance.objects.filter(
        event_id=target.event.id,
        ambassador_id=amb_id,
    ).exists()


def inspect_shift_release(target: ReleaseTarget, *, now: datetime | None = None) -> dict:
    now = now or timezone.now()
    lead = target.starts_at - now
    hours_until = round(lead.total_seconds() / 3600.0, 1)

    cancelled = _is_already_cancelled(target)
    checked_in = _is_already_checked_in(target)

    if cancelled:
        status = "already_released"
    elif checked_in or lead <= timedelta(0):
        status = "already_started"
    elif lead > SELF_RELEASE_MIN_LEAD:
        status = "can_release"
    else:
        status = "urgent_window"

    return {
        "brandLabel": target.brand_label,
        "typeLabel": target.type_label,
        "venue": target.venue,
        "dateLabel": target.date_label,
        "timeLabel": target.time_label,
        "startsAt": target.starts_at.isoformat(),
        "hoursUntilStart": hours_until,
        "minLeadHours": SELF_RELEASE_MIN_HOURS,
        "baFirstName": target.ba_first_name,
        "status": status,
        "canSelfRelease": status == "can_release",
        "contactEmail": STAFFING_EMAIL,
        "supportPhone": SUPPORT_PHONE,
        "supportPhoneHref": SUPPORT_PHONE_HREF,
        "boardToken": target.board_token or None,
    }


def _staffing_recipients(tenant) -> list[str]:
    config = job_board_config_for(tenant)
    seen: set[str] = set()
    recipients: list[str] = []
    for addr in [STAFFING_EMAIL, *(config.internal_emails if config else ())]:
        clean = (addr or "").strip().lower()
        if clean and clean not in seen:
            seen.add(clean)
            recipients.append(clean)
    return recipients


class JobBoardShiftReleaseInternalMailer(Mailer):
    """Notifies Ignite staffing of either a >36h self-release or a <=36h urgent callout."""

    def _build_logo_attachment(self):
        return None

    def __init__(
        self,
        *,
        target: ReleaseTarget,
        reason: str,
        urgent: bool,
        hours_until: float,
        spots_summary: str = "",
    ):
        self.target = target
        self.reason = reason
        self.urgent = urgent
        self.hours_until = hours_until
        self.spots_summary = spots_summary

    def envelope(self) -> Envelope:
        t = self.target
        phone_fmt = _format_us_phone(t.ba_phone)
        digits = "".join(ch for ch in (t.ba_phone or "") if ch.isdigit())
        call_href = f"tel:+1{digits}" if len(digits) == 10 else ""
        when = " · ".join(p for p in (t.date_label, t.time_label) if p)

        rows = [
            ("Ambassador", t.ba_name),
            ("Cell", phone_fmt),
            ("Email", t.ba_email),
            ("Brand", t.brand_label),
            ("Venue", t.venue),
            ("Type", t.type_label),
            ("When", when),
            ("Lead time", f"{max(self.hours_until, 0):.1f}h before start"),
            ("Address", t.address),
            ("Reason", self.reason),
        ]

        if self.urgent:
            subject = (
                f"[URGENT <36h] Shift callout: {t.ba_name} · {t.brand_label} · {t.venue}"
                + (f" · {t.date_label}" if t.date_label else "")
            )
            eyebrow = f"URGENT <36H CALLOUT · {t.brand_label.upper()}"
            eyebrow_color = SPARK_ORANGE
            headline = f"{t.ba_name} can't make {t.venue} ({max(self.hours_until, 0):.1f}h out)"
            banner_bg = "#fdeee8"
            banner_border = SPARK_ORANGE
            banner_html = (
                "<strong>Auto-release blocked (&le;36h guardrail).</strong> "
                f"{escape(t.ba_name)} is still on the shift until ops confirms coverage. "
                "Call or text immediately and re-assign or open the slot."
            )
        else:
            subject = (
                f"Shift released (>36h): {t.ba_name} · {t.brand_label} · {t.venue}"
                + (f" · {t.date_label}" if t.date_label else "")
            )
            eyebrow = f"{t.brand_label.upper()} · SELF-SERVE SHIFT RELEASE"
            eyebrow_color = "#5a6354"
            headline = f"{t.ba_name} released {t.venue}"
            banner_bg = "#f4fbe0"
            banner_border = SPARK_LIME
            extra = f" ({escape(self.spots_summary)})" if self.spots_summary else ""
            banner_html = (
                f"<strong>Slot re-opened automatically{extra}.</strong> "
                "Open-shift alerts have been dispatched to eligible local ambassadors."
            )

        request_obj = getattr(t.event, "request", None) if t.event is not None else None
        headers = {"Reply-To": t.ba_email} if t.ba_email else {}
        if self.urgent:
            headers["X-Priority"] = "1"
            headers["Importance"] = "high"

        return Envelope(
            subject=subject,
            template="jobboard.templates.emails.job_board_shift_release_internal",
            from_email=FROM_EMAIL,
            to_emails=_staffing_recipients(t.tenant),
            headers=headers,
            context={
                "subject": subject,
                "eyebrow": eyebrow,
                "eyebrow_color": eyebrow_color,
                "headline": headline,
                "banner_bg": banner_bg,
                "banner_border": banner_border,
                "banner_html": banner_html,
                "rows": [(k, v) for k, v in rows if v],
                "call_href": call_href if self.urgent else "",
                "request_url": _admin_request_url(request_obj) if request_obj else "",
                "board_url": admin_plan_url() if t.board_token else "",
                "accent": SPARK_LIME,
            },
        )


class JobBoardSelfReleaseBAMailer(Mailer):
    """Confirms to the BA that their >36h self-serve release went through."""

    def __init__(self, confirmation: EventConfirmation, brand_label: str):
        self.confirmation = confirmation
        self.brand_label = brand_label

    def envelope(self) -> Envelope:
        c = self.confirmation
        context = build_cancellation_context(c)
        context["intro_html"] = (
            f"We've released your <strong>{escape(self.brand_label)} "
            f"{escape((c.event_type_label or 'shift').lower())}</strong> shift and opened "
            "the spot for another ambassador. Thanks for letting us know ahead of time!"
        )
        return Envelope(
            subject=build_cancellation_subject(c),
            template="events.templates.emails.event_confirmation_cancelled",
            to_emails=[c.ba_email],
            headers={"Reply-To": CONFIRMATION_REPLY_TO},
            from_email=CONFIRMATION_FROM_EMAIL,
            context=context,
        )


def _notify_admin_push(tenant, *, title: str, body: str, kind: str) -> None:
    try:
        from ambassadors.mutations import _spark_admin_user_ids
        from ambassadors.push import send_push_to_user

        watcher_ids = _spark_admin_user_ids(tenant=tenant)
        for uid in watcher_ids:
            async_to_sync(send_push_to_user)(
                uid,
                title=title,
                body=body,
                data={"kind": kind, "screen": "today"},
            )
    except Exception:  # noqa: BLE001 — push never blocks shift release
        logger.warning("shift release admin push failed", exc_info=True)


def _after_self_release(target: ReleaseTarget, reason: str, hours_until: float) -> None:
    spots_summary = ""
    if target.event is not None:
        gigs = list_gigs(target.tenant, event_ids=[target.event.id])
        if gigs:
            g = gigs[0]
            spots_summary = f"{g.spots_left} of {g.open_shifts} open"

    try:
        JobBoardShiftReleaseInternalMailer(
            target=target,
            reason=reason,
            urgent=False,
            hours_until=hours_until,
            spots_summary=spots_summary,
        ).send_now()
    except Exception:  # noqa: BLE001
        logger.exception("self-release internal email failed target=%s", target.cache_id)

    if target.confirmation is not None and target.ba_email:
        try:
            JobBoardSelfReleaseBAMailer(target.confirmation, target.brand_label).send_now()
        except Exception:  # noqa: BLE001
            logger.exception("self-release BA email failed target=%s", target.cache_id)

    _notify_admin_push(
        target.tenant,
        title="Shift released — spot re-opened",
        body=f"{target.ba_name} released {target.venue} ({target.date_label}).",
        kind="shift_dropped",
    )

    if target.event is not None:
        try:
            call_command("send_open_shift_alerts")
        except Exception:  # noqa: BLE001
            logger.exception("send_open_shift_alerts after self-release failed event=%s", target.event.id)


def _dispatch_urgent_escalation(target: ReleaseTarget, reason: str, hours_until: float) -> None:
    dedupe_key = f"shift-urgent-escalation:{target.cache_id}:{hash(reason)}"
    if not cache.add(dedupe_key, True, timeout=600):
        return

    try:
        JobBoardShiftReleaseInternalMailer(
            target=target,
            reason=reason,
            urgent=True,
            hours_until=hours_until,
        ).send_now()
    except Exception:  # noqa: BLE001
        logger.exception("urgent <36h escalation email failed target=%s", target.cache_id)

    _notify_admin_push(
        target.tenant,
        title=f"URGENT <36h callout · {target.brand_label}",
        body=f"{target.ba_name} requested to drop {target.venue} ({target.date_label}): {reason[:90]}",
        kind="urgent_shift_callout",
    )


def execute_shift_release(
    release_token: str,
    *,
    reason: str,
    now: datetime | None = None,
) -> dict:
    target = resolve_release_target(release_token)
    if target is None:
        raise ReleaseError(
            "invalid_token",
            "This shift link is invalid or has expired.",
            status=404,
        )

    clean_reason = " ".join(str(reason or "").split())[:500]
    if len(clean_reason) < 3:
        raise ReleaseError(
            "reason_required",
            "Please share a quick note on why you can't make this shift.",
            status=400,
        )

    now = now or timezone.now()

    with transaction.atomic():
        if target.event is not None:
            Event.objects.select_for_update().filter(pk=target.event.pk).first()
        if target.booking is not None:
            target.booking = JobBoardBooking.objects.select_for_update(of=("self",)).select_related(
                "tenant", "event", "event__request", "ambassador", "ambassador__user", "confirmation"
            ).get(pk=target.booking.pk)
        if target.confirmation is not None:
            target.confirmation = EventConfirmation.objects.select_for_update(of=("self",)).select_related(
                "tenant", "event", "event__request", "ambassador_event", "ambassador_event__ambassador"
            ).get(pk=target.confirmation.pk)

        inspection = inspect_shift_release(target, now=now)
        status = inspection["status"]

        if status == "already_released":
            return {
                **inspection,
                "status": "already_released",
                "spotReopened": False,
                "message": "This shift was already released.",
            }

        if _is_already_checked_in(target):
            raise ReleaseError(
                "already_checked_in",
                f"You've already clocked in on this shift. Text or call {SUPPORT_PHONE} directly.",
                status=409,
            )

        if status == "already_started":
            raise ReleaseError(
                "already_started",
                f"This shift has already started. Text or call staffing right away at {SUPPORT_PHONE}.",
                status=409,
            )

        if status == "urgent_window":
            # <= 36h: strictly block silent drop and escalate to staffing ops!
            transaction.on_commit(
                partial(
                    _dispatch_urgent_escalation,
                    target,
                    clean_reason,
                    inspection["hoursUntilStart"],
                )
            )
            return {
                **inspection,
                "status": "escalated",
                "canSelfRelease": False,
                "spotReopened": False,
                "message": (
                    "Shifts starting in 36 hours or less cannot be dropped automatically. "
                    "We just sent an urgent alert to Ignite staffing with your note — "
                    f"please also text or call {SUPPORT_PHONE} right now until our team confirms coverage."
                ),
            }

        # > 36h: execute clean self-serve release & re-open the slot!
        amb_id = target.ambassador_id
        if target.booking is not None:
            if target.booking.ambassador_job_id:
                AmbassadorJob.objects.filter(pk=target.booking.ambassador_job_id).delete()
            elif amb_id and target.event is not None:
                AmbassadorJob.objects.filter(
                    ambassador_id=amb_id, job__event_id=target.event.id
                ).delete()

            if target.booking.ambassador_event_id:
                AmbassadorEvent.objects.filter(pk=target.booking.ambassador_event_id).delete()
            elif amb_id and target.event is not None:
                AmbassadorEvent.objects.filter(
                    ambassador_id=amb_id, event_id=target.event.id
                ).delete()

            target.booking.cancelled_at = now
            target.booking.save(update_fields=["cancelled_at"])
        elif target.confirmation is not None:
            if target.confirmation.ambassador_event_id:
                ae_id = target.confirmation.ambassador_event_id
                if amb_id and target.event is not None:
                    AmbassadorJob.objects.filter(
                        ambassador_id=amb_id, job__event_id=target.event.id
                    ).delete()
                AmbassadorEvent.objects.filter(pk=ae_id).delete()

        if target.confirmation is not None:
            EventConfirmation.objects.filter(pk=target.confirmation.pk).update(
                cancelled_at=now,
                send_reminders=False,
                updated_at=now,
            )
            target.confirmation.refresh_from_db()

        spot_reopened = False
        if target.event is not None:
            spot_reopened = True
            released_user = None
            if target.booking and target.booking.ambassador and target.booking.ambassador.user_id:
                released_user = target.booking.ambassador.user
            try:
                OpenShift.objects.create(
                    event=target.event,
                    released_by=released_user,
                )
            except Exception:  # noqa: BLE001
                logger.warning("OpenShift creation skipped on self-release", exc_info=True)

        transaction.on_commit(
            partial(
                _after_self_release,
                target,
                clean_reason,
                inspection["hoursUntilStart"],
            )
        )
        return {
            **inspection,
            "status": "released",
            "canSelfRelease": False,
            "spotReopened": spot_reopened,
            "message": (
                "Your shift has been released and re-opened for another ambassador. "
                "We've notified Ignite staffing and sent you a confirmation email."
            ),
        }
