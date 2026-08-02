"""Live "on the clock" board — today's shifts and who's actually working.

`liveShiftBoard(date?, tenantId?)` returns the day's shifts (default today) for
the admin's tenant(s), each assigned BA tagged with a live clock status so the
web board can render green (on the clock) / blue (done) / red (no-show) / amber
(not clocked in yet) at a glance. The read-side companion to the no-show radar:
the radar pushes/emails; this is the screen you keep open during the day.

Status is clock-centric (that's what the board is about): a BA with a
clock_in/clock_out Attendance is on/off the clock; one with none is placed by
time — upcoming, awaiting (started, still in grace), no_show (past grace, shift
still running), or missed (shift over, never showed).
"""
from __future__ import annotations

from datetime import date as _date, timedelta

import strawberry
from asgiref.sync import sync_to_async

from events.models import Event
from utils.graphql.permissions import IsClientOrSparkAdmin
from utils.graphql.mixins import resolve_id_to_int
from events.staffing_board import _accessible_tenants, _iso

# Minutes after start with no clock-in before a BA counts as a no-show
# (matches send_no_show_alerts' default threshold).
NO_SHOW_GRACE_MIN = 45

# settings.TIME_ZONE is UTC, so timezone.localdate() rolls over to tomorrow at
# 5pm Pacific — a "who's working right now" board that silently swapped to
# tomorrow's shifts every afternoon. "Today" here is the ops calendar day, the
# same basis email_daily_checkin_hours uses for its nightly digest.
OPS_TZ = "America/Los_Angeles"

# How far back to look for a still-open punch. Matches
# checkin_web.OPEN_SHIFT_RESUME_HOURS: past this, treat it as a BA who forgot
# to clock out rather than someone still on the floor.
OPEN_SHIFT_LOOKBACK_HOURS = 18


def _ops_today() -> _date:
    """Today's calendar date in the ops timezone (NOT the UTC date)."""
    from zoneinfo import ZoneInfo

    from django.utils import timezone

    return timezone.now().astimezone(ZoneInfo(OPS_TZ)).date()


def _open_shift_event_ids(now) -> set[int]:
    """Event ids where at least one BA is currently punched in.

    "Currently" = their most recent punch in the lookback window is a
    clock_in. Deliberately not tenant-scoped — the caller ORs these into a
    queryset that already carries the tenant filter.
    """
    from ambassadors.models import Attendance

    since = now - timedelta(hours=OPEN_SHIFT_LOOKBACK_HOURS)
    rows = (
        Attendance.objects.filter(
            clock_time__gte=since,
            source__name__in=["clock_in", "clock_out"],
        )
        .values_list("event_id", "ambassador_id", "source__name")
        .order_by("clock_time", "id")
    )
    latest: dict[tuple, str] = {}
    for ev_id, amb_id, src in rows:
        if ev_id is None:
            continue
        latest[(ev_id, amb_id)] = src  # time-ordered: last write wins
    return {ev_id for (ev_id, _amb), src in latest.items() if src == "clock_in"}


def _latest_locations(event_ids) -> dict[tuple[int, int], dict]:
    """{(event_id, ambassador_id): {lat, lng, at, source, accuracy}} — the
    freshest GPS fix per BA per shift.

    Primary source is LocationPing (written by both the mobile tracker and
    the web check-in flow). Falls back to the coordinates stamped on the BA's
    most recent clock punch, so a browser BA who granted location once at
    clock-in still gets a pin even though no ping loop is running.
    """
    from ambassadors.models import Attendance, LocationPing

    if not event_ids:
        return {}

    ids = list(event_ids)
    out: dict[tuple[int, int], dict] = {}

    # Clock punches first (the weaker source), so live pings overwrite them.
    punches = (
        Attendance.objects.filter(
            event_id__in=ids,
            source__name__in=["clock_in", "clock_out"],
            coordinates__isnull=False,
        )
        .values_list(
            "event_id", "ambassador_id", "coordinates", "clock_time", "source__name"
        )
        .order_by("clock_time", "id")
    )
    for ev_id, amb_id, coords, when, src in punches:
        # ArrayField(size=2) stored as [lat, lng]; [] means "no fix".
        if amb_id is None or not coords or len(coords) < 2:
            continue
        try:
            lat, lng = float(coords[0]), float(coords[1])
        except (TypeError, ValueError):
            continue
        if lat == 0.0 and lng == 0.0:
            continue
        # Time-ordered, so the last write per key wins.
        out[(ev_id, amb_id)] = {
            "lat": lat,
            "lng": lng,
            "at": when,
            "source": src,
            "accuracy": None,
        }

    # DISTINCT ON gives us the newest ping per (event, BA) in one round trip
    # instead of dragging back every 2-minute reading for the whole day.
    pings = (
        LocationPing.objects.filter(event_id__in=ids)
        .order_by("event_id", "ambassador_id", "-recorded_at")
        .distinct("event_id", "ambassador_id")
        .values_list(
            "event_id",
            "ambassador_id",
            "lat",
            "lng",
            "recorded_at",
            "source",
            "accuracy_meters",
        )
    )
    for ev_id, amb_id, lat, lng, when, src, acc in pings:
        if amb_id is None or lat is None or lng is None:
            continue
        prev = out.get((ev_id, amb_id))
        # Only let a ping win if it's genuinely newer than the punch we have.
        if prev and prev["at"] and when and when < prev["at"]:
            continue
        out[(ev_id, amb_id)] = {
            "lat": float(lat),
            "lng": float(lng),
            "at": when,
            "source": src,
            "accuracy": acc,
        }

    return out


