"""Event confirmation emails — the "here are your event details" send.

Replicates the confirmation Ignite had been sending Liquid Death BAs by hand,
and puts it on three moments instead of one: when the BA is booked, 24 hours
before, and 3 hours before. All three are the SAME email with a different
opening line, so the BA reads one consistent set of details.

Three things the rest of the stack has taught us, encoded here:

* **The links are read, never hardcoded.** The recap URL is built from
  ``Tenant.checkin_code`` and the training URL is ``Tenant.checkin_training_url``.
  Both are live columns other work is actively changing (the check-in link now
  asks the BA which program they're on, and its photo buckets moved), so a
  literal URL in this module would be correct only until the next deploy.

* **Time is instant arithmetic, never a local date.** ``settings.TIME_ZONE`` is
  UTC, so ``timezone.localdate()`` is the UTC date and every naive "now"
  comparison rolls over a day early at 5pm Pacific. ``starts_at`` is an aware
  datetime; "24 hours before" is ``starts_at - 24h`` and is correct in every
  venue timezone without special-casing. The venue timezone is used ONLY to
  render that instant back into the wall-clock the BA reads.

* **The send is at-most-once per (confirmation, stage).** See
  :func:`send_confirmation_stage` — the ledger row is claimed before the email
  goes out, so a sweep running every 15 minutes (or two overlapping runs)
  cannot double-send.

The only Liquid-Death-specific piece in the whole feature is the SKU list the
picker offers; everything here is tenant-generic.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from urllib.parse import quote_plus

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from events.models import EventConfirmation, EventConfirmationSend
from utils.mailer import Envelope, Mailer

logger = logging.getLogger(__name__)

# This email is the staffing team's, not the product's — it's the one BAs reply
# to about a shift. Set per-envelope ONLY; `settings.DEFAULT_FROM_EMAIL` stays
# the app-wide no-reply@ so nothing else starts sending as staffing@.
CONFIRMATION_FROM_EMAIL = "Ignite Productions <staffing@igniteproductions.co>"
# Where a BA's reply lands. Same mailbox the hand-sent version used.
CONFIRMATION_REPLY_TO = "staffing@igniteproductions.co"

SUPPORT_PHONE = "775.406.0435"
SUPPORT_PHONE_HREF = "+17754060435"

DEFAULT_CHECKIN_BASE_URL = "https://admin.igniteproductions.co"

# `product_options()` prefixes every SKU with its category so 31 options stay
# scannable in a dropdown on a phone ("Iced Tea — Sweet Reaper"). The email is
# prose, where the prefix is noise and the reference send didn't have it, so
# it's stripped back to the bare SKU for display only.
PRODUCT_CATEGORY_SEPARATOR = " — "


# ---------------------------------------------------------------------------
# Formatting — matches the reference email exactly
# ---------------------------------------------------------------------------

def format_event_date(value: datetime | None) -> str:
    """``08/01/2026``. Expects an already-localized datetime."""
    return value.strftime("%m/%d/%Y") if value else ""


def format_event_time(value: datetime | None) -> str:
    """Ignite's clock style: ``1p``, ``10a``, ``5:30p`` — minutes omitted on the
    hour. Same rule as the LD master-tracker mirror (utils.sheets_mirror
    ``_fmt_time_ld``) so the email and the client's sheet read identically."""
    if not value:
        return ""
    suffix = "a" if value.hour < 12 else "p"
    hour12 = value.strftime("%-I")
    if value.minute == 0:
        return f"{hour12}{suffix}"
    return f"{hour12}:{value.strftime('%M')}{suffix}"


def format_time_range(start: datetime | None, end: datetime | None) -> str:
    """``1p - 4p``, or just ``1p`` when there's no end time."""
    left, right = format_event_time(start), format_event_time(end)
    if left and right:
        return f"{left} - {right}"
    return left or right


def display_products(products) -> list[str]:
    """Strip the picker's category prefix for prose display."""
    out: list[str] = []
    for raw in products or []:
        name = str(raw).strip()
        if not name:
            continue
        if PRODUCT_CATEGORY_SEPARATOR in name:
            name = name.split(PRODUCT_CATEGORY_SEPARATOR, 1)[1].strip()
        out.append(name)
    return out


# ---------------------------------------------------------------------------
# Tenant-derived links
# ---------------------------------------------------------------------------

def recap_url_for(tenant) -> str:
    """The tenant's standing check-in/recap link, or "" when it has no code.

    Built from ``Tenant.checkin_code`` rather than stored on the confirmation:
    the code is a single column an admin can re-mint, and a snapshot taken at
    send time would keep pointing a 24h reminder at a dead link.
    """
    code = (getattr(tenant, "checkin_code", "") or "").strip()
    if not code:
        return ""
    base = (
        getattr(settings, "PUBLIC_CHECKIN_BASE_URL", "")
        or DEFAULT_CHECKIN_BASE_URL
    ).rstrip("/")
    return f"{base}/checkin/{code}"


