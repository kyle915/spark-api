"""Feature 1 service layer: Brand-owned workspaces, internal & agency
memberships, invitations, multi-membership context switching, scoped provider
assignments, mobile submission codes, and immediate revocation."""

from __future__ import annotations

import secrets
from datetime import timedelta
from typing import Any

from django.db import transaction
from django.utils import timezone
from django.utils.text import slugify

from brand_insights.facts import store_identity
from events.models import Event, EventType, Request
from pilot.entitlements import (
    require_new_visit_capacity,
    require_provider_feed_capacity,
)
from pilot.models import (
    CanonicalVisit,
    ProviderAssignment,
    ProviderOrganization,
    ProviderSourceInclusionRule,
    UserActiveWorkspaceContext,
    VisitSubmissionSource,
    WorkspaceInvitation,
    WorkspaceMembership,
    WorkspaceProfile,
    WorkspaceSharedLink,
)
from pilot.permissions import (
    ResolvedWorkspaceAccess,
    is_ignite_staff_user,
    log_workspace_audit,
)
from tenants.models import Role, Tenant, TenantedUser, User

_CODE_ALPHABET = "23456789ABCDEFGHJKLMNPQRSTUVWXYZ"


def _mint_submission_code() -> str:
    for _ in range(40):
        suffix = "".join(secrets.choice(_CODE_ALPHABET) for _ in range(6))
        candidate = f"PA-{suffix}"
        if not ProviderAssignment.objects.filter(
            submission_code__iexact=candidate
        ).exists():
            return candidate
    return f"PA-{secrets.token_hex(4).upper()}"


def ensure_workspace_profile(
    tenant: Tenant,
    *,
    mode: str | None = None,
    plan_tier: str | None = None,
) -> WorkspaceProfile:
    profile, _ = WorkspaceProfile.objects.get_or_create(
        tenant=tenant,
        defaults={
            "mode": mode or WorkspaceProfile.MODE_MANAGED,
            "plan_tier": plan_tier or WorkspaceProfile.PLAN_MANAGED_SOW,
        },
    )
    dirty: list[str] = []
    if mode and profile.mode != mode:
        profile.mode = mode
        dirty.append("mode")
    if plan_tier and profile.plan_tier != plan_tier:
        profile.plan_tier = plan_tier
        dirty.append("plan_tier")
    if dirty:
        dirty.append("updated_at")
        profile.save(update_fields=dirty)
    return profile


def _validate_role_and_provider(
    workspace_role: str, provider: ProviderOrganization | None
) -> None:
    valid_roles = {c[0] for c in WorkspaceMembership.ROLE_CHOICES}
    if workspace_role not in valid_roles:
        raise ValueError(f"Unsupported workspace role: {workspace_role}")
    if workspace_role in WorkspaceMembership.PROVIDER_ROLES and provider is None:
        raise ValueError(
            "Provider roles require an associated ProviderOrganization."
        )
    if workspace_role in WorkspaceMembership.BRAND_ROLES and provider is not None:
        raise ValueError(
            "Internal brand roles cannot be scoped to an external provider."
        )


def _sync_tenanted_user_for_brand(tenant: Tenant, user: User) -> None:
    """Ensure TenantedUser reflects whether the user has an active INTERNAL
    brand membership on this tenant. External provider roles NEVER receive an
    active TenantedUser row so legacy consolidated tenant queries stay sealed."""
    has_active_brand_role = WorkspaceMembership.objects.filter(
        tenant=tenant,
        user=user,
        status=WorkspaceMembership.STATUS_ACTIVE,
        workspace_role__in=WorkspaceMembership.BRAND_ROLES,
    ).exists()
    tu = TenantedUser.objects.filter(tenant=tenant, user=user).first()
    if has_active_brand_role:
        if tu is None:
            TenantedUser.objects.create(
                tenant=tenant, user=user, is_active=True
            )
        elif not tu.is_active:
            tu.is_active = True
            tu.save(update_fields=["is_active"])
    else:
        if tu is not None and tu.is_active:
            tu.is_active = False
            tu.save(update_fields=["is_active"])


@transaction.atomic
def create_or_update_provider(
    access: ResolvedWorkspaceAccess,
    *,
    name: str,
    contact_email: str = "",
    slug: str = "",
    is_ignite_managed: bool = False,
) -> ProviderOrganization:
    if not access.is_brand_admin:
        raise PermissionError(
            "Only brand administrators can register field providers."
        )
    clean_name = (name or "").strip()
    if not clean_name:
        raise ValueError("Provider name is required.")

    base_slug = slugify(slug or clean_name)[:70] or "provider"
    provider = ProviderOrganization.objects.filter(slug=base_slug).first()
    require_provider_feed_capacity(
        access.tenant, candidate_provider=provider
    )
    if provider is None:
        candidate = base_slug
        idx = 2
        while ProviderOrganization.objects.filter(slug=candidate).exists():
            candidate = f"{base_slug}-{idx}"
            idx += 1
        provider = ProviderOrganization.objects.create(
            name=clean_name,
            slug=candidate,
            contact_email=(contact_email or "").strip(),
            is_ignite_managed=is_ignite_managed,
            is_active=True,
            created_by=access.user,
        )
    else:
        # ProviderOrganization rows are shared across brand workspaces. A brand
        # administrator may link an existing active provider to their workspace
        # without mutating its shared identity or reactivating a globally
        # disabled provider.
        if not provider.is_active and not access.is_ignite_admin:
            raise PermissionError(
                "This provider organization is inactive and cannot be modified by a brand workspace."
            )
        shared_with_other_tenants = (
            ProviderSourceInclusionRule.objects.filter(provider=provider)
            .exclude(tenant=access.tenant)
            .exists()
            or WorkspaceMembership.objects.filter(provider=provider)
            .exclude(tenant=access.tenant)
            .exists()
            or ProviderAssignment.objects.filter(provider=provider)
            .exclude(tenant=access.tenant)
            .exists()
        )
        if access.is_ignite_admin and not shared_with_other_tenants:
            provider.name = clean_name
            if contact_email:
                provider.contact_email = contact_email.strip()
            provider.is_active = True
            provider.save(
                update_fields=["name", "contact_email", "is_active", "updated_at"]
            )

    ProviderSourceInclusionRule.objects.get_or_create(
        tenant=access.tenant,
        provider=provider,
        defaults={
            "include_in_combined_reporting": True,
            "updated_by": access.user,
        },
    )
    log_workspace_audit(
        tenant=access.tenant,
        actor=access.user,
        category="provider",
        action="provider_registered",
        target_type="ProviderOrganization",
        target_id=str(provider.uuid),
        summary=f"Registered provider {provider.name} in workspace.",
        metadata={"provider_slug": provider.slug},
    )
    return provider


