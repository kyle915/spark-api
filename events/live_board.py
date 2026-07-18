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
            from django.utils import timezone
            from ambassadors.attendance_hours import clock_facts, worked_hours

            now = timezone.now()
            try:
                day = _date.fromisoformat(str(date)) if date else timezone.localdate()
            except (TypeError, ValueError):
                day = timezone.localdate()

            qs = (
                Event.objects.filter(date__date=day)
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
                for ae in ev.ambassadors_events.all():
                    if not ae.is_approved:
                        continue
                    amb = ae.ambassador
                    if amb is None:
                        continue
                    u = getattr(amb, "user", None)
                    nm = ""
                    if u:
                        nm = (
                            f"{u.first_name or ''} {u.last_name or ''}".strip()
                            or (u.email or "")
                        )
                    f = facts.get((ev.id, amb.id))
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

                    assigned.append(
                        LiveBoardBA(
                            ambassador_uuid=str(amb.uuid),
                            name=nm or "(unnamed)",
                            status=status,
                            clock_in_at=_iso(f.get("first_in")) if f else None,
                            clock_out_at=_iso(f.get("last_out")) if f else None,
                            worked_hours=wh,
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
                    )
                )
            return shifts

        return await sync_to_async(_go)()
