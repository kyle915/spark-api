"""Gather the stored facts behind each paid shift in a pay period.

One ``ShiftFacts`` per approved (event, ambassador) booking — the same set
of shifts the payroll timesheet pays — built from what Spark already
records: Attendance punches (time, GPS fix, who created/edited them), the
event's booked window and venue position, approved shift extensions,
LocationPing GPS pings, the BA's recap, and mileage (GPS sessions and
payable claims). Nothing here judges anything; ``hours_check.rules`` does.

Reads only. A handful of bulk queries per period, never one per shift.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date as _date
from datetime import datetime, timedelta
from datetime import timezone as _dt_tz


@dataclass
class Punch:
    kind: str  # "clock_in" | "clock_out"
    at: datetime
    coords: tuple[float, float] | None
    created_by_id: int | None
    updated_by_id: int | None
    created_at: datetime | None
    updated_at: datetime | None
    # Written by the hourly auto clock-out job at the scheduled end.
    auto_closed: bool = False


@dataclass
class OtherShift:
    """A clocked interval of the same BA on another event (any client)."""

    event_id: int
    event_uuid: str
    event_name: str
    tenant_id: int | None
    first_in: datetime
    last_out: datetime
    venue: tuple[float, float] | None


@dataclass
class ShiftFacts:
    event_id: int
    event_uuid: str
    event_name: str
    tenant_id: int
    ambassador_id: int
    ambassador_uuid: str
    ambassador_name: str
    ba_user_id: int | None
    booking_source: str = "admin"
    time_zone: str = "America/Los_Angeles"
    local_date: _date | None = None

    booked_start: datetime | None = None
    booked_end: datetime | None = None
    scheduled_hours: float | None = None
    extension_minutes: int = 0

    punches: list[Punch] = field(default_factory=list)
    first_in: datetime | None = None
    last_out: datetime | None = None
    on_the_clock: bool = False
    worked_hours: float | None = None
    # True when worked_hours fell back to the scheduled length.
    estimated: bool = True

    venue: tuple[float, float] | None = None
    venue_source: str = ""  # "event" | "address lookup" | ""
    has_pings: bool = False

    has_recap: bool = False
    recap_submitted_at: datetime | None = None
    # (miles from venue, minutes between the ping and the submit)
    recap_ping_miles: float | None = None
    recap_ping_minutes: float | None = None

    gps_miles: float | None = None
    claim_miles: float | None = None

    shift_over: bool = True
    # Names (or emails) of people other than the BA who created/edited a punch.
    editors: dict[int, str] = field(default_factory=dict)
    others: list[OtherShift] = field(default_factory=list)

    @property
    def key(self) -> tuple[int, int]:
        return (self.event_id, self.ambassador_id)


def valid_coords(raw) -> tuple[float, float] | None:
    if not raw or len(raw) < 2:
        return None
    try:
        lat, lng = float(raw[0]), float(raw[1])
    except (TypeError, ValueError):
        return None
    if lat == 0.0 and lng == 0.0:
        return None
    if not (-90.0 <= lat <= 90.0) or not (-180.0 <= lng <= 180.0):
        return None
    return (lat, lng)


def haversine_miles(a: tuple[float, float], b: tuple[float, float]) -> float:
    import math

    lat1, lng1 = math.radians(a[0]), math.radians(a[1])
    lat2, lng2 = math.radians(b[0]), math.radians(b[1])
    dlat, dlng = lat2 - lat1, lng2 - lng1
    h = (
        math.sin(dlat / 2) ** 2
        + math.cos(lat1) * math.cos(lat2) * math.sin(dlng / 2) ** 2
    )
    return 3958.8 * 2 * math.asin(math.sqrt(h))


def _aware(dt):
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=_dt_tz.utc)
    return dt


def _booked_window(ev):
    start = _aware(getattr(ev, "start_time", None))
    end = _aware(getattr(ev, "end_time", None) or getattr(ev, "new_end_time", None))
    if start is None or end is None:
        return None, None
    # The stored end can carry the start's date for an overnight shift.
    while end <= start:
        end = end + timedelta(days=1)
    if end - start > timedelta(hours=24):
        # A multi-day span is a date problem on the event, not a window.
        return None, None
    return start, end


def _user_label(u) -> str:
    if u is None:
        return ""
    name = f"{u.first_name or ''} {u.last_name or ''}".strip()
    return name or (u.email or f"user {u.id}")


def _venue_positions(events) -> dict[int, tuple[tuple[float, float], str]]:
    from events.models import EventMapPoint

    out: dict[int, tuple[tuple[float, float], str]] = {}
    missing = []
    for ev in events:
        c = valid_coords(getattr(ev, "coordinates", None))
        if c:
            out[ev.id] = (c, "event")
        else:
            missing.append(ev.id)
    if missing:
        for ev_id, lat, lng in EventMapPoint.objects.filter(
            event_id__in=missing, status=EventMapPoint.STATUS_OK
        ).values_list("event_id", "lat", "lng"):
            c = valid_coords([lat, lng])
            if c:
                out[ev_id] = (c, "address lookup")
    return out


def _punch_rows(event_ids, amb_ids):
    from ambassadors.models import Attendance

    return (
        Attendance.objects.filter(
            event_id__in=list(event_ids),
            ambassador_id__in=list(amb_ids),
            source__name__in=["clock_in", "clock_out"],
        )
        .order_by("clock_time", "id")
        .values_list(
            "event_id",
            "ambassador_id",
            "source__name",
            "clock_time",
            "coordinates",
            "created_by_id",
            "updated_by_id",
            "created_at",
            "updated_at",
        )
    )


def _pair_bounds(punches: list[Punch]):
    ins = [p.at for p in punches if p.kind == "clock_in"]
    outs = [p.at for p in punches if p.kind == "clock_out"]
    first_in = min(ins) if ins else None
    last_out = max(outs) if outs else None
    latest = punches[-1].kind if punches else None
    return first_in, last_out, latest == "clock_in"


def gather(tenant_id: int, start: _date, end: _date, *, now=None) -> list[ShiftFacts]:
    """Every approved booking for ``tenant_id`` on events dated in
    [start, end] (inclusive, by event date — the payroll timesheet's scope)."""
    from django.contrib.auth import get_user_model
    from django.db.models import Sum
    from django.utils import timezone as dj_tz

    from ambassadors.attendance_hours import scheduled_hours, worked_hours
    from ambassadors.models import (
        AmbassadorEvent,
        LocationPing,
        MileageSession,
        PayableMileageClaim,
        ShiftExtensionRequest,
    )
    from events.venue_day import venue_local_date, venue_zone
    from recaps.models import CustomRecap, Recap

    now = now or dj_tz.now()
    bookings = list(
        AmbassadorEvent.objects.filter(
            event__tenant_id=tenant_id,
            event__date__date__gte=start,
            event__date__date__lte=end,
            is_approved=True,
        )
        .select_related("event", "event__timezone", "ambassador", "ambassador__user")
        .order_by("event__date", "id")[:5000]
    )
    if not bookings:
        return []

    shifts: dict[tuple[int, int], ShiftFacts] = {}
    events = {}
    for b in bookings:
        ev, amb = b.event, b.ambassador
        if ev is None or amb is None or (ev.id, amb.id) in shifts:
            continue
        events[ev.id] = ev
        u = getattr(amb, "user", None)
        b_start, b_end = _booked_window(ev)
        zone = venue_zone(ev)
        sf = ShiftFacts(
            event_id=ev.id,
            event_uuid=str(ev.uuid),
            event_name=ev.name or "",
            tenant_id=ev.tenant_id,
            ambassador_id=amb.id,
            ambassador_uuid=str(amb.uuid),
            ambassador_name=_user_label(u) or "(unnamed)",
            ba_user_id=getattr(u, "id", None),
            booking_source=b.source or "admin",
            time_zone=zone.key,
            local_date=venue_local_date(ev),
            booked_start=b_start,
            booked_end=b_end,
            scheduled_hours=scheduled_hours(
                getattr(ev, "start_time", None), getattr(ev, "end_time", None)
            ),
        )
        shifts[sf.key] = sf

    event_ids = set(events)
    amb_ids = {k[1] for k in shifts}

    # --- punches -----------------------------------------------------------
    editor_ids: set[int] = set()
    for (ev_id, amb_id, kind, at, coords, cby, uby, cat, uat) in _punch_rows(
        event_ids, amb_ids
    ):
        sf = shifts.get((ev_id, amb_id))
        if sf is None or at is None:
            continue
        ev = events[ev_id]
        end_time = _aware(getattr(ev, "end_time", None))
        auto = bool(
            kind == "clock_out"
            and coords is None
            and cby is None
            and end_time is not None
            and _aware(at) == end_time
        )
        sf.punches.append(
            Punch(
                kind=kind,
                at=_aware(at),
                coords=valid_coords(coords),
                created_by_id=cby,
                updated_by_id=uby,
                created_at=cat,
                updated_at=uat,
                auto_closed=auto,
            )
        )
        for uid in (cby, uby):
            if uid and uid != sf.ba_user_id:
                editor_ids.add(uid)

    users = {
        u.id: _user_label(u)
        for u in get_user_model().objects.filter(id__in=editor_ids)
    } if editor_ids else {}

    for sf in shifts.values():
        sf.first_in, sf.last_out, sf.on_the_clock = _pair_bounds(sf.punches)
        facts = None
        if sf.first_in or sf.last_out:
            facts = {
                "first_in": sf.first_in,
                "last_out": sf.last_out,
                "latest_kind": "clock_in" if sf.on_the_clock else "clock_out",
            }
        sf.worked_hours, sf.estimated = worked_hours(facts, sf.scheduled_hours)
        if sf.worked_hours is None:
            sf.estimated = True
        for p in sf.punches:
            for uid in (p.created_by_id, p.updated_by_id):
                if uid and uid in users:
                    sf.editors[uid] = users[uid]
        end_ref = sf.last_out or sf.booked_end
        sf.shift_over = bool(end_ref and end_ref < now) or (
            sf.local_date is not None and sf.local_date < now.date()
        )

    # --- venue -------------------------------------------------------------
    positions = _venue_positions(events.values())
    for sf in shifts.values():
        pos = positions.get(sf.event_id)
        if pos:
            sf.venue, sf.venue_source = pos

    # --- approved extensions -------------------------------------------------
    for ev_id, amb_id, minutes in ShiftExtensionRequest.objects.filter(
        event_id__in=event_ids,
        ambassador_id__in=amb_ids,
        status=ShiftExtensionRequest.STATUS_APPROVED,
    ).values_list("event_id", "ambassador_id", "approved_minutes"):
        sf = shifts.get((ev_id, amb_id))
        if sf is not None:
            sf.extension_minutes += int(minutes or 0)

    # --- recaps --------------------------------------------------------------
    for ev_id, amb_id, submitted in CustomRecap.objects.filter(
        event_id__in=event_ids, ambassador_id__in=amb_ids
    ).values_list("event_id", "ambassador_id", "submitted_at"):
        sf = shifts.get((ev_id, amb_id))
        if sf is None:
            continue
        sf.has_recap = True
        submitted = _aware(submitted)
        if submitted and (
            sf.recap_submitted_at is None or submitted < sf.recap_submitted_at
        ):
            # The FIRST submit is the one that tells us when it was filled.
            sf.recap_submitted_at = submitted
    for ev_id, amb_id in Recap.objects.filter(
        event_id__in=event_ids, ambassador_id__in=amb_ids
    ).values_list("event_id", "ambassador_id"):
        sf = shifts.get((ev_id, amb_id))
        if sf is not None:
            sf.has_recap = True

    # --- GPS pings -------------------------------------------------------------
    pinged = set(
        LocationPing.objects.filter(
            event_id__in=event_ids, ambassador_id__in=amb_ids
        )
        .values_list("event_id", "ambassador_id")
        .distinct()
    )
    for key in pinged:
        if key in shifts:
            shifts[key].has_pings = True

    th_window = timedelta(minutes=30)
    recap_shifts = [
        sf for sf in shifts.values()
        if sf.recap_submitted_at and sf.venue and sf.key in pinged
    ]
    if recap_shifts:
        lo = min(sf.recap_submitted_at for sf in recap_shifts) - th_window
        hi = max(sf.recap_submitted_at for sf in recap_shifts) + th_window
        best: dict[tuple[int, int], tuple[float, tuple[float, float]]] = {}
        for ev_id, amb_id, lat, lng, at in LocationPing.objects.filter(
            event_id__in={sf.event_id for sf in recap_shifts},
            ambassador_id__in={sf.ambassador_id for sf in recap_shifts},
            recorded_at__gte=lo,
            recorded_at__lte=hi,
        ).values_list("event_id", "ambassador_id", "lat", "lng", "recorded_at"):
            sf = shifts.get((ev_id, amb_id))
            if sf is None or sf.recap_submitted_at is None:
                continue
            c = valid_coords([lat, lng])
            if c is None:
                continue
            gap = abs((_aware(at) - sf.recap_submitted_at).total_seconds()) / 60.0
            if sf.key not in best or gap < best[sf.key][0]:
                best[sf.key] = (gap, c)
        for key, (gap, c) in best.items():
            sf = shifts[key]
            sf.recap_ping_minutes = round(gap, 1)
            sf.recap_ping_miles = round(haversine_miles(c, sf.venue), 2)

    # --- mileage -------------------------------------------------------------
    for ev_id, amb_id, miles in (
        MileageSession.objects.filter(
            event_id__in=event_ids,
            ambassador_id__in=amb_ids,
            status=MileageSession.STATUS_COMPLETED,
        )
        .values("event_id", "ambassador_id")
        .annotate(m=Sum("total_miles"))
        .values_list("event_id", "ambassador_id", "m")
    ):
        sf = shifts.get((ev_id, amb_id))
        if sf is not None and miles is not None:
            sf.gps_miles = round(float(miles), 2)
    for ev_id, amb_id, miles in (
        PayableMileageClaim.objects.filter(
            event_id__in=event_ids, ambassador_id__in=amb_ids
        )
        .values("event_id", "ambassador_id")
        .annotate(m=Sum("payable_miles"))
        .values_list("event_id", "ambassador_id", "m")
    ):
        sf = shifts.get((ev_id, amb_id))
        if sf is not None and miles is not None:
            sf.claim_miles = round(float(miles), 2)

    # --- the same BAs' other shifts (any client), for overlap + travel -------
    _attach_other_shifts(shifts, amb_ids, start, end)

    return sorted(
        shifts.values(),
        key=lambda s: (s.first_in or s.booked_start or now, s.event_id),
    )


def _attach_other_shifts(shifts, amb_ids, start, end) -> None:
    """Every clocked interval of these BAs on events dated a day either side
    of the period, across ALL clients — a BA double-booked on two brands is
    exactly the case to catch. Only staff ever see the result."""
    from ambassadors.models import AmbassadorEvent
    from events.models import Event

    ev_ids = set(
        AmbassadorEvent.objects.filter(
            ambassador_id__in=amb_ids,
            is_approved=True,
            event__date__date__gte=start - timedelta(days=1),
            event__date__date__lte=end + timedelta(days=1),
        ).values_list("event_id", flat=True)[:20000]
    )
    if not ev_ids:
        return
    evs = {
        e.id: e
        for e in Event.objects.filter(id__in=ev_ids).only(
            "id", "uuid", "name", "tenant_id", "coordinates"
        )
    }
    positions = _venue_positions(evs.values())
    by_pair: dict[tuple[int, int], list[Punch]] = defaultdict(list)
    for (ev_id, amb_id, kind, at, *_rest) in _punch_rows(ev_ids, amb_ids):
        if at is None:
            continue
        by_pair[(ev_id, amb_id)].append(
            Punch(kind, _aware(at), None, None, None, None, None)
        )
    intervals: dict[int, list[OtherShift]] = defaultdict(list)
    for (ev_id, amb_id), punches in by_pair.items():
        first_in, last_out, _on = _pair_bounds(punches)
        if not first_in or not last_out or last_out <= first_in:
            continue
        ev = evs.get(ev_id)
        if ev is None:
            continue
        pos = positions.get(ev_id)
        intervals[amb_id].append(
            OtherShift(
                event_id=ev_id,
                event_uuid=str(ev.uuid),
                event_name=ev.name or "",
                tenant_id=ev.tenant_id,
                first_in=first_in,
                last_out=last_out,
                venue=pos[0] if pos else None,
            )
        )
    for sf in shifts.values():
        sf.others = [
            o for o in intervals.get(sf.ambassador_id, []) if o.event_id != sf.event_id
        ]
