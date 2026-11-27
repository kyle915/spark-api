"""Additive models for Standalone Spark Milestone 1 (Features 1-6) with
clean seams for Milestone 2 (self-service onboarding, plan entitlements,
monthly visit usage, and subscription billing)."""

from __future__ import annotations

from decimal import Decimal
from uuid6 import uuid7

from django.conf import settings
from django.db import models


class ProviderOrganization(models.Model):
    """A field-marketing agency or operating team executing visits for brands."""

    id = models.BigAutoField(primary_key=True)
    uuid = models.UUIDField(default=uuid7, unique=True, editable=False)
    name = models.CharField(max_length=150)
    slug = models.SlugField(max_length=80, unique=True)
    contact_email = models.EmailField(blank=True, default="")
    is_ignite_managed = models.BooleanField(default=False)
    is_active = models.BooleanField(default=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="pilot_providers_created",
    )
    created_at = models.DateTimeField(auto_now_add=True, editable=False)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["name", "id"]

    def __str__(self) -> str:
        return self.name


class WorkspaceProfile(models.Model):
    """Per-brand workspace configuration and Milestone 2 entitlement seams."""

    MODE_MANAGED = "managed"
    MODE_PILOT = "pilot"
    MODE_SELF_SERVE = "self_serve"
    MODE_CHOICES = [
        (MODE_MANAGED, "Ignite Managed Service"),
        (MODE_PILOT, "Paid Standalone Pilot"),
        (MODE_SELF_SERVE, "Self-Service Workspace"),
    ]

    PLAN_MANAGED_SOW = "managed_sow"
    PLAN_PILOT_PRO = "pilot_pro"
    PLAN_FREE = "free"
    PLAN_CORE = "core"
    PLAN_PRO = "pro"
    PLAN_SCALE = "scale"
    PLAN_CHOICES = [
        (PLAN_MANAGED_SOW, "Managed SOW"),
        (PLAN_PILOT_PRO, "60-Day Pro Pilot"),
        (PLAN_FREE, "Free"),
        (PLAN_CORE, "Core"),
        (PLAN_PRO, "Pro"),
        (PLAN_SCALE, "Scale"),
    ]

    id = models.BigAutoField(primary_key=True)
    uuid = models.UUIDField(default=uuid7, unique=True, editable=False)
    tenant = models.OneToOneField(
        "tenants.Tenant",
        on_delete=models.CASCADE,
        related_name="workspace_profile",
    )
    mode = models.CharField(
        max_length=24, choices=MODE_CHOICES, default=MODE_MANAGED, db_index=True
    )
    plan_tier = models.CharField(
        max_length=24, choices=PLAN_CHOICES, default=PLAN_MANAGED_SOW
    )
    business_timezone = models.CharField(max_length=64, default="America/Chicago")
    default_contribution_per_unit = models.DecimalField(
        max_digits=10, decimal_places=2, null=True, blank=True
    )
    authorized_pos_retailer = models.CharField(max_length=150, blank=True, default="")
    # Weekly buyer store-report digest (distinct from Tenant.client_weekly_digest).
    buyer_weekly_digest_enabled = models.BooleanField(
        default=False,
        help_text=(
            "When ON, this brand receives the Monday weekly buyer store-report "
            "email (CSV attached). OFF by default — opt-in only."
        ),
    )
    buyer_digest_recipient_emails = models.TextField(
        blank=True,
        default="",
        help_text=(
            "Optional override recipients for the weekly buyer digest "
            "(comma/newline separated). When blank, brand_admin + "
            "finance_reviewer membership emails are used."
        ),
    )
    created_at = models.DateTimeField(auto_now_add=True, editable=False)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self) -> str:
        return f"Workspace({self.tenant_id}, {self.mode})"


class WorkspaceMembership(models.Model):
    """Role-scoped membership inside a brand workspace (internal or agency)."""

    ROLE_BRAND_ADMIN = "brand_admin"
    ROLE_BRAND_REVIEWER = "brand_reviewer"
    ROLE_FINANCE_REVIEWER = "finance_reviewer"
    ROLE_VIEWER = "viewer"
    ROLE_PROVIDER_MANAGER = "provider_manager"
    ROLE_FIELD_CONTRIBUTOR = "field_contributor"
    ROLE_CHOICES = [
        (ROLE_BRAND_ADMIN, "Brand Administrator"),
        (ROLE_BRAND_REVIEWER, "Brand Reviewer"),
        (ROLE_FINANCE_REVIEWER, "Finance Reviewer"),
        (ROLE_VIEWER, "Read-Only Viewer"),
        (ROLE_PROVIDER_MANAGER, "Provider Manager"),
        (ROLE_FIELD_CONTRIBUTOR, "Field Contributor"),
    ]
    BRAND_ROLES = frozenset(
        {
            ROLE_BRAND_ADMIN,
            ROLE_BRAND_REVIEWER,
            ROLE_FINANCE_REVIEWER,
            ROLE_VIEWER,
        }
    )
    PROVIDER_ROLES = frozenset({ROLE_PROVIDER_MANAGER, ROLE_FIELD_CONTRIBUTOR})

    STATUS_ACTIVE = "active"
    STATUS_REVOKED = "revoked"
    STATUS_CHOICES = [
        (STATUS_ACTIVE, "Active"),
        (STATUS_REVOKED, "Revoked"),
    ]

    id = models.BigAutoField(primary_key=True)
    uuid = models.UUIDField(default=uuid7, unique=True, editable=False)
    tenant = models.ForeignKey(
        "tenants.Tenant",
        on_delete=models.CASCADE,
        related_name="workspace_memberships",
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="workspace_memberships",
    )
    provider = models.ForeignKey(
        ProviderOrganization,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="workspace_memberships",
    )
    # Deterministic uniqueness token: "brand" for internal roles,
    # "provider:<id>" for agency-scoped roles.
    membership_scope = models.CharField(max_length=48, default="brand")
    workspace_role = models.CharField(
        max_length=32, choices=ROLE_CHOICES, default=ROLE_VIEWER
    )
    can_approve_recaps = models.BooleanField(default=False)
    can_approve_finance = models.BooleanField(default=False)
    can_approve_spend = models.BooleanField(default=False)
    status = models.CharField(
        max_length=16, choices=STATUS_CHOICES, default=STATUS_ACTIVE, db_index=True
    )
    revoked_at = models.DateTimeField(null=True, blank=True)
    revoked_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="workspace_memberships_revoked",
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="workspace_memberships_created",
    )
    created_at = models.DateTimeField(auto_now_add=True, editable=False)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["tenant", "user", "membership_scope"],
                name="pilot_ws_membership_tenant_user_scope_uniq",
            )
        ]
        indexes = [
            models.Index(
                fields=["user", "status"], name="pilot_ws_member_user_stat_idx"
            ),
            models.Index(
                fields=["tenant", "status"], name="pilot_ws_member_ten_stat_idx"
            ),
        ]

    def save(self, *args, **kwargs):
        if self.provider_id:
            self.membership_scope = f"provider:{self.provider_id}"
        else:
            self.membership_scope = "brand"
        super().save(*args, **kwargs)


class UserActiveWorkspaceContext(models.Model):
    """Explicit active membership context for multi-membership users."""

    id = models.BigAutoField(primary_key=True)
    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="active_workspace_context",
    )
    tenant = models.ForeignKey(
        "tenants.Tenant",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )
    membership = models.ForeignKey(
        WorkspaceMembership,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )
    updated_at = models.DateTimeField(auto_now=True)


class WorkspaceInvitation(models.Model):
    """Invitation for an internal brand teammate or outside field agency."""

    STATUS_PENDING = "pending"
    STATUS_ACCEPTED = "accepted"
    STATUS_REVOKED = "revoked"
    STATUS_EXPIRED = "expired"
    STATUS_CHOICES = [
        (STATUS_PENDING, "Pending"),
        (STATUS_ACCEPTED, "Accepted"),
        (STATUS_REVOKED, "Revoked"),
        (STATUS_EXPIRED, "Expired"),
    ]

    id = models.BigAutoField(primary_key=True)
    uuid = models.UUIDField(default=uuid7, unique=True, editable=False)
    tenant = models.ForeignKey(
        "tenants.Tenant",
        on_delete=models.CASCADE,
        related_name="workspace_invitations",
    )
    email = models.EmailField(db_index=True)
    workspace_role = models.CharField(
        max_length=32,
        choices=WorkspaceMembership.ROLE_CHOICES,
        default=WorkspaceMembership.ROLE_VIEWER,
    )
    provider = models.ForeignKey(
        ProviderOrganization,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="workspace_invitations",
    )
    can_approve_recaps = models.BooleanField(default=False)
    can_approve_finance = models.BooleanField(default=False)
    can_approve_spend = models.BooleanField(default=False)
    token = models.CharField(max_length=64, unique=True, db_index=True)
    status = models.CharField(
        max_length=16, choices=STATUS_CHOICES, default=STATUS_PENDING, db_index=True
    )
    expires_at = models.DateTimeField()
    invited_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="workspace_invitations_sent",
    )
    accepted_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="workspace_invitations_accepted",
    )
    accepted_at = models.DateTimeField(null=True, blank=True)
    revoked_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True, editable=False)
    updated_at = models.DateTimeField(auto_now=True)