@transaction.atomic
def upsert_workspace_membership(
    access: ResolvedWorkspaceAccess,
    *,
    target_user: User,
    workspace_role: str,
    provider: ProviderOrganization | None = None,
    can_approve_recaps: bool = False,
    can_approve_finance: bool = False,
    can_approve_spend: bool = False,
) -> WorkspaceMembership:
    if not access.is_brand_admin:
        raise PermissionError(
            "Only brand administrators can manage workspace memberships."
        )
    _validate_role_and_provider(workspace_role, provider)
    ensure_workspace_profile(access.tenant)

    if workspace_role in WorkspaceMembership.PROVIDER_ROLES:
        can_approve_recaps = False
        can_approve_finance = False
        can_approve_spend = False
    elif workspace_role == WorkspaceMembership.ROLE_VIEWER:
        can_approve_recaps = False
        can_approve_finance = False
        can_approve_spend = False
    elif workspace_role == WorkspaceMembership.ROLE_BRAND_ADMIN:
        can_approve_recaps = True
        can_approve_finance = True
        can_approve_spend = True

    if getattr(target_user, "role_id", None) is None:
        client_role, _ = Role.objects.get_or_create(
            slug=Role.CLIENT_SLUG, defaults={"name": "Client"}
        )
        target_user.role = client_role
        target_user.save(update_fields=["role"])

    scope = f"provider:{provider.id}" if provider else "brand"
    membership, created = WorkspaceMembership.objects.update_or_create(
        tenant=access.tenant,
        user=target_user,
        membership_scope=scope,
        defaults={
            "provider": provider,
            "workspace_role": workspace_role,
            "can_approve_recaps": can_approve_recaps,
            "can_approve_finance": can_approve_finance,
            "can_approve_spend": can_approve_spend,
            "status": WorkspaceMembership.STATUS_ACTIVE,
            "revoked_at": None,
            "revoked_by": None,
            "created_by": access.user,
        },
    )
    if provider is not None:
        ProviderSourceInclusionRule.objects.get_or_create(
            tenant=access.tenant,
            provider=provider,
            defaults={
                "include_in_combined_reporting": True,
                "updated_by": access.user,
            },
        )
    _sync_tenanted_user_for_brand(access.tenant, target_user)

    log_workspace_audit(
        tenant=access.tenant,
        actor=access.user,
        category="membership",
        action="membership_created" if created else "membership_updated",
        target_type="WorkspaceMembership",
        target_id=str(membership.uuid),
        summary=(
            f"Granted {workspace_role} access to {target_user.email}"
            + (f" ({provider.name})" if provider else "")
        ),
        metadata={
            "email": target_user.email,
            "workspace_role": workspace_role,
            "provider_uuid": str(provider.uuid) if provider else None,
            "can_approve_recaps": membership.can_approve_recaps,
            "can_approve_finance": membership.can_approve_finance,
            "can_approve_spend": membership.can_approve_spend,
        },
    )
    return membership