@strawberry.type
class LiveBoardBA:
    ambassador_uuid: str
    name: str
    # upcoming | awaiting | clocked_in | clocked_out | no_show | missed
    status: str
    clock_in_at: str | None = None
    clock_out_at: str | None = None
    # Worked hours so far (clock pair) or None while still open / never clocked.
    worked_hours: float | None = None
    # Last known GPS fix for this BA on this shift — the freshest LocationPing,
    # falling back to the coordinates stamped on their clock punch. Null when
    # they denied location or the browser never got a fix; the board still
    # shows them, just without a pin.
    lat: float | None = None
    lng: float | None = None
    location_at: str | None = None
    # foreground | background | clock_in | clock_out | punch
    location_source: str | None = None
    accuracy_meters: float | None = None
    # True when this BA reached the shift through a walk-up / standing
    # check-in link and their booking is still unapproved. They are working
    # (that's why they're on this board) but their hours don't count until a
    # recap is approved — see approve_booking_for_recap.
    pending_approval: bool = False


@strawberry.type
class LiveBoardShift:
    event_uuid: str
    event_name: str
    brand_name: str
    date: str | None = None
    start_time: str | None = None
    end_time: str | None = None
    address: str | None = None
    store: str | None = None
    request_uuid: str | None = None
    assigned: list[LiveBoardBA] = strawberry.field(default_factory=list)
    # Rollups for the shift card header.
    on_clock: int = 0
    no_shows: int = 0
    # How many of `assigned` are walk-ups still awaiting recap approval.
    pending_approval: int = 0