class CanonicalVisit(models.Model):
    """Canonical activation/visit record per brand, aligning dashboard,
    imports, cost rollups, POS store-weeks, actions, and Milestone 2 metering."""

    STATUS_SCHEDULED = "scheduled"
    STATUS_IN_PROGRESS = "in_progress"
    STATUS_DRAFT_RECAP = "draft_recap"
    STATUS_NEEDS_REVIEW = "needs_review"
    STATUS_COMPLETED = "completed"
    STATUS_CANCELLED = "cancelled"
    STATUS_ARCHIVED = "archived"
    STATUS_CHOICES = [
        (STATUS_SCHEDULED, "Scheduled"),
        (STATUS_IN_PROGRESS, "In Progress"),
        (STATUS_DRAFT_RECAP, "Draft Recap"),
        (STATUS_NEEDS_REVIEW, "Needs Review"),
        (STATUS_COMPLETED, "Completed"),
        (STATUS_CANCELLED, "Cancelled"),
        (STATUS_ARCHIVED, "Archived"),
    ]

    id = models.BigAutoField(primary_key=True)
    uuid = models.UUIDField(default=uuid7, unique=True, editable=False)
    tenant = models.ForeignKey(
        "tenants.Tenant",
        on_delete=models.CASCADE,
        related_name="canonical_visits",
    )
    event = models.ForeignKey(
        "events.Event",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="canonical_visits",
    )
    request = models.ForeignKey(
        "events.Request",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="canonical_visits",
    )
    primary_provider = models.ForeignKey(
        ProviderOrganization,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="primary_canonical_visits",
    )
    retailer = models.ForeignKey(
        "events.Retailer",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="canonical_visits",
    )
    event_type = models.ForeignKey(
        "events.EventType",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="canonical_visits",
    )
    store_key = models.CharField(max_length=200, blank=True, default="", db_index=True)
    store_label = models.CharField(max_length=255, blank=True, default="")
    chain_key = models.CharField(max_length=120, blank=True, default="", db_index=True)
    chain_label = models.CharField(max_length=200, blank=True, default="")
    store_number = models.CharField(max_length=64, blank=True, default="")
    external_store_id = models.CharField(max_length=100, blank=True, default="")
    address = models.CharField(max_length=300, blank=True, default="")
    city = models.CharField(max_length=120, blank=True, default="")
    state_code = models.CharField(max_length=16, blank=True, default="", db_index=True)
    market = models.CharField(max_length=120, blank=True, default="", db_index=True)
    visit_date = models.DateField(db_index=True)
    start_time = models.DateTimeField(null=True, blank=True)
    end_time = models.DateTimeField(null=True, blank=True)
    # Distinguishes multiple legitimate shifts at the same store on the same day.
    shift_key = models.CharField(max_length=120, default="default")
    status = models.CharField(
        max_length=24, choices=STATUS_CHOICES, default=STATUS_SCHEDULED, db_index=True
    )
    completed_at = models.DateTimeField(null=True, blank=True, db_index=True)
    is_historical_import = models.BooleanField(default=False, db_index=True)
    exclude_from_aggregates = models.BooleanField(default=False, db_index=True)
    co_brand_share_pct = models.DecimalField(
        max_digits=6, decimal_places=2, null=True, blank=True
    )
    counts_toward_allowance = models.BooleanField(default=True)

    samples_distributed = models.IntegerField(null=True, blank=True)
    consumers_sampled = models.IntegerField(null=True, blank=True)
    rep_observed_units = models.IntegerField(null=True, blank=True)
    total_engagements = models.IntegerField(null=True, blank=True)

    # Cached single-pass cost summary for fast store/program rollups.
    cached_supported_cost = models.DecimalField(
        max_digits=12, decimal_places=2, default=Decimal("0.00")
    )
    cached_total_cost = models.DecimalField(
        max_digits=12, decimal_places=2, null=True, blank=True
    )
    cached_cost_incomplete = models.BooleanField(default=True)
    cached_settlement_status = models.CharField(
        max_length=16, default="estimated"
    )
    cached_evidence_status = models.CharField(max_length=16, default="missing")

    created_at = models.DateTimeField(auto_now_add=True, editable=False)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-visit_date", "-id"]
        indexes = [
            models.Index(
                fields=["tenant", "visit_date", "store_key"],
                name="pilot_cvisit_ten_date_store",
            ),
            models.Index(
                fields=["tenant", "status", "completed_at"],
                name="pilot_cvisit_ten_stat_comp",
            ),
        ]


class ProviderAssignment(models.Model):
    """Assigns a program or specific visit to a field agency with a scoped
    mobile submission link."""

    SCOPE_PROGRAM = "program"
    SCOPE_VISIT = "visit"
    SCOPE_CHOICES = [
        (SCOPE_PROGRAM, "Program"),
        (SCOPE_VISIT, "Visit"),
    ]

    STATUS_ASSIGNED = "assigned"
    STATUS_ACCEPTED = "accepted"
    STATUS_DECLINED = "declined"
    STATUS_REVOKED = "revoked"
    STATUS_COMPLETED = "completed"
    STATUS_CHOICES = [
        (STATUS_ASSIGNED, "Assigned"),
        (STATUS_ACCEPTED, "Accepted"),
        (STATUS_DECLINED, "Declined"),
        (STATUS_REVOKED, "Revoked"),
        (STATUS_COMPLETED, "Completed"),
    ]

    id = models.BigAutoField(primary_key=True)
    uuid = models.UUIDField(default=uuid7, unique=True, editable=False)
    tenant = models.ForeignKey(
        "tenants.Tenant",
        on_delete=models.CASCADE,
        related_name="provider_assignments",
    )
    provider = models.ForeignKey(
        ProviderOrganization,
        on_delete=models.CASCADE,
        related_name="assignments",
    )
    scope_type = models.CharField(
        max_length=16, choices=SCOPE_CHOICES, default=SCOPE_VISIT
    )
    event_type = models.ForeignKey(
        "events.EventType",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="provider_assignments",
    )
    request = models.ForeignKey(
        "events.Request",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="provider_assignments",
    )
    event = models.ForeignKey(
        "events.Event",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="provider_assignments",
    )
    canonical_visit = models.ForeignKey(
        CanonicalVisit,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="provider_assignments",
    )
    assigned_owner = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="owned_provider_assignments",
    )
    status = models.CharField(
        max_length=16, choices=STATUS_CHOICES, default=STATUS_ASSIGNED, db_index=True
    )
    submission_code = models.CharField(max_length=32, unique=True, db_index=True)
    due_date = models.DateField(null=True, blank=True)
    notes = models.TextField(blank=True, default="")
    response_note = models.TextField(blank=True, default="")
    responded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="provider_assignments_responded",
    )
    responded_at = models.DateTimeField(null=True, blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="provider_assignments_created",
    )
    created_at = models.DateTimeField(auto_now_add=True, editable=False)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        indexes = [
            models.Index(
                fields=["tenant", "provider", "status"],
                name="pilot_passign_ten_prov_stat",
            )
        ]


class WorkspaceAuditLog(models.Model):
    """Immutable audit log of membership, access, approval, import, cost,
    finance-exception, POS, and store-action changes."""

    id = models.BigAutoField(primary_key=True)
    uuid = models.UUIDField(default=uuid7, unique=True, editable=False)
    tenant = models.ForeignKey(
        "tenants.Tenant",
        on_delete=models.CASCADE,
        related_name="workspace_audit_logs",
    )
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="workspace_audit_logs",
    )
    category = models.CharField(max_length=32, db_index=True)
    action = models.CharField(max_length=64, db_index=True)
    target_type = models.CharField(max_length=64, blank=True, default="")
    target_id = models.CharField(max_length=100, blank=True, default="")
    summary = models.CharField(max_length=500)
    metadata = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True, editable=False, db_index=True)

    class Meta:
        ordering = ["-created_at", "-id"]
        indexes = [
            models.Index(
                fields=["tenant", "category", "-created_at"],
                name="pilot_audit_ten_cat_created",
            )
        ]


class ProductAlias(models.Model):
    """Reviewed SKU / product alias mapping external provider or retailer SKU
    labels onto the brand's canonical events.Product catalog."""

    STATUS_PROPOSED = "proposed"
    STATUS_ACCEPTED = "accepted"
    STATUS_DISMISSED = "dismissed"
    STATUS_CHOICES = [
        (STATUS_PROPOSED, "Proposed"),
        (STATUS_ACCEPTED, "Accepted"),
        (STATUS_DISMISSED, "Dismissed"),
    ]

    id = models.BigAutoField(primary_key=True)
    uuid = models.UUIDField(default=uuid7, unique=True, editable=False)
    tenant = models.ForeignKey(
        "tenants.Tenant",
        on_delete=models.CASCADE,
        related_name="product_aliases",
    )
    provider = models.ForeignKey(
        ProviderOrganization,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="product_aliases",
    )
    alias_key = models.CharField(max_length=160)
    alias_name = models.CharField(max_length=200)
    external_sku_code = models.CharField(max_length=100, blank=True, default="")
    product = models.ForeignKey(
        "events.Product",
        on_delete=models.CASCADE,
        related_name="pilot_aliases",
    )
    status = models.CharField(
        max_length=16, choices=STATUS_CHOICES, default=STATUS_ACCEPTED, db_index=True
    )
    decided_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="product_aliases_decided",
    )
    decided_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True, editable=False)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["tenant", "alias_key"],
                name="pilot_product_alias_tenant_key_uniq",
            )
        ]


class ProviderSourceInclusionRule(models.Model):
    """Controls whether an eligible provider's approved submissions count in
    consolidated brand reporting (while preserving historical exclusions)."""

    id = models.BigAutoField(primary_key=True)
    uuid = models.UUIDField(default=uuid7, unique=True, editable=False)
    tenant = models.ForeignKey(
        "tenants.Tenant",
        on_delete=models.CASCADE,
        related_name="provider_source_rules",
    )
    provider = models.ForeignKey(
        ProviderOrganization,
        on_delete=models.CASCADE,
        related_name="source_inclusion_rules",
    )
    include_in_combined_reporting = models.BooleanField(default=True)
    notes = models.CharField(max_length=300, blank=True, default="")
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="provider_source_rules_updated",
    )
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["tenant", "provider"],
                name="pilot_source_rule_tenant_provider_uniq",
            )
        ]


