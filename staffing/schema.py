"""GraphQL for Ignite staffing overflow + Smart BA Matching (``schema_clients``).

* ``smartBaMatches`` — composite-ranked BA shortlist with transparent reasons.
* ``requestIgniteStaffing`` / ``igniteStaffingRequests`` — operational handoff
  queue with **zero client billing/checkout**.
* ``triggerNoShowCascade`` / ``noShowCascadeAlerts`` — backup cascade
  watchdog surface (manual trigger + live alert list).

Tenant posture mirrors ``ambassadors.staffing``: client-role callers are
pinned to their own tenant; Ignite staff / spark-admin / ``@igniteproductions.co``
may triage across workspaces.
"""

from __future__ import annotations

import uuid as uuid_lib
from typing import List

import strawberry
from asgiref.sync import sync_to_async
from django.utils import timezone
from graphql import GraphQLError
from strawberry.scalars import JSON

from events.models import Event
from staffing.cascade import trigger_noshow_cascade_for_event
from staffing.matching import compute_smart_ba_matches
from staffing.models import IgniteStaffingRequest, NoShowCascadeAlert
from utils.graphql.inputs import SparkGraphQLInput
from utils.graphql.mixins import SparkGraphQLMixin, resolve_id_to_int
from utils.graphql.permissions import (
    IsClientOrSparkAdmin,
    StrictIsAuthenticated,
    email_grants_ignite_admin,
    resolve_request_user_access,
)
from utils.utils import build_mutation_response


# ---------------------------------------------------------------------------
# Access helpers
# ---------------------------------------------------------------------------
class _StaffingAccess(SparkGraphQLMixin):
    async def is_ignite_staff(self, info: strawberry.Info) -> bool:
        user = await self.get_user(info)
        role_slug, is_staff, is_super, email = await resolve_request_user_access(user)
        return bool(
            is_staff
            or is_super
            or role_slug == "spark-admin"
            or email_grants_ignite_admin(email)
        )

    async def scope_tenant_id(self, info: strawberry.Info) -> int | None:
        """None for Ignite staff (cross-tenant); otherwise the caller's tenant pk."""
        if await self.is_ignite_staff(info):
            return None
        user = await self.get_user(info)
        tenant = await self.get_user_tenant(info, user=user)
        return int(tenant.id)

    async def resolve_write_tenant_id(
        self,
        info: strawberry.Info,
        tenant_id: strawberry.ID | None,
    ) -> int:
        """Tenant pk a caller may create overflow requests for."""
        scope = await self.scope_tenant_id(info)
        if scope is not None:
            if tenant_id is not None:
                try:
                    requested = resolve_id_to_int(tenant_id)
                except Exception as exc:  # noqa: BLE001
                    raise GraphQLError("Invalid tenant id.") from exc
                if requested != scope:
                    raise GraphQLError(
                        "You can only request Ignite staffing for your own workspace."
                    )
            return scope
        if tenant_id is None:
            raise GraphQLError("tenantId is required for Ignite staff.")
        return resolve_id_to_int(tenant_id)


def _resolve_event(identifier: str, scope_tenant_id: int | None) -> Event | None:
    clean = (identifier or "").strip()
    if not clean:
        return None
    qs = Event.objects.select_related("tenant")
    if scope_tenant_id is not None:
        qs = qs.filter(tenant_id=scope_tenant_id)
    try:
        uuid_lib.UUID(clean)
        return qs.filter(uuid=clean).first()
    except (ValueError, AttributeError, TypeError):
        try:
            return qs.filter(id=int(clean)).first()
        except (ValueError, TypeError):
            return None


# ---------------------------------------------------------------------------
# Types
# ---------------------------------------------------------------------------
@strawberry.type
class SmartBaMatch:
    ba_id: strawberry.ID
    ba_uuid: str
    name: str
    score: int
    units_per_hour: float | None = None
    reliability_rate: float | None = None
    on_time_checkins: int = 0
    total_tracked_shifts: int = 0
    brand_certified: bool = False
    brand_gigs: int = 0
    distance_miles: float | None = None
    is_available: bool | None = None
    is_favorited: bool = False
    email: str | None = None
    phone: str | None = None
    reasons: List[str] = strawberry.field(default_factory=list)
    summary_reason: str = ""


