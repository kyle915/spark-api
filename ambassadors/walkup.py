"""Walk-up self-serve clock-in — GraphQL surface.

A BA can clock in + file a recap for an event WITHOUT being pre-assigned:
an admin generates a short code (Event.walkup_code) for the event, hands it
out, and the BA enters it (or scans its QR) in the app. The code resolves to
exactly one event + brand, so there's no cross-brand search and recaps land on
the right brand. Possession of the code is the authorization; a brand-new
account's shift stays pending (is_approved=False) until an admin confirms it in
the Walk-ups queue, so its hours never count in KPI/payroll rollups (those
already gate on is_approved=True) until reviewed.

Kept in its own module so the mobile (resolve/start) and admin (generate/
confirm/reject/list) surfaces are easy to reason about; wired into the schemas
via ambassadors/schema.py.
"""
from __future__ import annotations

import math
import secrets
from datetime import datetime, timedelta

import strawberry
from strawberry import relay
from asgiref.sync import sync_to_async
from django.utils import timezone as dj_tz

from events.models import Event
from utils.graphql.permissions import (
    StrictIsAuthenticated,
    IsClientOrSparkAdmin,
)
from utils.graphql.mixins import resolve_id_to_int
from ambassadors import inputs as amb_inputs
from .models import Ambassador, AmbassadorEvent, Attendance


# Unambiguous alphabet — no I/O/0/1 so a code read off a screen or printout
# can't be mistyped. Codes are minted uppercase; the resolver matches
# case-insensitively so a BA can type lowercase.
_CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"


def _gen_code(length: int = 6) -> str:
    return "".join(secrets.choice(_CODE_ALPHABET) for _ in range(length))


def _haversine_miles(a_lat, a_lng, b_lat, b_lng) -> float | None:
    try:
        rlat1, rlat2 = math.radians(a_lat), math.radians(b_lat)
        dlat = math.radians(b_lat - a_lat)
        dlng = math.radians(b_lng - a_lng)
        h = (
            math.sin(dlat / 2) ** 2
            + math.cos(rlat1) * math.cos(rlat2) * math.sin(dlng / 2) ** 2
        )
        return round(2 * 3958.8 * math.asin(math.sqrt(h)), 2)
    except (TypeError, ValueError):
        return None


def _code_expiry_for_event(event: Event) -> datetime:
    """Codes stay valid through the end of the event's day plus a one-day
    grace buffer (so a shift that runs late, or a next-morning recap, still
    works). No event date → 2 days from now. Admins can revoke sooner."""
    base = getattr(event, "start_time", None) or getattr(event, "date", None)
    base = base or dj_tz.now()
    # end of that calendar day + 1 day grace
    end_of_day = base.replace(hour=23, minute=59, second=59, microsecond=0)
    return end_of_day + timedelta(days=1)


def _user_in_tenant(user, tenant_id: int) -> bool:
    from tenants.models import TenantedUser

    return TenantedUser.objects.filter(user=user, tenant_id=tenant_id).exists()


# --------------------------------------------------------------------------
# Shared GraphQL types
# --------------------------------------------------------------------------
@strawberry.type
class WalkupEventOption:
    """Minimal event info returned when a code resolves — enough for the BA
    to confirm "yes, this is where I am" without exposing anything sensitive."""

    event_uuid: str
    event_name: str
    brand_name: str
    address: str | None = None
    start_time: str | None = None


@strawberry.type
class WalkupCodeResolveResponse:
    found: bool
    message: str
    event: WalkupEventOption | None = None


@strawberry.input
class StartWalkupShiftInput:
    code: str
    latitude: float | None = None
    longitude: float | None = None
    client_mutation_id: strawberry.ID | None = None


@strawberry.type
class StartWalkupShiftResponse:
    success: bool
    message: str
    ambassador_event_uuid: str | None = None
    event_uuid: str | None = None
    # True when the shift needs an admin's confirmation before it counts
    # (a brand-new / not-yet-active account). An already-active BA is
    # auto-booked and this is False.
    pending_review: bool = False
    client_mutation_id: strawberry.ID | None = None


@strawberry.type
class WalkupSignUpResponse:
    success: bool
    message: str
    # Session tokens so a brand-new walk-up can act immediately (their
    # Ambassador stays unverified/pending — nothing they log counts until an
    # admin confirms it in the Walk-ups queue).
    token: str | None = None
    refresh_token: str | None = None
    client_mutation_id: strawberry.ID | None = None


