"""Booking BAs onto job board gigs, and the admin side of it.

Booking runs per gig under a row lock on the Event, so two BAs racing for
the last shift can't both get it. Each booking writes the normal staffing
records (approved AmbassadorEvent, AmbassadorJob, EventConfirmation) so the
shift shows up on the tracker, reminders fire, and walk-up clock-in finds it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from functools import partial

from django.core.exceptions import ValidationError
from django.core.validators import validate_email
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from ambassadors.checkin_web import get_or_create_checkin_ambassador
from ambassadors.models import AmbassadorEvent, Attendance
from events.models import Event, EventConfirmation
from jobboard.config import JobBoardConfig
from jobboard.gigs import Gig, date_label, list_gigs
from jobboard.mail import send_booking_emails, send_cancellation
from jobboard.models import JobBoard, JobBoardBooking, JobBoardGig
from jobs.models import AmbassadorJob, Job, JobTitle, Rate, RateType, Status

MAX_GIGS_PER_APPLY = 10
# Shifts without an end time block this long when checking for overlaps.
ASSUMED_SHIFT = timedelta(hours=4)


class ApplyError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class Applicant:
    first_name: str
    last_name: str
    phone: str
    email: str
    note: str

    @property
    def full_name(self) -> str:
        return " ".join(p for p in (self.first_name, self.last_name) if p)

    @property
    def phone_label(self) -> str:
        p = self.phone
        return f"({p[:3]}) {p[3:6]}-{p[6:]}"


def us_phone_digits(raw: str) -> str | None:
    digits = re.sub(r"\D", "", raw or "")
    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]
    if len(digits) != 10 or digits[0] in "01" or digits[3] in "01":
        return None
    return digits


def clean_applicant(data: dict) -> Applicant:
    name = " ".join(str(data.get("name") or "").split())[:120]
    if len(name) < 2:
        raise ApplyError("name_required", "Enter your full name.")
    phone = us_phone_digits(str(data.get("phone") or ""))
    if phone is None:
        raise ApplyError("phone_invalid", "Enter a 10-digit US cell number.")
    email = str(data.get("email") or "").strip().lower()
    try:
        validate_email(email)
    except ValidationError:
        raise ApplyError(
            "email_invalid",
            "Enter a valid email. Your booking details are sent there.",
        ) from None
    note = str(data.get("note") or "").strip()
    if len(note) > 500:
        raise ApplyError("note_too_long", "Keep the note under 500 characters.")
    first, _, last = name.partition(" ")
    return Applicant(first_name=first, last_name=last, phone=phone, email=email, note=note)


def _window(start: datetime, end: datetime | None) -> tuple[datetime, datetime]:
    return start, (end if end and end > start else start + ASSUMED_SHIFT)


def _conflicting_booking(applicant: Applicant, gig: Gig) -> JobBoardBooking | None:
    start, end = _window(gig.starts_at, gig.ends_at)
    others = (
        JobBoardBooking.objects.filter(cancelled_at__isnull=True)
        .filter(Q(phone=applicant.phone) | Q(email__iexact=applicant.email))
        .exclude(event_id=gig.event.id)
        .select_related("event")
    )
    for other in others:
        o_start = other.event.start_time or other.event.date
        if o_start is None:
            continue
        o_start, o_end = _window(o_start, other.event.end_time)
        if o_start < end and start < o_end:
            return other
    return None


def _job_refs(tenant, actor) -> tuple[Status, Rate, JobTitle]:
    status = (
        Status.objects.filter(tenant=tenant, slug__in=("accepted", "approved"))
        .order_by("slug")
        .first()
        or Status.objects.filter(tenant=tenant, name__iexact="accepted").first()
        or Status.objects.create(
            tenant=tenant,
            name="Accepted",
            slug="accepted",
            created_by=actor,
            updated_by=actor,
        )
    )
    rate = Rate.objects.filter(tenant=tenant).order_by("id").first()
    if rate is None:
        rate_type = RateType.objects.filter(
            tenant=tenant, name__iexact="hourly"
        ).first() or RateType.objects.create(
            name="Hourly", tenant=tenant, created_by=actor, updated_by=actor
        )
        rate = Rate.objects.create(
            amount=0, tenant=tenant, rate_type=rate_type, created_by=actor, updated_by=actor
        )
    title = (
        JobTitle.objects.filter(tenant=tenant).order_by("id").first()
        or JobTitle.objects.create(
            name="Brand Ambassador", tenant=tenant, created_by=actor, updated_by=actor
        )
    )
    return status, rate, title


def _job_for(event: Event, tenant, actor) -> tuple[Job, Status, Rate]:
    status, rate, title = _job_refs(tenant, actor)
    job = Job.objects.filter(event=event, deleted_at__isnull=True).order_by("id").first()
    if job is None:
        job = Job.objects.create(
            event=event,
            tenant=tenant,
            name=event.name,
            address=(event.address or "")[:255],
            start_date=event.start_time or event.date,
            end_date=event.end_time,
            job_title=title,
            rate=rate,
            created_by=actor,
            updated_by=actor,
        )
    return job, status, job.rate or rate


def _book_one(
    tenant, config: JobBoardConfig, event_uuid: str, applicant: Applicant, ambassador, *, now
) -> dict:
    result = {"id": event_uuid}
    with transaction.atomic():
        locked = (
            Event.objects.select_for_update()
            .filter(tenant=tenant, uuid=event_uuid)
            .values_list("id", flat=True)
            .first()
        )
        gigs = list_gigs(tenant, now=now, event_ids=[locked]) if locked else []
        if not gigs or not gigs[0].on_board:
            return {**result, "status": "unavailable", "message": "This gig is no longer open."}
        gig = gigs[0]
        result["label"] = f"{gig.venue} · {date_label(gig)}"

        live = JobBoardBooking.objects.filter(event=gig.event, cancelled_at__isnull=True)
        if (
            live.filter(Q(phone=applicant.phone) | Q(email__iexact=applicant.email)).exists()
            or AmbassadorEvent.objects.filter(
                event=gig.event, ambassador=ambassador, is_approved=True
            ).exists()
        ):
            return {**result, "status": "already", "message": "You're already booked on this gig."}
        if gig.spots_left <= 0:
            return {**result, "status": "full", "message": "All shifts were just taken."}
        clash = _conflicting_booking(applicant, gig)
        if clash is not None:
            return {
                **result,
                "status": "conflict",
                "message": f"Overlaps your booking at {clash.event.name}.",
            }

        actor = ambassador.user
        ae, _ = AmbassadorEvent.objects.get_or_create(
            ambassador=ambassador,
            event=gig.event,
            defaults={
                "tenant": tenant,
                "is_approved": True,
                "source": AmbassadorEvent.SOURCE_CLAIM,
                "created_by": actor,
                "updated_by": actor,
            },
        )
        if not ae.is_approved:
            ae.is_approved = True
            ae.updated_by = actor
            ae.save(update_fields=["is_approved", "updated_by", "updated_at"])

        job, status, rate = _job_for(gig.event, tenant, gig.event.created_by)
        aj = (
            AmbassadorJob.objects.filter(ambassador=ambassador, job=job).first()
            or AmbassadorJob.objects.create(
                tenant=tenant,
                ambassador=ambassador,
                job=job,
                status=status,
                rate=rate,
                created_by=actor,
                updated_by=actor,
            )
        )
        confirmation = EventConfirmation.objects.create(
            tenant=tenant,
            event=gig.event,
            ambassador_event=ae,
            ba_name=applicant.full_name,
            ba_email=applicant.email,
            store_name=gig.venue,
            address=(gig.event.address or "").strip(),
            event_type_label=gig.type_label,
            starts_at=gig.starts_at,
            ends_at=gig.ends_at,
            timezone_name=gig.zone.key,
            timezone=gig.event.timezone,
            products=gig.skus,
            send_reminders=True,
        )
        booking = JobBoardBooking.objects.create(
            tenant=tenant,
            event=gig.event,
            ambassador=ambassador,
            ambassador_event=ae,
            ambassador_job=aj,
            confirmation=confirmation,
            name=applicant.full_name,
            phone=applicant.phone,
            email=applicant.email,
            note=applicant.note,
        )
        transaction.on_commit(partial(send_booking_emails, booking.id))
        return {**result, "status": "booked", "message": "You're booked."}


def apply_to_gigs(board: JobBoard, config: JobBoardConfig, applicant: Applicant, gig_ids, *, now=None) -> list[dict]:
    ids = []
    for raw in gig_ids or []:
        value = str(raw or "").strip()
        if value and value not in ids:
            ids.append(value)
    if not ids:
        raise ApplyError("no_gigs", "Pick at least one gig.")
    if len(ids) > MAX_GIGS_PER_APPLY:
        raise ApplyError("too_many_gigs", f"Pick up to {MAX_GIGS_PER_APPLY} gigs at a time.")
    now = now or timezone.now()
    ambassador, _ = get_or_create_checkin_ambassador(
        first_name=applicant.first_name,
        last_name=applicant.last_name,
        phone=applicant.phone,
        email=applicant.email,
    )
    results = []
    for event_uuid in ids:
        try:
            results.append(
                _book_one(board.tenant, config, event_uuid, applicant, ambassador, now=now)
            )
        except (ValueError, ValidationError):
            results.append({"id": event_uuid, "status": "unavailable", "message": "This gig is no longer open."})
    return results


# ── Admin ─────────────────────────────────────────────────────────────


class CancelError(Exception):
    pass


def cancel_booking(booking: JobBoardBooking, actor) -> JobBoardBooking:
    """Release the shift and email the BA. Only for an explicit admin cancel."""
    with transaction.atomic():
        booking = JobBoardBooking.objects.select_for_update().get(pk=booking.pk)
        if booking.cancelled_at is not None:
            return booking
        if booking.ambassador_id and Attendance.objects.filter(
            event_id=booking.event_id, ambassador_id=booking.ambassador_id
        ).exists():
            raise CancelError("This BA already clocked in on this gig.")
        if booking.ambassador_job_id:
            AmbassadorJob.objects.filter(pk=booking.ambassador_job_id).delete()
        if booking.ambassador_event_id:
            AmbassadorEvent.objects.filter(pk=booking.ambassador_event_id).delete()
        now = timezone.now()
        if booking.confirmation_id:
            EventConfirmation.objects.filter(pk=booking.confirmation_id).update(
                cancelled_at=now, send_reminders=False
            )
        booking.cancelled_at = now
        booking.cancelled_by = actor
        booking.save(update_fields=["cancelled_at", "cancelled_by"])
        transaction.on_commit(partial(send_cancellation, booking.id))
    return booking


def set_gig(gig: Gig, *, open_shifts: int | None, listed: bool | None, actor) -> JobBoardGig:
    override = gig.override or JobBoardGig(
        tenant_id=gig.event.tenant_id,
        event=gig.event,
        open_shifts=gig.open_shifts,
        listed=gig.listed,
    )
    if open_shifts is not None:
        override.open_shifts = max(0, min(int(open_shifts), 50))
    if listed is not None:
        override.listed = bool(listed)
    override.updated_by = actor
    override.save()
    gig.override = override
    return override
