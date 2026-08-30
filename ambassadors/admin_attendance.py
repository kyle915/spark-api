"""Admin overrides for BA clock punches.

Ops need to manually clock a BA in, edit punch times, or close an open
punch when the BA's check-in fails (phone, GPS, session). Reuses the same
``Attendance`` + ``Source.name`` rows the BA mobile/web flows write — no
second punch table.

Mutations live on the clients schema (``IsClientOrSparkAdmin``) and mirror
the walk-up admin pattern: tenant-scoped for clients, all tenants for
Ignite admins. Side effects stay minimal — no recap stub on admin
clock-out (matching ``abandon_open_clock``); clock-in still auto-confirms
the booking like the BA path.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone as dt_timezone

import strawberry
from strawberry import relay
from asgiref.sync import sync_to_async
from django.utils import timezone as dj_tz
from django.utils.dateparse import parse_datetime

from utils.graphql.permissions import IsClientOrSparkAdmin


# --------------------------------------------------------------------------
# Types
# --------------------------------------------------------------------------
@strawberry.type
class AdminAttendanceResponse:
    success: bool
    message: str
    clock_in_at: str | None = None
    clock_out_at: str | None = None
    attendance_uuid: str | None = None
    client_mutation_id: strawberry.ID | None = None


@strawberry.input
class AdminClockPunchInput:
    """Manual clock-in or clock-out for a BA on an event."""

    event_uuid: strawberry.ID
    ambassador_uuid: strawberry.ID
    # ISO 8601. Omit / blank → server now. Admins can set any past time
    # (unlike the BA offline queue's 24h cap).
    clock_time: str | None = None
    note: str | None = None
    client_mutation_id: strawberry.ID | None = None


@strawberry.input
class AdminEditPunchInput:
    """Edit an existing punch's ``clock_time``.

    Prefer ``attendance_uuid`` when the UI has it (Attendance table).
    Otherwise pass ``event_uuid`` + ``ambassador_uuid`` + ``kind``
    (``clock_in`` / ``clock_out``) — used by the live board.
    """

    clock_time: str
    attendance_uuid: strawberry.ID | None = None
    event_uuid: strawberry.ID | None = None
    ambassador_uuid: strawberry.ID | None = None
    kind: str | None = None
    note: str | None = None
    client_mutation_id: strawberry.ID | None = None


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------
async def _admin_scope(user):
    """(is_admin_access, allowed_tenant_ids_or_None). None = all tenants."""
    from utils.graphql.permissions import (
        _is_admin_access,
        resolve_request_user_access,
    )

    rs, st, su, em = await resolve_request_user_access(user)
    if _is_admin_access(rs, st, su, em):
        return True, None
    from tenants.models import TenantedUser

    tids = await sync_to_async(
        lambda: list(
            TenantedUser.objects.filter(user=user).values_list(
                "tenant_id", flat=True
            )
        )
    )()
    return False, set(tids)


def _parse_admin_clock_time(raw: str | None):
    """Parse optional admin ISO clock time. ``None``/blank → server now.

    Unlike the BA offline path, admins may set times older than 24h.
    Future times more than 2 minutes ahead are refused.
    """
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return dj_tz.now()
    if not isinstance(raw, str):
        raise ValueError("clock_time must be an ISO timestamp.")
    parsed = parse_datetime(raw.strip())
    if parsed is None:
        try:
            parsed = datetime.fromisoformat(raw.strip().replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError("clock_time must be an ISO timestamp.") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt_timezone.utc)
    now = dj_tz.now()
    if parsed > now + timedelta(minutes=2):
        raise ValueError("That clock time is in the future.")
    return parsed


def _ba_name(ambassador) -> str:
    user = getattr(ambassador, "user", None)
    if not user:
        return "(BA)"
    name = (
        f"{(getattr(user, 'first_name', '') or '').strip()} "
        f"{(getattr(user, 'last_name', '') or '').strip()}"
    ).strip()
    return name or (getattr(user, "email", "") or "(BA)")


def _clock_payload(ambassador_id: int, event_id: int) -> dict:
    from ambassadors.checkin_web import clock_state

    state = clock_state(ambassador_id=ambassador_id, event_id=event_id)
    return {
        "clock_in_at": state.get("clockInAt"),
        "clock_out_at": state.get("clockOutAt"),
    }


def _log_attendance_adjust(
    *,
    event,
    actor,
    summary: str,
    metadata: dict,
) -> None:
    """Best-effort RequestActivityLog when the event has a parent request."""
    try:
        from events.activity_log import _safe_log
        from events.models import Request, RequestActivityLog

        req_id = getattr(event, "request_id", None)
        if not req_id:
            return
        req = Request.objects.filter(id=req_id).first()
        if req is None:
            return
        _safe_log(
            request=req,
            kind=RequestActivityLog.KIND_ATTENDANCE_ADJUSTED,
            actor_user=actor,
            summary=summary,
            metadata=metadata,
        )
    except Exception:  # noqa: BLE001 — audit must never break punches
        return


def _resolve_event_and_ba(event_uuid: str, ambassador_uuid: str):
    from ambassadors.models import Ambassador
    from events.models import Event

    event = (
        Event.objects.select_related("tenant", "request")
        .filter(uuid=str(event_uuid))
        .first()
    )
    ambassador = (
        Ambassador.objects.select_related("user")
        .filter(uuid=str(ambassador_uuid))
        .first()
    )
    return event, ambassador


def _resolve_booking(event, ambassador, actor):
    """Existing AmbassadorEvent for (BA, event), or None.

    Manual clock requires the BA already be on the shift roster (or a
    walk-up booking). We do not invent assignments here.
    """
    from ambassadors.models import AmbassadorEvent

    return (
        AmbassadorEvent.objects.select_related(
            "ambassador", "ambassador__user", "event"
        )
        .filter(ambassador=ambassador, event=event)
        .first()
    )


def _ensure_tenant_ok(event, is_admin, allowed) -> str | None:
    if event is None:
        return "notfound_event"
    if not is_admin and event.tenant_id not in (allowed or set()):
        return "denied"
    return None


# --------------------------------------------------------------------------
# Mutations
# --------------------------------------------------------------------------
@strawberry.type
class AdminAttendanceMutations:
    @relay.mutation(permission_classes=[IsClientOrSparkAdmin])
    async def admin_manual_clock_in(
        self, info: strawberry.Info, input: AdminClockPunchInput
    ) -> AdminAttendanceResponse:
        """Manually clock a BA in for an event (ops override)."""
        user = info.context.request.user
        is_admin, allowed = await _admin_scope(user)

        def _go():
            from ambassadors.checkin_web import (
                clock_state,
                record_attendance,
            )
            from ambassadors.mutations import _auto_confirm_on_attendance

            event, ambassador = _resolve_event_and_ba(
                str(input.event_uuid), str(input.ambassador_uuid)
            )
            err = _ensure_tenant_ok(event, is_admin, allowed)
            if err == "notfound_event":
                return "notfound", "Event not found.", None, None
            if err == "denied":
                return "denied", "Not authorized.", None, None
            if ambassador is None:
                return "notfound", "Ambassador not found.", None, None

            amb_event = _resolve_booking(event, ambassador, user)
            if amb_event is None:
                return (
                    "nobooking",
                    "This BA is not assigned to this shift. Assign them first.",
                    None,
                    None,
                )

            state = clock_state(
                ambassador_id=ambassador.id, event_id=event.id
            )
            if state.get("state") == "clocked_in":
                return (
                    "already",
                    "Already clocked in — clock out or edit the existing punch.",
                    None,
                    None,
                )

            try:
                when = _parse_admin_clock_time(input.clock_time)
            except ValueError as exc:
                return "badtime", str(exc), None, None

            # If they already clocked out earlier, refuse a second open
            # punch that would sit after the out (re-open needs edit/out delete).
            if state.get("state") == "clocked_out" and state.get("clockOutAt"):
                # Allow a new clock-in after a completed pair (multi-punch day)
                # only when the new in is after the last out.
                out_iso = state["clockOutAt"]
                out_dt = parse_datetime(out_iso) or datetime.fromisoformat(
                    out_iso.replace("Z", "+00:00")
                )
                if out_dt.tzinfo is None:
                    out_dt = out_dt.replace(tzinfo=dt_timezone.utc)
                if when <= out_dt:
                    return (
                        "badtime",
                        "New clock-in must be after the previous clock-out.",
                        None,
                        None,
                    )

            att = record_attendance(
                amb_event=amb_event,
                kind="clock_in",
                coordinates=None,
                actor=user,
                clock_time=when,
            )
            if getattr(att, "id", None):
                att.created_by = user
                att.updated_by = user
                att.save(update_fields=["created_by", "updated_by", "updated_at"])

            try:
                _auto_confirm_on_attendance(amb_event, "clock_in")
            except Exception:  # noqa: BLE001
                pass

            note = (input.note or "").strip()
            _log_attendance_adjust(
                event=event,
                actor=user,
                summary=(
                    f"Admin clocked in {_ba_name(ambassador)}"
                    + (f" — {note}" if note else "")
                ),
                metadata={
                    "action": "manual_clock_in",
                    "ambassador_uuid": str(ambassador.uuid),
                    "attendance_uuid": str(att.uuid),
                    "clock_time": when.isoformat(),
                    "note": note or None,
                },
            )
            payload = _clock_payload(ambassador.id, event.id)
            return "ok", "Clocked in.", att, payload

        status, message, att, payload = await sync_to_async(_go)()
        if status != "ok":
            return AdminAttendanceResponse(
                success=False,
                message=message,
                client_mutation_id=input.client_mutation_id,
            )
        return AdminAttendanceResponse(
            success=True,
            message=message,
            clock_in_at=(payload or {}).get("clock_in_at"),
            clock_out_at=(payload or {}).get("clock_out_at"),
            attendance_uuid=str(att.uuid) if att else None,
            client_mutation_id=input.client_mutation_id,
        )

    @relay.mutation(permission_classes=[IsClientOrSparkAdmin])
    async def admin_manual_clock_out(
        self, info: strawberry.Info, input: AdminClockPunchInput
    ) -> AdminAttendanceResponse:
        """Close an open punch for a BA (ops override). Does not start a recap."""
        user = info.context.request.user
        is_admin, allowed = await _admin_scope(user)

        def _go():
            from ambassadors.checkin_web import clock_state, record_attendance

            event, ambassador = _resolve_event_and_ba(
                str(input.event_uuid), str(input.ambassador_uuid)
            )
            err = _ensure_tenant_ok(event, is_admin, allowed)
            if err == "notfound_event":
                return "notfound", "Event not found.", None, None
            if err == "denied":
                return "denied", "Not authorized.", None, None
            if ambassador is None:
                return "notfound", "Ambassador not found.", None, None

            amb_event = _resolve_booking(event, ambassador, user)
            if amb_event is None:
                return (
                    "nobooking",
                    "This BA is not assigned to this shift.",
                    None,
                    None,
                )

            state = clock_state(
                ambassador_id=ambassador.id, event_id=event.id
            )
            if state.get("state") != "clocked_in":
                return (
                    "notin",
                    "Not clocked in — nothing to clock out.",
                    None,
                    None,
                )

            try:
                when = _parse_admin_clock_time(input.clock_time)
            except ValueError as exc:
                return "badtime", str(exc), None, None

            in_iso = state.get("clockInAt")
            if in_iso:
                in_dt = parse_datetime(in_iso) or datetime.fromisoformat(
                    in_iso.replace("Z", "+00:00")
                )
                if in_dt.tzinfo is None:
                    in_dt = in_dt.replace(tzinfo=dt_timezone.utc)
                if when <= in_dt:
                    return (
                        "badtime",
                        "Clock-out must be after clock-in.",
                        None,
                        None,
                    )

            att = record_attendance(
                amb_event=amb_event,
                kind="clock_out",
                coordinates=None,
                actor=user,
                clock_time=when,
            )
            if getattr(att, "id", None):
                att.created_by = user
                att.updated_by = user
                att.save(update_fields=["created_by", "updated_by", "updated_at"])

            note = (input.note or "").strip()
            _log_attendance_adjust(
                event=event,
                actor=user,
                summary=(
                    f"Admin clocked out {_ba_name(ambassador)}"
                    + (f" — {note}" if note else "")
                ),
                metadata={
                    "action": "manual_clock_out",
                    "ambassador_uuid": str(ambassador.uuid),
                    "attendance_uuid": str(att.uuid),
                    "clock_time": when.isoformat(),
                    "note": note or None,
                },
            )
            payload = _clock_payload(ambassador.id, event.id)
            return "ok", "Clocked out.", att, payload

        status, message, att, payload = await sync_to_async(_go)()
        if status != "ok":
            return AdminAttendanceResponse(
                success=False,
                message=message,
                client_mutation_id=input.client_mutation_id,
            )
        return AdminAttendanceResponse(
            success=True,
            message=message,
            clock_in_at=(payload or {}).get("clock_in_at"),
            clock_out_at=(payload or {}).get("clock_out_at"),
            attendance_uuid=str(att.uuid) if att else None,
            client_mutation_id=input.client_mutation_id,
        )

    @relay.mutation(permission_classes=[IsClientOrSparkAdmin])
    async def admin_edit_punch(
        self, info: strawberry.Info, input: AdminEditPunchInput
    ) -> AdminAttendanceResponse:
        """Edit an existing clock-in or clock-out timestamp."""
        user = info.context.request.user
        is_admin, allowed = await _admin_scope(user)

        def _go():
            from ambassadors.models import Attendance

            try:
                when = _parse_admin_clock_time(input.clock_time)
            except ValueError as exc:
                return "badtime", str(exc), None, None

            att = None
            if input.attendance_uuid:
                att = (
                    Attendance.objects.select_related(
                        "source", "ambassador", "ambassador__user", "event"
                    )
                    .filter(uuid=str(input.attendance_uuid))
                    .first()
                )
            elif input.event_uuid and input.ambassador_uuid and input.kind:
                kind = (input.kind or "").strip().lower()
                if kind not in ("clock_in", "clock_out"):
                    return (
                        "badkind",
                        "kind must be clock_in or clock_out.",
                        None,
                        None,
                    )
                event, ambassador = _resolve_event_and_ba(
                    str(input.event_uuid), str(input.ambassador_uuid)
                )
                if event is None or ambassador is None:
                    return "notfound", "Event or ambassador not found.", None, None
                err = _ensure_tenant_ok(event, is_admin, allowed)
                if err == "denied":
                    return "denied", "Not authorized.", None, None
                qs = (
                    Attendance.objects.filter(
                        event=event,
                        ambassador=ambassador,
                        source__name=kind,
                    )
                    .select_related(
                        "source", "ambassador", "ambassador__user", "event"
                    )
                    .order_by("clock_time", "id")
                )
                # First clock_in / latest clock_out — matches eventAttendance.
                att = qs.first() if kind == "clock_in" else qs.last()
            else:
                return (
                    "badinput",
                    "Provide attendanceUuid, or eventUuid + ambassadorUuid + kind.",
                    None,
                    None,
                )

            if att is None:
                return "notfound", "Punch not found.", None, None

            event = att.event
            err = _ensure_tenant_ok(event, is_admin, allowed)
            if err == "denied":
                return "denied", "Not authorized.", None, None
            if event is None:
                return "notfound", "Punch has no event.", None, None

            kind = (getattr(att.source, "name", "") or "").lower()
            old_time = att.clock_time

            # Keep pair ordering valid when the sibling punch exists.
            sibling_kind = (
                "clock_out" if kind == "clock_in" else "clock_in"
                if kind == "clock_out"
                else None
            )
            if sibling_kind and att.ambassador_id:
                siblings = list(
                    Attendance.objects.filter(
                        event_id=event.id,
                        ambassador_id=att.ambassador_id,
                        source__name=sibling_kind,
                    ).order_by("clock_time", "id")
                )
                if kind == "clock_in" and siblings:
                    # Compare against the latest out that should stay after this in.
                    out = siblings[-1]
                    if when >= out.clock_time:
                        return (
                            "badtime",
                            "Clock-in must be before clock-out.",
                            None,
                            None,
                        )
                if kind == "clock_out" and siblings:
                    cin = siblings[0]
                    if when <= cin.clock_time:
                        return (
                            "badtime",
                            "Clock-out must be after clock-in.",
                            None,
                            None,
                        )

            att.clock_time = when
            att.updated_by = user
            att.save(update_fields=["clock_time", "updated_by", "updated_at"])

            note = (input.note or "").strip()
            amb = att.ambassador
            _log_attendance_adjust(
                event=event,
                actor=user,
                summary=(
                    f"Admin edited {kind or 'punch'} for {_ba_name(amb)}"
                    + (f" — {note}" if note else "")
                ),
                metadata={
                    "action": "edit_punch",
                    "kind": kind,
                    "ambassador_uuid": str(amb.uuid) if amb else None,
                    "attendance_uuid": str(att.uuid),
                    "old_clock_time": old_time.isoformat() if old_time else None,
                    "new_clock_time": when.isoformat(),
                    "note": note or None,
                },
            )
            payload = (
                _clock_payload(att.ambassador_id, event.id)
                if att.ambassador_id
                else {"clock_in_at": None, "clock_out_at": None}
            )
            return "ok", "Punch updated.", att, payload

        status, message, att, payload = await sync_to_async(_go)()
        if status != "ok":
            return AdminAttendanceResponse(
                success=False,
                message=message,
                client_mutation_id=input.client_mutation_id,
            )
        return AdminAttendanceResponse(
            success=True,
            message=message,
            clock_in_at=(payload or {}).get("clock_in_at"),
            clock_out_at=(payload or {}).get("clock_out_at"),
            attendance_uuid=str(att.uuid) if att else None,
            client_mutation_id=input.client_mutation_id,
        )