class VisitSubmissionSource(models.Model):
    """Individual provider or field submission linked to a CanonicalVisit,
    retaining version history and source rows across corrections."""

    KIND_PROVIDER_CSV = "provider_csv"
    KIND_WALKUP_CHECKIN = "walkup_checkin"
    KIND_PORTAL_RECAP = "portal_recap"
    KIND_LEGACY_RECAP = "legacy_recap"
    KIND_CHOICES = [
        (KIND_PROVIDER_CSV, "Provider CSV Import"),
        (KIND_WALKUP_CHECKIN, "Mobile Field Check-In"),
        (KIND_PORTAL_RECAP, "Portal Recap"),
        (KIND_LEGACY_RECAP, "Legacy Recap"),
    ]

    RECAP_DRAFT = "draft"
    RECAP_SUBMITTED = "submitted"
    RECAP_APPROVED = "approved"
    RECAP_EXCLUDED = "excluded"
    RECAP_STATUS_CHOICES = [
        (RECAP_DRAFT, "Draft"),
        (RECAP_SUBMITTED, "Submitted"),
        (RECAP_APPROVED, "Approved"),
        (RECAP_EXCLUDED, "Excluded"),
    ]

    id = models.BigAutoField(primary_key=True)
    uuid = models.UUIDField(default=uuid7, unique=True, editable=False)
    tenant = models.ForeignKey(
        "tenants.Tenant",
        on_delete=models.CASCADE,
        related_name="visit_submission_sources",
    )
    canonical_visit = models.ForeignKey(
        CanonicalVisit,
        on_delete=models.CASCADE,
        related_name="submissions",
    )
    provider = models.ForeignKey(
        ProviderOrganization,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="visit_submissions",
    )
    source_kind = models.CharField(
        max_length=24, choices=KIND_CHOICES, default=KIND_PROVIDER_CSV
    )
    provider_event_id = models.CharField(max_length=120, blank=True, default="")
    custom_recap = models.ForeignKey(
        "recaps.CustomRecap",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="pilot_submission_sources",
    )
    legacy_recap = models.ForeignKey(
        "recaps.Recap",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="pilot_submission_sources",
    )
    recap_status = models.CharField(
        max_length=16, choices=RECAP_STATUS_CHOICES, default=RECAP_SUBMITTED
    )
    exclude_from_aggregates = models.BooleanField(default=False)
    version = models.PositiveIntegerField(default=1)
    row_fingerprint = models.CharField(max_length=64, blank=True, default="")

    samples_distributed = models.IntegerField(null=True, blank=True)
    consumers_sampled = models.IntegerField(null=True, blank=True)
    rep_observed_units = models.IntegerField(null=True, blank=True)
    sku_breakdown = models.JSONField(default=list, blank=True)
    notes = models.TextField(blank=True, default="")

    raw_row = models.JSONField(default=dict, blank=True)
    previous_versions = models.JSONField(default=list, blank=True)

    submitted_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="pilot_visit_submissions",
    )
    created_at = models.DateTimeField(auto_now_add=True, editable=False)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        indexes = [
            models.Index(
                fields=["tenant", "provider", "provider_event_id"],
                name="pilot_vsub_ten_prov_ext_idx",
            )
        ]


class ProviderImportMapping(models.Model):
    """Saved CSV column mapping per brand and provider."""

    id = models.BigAutoField(primary_key=True)
    uuid = models.UUIDField(default=uuid7, unique=True, editable=False)
    tenant = models.ForeignKey(
        "tenants.Tenant",
        on_delete=models.CASCADE,
        related_name="provider_import_mappings",
    )
    provider = models.ForeignKey(
        ProviderOrganization,
        on_delete=models.CASCADE,
        related_name="import_mappings",
    )
    name = models.CharField(max_length=120)
    preset_key = models.CharField(max_length=48, blank=True, default="custom")
    column_map = models.JSONField(default=dict)
    is_default = models.BooleanField(default=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="provider_import_mappings_created",
    )
    created_at = models.DateTimeField(auto_now_add=True, editable=False)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["tenant", "provider", "name"],
                name="pilot_import_map_ten_prov_name_uniq",
            )
        ]


class ProviderImportJob(models.Model):
    """Import history record for committed provider CSV uploads."""

    STATUS_COMMITTED = "committed"
    STATUS_PARTIAL_REVIEW = "partial_review"
    STATUS_FAILED = "failed"
    STATUS_CHOICES = [
        (STATUS_COMMITTED, "Committed"),
        (STATUS_PARTIAL_REVIEW, "Needs Match Review"),
        (STATUS_FAILED, "Failed"),
    ]

    id = models.BigAutoField(primary_key=True)
    uuid = models.UUIDField(default=uuid7, unique=True, editable=False)
    tenant = models.ForeignKey(
        "tenants.Tenant",
        on_delete=models.CASCADE,
        related_name="provider_import_jobs",
    )
    provider = models.ForeignKey(
        ProviderOrganization,
        on_delete=models.CASCADE,
        related_name="import_jobs",
    )
    mapping = models.ForeignKey(
        ProviderImportMapping,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="import_jobs",
    )
    mapping_snapshot = models.JSONField(default=dict)
    filename = models.CharField(max_length=255, blank=True, default="upload.csv")
    file_sha256 = models.CharField(max_length=64, db_index=True)
    idempotency_key = models.CharField(max_length=100, db_index=True)
    is_historical = models.BooleanField(default=False)
    status = models.CharField(
        max_length=24, choices=STATUS_CHOICES, default=STATUS_COMMITTED
    )
    total_rows = models.PositiveIntegerField(default=0)
    created_count = models.PositiveIntegerField(default=0)
    updated_count = models.PositiveIntegerField(default=0)
    unchanged_count = models.PositiveIntegerField(default=0)
    review_count = models.PositiveIntegerField(default=0)
    error_count = models.PositiveIntegerField(default=0)
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="provider_import_jobs",
    )
    created_at = models.DateTimeField(auto_now_add=True, editable=False)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at", "-id"]
        constraints = [
            models.UniqueConstraint(
                fields=["tenant", "provider", "idempotency_key"],
                name="pilot_import_job_idempotency_uniq",
            )
        ]


class ProviderImportRow(models.Model):
    """Per-row outcome or uncertain-match queue entry for a ProviderImportJob."""

    STATUS_CREATED = "created"
    STATUS_UPDATED = "updated"
    STATUS_UNCHANGED = "unchanged"
    STATUS_NEEDS_REVIEW = "needs_review"
    STATUS_ERROR = "error"
    STATUS_CHOICES = [
        (STATUS_CREATED, "Created"),
        (STATUS_UPDATED, "Updated"),
        (STATUS_UNCHANGED, "Unchanged"),
        (STATUS_NEEDS_REVIEW, "Needs Match Review"),
        (STATUS_ERROR, "Error"),
    ]

    id = models.BigAutoField(primary_key=True)
    uuid = models.UUIDField(default=uuid7, unique=True, editable=False)
    job = models.ForeignKey(
        ProviderImportJob,
        on_delete=models.CASCADE,
        related_name="rows",
    )
    tenant = models.ForeignKey(
        "tenants.Tenant",
        on_delete=models.CASCADE,
        related_name="provider_import_rows",
    )
    provider = models.ForeignKey(
        ProviderOrganization,
        on_delete=models.CASCADE,
        related_name="import_rows",
    )
    row_number = models.PositiveIntegerField()
    status = models.CharField(
        max_length=24, choices=STATUS_CHOICES, db_index=True
    )
    error_code = models.CharField(max_length=64, blank=True, default="")
    message = models.CharField(max_length=500, blank=True, default="")
    raw_row = models.JSONField(default=dict)
    normalized_row = models.JSONField(default=dict)
    canonical_visit = models.ForeignKey(
        CanonicalVisit,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="import_rows",
    )
    submission = models.ForeignKey(
        VisitSubmissionSource,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="import_rows",
    )
    resolved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="provider_import_rows_resolved",
    )
    resolved_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True, editable=False)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["row_number", "id"]
        constraints = [
            models.UniqueConstraint(
                fields=["job", "row_number"],
                name="pilot_import_row_job_num_uniq",
            )
        ]


class ProviderAgreement(models.Model):
    """Contracted rate and fee base configuration between a brand and provider."""

    id = models.BigAutoField(primary_key=True)
    uuid = models.UUIDField(default=uuid7, unique=True, editable=False)
    tenant = models.ForeignKey(
        "tenants.Tenant",
        on_delete=models.CASCADE,
        related_name="provider_agreements",
    )
    provider = models.ForeignKey(
        ProviderOrganization,
        on_delete=models.CASCADE,
        related_name="agreements",
    )
    event_type = models.ForeignKey(
        "events.EventType",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="provider_agreements",
    )
    name = models.CharField(max_length=150)
    currency = models.CharField(max_length=8, default="USD")
    contracted_demo_fee = models.DecimalField(
        max_digits=12, decimal_places=2, null=True, blank=True
    )
    included_hours = models.DecimalField(
        max_digits=6, decimal_places=2, default=Decimal("4.00")
    )
    extra_hour_rate = models.DecimalField(
        max_digits=10, decimal_places=2, null=True, blank=True
    )
    mileage_rate = models.DecimalField(
        max_digits=8, decimal_places=3, null=True, blank=True
    )
    admin_fee_percent = models.DecimalField(
        max_digits=6, decimal_places=2, default=Decimal("0.00")
    )
    # List of BrandCostLine categories to which admin_fee_percent applies,
    # e.g. ["product"] for a 5% admin fee on product only.
    admin_fee_bases = models.JSONField(default=list, blank=True)
    tax_percent = models.DecimalField(
        max_digits=6, decimal_places=2, default=Decimal("0.00")
    )
    tax_bases = models.JSONField(default=list, blank=True)
    is_active = models.BooleanField(default=True)
    effective_from = models.DateField(null=True, blank=True)
    effective_to = models.DateField(null=True, blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="provider_agreements_created",
    )
    created_at = models.DateTimeField(auto_now_add=True, editable=False)
    updated_at = models.DateTimeField(auto_now=True)


