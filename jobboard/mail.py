"""Job board emails: the BA's booking, Ignite's heads-up, and a cancel notice.

The BA's booking email is the standard event confirmation (staffing@, check-in
and recap CTAs on the client host, the brand's BA materials) with a job board
subject and a few extra blocks. It is the "booked" stage of that confirmation,
so the usual 24h / 3h reminders follow from the same sweep.
"""

from __future__ import annotations

import logging

from events.envelopes import _admin_request_url
from events.event_confirmations import (
    CONFIRMATION_FROM_EMAIL,
    CONFIRMATION_REPLY_TO,
    EventConfirmationMailer,
    build_cancellation_context,
    build_cancellation_subject,
    build_context,
    send_confirmation_stage,
)
from events.field_marketing import SAMPLING_LABELS
from events.field_marketing_mail import FROM_EMAIL, SPARK_LIME, admin_plan_url
from events.models import EventConfirmation
from jobboard.config import job_board_config_for
from jobboard.gigs import STAFFING_EMAIL, Gig, date_label, list_gigs, time_label
from jobboard.models import JobBoardBooking
from utils.mailer import Envelope, Mailer

logger = logging.getLogger(__name__)

BRING_ITEMS = (
    "A fully charged phone. You clock in, take photos, and file your recap on it.",
    "A valid photo ID.",
    "Arrive 15 minutes early to set up.",
)


def _gig_for(booking: JobBoardBooking) -> Gig | None:
    gigs = list_gigs(booking.tenant, event_ids=[booking.event_id])
    return gigs[0] if gigs else None


def _brand_label(booking: JobBoardBooking) -> str:
    config = job_board_config_for(booking.tenant)
    return config.brand_label if config else (booking.tenant.name or "").strip()


def booked_subject(confirmation: EventConfirmation, brand_label: str) -> str:
    start = confirmation.local_start()
    parts = [brand_label, confirmation.event_type_label, start.strftime("%a, %b %-d")]
    return "You're booked: " + " · ".join(p for p in parts if p)


class JobBoardBookedMailer(EventConfirmationMailer):
    def envelope(self) -> Envelope:
        c = self.confirmation
        booking = JobBoardBooking.objects.filter(confirmation=c).select_related(
            "tenant", "event"
        ).first()
        brand = _brand_label(booking) if booking else (c.tenant.name or "")
        context = build_context(c, self.stage)
        gig = _gig_for(booking) if booking else None
        context.update(
            {
                "intro_html": (
                    f"You're booked for <strong>{brand} "
                    f"{(c.event_type_label or 'shift').lower()}</strong>. "
                    "Here's everything you need for the day."
                ),
                "bring_items": BRING_ITEMS,
                "contact_email": STAFFING_EMAIL,
                "brief": (gig.plan.support_scope or "").strip() if gig else "",
                "sampling_label": (
                    SAMPLING_LABELS.get(gig.plan.sampling_format or "", "") if gig else ""
                ),
            }
        )
        return Envelope(
            subject=booked_subject(c, brand),
            template="events.templates.emails.event_confirmation",
            to_emails=[c.ba_email],
            headers={"Reply-To": CONFIRMATION_REPLY_TO},
            from_email=CONFIRMATION_FROM_EMAIL,
            context=context,
        )


class JobBoardBookingInternalMailer(Mailer):
    """Heads-up to the brand's Ignite list. Reply goes to the BA."""

    def _build_logo_attachment(self):
        return None

    def __init__(self, booking: JobBoardBooking, gig: Gig | None, to_emails: list[str], brand_label: str):
        self.booking = booking
        self.gig = gig
        self.to_emails = to_emails
        self.brand_label = brand_label

    def envelope(self) -> Envelope:
        b = self.booking
        gig = self.gig
        event = b.event
        when = f"{date_label(gig)} · {time_label(gig)}" if gig else ""
        venue = gig.venue if gig else event.name
        rows = [
            ("BA", b.name),
            ("Cell", f"({b.phone[:3]}) {b.phone[3:6]}-{b.phone[6:]}"),
            ("Email", b.email),
            ("Gig", venue),
            ("Type", gig.type_label if gig else ""),
            ("When", when),
            ("Address", (event.address or "").strip()),
        ]
        if b.note:
            rows.append(("BA note", b.note))
        spots = (
            f"{gig.spots_left} of {gig.open_shifts} open" if gig else ""
        )
        return Envelope(
            subject=(
                f"Job board booking: {b.name} · {self.brand_label} · {venue}"
                + (f" · {date_label(gig)}" if gig else "")
            ),
            template="jobboard.templates.emails.job_board_booking_internal",
            from_email=FROM_EMAIL,
            to_emails=self.to_emails,
            headers={"Reply-To": b.email},
            context={
                "brand_label": self.brand_label,
                "name": b.name,
                "venue": venue,
                "rows": [(k, v) for k, v in rows if v],
                "spots": spots,
                "full": bool(gig and gig.spots_left <= 0),
                "accent": SPARK_LIME,
                "request_url": _admin_request_url(event.request),
                "board_url": admin_plan_url(),
            },
        )


class JobBoardCancelledMailer(Mailer):
    def __init__(self, confirmation: EventConfirmation, brand_label: str):
        self.confirmation = confirmation
        self.brand_label = brand_label

    def envelope(self) -> Envelope:
        c = self.confirmation
        context = build_cancellation_context(c)
        context["intro_html"] = (
            f"Your <strong>{self.brand_label} {(c.event_type_label or 'shift').lower()}</strong> "
            "booking was cancelled by the Ignite team, so you're no longer on this shift. "
            f"Questions? Email {STAFFING_EMAIL}."
        )
        return Envelope(
            subject=build_cancellation_subject(c),
            template="events.templates.emails.event_confirmation_cancelled",
            to_emails=[c.ba_email],
            headers={"Reply-To": CONFIRMATION_REPLY_TO},
            from_email=CONFIRMATION_FROM_EMAIL,
            context=context,
        )


def send_booking_emails(booking_id: int) -> None:
    booking = (
        JobBoardBooking.objects.select_related("tenant", "event", "event__request", "confirmation")
        .filter(pk=booking_id)
        .first()
    )
    if booking is None or booking.cancelled_at is not None:
        return
    if booking.confirmation is not None:
        send_confirmation_stage(
            booking.confirmation,
            EventConfirmation.STAGE_BOOKED,
            mailer_class=JobBoardBookedMailer,
        )
    config = job_board_config_for(booking.tenant)
    if config is None or not config.internal_emails:
        return
    try:
        JobBoardBookingInternalMailer(
            booking, _gig_for(booking), list(config.internal_emails), config.brand_label
        ).send_now()
    except Exception:  # noqa: BLE001 — the BA is booked either way
        logger.exception("job board internal notice failed booking=%s", booking_id)


def send_cancellation(booking_id: int) -> None:
    booking = (
        JobBoardBooking.objects.select_related("tenant", "confirmation", "confirmation__tenant")
        .filter(pk=booking_id)
        .first()
    )
    if booking is None or booking.confirmation is None:
        return
    try:
        JobBoardCancelledMailer(booking.confirmation, _brand_label(booking)).send_now()
    except Exception:  # noqa: BLE001 — cancel already took effect
        logger.exception("job board cancel email failed booking=%s", booking_id)