# --------------------------------------------------------------------------
# Mobile surface
# --------------------------------------------------------------------------
@strawberry.type
class WalkupMobileQueries:
    @strawberry.field(permission_classes=[StrictIsAuthenticated])
    async def resolve_walkup_code(
        self, info: strawberry.Info, code: str
    ) -> WalkupCodeResolveResponse:
        """Resolve an event code the BA typed/scanned to its event + brand."""

        def _go():
            clean = (code or "").strip().upper()
            if not clean:
                return "empty", None
            event = (
                Event.objects.select_related("tenant")
                .filter(walkup_code__iexact=clean)
                .first()
            )
            if not event:
                return "notfound", None
            exp = event.walkup_code_expires_at
            if exp and exp < dj_tz.now():
                return "expired", None
            return "ok", event

        status, event = await sync_to_async(_go)()
        if status == "empty":
            return WalkupCodeResolveResponse(found=False, message="Enter a code.")
        if status == "notfound":
            return WalkupCodeResolveResponse(
                found=False,
                message="That code isn't valid. Double-check it with your lead.",
            )
        if status == "expired":
            return WalkupCodeResolveResponse(
                found=False,
                message="That code has expired. Ask your lead for a new one.",
            )
        opt = WalkupEventOption(
            event_uuid=str(event.uuid),
            event_name=event.name,
            brand_name=event.tenant.name if event.tenant_id else "",
            address=getattr(event, "address", None),
            start_time=(
                event.start_time.isoformat()
                if getattr(event, "start_time", None)
                else (event.date.isoformat() if event.date else None)
            ),
        )
        return WalkupCodeResolveResponse(found=True, message="OK", event=opt)


@strawberry.type
class WalkupMobileMutations:
    @relay.mutation
    async def walkup_sign_up(
        self, info: strawberry.Info, input: amb_inputs.CreatePublicAmbassadorInput
    ) -> WalkupSignUpResponse:
        """Create an account AND return a session token immediately so a
        brand-new walk-up can act right away (no email round-trip). The
        Ambassador is created unverified/pending — nothing they log counts
        until an admin confirms it. Public (no auth) — this IS the sign-up."""
        from ambassadors.services import PublicAmbassadorCreationService

        resp = await PublicAmbassadorCreationService.create(
            input, info, ambassador_is_active=False
        )
        if not resp.success or not resp.ambassador:
            return WalkupSignUpResponse(
                success=False,
                message=resp.message or "Couldn't create your account.",
            )
        user = await sync_to_async(lambda: resp.ambassador.user)()
        try:
            from gqlauth.jwt.types_ import TokenType
            from gqlauth.models import RefreshToken

            token_obj = await sync_to_async(TokenType.from_user)(user)
            token = token_obj.token
            try:
                refresh_obj = await sync_to_async(RefreshToken.from_user)(user)
                refresh = refresh_obj.token
            except Exception:  # noqa: BLE001 — refresh is optional
                refresh = None
        except Exception:  # noqa: BLE001 — account still created; ask to sign in
            return WalkupSignUpResponse(
                success=True,
                message="Account created — sign in to continue.",
            )
        return WalkupSignUpResponse(
            success=True,
            message="Account created.",
            token=token,
            refresh_token=refresh,
        )

    @relay.mutation(permission_classes=[StrictIsAuthenticated])
    async def start_walkup_shift(
        self, info: strawberry.Info, input: StartWalkupShiftInput
    ) -> StartWalkupShiftResponse:
        """Create (or reuse) this BA's booking for the coded event so the
        existing clock-in + recap flow can carry the rest. An already-active
        BA is auto-approved; a new account is left pending admin review."""
        user = info.context.request.user

        def _go():
            clean = (input.code or "").strip().upper()
            event = (
                Event.objects.select_related("tenant")
                .filter(walkup_code__iexact=clean)
                .first()
            )
            if not event:
                return None, "That code isn't valid. Double-check it with your lead.", None
            exp = event.walkup_code_expires_at
            if exp and exp < dj_tz.now():
                return None, "That code has expired. Ask your lead for a new one.", None
            try:
                amb = Ambassador.objects.select_related("user").get(user=user)
            except Ambassador.DoesNotExist:
                return None, "Finish setting up your profile first.", None

            auto = bool(getattr(amb, "is_active", False))
            amb_event, created = AmbassadorEvent.objects.get_or_create(
                ambassador=amb,
                event=event,
                defaults=dict(
                    tenant=event.tenant,
                    is_approved=auto,
                    source=AmbassadorEvent.SOURCE_WALKUP,
                    created_by=user,
                    updated_by=user,
                ),
            )
            return amb_event, "ok", event

        amb_event, msg, event = await sync_to_async(_go)()
        if amb_event is None:
            return StartWalkupShiftResponse(
                success=False, message=msg,
                client_mutation_id=input.client_mutation_id,
            )
        pending = not amb_event.is_approved
        return StartWalkupShiftResponse(
            success=True,
            message=(
                "Checked in — pending your lead's confirmation."
                if pending
                else f"You're set for {event.name}. Clock in when you're ready."
            ),
            ambassador_event_uuid=str(amb_event.uuid),
            event_uuid=str(event.uuid),
            pending_review=pending,
            client_mutation_id=input.client_mutation_id,
        )