class SharedCostSource(models.Model):
    """Source charge allocated across multiple visits or participating brands
    without exposing one brand's identity or share to another brand."""

    MODE_AMOUNT = "amount"
    MODE_PERCENTAGE = "percentage"
    MODE_CHOICES = [
        (MODE_AMOUNT, "Explicit Amounts"),
        (MODE_PERCENTAGE, "Percentages"),
    ]

    id = models.BigAutoField(primary_key=True)
    uuid = models.UUIDField(default=uuid7, unique=True, editable=False)
    provider = models.ForeignKey(
        ProviderOrganization,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="shared_cost_sources",
    )
    category = models.CharField(max_length=32)
    source_type = models.CharField(max_length=32, default="invoice_line")
    description = models.CharField(max_length=300, blank=True, default="")
    total_amount = models.DecimalField(max_digits=12, decimal_places=2)
    currency = models.CharField(max_length=8, default="USD")
    allocation_mode = models.CharField(
        max_length=16, choices=MODE_CHOICES, default=MODE_PERCENTAGE
    )
    settlement_status = models.CharField(max_length=16, default="invoiced")
    evidence_status = models.CharField(max_length=16, default="supported")
    invoice_line = models.ForeignKey(
        "billing.InvoiceLineItem",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="pilot_shared_sources",
    )
    receipt_file = models.ForeignKey(
        "recaps.CustomRecapFile",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="pilot_shared_sources",
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="pilot_shared_sources_created",
    )
    created_at = models.DateTimeField(auto_now_add=True, editable=False)


class BrandCostLine(models.Model):
    """A single brand-visible cost or credit line on a CanonicalVisit.
    Never holds internal BA labor pay."""

    CAT_DEMO_FEE = "demo_fee"
    CAT_EXTRA_HOURS = "extra_hours"
    CAT_PRODUCT = "product"
    CAT_MILEAGE = "mileage"
    CAT_PARKING = "parking"
    CAT_SHIPPING = "shipping"
    CAT_RETAILER_FEE = "retailer_fee"
    CAT_SERVICE_ADMIN_FEE = "service_admin_fee"
    CAT_TAX = "tax"
    CAT_CREDIT = "credit"
    CATEGORY_CHOICES = [
        (CAT_DEMO_FEE, "Contracted Demo Fee"),
        (CAT_EXTRA_HOURS, "Billable Extra Hours"),
        (CAT_PRODUCT, "Product"),
        (CAT_MILEAGE, "Mileage"),
        (CAT_PARKING, "Parking"),
        (CAT_SHIPPING, "Shipping"),
        (CAT_RETAILER_FEE, "Retailer Fee"),
        (CAT_SERVICE_ADMIN_FEE, "Service / Admin Fee"),
        (CAT_TAX, "Tax"),
        (CAT_CREDIT, "Credit"),
    ]

    SOURCE_AGREEMENT = "agreement"
    SOURCE_INVOICE_LINE = "invoice_line"
    SOURCE_EXPENSE_RECEIPT = "expense_receipt"
    SOURCE_CREDIT = "credit"
    SOURCE_ALLOCATION = "allocation"
    SOURCE_CHOICES = [
        (SOURCE_AGREEMENT, "Agreement"),
        (SOURCE_INVOICE_LINE, "Invoice Line"),
        (SOURCE_EXPENSE_RECEIPT, "Approved Expense"),
        (SOURCE_CREDIT, "Credit Memo"),
        (SOURCE_ALLOCATION, "Shared Cost Allocation"),
    ]

    SETTLEMENT_ESTIMATED = "estimated"
    SETTLEMENT_INVOICED = "invoiced"
    SETTLEMENT_PAID = "paid"
    SETTLEMENT_CHOICES = [
        (SETTLEMENT_ESTIMATED, "Estimated"),
        (SETTLEMENT_INVOICED, "Invoiced"),
        (SETTLEMENT_PAID, "Paid"),
    ]

    EVIDENCE_MISSING = "missing"
    EVIDENCE_ESTIMATED = "estimated"
    EVIDENCE_SUPPORTED = "supported"
    EVIDENCE_CHOICES = [
        (EVIDENCE_MISSING, "Missing"),
        (EVIDENCE_ESTIMATED, "Estimated"),
        (EVIDENCE_SUPPORTED, "Supported"),
    ]

    id = models.BigAutoField(primary_key=True)
    uuid = models.UUIDField(default=uuid7, unique=True, editable=False)
    tenant = models.ForeignKey(
        "tenants.Tenant",
        on_delete=models.CASCADE,
        related_name="brand_cost_lines",
    )
    canonical_visit = models.ForeignKey(
        CanonicalVisit,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="cost_lines",
    )
    provider = models.ForeignKey(
        ProviderOrganization,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="brand_cost_lines",
    )
    agreement = models.ForeignKey(
        ProviderAgreement,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="cost_lines",
    )
    invoice_line = models.ForeignKey(
        "billing.InvoiceLineItem",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="brand_cost_lines",
    )
    shared_source = models.ForeignKey(
        SharedCostSource,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="allocated_lines",
    )
    category = models.CharField(max_length=32, choices=CATEGORY_CHOICES, db_index=True)
    source_type = models.CharField(
        max_length=32, choices=SOURCE_CHOICES, default=SOURCE_AGREEMENT
    )
    description = models.CharField(max_length=300, blank=True, default="")
    quantity = models.DecimalField(
        max_digits=10, decimal_places=2, null=True, blank=True
    )
    unit_rate = models.DecimalField(
        max_digits=12, decimal_places=3, null=True, blank=True
    )
    # Null means UNKNOWN / MISSING amount (never treated as $0.00).
    # Decimal("0.00") with is_confirmed_zero=True means a confirmed $0 charge.
    amount = models.DecimalField(
        max_digits=12, decimal_places=2, null=True, blank=True
    )
    is_confirmed_zero = models.BooleanField(default=False)
    currency = models.CharField(max_length=8, default="USD")
    allocation_percentage = models.DecimalField(
        max_digits=7, decimal_places=4, null=True, blank=True
    )
    settlement_status = models.CharField(
        max_length=16, choices=SETTLEMENT_CHOICES, default=SETTLEMENT_ESTIMATED
    )
    evidence_status = models.CharField(
        max_length=16, choices=EVIDENCE_CHOICES, default=EVIDENCE_ESTIMATED
    )
    source_reference = models.CharField(max_length=120, blank=True, default="")
    is_auto_generated_fee = models.BooleanField(default=False)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="brand_cost_lines_created",
    )
    created_at = models.DateTimeField(auto_now_add=True, editable=False)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["id"]
        indexes = [
            models.Index(
                fields=["tenant", "canonical_visit"],
                name="pilot_cost_ten_visit_idx",
            )
        ]
        constraints = [
            # Auto-booked recap product-spend lines must be unique per tenant
            # so concurrent OCR + evaluate cannot create duplicate charges.
            models.UniqueConstraint(
                fields=["tenant", "source_reference"],
                condition=models.Q(
                    source_reference__startswith="recap-product-spend:"
                ),
                name="pilot_cost_recap_spend_src_uniq",
            ),
        ]


class CostEvidenceLink(models.Model):
    """Links a receipt file or OCR reading to a BrandCostLine as supporting
    proof for the SAME charge (preventing double-counting across systems)."""

    id = models.BigAutoField(primary_key=True)
    uuid = models.UUIDField(default=uuid7, unique=True, editable=False)
    cost_line = models.ForeignKey(
        BrandCostLine,
        on_delete=models.CASCADE,
        related_name="evidence_links",
    )
    recap_file = models.ForeignKey(
        "recaps.CustomRecapFile",
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="pilot_cost_evidence_links",
    )
    receipt_reading = models.ForeignKey(
        "recaps.ReceiptReading",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="pilot_cost_evidence_links",
    )
    proof_url = models.CharField(max_length=500, blank=True, default="")
    label = models.CharField(max_length=200, blank=True, default="")
    verified_amount = models.DecimalField(
        max_digits=12, decimal_places=2, null=True, blank=True
    )
    is_itemized = models.BooleanField(default=True)
    linked_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="pilot_cost_evidence_linked",
    )
    created_at = models.DateTimeField(auto_now_add=True, editable=False)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["receipt_reading"],
                condition=models.Q(receipt_reading__isnull=False),
                name="pilot_cost_evidence_reading_uniq",
            ),
        ]


class FinanceException(models.Model):
    """Assignable financial exception queue item, decoupled from operational
    recap approval."""

    TYPE_MISSING_RATE = "missing_rate_or_amount"
    TYPE_MISSING_PROOF = "missing_proof"
    TYPE_UNITEMIZED_PROOF = "unitemized_proof"
    TYPE_UNMATCHED_TXN = "unmatched_transaction"
    TYPE_SUSPECTED_DUPLICATE = "suspected_duplicate"
    TYPE_ALLOCATION_DISCREPANCY = "allocation_discrepancy"
    TYPE_CHOICES = [
        (TYPE_MISSING_RATE, "Missing Rate or Amount"),
        (TYPE_MISSING_PROOF, "Missing Proof"),
        (TYPE_UNITEMIZED_PROOF, "Unitemized Proof"),
        (TYPE_UNMATCHED_TXN, "Unmatched Transaction"),
        (TYPE_SUSPECTED_DUPLICATE, "Suspected Duplicate"),
        (TYPE_ALLOCATION_DISCREPANCY, "Allocation Discrepancy"),
    ]

    SEV_HIGH = "high"
    SEV_MEDIUM = "medium"
    SEV_LOW = "low"
    SEVERITY_CHOICES = [
        (SEV_HIGH, "High"),
        (SEV_MEDIUM, "Medium"),
        (SEV_LOW, "Low"),
    ]

    STATUS_OPEN = "open"
    STATUS_CLARIFICATION = "clarification_requested"
    STATUS_RESOLVED = "resolved"
    STATUS_EXCEPTION_GRANTED = "exception_granted"
    STATUS_AUTO_CLEARED = "auto_cleared"
    STATUS_CHOICES = [
        (STATUS_OPEN, "Open"),
        (STATUS_CLARIFICATION, "Clarification Requested"),
        (STATUS_RESOLVED, "Resolved"),
        (STATUS_EXCEPTION_GRANTED, "Exception Granted"),
        (STATUS_AUTO_CLEARED, "Auto-Cleared"),
    ]

    id = models.BigAutoField(primary_key=True)
    uuid = models.UUIDField(default=uuid7, unique=True, editable=False)
    tenant = models.ForeignKey(
        "tenants.Tenant",
        on_delete=models.CASCADE,
        related_name="finance_exceptions",
    )
    canonical_visit = models.ForeignKey(
        CanonicalVisit,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="finance_exceptions",
    )
    cost_line = models.ForeignKey(
        BrandCostLine,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="finance_exceptions",
    )
    shared_source = models.ForeignKey(
        SharedCostSource,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="finance_exceptions",
    )
    provider = models.ForeignKey(
        ProviderOrganization,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="finance_exceptions",
    )
    recap_file = models.ForeignKey(
        "recaps.CustomRecapFile",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="finance_exceptions",
    )
    invoice_line = models.ForeignKey(
        "billing.InvoiceLineItem",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="finance_exceptions",
    )
    exception_type = models.CharField(
        max_length=32, choices=TYPE_CHOICES, db_index=True
    )
    severity = models.CharField(
        max_length=12, choices=SEVERITY_CHOICES, default=SEV_MEDIUM
    )
    status = models.CharField(
        max_length=24, choices=STATUS_CHOICES, default=STATUS_OPEN, db_index=True
    )
    affected_amount = models.DecimalField(
        max_digits=12, decimal_places=2, null=True, blank=True
    )
    source_label = models.CharField(max_length=200, blank=True, default="")
    explanation = models.TextField()
    dedupe_key = models.CharField(max_length=140)
    evidence_fingerprint = models.CharField(max_length=64, blank=True, default="")
    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="assigned_finance_exceptions",
    )
    clarification_note = models.TextField(blank=True, default="")
    resolution_reason = models.TextField(blank=True, default="")
    resolved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="resolved_finance_exceptions",
    )
    resolved_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True, editable=False)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at", "-id"]
        constraints = [
            models.UniqueConstraint(
                fields=["tenant", "exception_type", "dedupe_key"],
                name="pilot_fin_exc_ten_type_key_uniq",
            )
        ]