@transaction.atomic
def invite_workspace_member(
    access: ResolvedWorkspaceAccess,
    *,
    email: str,
    workspace_role: str,
    provider: ProviderOrganization | None = None,
    can_approve_recaps: bool = False,
    can_approve_finance: bool = False,
    can_approve_spend: bool = False,
) -> WorkspaceInvitation:
    if not access.is_brand_admin:
        raise PermissionError(
            "Only brand administrators can invite users or agencies."
        )
    clean_email = (email or "").strip().lower()
    if not clean_email or "@" not in clean_email:
        raise ValueError("A valid email address is required.")
    _validate_role_and_provider(workspace_role, provider)
    ensure_workspace_profile(access.tenant)

    if workspace_role in WorkspaceMembership.PROVIDER_ROLES or (
        workspace_role == WorkspaceMembership.ROLE_VIEWER
    ):
        can_approve_recaps = False
        can_approve_finance = False
        can_approve_spend = False
    elif workspace_role == WorkspaceMembership.ROLE_BRAND_ADMIN:
        can_approve_recaps = True
        can_approve_finance = True
        can_approve_spend = True

    token = secrets.token_urlsafe(32)
    invitation = WorkspaceInvitation.objects.create(
        tenant=access.tenant,
        email=clean_email,
        workspace_role=workspace_role,
        provider=provider,
        can_approve_recaps=can_approve_recaps,
        can_approve_finance=can_approve_finance,
        can_approve_spend=can_approve_spend,
        token=token,
        status=WorkspaceInvitation.STATUS_PENDING,
        expires_at=timezone.now() + timedelta(days=14),
        invited_by=access.user,
    )

    # If the user already exists in Spark, provision their membership right away
    # as well so they can switch into the workspace immediately upon next login.
    existing_user = User.objects.filter(email__iexact=clean_email).first()
    if existing_user is not None:
        upsert_workspace_membership(
            access,
            target_user=existing_user,
            workspace_role=workspace_role,
            provider=provider,
            can_approve_recaps=can_approve_recaps,
            can_approve_finance=can_approve_finance,
            can_approve_spend=can_approve_spend,
        )
        invitation.status = WorkspaceInvitation.STATUS_ACCEPTED
        invitation.accepted_by = existing_user
        invitation.accepted_at = timezone.now()
        invitation.save(
            update_fields=["status", "accepted_by", "accepted_at", "updated_at"]
        )

    log_workspace_audit(
        tenant=access.tenant,
        actor=access.user,
        category="invitation",
        action="invitation_sent",
        target_type="WorkspaceInvitation",
        target_id=str(invitation.uuid),
        summary=(
            f"Invited {clean_email} as {workspace_role}"
            + (f" for {provider.name}" if provider else "")
        ),
        metadata={
            "email": clean_email,
            "workspace_role": workspace_role,
            "provider_uuid": str(provider.uuid) if provider else None,
            "status": invitation.status,
        },
    )
    return invitation


@transaction.atomic
def accept_workspace_invitation(
    *,
    user: User,
    token: str,
) -> WorkspaceMembership:
    invitation = (
        WorkspaceInvitation.objects.select_related("tenant", "provider")
        .filter(token=(token or "").strip())
        .first()
    )
    if invitation is None:
        raise ValueError("Invitation token is invalid.")
    if invitation.status == WorkspaceInvitation.STATUS_REVOKED:
        raise PermissionError("This workspace invitation was revoked.")
    if invitation.status == WorkspaceInvitation.STATUS_ACCEPTED:
        raise PermissionError("This workspace invitation has already been accepted.")
    if invitation.status != WorkspaceInvitation.STATUS_PENDING:
        raise PermissionError("This workspace invitation is no longer pending.")
    if invitation.expires_at < timezone.now():
        invitation.status = WorkspaceInvitation.STATUS_EXPIRED
        invitation.save(update_fields=["status", "updated_at"])
        raise PermissionError("This workspace invitation has expired.")
    if user.email.strip().lower() != invitation.email.strip().lower():
        raise PermissionError(
            "Signed-in email does not match the invited email address."
        )

    if getattr(user, "role_id", None) is None:
        client_role, _ = Role.objects.get_or_create(
            slug=Role.CLIENT_SLUG, defaults={"name": "Client"}
        )
        user.role = client_role
        user.save(update_fields=["role"])

    scope = (
        f"provider:{invitation.provider_id}"
        if invitation.provider_id
        else "brand"
    )
    membership, _ = WorkspaceMembership.objects.update_or_create(
        tenant=invitation.tenant,
        user=user,
        membership_scope=scope,
        defaults={
            "provider": invitation.provider,
            "workspace_role": invitation.workspace_role,
            "can_approve_recaps": invitation.can_approve_recaps,
            "can_approve_finance": invitation.can_approve_finance,
            "can_approve_spend": invitation.can_approve_spend,
            "status": WorkspaceMembership.STATUS_ACTIVE,
            "revoked_at": None,
            "revoked_by": None,
            "created_by": invitation.invited_by,
        },
    )
    invitation.status = WorkspaceInvitation.STATUS_ACCEPTED
    invitation.accepted_by = user
    invitation.accepted_at = timezone.now()
    invitation.save(
        update_fields=["status", "accepted_by", "accepted_at", "updated_at"]
    )
    _sync_tenanted_user_for_brand(invitation.tenant, user)
    UserActiveWorkspaceContext.objects.update_or_create(
        user=user,
        defaults={"tenant": invitation.tenant, "membership": membership},
    )
    log_workspace_audit(
        tenant=invitation.tenant,
        actor=user,
        category="invitation",
        action="invitation_accepted",
        target_type="WorkspaceMembership",
        target_id=str(membership.uuid),
        summary=f"{user.email} accepted {membership.workspace_role} invitation.",
    )
    return membership


@transaction.atomic
def revoke_workspace_membership(
    access: ResolvedWorkspaceAccess,
    *,
    membership_uuid: str,
) -> WorkspaceMembership:
    if not access.is_brand_admin:
        raise PermissionError(
            "Only brand administrators can revoke workspace memberships."
        )
    membership = (
        WorkspaceMembership.objects.select_related("user", "provider")
        .filter(tenant=access.tenant, uuid=membership_uuid)
        .first()
    )
    if membership is None:
        raise ValueError("Workspace membership not found.")

    membership.status = WorkspaceMembership.STATUS_REVOKED
    membership.revoked_at = timezone.now()
    membership.revoked_by = access.user
    membership.save(
        update_fields=["status", "revoked_at", "revoked_by", "updated_at"]
    )

    UserActiveWorkspaceContext.objects.filter(
        user=membership.user, membership=membership
    ).update(membership=None, tenant=None)

    WorkspaceSharedLink.objects.filter(
        tenant=access.tenant,
        created_by_membership=membership,
        revoked_at__isnull=True,
    ).update(revoked_at=timezone.now())

    _sync_tenanted_user_for_brand(access.tenant, membership.user)

    log_workspace_audit(
        tenant=access.tenant,
        actor=access.user,
        category="membership",
        action="membership_revoked",
        target_type="WorkspaceMembership",
        target_id=str(membership.uuid),
        summary=(
            f"Revoked {membership.workspace_role} access for {membership.user.email}"
            + (f" ({membership.provider.name})" if membership.provider else "")
        ),
        metadata={
            "email": membership.user.email,
            "workspace_role": membership.workspace_role,
            "provider_uuid": (
                str(membership.provider.uuid) if membership.provider else None
            ),
        },
    )
    return membership