@strawberry.type
class IgniteStaffingRequestType:
    id: strawberry.ID
    uuid: str
    tenant_id: strawberry.ID
    tenant_name: str
    status: str
    source: str
    title: str
    activation_type: str
    requested_shift_count: int
    requested_ba_count: int
    requested_dates: List[str]
    requested_stores: List[str]
    event_ids: List[str]
    notes: str
    ops_notes: str
    requested_by_name: str | None = None
    assigned_ops_owner_name: str | None = None
    accepted_at: str | None = None
    assigned_at: str | None = None
    completed_at: str | None = None
    created_at: str
    updated_at: str


@strawberry.type
class NoShowCascadeAlertType:
    id: strawberry.ID
    uuid: str
    tenant_id: strawberry.ID
    tenant_name: str
    event_id: strawberry.ID
    event_uuid: str
    event_name: str
    trigger_reason: str
    status: str
    minutes_past_start: int | None = None
    notified_backup_bas: JSON | None = None
    delivery_audit: JSON | None = None
    ops_notified_emails: List[str] = strawberry.field(default_factory=list)
    notes: str = ""
    dispatched_at: str | None = None
    resolved_at: str | None = None
    created_at: str


@strawberry.type
class RequestIgniteStaffingResult:
    success: bool
    message: str
    client_mutation_id: strawberry.ID | None = None
    request: IgniteStaffingRequestType | None = None


@strawberry.type
class UpdateIgniteStaffingRequestResult:
    success: bool
    message: str
    client_mutation_id: strawberry.ID | None = None
    request: IgniteStaffingRequestType | None = None


@strawberry.type
class TriggerNoShowCascadeResult:
    success: bool
    message: str
    client_mutation_id: strawberry.ID | None = None
    alert: NoShowCascadeAlertType | None = None
    created: bool = False


def _person_name(user) -> str | None:
    if user is None:
        return None
    full = f"{getattr(user, 'first_name', '')} {getattr(user, 'last_name', '')}".strip()
    return full or getattr(user, "email", None) or None


def _request_type(row: IgniteStaffingRequest) -> IgniteStaffingRequestType:
    return IgniteStaffingRequestType(
        id=strawberry.ID(str(row.id)),
        uuid=str(row.uuid),
        tenant_id=strawberry.ID(str(row.tenant_id)),
        tenant_name=getattr(row.tenant, "name", "") or "",
        status=row.status,
        source=row.source,
        title=row.title or "",
        activation_type=row.activation_type or "",
        requested_shift_count=int(row.requested_shift_count or 0),
        requested_ba_count=int(row.requested_ba_count or 0),
        requested_dates=[str(d) for d in (row.requested_dates or [])],
        requested_stores=[str(s) for s in (row.requested_stores or [])],
        event_ids=[str(e) for e in (row.event_ids or [])],
        notes=row.notes or "",
        ops_notes=row.ops_notes or "",
        requested_by_name=_person_name(getattr(row, "requested_by", None)),
        assigned_ops_owner_name=_person_name(getattr(row, "assigned_ops_owner", None)),
        accepted_at=row.accepted_at.isoformat() if row.accepted_at else None,
        assigned_at=row.assigned_at.isoformat() if row.assigned_at else None,
        completed_at=row.completed_at.isoformat() if row.completed_at else None,
        created_at=row.created_at.isoformat(),
        updated_at=row.updated_at.isoformat(),
    )


def _alert_type(row: NoShowCascadeAlert) -> NoShowCascadeAlertType:
    event = getattr(row, "event", None)
    return NoShowCascadeAlertType(
        id=strawberry.ID(str(row.id)),
        uuid=str(row.uuid),
        tenant_id=strawberry.ID(str(row.tenant_id)),
        tenant_name=getattr(row.tenant, "name", "") or "",
        event_id=strawberry.ID(str(row.event_id)),
        event_uuid=str(getattr(event, "uuid", "") or "") if event is not None else "",
        event_name=(getattr(event, "name", None) or "") if event is not None else "",
        trigger_reason=row.trigger_reason,
        status=row.status,
        minutes_past_start=row.minutes_past_start,
        notified_backup_bas=row.notified_backup_bas or [],
        delivery_audit=row.delivery_audit or [],
        ops_notified_emails=[str(e) for e in (row.ops_notified_emails or [])],
        notes=row.notes or "",
        dispatched_at=row.dispatched_at.isoformat() if row.dispatched_at else None,
        resolved_at=row.resolved_at.isoformat() if row.resolved_at else None,
        created_at=row.created_at.isoformat(),
    )