class FinanceExceptionAudit(models.Model):
    """Audit trail of every state, owner, proof, or match change on a
    FinanceException."""

    id = models.BigAutoField(primary_key=True)
    uuid = models.UUIDField(default=uuid7, unique=True, editable=False)
    exception = models.ForeignKey(
        FinanceException,
        on_delete=models.CASCADE,
        related_name="audits",
    )
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="finance_exception_audits",
    )
    action = models.CharField(max_length=48)
    note = models.TextField(blank=True, default="")
    before_state = models.JSONField(default=dict, blank=True)
    after_state = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True, editable=False)

    class Meta:
        ordering = ["-created_at", "-id"]


class RetailerSalesImport(models.Model):
    """Permissioned retailer POS sales feed import (store x SKU x week),
    supporting clean version replacement on reimport."""

    COMPLETENESS_COMPLETE = "complete"
    COMPLETENESS_PARTIAL = "partial"
    COMPLETENESS_DELAYED = "delayed"
    COMPLETENESS_CHOICES = [
        (COMPLETENESS_COMPLETE, "Complete Feed"),
        (COMPLETENESS_PARTIAL, "Partial Feed"),
        (COMPLETENESS_DELAYED, "Delayed Feed"),
    ]

    id = models.BigAutoField(primary_key=True)
    uuid = models.UUIDField(default=uuid7, unique=True, editable=False)
    tenant = models.ForeignKey(
        "tenants.Tenant",
        on_delete=models.CASCADE,
        related_name="retailer_sales_imports",
    )
    retailer_banner = models.CharField(max_length=150, db_index=True)
    retailer = models.ForeignKey(
        "events.Retailer",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="retailer_sales_imports",
    )
    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="retailer_sales_imports",
    )
    period_start = models.DateField()
    period_end = models.DateField()
    version = models.PositiveIntegerField(default=1)
    is_active_version = models.BooleanField(default=True, db_index=True)
    superseded_by = models.ForeignKey(
        "self",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="supersedes",
    )
    feed_completeness = models.CharField(
        max_length=16,
        choices=COMPLETENESS_CHOICES,
        default=COMPLETENESS_COMPLETE,
    )
    filename = models.CharField(max_length=255, blank=True, default="pos.csv")
    file_sha256 = models.CharField(max_length=64, db_index=True)
    mapping_snapshot = models.JSONField(default=dict, blank=True)
    total_rows = models.PositiveIntegerField(default=0)
    matched_rows = models.PositiveIntegerField(default=0)
    unmatched_rows = models.PositiveIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True, editable=False)

    class Meta:
        ordering = ["-created_at", "-id"]
        indexes = [
            models.Index(
                fields=["tenant", "is_active_version", "period_start"],
                name="pilot_pos_imp_ten_act_per_idx",
            )
        ]


class RetailerSalesRow(models.Model):
    """Store x SKU x week register sales row from a RetailerSalesImport."""

    MATCH_MATCHED = "matched"
    MATCH_UNMATCHED_STORE = "unmatched_store"
    MATCH_UNMATCHED_PRODUCT = "unmatched_product"
    MATCH_UNMATCHED_DATE = "unmatched_date"
    MATCH_INVALID_UNITS = "invalid_units"
    MATCH_CHOICES = [
        (MATCH_MATCHED, "Matched"),
        (MATCH_UNMATCHED_STORE, "Unmatched Store"),
        (MATCH_UNMATCHED_PRODUCT, "Unmatched Product"),
        (MATCH_UNMATCHED_DATE, "Invalid Date"),
        (MATCH_INVALID_UNITS, "Invalid Units"),
    ]

    id = models.BigAutoField(primary_key=True)
    uuid = models.UUIDField(default=uuid7, unique=True, editable=False)
    sales_import = models.ForeignKey(
        RetailerSalesImport,
        on_delete=models.CASCADE,
        related_name="rows",
    )
    tenant = models.ForeignKey(
        "tenants.Tenant",
        on_delete=models.CASCADE,
        related_name="retailer_sales_rows",
    )
    row_number = models.PositiveIntegerField()
    raw_retailer = models.CharField(max_length=150, blank=True, default="")
    raw_store_id = models.CharField(max_length=100, blank=True, default="")
    raw_store_name = models.CharField(max_length=255, blank=True, default="")
    raw_sku = models.CharField(max_length=200, blank=True, default="")
    raw_pack_size = models.CharField(max_length=64, blank=True, default="")
    raw_week_start = models.CharField(max_length=64, blank=True, default="")
    raw_week_end = models.CharField(max_length=64, blank=True, default="")
    raw_row = models.JSONField(default=dict)

    store_key = models.CharField(max_length=200, blank=True, default="", db_index=True)
    store_label = models.CharField(max_length=255, blank=True, default="")
    chain_key = models.CharField(max_length=120, blank=True, default="")
    retailer_store = models.ForeignKey(
        "events.Retailer",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="pos_sales_rows",
    )
    product = models.ForeignKey(
        "events.Product",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="pos_sales_rows",
    )
    pack_size = models.CharField(max_length=64, blank=True, default="")
    week_start = models.DateField(null=True, blank=True, db_index=True)
    week_end = models.DateField(null=True, blank=True)

    register_units = models.IntegerField(null=True, blank=True)
    is_explicit_zero = models.BooleanField(default=False)
    sales_dollars = models.DecimalField(
        max_digits=12, decimal_places=2, null=True, blank=True
    )

    baseline_units = models.DecimalField(
        max_digits=12, decimal_places=2, null=True, blank=True
    )
    baseline_method = models.CharField(max_length=64, blank=True, default="")
    baseline_window_label = models.CharField(max_length=120, blank=True, default="")
    baseline_sample_size = models.PositiveIntegerField(default=0)

    has_promotion = models.BooleanField(default=False)
    has_pack_change = models.BooleanField(default=False)
    has_stockout = models.BooleanField(default=False)

    match_status = models.CharField(
        max_length=24, choices=MATCH_CHOICES, default=MATCH_MATCHED, db_index=True
    )
    match_note = models.CharField(max_length=300, blank=True, default="")
    resolved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="pos_rows_resolved",
    )
    resolved_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True, editable=False)

    class Meta:
        ordering = ["row_number", "id"]
        indexes = [
            models.Index(
                fields=["tenant", "store_key", "product", "week_start"],
                name="pilot_pos_row_lookup_idx",
            )
        ]


class CrewPurchaseAdjustment(models.Model):
    """Links a field/crew purchase transaction to a specific retailer/store/SKU/week
    so adjusted register units subtract only verified in-period crew purchases."""

    id = models.BigAutoField(primary_key=True)
    uuid = models.UUIDField(default=uuid7, unique=True, editable=False)
    tenant = models.ForeignKey(
        "tenants.Tenant",
        on_delete=models.CASCADE,
        related_name="crew_purchase_adjustments",
    )
    canonical_visit = models.ForeignKey(
        CanonicalVisit,
        on_delete=models.CASCADE,
        related_name="crew_purchase_adjustments",
    )
    product = models.ForeignKey(
        "events.Product",
        on_delete=models.CASCADE,
        related_name="crew_purchase_adjustments",
    )
    store_key = models.CharField(max_length=200, db_index=True)
    retailer_banner = models.CharField(max_length=150, blank=True, default="")
    week_start = models.DateField(db_index=True)
    purchase_date = models.DateField()
    units_purchased = models.PositiveIntegerField()
    receipt_reading = models.ForeignKey(
        "recaps.ReceiptReading",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="crew_purchase_adjustments",
    )
    recap_file = models.ForeignKey(
        "recaps.CustomRecapFile",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="crew_purchase_adjustments",
    )
    cost_line = models.ForeignKey(
        BrandCostLine,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="crew_purchase_adjustments",
    )
    transaction_ref = models.CharField(max_length=120, blank=True, default="")
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="crew_purchase_adjustments_created",
    )
    created_at = models.DateTimeField(auto_now_add=True, editable=False)