@transaction.atomic
def revoke_provider_from_workspace(
    access: ResolvedWorkspaceAccess,
    *,
    provider_uuid: str,
) -> int:
    """Revoke all memberships, pending invitations, and assignments for an
    agency inside this brand workspace immediately."""
    if not access.is_brand_admin:
        raise PermissionError(
            "Only brand administrators can revoke agency access."
        )
    provider = ProviderOrganization.objects.filter(uuid=provider_uuid).first()
    if provider is None:
        raise ValueError("Provider organization not found.")

    now = timezone.now()
    affected_memberships = list(
        WorkspaceMembership.objects.select_related("user").filter(
            tenant=access.tenant,
            provider=provider,
            status=WorkspaceMembership.STATUS_ACTIVE,
        )
    )
    for m in affected_memberships:
        m.status = WorkspaceMembership.STATUS_REVOKED
        m.revoked_at = now
        m.revoked_by = access.user
        m.save(update_fields=["status", "revoked_at", "revoked_by", "updated_at"])
        UserActiveWorkspaceContext.objects.filter(
            user=m.user, membership=m
        ).update(membership=None, tenant=None)
        WorkspaceSharedLink.objects.filter(
            tenant=access.tenant,
            created_by_membership=m,
            revoked_at__isnull=True,
        ).update(revoked_at=now)
        _sync_tenanted_user_for_brand(access.tenant, m.user)

    WorkspaceInvitation.objects.filter(
        tenant=access.tenant,
        provider=provider,
        status=WorkspaceInvitation.STATUS_PENDING,
    ).update(status=WorkspaceInvitation.STATUS_REVOKED, revoked_at=now)

    revoked_assignments = ProviderAssignment.objects.filter(
        tenant=access.tenant,
        provider=provider,
    ).exclude(status=ProviderAssignment.STATUS_REVOKED).update(
        status=ProviderAssignment.STATUS_REVOKED,
        updated_at=now,
    )

    log_workspace_audit(
        tenant=access.tenant,
        actor=access.user,
        category="provider",
        action="provider_access_revoked",
        target_type="ProviderOrganization",
        target_id=str(provider.uuid),
        summary=(
            f"Revoked {provider.name} from workspace "
            f"({len(affected_memberships)} memberships, {revoked_assignments} assignments)."
        ),
    )
    return len(affected_memberships)


def list_user_workspace_contexts(user: User) -> list[dict[str, Any]]:
    """Return every active workspace context available to the authenticated user."""
    if not user or not getattr(user, "is_authenticated", False):
        return []

    active_ctx = (
        UserActiveWorkspaceContext.objects.select_related("membership", "tenant")
        .filter(user=user)
        .first()
    )
    active_membership_id = active_ctx.membership_id if active_ctx else None
    active_tenant_id = active_ctx.tenant_id if active_ctx else None

    contexts: list[dict[str, Any]] = []
    seen_brand_tenants: set[int] = set()

    memberships = (
        WorkspaceMembership.objects.select_related("tenant", "provider")
        .filter(user=user, status=WorkspaceMembership.STATUS_ACTIVE)
        .order_by("tenant__name", "id")
    )
    for m in memberships:
        if m.provider and not m.provider.is_active:
            continue
        seen_brand_tenants.add(m.tenant_id)
        is_current = (
            active_membership_id == m.id
            if active_membership_id
            else active_tenant_id == m.tenant_id
        )
        label = (
            f"{m.tenant.name} · {m.provider.name} ({m.get_workspace_role_display()})"
            if m.provider
            else f"{m.tenant.name} · {m.get_workspace_role_display()}"
        )
        contexts.append(
            {
                "membership_uuid": str(m.uuid),
                "tenant_uuid": str(m.tenant.uuid),
                "tenant_name": m.tenant.name,
                "tenant_slug": m.tenant.slug or "",
                "workspace_role": m.workspace_role,
                "provider_uuid": str(m.provider.uuid) if m.provider else None,
                "provider_name": m.provider.name if m.provider else None,
                "can_approve_recaps": m.can_approve_recaps
                or m.workspace_role
                in {
                    WorkspaceMembership.ROLE_BRAND_ADMIN,
                    WorkspaceMembership.ROLE_BRAND_REVIEWER,
                },
                "can_approve_finance": m.can_approve_finance
                or m.workspace_role
                in {
                    WorkspaceMembership.ROLE_BRAND_ADMIN,
                    WorkspaceMembership.ROLE_FINANCE_REVIEWER,
                },
                "can_approve_spend": m.can_approve_spend
                or m.workspace_role == WorkspaceMembership.ROLE_BRAND_ADMIN,
                "display_label": label,
                "is_current": bool(is_current),
            }
        )

    # Include legacy TenantedUser brand contexts that don't yet have a
    # WorkspaceMembership row (and haven't been revoked).
    revoked_tenant_ids = set(
        WorkspaceMembership.objects.filter(
            user=user, status=WorkspaceMembership.STATUS_REVOKED
        ).values_list("tenant_id", flat=True)
    )
    legacy_tus = (
        TenantedUser.objects.select_related("tenant")
        .filter(user=user, is_active=True)
        .exclude(tenant_id__in=seen_brand_tenants | revoked_tenant_ids)
        .order_by("tenant__name")
    )
    for tu in legacy_tus:
        contexts.append(
            {
                "membership_uuid": None,
                "tenant_uuid": str(tu.tenant.uuid),
                "tenant_name": tu.tenant.name,
                "tenant_slug": tu.tenant.slug or "",
                "workspace_role": (
                    WorkspaceMembership.ROLE_BRAND_ADMIN
                    if is_ignite_staff_user(user)
                    else WorkspaceMembership.ROLE_BRAND_REVIEWER
                ),
                "provider_uuid": None,
                "provider_name": None,
                "can_approve_recaps": True,
                "can_approve_finance": is_ignite_staff_user(user),
                "can_approve_spend": True,
                "display_label": f"{tu.tenant.name} · Managed Workspace",
                "is_current": bool(
                    active_membership_id is None
                    and active_tenant_id == tu.tenant_id
                ),
            }
        )
    return contexts