def _match_type(row: dict) -> SmartBaMatch:
    return SmartBaMatch(
        ba_id=strawberry.ID(str(row["ba_id"])),
        ba_uuid=str(row["ba_uuid"]),
        name=row["name"],
        score=int(row["score"]),
        units_per_hour=row.get("units_per_hour"),
        reliability_rate=row.get("reliability_rate"),
        on_time_checkins=int(row.get("on_time_checkins") or 0),
        total_tracked_shifts=int(row.get("total_tracked_shifts") or 0),
        brand_certified=bool(row.get("brand_certified")),
        brand_gigs=int(row.get("brand_gigs") or 0),
        distance_miles=row.get("distance_miles"),
        is_available=row.get("is_available"),
        is_favorited=bool(row.get("is_favorited")),
        email=row.get("email"),
        phone=row.get("phone"),
        reasons=list(row.get("reasons") or []),
        summary_reason=str(row.get("summary_reason") or ""),
    )


# ---------------------------------------------------------------------------
# Inputs
# ---------------------------------------------------------------------------
@strawberry.input
class RequestIgniteStaffingInput(SparkGraphQLInput):
    tenant_id: strawberry.ID | None = None
    source: str | None = None
    title: str | None = None
    activation_type: str | None = None
    requested_shift_count: int | None = 1
    requested_ba_count: int | None = 1
    requested_dates: List[str] | None = None
    requested_stores: List[str] | None = None
    event_ids: List[str] | None = None
    notes: str | None = None


@strawberry.input
class UpdateIgniteStaffingRequestInput(SparkGraphQLInput):
    uuid: str
    status: str
    ops_notes: str | None = None


@strawberry.input
class TriggerNoShowCascadeInput(SparkGraphQLInput):
    event_id: strawberry.ID
    notes: str | None = None
    force: bool | None = False


# ---------------------------------------------------------------------------
# Queries
# ---------------------------------------------------------------------------
@strawberry.type
class IgniteStaffingQueries:
    @strawberry.field(permission_classes=[IsClientOrSparkAdmin])
    async def smart_ba_matches(
        self,
        info: strawberry.Info,
        event_id: strawberry.ID | None = None,
        store_key: str | None = None,
        tenant_id: strawberry.ID | None = None,
        limit: int | None = 20,
    ) -> List[SmartBaMatch]:
        """Ranked BA shortlist with contact fields — client / staff only."""
        access = _StaffingAccess()
        scope = await access.scope_tenant_id(info)

        try:
            cap = int(limit) if limit is not None else 20
        except (TypeError, ValueError):
            cap = 20

        def _build() -> List[SmartBaMatch]:
            target_tenant: int | None = None
            event_pk: int | None = None

            if event_id is not None:
                event = _resolve_event(str(event_id), scope)
                if event is None:
                    return []
                target_tenant = int(event.tenant_id)
                event_pk = int(event.id)
            elif tenant_id is not None:
                try:
                    tid = resolve_id_to_int(tenant_id)
                except Exception:
                    return []
                if scope is not None and tid != scope:
                    return []
                target_tenant = tid
            elif scope is not None:
                target_tenant = scope
            else:
                return []

            rows = compute_smart_ba_matches(
                tenant_id=target_tenant,
                event_id=event_pk,
                store_key=(store_key or "").strip() or None,
                limit=cap,
            )
            return [_match_type(r) for r in rows]

        return await sync_to_async(_build, thread_sensitive=True)()

    @strawberry.field(permission_classes=[IsClientOrSparkAdmin])
    async def ignite_staffing_requests(
        self,
        info: strawberry.Info,
        status: str | None = None,
        tenant_id: strawberry.ID | None = None,
        first: int | None = 50,
    ) -> List[IgniteStaffingRequestType]:
        access = _StaffingAccess()
        scope = await access.scope_tenant_id(info)

        def _load() -> List[IgniteStaffingRequestType]:
            qs = IgniteStaffingRequest.objects.select_related(
                "tenant", "requested_by", "assigned_ops_owner"
            ).order_by("-created_at", "-id")
            if scope is not None:
                qs = qs.filter(tenant_id=scope)
            elif tenant_id is not None:
                try:
                    qs = qs.filter(tenant_id=resolve_id_to_int(tenant_id))
                except Exception:
                    return []
            if status:
                qs = qs.filter(status=status.strip().lower())
            try:
                cap = max(1, min(int(first or 50), 200))
            except (TypeError, ValueError):
                cap = 50
            return [_request_type(row) for row in qs[:cap]]

        return await sync_to_async(_load, thread_sensitive=True)()

    @strawberry.field(permission_classes=[IsClientOrSparkAdmin])
    async def no_show_cascade_alerts(
        self,
        info: strawberry.Info,
        status: str | None = None,
        tenant_id: strawberry.ID | None = None,
        first: int | None = 50,
    ) -> List[NoShowCascadeAlertType]:
        access = _StaffingAccess()
        scope = await access.scope_tenant_id(info)

        def _load() -> List[NoShowCascadeAlertType]:
            qs = NoShowCascadeAlert.objects.select_related(
                "tenant", "event"
            ).order_by("-created_at", "-id")
            if scope is not None:
                qs = qs.filter(tenant_id=scope)
            elif tenant_id is not None:
                try:
                    qs = qs.filter(tenant_id=resolve_id_to_int(tenant_id))
                except Exception:
                    return []
            if status:
                qs = qs.filter(status=status.strip().lower())
            try:
                cap = max(1, min(int(first or 50), 200))
            except (TypeError, ValueError):
                cap = 50
            return [_alert_type(row) for row in qs[:cap]]

        return await sync_to_async(_load, thread_sensitive=True)()