class StoreRecommendationAction(models.Model):
    """Durable store action (Rebook, Hold, Restock) created from a published
    BrandInsight, preserving decision-date figures across refreshes and
    provider changes."""

    ACTION_REBOOK = "rebook"
    ACTION_HOLD = "hold"
    ACTION_RESTOCK = "restock"
    ACTION_CHOICES = [
        (ACTION_REBOOK, "Rebook"),
        (ACTION_HOLD, "Hold"),
        (ACTION_RESTOCK, "Restock"),
    ]

    STATUS_ASSIGNED = "assigned"
    STATUS_ACCEPTED = "accepted"
    STATUS_DECLINED = "declined"
    STATUS_REASSIGNED = "reassigned"
    STATUS_ON_HOLD = "on_hold"
    STATUS_AWAITING_VERIFICATION = "awaiting_verification"
    STATUS_COMPLETED = "completed"
    STATUS_CANCELLED = "cancelled"
    STATUS_CHOICES = [
        (STATUS_ASSIGNED, "Assigned"),
        (STATUS_ACCEPTED, "Accepted"),
        (STATUS_DECLINED, "Declined"),
        (STATUS_REASSIGNED, "Reassigned"),
        (STATUS_ON_HOLD, "On Hold"),
        (STATUS_AWAITING_VERIFICATION, "Awaiting Verification"),
        (STATUS_COMPLETED, "Completed"),
        (STATUS_CANCELLED, "Cancelled"),
    ]

    VERIFY_NONE = ""
    VERIFY_NOTE = "note"
    VERIFY_PHOTO = "photo"
    VERIFY_FOLLOWUP_VISIT = "followup_visit"
    VERIFY_CHOICES = [
        (VERIFY_NONE, "Not Verified Yet"),
        (VERIFY_NOTE, "Verified by Note"),
        (VERIFY_PHOTO, "Verified by Shelf Photo"),
        (VERIFY_FOLLOWUP_VISIT, "Verified by Follow-up Visit"),
    ]

    id = models.BigAutoField(primary_key=True)
    uuid = models.UUIDField(default=uuid7, unique=True, editable=False)
    tenant = models.ForeignKey(
        "tenants.Tenant",
        on_delete=models.CASCADE,
        related_name="store_recommendation_actions",
    )
    brand_insight = models.ForeignKey(
        "brand_insights.BrandInsight",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="store_actions",
    )
    finding_id_snapshot = models.CharField(max_length=64, db_index=True)
    finding_code = models.CharField(max_length=40)
    finding_scope_key = models.CharField(max_length=200)
    store_key = models.CharField(max_length=200, db_index=True)
    store_label = models.CharField(max_length=255)
    retailer = models.ForeignKey(
        "events.Retailer",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="store_recommendation_actions",
    )
    action_type = models.CharField(
        max_length=16, choices=ACTION_CHOICES, db_index=True
    )
    idempotency_key = models.CharField(max_length=140)
    status = models.CharField(
        max_length=24, choices=STATUS_CHOICES, default=STATUS_ASSIGNED, db_index=True
    )
    decision_snapshot = models.JSONField(default=dict)

    provider = models.ForeignKey(
        ProviderOrganization,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="store_actions",
    )
    previous_provider = models.ForeignKey(
        ProviderOrganization,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="reassigned_store_actions",
    )
    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="owned_store_actions",
    )
    spend_approved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="approved_store_actions",
    )
    budget_amount = models.DecimalField(
        max_digits=12, decimal_places=2, null=True, blank=True
    )
    target_date = models.DateField(null=True, blank=True)
    due_date = models.DateField(null=True, blank=True)

    hold_reason = models.TextField(blank=True, default="")
    next_review_date = models.DateField(null=True, blank=True)

    provider_response_status = models.CharField(
        max_length=24, default="pending"
    )
    provider_response_note = models.TextField(blank=True, default="")
    provider_responded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="responded_store_actions",
    )
    provider_responded_at = models.DateTimeField(null=True, blank=True)

    restock_verification_method = models.CharField(
        max_length=24, choices=VERIFY_CHOICES, blank=True, default=VERIFY_NONE
    )
    restock_verification_note = models.TextField(blank=True, default="")
    restock_photo_file = models.ForeignKey(
        "recaps.CustomRecapFile",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="verified_restock_actions",
    )
    restock_verified_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="verified_restock_actions",
    )
    restock_verified_at = models.DateTimeField(null=True, blank=True)

    linked_request = models.ForeignKey(
        "events.Request",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="originating_store_actions",
    )
    provider_assignment = models.ForeignKey(
        ProviderAssignment,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="originating_store_actions",
    )
    outcome_visit = models.ForeignKey(
        CanonicalVisit,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="originating_store_actions",
    )
    outcome_recap = models.ForeignKey(
        "recaps.CustomRecap",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="originating_store_actions",
    )
    outcome_summary = models.JSONField(default=dict, blank=True)
    history_log = models.JSONField(default=list, blank=True)

    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="created_store_actions",
    )
    created_at = models.DateTimeField(auto_now_add=True, editable=False)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at", "-id"]
        constraints = [
            models.UniqueConstraint(
                fields=["tenant", "idempotency_key"],
                name="pilot_store_action_ten_idem_uniq",
            )
        ]
        indexes = [
            models.Index(
                fields=["tenant", "store_key", "-created_at"],
                name="pilot_saction_ten_store_idx",
            )
        ]


class WorkspaceSharedLink(models.Model):
    """Revocable shared link for a canonical visit recap or weekly buyer report.
    Automatically invalidated if explicitly revoked or if the creator's
    workspace membership on the brand is revoked."""

    RESOURCE_VISIT = "visit"
    RESOURCE_BUYER_REPORT = "buyer_report"
    RESOURCE_RECAP = "recap"
    RESOURCE_CHOICES = [
        (RESOURCE_VISIT, "Canonical Visit Recap"),
        (RESOURCE_BUYER_REPORT, "Weekly Buyer Store Report"),
        (RESOURCE_RECAP, "Custom Recap"),
    ]

    id = models.BigAutoField(primary_key=True)
    uuid = models.UUIDField(default=uuid7, unique=True, editable=False)
    tenant = models.ForeignKey(
        "tenants.Tenant",
        on_delete=models.CASCADE,
        related_name="workspace_shared_links",
    )
    token = models.CharField(max_length=80, unique=True, db_index=True)
    resource_type = models.CharField(
        max_length=24, choices=RESOURCE_CHOICES, default=RESOURCE_VISIT
    )
    canonical_visit = models.ForeignKey(
        CanonicalVisit,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="shared_links",
    )
    custom_recap = models.ForeignKey(
        "recaps.CustomRecap",
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="workspace_shared_links",
    )
    report_params = models.JSONField(default=dict, blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="workspace_shared_links_created",
    )
    created_by_membership = models.ForeignKey(
        WorkspaceMembership,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="shared_links_created",
    )
    expires_at = models.DateTimeField()
    revoked_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True, editable=False)

# Milestone 2 — Self-Service Onboarding, Plan Entitlements & Software Billing
# ---------------------------------------------------------------------------