def training_url_for(tenant) -> str:
    """The tenant's BA reference/training site, or "" when unset."""
    return (getattr(tenant, "checkin_training_url", "") or "").strip()


# ---------------------------------------------------------------------------
# Email body
# ---------------------------------------------------------------------------

# The opening line is the ONLY thing that differs between the three sends.
# "tomorrow" / "in a few hours" reuse the wording of the reference email and of
# the existing job reminders, so a BA who gets both doesn't see two voices.
_STAGE_INTRO = {
    EventConfirmation.STAGE_BOOKED: (
        "You're booked for a <strong>{what}</strong> — here are your event "
        "details. Please review them and keep this email handy; we'll send you "
        "a reminder the day before and again a few hours out."
    ),
    EventConfirmation.STAGE_T24: (
        "Just a friendly reminder that you're scheduled for a "
        "<strong>{what}</strong> tomorrow. We're looking forward to seeing you "
        "in action!"
    ),
    EventConfirmation.STAGE_T3: (
        "Just a friendly reminder that you're scheduled for a "
        "<strong>{what}</strong> in a few hours. We're looking forward to "
        "seeing you in action!"
    ),
}


def _what_label(confirmation: EventConfirmation) -> str:
    """``Liquid Death retail sampling`` — brand kept in title case, program
    lowercased so it reads as prose mid-sentence."""
    brand = (getattr(confirmation.tenant, "name", "") or "").strip()
    program = (confirmation.event_type_label or "").strip().lower()
    return " ".join(p for p in (brand, program) if p) or "shift"


def _event_title(confirmation: EventConfirmation, date_label, time_label) -> str:
    """``Jewel Osco | 08/01/2026 | 1p - 4p`` — the reference email's one-line
    event identity. Falls back through whatever parts exist."""
    parts = [
        (confirmation.store_name or "").strip(),
        date_label,
        time_label,
    ]
    return " | ".join(p for p in parts if p)


def build_context(confirmation: EventConfirmation, stage: str) -> dict:
    """Template context for one stage of one confirmation."""
    local_start = confirmation.local_start()
    local_end = confirmation.local_end()

    date_label = format_event_date(local_start)
    time_label = format_time_range(local_start, local_end)
    what = _what_label(confirmation)

    brand = (getattr(confirmation.tenant, "name", "") or "").strip()
    program = (confirmation.event_type_label or "").strip()
    eyebrow = " • ".join(p.upper() for p in (brand, program) if p)

    address = (confirmation.address or "").strip()
    products = display_products(confirmation.products)

    intro = _STAGE_INTRO.get(stage, _STAGE_INTRO[EventConfirmation.STAGE_BOOKED])

    return {
        "eyebrow": eyebrow,
        "ba_first_name": (confirmation.ba_name or "").strip().split(" ")[0],
        "intro_html": intro.format(what=what),
        "event_title": _event_title(confirmation, date_label, time_label),
        "date_label": date_label,
        "store_name": (confirmation.store_name or "").strip(),
        "address": address,
        "time_label": time_label,
        "products_label": ", ".join(products),
        "map_url": (
            "https://www.google.com/maps/search/?api=1&query="
            + quote_plus(address)
        ) if address else "",
        "recap_url": recap_url_for(confirmation.tenant),
        "training_url": training_url_for(confirmation.tenant),
        "support_phone": SUPPORT_PHONE,
        "support_phone_href": SUPPORT_PHONE_HREF,
        "from_address": CONFIRMATION_REPLY_TO,
    }


def build_subject(confirmation: EventConfirmation) -> str:
    """``Your Liquid Death Retail Sampling – Jewel Osco | 08/01/2026 | 1p - 4p``

    Deliberately IDENTICAL across all three stages so the booked email and both
    reminders thread together in the BA's inbox instead of arriving as three
    unrelated messages about the same shift.
    """
    local_start = confirmation.local_start()
    title = _event_title(
        confirmation,
        format_event_date(local_start),
        format_time_range(local_start, confirmation.local_end()),
    )
    brand = (getattr(confirmation.tenant, "name", "") or "").strip()
    program = (confirmation.event_type_label or "").strip()
    lead = " ".join(p for p in (brand, program) if p) or "Shift"
    return f"Your {lead} – {title}" if title else f"Your {lead}"


class EventConfirmationMailer(Mailer):
    """Sends one stage of one confirmation from the staffing mailbox."""

    def __init__(self, confirmation: EventConfirmation, stage: str) -> None:
        self.confirmation = confirmation
        self.stage = stage

    def envelope(self) -> Envelope:
        return Envelope(
            subject=build_subject(self.confirmation),
            template="events.templates.emails.event_confirmation",
            to_emails=[self.confirmation.ba_email],
            headers={"Reply-To": CONFIRMATION_REPLY_TO},
            from_email=CONFIRMATION_FROM_EMAIL,
            context=build_context(self.confirmation, self.stage),
        )


# ---------------------------------------------------------------------------
# Sending — at-most-once per (confirmation, stage)
# ---------------------------------------------------------------------------