@transaction.atomic
def switch_workspace_context(
    *,
    user: User,
    membership_uuid: str | None = None,
    tenant_uuid: str | None = None,
) -> UserActiveWorkspaceContext:
    if not user or not getattr(user, "is_authenticated", False):
        raise PermissionError("Authentication required.")

    if membership_uuid:
        membership = (
            WorkspaceMembership.objects.select_related("tenant", "provider")
            .filter(uuid=membership_uuid, user=user)
            .first()
        )
        if membership is None:
            raise PermissionError("Workspace membership not found for user.")
        if membership.status != WorkspaceMembership.STATUS_ACTIVE:
            raise PermissionError("Workspace membership has been revoked.")
        if membership.provider and not membership.provider.is_active:
            raise PermissionError("Provider organization is inactive.")
        ctx, _ = UserActiveWorkspaceContext.objects.update_or_create(
            user=user,
            defaults={"tenant": membership.tenant, "membership": membership},
        )
        return ctx

    if tenant_uuid:
        tenant = Tenant.objects.filter(uuid=tenant_uuid).first()
        if tenant is None:
            raise ValueError("Brand workspace not found.")
        if is_ignite_staff_user(user):
            ctx, _ = UserActiveWorkspaceContext.objects.update_or_create(
                user=user,
                defaults={"tenant": tenant, "membership": None},
            )
            return ctx
        active_m = (
            WorkspaceMembership.objects.filter(
                user=user,
                tenant=tenant,
                status=WorkspaceMembership.STATUS_ACTIVE,
            )
            .order_by("id")
            .first()
        )
        if active_m is not None:
            ctx, _ = UserActiveWorkspaceContext.objects.update_or_create(
                user=user,
                defaults={"tenant": tenant, "membership": active_m},
            )
            return ctx
        if WorkspaceMembership.objects.filter(
            user=user, tenant=tenant, status=WorkspaceMembership.STATUS_REVOKED
        ).exists():
            raise PermissionError("Workspace access has been revoked.")
        if TenantedUser.objects.filter(
            user=user, tenant=tenant, is_active=True
        ).exists():
            ctx, _ = UserActiveWorkspaceContext.objects.update_or_create(
                user=user,
                defaults={"tenant": tenant, "membership": None},
            )
            return ctx
        raise PermissionError("You do not have access to this brand workspace.")

    raise ValueError("Provide membership_uuid or tenant_uuid to switch context.")


def _event_city_state(event: Event) -> tuple[str, str]:
    city = event.location.name if getattr(event, "location_id", None) and event.location else ""
    state_code = ""
    if getattr(event, "state_id", None) and event.state:
        state_code = event.state.code or ""
    elif getattr(event, "location_id", None) and event.location and event.location.state:
        state_code = event.location.state.code or ""
    return city, state_code


def ensure_canonical_visit_for_event(
    event: Event,
    *,
    primary_provider: ProviderOrganization | None = None,
) -> CanonicalVisit:
    """Create or update the CanonicalVisit backing an events.Event."""
    tenant = event.tenant
    existing = CanonicalVisit.objects.filter(tenant=tenant, event=event).first()

    retailer_name = event.retailer.name if event.retailer else ""
    ev_city, ev_state = _event_city_state(event)
    s_key, s_label, c_key, c_label = store_identity(
        retailer_name,
        event.address or "",
        ev_city,
        ev_state,
    )
    visit_date = (
        timezone.localtime(event.start_time).date()
        if event.start_time
        else timezone.now().date()
    )

    if existing is not None:
        dirty: list[str] = []
        if primary_provider and existing.primary_provider_id is None:
            existing.primary_provider = primary_provider
            dirty.append("primary_provider")
        if s_key and existing.store_key != s_key:
            existing.store_key = s_key
            existing.store_label = s_label
            existing.chain_key = c_key
            existing.chain_label = c_label
            dirty.extend(["store_key", "store_label", "chain_key", "chain_label"])
        if dirty:
            dirty.append("updated_at")
            existing.save(update_fields=dirty)
        return existing

    return CanonicalVisit.objects.create(
        tenant=tenant,
        event=event,
        request=event.request,
        primary_provider=primary_provider,
        retailer=event.retailer,
        event_type=event.event_type,
        store_key=s_key,
        store_label=s_label,
        chain_key=c_key,
        chain_label=c_label,
        address=event.address or "",
        city=ev_city,
        state_code=ev_state,
        market=ev_city or ev_state or "",
        visit_date=visit_date,
        start_time=event.start_time,
        end_time=event.end_time,
        shift_key=f"event:{event.id}",
        status=CanonicalVisit.STATUS_SCHEDULED,
    )


