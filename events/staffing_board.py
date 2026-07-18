"""Staffing Board — a week-at-a-glance grid of shifts and who's assigned.

`staffingBoard(startDate, endDate, tenantId?)` returns every event in the date
range for the admin's tenant(s), each with its assigned BAs and a staffed flag.
The web grid lays these out (rows = shifts/stores, columns = dates); an empty
cell opens the existing assign flow (inviteAmbassadorToShift, approved=true).

Self-contained (own module) like ambassadors/walkup.py so the query is easy to
reason about; wired into the clients + spark event query classes.
"""
from __future__ import annotations

from datetime import date as _date

import strawberry
from asgiref.sync import sync_to_async
from strawberry.relay import to_base64

from events.models import Event
from utils.graphql.permissions import (
    IsClientOrSparkAdmin,
    _is_admin_access,
    resolve_request_user_access,
)
from utils.graphql.mixins import resolve_id_to_int


@strawberry.type
class StaffingBoardBA:
    ambassador_uuid: str
    # Relay global id for the invite/assign mutation's ambassadorId.
    ambassador_id: str
    name: str
    is_approved: bool


@strawberry.type
class StaffingBoardShift:
    event_uuid: str
    # Relay global id the web passes to inviteAmbassadorToShift.eventId.
    event_id: str
    event_name: str
    brand_name: str
    date: str | None = None
    start_time: str | None = None
    end_time: str | None = None
    address: str | None = None
    store: str | None = None
    request_uuid: str | None = None
    tenant_id: str | None = None
    assigned: list[StaffingBoardBA] = strawberry.field(default_factory=list)
    # True once at least one BA is firmly booked (approved) on the shift.
    is_staffed: bool = False


async def _accessible_tenants(user):
    """(is_admin_access, allowed_tenant_ids_or_None). None = all tenants."""
    rs, st, su, em = await resolve_request_user_access(user)
    if _is_admin_access(rs, st, su, em):
        return True, None
    from tenants.models import TenantedUser

    tids = await sync_to_async(
        lambda: set(
            TenantedUser.objects.filter(user=user).values_list(
                "tenant_id", flat=True
            )
        )
    )()
    return False, tids


def _iso(dt) -> str | None:
    try:
        return dt.isoformat() if dt else None
    except Exception:  # noqa: BLE001
        return None


@strawberry.type
class StaffingBoardQueries:
    @strawberry.field(permission_classes=[IsClientOrSparkAdmin])
    async def staffing_board(
        self,
        info: strawberry.Info,
        start_date: str,
        end_date: str,
        tenant_id: strawberry.ID | None = None,
    ) -> list[StaffingBoardShift]:
        """Shifts in [start_date, end_date] (inclusive, by event date) with
        their assigned BAs. Tenant-scoped; capped at 600 shifts."""
        user = info.context.request.user
        is_admin, allowed = await _accessible_tenants(user)
        resolved_tid = None
        if tenant_id is not None:
            try:
                resolved_tid = resolve_id_to_int(tenant_id)
            except Exception:  # noqa: BLE001
                resolved_tid = None

        def _go():
            try:
                start = _date.fromisoformat(str(start_date))
                end = _date.fromisoformat(str(end_date))
            except (TypeError, ValueError):
                return []

            qs = (
                Event.objects.filter(
                    date__date__gte=start, date__date__lte=end
                )
                .select_related("tenant", "request", "retailer")
                .prefetch_related(
                    "ambassadors_events__ambassador__user"
                )
                .order_by("date", "start_time", "name")
            )
            if not is_admin:
                qs = qs.filter(tenant_id__in=(allowed or set()))
            elif resolved_tid is not None:
                qs = qs.filter(tenant_id=resolved_tid)

            shifts: list[StaffingBoardShift] = []
            for ev in qs[:600]:
                assigned: list[StaffingBoardBA] = []
                approved_count = 0
                for ae in ev.ambassadors_events.all():
                    amb = ae.ambassador
                    user_ = getattr(amb, "user", None)
                    nm = ""
                    if user_:
                        nm = (
                            f"{user_.first_name or ''} {user_.last_name or ''}".strip()
                            or (user_.email or "")
                        )
                    assigned.append(
                        StaffingBoardBA(
                            ambassador_uuid=str(amb.uuid),
                            ambassador_id=to_base64("AmbassadorType", amb.id),
                            name=nm or "(unnamed)",
                            is_approved=ae.is_approved,
                        )
                    )
                    if ae.is_approved:
                        approved_count += 1

                store = None
                if ev.retailer_id and getattr(ev, "retailer", None):
                    store = ev.retailer.name
                elif ev.request_id and getattr(ev, "request", None):
                    store = getattr(ev.request, "retailer_name", None)

                shifts.append(
                    StaffingBoardShift(
                        event_uuid=str(ev.uuid),
                        event_id=to_base64("EventType", ev.id),
                        event_name=ev.name,
                        brand_name=ev.tenant.name if ev.tenant_id else "",
                        date=_iso(getattr(ev, "date", None)),
                        start_time=_iso(getattr(ev, "start_time", None)),
                        end_time=_iso(getattr(ev, "end_time", None)),
                        address=getattr(ev, "address", None),
                        store=store,
                        request_uuid=(
                            str(ev.request.uuid)
                            if ev.request_id and getattr(ev, "request", None)
                            else None
                        ),
                        tenant_id=(
                            to_base64("TenantType", ev.tenant_id)
                            if ev.tenant_id
                            else None
                        ),
                        assigned=assigned,
                        is_staffed=approved_count > 0,
                    )
                )
            return shifts

        return await sync_to_async(_go)()