class PlanCatalogEntry(models.Model):
    """Centralized software plan catalog so Ignite can test price, included
    completed-visit bands, effective dates, and feature entitlements without
    touching agency service invoices or BA payouts."""

    id = models.BigAutoField(primary_key=True)
    uuid = models.UUIDField(default=uuid7, unique=True, editable=False)
    plan_code = models.CharField(max_length=24, unique=True, db_index=True)
    display_name = models.CharField(max_length=80)
    tagline = models.CharField(max_length=240, blank=True, default="")
    currency = models.CharField(max_length=8, default="USD")
    monthly_price_usd = models.DecimalField(
        max_digits=10, decimal_places=2, default=Decimal("0.00")
    )
    included_completed_visits = models.PositiveIntegerField(
        null=True, blank=True
    )
    is_quote_only = models.BooleanField(default=False)
    is_self_serve_selectable = models.BooleanField(default=True)
    stripe_price_id = models.CharField(max_length=120, blank=True, default="")

    max_active_programs = models.PositiveIntegerField(null=True, blank=True)
    max_operating_providers = models.PositiveIntegerField(null=True, blank=True)
    max_pos_retailers = models.PositiveIntegerField(null=True, blank=True)

    allows_branded_forms = models.BooleanField(default=False)
    allows_approvals_and_reminders = models.BooleanField(default=False)
    allows_exports_and_weekly_reporting = models.BooleanField(default=False)
    allows_store_recommendations = models.BooleanField(default=False)
    allows_provider_csv_imports = models.BooleanField(default=False)
    allows_brand_cost_tracking = models.BooleanField(default=False)
    allows_finance_review_queue = models.BooleanField(default=False)
    allows_retailer_pos_measurement = models.BooleanField(default=False)
    allows_tracked_store_actions = models.BooleanField(default=False)

    effective_from = models.DateField(null=True, blank=True)
    effective_to = models.DateField(null=True, blank=True)
    sort_order = models.PositiveSmallIntegerField(default=10)
    is_active = models.BooleanField(default=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["sort_order", "id"]

    def __str__(self) -> str:
        return f"{self.display_name} ({self.plan_code})"


class BrandSubscription(models.Model):
    """Per-brand software subscription & contract/pilot entitlement record.
    Kept strictly separate from billing.Invoice (agency service invoices),
    receipts.ConsumerReceipt (shopper rebates), and jobs.AmbassadorJob."""

    STATUS_FREE_ACTIVE = "free_active"
    STATUS_PILOT_ACTIVE = "pilot_active"
    STATUS_PILOT_EXPIRED = "pilot_expired"
    STATUS_CHECKOUT_PENDING = "checkout_pending"
    STATUS_ACTIVE = "active"
    STATUS_PAST_DUE = "past_due"
    STATUS_CANCEL_AT_PERIOD_END = "cancel_at_period_end"
    STATUS_CANCELED = "canceled"
    STATUS_MANAGED_SOW = "managed_sow"
    STATUS_CHOICES = [
        (STATUS_FREE_ACTIVE, "Free Active"),
        (STATUS_PILOT_ACTIVE, "Paid Pilot Active"),
        (STATUS_PILOT_EXPIRED, "Pilot Ended"),
        (STATUS_CHECKOUT_PENDING, "Checkout Pending"),
        (STATUS_ACTIVE, "Active Subscription"),
        (STATUS_PAST_DUE, "Past Due — Payment Failed"),
        (STATUS_CANCEL_AT_PERIOD_END, "Cancels at Period End"),
        (STATUS_CANCELED, "Canceled"),
        (STATUS_MANAGED_SOW, "Managed Service SOW"),
    ]

    PAY_NONE = "none"
    PAY_SUCCEEDED = "succeeded"
    PAY_FAILED = "failed"
    PAY_REQUIRES_ACTION = "requires_action"
    PAY_STATUS_CHOICES = [
        (PAY_NONE, "No Charge Yet"),
        (PAY_SUCCEEDED, "Succeeded"),
        (PAY_FAILED, "Payment Failed"),
        (PAY_REQUIRES_ACTION, "Requires Action"),
    ]

    id = models.BigAutoField(primary_key=True)
    uuid = models.UUIDField(default=uuid7, unique=True, editable=False)
    tenant = models.OneToOneField(
        "tenants.Tenant",
        on_delete=models.CASCADE,
        related_name="brand_subscription",
    )
    plan_code = models.CharField(
        max_length=24,
        choices=WorkspaceProfile.PLAN_CHOICES,
        default=WorkspaceProfile.PLAN_MANAGED_SOW,
        db_index=True,
    )
    pending_plan_code = models.CharField(max_length=24, blank=True, default="")
    addon_plan_code = models.CharField(max_length=24, blank=True, default="")
    status = models.CharField(
        max_length=28,
        choices=STATUS_CHOICES,
        default=STATUS_MANAGED_SOW,
        db_index=True,
    )

    # Manually provisioned $1,500, 60-day Pro pilot support
    is_pilot = models.BooleanField(default=False)
    pilot_fee_usd = models.DecimalField(
        max_digits=10, decimal_places=2, null=True, blank=True
    )
    pilot_starts_at = models.DateTimeField(null=True, blank=True)
    pilot_ends_at = models.DateTimeField(null=True, blank=True)

    # Explicit buyer acceptance before recurring charges begin
    recurring_billing_accepted_at = models.DateTimeField(null=True, blank=True)
    recurring_billing_accepted_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="accepted_brand_subscriptions",
    )

    # Contract & volume-band overrides
    custom_monthly_price_usd = models.DecimalField(
        max_digits=10, decimal_places=2, null=True, blank=True
    )
    custom_included_visits_monthly = models.PositiveIntegerField(
        null=True, blank=True
    )
    agreed_volume_band_visits = models.PositiveIntegerField(
        null=True, blank=True
    )
    overage_agreement_approved = models.BooleanField(default=False)
    overage_unit_price_usd = models.DecimalField(
        max_digits=10, decimal_places=2, null=True, blank=True
    )
    overage_approved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="approved_overage_subscriptions",
    )
    overage_approved_at = models.DateTimeField(null=True, blank=True)
    custom_max_providers = models.PositiveIntegerField(null=True, blank=True)
    custom_max_pos_retailers = models.PositiveIntegerField(
        null=True, blank=True
    )
    contract_notes = models.TextField(blank=True, default="")

    # Stripe Billing state (test mode by default)
    stripe_mode = models.CharField(max_length=12, default="test")
    stripe_customer_id = models.CharField(
        max_length=120, blank=True, default="", db_index=True
    )
    stripe_subscription_id = models.CharField(
        max_length=120, blank=True, default="", db_index=True
    )
    stripe_latest_invoice_id = models.CharField(
        max_length=120, blank=True, default=""
    )
    current_period_start = models.DateTimeField(null=True, blank=True)
    current_period_end = models.DateTimeField(null=True, blank=True)
    cancel_at_period_end = models.BooleanField(default=False)
    canceled_at = models.DateTimeField(null=True, blank=True)
    last_payment_status = models.CharField(
        max_length=24, choices=PAY_STATUS_CHOICES, default=PAY_NONE
    )
    last_payment_error = models.CharField(
        max_length=500, blank=True, default=""
    )
    # Monotonic watermark of newest applied Stripe event timestamp so delayed /
    # out-of-order webhooks never overwrite newer state.
    last_stripe_event_created = models.BigIntegerField(default=0)

    billing_owner = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="owned_brand_subscriptions",
    )
    created_at = models.DateTimeField(auto_now_add=True, editable=False)
    updated_at = models.DateTimeField(auto_now=True)


class StripeCheckoutRecord(models.Model):
    """Hosted Stripe Checkout session + explicit pre-confirmation charge/date
    disclosure record."""

    STATUS_OPEN = "open"
    STATUS_COMPLETED = "completed"
    STATUS_EXPIRED = "expired"
    STATUS_CHOICES = [
        (STATUS_OPEN, "Open"),
        (STATUS_COMPLETED, "Completed"),
        (STATUS_EXPIRED, "Expired"),
    ]

    id = models.BigAutoField(primary_key=True)
    uuid = models.UUIDField(default=uuid7, unique=True, editable=False)
    tenant = models.ForeignKey(
        "tenants.Tenant",
        on_delete=models.CASCADE,
        related_name="stripe_checkout_records",
    )
    initiated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="stripe_checkout_records",
    )
    target_plan_code = models.CharField(max_length=24)
    quoted_monthly_price_usd = models.DecimalField(
        max_digits=10, decimal_places=2
    )
    quoted_included_visits = models.PositiveIntegerField()
    effective_summary = models.CharField(max_length=400, blank=True, default="")
    idempotency_key = models.CharField(max_length=140, db_index=True)
    stripe_session_id = models.CharField(
        max_length=140, unique=True, db_index=True
    )
    checkout_url = models.CharField(max_length=600)
    mode = models.CharField(max_length=12, default="test")
    status = models.CharField(
        max_length=16, choices=STATUS_CHOICES, default=STATUS_OPEN
    )
    explicit_terms_accepted = models.BooleanField(default=False)
    accepted_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True, editable=False)

    class Meta:
        ordering = ["-created_at", "-id"]
        constraints = [
            models.UniqueConstraint(
                fields=["tenant", "idempotency_key"],
                name="pilot_stripe_checkout_ten_idem_uniq",
            )
        ]


class StripeWebhookEvent(models.Model):
    """Idempotent ledger of verified Stripe Billing webhook deliveries."""

    PROC_PROCESSED = "processed"
    PROC_DUPLICATE = "ignored_duplicate"
    PROC_STALE = "ignored_stale_out_of_order"
    PROC_UNMATCHED = "ignored_unmatched"
    PROC_FAILED = "failed"
    PROC_CHOICES = [
        (PROC_PROCESSED, "Processed"),
        (PROC_DUPLICATE, "Ignored Duplicate"),
        (PROC_STALE, "Ignored Out-of-Order"),
        (PROC_UNMATCHED, "Ignored Unmatched"),
        (PROC_FAILED, "Failed"),
    ]

    id = models.BigAutoField(primary_key=True)
    uuid = models.UUIDField(default=uuid7, unique=True, editable=False)
    stripe_event_id = models.CharField(
        max_length=140, unique=True, db_index=True
    )
    event_type = models.CharField(max_length=100, db_index=True)
    stripe_created_ts = models.BigIntegerField(default=0)
    livemode = models.BooleanField(default=False)
    tenant = models.ForeignKey(
        "tenants.Tenant",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="stripe_webhook_events",
    )
    processing_status = models.CharField(
        max_length=32, choices=PROC_CHOICES, default=PROC_PROCESSED
    )
    note = models.CharField(max_length=300, blank=True, default="")
    payload = models.JSONField(default=dict)
    processed_at = models.DateTimeField(auto_now_add=True, editable=False)

    class Meta:
        ordering = ["-processed_at", "-id"]


class BrandOnboardingProgress(models.Model):
    """Idempotent progress state for Feature 7 ('Use Spark with my team')
    self-service brand onboarding."""

    id = models.BigAutoField(primary_key=True)
    uuid = models.UUIDField(default=uuid7, unique=True, editable=False)
    tenant = models.OneToOneField(
        "tenants.Tenant",
        on_delete=models.CASCADE,
        related_name="onboarding_progress",
    )
    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="owned_brand_onboardings",
    )
    starter_template_key = models.CharField(
        max_length=48, blank=True, default=""
    )
    starter_event_type = models.ForeignKey(
        "events.EventType",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )
    starter_recap_template = models.ForeignKey(
        "recaps.CustomRecapTemplate",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )
    default_provider = models.ForeignKey(
        ProviderOrganization,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )
    starter_assignment = models.ForeignKey(
        ProviderAssignment,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )
    first_visit = models.ForeignKey(
        CanonicalVisit,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )
    first_shared_link = models.ForeignKey(
        WorkspaceSharedLink,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )

    step_workspace_created = models.BooleanField(default=True)
    step_program_selected = models.BooleanField(default=False)
    step_contributor_invited = models.BooleanField(default=False)
    step_mobile_link_opened = models.BooleanField(default=False)
    step_first_visit_completed = models.BooleanField(default=False)
    # Marketing UTM attribution captured on /onboarding (utm_source…utm_term).
    marketing_attribution = models.JSONField(default=dict, blank=True)
    close_lead_id = models.CharField(max_length=80, blank=True, default="")
    completed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True, editable=False)
    updated_at = models.DateTimeField(auto_now=True)