@transaction.atomic
def assign_provider_work(
    access: ResolvedWorkspaceAccess,
    *,
    provider: ProviderOrganization,
    scope_type: str,
    event_type: EventType | None = None,
    event: Event | None = None,
    request_obj: Request | None = None,
    canonical_visit: CanonicalVisit | None = None,
    due_date=None,
    notes: str = "",
    assigned_owner: User | None = None,
) -> ProviderAssignment:
    if not access.can_edit_brand_ops:
        raise PermissionError(
            "Only brand administrators or reviewers can assign providers."
        )
    if scope_type not in {
        ProviderAssignment.SCOPE_PROGRAM,
        ProviderAssignment.SCOPE_VISIT,
    }:
        raise ValueError(f"Unsupported assignment scope: {scope_type}")

    if scope_type == ProviderAssignment.SCOPE_PROGRAM and event_type is None:
        raise ValueError("Program assignments require an EventType.")
    if event_type is not None and event_type.tenant_id not in (
        None,
        access.tenant.id,
    ):
        raise ValueError("Program does not belong to this brand workspace.")

    if event is not None:
        if event.tenant_id != access.tenant.id:
            raise ValueError("Event does not belong to this brand workspace.")
        if canonical_visit is None:
            canonical_visit = ensure_canonical_visit_for_event(
                event, primary_provider=provider
            )
        if request_obj is None:
            request_obj = event.request
        if event_type is None:
            event_type = event.event_type

    if canonical_visit is not None and canonical_visit.tenant_id != access.tenant.id:
        raise ValueError("Visit does not belong to this brand workspace.")

    if scope_type == ProviderAssignment.SCOPE_VISIT and (
        canonical_visit is None and event is None and request_obj is None
    ):
        raise ValueError(
            "Visit assignments require a target visit, event, or request."
        )

    require_provider_feed_capacity(
        access.tenant, candidate_provider=provider
    )
    if (
        scope_type == ProviderAssignment.SCOPE_VISIT
        and canonical_visit is not None
        and canonical_visit.status == CanonicalVisit.STATUS_SCHEDULED
        and not ProviderAssignment.objects.filter(
            tenant=access.tenant, canonical_visit=canonical_visit
        ).exists()
    ):
        require_new_visit_capacity(
            access.tenant, is_ignite_admin=access.is_ignite_admin
        )

    ProviderSourceInclusionRule.objects.get_or_create(
        tenant=access.tenant,
        provider=provider,
        defaults={
            "include_in_combined_reporting": True,
            "updated_by": access.user,
        },
    )

    assignment = ProviderAssignment.objects.create(
        tenant=access.tenant,
        provider=provider,
        scope_type=scope_type,
        event_type=event_type,
        request=request_obj,
        event=event,
        canonical_visit=canonical_visit,
        assigned_owner=assigned_owner,
        status=ProviderAssignment.STATUS_ASSIGNED,
        submission_code=_mint_submission_code(),
        due_date=due_date,
        notes=(notes or "").strip(),
        created_by=access.user,
    )
    log_workspace_audit(
        tenant=access.tenant,
        actor=access.user,
        category="assignment",
        action="provider_assigned",
        target_type="ProviderAssignment",
        target_id=str(assignment.uuid),
        summary=f"Assigned {scope_type} to {provider.name} ({assignment.submission_code}).",
        metadata={
            "provider_uuid": str(provider.uuid),
            "scope_type": scope_type,
            "submission_code": assignment.submission_code,
        },
    )
    return assignment


@transaction.atomic
def respond_provider_assignment(
    access: ResolvedWorkspaceAccess,
    *,
    assignment_uuid: str,
    decision: str,
    response_note: str = "",
) -> ProviderAssignment:
    assignment = (
        ProviderAssignment.objects.select_related("provider")
        .filter(tenant=access.tenant, uuid=assignment_uuid)
        .first()
    )
    if assignment is None:
        raise ValueError("Provider assignment not found.")
    if assignment.status == ProviderAssignment.STATUS_REVOKED:
        raise PermissionError("This assignment has been revoked.")

    can_respond = access.is_brand_admin or (
        access.workspace_role == WorkspaceMembership.ROLE_PROVIDER_MANAGER
        and access.provider is not None
        and access.provider.id == assignment.provider_id
    )
    if not can_respond:
        raise PermissionError(
            "Only the assigned provider manager or brand admin can respond."
        )

    if decision not in {
        ProviderAssignment.STATUS_ACCEPTED,
        ProviderAssignment.STATUS_DECLINED,
    }:
        raise ValueError("Decision must be 'accepted' or 'declined'.")

    assignment.status = decision
    assignment.response_note = (response_note or "").strip()
    assignment.responded_by = access.user
    assignment.responded_at = timezone.now()
    assignment.save(
        update_fields=[
            "status",
            "response_note",
            "responded_by",
            "responded_at",
            "updated_at",
        ]
    )
    log_workspace_audit(
        tenant=access.tenant,
        actor=access.user,
        category="assignment",
        action=f"assignment_{decision}",
        target_type="ProviderAssignment",
        target_id=str(assignment.uuid),
        summary=f"{assignment.provider.name} {decision} assignment {assignment.submission_code}.",
    )
    return assignment


