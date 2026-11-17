"""Which calendar day an event is on, at the venue.

``settings.TIME_ZONE`` is UTC, so ``date__date`` is the UTC day: a 6 PM
Pacific shift on Nov 16 is stored Nov 17 02:00 UTC and lands on Nov 17. Day
views (the live board, Today, Upcoming) should group by the day the shift is
on where it happens.

``start_time`` is the authority (the request/event write paths localize it to
the venue), with ``date`` as the fallback for an untimed event, the same rule
as ``ambassadors.queries._event_local_dates``. An event with no resolvable
zone is read in the ops zone (Pacific, ``events.live_board.OPS_TZ``).
"""
from __future__ import annotations

from datetime import date as _date
from datetime import datetime as _datetime
from datetime import timedelta
from datetime import timezone as _dt_timezone
from zoneinfo import ZoneInfo

from django.db.models import Q

OPS_TZ = "America/Los_Angeles"


def venue_zone(event) -> ZoneInfo:
    from utils.tz import resolve_zoneinfo

    tz_row = getattr(event, "timezone", None) if getattr(event, "timezone_id", None) else None
    return resolve_zoneinfo(tz_row) or ZoneInfo(OPS_TZ)


def venue_local_date(event) -> _date | None:
    source = getattr(event, "start_time", None) or getattr(event, "date", None)
    if source is None:
        return None
    if not isinstance(source, _datetime):
        return source
    if source.tzinfo is None:
        source = source.replace(tzinfo=_dt_timezone.utc)
    return source.astimezone(venue_zone(event)).date()


def event_ids_on_venue_day(queryset, day: _date) -> set[int]:
    """Ids of events in ``queryset`` whose venue-local day is ``day``.

    US venues sit behind UTC, so a venue day's events have a UTC date of that
    day or the next. One day either side is read and then decided per event
    in its own zone; only the columns needed for that are loaded.
    """
    lo, hi = day - timedelta(days=1), day + timedelta(days=1)
    candidates = (
        queryset.filter(
            Q(start_time__date__range=(lo, hi))
            | Q(start_time__isnull=True, date__date__range=(lo, hi))
        )
        .select_related("timezone")
        .only("id", "start_time", "date", "timezone")
    )
    return {ev.id for ev in candidates if venue_local_date(ev) == day}