# --------------------------------------------------------------------------
# Admin surface (clients schema)
# --------------------------------------------------------------------------
@strawberry.input
class GenerateWalkupCodeInput:
    event_uuid: strawberry.ID
    client_mutation_id: strawberry.ID | None = None


@strawberry.type
class WalkupCodeResponse:
    success: bool
    message: str
    code: str | None = None
    expires_at: str | None = None
    client_mutation_id: strawberry.ID | None = None


@strawberry.type
class WalkupShiftType:
    ambassador_event_uuid: str
    event_uuid: str
    event_name: str
    brand_name: str
    ambassador_name: str
    ambassador_email: str | None = None
    ambassador_phone: str | None = None
    is_new_account: bool = False
    is_approved: bool = False
    clock_in_at: str | None = None
    clock_out_at: str | None = None
    distance_from_venue_mi: float | None = None
    has_recap: bool = False
    created_at: str | None = None


@strawberry.input
class ConfirmWalkupShiftInput:
    ambassador_event_uuid: strawberry.ID
    # Optional: attach the walk-up to a DIFFERENT event (they entered the
    # wrong code / the admin wants to re-file it against the right shift).
    reassign_event_uuid: strawberry.ID | None = None
    client_mutation_id: strawberry.ID | None = None


@strawberry.input
class RejectWalkupShiftInput:
    ambassador_event_uuid: strawberry.ID
    client_mutation_id: strawberry.ID | None = None


@strawberry.type
class WalkupActionResponse:
    success: bool
    message: str
    client_mutation_id: strawberry.ID | None = None


@strawberry.input
class BulkWalkupShiftsInput:
    ambassador_event_uuids: list[strawberry.ID]
    client_mutation_id: strawberry.ID | None = None


@strawberry.type
class BulkWalkupActionResponse:
    success: bool
    message: str
    count: int = 0
    client_mutation_id: strawberry.ID | None = None


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


def _build_walkup_row(amb_event) -> WalkupShiftType:
    event = amb_event.event
    amb = amb_event.ambassador
    user = getattr(amb, "user", None)
    name = ""
    if user:
        name = (
            f"{user.first_name or ''} {user.last_name or ''}".strip()
            or (user.email or "")
        )
    atts = list(
        Attendance.objects.filter(
            ambassador=amb, event=event
        ).select_related("source")
    )
    clock_in = next(
        (a for a in sorted(atts, key=lambda x: x.clock_time)
         if getattr(a.source, "name", "") == "clock_in"),
        None,
    )
    clock_out = next(
        (a for a in sorted(atts, key=lambda x: x.clock_time, reverse=True)
         if getattr(a.source, "name", "") == "clock_out"),
        None,
    )
    dist = None
    if clock_in and clock_in.coordinates and event.coordinates:
        try:
            dist = _haversine_miles(
                clock_in.coordinates[0], clock_in.coordinates[1],
                event.coordinates[0], event.coordinates[1],
            )
        except (IndexError, TypeError):
            dist = None
    has_recap = False
    try:
        from recaps.models import CustomRecap, Recap

        has_recap = (
            CustomRecap.objects.filter(event=event, ambassador=amb).exists()
            or Recap.objects.filter(event=event, ambassador=amb).exists()
        )
    except Exception:  # noqa: BLE001 — recap presence is decorative
        has_recap = False
    return WalkupShiftType(
        ambassador_event_uuid=str(amb_event.uuid),
        event_uuid=str(event.uuid),
        event_name=event.name,
        brand_name=event.tenant.name if event.tenant_id else "",
        ambassador_name=name,
        ambassador_email=(user.email if user else None),
        ambassador_phone=getattr(amb, "phone_number", None)
        or getattr(user, "phone", None),
        is_new_account=not bool(getattr(amb, "is_active", False)),
        is_approved=amb_event.is_approved,
        clock_in_at=clock_in.clock_time.isoformat() if clock_in else None,
        clock_out_at=clock_out.clock_time.isoformat() if clock_out else None,
        distance_from_venue_mi=dist,
        has_recap=has_recap,
        created_at=amb_event.created_at.isoformat() if amb_event.created_at else None,
    )