@transaction.atomic
def revoke_provider_assignment(
    access: ResolvedWorkspaceAccess,
    *,
    assignment_uuid: str,
) -> ProviderAssignment:
    if not access.can_edit_brand_ops:
        raise PermissionError(
            "Only brand administrators or reviewers can revoke assignments."
        )
    assignment = (
        ProviderAssignment.objects.select_related("provider")
        .filter(tenant=access.tenant, uuid=assignment_uuid)
        .first()
    )
    if assignment is None:
        raise ValueError("Provider assignment not found.")
    assignment.status = ProviderAssignment.STATUS_REVOKED
    assignment.save(update_fields=["status", "updated_at"])
    log_workspace_audit(
        tenant=access.tenant,
        actor=access.user,
        category="assignment",
        action="assignment_revoked",
        target_type="ProviderAssignment",
        target_id=str(assignment.uuid),
        summary=f"Revoked assignment {assignment.submission_code} from {assignment.provider.name}.",
    )
    return assignment


def ensure_event_for_canonical_visit(
    visit: CanonicalVisit,
    *,
    event_type: EventType | None = None,
    actor: User | None = None,
) -> Event:
    """Materialize and attach a visit-scoped `events.Event` when a
    `CanonicalVisit` has no legacy `Event` yet so `/checkin/PA-*` codes on
    visit-scoped assignments stay locked to that single visit."""
    if visit.event_id is not None and visit.event is not None:
        return visit.event

    from datetime import datetime, time
    from datetime import timezone as dt_timezone

    resolved_actor = (
        actor
        or getattr(visit.tenant, "created_by", None)
        or WorkspaceMembership.objects.filter(
            tenant=visit.tenant, status=WorkspaceMembership.STATUS_ACTIVE
        )
        .select_related("user")
        .first()
    )
    if isinstance(resolved_actor, WorkspaceMembership):
        resolved_actor = resolved_actor.user
    if resolved_actor is None:
        resolved_actor = User.objects.order_by("id").first()
    if resolved_actor is None:
        raise ValueError("Cannot materialize Event without a creator user.")

    day_dt = visit.start_time or datetime.combine(
        visit.visit_date, time(12, 0), tzinfo=dt_timezone.utc
    )
    resolved_type = event_type or visit.event_type
    title = (
        visit.store_label
        or visit.address
        or f"Assigned Visit {visit.visit_date.isoformat()}"
    )[:255]
    event = Event.objects.create(
        tenant=visit.tenant,
        request=visit.request,
        retailer=visit.retailer,
        event_type=resolved_type,
        name=title,
        address=visit.address or "",
        date=day_dt,
        start_time=visit.start_time,
        end_time=visit.end_time,
        track_mileage=bool(getattr(visit.tenant, "default_track_mileage", False)),
        mileage_rate=getattr(visit.tenant, "default_mileage_rate", None),
        created_by=resolved_actor,
        updated_by=resolved_actor,
    )
    visit.event = event
    if resolved_type and visit.event_type_id is None:
        visit.event_type = resolved_type
        visit.save(update_fields=["event", "event_type", "updated_at"])
    else:
        visit.save(update_fields=["event", "updated_at"])
    return event


def resolve_checkin_target_for_assignment(
    code: str | None,
) -> tuple[str | None, Any]:
    """Resolve a `PA-XXXXXX` code for `/checkin/<code>`.
    Visit-scoped assignments bind strictly to their assigned visit's `Event`
    (materializing one on `canonical_visit` when needed) and NEVER fall back
    to tenant-wide arbitrary store/date selection."""
    assignment = resolve_assignment_by_submission_code(code)
    if assignment is None:
        return None, None
    if (
        assignment.scope_type == ProviderAssignment.SCOPE_VISIT
        or assignment.canonical_visit_id is not None
    ):
        if assignment.event is not None:
            return "event", assignment.event
        if assignment.canonical_visit is not None:
            event = ensure_event_for_canonical_visit(
                assignment.canonical_visit,
                event_type=assignment.event_type,
                actor=assignment.created_by,
            )
            if assignment.event_id != event.id:
                assignment.event = event
                assignment.save(update_fields=["event", "updated_at"])
            return "event", event
        return None, None
    return "tenant", assignment.tenant


def resolve_assignment_by_submission_code(
    code: str | None,
) -> ProviderAssignment | None:
    """Resolve a provider mobile submission code (`PA-XXXXXX`). Returns None
    immediately if the assignment or provider was revoked/declined."""
    clean = (code or "").strip()
    if not clean or not clean.upper().startswith("PA-"):
        return None
    assignment = (
        ProviderAssignment.objects.select_related(
            "tenant",
            "provider",
            "event_type",
            "event",
            "event__tenant",
            "canonical_visit",
        )
        .filter(submission_code__iexact=clean)
        .first()
    )
    if assignment is None:
        return None
    if not assignment.provider.is_active:
        return None
    if assignment.status in {
        ProviderAssignment.STATUS_REVOKED,
        ProviderAssignment.STATUS_DECLINED,
    }:
        return None
    return assignment


