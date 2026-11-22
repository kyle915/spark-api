"""Feature 7 service layer: Self-service brand onboarding ('Use Spark with my
team') with idempotent resume, existing-account & managed-brand resolution,
starter program + recap template provisioning, mobile field recap link, and
self-service visit approval + shareable recap without Ignite staff."""

from __future__ import annotations

import logging
import secrets
from typing import Any

from django.conf import settings
from django.contrib.auth import authenticate
from django.db import connection, transaction
from django.utils import timezone
from django.utils.text import slugify
from gqlauth.jwt.types_ import TokenType
from gqlauth.models import RefreshToken, UserStatus

from brand_insights.facts import store_identity
from events.models import EventStatus, EventType, Product, ProductType, RequestStatus, RequestType
from legal import versions as legal_versions
from legal.service import accept_current as accept_legal_terms
from pilot.entitlements import (
    compute_monthly_visit_usage,
    ensure_brand_subscription,
    require_active_program_capacity,
    require_new_visit_capacity,
    resolve_brand_entitlements,
)
from pilot.models import (
    BrandOnboardingProgress,
    BrandSubscription,
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
from pilot.permissions import ResolvedWorkspaceAccess, log_workspace_audit
from pilot.workspaces import (
    _mint_submission_code,
    _sync_tenanted_user_for_brand,
    create_workspace_shared_link,
    ensure_workspace_profile,
)
from recaps.models import (
    CustomField,
    CustomRecapFieldType,
    CustomRecapTemplate,
    FileRecapCategory,
    RecapSection,
)
from sales_assist.marketing_lead import (
    queue_self_serve_close_sync,
    sanitize_utm,
)
from tenants.models import Role, Tenant, TenantedUser, User

logger = logging.getLogger(__name__)


STARTER_PROGRAM_CATALOG: tuple[dict[str, Any], ...] = (
    {
        "key": "beverage_retail_demo",
        "program_name": "Retail Tasting & Velocity Demo",
        "program_slug": "retail-sampling",
        "template_name": "Starter Retail Tasting Recap",
        "summary": "In-store wet or dry sampling focused on cans/units sold, shopper feedback, and shelf stock.",
        "default_sku_name": "Flagship Sparkling 12oz",
        "sections": [
            {
                "name": "Shopper & Shelf Outcomes",
                "fields": [
                    ("Top Performing Flavor / SKU", "text", True, "e.g., Blood Orange 4pk"),
                    ("Shelf Stock Condition on Arrival", "select", True, "Full shelf / Low stock / Out of stock"),
                    ("Shopper Taste & Price Feedback", "textarea", True, "What shoppers loved or hesitated on"),
                ],
            }
        ],
    },
    {
        "key": "on_premise_tasting",
        "program_name": "On-Premise Bar & Venue Sampling",
        "program_slug": "on-premise-sampling",
        "template_name": "Starter On-Premise Venue Recap",
        "summary": "Bar, restaurant, and taproom sampling tracking tabs/pours, bartender engagement, and menu placement.",
        "default_sku_name": "Draft / Can Pour",
        "sections": [
            {
                "name": "Venue & Bartender Notes",
                "fields": [
                    ("Featured Pour or Cocktail", "text", True, "e.g., Crisp Citrus Spritz"),
                    ("Bartender & Guest Reaction", "textarea", True, "Staff enthusiasm and repeat orders"),
                ],
            }
        ],
    },
    {
        "key": "event_sampling",
        "program_name": "Community & Festival Activation",
        "program_slug": "event",
        "template_name": "Starter Event Activation Recap",
        "summary": "High-traffic run clubs, festivals, and fitness pop-ups with photo proof and footprint notes.",
        "default_sku_name": "Trial Sample Can",
        "sections": [
            {
                "name": "Footprint & Crowd Highlights",
                "fields": [
                    ("Crowd Energy & Demographics", "textarea", True, "Who stopped by the booth"),
                    ("Nearest Retail Callout Shared", "text", False, "Where guests were directed to buy"),
                ],
            }
        ],
    },
)


def list_starter_programs() -> list[dict[str, Any]]:
    return [
        {
            "key": item["key"],
            "program_name": item["program_name"],
            "template_name": item["template_name"],
            "summary": item["summary"],
            "default_sku_name": item["default_sku_name"],
        }
        for item in STARTER_PROGRAM_CATALOG
    ]


def _seed_tenant_operational_defaults(tenant: Tenant, owner: User) -> None:
    """Idempotently seed baseline statuses and file categories so a standalone
    brand workspace works across Requests, Events, and Recaps without Ignite
    admin setup."""
    for idx, st_name in enumerate(["Pending", "Approved", "Declined", "Archived"]):
        RequestStatus.objects.get_or_create(
            tenant=tenant,
            name=st_name,
            defaults={
                "is_default": idx == 0,
                "created_by": owner,
                "updated_by": owner,
            },
        )
        EventStatus.objects.get_or_create(
            tenant=tenant,
            name=st_name,
            defaults={
                "is_default": idx == 0,
                "created_by": owner,
                "updated_by": owner,
            },
        )
    for rt_name in ["Retail Sampling", "On-Premise", "Event Activation"]:
        RequestType.objects.get_or_create(
            tenant=tenant,
            name=rt_name,
            defaults={"created_by": owner, "updated_by": owner},
        )
    for cat_name in ["Sampling photos", "Table setup", "Receipts"]:
        FileRecapCategory.objects.get_or_create(
            tenant=tenant,
            name=cat_name,
            defaults={"created_by": owner, "updated_by": owner},
        )


def ensure_role_by_slug(slug: str, name: str) -> Role:
    """Resolve or create a Role safely even on Postgres databases where
    `tenants.0001_initial` seeded explicit IDs without advancing the sequence."""
    role = (
        Role.objects.filter(slug=slug).first()
        or Role.objects.filter(name__iexact=name).first()
    )
    if role is not None:
        if not role.slug:
            role.slug = slug
            role.save(update_fields=["slug"])
        return role
    if connection.vendor == "postgresql":
        with connection.cursor() as cur:
            cur.execute(
                "SELECT setval(pg_get_serial_sequence('tenants_role', 'id'),"
                " GREATEST(COALESCE((SELECT MAX(id) FROM tenants_role), 0), 1))"
            )
    return Role.objects.create(name=name, slug=slug)


def _issue_auth_tokens(user: User) -> tuple[str, str]:
    jwt_token = TokenType.from_user(user).token
    refresh_obj = RefreshToken.from_user(user)
    return jwt_token, refresh_obj.token


def _unique_tenant_slug(base_name: str) -> str:
    base = slugify(base_name)[:60] or "spark-brand"
    candidate = base
    idx = 2
    while (
        Tenant.objects.filter(slug=candidate).exists()
        or Tenant.objects.filter(request_url_name=candidate).exists()
    ):
        candidate = f"{base}-{idx}"
        idx += 1
    return candidate


@transaction.atomic
def start_or_resume_self_serve_onboarding(
    *,
    authenticated_user: User | None = None,
    email: str = "",
    password: str = "",
    first_name: str = "",
    last_name: str = "",
    brand_name: str = "",
    business_timezone: str = "America/Chicago",
    accept_terms: bool = True,
    force_new_workspace: bool = False,
    invitation_token: str = "",
    client_ip: str = "127.0.0.1",
    user_agent: str = "spark-self-serve",
    marketing_attribution: dict[str, Any] | None = None,
    landing_path: str = "/onboarding",
) -> dict[str, Any]:
    """Create or safely resume a standalone Free brand workspace without
    requiring a credit card or Ignite staff.
    - Existing account holders and managed brands resolve cleanly to their
      existing membership (or pending invitation when email ownership is
      verified) without creating duplicate brands, memberships, or invites.
    - Interrupted signups resume the same workspace and progress state.
    - New workspaces best-effort sync a Close CRM lead with UTM attribution."""
    if not getattr(settings, "STANDALONE_SIGNUP_ENABLED", True):
        raise PermissionError(
            "Self-service workspace signup is currently gated by release control."
        )

    user = (
        authenticated_user
        if authenticated_user and getattr(authenticated_user, "is_authenticated", False)
        else None
    )
    clean_inv_token = (invitation_token or "").strip()
    now = timezone.now()
    can_auto_accept_pending_invitation = False
    matched_token_inv: WorkspaceInvitation | None = None

    clean_email = (email or (user.email if user else "")).strip().lower()
    if user is not None:
        can_auto_accept_pending_invitation = (
            UserStatus.objects.filter(user=user, verified=True).exists()
            or bool(getattr(user, "is_staff", False))
            or bool(getattr(user, "is_superuser", False))
        )
    else:
        if not clean_email or "@" not in clean_email:
            raise ValueError("A valid work email address is required.")
        existing_user = User.objects.filter(email__iexact=clean_email).first()
        if existing_user is not None:
            if not password:
                raise PermissionError(
                    "An account with this email already exists. Enter your password or sign in to continue."
                )
            authed = authenticate(username=existing_user.username, password=password)
            if authed is None:
                authed = authenticate(username=clean_email, password=password)
            if authed is None and not existing_user.check_password(password):
                raise PermissionError(
                    "Invalid password for existing Spark account. Sign in or reset your password."
                )
            user = existing_user
            can_auto_accept_pending_invitation = (
                UserStatus.objects.filter(user=existing_user, verified=True).exists()
                or bool(getattr(existing_user, "is_staff", False))
                or bool(getattr(existing_user, "is_superuser", False))
            )
        else:
            if not accept_terms:
                raise ValueError(
                    "Please accept the Spark Terms of Service to create your workspace."
                )
            if len(password or "") < 8:
                raise ValueError(
                    "Choose a password of at least 8 characters to secure your workspace."
                )
            pending_candidates = list(
                WorkspaceInvitation.objects.select_related("tenant", "provider")
                .filter(
                    email__iexact=clean_email,
                    status=WorkspaceInvitation.STATUS_PENDING,
                    expires_at__gt=now,
                )
                .order_by("-created_at")
            )
            if clean_inv_token:
                matched_token_inv = next(
                    (
                        inv
                        for inv in pending_candidates
                        if secrets.compare_digest(inv.token, clean_inv_token)
                    ),
                    None,
                )
                if matched_token_inv is None:
                    raise PermissionError(
                        "Invalid or expired workspace invitation token for this email address."
                    )
                can_auto_accept_pending_invitation = True
            elif pending_candidates:
                raise PermissionError(
                    "This email address has a pending workspace invitation. Open your invitation link or provide the invitation token to verify email ownership before joining."
                )

            client_role = ensure_role_by_slug(Role.CLIENT_SLUG, "Client")
            username_base = clean_email
            user = User.objects.create_user(
                username=username_base,
                email=clean_email,
                password=password,
                first_name=(first_name or "").strip()[:80],
                last_name=(last_name or "").strip()[:80],
                role=client_role,
                is_active=True,
            )
            UserStatus.objects.update_or_create(
                user=user, defaults={"verified": True, "archived": False}
            )

    if accept_terms:
        accept_legal_terms(
            user.id,
            version=legal_versions.current_version(),
            ip=client_ip,
            ua=user_agent,
        )

    # 1. Pending invitation check: accept ONLY when email ownership is verified
    # (either via a matching invitation token or an already-verified existing user).
    pending_inv: WorkspaceInvitation | None = None
    if clean_inv_token:
        if matched_token_inv is not None:
            pending_inv = matched_token_inv
        else:
            pending_candidates = list(
                WorkspaceInvitation.objects.select_related("tenant", "provider")
                .filter(
                    email__iexact=user.email,
                    status=WorkspaceInvitation.STATUS_PENDING,
                    expires_at__gt=now,
                )
                .order_by("-created_at")
            )
            pending_inv = next(
                (
                    inv
                    for inv in pending_candidates
                    if secrets.compare_digest(inv.token, clean_inv_token)
                ),
                None,
            )
            if pending_inv is None:
                raise PermissionError(
                    "Invalid or expired workspace invitation token for this email address."
                )
    elif can_auto_accept_pending_invitation and not force_new_workspace:
        pending_inv = (
            WorkspaceInvitation.objects.select_related("tenant", "provider")
            .filter(
                email__iexact=user.email,
                status=WorkspaceInvitation.STATUS_PENDING,
                expires_at__gt=now,
            )
            .order_by("-created_at")
            .first()
        )
    if pending_inv is not None and not force_new_workspace:
        scope = (
            f"provider:{pending_inv.provider_id}"
            if pending_inv.provider_id
            else "brand"
        )
        membership, _ = WorkspaceMembership.objects.update_or_create(
            tenant=pending_inv.tenant,
            user=user,
            membership_scope=scope,
            defaults={
                "provider": pending_inv.provider,
                "workspace_role": pending_inv.workspace_role,
                "can_approve_recaps": pending_inv.can_approve_recaps,
                "can_approve_finance": pending_inv.can_approve_finance,
                "can_approve_spend": pending_inv.can_approve_spend,
                "status": WorkspaceMembership.STATUS_ACTIVE,
                "created_by": pending_inv.invited_by,
            },
        )
        pending_inv.status = WorkspaceInvitation.STATUS_ACCEPTED
        pending_inv.accepted_by = user
        pending_inv.accepted_at = timezone.now()
        pending_inv.save(
            update_fields=["status", "accepted_by", "accepted_at", "updated_at"]
        )
        _sync_tenanted_user_for_brand(pending_inv.tenant, user)
        UserActiveWorkspaceContext.objects.update_or_create(
            user=user,
            defaults={"tenant": pending_inv.tenant, "membership": membership},
        )
        jwt_tok, ref_tok = _issue_auth_tokens(user)
        return {
            "user": user,
            "tenant": pending_inv.tenant,
            "membership": membership,
            "resumed_existing": True,
            "resolved_managed": False,
            "joined_invitation": True,
            "token": jwt_tok,
            "refresh_token": ref_tok,
        }

    clean_brand = (brand_name or "").strip()

    # 2. Returning user resume check: if user already has an active self-serve
    # workspace (either matching brand name or any existing self-serve workspace
    # when resuming without force_new_workspace), resume it idempotently!
    existing_memberships = list(
        WorkspaceMembership.objects.select_related(
            "tenant", "tenant__workspace_profile"
        )
        .filter(
            user=user,
            status=WorkspaceMembership.STATUS_ACTIVE,
            workspace_role__in=WorkspaceMembership.BRAND_ROLES,
        )
        .order_by("id")
    )
    if existing_memberships and not force_new_workspace:
        matched_m = None
        if clean_brand:
            for m in existing_memberships:
                if m.tenant.name.strip().lower() == clean_brand.lower():
                    matched_m = m
                    break
        if matched_m is None:
            # Prefer a self-serve workspace in progress; otherwise first brand membership
            self_serve_ms = [
                m
                for m in existing_memberships
                if getattr(m.tenant, "workspace_profile", None)
                and m.tenant.workspace_profile.mode
                == WorkspaceProfile.MODE_SELF_SERVE
            ]
            matched_m = self_serve_ms[0] if self_serve_ms else existing_memberships[0]

        UserActiveWorkspaceContext.objects.update_or_create(
            user=user,
            defaults={"tenant": matched_m.tenant, "membership": matched_m},
        )
        profile = ensure_workspace_profile(matched_m.tenant)
        BrandOnboardingProgress.objects.get_or_create(
            tenant=matched_m.tenant,
            defaults={"owner": user, "step_workspace_created": True},
        )
        jwt_tok, ref_tok = _issue_auth_tokens(user)
        return {
            "user": user,
            "tenant": matched_m.tenant,
            "membership": matched_m,
            "resumed_existing": True,
            "resolved_managed": (
                profile.mode == WorkspaceProfile.MODE_MANAGED
            ),
            "joined_invitation": False,
            "token": jwt_tok,
            "refresh_token": ref_tok,
        }

    # 3. Existing Ignite-managed brand user (TenantedUser without WorkspaceMembership)
    legacy_tu = (
        TenantedUser.objects.select_related("tenant")
        .filter(user=user, is_active=True)
        .order_by("id")
        .first()
    )
    if legacy_tu is not None and not force_new_workspace:
        ensure_workspace_profile(
            legacy_tu.tenant,
            mode=WorkspaceProfile.MODE_MANAGED,
            plan_tier=WorkspaceProfile.PLAN_MANAGED_SOW,
        )
        ensure_brand_subscription(legacy_tu.tenant)
        UserActiveWorkspaceContext.objects.update_or_create(
            user=user,
            defaults={"tenant": legacy_tu.tenant, "membership": None},
        )
        jwt_tok, ref_tok = _issue_auth_tokens(user)
        return {
            "user": user,
            "tenant": legacy_tu.tenant,
            "membership": None,
            "resumed_existing": True,
            "resolved_managed": True,
            "joined_invitation": False,
            "token": jwt_tok,
            "refresh_token": ref_tok,
        }

    # 4. Create brand-new standalone Free workspace (no payment method required!)
    if not clean_brand:
        raise ValueError("Enter your brand or company name to create a workspace.")

    slug = _unique_tenant_slug(clean_brand)
    tenant = Tenant.objects.create(
        name=clean_brand,
        slug=slug,
        request_url_name=slug,
        created_by=user,
        updated_by=user,
    )
    profile = WorkspaceProfile.objects.create(
        tenant=tenant,
        mode=WorkspaceProfile.MODE_SELF_SERVE,
        plan_tier=WorkspaceProfile.PLAN_FREE,
        business_timezone=(business_timezone or "America/Chicago").strip()
        or "America/Chicago",
    )
    BrandSubscription.objects.create(
        tenant=tenant,
        plan_code=WorkspaceProfile.PLAN_FREE,
        status=BrandSubscription.STATUS_FREE_ACTIVE,
        stripe_mode=getattr(settings, "STRIPE_BILLING_MODE", "test") or "test",
        billing_owner=user,
    )
    _seed_tenant_operational_defaults(tenant, user)

    membership = WorkspaceMembership.objects.create(
        tenant=tenant,
        user=user,
        membership_scope="brand",
        workspace_role=WorkspaceMembership.ROLE_BRAND_ADMIN,
        can_approve_recaps=True,
        can_approve_finance=True,
        can_approve_spend=True,
        status=WorkspaceMembership.STATUS_ACTIVE,
        created_by=user,
    )
    _sync_tenanted_user_for_brand(tenant, user)
    UserActiveWorkspaceContext.objects.update_or_create(
        user=user,
        defaults={"tenant": tenant, "membership": membership},
    )
    clean_attribution = sanitize_utm(marketing_attribution)
    progress = BrandOnboardingProgress.objects.create(
        tenant=tenant,
        owner=user,
        step_workspace_created=True,
        marketing_attribution=clean_attribution,
    )
    log_workspace_audit(
        tenant=tenant,
        actor=user,
        category="onboarding",
        action="self_serve_workspace_created",
        target_type="Tenant",
        target_id=str(tenant.uuid),
        summary=f"Created self-service Free workspace for {tenant.name} ({profile.business_timezone}).",
    )
    # After commit, enqueue Close I/O onto Cloud Tasks (else a daemon thread)
    # so signup never waits on CRM HTTP even though on_commit itself is sync.
    queue_self_serve_close_sync(
        tenant_id=tenant.id,
        progress_id=progress.id,
        user_id=user.id,
        brand_name=clean_brand,
        email=clean_email,
        first_name=first_name,
        last_name=last_name,
        utm=dict(clean_attribution),
        landing_path=landing_path or "/onboarding",
    )
    jwt_tok, ref_tok = _issue_auth_tokens(user)
    return {
        "user": user,
        "tenant": tenant,
        "membership": membership,
        "resumed_existing": False,
        "resolved_managed": False,
        "joined_invitation": False,
        "token": jwt_tok,
        "refresh_token": ref_tok,
    }


@transaction.atomic
def select_onboarding_starter_program(
    access: ResolvedWorkspaceAccess,
    *,
    starter_key: str,
    custom_program_name: str = "",
    starter_sku_name: str = "",
) -> BrandOnboardingProgress:
    """Idempotently provision (or update in-place) the brand's starter program,
    recap template, internal operating team, and mobile field submission link
    (`PA-XXXXXX`) without exceeding Free's 1-active-program limit."""
    if not access.is_brand_admin and not access.can_edit_brand_ops:
        raise PermissionError(
            "Only brand administrators can configure the starter program."
        )

    spec = next(
        (s for s in STARTER_PROGRAM_CATALOG if s["key"] == starter_key),
        STARTER_PROGRAM_CATALOG[0],
    )
    progress, _ = BrandOnboardingProgress.objects.get_or_create(
        tenant=access.tenant,
        defaults={"owner": access.user, "step_workspace_created": True},
    )

    prog_name = (custom_program_name or spec["program_name"]).strip()
    prog_slug = slugify(prog_name)[:60] or spec["program_slug"]

    # Reuse existing starter_event_type or single existing EventType on Free so
    # selecting/updating starter template stays within Free's 1-program rule.
    event_type = progress.starter_event_type
    if event_type is None:
        event_type = (
            EventType.objects.filter(tenant=access.tenant)
            .order_by("id")
            .first()
        )
    if event_type is None:
        require_active_program_capacity(access.tenant)
        event_type = EventType.objects.create(
            tenant=access.tenant,
            name=prog_name,
            slug=prog_slug,
            is_default=True,
            created_by=access.user,
            updated_by=access.user,
        )
    else:
        require_active_program_capacity(
            access.tenant, existing_event_type_id=event_type.id
        )
        event_type.name = prog_name
        event_type.slug = prog_slug
        event_type.updated_by = access.user
        event_type.save(update_fields=["name", "slug", "updated_by", "updated_at"])

    # Seed starter SKU if catalog is empty or starter_sku_name provided
    sku_label = (starter_sku_name or spec["default_sku_name"]).strip()
    if sku_label and not Product.objects.filter(
        tenant=access.tenant, name__iexact=sku_label
    ).exists():
        ptype, _ = ProductType.objects.get_or_create(
            tenant=access.tenant,
            name="Beverage",
            defaults={"created_by": access.user, "updated_by": access.user},
        )
        Product.objects.create(
            tenant=access.tenant,
            name=sku_label,
            description="Starter SKU",
            product_type=ptype,
            created_by=access.user,
            updated_by=access.user,
        )

    # Create or update starter CustomRecapTemplate
    template = progress.starter_recap_template or (
        CustomRecapTemplate.objects.filter(
            tenant=access.tenant, event_type=event_type
        ).first()
    )
    if template is None:
        template = CustomRecapTemplate.objects.create(
            tenant=access.tenant,
            event_type=event_type,
            name=spec["template_name"],
            product_samples=True,
            sales_performance=True,
            created_by=access.user,
            updated_by=access.user,
        )
    else:
        template.name = spec["template_name"]
        template.event_type = event_type
        template.updated_by = access.user
        template.save(update_fields=["name", "event_type", "updated_by", "updated_at"])

    if not CustomField.objects.filter(custom_recap_template=template).exists():
        for s_idx, sec_spec in enumerate(spec["sections"]):
            section, _ = RecapSection.objects.get_or_create(
                tenant=access.tenant,
                name=sec_spec["name"],
                defaults={
                    "order": s_idx,
                    "created_by": access.user,
                    "updated_by": access.user,
                },
            )
            for f_idx, (f_name, f_type_name, req_flag, ph) in enumerate(
                sec_spec["fields"]
            ):
                ftype, _ = CustomRecapFieldType.objects.get_or_create(
                    name=f_type_name,
                    defaults={
                        "created_by": access.user,
                        "updated_by": access.user,
                    },
                )
                CustomField.objects.create(
                    custom_recap_template=template,
                    recap_section=section,
                    custom_field_type=ftype,
                    name=f_name,
                    required=req_flag,
                    order=f_idx,
                    options=(
                        ["Full shelf", "Low stock", "Out of stock"]
                        if f_type_name == "select"
                        else []
                    ),
                    placeholder=ph,
                    created_by=access.user,
                    updated_by=access.user,
                )

    # Ensure 1 default operating team + active mobile recap link (`PA-XXXXXX`)
    provider = progress.default_provider
    if provider is None:
        team_slug = slugify(f"{access.tenant.slug or access.tenant.name}-field-team")[:75]
        provider, _ = ProviderOrganization.objects.get_or_create(
            slug=team_slug,
            defaults={
                "name": f"{access.tenant.name} Field Team",
                "contact_email": access.user.email or "",
                "is_ignite_managed": False,
                "is_active": True,
                "created_by": access.user,
            },
        )
        ProviderSourceInclusionRule.objects.get_or_create(
            tenant=access.tenant,
            provider=provider,
            defaults={
                "include_in_combined_reporting": True,
                "updated_by": access.user,
            },
        )

    assignment = progress.starter_assignment
    if assignment is None or assignment.status == ProviderAssignment.STATUS_REVOKED:
        assignment = ProviderAssignment.objects.create(
            tenant=access.tenant,
            provider=provider,
            scope_type=ProviderAssignment.SCOPE_PROGRAM,
            event_type=event_type,
            status=ProviderAssignment.STATUS_ACCEPTED,
            submission_code=_mint_submission_code(),
            notes="Self-service starter mobile recap link",
            created_by=access.user,
        )
    else:
        assignment.event_type = event_type
        assignment.save(update_fields=["event_type", "updated_at"])

    progress.starter_template_key = spec["key"]
    progress.starter_event_type = event_type
    progress.starter_recap_template = template
    progress.default_provider = provider
    progress.starter_assignment = assignment
    progress.step_program_selected = True
    progress.save()

    log_workspace_audit(
        tenant=access.tenant,
        actor=access.user,
        category="onboarding",
        action="starter_program_selected",
        target_type="EventType",
        target_id=str(event_type.id),
        summary=f"Selected starter program '{event_type.name}' ({assignment.submission_code}).",
    )
    return progress


@transaction.atomic
def onboarding_invite_contributor(
    access: ResolvedWorkspaceAccess,
    *,
    email: str,
    workspace_role: str = WorkspaceMembership.ROLE_FIELD_CONTRIBUTOR,
    provider_name: str = "",
) -> WorkspaceInvitation:
    """Idempotently invite a field contributor, provider manager, or viewer
    during self-service onboarding. Repeating the same invite resumes and
    returns the existing invitation without creating duplicate brands,
    memberships, or invitations."""
    if not access.is_brand_admin:
        raise PermissionError("Only brand administrators can invite contributors.")
    clean_email = (email or "").strip().lower()
    if not clean_email or "@" not in clean_email:
        raise ValueError("Enter a valid email address.")

    progress, _ = BrandOnboardingProgress.objects.get_or_create(
        tenant=access.tenant,
        defaults={"owner": access.user, "step_workspace_created": True},
    )

    provider: ProviderOrganization | None = None
    if workspace_role in WorkspaceMembership.PROVIDER_ROLES:
        if provider_name.strip():
            p_slug = slugify(f"{access.tenant.slug}-{provider_name}")[:75]
            provider = ProviderOrganization.objects.filter(slug=p_slug).first()
            if provider is None:
                # Reuse default_provider if same operating slot on Free/Core
                if progress.default_provider is not None and (
                    progress.default_provider.name.lower()
                    == provider_name.strip().lower()
                ):
                    provider = progress.default_provider
                else:
                    provider = ProviderOrganization.objects.create(
                        name=provider_name.strip(),
                        slug=p_slug,
                        contact_email=clean_email,
                        is_ignite_managed=False,
                        is_active=True,
                        created_by=access.user,
                    )
                    ProviderSourceInclusionRule.objects.get_or_create(
                        tenant=access.tenant,
                        provider=provider,
                        defaults={
                            "include_in_combined_reporting": True,
                            "updated_by": access.user,
                        },
                    )
        else:
            provider = progress.default_provider
            if provider is None:
                p_slug = slugify(f"{access.tenant.slug}-field-team")[:75]
                provider, _ = ProviderOrganization.objects.get_or_create(
                    slug=p_slug,
                    defaults={
                        "name": f"{access.tenant.name} Field Team",
                        "contact_email": clean_email,
                        "is_ignite_managed": False,
                        "is_active": True,
                        "created_by": access.user,
                    },
                )

    # Idempotent check: return existing non-revoked invitation if repeated!
    existing_inv = (
        WorkspaceInvitation.objects.filter(
            tenant=access.tenant,
            email__iexact=clean_email,
            workspace_role=workspace_role,
            provider=provider,
        )
        .exclude(status=WorkspaceInvitation.STATUS_REVOKED)
        .order_by("-created_at")
        .first()
    )
    if existing_inv is not None:
        if not progress.step_contributor_invited:
            progress.step_contributor_invited = True
            progress.save(update_fields=["step_contributor_invited", "updated_at"])
        return existing_inv

    invitation = WorkspaceInvitation.objects.create(
        tenant=access.tenant,
        email=clean_email,
        workspace_role=workspace_role,
        provider=provider,
        can_approve_recaps=False,
        can_approve_finance=False,
        can_approve_spend=False,
        token=secrets.token_urlsafe(32),
        status=WorkspaceInvitation.STATUS_PENDING,
        expires_at=timezone.now() + timezone.timedelta(days=14),
        invited_by=access.user,
    )

    existing_user = User.objects.filter(email__iexact=clean_email).first()
    if existing_user is not None:
        if getattr(existing_user, "role_id", None) is None:
            client_role = ensure_role_by_slug(Role.CLIENT_SLUG, "Client")
            existing_user.role = client_role
            existing_user.save(update_fields=["role"])
        scope = f"provider:{provider.id}" if provider else "brand"
        WorkspaceMembership.objects.update_or_create(
            tenant=access.tenant,
            user=existing_user,
            membership_scope=scope,
            defaults={
                "provider": provider,
                "workspace_role": workspace_role,
                "status": WorkspaceMembership.STATUS_ACTIVE,
                "created_by": access.user,
            },
        )
        _sync_tenanted_user_for_brand(access.tenant, existing_user)
        invitation.status = WorkspaceInvitation.STATUS_ACCEPTED
        invitation.accepted_by = existing_user
        invitation.accepted_at = timezone.now()
        invitation.save(
            update_fields=["status", "accepted_by", "accepted_at", "updated_at"]
        )

    progress.step_contributor_invited = True
    progress.save(update_fields=["step_contributor_invited", "updated_at"])

    log_workspace_audit(
        tenant=access.tenant,
        actor=access.user,
        category="onboarding",
        action="onboarding_contributor_invited",
        target_type="WorkspaceInvitation",
        target_id=str(invitation.uuid),
        summary=f"Invited {clean_email} ({workspace_role}) during self-serve onboarding.",
    )
    return invitation


@transaction.atomic
def schedule_standalone_visit(
    access: ResolvedWorkspaceAccess,
    *,
    store_name: str,
    visit_date,
    address: str = "",
    city: str = "",
    state_code: str = "",
    provider_uuid: str | None = None,
    event_type_id: int | None = None,
    notes: str = "",
) -> tuple[CanonicalVisit, ProviderAssignment]:
    """Schedule a new standalone brand visit and mint its scoped mobile field
    submission link (`PA-XXXXXX`). Enforces the monthly completed-visit cap
    before creating a new billable booking!"""
    if not access.can_edit_brand_ops:
        raise PermissionError(
            "Only brand administrators or reviewers can schedule visits."
        )
    require_new_visit_capacity(
        access.tenant, is_ignite_admin=access.is_ignite_admin
    )

    clean_store = (store_name or "").strip()
    if not clean_store:
        raise ValueError("Store name is required to schedule a visit.")

    provider: ProviderOrganization | None = None
    if provider_uuid:
        provider = ProviderOrganization.objects.filter(
            uuid=provider_uuid, is_active=True
        ).first()
    if provider is None:
        progress = BrandOnboardingProgress.objects.filter(
            tenant=access.tenant
        ).first()
        provider = progress.default_provider if progress else None
    if provider is None:
        p_slug = slugify(f"{access.tenant.slug or access.tenant.name}-field-team")[:75]
        provider, _ = ProviderOrganization.objects.get_or_create(
            slug=p_slug,
            defaults={
                "name": f"{access.tenant.name} Field Team",
                "is_ignite_managed": False,
                "is_active": True,
                "created_by": access.user,
            },
        )
        ProviderSourceInclusionRule.objects.get_or_create(
            tenant=access.tenant,
            provider=provider,
            defaults={
                "include_in_combined_reporting": True,
                "updated_by": access.user,
            },
        )

    event_type = (
        EventType.objects.filter(tenant=access.tenant, id=event_type_id).first()
        if event_type_id
        else EventType.objects.filter(tenant=access.tenant).order_by("id").first()
    )

    s_key, s_label, c_key, c_label = store_identity(
        clean_store, address, city, state_code
    )
    visit = CanonicalVisit.objects.create(
        tenant=access.tenant,
        primary_provider=provider,
        event_type=event_type,
        store_key=s_key,
        store_label=s_label,
        chain_key=c_key,
        chain_label=c_label,
        address=(address or "").strip(),
        city=(city or "").strip(),
        state_code=(state_code or "").strip().upper()[:16],
        market=(city or state_code or "").strip(),
        visit_date=visit_date,
        shift_key=f"scheduled:{secrets.token_hex(3)}",
        status=CanonicalVisit.STATUS_SCHEDULED,
    )
    assignment = ProviderAssignment.objects.create(
        tenant=access.tenant,
        provider=provider,
        scope_type=ProviderAssignment.SCOPE_VISIT,
        event_type=event_type,
        canonical_visit=visit,
        status=ProviderAssignment.STATUS_ASSIGNED,
        submission_code=_mint_submission_code(),
        due_date=visit_date,
        notes=(notes or "").strip(),
        created_by=access.user,
    )
    log_workspace_audit(
        tenant=access.tenant,
        actor=access.user,
        category="visit",
        action="standalone_visit_scheduled",
        target_type="CanonicalVisit",
        target_id=str(visit.uuid),
        summary=f"Scheduled visit at {visit.store_label} on {visit.visit_date} ({assignment.submission_code}).",
    )
    return visit, assignment


@transaction.atomic
def approve_canonical_visit_recap(
    access: ResolvedWorkspaceAccess,
    *,
    visit_uuid: str,
    completed_at=None,
) -> tuple[CanonicalVisit, WorkspaceSharedLink]:
    """Brand-side self-service recap approval without requiring Ignite staff!
    - Marks the CanonicalVisit `completed` (which counts 1 canonical visit in
      the brand's configured timezone for that completion month).
    - Never blocked when the brand is already at its monthly cap (in-progress /
      already-conducted fieldwork always completes with zero automatic charge!).
    - Mints or returns a shareable recap link (`wsl_...`) and marks onboarding
      complete on the brand's first approved visit."""
    if not access.can_approve_recaps:
        raise PermissionError(
            "You do not have recap approval permission in this workspace."
        )
    visit = (
        CanonicalVisit.objects.filter(tenant=access.tenant, uuid=visit_uuid)
        .select_for_update()
        .first()
    )
    if visit is None:
        raise ValueError("Visit not found in this brand workspace.")

    now = completed_at or timezone.now()
    visit.status = CanonicalVisit.STATUS_COMPLETED
    if visit.completed_at is None or completed_at is not None:
        visit.completed_at = now
    visit.save(update_fields=["status", "completed_at", "updated_at"])

    for sub in VisitSubmissionSource.objects.select_related("custom_recap").filter(
        tenant=access.tenant, canonical_visit=visit
    ):
        if sub.recap_status != VisitSubmissionSource.RECAP_EXCLUDED:
            sub.recap_status = VisitSubmissionSource.RECAP_APPROVED
            sub.save(update_fields=["recap_status", "updated_at"])
        if sub.custom_recap and not sub.custom_recap.approved:
            sub.custom_recap.approved = True
            sub.custom_recap.approved_by = access.user
            sub.custom_recap.approved_at = now
            sub.custom_recap.save(
                update_fields=["approved", "approved_by", "approved_at", "updated_at"]
            )

    ProviderAssignment.objects.filter(
        tenant=access.tenant, canonical_visit=visit
    ).exclude(status=ProviderAssignment.STATUS_REVOKED).update(
        status=ProviderAssignment.STATUS_COMPLETED, updated_at=now
    )

    shared_link = (
        WorkspaceSharedLink.objects.filter(
            tenant=access.tenant,
            canonical_visit=visit,
            revoked_at__isnull=True,
            expires_at__gt=timezone.now(),
        )
        .order_by("-created_at")
        .first()
    )
    if shared_link is None:
        shared_link = create_workspace_shared_link(
            access,
            resource_type=WorkspaceSharedLink.RESOURCE_VISIT,
            visit_uuid=str(visit.uuid),
        )

    progress = BrandOnboardingProgress.objects.filter(
        tenant=access.tenant
    ).first()
    if progress is not None and not progress.step_first_visit_completed:
        progress.first_visit = visit
        progress.first_shared_link = shared_link
        progress.step_mobile_link_opened = True
        progress.step_first_visit_completed = True
        progress.completed_at = now
        progress.save()

    log_workspace_audit(
        tenant=access.tenant,
        actor=access.user,
        category="recap",
        action="canonical_visit_approved",
        target_type="CanonicalVisit",
        target_id=str(visit.uuid),
        summary=f"Approved recap for {visit.store_label} ({visit.visit_date}) and generated shareable link.",
    )
    return visit, shared_link


@transaction.atomic
def reverse_canonical_visit_completion(
    access: ResolvedWorkspaceAccess,
    *,
    visit_uuid: str,
    new_status: str = CanonicalVisit.STATUS_CANCELLED,
    reason: str = "",
) -> CanonicalVisit:
    """Deterministic reversal / cancellation of a visit so monthly metering
    cleanly drops reversed or cancelled activations."""
    if not access.can_edit_brand_ops:
        raise PermissionError(
            "Only brand administrators or reviewers can reverse or cancel visits."
        )
    if new_status not in {
        CanonicalVisit.STATUS_CANCELLED,
        CanonicalVisit.STATUS_NEEDS_REVIEW,
        CanonicalVisit.STATUS_ARCHIVED,
    }:
        raise ValueError("Invalid reversal target status.")
    visit = CanonicalVisit.objects.filter(
        tenant=access.tenant, uuid=visit_uuid
    ).first()
    if visit is None:
        raise ValueError("Visit not found.")
    visit.status = new_status
    visit.completed_at = None
    visit.save(update_fields=["status", "completed_at", "updated_at"])
    log_workspace_audit(
        tenant=access.tenant,
        actor=access.user,
        category="visit",
        action="visit_completion_reversed",
        target_type="CanonicalVisit",
        target_id=str(visit.uuid),
        summary=f"Reversed visit {visit.store_label} to {new_status}. {reason}".strip(),
    )
    return visit


def get_onboarding_state(access: ResolvedWorkspaceAccess) -> dict[str, Any]:
    progress, _ = BrandOnboardingProgress.objects.select_related(
        "starter_event_type",
        "starter_recap_template",
        "default_provider",
        "starter_assignment",
        "first_visit",
        "first_shared_link",
    ).get_or_create(
        tenant=access.tenant,
        defaults={
            "owner": access.user,
            "step_workspace_created": True,
        },
    )

    latest_visit = progress.first_visit or (
        CanonicalVisit.objects.filter(tenant=access.tenant)
        .order_by("-created_at")
        .first()
    )
    has_completed_visit = CanonicalVisit.objects.filter(
        tenant=access.tenant, status=CanonicalVisit.STATUS_COMPLETED
    ).exists()
    if has_completed_visit and not progress.step_first_visit_completed:
        progress.step_first_visit_completed = True
        progress.first_visit = latest_visit
        if progress.completed_at is None:
            progress.completed_at = timezone.now()
        progress.save(
            update_fields=[
                "step_first_visit_completed",
                "first_visit",
                "completed_at",
                "updated_at",
            ]
        )

    ent = resolve_brand_entitlements(access.tenant)
    usage = compute_monthly_visit_usage(access.tenant)

    steps_completed = sum(
        [
            bool(progress.step_workspace_created),
            bool(progress.step_program_selected),
            bool(progress.step_contributor_invited),
            bool(progress.step_mobile_link_opened or latest_visit is not None),
            bool(progress.step_first_visit_completed),
        ]
    )

    return {
        "tenant_uuid": str(access.tenant.uuid),
        "tenant_name": access.tenant.name,
        "workspace_mode": ent.workspace_mode,
        "plan_code": ent.effective_plan_code,
        "plan_display_name": ent.display_name,
        "steps_completed": steps_completed,
        "total_steps": 5,
        "step_workspace_created": bool(progress.step_workspace_created),
        "step_program_selected": bool(progress.step_program_selected),
        "step_contributor_invited": bool(progress.step_contributor_invited),
        "step_mobile_link_opened": bool(
            progress.step_mobile_link_opened or latest_visit is not None
        ),
        "step_first_visit_completed": bool(progress.step_first_visit_completed),
        "starter_template_key": progress.starter_template_key or "beverage_retail_demo",
        "starter_program_name": (
            progress.starter_event_type.name
            if progress.starter_event_type
            else None
        ),
        "starter_recap_template_name": (
            progress.starter_recap_template.name
            if progress.starter_recap_template
            else None
        ),
        "submission_code": (
            progress.starter_assignment.submission_code
            if progress.starter_assignment
            else None
        ),
        "first_visit_uuid": str(latest_visit.uuid) if latest_visit else None,
        "first_visit_store_label": (
            latest_visit.store_label if latest_visit else None
        ),
        "first_visit_status": latest_visit.status if latest_visit else None,
        "first_shared_token": (
            progress.first_shared_link.token
            if progress.first_shared_link
            else None
        ),
        "completed_visits_used": usage["completed_visits_used"],
        "included_visits": usage["included_visits"],
        "starter_catalog": list_starter_programs(),
    }