@dataclass
class SendResult:
    stage: str
    sent: bool
    reason: str = ""

    def __bool__(self) -> bool:  # truthy only when an email actually left
        return self.sent


def send_confirmation_stage(
    confirmation: EventConfirmation,
    stage: str,
    *,
    dry_run: bool = False,
) -> SendResult:
    """Send one stage, exactly once.

    The ledger row is CLAIMED (inserted, attempts incremented) inside a
    transaction before the email is handed to the driver. Two sweeps racing the
    same row therefore can't both send: the unique constraint on
    (confirmation, stage) means one INSERT wins and the loser re-reads a row
    whose attempt is already taken. This ordering trades a vanishingly rare
    lost email (process dies between claim and send) for never double-emailing
    a BA, which is the right way round for a job that runs every 15 minutes.

    A send that fails leaves ``sent_at`` NULL with the error recorded, so the
    next sweep retries it until ``MAX_ATTEMPTS`` rather than dropping it.
    """
    if dry_run:
        return SendResult(stage=stage, sent=False, reason="dry-run")

    if not (confirmation.ba_email or "").strip():
        return SendResult(stage=stage, sent=False, reason="no-email")

    with transaction.atomic():
        row, created = (
            EventConfirmationSend.objects.select_for_update().get_or_create(
                confirmation=confirmation,
                stage=stage,
                defaults={"to_email": confirmation.ba_email},
            )
        )
        if row.sent_at:
            return SendResult(stage=stage, sent=False, reason="already-sent")
        if not created and row.attempts >= EventConfirmationSend.MAX_ATTEMPTS:
            return SendResult(stage=stage, sent=False, reason="attempts-exhausted")
        row.attempts += 1
        row.to_email = confirmation.ba_email
        row.save(update_fields=["attempts", "to_email", "updated_at"])

    try:
        # send_now, not send(): there is no Redis/RQ on Cloud Run, so the queue
        # path would fall back to inline anyway — this just says so plainly.
        EventConfirmationMailer(confirmation, stage).send_now()
    except Exception as exc:  # noqa: BLE001 — recorded, retried next sweep
        logger.exception(
            "event confirmation send failed confirmation=%s stage=%s",
            confirmation.id, stage,
        )
        EventConfirmationSend.objects.filter(pk=row.pk).update(
            last_error=str(exc)[:2000], updated_at=timezone.now()
        )
        return SendResult(stage=stage, sent=False, reason=f"error: {exc}")

    EventConfirmationSend.objects.filter(pk=row.pk).update(
        sent_at=timezone.now(), last_error="", updated_at=timezone.now()
    )
    logger.info(
        "event confirmation sent confirmation=%s stage=%s to=%s",
        confirmation.id, stage, confirmation.ba_email,
    )
    return SendResult(stage=stage, sent=True)


# ---------------------------------------------------------------------------
# The sweep's queryset
# ---------------------------------------------------------------------------

def due_reminders(now: datetime | None = None, *, grace_hours: int = 6):
    """(confirmation, stage) pairs whose reminder is due but unsent.

    A stage is due once ``now`` has passed ``starts_at - lead``. The window is
    right-bounded by ``grace_hours`` so a sweep that was down for a while
    doesn't fire a 24h reminder five minutes before the shift — past that the
    reminder is stale and the later stage (or nothing) is the honest outcome.

    A stage whose moment had ALREADY PASSED when the confirmation was created
    never fires. Book a BA three hours before their shift and the booked email
    is the only sensible message: without this guard the sweep would notice
    that ``starts_at - 3h`` is in the past-but-recent window and fire the
    "in a few hours" reminder minutes behind it, and a same-week booking would
    get the "tomorrow" one too.

    Excluded: shifts already started, cancelled rows, rows whose sender opted
    out of reminders, and any (confirmation, stage) already in the ledger.
    """
    now = now or timezone.now()

    confirmations = (
        EventConfirmation.objects.select_related("tenant", "timezone")
        .filter(
            send_reminders=True,
            cancelled_at__isnull=True,
            # Never remind about a shift that has already begun.
            starts_at__gt=now,
        )
    )

    # One query for every stage already spoken for, so the per-row check below
    # is a set lookup rather than N queries.
    claimed = set(
        EventConfirmationSend.objects.filter(
            confirmation__in=confirmations
        ).values_list("confirmation_id", "stage")
    )

    due: list[tuple[EventConfirmation, str]] = []
    for confirmation in confirmations:
        for stage in EventConfirmation.REMINDER_STAGES:
            if (confirmation.id, stage) in claimed:
                continue
            lead = timedelta(
                hours=EventConfirmation.STAGE_LEAD_HOURS[stage]
            )
            fire_at = confirmation.starts_at - lead
            # The booking has to predate the reminder moment for it to mean
            # anything — see the docstring on late bookings.
            if confirmation.created_at and fire_at < confirmation.created_at:
                continue
            if fire_at <= now <= fire_at + timedelta(hours=grace_hours):
                due.append((confirmation, stage))
    return due