@strawberry.type
class LiveBoardQueries:
    @strawberry.field(permission_classes=[IsClientOrSparkAdmin])
    async def client_live_token(
        self,
        info: strawberry.Info,
        tenant_id: strawberry.ID,
    ) -> str:
        """Mint a signed share token for a tenant's public client-live page.
        The web builds the shareable URL as `<origin>/live/<token>`. Admin
        can only mint for a tenant they can access."""
        user = info.context.request.user
        is_admin, allowed = await _accessible_tenants(user)
        try:
            tid = resolve_id_to_int(tenant_id)
        except Exception:  # noqa: BLE001
            raise ValueError("Invalid tenant id.")
        if not is_admin and tid not in (allowed or set()):
            raise ValueError("Not allowed for this tenant.")
        from events.client_live_tokens import make_client_live_token

        return await sync_to_async(make_client_live_token)(tid)

    @strawberry.field(permission_classes=[IsClientOrSparkAdmin])
    async def live_shift_board(
        self,
        info: strawberry.Info,
        date: str | None = None,
        tenant_id: strawberry.ID | None = None,
    ) -> list[LiveBoardShift]:
        """Today's (or `date`'s) shifts with each BA's live clock status.
        Tenant-scoped; capped at 400 shifts. `date` is YYYY-MM-DD (local)."""
        user = info.context.request.user
        is_admin, allowed = await _accessible_tenants(user)
        resolved_tid = None
        if tenant_id is not None:
            try:
                resolved_tid = resolve_id_to_int(tenant_id)
            except Exception:  # noqa: BLE001
                resolved_tid = None

        def _go():
            from django.db.models import Q
            from django.utils import timezone
            from ambassadors.attendance_hours import clock_facts, worked_hours

            now = timezone.now()
            try:
                day = _date.fromisoformat(str(date)) if date else _ops_today()
            except (TypeError, ValueError):
                day = _ops_today()

            # A shift stays on the board while someone is still punched into
            # it, whatever calendar day it was filed under. Without this an
            # evening shift falls off at midnight ops-time while the BA is
            # standing in the store, and a BA in a later timezone can drop off
            # before they clock out.
            open_ids = _open_shift_event_ids(now)

            qs = (
                Event.objects.filter(Q(date__date=day) | Q(id__in=open_ids))
                .select_related("tenant", "request", "retailer")
                .prefetch_related("ambassadors_events__ambassador__user")
                .order_by("start_time", "name")
            )
            if not is_admin:
                qs = qs.filter(tenant_id__in=(allowed or set()))
            elif resolved_tid is not None:
                qs = qs.filter(tenant_id=resolved_tid)

            events = list(qs[:400])
            event_ids = [ev.id for ev in events]
            facts = clock_facts(event_ids)
            locs = _latest_locations(event_ids)

            grace = timedelta(minutes=NO_SHOW_GRACE_MIN)
            shifts: list[LiveBoardShift] = []
            for ev in events:
                start = getattr(ev, "start_time", None)
                end = getattr(ev, "end_time", None)
                started = bool(start and start <= now)
                ended = bool(end and end <= now)

                assigned: list[LiveBoardBA] = []
                on_clock = 0
                no_shows = 0
                pending = 0
                for ae in ev.ambassadors_events.all():
                    amb = ae.ambassador
                    if amb is None:
                        continue
                    f = facts.get((ev.id, amb.id))
                    # Unapproved bookings are normally noise (applicants,
                    # pending invites) — but a walk-up who punched in through
                    # the standing check-in link is unapproved until their
                    # recap is signed off, and they are physically working
                    # right now. Dropping them made the board answer "nobody
                    # is on the clock" while people were on the clock. Clock
                    # activity is the tiebreaker: no punch, still hidden.
                    if not ae.is_approved and not f:
                        continue
                    u = getattr(amb, "user", None)
                    nm = ""
                    if u:
                        nm = (
                            f"{u.first_name or ''} {u.last_name or ''}".strip()
                            or (u.email or "")
                        )
                    wh, _est = worked_hours(f, None)  # None sched → real-only
                    latest = f.get("latest_kind") if f else None

                    if latest == "clock_in":
                        status = "clocked_in"
                        on_clock += 1
                    elif latest == "clock_out":
                        status = "clocked_out"
                    elif not started:
                        status = "upcoming"
                    elif ended:
                        status = "missed"
                        no_shows += 1
                    elif start and (now - start) > grace:
                        status = "no_show"
                        no_shows += 1
                    else:
                        status = "awaiting"

                    if not ae.is_approved:
                        pending += 1

                    loc = locs.get((ev.id, amb.id)) or {}
                    assigned.append(
                        LiveBoardBA(
                            ambassador_uuid=str(amb.uuid),
                            name=nm or "(unnamed)",
                            status=status,
                            clock_in_at=_iso(f.get("first_in")) if f else None,
                            clock_out_at=_iso(f.get("last_out")) if f else None,
                            worked_hours=wh,
                            lat=loc.get("lat"),
                            lng=loc.get("lng"),
                            location_at=_iso(loc.get("at")),
                            location_source=loc.get("source"),
                            accuracy_meters=loc.get("accuracy"),
                            pending_approval=not ae.is_approved,
                        )
                    )

                store = None
                if ev.retailer_id and getattr(ev, "retailer", None):
                    store = ev.retailer.name
                elif ev.request_id and getattr(ev, "request", None):
                    store = getattr(ev.request, "retailer_name", None)

                # Skip empty shells — the board is about staffed shifts today.
                if not assigned:
                    continue

                shifts.append(
                    LiveBoardShift(
                        event_uuid=str(ev.uuid),
                        event_name=ev.name,
                        brand_name=ev.tenant.name if ev.tenant_id else "",
                        date=_iso(getattr(ev, "date", None)),
                        start_time=_iso(start),
                        end_time=_iso(end),
                        address=getattr(ev, "address", None),
                        store=store,
                        request_uuid=(
                            str(ev.request.uuid)
                            if ev.request_id and getattr(ev, "request", None)
                            else None
                        ),
                        assigned=assigned,
                        on_clock=on_clock,
                        no_shows=no_shows,
                        pending_approval=pending,
                    )
                )
            return shifts

        return await sync_to_async(_go)()
