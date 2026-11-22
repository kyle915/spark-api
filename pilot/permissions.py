"""Server-side workspace access resolution, role/capability checks,
context switching headers, and audit logging."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from uuid import UUID

from django.db.models import Q

from pilot.models import (
    CanonicalVisit,
    ProviderAssignment,
    ProviderOrganization,
    UserActiveWorkspaceContext,
    VisitSubmissionSource,
    WorkspaceAuditLog,
    WorkspaceMembership,
)
from tenants.models import Role, Tenant, TenantedUser
from utils.graphql.permissions import user_grants_ignite_admin

SELECTED_TENANT_HEADER = "HTTP_X_SPARK_TENANT"
SELECTED_MEMBERSHIP_HEADER = "HTTP_X_SPARK_MEMBERSHIP"


def is_ignite_staff_user(user: Any) -> bool:
    """True only for Ignite internal platform admins (never client or agency)."""
    if not user or not getattr(user, "is_authenticated", False):
        return False
    if getattr(user, "pk", None):
        return user_grants_ignite_admin(user)
    if getattr(user, "is_superuser", False) or getattr(user, "is_staff", False):
        return True
    role = getattr(user, "role", None)
    return bool(role and role.slug == Role.SPARK_ADMIN_SLUG)


@dataclass(frozen=True)
class ResolvedWorkspaceAccess:
    user: Any
    tenant: Tenant
    membership: WorkspaceMembership | None
    workspace_role: str
    provider: ProviderOrganization | None
    is_ignite_admin: bool
    is_legacy_managed_member: bool
    can_approve_recaps: bool
    can_approve_finance: bool
    can_approve_spend: bool

    @property
    def is_brand_member(self) -> bool:
        if self.is_ignite_admin or self.is_legacy_managed_member:
            return True
        return self.workspace_role in WorkspaceMembership.BRAND_ROLES

    @property
    def is_brand_admin(self) -> bool:
        return self.is_ignite_admin or (
            self.workspace_role == WorkspaceMembership.ROLE_BRAND_ADMIN
        )

    @property
    def is_read_only_viewer(self) -> bool:
        return (
            not self.is_ignite_admin
            and self.workspace_role == WorkspaceMembership.ROLE_VIEWER
        )

    @property
    def is_provider_scoped(self) -> bool:
        return (
            not self.is_ignite_admin
            and self.workspace_role in WorkspaceMembership.PROVIDER_ROLES
        )

    @property
    def can_view_brand_consolidated(self) -> bool:
        """Consolidated multi-provider reporting, costs, POS, and insights."""
        return self.is_brand_member and not self.is_provider_scoped

    @property
    def can_edit_brand_ops(self) -> bool:
        if self.is_ignite_admin or self.is_legacy_managed_member:
            return True
        return self.workspace_role in {
            WorkspaceMembership.ROLE_BRAND_ADMIN,
            WorkspaceMembership.ROLE_BRAND_REVIEWER,
        }

    @property
    def can_edit_finance(self) -> bool:
        if self.is_ignite_admin:
            return True
        if self.is_provider_scoped or self.is_read_only_viewer:
            return False
        return (
            self.workspace_role
            in {
                WorkspaceMembership.ROLE_BRAND_ADMIN,
                WorkspaceMembership.ROLE_FINANCE_REVIEWER,
            }
            or self.can_approve_finance
        )

    @property
    def can_manage_pos(self) -> bool:
        if self.is_ignite_admin:
            return True
        return self.workspace_role in {
            WorkspaceMembership.ROLE_BRAND_ADMIN,
            WorkspaceMembership.ROLE_BRAND_REVIEWER,
            WorkspaceMembership.ROLE_FINANCE_REVIEWER,
        }

    def can_import_for_provider(self, provider_id: int) -> bool:
        if self.is_ignite_admin:
            return True
        if self.workspace_role in {
            WorkspaceMembership.ROLE_BRAND_ADMIN,
            WorkspaceMembership.ROLE_BRAND_REVIEWER,
        }:
            return True
        if (
            self.workspace_role == WorkspaceMembership.ROLE_PROVIDER_MANAGER
            and self.provider is not None
            and self.provider.id == provider_id
        ):
            return ProviderAssignment.objects.filter(
                tenant=self.tenant,
                provider=self.provider,
            ).exclude(status=ProviderAssignment.STATUS_REVOKED).exists() or True
        return False

    def allowed_canonical_visits_q(self) -> Q:
        """Row-level filter for CanonicalVisit queries."""
        if self.can_view_brand_consolidated:
            return Q(tenant=self.tenant)
        if not self.provider:
            return Q(pk__in=[])
        active_assignments = ProviderAssignment.objects.filter(
            tenant=self.tenant,
            provider=self.provider,
        ).exclude(status=ProviderAssignment.STATUS_REVOKED)
        visit_ids = list(
            active_assignments.exclude(canonical_visit_id=None).values_list(
                "canonical_visit_id", flat=True
            )
        )
        event_ids = list(
            active_assignments.exclude(event_id=None).values_list(
                "event_id", flat=True
            )
        )
        program_ids = list(
            active_assignments.filter(
                scope_type=ProviderAssignment.SCOPE_PROGRAM
            )
            .exclude(event_type_id=None)
            .values_list("event_type_id", flat=True)
        )
        submitted_visit_ids = list(
            VisitSubmissionSource.objects.filter(
                tenant=self.tenant, provider=self.provider
            ).values_list("canonical_visit_id", flat=True)
        )
        cond = Q(pk__in=visit_ids + submitted_visit_ids)
        if event_ids:
            cond |= Q(event_id__in=event_ids)
        if program_ids:
            cond |= Q(
                event_type_id__in=program_ids,
                primary_provider=self.provider,
            )
        return Q(tenant=self.tenant) & cond

    def allowed_events_q(self) -> Q:
        """Row-level filter for legacy events.Event / CustomRecap queries when
        accessed by a provider-scoped membership."""
        if self.can_view_brand_consolidated:
            return Q(tenant=self.tenant)
        if not self.provider:
            return Q(pk__in=[])
        active_assignments = ProviderAssignment.objects.filter(
            tenant=self.tenant,
            provider=self.provider,
        ).exclude(status=ProviderAssignment.STATUS_REVOKED)
        direct_event_ids = list(
            active_assignments.exclude(event_id=None).values_list(
                "event_id", flat=True
            )
        )
        direct_req_ids = list(
            active_assignments.exclude(request_id=None).values_list(
                "request_id", flat=True
            )
        )
        visit_event_ids = list(
            CanonicalVisit.objects.filter(self.allowed_canonical_visits_q())
            .exclude(event_id=None)
            .values_list("event_id", flat=True)
        )
        all_event_ids = set(direct_event_ids) | set(visit_event_ids)
        cond = Q(pk__in=all_event_ids)
        if direct_req_ids:
            cond |= Q(request_id__in=direct_req_ids)
        return Q(tenant=self.tenant) & cond


def _parse_uuid_str(raw: str | None) -> str | None:
    if not raw:
        return None
    cleaned = raw.strip()
    if not cleaned:
        return None
    try:
        return str(UUID(cleaned))
    except (ValueError, TypeError):
        return None


def resolve_tenant_from_request(user: Any, request: Any = None) -> Tenant | None:
    """Resolve target brand Tenant from X-Spark-Tenant header, active workspace
    context, or fallback membership."""
    meta = getattr(request, "META", {}) or {}
    header_uuid = _parse_uuid_str(meta.get(SELECTED_TENANT_HEADER))
    if header_uuid:
        return Tenant.objects.filter(uuid=header_uuid).first()

    if user and getattr(user, "is_authenticated", False):
        ctx = (
            UserActiveWorkspaceContext.objects.select_related(
                "tenant", "membership", "membership__tenant"
            )
            .filter(user=user)
            .first()
        )
        if ctx:
            if (
                ctx.membership
                and ctx.membership.status == WorkspaceMembership.STATUS_ACTIVE
            ):
                return ctx.membership.tenant
            if ctx.tenant:
                return ctx.tenant

        first_ws = (
            WorkspaceMembership.objects.select_related("tenant")
            .filter(user=user, status=WorkspaceMembership.STATUS_ACTIVE)
            .order_by("id")
            .first()
        )
        if first_ws:
            return first_ws.tenant

        tu = (
            TenantedUser.objects.select_related("tenant")
            .filter(user=user, is_active=True)
            .order_by("id")
            .first()
        )
        if tu:
            return tu.tenant

    return None


def resolve_workspace_access(
    user: Any,
    tenant: Tenant | None = None,
    request: Any = None,
) -> ResolvedWorkspaceAccess:
    """Resolve and validate the caller's active workspace membership on tenant.
    Raises PermissionError if unauthenticated, unauthorized, or revoked."""
    if not user or not getattr(user, "is_authenticated", False):
        raise PermissionError("Authentication required.")
    if not getattr(user, "is_active", True):
        raise PermissionError("User account is inactive.")

    resolved_tenant = tenant or resolve_tenant_from_request(user, request)
    if resolved_tenant is None:
        raise PermissionError("No active brand workspace selected.")

    if is_ignite_staff_user(user):
        return ResolvedWorkspaceAccess(
            user=user,
            tenant=resolved_tenant,
            membership=None,
            workspace_role=WorkspaceMembership.ROLE_BRAND_ADMIN,
            provider=None,
            is_ignite_admin=True,
            is_legacy_managed_member=False,
            can_approve_recaps=True,
            can_approve_finance=True,
            can_approve_spend=True,
        )

    meta = getattr(request, "META", {}) or {}
    header_membership_uuid = _parse_uuid_str(meta.get(SELECTED_MEMBERSHIP_HEADER))

    chosen_membership: WorkspaceMembership | None = None
    if header_membership_uuid:
        candidate = (
            WorkspaceMembership.objects.select_related("provider", "tenant")
            .filter(uuid=header_membership_uuid, user=user)
            .first()
        )
        if candidate is None or candidate.tenant_id != resolved_tenant.id:
            raise PermissionError(
                "Selected workspace membership does not belong to this brand."
            )
        if candidate.status != WorkspaceMembership.STATUS_ACTIVE:
            raise PermissionError("Workspace membership has been revoked.")
        if candidate.provider and not candidate.provider.is_active:
            raise PermissionError("Provider organization is inactive.")
        chosen_membership = candidate

    if chosen_membership is None:
        ctx = (
            UserActiveWorkspaceContext.objects.select_related(
                "membership", "membership__provider"
            )
            .filter(user=user)
            .first()
        )
        if (
            ctx
            and ctx.membership
            and ctx.membership.tenant_id == resolved_tenant.id
            and ctx.membership.status == WorkspaceMembership.STATUS_ACTIVE
            and (ctx.membership.provider is None or ctx.membership.provider.is_active)
        ):
            chosen_membership = ctx.membership

    if chosen_membership is None:
        active_memberships = list(
            WorkspaceMembership.objects.select_related("provider")
            .filter(
                tenant=resolved_tenant,
                user=user,
                status=WorkspaceMembership.STATUS_ACTIVE,
            )
            .order_by("id")
        )
        active_memberships = [
            m for m in active_memberships if m.provider is None or m.provider.is_active
        ]
        if active_memberships:
            # Prefer internal brand role over provider role if user has both and
            # hasn't explicitly switched to a provider membership.
            brand_first = [
                m
                for m in active_memberships
                if m.workspace_role in WorkspaceMembership.BRAND_ROLES
            ]
            chosen_membership = brand_first[0] if brand_first else active_memberships[0]

    if chosen_membership is not None:
        is_admin_role = (
            chosen_membership.workspace_role == WorkspaceMembership.ROLE_BRAND_ADMIN
        )
        return ResolvedWorkspaceAccess(
            user=user,
            tenant=resolved_tenant,
            membership=chosen_membership,
            workspace_role=chosen_membership.workspace_role,
            provider=chosen_membership.provider,
            is_ignite_admin=False,
            is_legacy_managed_member=False,
            can_approve_recaps=bool(
                is_admin_role
                or chosen_membership.can_approve_recaps
                or chosen_membership.workspace_role
                == WorkspaceMembership.ROLE_BRAND_REVIEWER
            ),
            can_approve_finance=bool(
                is_admin_role
                or chosen_membership.can_approve_finance
                or chosen_membership.workspace_role
                == WorkspaceMembership.ROLE_FINANCE_REVIEWER
            ),
            can_approve_spend=bool(
                is_admin_role or chosen_membership.can_approve_spend
            ),
        )

    # Check if user had workspace memberships on this tenant that were revoked.
    if WorkspaceMembership.objects.filter(
        tenant=resolved_tenant,
        user=user,
        status=WorkspaceMembership.STATUS_REVOKED,
    ).exists():
        raise PermissionError("Workspace access has been revoked.")

    # Legacy Ignite-managed client compatibility (TenantedUser without
    # WorkspaceMembership rows yet).
    role = getattr(user, "role", None)
    if (
        role
        and role.slug == Role.CLIENT_SLUG
        and TenantedUser.objects.filter(
            tenant=resolved_tenant, user=user, is_active=True
        ).exists()
    ):
        return ResolvedWorkspaceAccess(
            user=user,
            tenant=resolved_tenant,
            membership=None,
            workspace_role=WorkspaceMembership.ROLE_BRAND_REVIEWER,
            provider=None,
            is_ignite_admin=False,
            is_legacy_managed_member=True,
            can_approve_recaps=True,
            can_approve_finance=False,
            can_approve_spend=True,
        )

    raise PermissionError("You do not have access to this brand workspace.")


def has_brand_consolidated_access(
    user: Any, tenant: Tenant, request: Any = None
) -> bool:
    """Non-throwing helper for legacy resolvers / MCP / chat tools."""
    try:
        access = resolve_workspace_access(user, tenant=tenant, request=request)
    except PermissionError:
        return False
    return access.can_view_brand_consolidated


def filter_events_for_workspace_member(
    qs, user: Any, tenant: Tenant, request: Any = None
):
    """Apply workspace revocation and provider-assignment scoping to an Event
    queryset while keeping legacy managed client users working."""
    try:
        access = resolve_workspace_access(user, tenant=tenant, request=request)
    except PermissionError:
        return qs.none()
    if access.can_view_brand_consolidated:
        return qs
    return qs.filter(access.allowed_events_q())


def log_workspace_audit(
    *,
    tenant: Tenant,
    actor: Any,
    category: str,
    action: str,
    summary: str,
    target_type: str = "",
    target_id: str = "",
    metadata: dict[str, Any] | None = None,
) -> WorkspaceAuditLog:
    actor_user = (
        actor if actor and getattr(actor, "is_authenticated", False) else None
    )
    return WorkspaceAuditLog.objects.create(
        tenant=tenant,
        actor=actor_user,
        category=category,
        action=action,
        target_type=target_type,
        target_id=str(target_id or ""),
        summary=summary[:500],
        metadata=metadata or {},
    )