@transaction.atomic
def record_checkin_submission_for_assignment(
    *,
    assignment: ProviderAssignment,
    event: Event,
    custom_recap,
) -> VisitSubmissionSource:
    """Link a mobile field check-in submission filed via a scoped
    `PA-XXXXXX` link to its CanonicalVisit and VisitSubmissionSource."""
    visit = assignment.canonical_visit
    if visit is None or (visit.event_id and visit.event_id != event.id):
        visit = ensure_canonical_visit_for_event(
            event, primary_provider=assignment.provider
        )
        if (
            assignment.scope_type == ProviderAssignment.SCOPE_VISIT
            and assignment.canonical_visit_id is None
        ):
            assignment.canonical_visit = visit
            assignment.event = event
            assignment.save(
                update_fields=["canonical_visit", "event", "updated_at"]
            )

    rule = ProviderSourceInclusionRule.objects.filter(
        tenant=assignment.tenant, provider=assignment.provider
    ).first()
    exclude = bool(rule and not rule.include_in_combined_reporting) or bool(
        getattr(custom_recap, "exclude_from_aggregates", False)
    )

    visit.status = (
        CanonicalVisit.STATUS_COMPLETED
        if getattr(custom_recap, "approved", False)
        else CanonicalVisit.STATUS_NEEDS_REVIEW
    )
    if visit.primary_provider_id is None:
        visit.primary_provider = assignment.provider
    visit.save(update_fields=["status", "primary_provider", "updated_at"])

    sub, _ = VisitSubmissionSource.objects.update_or_create(
        tenant=assignment.tenant,
        canonical_visit=visit,
        provider=assignment.provider,
        custom_recap=custom_recap,
        defaults={
            "source_kind": VisitSubmissionSource.KIND_WALKUP_CHECKIN,
            "recap_status": (
                VisitSubmissionSource.RECAP_APPROVED
                if getattr(custom_recap, "approved", False)
                else VisitSubmissionSource.RECAP_SUBMITTED
            ),
            "exclude_from_aggregates": exclude,
            "samples_distributed": getattr(custom_recap, "total_samples", None),
            "rep_observed_units": getattr(custom_recap, "sales", None),
        },
    )
    return sub


@transaction.atomic
def set_provider_source_inclusion(
    access: ResolvedWorkspaceAccess,
    *,
    provider_uuid: str,
    include_in_combined_reporting: bool,
    notes: str = "",
) -> ProviderSourceInclusionRule:
    if not access.is_brand_admin:
        raise PermissionError(
            "Only brand administrators can configure provider reporting inclusion."
        )
    provider = ProviderOrganization.objects.filter(uuid=provider_uuid).first()
    if provider is None:
        raise ValueError("Provider organization not found.")

    rule, _ = ProviderSourceInclusionRule.objects.update_or_create(
        tenant=access.tenant,
        provider=provider,
        defaults={
            "include_in_combined_reporting": include_in_combined_reporting,
            "notes": (notes or "").strip()[:300],
            "updated_by": access.user,
        },
    )
    log_workspace_audit(
        tenant=access.tenant,
        actor=access.user,
        category="reporting",
        action="provider_inclusion_updated",
        target_type="ProviderSourceInclusionRule",
        target_id=str(rule.uuid),
        summary=(
            f"Set {provider.name} combined reporting inclusion to "
            f"{include_in_combined_reporting}."
        ),
        metadata={
            "provider_uuid": str(provider.uuid),
            "include_in_combined_reporting": include_in_combined_reporting,
        },
    )
    return rule


@transaction.atomic
def create_workspace_shared_link(
    access: ResolvedWorkspaceAccess,
    *,
    resource_type: str = WorkspaceSharedLink.RESOURCE_VISIT,
    visit_uuid: str | None = None,
    custom_recap_id: int | None = None,
    report_params: dict[str, Any] | None = None,
    expires_in_days: int = 30,
) -> WorkspaceSharedLink:
    if not access.can_view_brand_consolidated:
        raise PermissionError(
            "Only brand workspace members can create shared workspace links."
        )
    visit = (
        CanonicalVisit.objects.filter(
            tenant=access.tenant, uuid=visit_uuid
        ).first()
        if visit_uuid
        else None
    )
    token = f"wsl_{secrets.token_urlsafe(28)}"
    link = WorkspaceSharedLink.objects.create(
        tenant=access.tenant,
        token=token,
        resource_type=resource_type,
        canonical_visit=visit,
        custom_recap_id=custom_recap_id,
        report_params=report_params or {},
        created_by=access.user,
        created_by_membership=access.membership,
        expires_at=timezone.now() + timedelta(days=max(expires_in_days, 1)),
    )
    log_workspace_audit(
        tenant=access.tenant,
        actor=access.user,
        category="share",
        action="shared_link_created",
        target_type="WorkspaceSharedLink",
        target_id=str(link.uuid),
        summary=f"Created shared link ({resource_type}).",
    )
    return link


@transaction.atomic
def revoke_workspace_shared_link(
    access: ResolvedWorkspaceAccess,
    *,
    token: str,
) -> WorkspaceSharedLink:
    if not access.is_brand_admin and not access.can_edit_brand_ops:
        raise PermissionError(
            "Only brand administrators or reviewers can revoke shared links."
        )
    link = WorkspaceSharedLink.objects.filter(
        tenant=access.tenant, token=token
    ).first()
    if link is None:
        raise ValueError("Shared link not found.")
    link.revoked_at = timezone.now()
    link.save(update_fields=["revoked_at"])
    log_workspace_audit(
        tenant=access.tenant,
        actor=access.user,
        category="share",
        action="shared_link_revoked",
        target_type="WorkspaceSharedLink",
        target_id=str(link.uuid),
        summary=f"Revoked shared link ({link.resource_type}).",
    )
    return link