class PosFeedConfig(models.Model):
    """Per-banner automated POS feed configuration (email-drop / scheduled pull).

    Brands mint an ingest token and forward weekly portal CSVs to
    ``POST /api/public/pos-feed/<token>`` (or a future sync+ mailbox). Cron
    processes pending ``PosFeedDrop`` rows through ``import_retailer_pos_csv``.
    """

    SOURCE_EMAIL_DROP = "email_drop"
    SOURCE_MANUAL_UPLOAD = "manual_upload"
    SOURCE_SFTP = "sftp"
    SOURCE_CHOICES = [
        (SOURCE_EMAIL_DROP, "Email / Webhook Drop"),
        (SOURCE_MANUAL_UPLOAD, "Manual Upload Assist"),
        (SOURCE_SFTP, "Scheduled SFTP / Connector"),
    ]

    id = models.BigAutoField(primary_key=True)
    uuid = models.UUIDField(default=uuid7, unique=True, editable=False)
    tenant = models.ForeignKey(
        "tenants.Tenant",
        on_delete=models.CASCADE,
        related_name="pos_feed_configs",
    )
    retailer_banner = models.CharField(max_length=150, db_index=True)
    preset_key = models.CharField(max_length=64, blank=True, default="")
    source_kind = models.CharField(
        max_length=24, choices=SOURCE_CHOICES, default=SOURCE_EMAIL_DROP
    )
    ingest_token = models.CharField(max_length=64, unique=True, db_index=True)
    enabled = models.BooleanField(default=True, db_index=True)
    default_feed_completeness = models.CharField(
        max_length=16,
        default=RetailerSalesImport.COMPLETENESS_COMPLETE,
    )
    notes = models.TextField(blank=True, default="")
    last_ingested_at = models.DateTimeField(null=True, blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="pos_feed_configs_created",
    )
    created_at = models.DateTimeField(auto_now_add=True, editable=False)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["retailer_banner", "id"]
        indexes = [
            models.Index(
                fields=["tenant", "enabled"],
                name="pilot_posfeed_ten_en_idx",
            )
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["tenant", "retailer_banner", "preset_key"],
                name="pilot_posfeed_tenant_banner_preset_uniq",
            )
        ]

    def __str__(self) -> str:
        return f"PosFeed({self.tenant_id}, {self.retailer_banner}, {self.preset_key})"


class PosFeedDrop(models.Model):
    """Inbound POS CSV payload awaiting (or finished) automated import."""

    STATUS_PENDING = "pending"
    STATUS_PROCESSING = "processing"
    STATUS_PROCESSED = "processed"
    STATUS_FAILED = "failed"
    STATUS_SKIPPED = "skipped"
    STATUS_CHOICES = [
        (STATUS_PENDING, "Pending"),
        (STATUS_PROCESSING, "Processing"),
        (STATUS_PROCESSED, "Processed"),
        (STATUS_FAILED, "Failed"),
        (STATUS_SKIPPED, "Skipped"),
    ]

    id = models.BigAutoField(primary_key=True)
    uuid = models.UUIDField(default=uuid7, unique=True, editable=False)
    tenant = models.ForeignKey(
        "tenants.Tenant",
        on_delete=models.CASCADE,
        related_name="pos_feed_drops",
    )
    feed_config = models.ForeignKey(
        PosFeedConfig,
        on_delete=models.CASCADE,
        related_name="drops",
    )
    status = models.CharField(
        max_length=16, choices=STATUS_CHOICES, default=STATUS_PENDING, db_index=True
    )
    filename = models.CharField(max_length=255, blank=True, default="pos.csv")
    csv_text = models.TextField()
    file_sha256 = models.CharField(max_length=64, blank=True, default="", db_index=True)
    source_from_email = models.EmailField(blank=True, default="")
    source_message_id = models.CharField(max_length=255, blank=True, default="")
    error_message = models.TextField(blank=True, default="")
    sales_import = models.ForeignKey(
        RetailerSalesImport,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="feed_drops",
    )
    received_at = models.DateTimeField(auto_now_add=True, editable=False)
    processed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-received_at", "-id"]
        indexes = [
            models.Index(
                fields=["tenant", "status", "received_at"],
                name="pilot_posdrop_ten_st_rec_idx",
            )
        ]


class SavedReportView(models.Model):
    """Named regional/retailer report slice with optional scheduled digest email."""

    SURFACE_BUYER_REPORT = "buyer_report"
    SURFACE_PROGRAM_REPORT = "program_report"
    SURFACE_SHELF_VOICE = "shelf_voice"
    SURFACE_RETAILER_POS = "retailer_pos"
    SURFACE_CHOICES = [
        (SURFACE_BUYER_REPORT, "Buyer Report"),
        (SURFACE_PROGRAM_REPORT, "Program Report"),
        (SURFACE_SHELF_VOICE, "Shelf & Voice"),
        (SURFACE_RETAILER_POS, "Retailer POS"),
    ]

    CADENCE_WEEKLY = "weekly"
    CADENCE_BIWEEKLY = "biweekly"
    CADENCE_MONTHLY = "monthly"
    CADENCE_CHOICES = [
        (CADENCE_WEEKLY, "Weekly"),
        (CADENCE_BIWEEKLY, "Bi-weekly"),
        (CADENCE_MONTHLY, "Monthly"),
    ]

    FORMAT_CSV = "csv"
    FORMAT_SUMMARY = "summary"
    FORMAT_CHOICES = [
        (FORMAT_CSV, "Summary + CSV"),
        (FORMAT_SUMMARY, "Summary Only"),
    ]

    id = models.BigAutoField(primary_key=True)
    uuid = models.UUIDField(default=uuid7, unique=True, editable=False)
    tenant = models.ForeignKey(
        "tenants.Tenant",
        on_delete=models.CASCADE,
        related_name="saved_report_views",
    )
    name = models.CharField(max_length=120)
    surface = models.CharField(
        max_length=32,
        choices=SURFACE_CHOICES,
        default=SURFACE_BUYER_REPORT,
        db_index=True,
    )
    filters = models.JSONField(default=dict, blank=True)
    schedule_enabled = models.BooleanField(default=False, db_index=True)
    schedule_cadence = models.CharField(
        max_length=16, choices=CADENCE_CHOICES, default=CADENCE_WEEKLY
    )
    # 0=Mon, 1=Tue, 2=Wed, 3=Thu, 4=Fri (default), 5=Sat, 6=Sun
    schedule_day_of_week = models.PositiveSmallIntegerField(default=4)
    export_format = models.CharField(
        max_length=16, choices=FORMAT_CHOICES, default=FORMAT_CSV
    )
    recipient_emails = models.TextField(blank=True, default="")
    last_sent_at = models.DateTimeField(null=True, blank=True)
    last_send_status = models.CharField(max_length=32, blank=True, default="")
    last_send_error = models.CharField(max_length=240, blank=True, default="")
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="saved_report_views_created",
    )
    created_at = models.DateTimeField(auto_now_add=True, editable=False)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["name", "id"]
        indexes = [
            models.Index(
                fields=["tenant", "surface"],
                name="pilot_savedview_ten_surf_idx",
            ),
            models.Index(
                fields=["schedule_enabled", "schedule_day_of_week"],
                name="pilot_savedview_sched_day_idx",
            ),
        ]

    def __str__(self) -> str:
        return f"SavedReportView({self.tenant_id}, {self.name!r})"


class WebhookEndpoint(models.Model):
    """Tenant-scoped outbound HTTPS webhook subscription (HMAC-SHA256 signed)."""

    EVENT_RECAP_APPROVED = "recap.approved"
    EVENT_REQUEST_CREATED = "request.created"
    EVENT_SHELF_OOS_DETECTED = "shelf.oos_detected"
    SUPPORTED_EVENTS = (
        EVENT_RECAP_APPROVED,
        EVENT_REQUEST_CREATED,
        EVENT_SHELF_OOS_DETECTED,
    )

    id = models.BigAutoField(primary_key=True)
    uuid = models.UUIDField(default=uuid7, unique=True, editable=False)
    tenant = models.ForeignKey(
        "tenants.Tenant",
        on_delete=models.CASCADE,
        related_name="webhook_endpoints",
    )
    name = models.CharField(max_length=120)
    url = models.URLField(max_length=500)
    description = models.CharField(max_length=240, blank=True, default="")
    events = models.JSONField(default=list, blank=True)
    signing_secret = models.CharField(max_length=96)
    is_active = models.BooleanField(default=True, db_index=True)
    consecutive_failures = models.PositiveSmallIntegerField(default=0)
    last_delivery_at = models.DateTimeField(null=True, blank=True)
    last_status_code = models.PositiveSmallIntegerField(null=True, blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="webhook_endpoints_created",
    )
    created_at = models.DateTimeField(auto_now_add=True, editable=False)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at", "-id"]
        indexes = [
            models.Index(
                fields=["tenant", "is_active"],
                name="pilot_webhook_ten_act_idx",
            )
        ]

    def __str__(self) -> str:
        return f"WebhookEndpoint({self.tenant_id}, {self.name!r})"


class WebhookDelivery(models.Model):
    """Delivery audit log row for an outbound webhook attempt."""

    STATUS_DELIVERED = "delivered"
    STATUS_FAILED = "failed"
    STATUS_CHOICES = [
        (STATUS_DELIVERED, "Delivered"),
        (STATUS_FAILED, "Failed"),
    ]

    id = models.BigAutoField(primary_key=True)
    uuid = models.UUIDField(default=uuid7, unique=True, editable=False)
    endpoint = models.ForeignKey(
        WebhookEndpoint,
        on_delete=models.CASCADE,
        related_name="deliveries",
    )
    tenant = models.ForeignKey(
        "tenants.Tenant",
        on_delete=models.CASCADE,
        related_name="webhook_deliveries",
    )
    event_type = models.CharField(max_length=48, db_index=True)
    event_id = models.CharField(max_length=64, db_index=True)
    payload = models.JSONField(default=dict, blank=True)
    status = models.CharField(
        max_length=16, choices=STATUS_CHOICES, default=STATUS_FAILED, db_index=True
    )
    http_status = models.PositiveSmallIntegerField(null=True, blank=True)
    duration_ms = models.PositiveIntegerField(null=True, blank=True)
    response_excerpt = models.CharField(max_length=300, blank=True, default="")
    error_message = models.CharField(max_length=300, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True, editable=False, db_index=True)

    class Meta:
        ordering = ["-created_at", "-id"]
        indexes = [
            models.Index(
                fields=["endpoint", "created_at"],
                name="pilot_whdeliv_ep_cr_idx",
            ),
            models.Index(
                fields=["tenant", "created_at"],
                name="pilot_whdeliv_ten_cr_idx",
            ),
        ]