# ---------------------------------------------------------------------------
# Mutations
# ---------------------------------------------------------------------------
@strawberry.type
class IgniteStaffingMutations:
    @strawberry.mutation(permission_classes=[IsClientOrSparkAdmin])
    async def request_ignite_staffing(
        self, info: strawberry.Info, input: RequestIgniteStaffingInput
    ) -> RequestIgniteStaffingResult:
        """Create an Ignite ops staffing overflow handoff — no billing checkout."""
        access = _StaffingAccess()
        user = await access.get_user(info)
        tenant_pk = await access.resolve_write_tenant_id(info, input.tenant_id)

        source = (input.source or IgniteStaffingRequest.SOURCE_WAVE_SHEET).strip()
        valid_sources = {c[0] for c in IgniteStaffingRequest.SOURCE_CHOICES}
        if source not in valid_sources:
            source = IgniteStaffingRequest.SOURCE_WAVE_SHEET

        shift_count = max(1, int(input.requested_shift_count or 1))
        ba_count = max(1, int(input.requested_ba_count or 1))

        def _create() -> IgniteStaffingRequest:
            from tenants.models import Tenant

            tenant = Tenant.objects.filter(id=tenant_pk).first()
            if tenant is None:
                raise GraphQLError("Workspace not found.")
            title = (input.title or "").strip()
            if not title:
                title = f"Ignite staffing · {tenant.name}"
            return IgniteStaffingRequest.objects.create(
                tenant=tenant,
                requested_by=user if getattr(user, "is_authenticated", False) else None,
                status=IgniteStaffingRequest.STATUS_PENDING,
                source=source,
                title=title[:255],
                activation_type=(input.activation_type or "").strip()[:120],
                requested_shift_count=shift_count,
                requested_ba_count=ba_count,
                requested_dates=list(input.requested_dates or []),
                requested_stores=list(input.requested_stores or []),
                event_ids=list(input.event_ids or []),
                notes=(input.notes or "").strip()[:4000],
            )

        try:
            row = await sync_to_async(_create, thread_sensitive=True)()
        except GraphQLError as exc:
            return build_mutation_response(
                RequestIgniteStaffingResult,
                success=False,
                message=str(exc),
                input_obj=input,
            )

        row = await sync_to_async(
            lambda: IgniteStaffingRequest.objects.select_related(
                "tenant", "requested_by", "assigned_ops_owner"
            ).get(id=row.id),
            thread_sensitive=True,
        )()
        return build_mutation_response(
            RequestIgniteStaffingResult,
            success=True,
            message=(
                f"Ignite staffing requested for {row.requested_shift_count} "
                f"shift{'s' if row.requested_shift_count != 1 else ''} — "
                "ops will follow up. No billing required."
            ),
            input_obj=input,
            request=_request_type(row),
        )

    @strawberry.mutation(permission_classes=[StrictIsAuthenticated])
    async def update_ignite_staffing_request_status(
        self, info: strawberry.Info, input: UpdateIgniteStaffingRequestInput
    ) -> UpdateIgniteStaffingRequestResult:
        access = _StaffingAccess()
        if not await access.is_ignite_staff(info):
            return build_mutation_response(
                UpdateIgniteStaffingRequestResult,
                success=False,
                message="Only Ignite staff can update staffing request status.",
                input_obj=input,
            )
        user = await access.get_user(info)
        status = (input.status or "").strip().lower()
        if status not in IgniteStaffingRequest.STAFF_MANAGEABLE_STATUSES:
            return build_mutation_response(
                UpdateIgniteStaffingRequestResult,
                success=False,
                message="Unknown staffing request status.",
                input_obj=input,
            )

        def _update() -> IgniteStaffingRequest | None:
            row = (
                IgniteStaffingRequest.objects.select_related(
                    "tenant", "requested_by", "assigned_ops_owner"
                )
                .filter(uuid=str(input.uuid).strip())
                .first()
            )
            if row is None:
                return None
            now = timezone.now()
            row.status = status
            if input.ops_notes is not None:
                row.ops_notes = (input.ops_notes or "").strip()[:4000]
            if status == IgniteStaffingRequest.STATUS_ACCEPTED and row.accepted_at is None:
                row.accepted_at = now
                row.assigned_ops_owner = user
            if status == IgniteStaffingRequest.STATUS_ASSIGNED and row.assigned_at is None:
                row.assigned_at = now
                if row.accepted_at is None:
                    row.accepted_at = now
                row.assigned_ops_owner = row.assigned_ops_owner or user
            if status == IgniteStaffingRequest.STATUS_COMPLETED and row.completed_at is None:
                row.completed_at = now
                if row.assigned_at is None:
                    row.assigned_at = now
                if row.accepted_at is None:
                    row.accepted_at = now
            row.save()
            return row

        row = await sync_to_async(_update, thread_sensitive=True)()
        if row is None:
            return build_mutation_response(
                UpdateIgniteStaffingRequestResult,
                success=False,
                message="Staffing request not found.",
                input_obj=input,
            )
        return build_mutation_response(
            UpdateIgniteStaffingRequestResult,
            success=True,
            message=f"Status updated to {status}.",
            input_obj=input,
            request=_request_type(row),
        )

    @strawberry.mutation(permission_classes=[StrictIsAuthenticated])
    async def trigger_no_show_cascade(
        self, info: strawberry.Info, input: TriggerNoShowCascadeInput
    ) -> TriggerNoShowCascadeResult:
        access = _StaffingAccess()
        if not await access.is_ignite_staff(info):
            return build_mutation_response(
                TriggerNoShowCascadeResult,
                success=False,
                message="Only Ignite staff can trigger backup cascades.",
                input_obj=input,
            )
        user = await access.get_user(info)

        def _run():
            event = _resolve_event(str(input.event_id), None)
            if event is None:
                return None, False, "Event not found."
            alert, created, message = trigger_noshow_cascade_for_event(
                event=event,
                trigger_reason=NoShowCascadeAlert.REASON_MANUAL,
                triggered_by=user,
                notes=input.notes or "",
                force=bool(input.force),
            )
            if alert is not None:
                alert = (
                    NoShowCascadeAlert.objects.select_related("tenant", "event")
                    .filter(id=alert.id)
                    .first()
                )
            return alert, created, message

        alert, created, message = await sync_to_async(_run, thread_sensitive=True)()
        # Existing-alert case still returns the alert (success=True, created=False).
        # Missing/malformed event returns alert=None → success=False.
        return build_mutation_response(
            TriggerNoShowCascadeResult,
            success=alert is not None,
            message=message,
            input_obj=input,
            alert=_alert_type(alert) if alert is not None else None,
            created=bool(created),
        )