@strawberry.type
class WalkupAdminQueries:
    @strawberry.field(permission_classes=[IsClientOrSparkAdmin])
    async def walkup_shifts(
        self,
        info: strawberry.Info,
        status: str | None = None,
        tenant_id: strawberry.ID | None = None,
    ) -> list[WalkupShiftType]:
        """Walk-up bookings for the admin's tenant(s). status="pending" (needs
        review) | "confirmed" | None (all). Newest first; capped at 200."""
        user = info.context.request.user
        is_admin, allowed = await _admin_scope(user)
        resolved_tid = None
        if tenant_id is not None:
            try:
                resolved_tid = resolve_id_to_int(tenant_id)
            except Exception:  # noqa: BLE001
                resolved_tid = None

        def _go():
            qs = (
                AmbassadorEvent.objects.filter(
                    source=AmbassadorEvent.SOURCE_WALKUP
                )
                .select_related(
                    "ambassador__user", "event", "event__tenant"
                )
                .order_by("-created_at")
            )
            if not is_admin:
                qs = qs.filter(tenant_id__in=(allowed or set()))
            elif resolved_tid is not None:
                qs = qs.filter(tenant_id=resolved_tid)
            if status == "pending":
                qs = qs.filter(is_approved=False)
            elif status == "confirmed":
                qs = qs.filter(is_approved=True)
            return [_build_walkup_row(ae) for ae in qs[:200]]

        return await sync_to_async(_go)()

    @strawberry.field(permission_classes=[IsClientOrSparkAdmin])
    async def pending_walkup_count(
        self,
        info: strawberry.Info,
        tenant_id: strawberry.ID | None = None,
    ) -> int:
        """Count of walk-ups awaiting review for the admin's tenant(s) — a light
        scalar for the sidebar badge (no row payload)."""
        user = info.context.request.user
        is_admin, allowed = await _admin_scope(user)
        resolved_tid = None
        if tenant_id is not None:
            try:
                resolved_tid = resolve_id_to_int(tenant_id)
            except Exception:  # noqa: BLE001
                resolved_tid = None

        def _go():
            qs = AmbassadorEvent.objects.filter(
                source=AmbassadorEvent.SOURCE_WALKUP, is_approved=False
            )
            if not is_admin:
                qs = qs.filter(tenant_id__in=(allowed or set()))
            elif resolved_tid is not None:
                qs = qs.filter(tenant_id=resolved_tid)
            return qs.count()

        return await sync_to_async(_go)()


