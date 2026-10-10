"""Send a job board BA to the gig they booked when they open the walk-up link.

Without this, a BA clocking in on the standing link would mint a separate
walk-in event and their booked shift would sit empty on the tracker.
"""

from __future__ import annotations

from datetime import date

from django.db import transaction

from events.models import Event, FieldMarketingEvent
from jobboard.gigs import GIG_TYPE_LABELS, gig_zone
from jobboard.models import JobBoardBooking
from recaps.models import CustomRecap, Recap

# A picked program fits a gig when its name carries the plan tactic's keyword.
_TYPE_KEYWORDS = {
    "Event Activation": "activation",
    "Guerilla": "guerilla",
    "Product Seeding": "seeding",
}


def _fits(chosen_type, event: Event, plan: FieldMarketingEvent | None) -> bool:
    if chosen_type is None or event.event_type_id in (None, chosen_type.id):
        return True
    if plan is None:
        return False
    keyword = _TYPE_KEYWORDS.get(GIG_TYPE_LABELS.get(plan.activity, ""))
    return bool(keyword and keyword in (chosen_type.name or "").lower())


def _norm(value: str) -> str:
    return " ".join((value or "").lower().replace(",", " ").split())


def booked_event_for(*, ambassador, tenant, on_date: date, address: str = "", chosen_type=None):
    """The gig this BA booked on the board for ``on_date``, or None."""
    if ambassador is None or tenant is None or on_date is None:
        return None
    bookings = (
        JobBoardBooking.objects.filter(
            ambassador=ambassador, tenant=tenant, cancelled_at__isnull=True
        )
        .select_related("event", "event__event_type", "event__timezone")
        .order_by("created_at")
    )
    matches: list[Event] = []
    for booking in bookings:
        event = booking.event
        start = event.start_time or event.date
        if start is None:
            continue
        plan = (
            FieldMarketingEvent.objects.filter(request_id=event.request_id)
            .order_by("id")
            .first()
            if event.request_id
            else None
        )
        zone = gig_zone(event, plan) if plan else None
        local_day = start.astimezone(zone).date() if zone else start.date()
        if local_day != on_date or not _fits(chosen_type, event, plan):
            continue
        matches.append(event)
    if not matches:
        return None
    typed = _norm(address)
    picked = next(
        (
            e
            for e in matches
            if typed and _norm(e.address) and (typed in _norm(e.address) or _norm(e.address) in typed)
        ),
        matches[0],
    )
    if chosen_type is not None and picked.event_type_id != chosen_type.id:
        _adopt_type(picked, chosen_type)
    return picked


def _adopt_type(event: Event, chosen_type) -> None:
    """Plan gigs land on the brand's default program; the BA on site knows
    which one it is. Only before any recap exists, so a filed form never
    changes under someone."""
    with transaction.atomic():
        if (
            CustomRecap.objects.filter(event_id=event.id).exists()
            or Recap.objects.filter(event_id=event.id).exists()
        ):
            return
        Event.objects.filter(pk=event.pk).update(event_type=chosen_type)
        event.event_type = chosen_type
        event.event_type_id = chosen_type.id
