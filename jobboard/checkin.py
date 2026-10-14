"""Send a BA on the standing walk-up link to the plan gig they're working.

Without this, a BA clocking in on the standing link would mint a separate
walk-in event and the plan's booked event would sit empty — so the recap
never reaches the plan's Actuals (approval only auto-attaches recaps filed
on the plan's own event). Job board bookings land automatically; everyone
else lands by an explicit "Is this one of today's scheduled events?" pick
or, when unambiguous, by the place they typed / GPS-picked.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from datetime import timezone as dt_timezone

from django.db import transaction
from django.db.models import Q

from ambassadors.checkin_web import addresses_fuzzy_match, normalize_place
from events.field_marketing import ACTIVITY_FILTERS
from events.market import market_for, parse_address_geo
from events.models import Event, FieldMarketingEvent
from events.plan_event_types import event_type_fits_plan
from jobboard.gigs import DEAD_STATUS_SLUGS, Gig, _live_plans, gig_zone
from jobboard.models import JobBoardBooking
from recaps.models import CustomRecap, Recap

_FM = FieldMarketingEvent
CITY_MATCH_ACTIVITIES = (
    ACTIVITY_FILTERS[_FM.ACTIVITY_EVENT_ACTIVATION] | ACTIVITY_FILTERS[_FM.ACTIVITY_GUERILLA]
)


def _fits(chosen_type, event: Event, plan: FieldMarketingEvent | None) -> bool:
    if chosen_type is None or event.event_type_id in (None, chosen_type.id):
        return True
    return plan is not None and event_type_fits_plan(chosen_type, plan)


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


def plan_gigs_on(*, tenant, on_date: date, chosen_type=None) -> list[Gig]:
    """Live plan gigs (booked events) on ``on_date`` in the gig's own market
    timezone that fit the program the BA picked, earliest first."""
    if tenant is None or on_date is None:
        return []
    plans = _live_plans(tenant)
    if not plans:
        return []
    lo = datetime.combine(on_date - timedelta(days=1), time.min, tzinfo=dt_timezone.utc)
    hi = lo + timedelta(days=3)
    events = (
        Event.objects.filter(tenant=tenant, request_id__in=list(plans))
        .filter(
            Q(start_time__gte=lo, start_time__lt=hi)
            | Q(start_time__isnull=True, date__gte=lo, date__lt=hi)
        )
        .exclude(status__slug__in=DEAD_STATUS_SLUGS)
        .select_related("event_type", "timezone", "state", "location")
    )
    gigs: list[Gig] = []
    for event in events:
        plan = plans[event.request_id]
        start = event.start_time or event.date
        if start.astimezone(gig_zone(event, plan)).date() != on_date:
            continue
        if not _fits(chosen_type, event, plan):
            continue
        gigs.append(Gig(event=event, plan=plan, override=None, booked=0))
    gigs.sort(key=lambda g: (g.starts_at, g.event.id))
    return gigs


def _same_place(typed: str, other: str) -> bool:
    if not typed or not other:
        return False
    return normalize_place(typed) == normalize_place(other) or addresses_fuzzy_match(typed, other)


def _city(obj) -> tuple[str, str] | None:
    market = market_for(obj)
    if not market.city or not market.state_code:
        return None
    return market.city.strip().lower(), market.state_code


def plan_gig_for_place(gigs: list[Gig], address: str) -> Gig | None:
    """The one gig the typed / GPS place clearly names, else None.

    Same street address wins. Otherwise an Event / Guerilla gig that is the
    only one that day in the same city counts. Anything ambiguous (two gigs
    in one city, a store-level retail plan, no parseable city) is left to
    the BA's explicit pick.
    """
    if not gigs or not (address or "").strip():
        return None
    for gig in gigs:
        if _same_place(address, gig.event.address) or _same_place(address, gig.plan.address):
            return gig
    geo = parse_address_geo(address)
    if not geo.city or not geo.state_code:
        return None
    typed_city = (geo.city.strip().lower(), geo.state_code)
    same_city = [
        gig
        for gig in gigs
        if gig.plan.activity in CITY_MATCH_ACTIVITIES
        and typed_city in {_city(gig.event), _city(gig.plan)}
    ]
    return same_city[0] if len(same_city) == 1 else None


def join_plan_gig(gig: Gig, chosen_type) -> Event:
    if chosen_type is not None and gig.event.event_type_id != chosen_type.id:
        _adopt_type(gig.event, chosen_type)
    return gig.event


def public_gig(gig: Gig) -> dict:
    """What the walk-up pick-list shows — the job board's public fields,
    never notes or the street address."""
    ends = gig.ends_at
    return {
        "id": str(gig.event.id),
        "name": gig.venue,
        "area": gig.area,
        "typeLabel": gig.type_label,
        "startTime": gig.starts_at.isoformat() if gig.event.start_time else None,
        "endTime": ends.isoformat() if ends else None,
        "timeZone": str(gig.zone),
    }