@strawberry.type
class WalkupAdminMutations:
    @relay.mutation(permission_classes=[IsClientOrSparkAdmin])
    async def generate_walkup_code(
        self, info: strawberry.Info, input: GenerateWalkupCodeInput
    ) -> WalkupCodeResponse:
        """Mint (or refresh) the walk-up code for an event — enabling walk-ups
        for it. Idempotent-ish: regenerating replaces the prior code."""
        user = info.context.request.user
        is_admin, allowed = await _admin_scope(user)

        def _go():
            event = (
                Event.objects.select_related("tenant")
                .filter(uuid=str(input.event_uuid))
                .first()
            )
            if not event:
                return "notfound", None
            if not is_admin and event.tenant_id not in (allowed or set()):
                return "denied", None
            code = _gen_code()
            # Avoid the (astronomically unlikely) collision.
            for _ in range(6):
                if not Event.objects.filter(walkup_code__iexact=code).exists():
                    break
                code = _gen_code()
            event.walkup_code = code
            event.walkup_code_expires_at = _code_expiry_for_event(event)
            event.save(
                update_fields=["walkup_code", "walkup_code_expires_at"]
            )
            return "ok", event

        status, event = await sync_to_async(_go)()
        if status == "notfound":
            return WalkupCodeResponse(success=False, message="Event not found.")
        if status == "denied":
            return WalkupCodeResponse(success=False, message="Not authorized.")
        return WalkupCodeResponse(
            success=True,
            message="Walk-up code ready.",
            code=event.walkup_code,
            expires_at=(
                event.walkup_code_expires_at.isoformat()
                if event.walkup_code_expires_at
                else None
            ),
            client_mutation_id=input.client_mutation_id,
        )

    @relay.mutation(permission_classes=[IsClientOrSparkAdmin])
    async def revoke_walkup_code(
        self, info: strawberry.Info, input: GenerateWalkupCodeInput
    ) -> WalkupActionResponse:
        """Clear an event's walk-up code — disabling walk-ups for it."""
        user = info.context.request.user
        is_admin, allowed = await _admin_scope(user)

        def _go():
            event = Event.objects.filter(uuid=str(input.event_uuid)).first()
            if not event:
                return "notfound"
            if not is_admin and event.tenant_id not in (allowed or set()):
                return "denied"
            event.walkup_code = None
            event.walkup_code_expires_at = None
            event.save(
                update_fields=["walkup_code", "walkup_code_expires_at"]
            )
            return "ok"

        status = await sync_to_async(_go)()
        if status == "notfound":
            return WalkupActionResponse(success=False, message="Event not found.")
        if status == "denied":
            return WalkupActionResponse(success=False, message="Not authorized.")
        return WalkupActionResponse(
            success=True,
            message="Walk-ups turned off for this event.",
            client_mutation_id=input.client_mutation_id,
        )

    @relay.mutation(permission_classes=[IsClientOrSparkAdmin])
    async def confirm_walkup_shift(
        self, info: strawberry.Info, input: ConfirmWalkupShiftInput
    ) -> WalkupActionResponse:
        """Confirm a walk-up: approve the booking (hours now count), activate a
        new BA's account, and add them to the brand's roster. Optionally
        reassign it to a different event first."""
        user = info.context.request.user
        is_admin, allowed = await _admin_scope(user)

        def _go():
            amb_event = (
                AmbassadorEvent.objects.select_related(
                    "ambassador__user", "event", "tenant"
                )
                .filter(
                    uuid=str(input.ambassador_event_uuid),
                    source=AmbassadorEvent.SOURCE_WALKUP,
                )
                .first()
            )
            if not amb_event:
                return "notfound"
            if not is_admin and amb_event.tenant_id not in (allowed or set()):
                return "denied"
            if input.reassign_event_uuid:
                new_event = (
                    Event.objects.select_related("tenant")
                    .filter(uuid=str(input.reassign_event_uuid))
                    .first()
                )
                if new_event:
                    if not is_admin and new_event.tenant_id not in (allowed or set()):
                        return "denied"
                    amb_event.event = new_event
                    amb_event.tenant = new_event.tenant
            amb_event.is_approved = True
            amb_event.updated_by = user
            amb_event.save(
                update_fields=["is_approved", "event", "tenant", "updated_by", "updated_at"]
            )
            amb = amb_event.ambassador
            if amb and not getattr(amb, "is_active", False):
                amb.is_active = True
                amb.save(update_fields=["is_active"])
            # Brand-roster membership (best-effort; the confirm stands even if
            # the membership row can't be created).
            try:
                from tenants.models import TenantedUser

                if amb and amb.user_id:
                    TenantedUser.objects.get_or_create(
                        user=amb.user, tenant=amb_event.tenant
                    )
            except Exception:  # noqa: BLE001
                pass
            return "ok"

        status = await sync_to_async(_go)()
        if status == "notfound":
            return WalkupActionResponse(
                success=False, message="Walk-up not found."
            )
        if status == "denied":
            return WalkupActionResponse(success=False, message="Not authorized.")
        return WalkupActionResponse(
            success=True,
            message="Walk-up confirmed — hours now count.",
            client_mutation_id=input.client_mutation_id,
        )

    @relay.mutation(permission_classes=[IsClientOrSparkAdmin])
    async def reject_walkup_shift(
        self, info: strawberry.Info, input: RejectWalkupShiftInput
    ) -> WalkupActionResponse:
        """Reject a walk-up: remove the booking + its attendance punches. Any
        recap the BA filed stays and is handled through the normal recap
        review flow."""
        user = info.context.request.user
        is_admin, allowed = await _admin_scope(user)

        def _go():
            amb_event = (
                AmbassadorEvent.objects.select_related("ambassador", "event")
                .filter(
                    uuid=str(input.ambassador_event_uuid),
                    source=AmbassadorEvent.SOURCE_WALKUP,
                )
                .first()
            )
            if not amb_event:
                return "notfound"
            if not is_admin and amb_event.tenant_id not in (allowed or set()):
                return "denied"
            Attendance.objects.filter(
                ambassador=amb_event.ambassador, event=amb_event.event
            ).delete()
            amb_event.delete()
            return "ok"

        status = await sync_to_async(_go)()
        if status == "notfound":
            return WalkupActionResponse(
                success=False, message="Walk-up not found."
            )
        if status == "denied":
            return WalkupActionResponse(success=False, message="Not authorized.")
        return WalkupActionResponse(
            success=True,
            message="Walk-up rejected.",
            client_mutation_id=input.client_mutation_id,
        )

    @relay.mutation(permission_classes=[IsClientOrSparkAdmin])
    async def bulk_confirm_walkup_shifts(
        self, info: strawberry.Info, input: BulkWalkupShiftsInput
    ) -> BulkWalkupActionResponse:
        """Confirm many walk-ups at once (approve booking + activate new BAs +
        roster them). Skips any the admin can't access; reports how many landed.
        Same per-item semantics as confirm_walkup_shift."""
        user = info.context.request.user
        is_admin, allowed = await _admin_scope(user)
        uuids = [str(u) for u in (input.ambassador_event_uuids or [])]

        def _go():
            from tenants.models import TenantedUser

            confirmed = 0
            rows = (
                AmbassadorEvent.objects.select_related("ambassador__user", "tenant")
                .filter(uuid__in=uuids, source=AmbassadorEvent.SOURCE_WALKUP)
            )
            for amb_event in rows:
                if not is_admin and amb_event.tenant_id not in (allowed or set()):
                    continue
                if amb_event.is_approved:
                    continue
                amb_event.is_approved = True
                amb_event.updated_by = user
                amb_event.save(
                    update_fields=["is_approved", "updated_by", "updated_at"]
                )
                amb = amb_event.ambassador
                if amb and not getattr(amb, "is_active", False):
                    amb.is_active = True
                    amb.save(update_fields=["is_active"])
                try:
                    if amb and amb.user_id:
                        TenantedUser.objects.get_or_create(
                            user=amb.user, tenant=amb_event.tenant
                        )
                except Exception:  # noqa: BLE001
                    pass
                confirmed += 1
            return confirmed

        count = await sync_to_async(_go)()
        return BulkWalkupActionResponse(
            success=True,
            message=f"Confirmed {count} walk-up{'s' if count != 1 else ''} — hours now count.",
            count=count,
            client_mutation_id=input.client_mutation_id,
        )

    @relay.mutation(permission_classes=[IsClientOrSparkAdmin])
    async def bulk_reject_walkup_shifts(
        self, info: strawberry.Info, input: BulkWalkupShiftsInput
    ) -> BulkWalkupActionResponse:
        """Reject many walk-ups at once (remove booking + attendance punches).
        Any recap a BA filed stays for the normal recap review flow. Same
        per-item semantics as reject_walkup_shift."""
        user = info.context.request.user
        is_admin, allowed = await _admin_scope(user)
        uuids = [str(u) for u in (input.ambassador_event_uuids or [])]

        def _go():
            rejected = 0
            rows = (
                AmbassadorEvent.objects.select_related("ambassador", "event")
                .filter(uuid__in=uuids, source=AmbassadorEvent.SOURCE_WALKUP)
            )
            for amb_event in rows:
                if not is_admin and amb_event.tenant_id not in (allowed or set()):
                    continue
                Attendance.objects.filter(
                    ambassador=amb_event.ambassador, event=amb_event.event
                ).delete()
                amb_event.delete()
                rejected += 1
            return rejected

        count = await sync_to_async(_go)()
        return BulkWalkupActionResponse(
            success=True,
            message=f"Rejected {count} walk-up{'s' if count != 1 else ''}.",
            count=count,
            client_mutation_id=input.client_mutation_id,
        )
