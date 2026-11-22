"""Strawberry GraphQL schema for Standalone Spark Milestone 1 (Features 1-6)."""

from __future__ import annotations

import json
from datetime import date
from decimal import Decimal

import strawberry
from asgiref.sync import sync_to_async

from django.conf import settings
from events.models import Event, EventType, Product, Retailer
from tenants.models import Tenant
from pilot.actions import (
    build_weekly_buyer_store_report,
    complete_rebook_return_visit,
    create_store_recommendation_action,
    export_weekly_buyer_report_csv,
    export_weekly_buyer_report_xlsx_base64,
    reassign_store_action_provider,
    respond_store_action,
    verify_restock_action,
)
from pilot.costs import (
    allocate_shared_cost,
    build_brand_cost_rollups,
    compute_visit_cost_summary,
    link_receipt_evidence_to_cost_line,
    quantize_money,
    upsert_brand_cost_line,
    upsert_provider_agreement,
)
from pilot.entitlements import (
    PlanEntitlementError,
    require_plan_feature,
    resolve_brand_entitlements,
)
from pilot.finance import (
    assign_finance_exception_owner,
    attach_proof_to_finance_exception,
    evaluate_finance_exceptions,
    fix_finance_exception_match,
    list_finance_exceptions,
    list_operational_field_notices,
    request_finance_clarification,
    resolve_finance_exception,
)
from pilot.imports import (
    IMPORT_PRESETS,
    commit_provider_import,
    preview_provider_import,
    resolve_provider_import_row,
    sync_existing_events_to_canonical_visits,
)
from pilot.models import (
    CanonicalVisit,
    ProviderAgreement,
    ProviderAssignment,
    ProviderImportJob,
    ProviderImportMapping,
    ProviderImportRow,
    ProviderOrganization,
    ProviderSourceInclusionRule,
    RetailerSalesImport,
    RetailerSalesRow,
    StoreRecommendationAction,
    WorkspaceAuditLog,
    WorkspaceInvitation,
    WorkspaceMembership,
)
from pilot.onboarding import (
    STARTER_PROGRAM_CATALOG,
    approve_canonical_visit_recap,
    get_onboarding_state,
    onboarding_invite_contributor,
    reverse_canonical_visit_completion,
    schedule_standalone_visit,
    select_onboarding_starter_program,
    start_or_resume_self_serve_onboarding,
)
from pilot.permissions import (
    ResolvedWorkspaceAccess,
    resolve_tenant_from_request,
    resolve_workspace_access,
)
from pilot.pos import (
    build_retailer_pos_measurement,
    import_retailer_pos_csv,
    record_crew_purchase_adjustment,
    resolve_pos_sales_row,
)
from pilot.subscriptions import (
    approve_overage_or_volume_band,
    cancel_brand_subscription,
    change_subscription_plan,
    complete_test_mode_checkout,
    create_customer_portal_session,
    create_subscription_checkout_session,
    get_plans_and_billing_overview,
    preview_subscription_change,
    provision_pro_pilot,
)
from pilot.workspaces import (
    accept_workspace_invitation,
    assign_provider_work,
    create_or_update_provider,
    create_workspace_shared_link,
    ensure_workspace_profile,
    invite_workspace_member,
    list_user_workspace_contexts,
    respond_provider_assignment,
    revoke_provider_assignment,
    revoke_provider_from_workspace,
    revoke_workspace_membership,
    revoke_workspace_shared_link,
    set_provider_source_inclusion,
    switch_workspace_context,
)
from tenants.models import Tenant
from utils.graphql.mixins import resolve_id_to_int
from utils.graphql.permissions import StrictIsAuthenticated


def _opt_date(val: str | None) -> date | None:
    if not val or not val.strip():
        return None
    return date.fromisoformat(val.strip()[:10])


def _opt_dec(val: str | None) -> Decimal | None:
    if val is None or str(val).strip() == "":
        return None
    return Decimal(str(val).strip())


def _access_for_info(
    info: strawberry.Info, tenant_id: strawberry.ID | None = None
) -> ResolvedWorkspaceAccess:
    req = info.context.request
    user = req.user
    tenant: Tenant | None = None
    if tenant_id is not None:
        raw = str(tenant_id).strip()
        try:
            pk = resolve_id_to_int(tenant_id)
            tenant = Tenant.objects.filter(id=pk).first()
        except Exception:  # noqa: BLE001
            tenant = Tenant.objects.filter(uuid=raw).first()
    if tenant is None:
        tenant = resolve_tenant_from_request(user, req)
    return resolve_workspace_access(user, tenant=tenant, request=req)


@strawberry.type
class PilotMutationOutcome:
    ok: bool
    error: str | None = None
    target_uuid: str | None = None


@strawberry.type
class WorkspaceContextItem:
    membership_uuid: str | None
    tenant_uuid: str
    tenant_name: str
    tenant_slug: str
    workspace_role: str
    provider_uuid: str | None
    provider_name: str | None
    can_approve_recaps: bool
    can_approve_finance: bool
    can_approve_spend: bool
    display_label: str
    is_current: bool


@strawberry.type
class ProviderOrgItem:
    uuid: str
    name: str
    slug: str
    contact_email: str
    is_ignite_managed: bool
    include_in_combined_reporting: bool
    inclusion_notes: str


@strawberry.type
class WorkspaceMemberItem:
    uuid: str
    user_id: int
    email: str
    full_name: str
    workspace_role: str
    provider_uuid: str | None
    provider_name: str | None
    can_approve_recaps: bool
    can_approve_finance: bool
    can_approve_spend: bool
    status: str
    created_at: str


@strawberry.type
class WorkspaceInvitationItem:
    uuid: str
    email: str
    workspace_role: str
    provider_name: str | None
    status: str
    token: str
    expires_at: str


@strawberry.type
class ProviderAssignmentItem:
    uuid: str
    provider_uuid: str
    provider_name: str
    scope_type: str
    program_name: str | None
    visit_uuid: str | None
    store_label: str | None
    status: str
    submission_code: str
    due_date: str | None
    notes: str
    response_note: str


@strawberry.type
class WorkspaceAuditItem:
    uuid: str
    actor_email: str | None
    category: str
    action: str
    summary: str
    created_at: str


@strawberry.type
class SimpleCatalogItem:
    id: int
    name: str
    subtitle: str


@strawberry.type
class WorkspaceOverview:
    tenant_uuid: str
    tenant_name: str
    mode: str
    plan_tier: str
    default_contribution_per_unit: str | None
    viewer_role: str
    viewer_provider_uuid: str | None
    viewer_provider_name: str | None
    is_brand_admin: bool
    can_view_consolidated: bool
    can_edit_brand_ops: bool
    can_approve_recaps: bool
    can_edit_finance: bool
    can_approve_spend: bool
    can_manage_pos: bool
    providers: list[ProviderOrgItem]
    members: list[WorkspaceMemberItem]
    invitations: list[WorkspaceInvitationItem]
    assignments: list[ProviderAssignmentItem]
    audit_logs: list[WorkspaceAuditItem]
    programs: list[SimpleCatalogItem]
    stores: list[SimpleCatalogItem]
    products: list[SimpleCatalogItem]


@strawberry.type
class ImportPresetItem:
    key: str
    label: str
    column_map_json: str


@strawberry.type
class SavedImportMappingItem:
    uuid: str
    provider_uuid: str
    provider_name: str
    name: str
    preset_key: str
    column_map_json: str
    is_default: bool


@strawberry.type
class ImportRowPreviewItem:
    row_uuid: str | None
    row_number: int
    status: str
    error_code: str
    message: str
    raw_row_json: str
    normalized_row_json: str


@strawberry.type
class ImportPreviewResult:
    headers: list[str]
    resolved_map_json: str
    total_rows: int
    created_count: int
    updated_count: int
    unchanged_count: int
    review_count: int
    error_count: int
    rows: list[ImportRowPreviewItem]


@strawberry.type
class ImportJobItem:
    uuid: str
    provider_uuid: str
    provider_name: str
    filename: str
    status: str
    is_historical: bool
    total_rows: int
    created_count: int
    updated_count: int
    unchanged_count: int
    review_count: int
    error_count: int
    created_at: str


@strawberry.type
class ProviderImportWorkspace:
    presets: list[ImportPresetItem]
    saved_mappings: list[SavedImportMappingItem]
    recent_jobs: list[ImportJobItem]
    review_queue: list[ImportRowPreviewItem]


@strawberry.type
class VisitSubmissionItem:
    uuid: str
    provider_name: str | None
    source_kind: str
    provider_event_id: str
    recap_status: str
    exclude_from_aggregates: bool
    version: int
    samples_distributed: int | None
    consumers_sampled: int | None
    rep_observed_units: int | None
    notes: str


@strawberry.type
class CostLineItem:
    uuid: str
    category: str
    source_type: str
    description: str
    amount: str | None
    is_confirmed_zero: bool
    settlement_status: str
    evidence_status: str
    allocation_percentage: str | None
    provider_name: str | None
    evidence_count: int
    has_unitemized_proof: bool


@strawberry.type
class CanonicalVisitItem:
    uuid: str
    store_key: str
    store_label: str
    chain_label: str
    market: str
    visit_date: str
    shift_key: str
    status: str
    primary_provider_name: str | None
    exclude_from_aggregates: bool
    samples_distributed: int | None
    consumers_sampled: int | None
    rep_observed_units: int | None
    total_cost: str | None
    supported_cost: str
    is_cost_incomplete: bool
    settlement_status: str
    evidence_status: str
    cost_per_sample: str | None
    cost_per_observed_unit: str | None
    efficiency_withheld_reason: str | None
    submissions: list[VisitSubmissionItem]
    cost_lines: list[CostLineItem]


@strawberry.type
class ProviderAgreementItem:
    uuid: str
    provider_uuid: str
    provider_name: str
    name: str
    contracted_demo_fee: str | None
    included_hours: str
    extra_hour_rate: str | None
    mileage_rate: str | None
    admin_fee_percent: str
    admin_fee_bases: list[str]
    tax_percent: str


@strawberry.type
class BrandCostRollupRow:
    key: str
    label: str
    visit_count: int
    complete_visit_count: int
    incomplete_visit_count: int
    total_cost: str | None
    known_total_cost: str
    supported_cost: str
    invoiced_cost: str
    paid_cost: str
    estimated_cost: str
    has_incomplete_costs: bool
    cost_per_sample: str | None
    cost_per_observed_unit: str | None


@strawberry.type
class BrandCostDashboard:
    group_by: str
    agreements: list[ProviderAgreementItem]
    rollups: list[BrandCostRollupRow]
    plan_gated: bool = False
    plan_upgrade_message: str | None = None
    required_plan: str | None = None


@strawberry.type
class FinanceExceptionAuditItem:
    uuid: str
    actor_email: str | None
    action: str
    note: str
    created_at: str


@strawberry.type
class FinanceExceptionItem:
    uuid: str
    exception_type: str
    severity: str
    status: str
    affected_amount: str | None
    source_label: str
    explanation: str
    visit_uuid: str | None
    store_label: str | None
    visit_date: str | None
    cost_line_uuid: str | None
    provider_name: str | None
    owner_user_id: int | None
    owner_email: str | None
    clarification_note: str
    resolution_reason: str
    created_at: str
    audits: list[FinanceExceptionAuditItem]


@strawberry.type
class OperationalFieldNoticeItem:
    id: int
    kind: str
    status: str
    detail: str
    event_id: int | None
    created_at: str


@strawberry.type
class FinanceQueueView:
    exceptions: list[FinanceExceptionItem]
    operational_notices: list[OperationalFieldNoticeItem]
    plan_gated: bool = False
    plan_upgrade_message: str | None = None
    required_plan: str | None = None


@strawberry.type
class PosImportHistoryItem:
    uuid: str
    retailer_banner: str
    period_start: str
    period_end: str
    version: int
    is_active_version: bool
    feed_completeness: str
    filename: str
    total_rows: int
    matched_rows: int
    unmatched_rows: int
    created_at: str


@strawberry.type
class UnmatchedPosRowItem:
    uuid: str
    row_number: int
    raw_retailer: str
    raw_store_id: str
    raw_store_name: str
    raw_sku: str
    raw_week_start: str
    register_units: int | None
    match_status: str
    match_note: str


@strawberry.type
class PosMissingCellItem:
    store_key: str
    store_label: str
    product_id: int
    product_name: str
    week_start: str
    status: str


@strawberry.type
class PosStoreWeekItem:
    store_key: str
    store_label: str
    week_start: str
    week_end: str
    demo_count: int
    shared_store_week: bool
    rep_observed_units: int
    register_units: int | None
    crew_purchased_units: int
    adjusted_register_units: int | None
    observed_vs_baseline_diff: str | None
    incremental_lift_units: str | None
    eligible_brand_cost: str | None
    incremental_contribution: str | None
    roi: str | None
    roi_withheld_reason: str | None
    sku_cells_json: str


@strawberry.type
class RetailerPosDashboard:
    available: bool
    unavailable_message: str | None
    retailer_banners: list[str]
    matched_store_sku_weeks: int
    eligible_store_sku_weeks: int
    coverage_ratio: str | None
    unmatched_row_count: int
    missing_rows: list[PosMissingCellItem]
    rep_observed_units: int
    register_units: int | None
    crew_purchased_units: int
    adjusted_register_units: int | None
    observed_vs_baseline_diff: str | None
    incremental_lift_units: str | None
    incremental_contribution: str | None
    eligible_brand_cost: str | None
    roi: str | None
    lift_withheld_reason: str | None
    roi_withheld_reason: str | None
    imports: list[PosImportHistoryItem]
    unmatched_rows: list[UnmatchedPosRowItem]
    store_weeks: list[PosStoreWeekItem]
    plan_gated: bool = False
    plan_upgrade_message: str | None = None
    required_plan: str | None = None


@strawberry.type
class StoreActionItem:
    uuid: str
    finding_id: str
    finding_code: str
    finding_scope_key: str
    store_key: str
    store_label: str
    action_type: str
    status: str
    provider_uuid: str | None
    provider_name: str | None
    previous_provider_name: str | None
    owner_email: str | None
    target_date: str | None
    due_date: str | None
    hold_reason: str
    next_review_date: str | None
    provider_response_status: str
    provider_response_note: str
    restock_verification_method: str
    restock_verification_note: str
    submission_code: str | None
    linked_request_uuid: str | None
    outcome_visit_uuid: str | None
    decision_snapshot_json: str
    outcome_summary_json: str
    history_log_json: str
    created_at: str


@strawberry.type
class WeeklyBuyerStoreRow:
    store_key: str
    store_label: str
    retailer_banner: str
    market: str
    providers: list[str]
    visit_count: int
    supported_brand_cost: str
    total_brand_cost: str | None
    has_incomplete_cost: bool
    samples_distributed: int
    rep_observed_units: int
    matched_register_units: int | None
    adjusted_register_units: int | None
    pos_coverage_label: str
    recommendation_id: str | None
    recommendation_code: str | None
    recommendation_summary: str | None
    current_action_uuid: str | None
    current_action_type: str | None
    current_action_status: str | None
    current_action_owner: str | None
    current_action_provider: str | None


@strawberry.type
class WeeklyBuyerReport:
    period_start: str
    period_end: str
    pos_available: bool
    total_visits: int
    total_supported_cost: str
    rows: list[WeeklyBuyerStoreRow]
    plan_gated: bool = False
    plan_upgrade_message: str | None = None
    required_plan: str | None = None


@strawberry.type
class WeeklyBuyerReportExport:
    filename: str
    mime_type: str
    is_base64: bool
    content: str


@strawberry.type
class StarterProgramOption:
    key: str
    name: str
    slug: str
    description: str
    template_name: str
    default_sku_hint: str


@strawberry.type
class OnboardingStarterCatalogView:
    signup_enabled: bool
    starters: list[StarterProgramOption]


@strawberry.type
class OnboardingSessionPayload:
    ok: bool
    error: str | None = None
    token: str | None = None
    refresh_token: str | None = None
    tenant_uuid: str | None = None
    tenant_name: str | None = None
    tenant_slug: str | None = None
    membership_uuid: str | None = None
    workspace_role: str | None = None
    resumed_existing: bool = False
    resolved_managed: bool = False
    joined_invitation: bool = False


@strawberry.type
class OnboardingVisitItem:
    uuid: str
    store_label: str
    visit_date: str
    status: str
    samples_distributed: int | None
    consumers_sampled: int | None
    rep_observed_units: int | None
    completed_at: str | None
    submission_count: int


@strawberry.type
class OnboardingStateView:
    tenant_uuid: str
    tenant_name: str
    tenant_slug: str
    workspace_mode: str
    plan_code: str
    plan_display_name: str
    business_timezone: str
    steps_completed: int
    total_steps: int
    step_workspace_created: bool
    step_program_selected: bool
    step_contributor_invited: bool
    step_mobile_link_opened: bool
    step_first_visit_completed: bool
    starter_template_key: str
    starter_program_name: str | None
    starter_recap_template_name: str | None
    submission_code: str | None
    first_visit_uuid: str | None
    first_visit_store_label: str | None
    first_visit_status: str | None
    first_shared_token: str | None
    completed_visits_used: int
    included_visits: int | None
    recent_visits: list[OnboardingVisitItem]
    starters: list[StarterProgramOption]


@strawberry.type
class PlanTierCardItem:
    plan_code: str
    display_name: str
    tagline: str
    monthly_price_usd: str
    included_completed_visits: int | None
    is_quote_only: bool
    is_self_serve_selectable: bool
    is_current: bool
    max_active_programs: int | None
    max_operating_providers: int | None
    max_pos_retailers: int | None
    allows_branded_forms: bool
    allows_approvals_and_reminders: bool
    allows_exports_and_weekly_reporting: bool
    allows_store_recommendations: bool
    allows_provider_csv_imports: bool
    allows_brand_cost_tracking: bool
    allows_finance_review_queue: bool
    allows_retailer_pos_measurement: bool
    allows_tracked_store_actions: bool


@strawberry.type
class CountedVisitMeterItem:
    visit_uuid: str
    store_label: str
    visit_date: str
    completion_local_date: str
    provider_name: str | None
    submission_count: int
    co_brand_share_pct: str | None


@strawberry.type
class MonthlyVisitUsageView:
    month_key: str
    business_timezone: str
    completed_visits_used: int
    included_visits: int | None
    effective_visit_cap: int | None
    remaining_visits: int | None
    at_or_over_cap: bool
    overage_units: int
    overage_agreement_approved: bool
    can_book_new_visit: bool
    in_progress_fieldwork_always_allowed: bool
    historical_excluded_count: int
    in_progress_or_pending_count: int
    cancelled_or_reversed_count: int
    co_branded_visit_count: int
    next_upgrade_plan: str | None
    counted_visits: list[CountedVisitMeterItem]


@strawberry.type
class SubscriptionChangePreview:
    current_plan_code: str
    target_plan_code: str
    target_display_name: str
    is_quote_only: bool
    can_self_serve_confirm: bool
    monthly_price_usd: str
    charge_today_usd: str
    included_visits: int
    change_kind: str
    effective_date_label: str
    paid_through_date: str | None
    disclosure: str


@strawberry.type
class OpenCheckoutItem:
    stripe_session_id: str
    target_plan_code: str
    quoted_monthly_price_usd: str
    checkout_url: str
    status: str
    effective_summary: str


@strawberry.type
class PlansAndBillingView:
    billing_enabled: bool
    stripe_mode: str
    stripe_configured_remotely: bool
    tenant_uuid: str
    tenant_name: str
    workspace_mode: str
    business_timezone: str
    can_manage_billing: bool
    is_ignite_admin: bool
    configured_plan_code: str
    effective_plan_code: str
    plan_display_name: str
    subscription_status: str
    pending_plan_code: str | None
    addon_plan_code: str | None
    monthly_price_usd: str
    is_managed_sow: bool
    is_pilot: bool
    pilot_fee_usd: str | None
    pilot_starts_at: str | None
    pilot_ends_at: str | None
    current_period_start: str | None
    current_period_end: str | None
    paid_through_at: str | None
    cancel_at_period_end: bool
    has_payment_failure: bool
    payment_failure_message: str
    recurring_billing_accepted: bool
    viewer_and_contributor_seats_free: bool
    billing_separation_notice: str
    usage: MonthlyVisitUsageView
    available_plans: list[PlanTierCardItem]
    open_checkouts: list[OpenCheckoutItem]


@strawberry.type
class CheckoutSessionOutcome:
    ok: bool
    error: str | None = None
    stripe_session_id: str | None = None
    checkout_url: str | None = None
    mode: str | None = None
    already_completed: bool = False
    disclosure: str | None = None


@strawberry.type
class PortalSessionOutcome:
    ok: bool
    error: str | None = None
    portal_url: str | None = None
    mode: str | None = None


def _serialize_action(act: StoreRecommendationAction) -> StoreActionItem:
    return StoreActionItem(
        uuid=str(act.uuid),
        finding_id=act.finding_id_snapshot,
        finding_code=act.finding_code,
        finding_scope_key=act.finding_scope_key,
        store_key=act.store_key,
        store_label=act.store_label,
        action_type=act.action_type,
        status=act.status,
        provider_uuid=str(act.provider.uuid) if act.provider else None,
        provider_name=act.provider.name if act.provider else None,
        previous_provider_name=(
            act.previous_provider.name if act.previous_provider else None
        ),
        owner_email=act.owner.email if act.owner else None,
        target_date=act.target_date.isoformat() if act.target_date else None,
        due_date=act.due_date.isoformat() if act.due_date else None,
        hold_reason=act.hold_reason,
        next_review_date=(
            act.next_review_date.isoformat() if act.next_review_date else None
        ),
        provider_response_status=act.provider_response_status,
        provider_response_note=act.provider_response_note,
        restock_verification_method=act.restock_verification_method,
        restock_verification_note=act.restock_verification_note,
        submission_code=(
            act.provider_assignment.submission_code
            if act.provider_assignment
            else None
        ),
        linked_request_uuid=(
            str(act.linked_request.uuid) if act.linked_request else None
        ),
        outcome_visit_uuid=(
            str(act.outcome_visit.uuid) if act.outcome_visit else None
        ),
        decision_snapshot_json=json.dumps(act.decision_snapshot or {}, default=str),
        outcome_summary_json=json.dumps(act.outcome_summary or {}, default=str),
        history_log_json=json.dumps(act.history_log or [], default=str),
        created_at=act.created_at.isoformat(),
    )


@strawberry.type
class PilotQueries:
    @strawberry.field(permission_classes=[StrictIsAuthenticated])
    async def my_workspace_contexts(
        self, info: strawberry.Info
    ) -> list[WorkspaceContextItem]:
        def _run():
            items = list_user_workspace_contexts(info.context.request.user)
            return [WorkspaceContextItem(**i) for i in items]

        return await sync_to_async(_run, thread_sensitive=True)()

    @strawberry.field(permission_classes=[StrictIsAuthenticated])
    async def workspace_overview(
        self, info: strawberry.Info, tenant_id: strawberry.ID | None = None
    ) -> WorkspaceOverview:
        def _run():
            access = _access_for_info(info, tenant_id)
            profile = ensure_workspace_profile(access.tenant)

            rules_by_prov = {
                r.provider_id: r
                for r in ProviderSourceInclusionRule.objects.filter(
                    tenant=access.tenant
                )
            }
            prov_ids = set(rules_by_prov.keys()) | set(
                WorkspaceMembership.objects.filter(
                    tenant=access.tenant
                )
                .exclude(provider_id=None)
                .values_list("provider_id", flat=True)
            ) | set(
                ProviderAssignment.objects.filter(tenant=access.tenant).values_list(
                    "provider_id", flat=True
                )
            )
            if access.is_provider_scoped and access.provider:
                prov_ids = {access.provider.id}

            providers_out: list[ProviderOrgItem] = []
            for p in ProviderOrganization.objects.filter(
                id__in=prov_ids, is_active=True
            ).order_by("name"):
                rule = rules_by_prov.get(p.id)
                providers_out.append(
                    ProviderOrgItem(
                        uuid=str(p.uuid),
                        name=p.name,
                        slug=p.slug,
                        contact_email=p.contact_email,
                        is_ignite_managed=p.is_ignite_managed,
                        include_in_combined_reporting=(
                            rule.include_in_combined_reporting if rule else True
                        ),
                        inclusion_notes=rule.notes if rule else "",
                    )
                )

            members_out: list[WorkspaceMemberItem] = []
            invitations_out: list[WorkspaceInvitationItem] = []
            audits_out: list[WorkspaceAuditItem] = []

            if access.can_view_brand_consolidated:
                for m in (
                    WorkspaceMembership.objects.select_related("user", "provider")
                    .filter(tenant=access.tenant)
                    .order_by("status", "user__email")
                ):
                    full_name = (
                        f"{m.user.first_name or ''} {m.user.last_name or ''}".strip()
                        or m.user.email
                    )
                    members_out.append(
                        WorkspaceMemberItem(
                            uuid=str(m.uuid),
                            user_id=m.user_id,
                            email=m.user.email,
                            full_name=full_name,
                            workspace_role=m.workspace_role,
                            provider_uuid=(
                                str(m.provider.uuid) if m.provider else None
                            ),
                            provider_name=(
                                m.provider.name if m.provider else None
                            ),
                            can_approve_recaps=m.can_approve_recaps,
                            can_approve_finance=m.can_approve_finance,
                            can_approve_spend=m.can_approve_spend,
                            status=m.status,
                            created_at=m.created_at.isoformat(),
                        )
                    )
                for inv in (
                    WorkspaceInvitation.objects.select_related("provider")
                    .filter(tenant=access.tenant)
                    .order_by("-created_at")[:50]
                ):
                    invitations_out.append(
                        WorkspaceInvitationItem(
                            uuid=str(inv.uuid),
                            email=inv.email,
                            workspace_role=inv.workspace_role,
                            provider_name=(
                                inv.provider.name if inv.provider else None
                            ),
                            status=inv.status,
                            token=inv.token if access.is_brand_admin else "",
                            expires_at=inv.expires_at.isoformat(),
                        )
                    )
                for log in (
                    WorkspaceAuditLog.objects.select_related("actor")
                    .filter(tenant=access.tenant)
                    .order_by("-created_at")[:60]
                ):
                    audits_out.append(
                        WorkspaceAuditItem(
                            uuid=str(log.uuid),
                            actor_email=log.actor.email if log.actor else None,
                            category=log.category,
                            action=log.action,
                            summary=log.summary,
                            created_at=log.created_at.isoformat(),
                        )
                    )

            assign_qs = ProviderAssignment.objects.select_related(
                "provider", "event_type", "canonical_visit"
            ).filter(tenant=access.tenant)
            if access.is_provider_scoped:
                if access.provider:
                    assign_qs = assign_qs.filter(
                        provider=access.provider
                    ).exclude(status=ProviderAssignment.STATUS_REVOKED)
                else:
                    assign_qs = assign_qs.none()

            assignments_out = [
                ProviderAssignmentItem(
                    uuid=str(a.uuid),
                    provider_uuid=str(a.provider.uuid),
                    provider_name=a.provider.name,
                    scope_type=a.scope_type,
                    program_name=a.event_type.name if a.event_type else None,
                    visit_uuid=(
                        str(a.canonical_visit.uuid)
                        if a.canonical_visit
                        else None
                    ),
                    store_label=(
                        a.canonical_visit.store_label
                        if a.canonical_visit
                        else None
                    ),
                    status=a.status,
                    submission_code=(
                        "" if a.status == ProviderAssignment.STATUS_REVOKED else a.submission_code
                    ),
                    due_date=a.due_date.isoformat() if a.due_date else None,
                    notes=a.notes,
                    response_note=a.response_note,
                )
                for a in assign_qs.order_by("-created_at")[:100]
            ]

            programs = [
                SimpleCatalogItem(id=et.id, name=et.name, subtitle=et.slug or "")
                for et in EventType.objects.filter(tenant=access.tenant).order_by("name")
            ]
            stores = [
                SimpleCatalogItem(
                    id=r.id,
                    name=r.name,
                    subtitle=r.address or "",
                )
                for r in Retailer.objects.filter(tenant=access.tenant).order_by("name")[:200]
            ]
            products = [
                SimpleCatalogItem(
                    id=p.id,
                    name=p.name,
                    subtitle=p.description or "",
                )
                for p in Product.objects.filter(tenant=access.tenant).order_by("name")[:200]
            ]

            return WorkspaceOverview(
                tenant_uuid=str(access.tenant.uuid),
                tenant_name=access.tenant.name,
                mode=profile.mode,
                plan_tier=profile.plan_tier,
                default_contribution_per_unit=(
                    str(profile.default_contribution_per_unit)
                    if profile.default_contribution_per_unit is not None
                    else None
                ),
                viewer_role=access.workspace_role,
                viewer_provider_uuid=(
                    str(access.provider.uuid) if access.provider else None
                ),
                viewer_provider_name=(
                    access.provider.name if access.provider else None
                ),
                is_brand_admin=access.is_brand_admin,
                can_view_consolidated=access.can_view_brand_consolidated,
                can_edit_brand_ops=access.can_edit_brand_ops,
                can_approve_recaps=access.can_approve_recaps,
                can_edit_finance=access.can_edit_finance,
                can_approve_spend=access.can_approve_spend,
                can_manage_pos=access.can_manage_pos,
                providers=providers_out,
                members=members_out,
                invitations=invitations_out,
                assignments=assignments_out,
                audit_logs=audits_out,
                programs=programs,
                stores=stores,
                products=products,
            )

        return await sync_to_async(_run, thread_sensitive=True)()

    @strawberry.field(permission_classes=[StrictIsAuthenticated])
    async def provider_import_workspace(
        self, info: strawberry.Info, provider_uuid: str | None = None
    ) -> ProviderImportWorkspace:
        def _run():
            access = _access_for_info(info)
            presets = [
                ImportPresetItem(
                    key="agency_fieldpulse",
                    label="FieldPulse Agency CSV",
                    column_map_json=json.dumps(IMPORT_PRESETS["agency_fieldpulse"]),
                ),
                ImportPresetItem(
                    key="agency_promoworks",
                    label="PromoWorks Shift Tracker CSV",
                    column_map_json=json.dumps(IMPORT_PRESETS["agency_promoworks"]),
                ),
            ]
            map_qs = ProviderImportMapping.objects.select_related(
                "provider"
            ).filter(tenant=access.tenant)
            job_qs = ProviderImportJob.objects.select_related("provider").filter(
                tenant=access.tenant
            )
            row_qs = ProviderImportRow.objects.filter(
                tenant=access.tenant, status=ProviderImportRow.STATUS_NEEDS_REVIEW
            )

            if access.is_provider_scoped:
                if not access.provider:
                    return ProviderImportWorkspace(
                        presets=presets,
                        saved_mappings=[],
                        recent_jobs=[],
                        review_queue=[],
                    )
                map_qs = map_qs.filter(provider=access.provider)
                job_qs = job_qs.filter(provider=access.provider)
                row_qs = row_qs.none()
            elif provider_uuid:
                map_qs = map_qs.filter(provider__uuid=provider_uuid)
                job_qs = job_qs.filter(provider__uuid=provider_uuid)
                row_qs = row_qs.filter(provider__uuid=provider_uuid)

            return ProviderImportWorkspace(
                presets=presets,
                saved_mappings=[
                    SavedImportMappingItem(
                        uuid=str(m.uuid),
                        provider_uuid=str(m.provider.uuid),
                        provider_name=m.provider.name,
                        name=m.name,
                        preset_key=m.preset_key,
                        column_map_json=json.dumps(m.column_map or {}),
                        is_default=m.is_default,
                    )
                    for m in map_qs.order_by("-is_default", "name")
                ],
                recent_jobs=[
                    ImportJobItem(
                        uuid=str(j.uuid),
                        provider_uuid=str(j.provider.uuid),
                        provider_name=j.provider.name,
                        filename=j.filename,
                        status=j.status,
                        is_historical=j.is_historical,
                        total_rows=j.total_rows,
                        created_count=j.created_count,
                        updated_count=j.updated_count,
                        unchanged_count=j.unchanged_count,
                        review_count=j.review_count,
                        error_count=j.error_count,
                        created_at=j.created_at.isoformat(),
                    )
                    for j in job_qs[:40]
                ],
                review_queue=[
                    ImportRowPreviewItem(
                        row_uuid=str(r.uuid),
                        row_number=r.row_number,
                        status=r.status,
                        error_code=r.error_code,
                        message=r.message,
                        raw_row_json=json.dumps(r.raw_row or {}),
                        normalized_row_json=json.dumps(
                            r.normalized_row or {}, default=str
                        ),
                    )
                    for r in row_qs[:100]
                ],
            )

        return await sync_to_async(_run, thread_sensitive=True)()

    @strawberry.field(permission_classes=[StrictIsAuthenticated])
    async def canonical_visits(
        self,
        info: strawberry.Info,
        period_start: str | None = None,
        period_end: str | None = None,
        store_key: str | None = None,
        provider_uuid: str | None = None,
    ) -> list[CanonicalVisitItem]:
        def _run():
            access = _access_for_info(info)
            if access.can_view_brand_consolidated:
                sync_existing_events_to_canonical_visits(access.tenant)
            qs = (
                CanonicalVisit.objects.select_related("primary_provider")
                .prefetch_related(
                    "submissions",
                    "submissions__provider",
                    "cost_lines",
                    "cost_lines__provider",
                    "cost_lines__evidence_links",
                )
                .filter(access.allowed_canonical_visits_q())
            )
            p_start = _opt_date(period_start)
            p_end = _opt_date(period_end)
            if p_start:
                qs = qs.filter(visit_date__gte=p_start)
            if p_end:
                qs = qs.filter(visit_date__lte=p_end)
            if store_key:
                qs = qs.filter(store_key=store_key)
            if provider_uuid and access.can_view_brand_consolidated:
                qs = qs.filter(primary_provider__uuid=provider_uuid)

            out: list[CanonicalVisitItem] = []
            for v in qs.order_by("-visit_date", "-id")[:150]:
                v_lines = (
                    list(v.cost_lines.all())
                    if access.can_view_brand_consolidated
                    else []
                )
                csum = compute_visit_cost_summary(v, v_lines)
                subs = [
                    VisitSubmissionItem(
                        uuid=str(s.uuid),
                        provider_name=s.provider.name if s.provider else None,
                        source_kind=s.source_kind,
                        provider_event_id=s.provider_event_id,
                        recap_status=s.recap_status,
                        exclude_from_aggregates=s.exclude_from_aggregates,
                        version=s.version,
                        samples_distributed=s.samples_distributed,
                        consumers_sampled=s.consumers_sampled,
                        rep_observed_units=s.rep_observed_units,
                        notes=s.notes,
                    )
                    for s in v.submissions.all()
                    if access.can_view_brand_consolidated
                    or (access.provider and s.provider_id == access.provider.id)
                ]
                lines_out = [
                    CostLineItem(
                        uuid=str(cl.uuid),
                        category=cl.category,
                        source_type=cl.source_type,
                        description=cl.description,
                        amount=str(cl.amount) if cl.amount is not None else None,
                        is_confirmed_zero=cl.is_confirmed_zero,
                        settlement_status=cl.settlement_status,
                        evidence_status=cl.evidence_status,
                        allocation_percentage=(
                            str(cl.allocation_percentage)
                            if cl.allocation_percentage is not None
                            else None
                        ),
                        provider_name=(
                            cl.provider.name if cl.provider else None
                        ),
                        evidence_count=len(list(cl.evidence_links.all())),
                        has_unitemized_proof=any(
                            not ev.is_itemized
                            for ev in cl.evidence_links.all()
                        ),
                    )
                    for cl in v_lines
                ]
                out.append(
                    CanonicalVisitItem(
                        uuid=str(v.uuid),
                        store_key=v.store_key,
                        store_label=v.store_label or v.address,
                        chain_label=v.chain_label,
                        market=v.market or v.city or v.state_code,
                        visit_date=v.visit_date.isoformat(),
                        shift_key=v.shift_key,
                        status=v.status,
                        primary_provider_name=(
                            v.primary_provider.name
                            if v.primary_provider
                            else None
                        ),
                        exclude_from_aggregates=v.exclude_from_aggregates,
                        samples_distributed=v.samples_distributed,
                        consumers_sampled=v.consumers_sampled,
                        rep_observed_units=v.rep_observed_units,
                        total_cost=(
                            str(csum.total_cost)
                            if access.can_view_brand_consolidated
                            and csum.total_cost is not None
                            else None
                        ),
                        supported_cost=(
                            str(csum.supported_cost)
                            if access.can_view_brand_consolidated
                            else "0.00"
                        ),
                        is_cost_incomplete=csum.is_incomplete,
                        settlement_status=csum.settlement_status,
                        evidence_status=csum.evidence_status,
                        cost_per_sample=(
                            str(csum.cost_per_sample)
                            if access.can_view_brand_consolidated
                            and csum.cost_per_sample is not None
                            else None
                        ),
                        cost_per_observed_unit=(
                            str(csum.cost_per_observed_unit)
                            if access.can_view_brand_consolidated
                            and csum.cost_per_observed_unit is not None
                            else None
                        ),
                        efficiency_withheld_reason=(
                            csum.efficiency_withheld_reason
                            if access.can_view_brand_consolidated
                            else None
                        ),
                        submissions=subs,
                        cost_lines=lines_out,
                    )
                )
            return out

        return await sync_to_async(_run, thread_sensitive=True)()

    @strawberry.field(permission_classes=[StrictIsAuthenticated])
    async def brand_cost_dashboard(
        self,
        info: strawberry.Info,
        group_by: str = "store",
        period_start: str | None = None,
        period_end: str | None = None,
        provider_uuid: str | None = None,
    ) -> BrandCostDashboard:
        def _run():
            access = _access_for_info(info)
            try:
                rollups = build_brand_cost_rollups(
                    access,
                    group_by=group_by,
                    period_start=_opt_date(period_start),
                    period_end=_opt_date(period_end),
                    provider_uuid=provider_uuid,
                )
            except PlanEntitlementError as exc:
                return BrandCostDashboard(
                    group_by=group_by,
                    agreements=[],
                    rollups=[],
                    plan_gated=True,
                    plan_upgrade_message=str(exc),
                    required_plan=exc.required_plan,
                )
            agreements = [
                ProviderAgreementItem(
                    uuid=str(ag.uuid),
                    provider_uuid=str(ag.provider.uuid),
                    provider_name=ag.provider.name,
                    name=ag.name,
                    contracted_demo_fee=(
                        str(ag.contracted_demo_fee)
                        if ag.contracted_demo_fee is not None
                        else None
                    ),
                    included_hours=str(ag.included_hours),
                    extra_hour_rate=(
                        str(ag.extra_hour_rate)
                        if ag.extra_hour_rate is not None
                        else None
                    ),
                    mileage_rate=(
                        str(ag.mileage_rate)
                        if ag.mileage_rate is not None
                        else None
                    ),
                    admin_fee_percent=str(ag.admin_fee_percent),
                    admin_fee_bases=list(ag.admin_fee_bases or []),
                    tax_percent=str(ag.tax_percent),
                )
                for ag in ProviderAgreement.objects.select_related(
                    "provider"
                ).filter(tenant=access.tenant, is_active=True)
            ]
            return BrandCostDashboard(
                group_by=group_by,
                agreements=agreements,
                rollups=[BrandCostRollupRow(**r) for r in rollups],
            )

        return await sync_to_async(_run, thread_sensitive=True)()

    @strawberry.field(permission_classes=[StrictIsAuthenticated])
    async def finance_exception_queue(
        self,
        info: strawberry.Info,
        exception_type: str | None = None,
        status: str | None = None,
        provider_uuid: str | None = None,
        store_key: str | None = None,
        period_start: str | None = None,
        period_end: str | None = None,
    ) -> FinanceQueueView:
        def _run():
            access = _access_for_info(info)
            if not access.can_view_brand_consolidated:
                raise PermissionError(
                    "Outside providers cannot view the finance review queue."
                )
            try:
                evaluate_finance_exceptions(access.tenant)
                excs = list_finance_exceptions(
                    access,
                    exception_type=exception_type,
                    status=status,
                    provider_uuid=provider_uuid,
                    store_key=store_key,
                    period_start=_opt_date(period_start),
                    period_end=_opt_date(period_end),
                )
            except PlanEntitlementError as exc:
                return FinanceQueueView(
                    exceptions=[],
                    operational_notices=[],
                    plan_gated=True,
                    plan_upgrade_message=str(exc),
                    required_plan=exc.required_plan,
                )
            notices = list_operational_field_notices(access)
            return FinanceQueueView(
                exceptions=[
                    FinanceExceptionItem(
                        uuid=str(e.uuid),
                        exception_type=e.exception_type,
                        severity=e.severity,
                        status=e.status,
                        affected_amount=(
                            str(e.affected_amount)
                            if e.affected_amount is not None
                            else None
                        ),
                        source_label=e.source_label,
                        explanation=e.explanation,
                        visit_uuid=(
                            str(e.canonical_visit.uuid)
                            if e.canonical_visit
                            else None
                        ),
                        store_label=(
                            e.canonical_visit.store_label
                            if e.canonical_visit
                            else None
                        ),
                        visit_date=(
                            e.canonical_visit.visit_date.isoformat()
                            if e.canonical_visit
                            else None
                        ),
                        cost_line_uuid=(
                            str(e.cost_line.uuid) if e.cost_line else None
                        ),
                        provider_name=(
                            e.provider.name if e.provider else None
                        ),
                        owner_user_id=e.owner_id,
                        owner_email=e.owner.email if e.owner else None,
                        clarification_note=e.clarification_note,
                        resolution_reason=e.resolution_reason,
                        created_at=e.created_at.isoformat(),
                        audits=[
                            FinanceExceptionAuditItem(
                                uuid=str(au.uuid),
                                actor_email=(
                                    au.actor.email if au.actor else None
                                ),
                                action=au.action,
                                note=au.note,
                                created_at=au.created_at.isoformat(),
                            )
                            for au in e.audits.all()
                        ],
                    )
                    for e in excs
                ],
                operational_notices=[
                    OperationalFieldNoticeItem(
                        id=n["id"],
                        kind=n["kind"],
                        status=n["status"],
                        detail=n["detail"],
                        event_id=n["event_id"],
                        created_at=n["created_at"],
                    )
                    for n in notices
                ],
            )

        return await sync_to_async(_run, thread_sensitive=True)()

    @strawberry.field(permission_classes=[StrictIsAuthenticated])
    async def retailer_pos_dashboard(
        self,
        info: strawberry.Info,
        period_start: str | None = None,
        period_end: str | None = None,
        store_key: str | None = None,
        contribution_per_unit: str | None = None,
    ) -> RetailerPosDashboard:
        def _run():
            access = _access_for_info(info)
            try:
                data = build_retailer_pos_measurement(
                    access,
                    period_start=_opt_date(period_start),
                    period_end=_opt_date(period_end),
                    store_key=store_key,
                    contribution_per_unit=_opt_dec(contribution_per_unit),
                )
            except PlanEntitlementError as exc:
                return RetailerPosDashboard(
                    available=False,
                    unavailable_message=str(exc),
                    retailer_banners=[],
                    matched_store_sku_weeks=0,
                    eligible_store_sku_weeks=0,
                    coverage_ratio=None,
                    unmatched_row_count=0,
                    missing_rows=[],
                    rep_observed_units=0,
                    register_units=None,
                    crew_purchased_units=0,
                    adjusted_register_units=None,
                    observed_vs_baseline_diff=None,
                    incremental_lift_units=None,
                    incremental_contribution=None,
                    eligible_brand_cost=None,
                    roi=None,
                    lift_withheld_reason=str(exc),
                    roi_withheld_reason=str(exc),
                    imports=[],
                    unmatched_rows=[],
                    store_weeks=[],
                    plan_gated=True,
                    plan_upgrade_message=str(exc),
                    required_plan=exc.required_plan,
                )
            imports = [
                PosImportHistoryItem(
                    uuid=str(imp.uuid),
                    retailer_banner=imp.retailer_banner,
                    period_start=imp.period_start.isoformat(),
                    period_end=imp.period_end.isoformat(),
                    version=imp.version,
                    is_active_version=imp.is_active_version,
                    feed_completeness=imp.feed_completeness,
                    filename=imp.filename,
                    total_rows=imp.total_rows,
                    matched_rows=imp.matched_rows,
                    unmatched_rows=imp.unmatched_rows,
                    created_at=imp.created_at.isoformat(),
                )
                for imp in RetailerSalesImport.objects.filter(
                    tenant=access.tenant
                ).order_by("-created_at")[:30]
            ]
            unmatched = [
                UnmatchedPosRowItem(
                    uuid=str(r.uuid),
                    row_number=r.row_number,
                    raw_retailer=r.raw_retailer,
                    raw_store_id=r.raw_store_id,
                    raw_store_name=r.raw_store_name,
                    raw_sku=r.raw_sku,
                    raw_week_start=r.raw_week_start,
                    register_units=r.register_units,
                    match_status=r.match_status,
                    match_note=r.match_note,
                )
                for r in RetailerSalesRow.objects.filter(
                    tenant=access.tenant,
                    sales_import__is_active_version=True,
                ).exclude(match_status=RetailerSalesRow.MATCH_MATCHED)[:100]
            ]
            cov = data["coverage"]
            tot = data["totals"]
            return RetailerPosDashboard(
                available=data["available"],
                unavailable_message=data["unavailable_message"],
                retailer_banners=data["retailer_banners"],
                matched_store_sku_weeks=cov["matched_store_sku_weeks"],
                eligible_store_sku_weeks=cov["eligible_store_sku_weeks"],
                coverage_ratio=cov["coverage_ratio"],
                unmatched_row_count=cov["unmatched_row_count"],
                missing_rows=[
                    PosMissingCellItem(**mr) for mr in cov["missing_rows"]
                ],
                rep_observed_units=tot["rep_observed_units"],
                register_units=tot["register_units"],
                crew_purchased_units=tot["crew_purchased_units"],
                adjusted_register_units=tot["adjusted_register_units"],
                observed_vs_baseline_diff=tot["observed_vs_baseline_diff"],
                incremental_lift_units=tot["incremental_lift_units"],
                incremental_contribution=tot["incremental_contribution"],
                eligible_brand_cost=tot["eligible_brand_cost"],
                roi=tot["roi"],
                lift_withheld_reason=tot["lift_withheld_reason"],
                roi_withheld_reason=tot["roi_withheld_reason"],
                imports=imports,
                unmatched_rows=unmatched,
                store_weeks=[
                    PosStoreWeekItem(
                        store_key=sw["store_key"],
                        store_label=sw["store_label"],
                        week_start=sw["week_start"],
                        week_end=sw["week_end"],
                        demo_count=sw["demo_count"],
                        shared_store_week=sw["shared_store_week"],
                        rep_observed_units=sw["rep_observed_units"],
                        register_units=sw["register_units"],
                        crew_purchased_units=sw["crew_purchased_units"],
                        adjusted_register_units=sw["adjusted_register_units"],
                        observed_vs_baseline_diff=sw[
                            "observed_vs_baseline_diff"
                        ],
                        incremental_lift_units=sw["incremental_lift_units"],
                        eligible_brand_cost=sw["eligible_brand_cost"],
                        incremental_contribution=sw["incremental_contribution"],
                        roi=sw["roi"],
                        roi_withheld_reason=sw["roi_withheld_reason"],
                        sku_cells_json=json.dumps(
                            sw["sku_cells"], default=str
                        ),
                    )
                    for sw in data["store_weeks"]
                ],
            )

        return await sync_to_async(_run, thread_sensitive=True)()

    @strawberry.field(permission_classes=[StrictIsAuthenticated])
    async def store_recommendation_actions(
        self, info: strawberry.Info, store_key: str | None = None
    ) -> list[StoreActionItem]:
        def _run():
            access = _access_for_info(info)
            qs = StoreRecommendationAction.objects.select_related(
                "provider",
                "previous_provider",
                "owner",
                "provider_assignment",
                "linked_request",
                "outcome_visit",
            ).filter(tenant=access.tenant)
            if access.is_provider_scoped:
                if not access.provider:
                    return []
                qs = qs.filter(provider=access.provider)
            if store_key:
                qs = qs.filter(store_key=store_key)
            return [_serialize_action(a) for a in qs.order_by("-created_at")[:100]]

        return await sync_to_async(_run, thread_sensitive=True)()

    @strawberry.field(permission_classes=[StrictIsAuthenticated])
    async def weekly_buyer_store_report(
        self,
        info: strawberry.Info,
        period_start: str | None = None,
        period_end: str | None = None,
        retailer_filter: str | None = None,
        market_filter: str | None = None,
        provider_uuid: str | None = None,
    ) -> WeeklyBuyerReport:
        def _run():
            access = _access_for_info(info)
            ent = resolve_brand_entitlements(access.tenant)
            if not ent.allows_exports_and_weekly_reporting:
                return WeeklyBuyerReport(
                    period_start=period_start or "",
                    period_end=period_end or "",
                    pos_available=False,
                    total_visits=0,
                    total_supported_cost="0.00",
                    rows=[],
                    plan_gated=True,
                    plan_upgrade_message=(
                        "Weekly buyer reporting and exports require the Core plan ($299/mo) or higher."
                    ),
                    required_plan="core",
                )
            rep = build_weekly_buyer_store_report(
                access,
                period_start=_opt_date(period_start),
                period_end=_opt_date(period_end),
                retailer_filter=retailer_filter,
                market_filter=market_filter,
                provider_uuid=provider_uuid,
            )
            return WeeklyBuyerReport(
                period_start=rep["period_start"],
                period_end=rep["period_end"],
                pos_available=rep["pos_available"],
                total_visits=rep["total_visits"],
                total_supported_cost=rep["total_supported_cost"],
                rows=[WeeklyBuyerStoreRow(**r) for r in rep["rows"]],
            )

        return await sync_to_async(_run, thread_sensitive=True)()

    @strawberry.field(permission_classes=[StrictIsAuthenticated])
    async def export_weekly_buyer_report(
        self,
        info: strawberry.Info,
        format: str = "csv",
        period_start: str | None = None,
        period_end: str | None = None,
        retailer_filter: str | None = None,
        market_filter: str | None = None,
        provider_uuid: str | None = None,
    ) -> WeeklyBuyerReportExport:
        def _run():
            access = _access_for_info(info)
            require_plan_feature(
                access.tenant,
                "allows_exports_and_weekly_reporting",
                feature_label="Exports and weekly buyer reporting",
                required_plan="core",
            )
            rep = build_weekly_buyer_store_report(
                access,
                period_start=_opt_date(period_start),
                period_end=_opt_date(period_end),
                retailer_filter=retailer_filter,
                market_filter=market_filter,
                provider_uuid=provider_uuid,
            )
            fmt = (format or "csv").strip().lower()
            if fmt == "xlsx":
                return WeeklyBuyerReportExport(
                    filename=f"buyer-store-report-{rep['period_start']}.xlsx",
                    mime_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    is_base64=True,
                    content=export_weekly_buyer_report_xlsx_base64(rep),
                )
            return WeeklyBuyerReportExport(
                filename=f"buyer-store-report-{rep['period_start']}.csv",
                mime_type="text/csv;charset=utf-8",
                is_base64=False,
                content=export_weekly_buyer_report_csv(rep),
            )

        return await sync_to_async(_run, thread_sensitive=True)()

    @strawberry.field(permission_classes=[])
    async def self_serve_starter_catalog(
        self, info: strawberry.Info
    ) -> OnboardingStarterCatalogView:
        return OnboardingStarterCatalogView(
            signup_enabled=bool(
                getattr(settings, "STANDALONE_SIGNUP_ENABLED", True)
            ),
            starters=[
                StarterProgramOption(
                    key=s["key"],
                    name=s["program_name"],
                    slug=s["program_slug"],
                    description=s["summary"],
                    template_name=s["template_name"],
                    default_sku_hint=s["default_sku_name"],
                )
                for s in STARTER_PROGRAM_CATALOG
            ],
        )

    @strawberry.field(permission_classes=[StrictIsAuthenticated])
    async def onboarding_state(
        self, info: strawberry.Info
    ) -> OnboardingStateView:
        def _run():
            access = _access_for_info(info)
            st = get_onboarding_state(access)
            profile = ensure_workspace_profile(access.tenant)
            recent_visits = [
                OnboardingVisitItem(
                    uuid=str(v.uuid),
                    store_label=v.store_label or v.address or "Visit",
                    visit_date=v.visit_date.isoformat(),
                    status=v.status,
                    samples_distributed=v.samples_distributed,
                    consumers_sampled=v.consumers_sampled,
                    rep_observed_units=v.rep_observed_units,
                    completed_at=(
                        v.completed_at.isoformat() if v.completed_at else None
                    ),
                    submission_count=v.submissions.count(),
                )
                for v in CanonicalVisit.objects.filter(
                    tenant=access.tenant
                ).order_by("-created_at")[:10]
            ]
            return OnboardingStateView(
                tenant_uuid=st["tenant_uuid"],
                tenant_name=st["tenant_name"],
                tenant_slug=access.tenant.slug or "",
                workspace_mode=st["workspace_mode"],
                plan_code=st["plan_code"],
                plan_display_name=st["plan_display_name"],
                business_timezone=profile.business_timezone,
                steps_completed=st["steps_completed"],
                total_steps=st["total_steps"],
                step_workspace_created=st["step_workspace_created"],
                step_program_selected=st["step_program_selected"],
                step_contributor_invited=st["step_contributor_invited"],
                step_mobile_link_opened=st["step_mobile_link_opened"],
                step_first_visit_completed=st["step_first_visit_completed"],
                starter_template_key=st["starter_template_key"],
                starter_program_name=st["starter_program_name"],
                starter_recap_template_name=st["starter_recap_template_name"],
                submission_code=st["submission_code"],
                first_visit_uuid=st["first_visit_uuid"],
                first_visit_store_label=st["first_visit_store_label"],
                first_visit_status=st["first_visit_status"],
                first_shared_token=st["first_shared_token"],
                completed_visits_used=st["completed_visits_used"],
                included_visits=st["included_visits"],
                recent_visits=recent_visits,
                starters=[
                    StarterProgramOption(
                        key=s["key"],
                        name=s["program_name"],
                        slug=s["program_slug"],
                        description=s["summary"],
                        template_name=s["template_name"],
                        default_sku_hint=s["default_sku_name"],
                    )
                    for s in STARTER_PROGRAM_CATALOG
                ],
            )

        return await sync_to_async(_run, thread_sensitive=True)()

    @strawberry.field(permission_classes=[StrictIsAuthenticated])
    async def plans_and_billing_overview(
        self, info: strawberry.Info
    ) -> PlansAndBillingView:
        def _run():
            access = _access_for_info(info)
            ov = get_plans_and_billing_overview(access)
            usage = ov["usage"]
            return PlansAndBillingView(
                billing_enabled=bool(
                    getattr(settings, "STANDALONE_BILLING_ENABLED", True)
                ),
                stripe_mode=ov["stripe_mode"],
                stripe_configured_remotely=ov["stripe_configured_remotely"],
                tenant_uuid=ov["tenant_uuid"],
                tenant_name=ov["tenant_name"],
                workspace_mode=ov["workspace_mode"],
                business_timezone=usage["business_timezone"],
                can_manage_billing=access.is_brand_admin,
                is_ignite_admin=access.is_ignite_admin,
                configured_plan_code=ov["configured_plan_code"],
                effective_plan_code=ov["effective_plan_code"],
                plan_display_name=ov["plan_display_name"],
                subscription_status=ov["subscription_status"],
                pending_plan_code=ov["pending_plan_code"],
                addon_plan_code=ov["addon_plan_code"],
                monthly_price_usd=ov["monthly_price_usd"],
                is_managed_sow=ov["is_managed_sow"],
                is_pilot=ov["is_pilot"],
                pilot_fee_usd=ov["pilot_fee_usd"],
                pilot_starts_at=ov["pilot_starts_at"],
                pilot_ends_at=ov["pilot_ends_at"],
                current_period_start=ov["current_period_start"],
                current_period_end=ov["current_period_end"],
                paid_through_at=ov["paid_through_at"],
                cancel_at_period_end=ov["cancel_at_period_end"],
                has_payment_failure=ov["has_payment_failure"],
                payment_failure_message=ov["payment_failure_message"],
                recurring_billing_accepted=ov["recurring_billing_accepted"],
                viewer_and_contributor_seats_free=ov[
                    "viewer_and_contributor_seats_free"
                ],
                billing_separation_notice=ov["billing_separation_notice"],
                usage=MonthlyVisitUsageView(
                    month_key=usage["month_key"],
                    business_timezone=usage["business_timezone"],
                    completed_visits_used=usage["completed_visits_used"],
                    included_visits=usage["included_visits"],
                    effective_visit_cap=usage["effective_visit_cap"],
                    remaining_visits=usage["remaining_visits"],
                    at_or_over_cap=usage["at_or_over_cap"],
                    overage_units=usage["overage_units"],
                    overage_agreement_approved=usage[
                        "overage_agreement_approved"
                    ],
                    can_book_new_visit=usage["can_book_new_visit"],
                    in_progress_fieldwork_always_allowed=usage[
                        "in_progress_fieldwork_always_allowed"
                    ],
                    historical_excluded_count=usage[
                        "historical_excluded_count"
                    ],
                    in_progress_or_pending_count=usage[
                        "in_progress_or_pending_count"
                    ],
                    cancelled_or_reversed_count=usage[
                        "cancelled_or_reversed_count"
                    ],
                    co_branded_visit_count=usage["co_branded_visit_count"],
                    next_upgrade_plan=usage["next_upgrade_plan"],
                    counted_visits=[
                        CountedVisitMeterItem(**cv)
                        for cv in usage["counted_visits"]
                    ],
                ),
                available_plans=[
                    PlanTierCardItem(**p) for p in ov["plans"]
                ],
                open_checkouts=[
                    OpenCheckoutItem(**c) for c in ov["open_checkouts"]
                ],
            )

        return await sync_to_async(_run, thread_sensitive=True)()

    @strawberry.field(permission_classes=[StrictIsAuthenticated])
    async def preview_subscription_plan_change(
        self, info: strawberry.Info, target_plan_code: str
    ) -> SubscriptionChangePreview:
        def _run():
            access = _access_for_info(info)
            p = preview_subscription_change(
                access, target_plan_code=target_plan_code
            )
            return SubscriptionChangePreview(**p)

        return await sync_to_async(_run, thread_sensitive=True)()


@strawberry.type
class PilotMutations:
    @strawberry.mutation(permission_classes=[StrictIsAuthenticated])
    async def switch_workspace_context(
        self,
        info: strawberry.Info,
        membership_uuid: str | None = None,
        tenant_uuid: str | None = None,
    ) -> PilotMutationOutcome:
        def _run():
            try:
                ctx = switch_workspace_context(
                    user=info.context.request.user,
                    membership_uuid=membership_uuid,
                    tenant_uuid=tenant_uuid,
                )
                return PilotMutationOutcome(
                    ok=True,
                    target_uuid=str(ctx.tenant.uuid) if ctx.tenant else None,
                )
            except Exception as exc:  # noqa: BLE001
                return PilotMutationOutcome(ok=False, error=str(exc))

        return await sync_to_async(_run, thread_sensitive=True)()

    @strawberry.mutation(permission_classes=[StrictIsAuthenticated])
    async def register_provider_organization(
        self,
        info: strawberry.Info,
        name: str,
        contact_email: str = "",
        slug: str = "",
    ) -> PilotMutationOutcome:
        def _run():
            try:
                access = _access_for_info(info)
                prov = create_or_update_provider(
                    access, name=name, contact_email=contact_email, slug=slug
                )
                return PilotMutationOutcome(ok=True, target_uuid=str(prov.uuid))
            except Exception as exc:  # noqa: BLE001
                return PilotMutationOutcome(ok=False, error=str(exc))

        return await sync_to_async(_run, thread_sensitive=True)()

    @strawberry.mutation(permission_classes=[StrictIsAuthenticated])
    async def invite_workspace_member(
        self,
        info: strawberry.Info,
        email: str,
        workspace_role: str,
        provider_uuid: str | None = None,
        can_approve_recaps: bool = False,
        can_approve_finance: bool = False,
        can_approve_spend: bool = False,
    ) -> PilotMutationOutcome:
        def _run():
            try:
                access = _access_for_info(info)
                prov = (
                    ProviderOrganization.objects.filter(
                        uuid=provider_uuid
                    ).first()
                    if provider_uuid
                    else None
                )
                inv = invite_workspace_member(
                    access,
                    email=email,
                    workspace_role=workspace_role,
                    provider=prov,
                    can_approve_recaps=can_approve_recaps,
                    can_approve_finance=can_approve_finance,
                    can_approve_spend=can_approve_spend,
                )
                return PilotMutationOutcome(ok=True, target_uuid=str(inv.uuid))
            except Exception as exc:  # noqa: BLE001
                return PilotMutationOutcome(ok=False, error=str(exc))

        return await sync_to_async(_run, thread_sensitive=True)()

    @strawberry.mutation(permission_classes=[StrictIsAuthenticated])
    async def accept_workspace_invitation(
        self, info: strawberry.Info, token: str
    ) -> PilotMutationOutcome:
        def _run():
            try:
                m = accept_workspace_invitation(
                    user=info.context.request.user, token=token
                )
                return PilotMutationOutcome(ok=True, target_uuid=str(m.uuid))
            except Exception as exc:  # noqa: BLE001
                return PilotMutationOutcome(ok=False, error=str(exc))

        return await sync_to_async(_run, thread_sensitive=True)()

    @strawberry.mutation(permission_classes=[StrictIsAuthenticated])
    async def revoke_workspace_membership(
        self, info: strawberry.Info, membership_uuid: str
    ) -> PilotMutationOutcome:
        def _run():
            try:
                access = _access_for_info(info)
                m = revoke_workspace_membership(
                    access, membership_uuid=membership_uuid
                )
                return PilotMutationOutcome(ok=True, target_uuid=str(m.uuid))
            except Exception as exc:  # noqa: BLE001
                return PilotMutationOutcome(ok=False, error=str(exc))

        return await sync_to_async(_run, thread_sensitive=True)()

    @strawberry.mutation(permission_classes=[StrictIsAuthenticated])
    async def revoke_provider_workspace_access(
        self, info: strawberry.Info, provider_uuid: str
    ) -> PilotMutationOutcome:
        def _run():
            try:
                access = _access_for_info(info)
                revoke_provider_from_workspace(
                    access, provider_uuid=provider_uuid
                )
                return PilotMutationOutcome(ok=True, target_uuid=provider_uuid)
            except Exception as exc:  # noqa: BLE001
                return PilotMutationOutcome(ok=False, error=str(exc))

        return await sync_to_async(_run, thread_sensitive=True)()

    @strawberry.mutation(permission_classes=[StrictIsAuthenticated])
    async def assign_provider_work(
        self,
        info: strawberry.Info,
        provider_uuid: str,
        scope_type: str,
        event_type_id: int | None = None,
        event_id: int | None = None,
        visit_uuid: str | None = None,
        due_date: str | None = None,
        notes: str = "",
    ) -> PilotMutationOutcome:
        def _run():
            try:
                access = _access_for_info(info)
                prov = ProviderOrganization.objects.filter(
                    uuid=provider_uuid
                ).first()
                if prov is None:
                    raise ValueError("Provider organization not found.")
                et = (
                    EventType.objects.filter(
                        tenant=access.tenant, id=event_type_id
                    ).first()
                    if event_type_id
                    else None
                )
                ev = (
                    Event.objects.filter(
                        tenant=access.tenant, id=event_id
                    ).first()
                    if event_id
                    else None
                )
                cv = (
                    CanonicalVisit.objects.filter(
                        tenant=access.tenant, uuid=visit_uuid
                    ).first()
                    if visit_uuid
                    else None
                )
                assignment = assign_provider_work(
                    access,
                    provider=prov,
                    scope_type=scope_type,
                    event_type=et,
                    event=ev,
                    canonical_visit=cv,
                    due_date=_opt_date(due_date),
                    notes=notes,
                )
                return PilotMutationOutcome(
                    ok=True, target_uuid=assignment.submission_code
                )
            except Exception as exc:  # noqa: BLE001
                return PilotMutationOutcome(ok=False, error=str(exc))

        return await sync_to_async(_run, thread_sensitive=True)()

    @strawberry.mutation(permission_classes=[StrictIsAuthenticated])
    async def respond_provider_assignment(
        self,
        info: strawberry.Info,
        assignment_uuid: str,
        decision: str,
        response_note: str = "",
    ) -> PilotMutationOutcome:
        def _run():
            try:
                access = _access_for_info(info)
                a = respond_provider_assignment(
                    access,
                    assignment_uuid=assignment_uuid,
                    decision=decision,
                    response_note=response_note,
                )
                return PilotMutationOutcome(ok=True, target_uuid=str(a.uuid))
            except Exception as exc:  # noqa: BLE001
                return PilotMutationOutcome(ok=False, error=str(exc))

        return await sync_to_async(_run, thread_sensitive=True)()

    @strawberry.mutation(permission_classes=[StrictIsAuthenticated])
    async def revoke_provider_assignment(
        self, info: strawberry.Info, assignment_uuid: str
    ) -> PilotMutationOutcome:
        def _run():
            try:
                access = _access_for_info(info)
                a = revoke_provider_assignment(
                    access, assignment_uuid=assignment_uuid
                )
                return PilotMutationOutcome(ok=True, target_uuid=str(a.uuid))
            except Exception as exc:  # noqa: BLE001
                return PilotMutationOutcome(ok=False, error=str(exc))

        return await sync_to_async(_run, thread_sensitive=True)()

    @strawberry.mutation(permission_classes=[StrictIsAuthenticated])
    async def set_provider_source_inclusion(
        self,
        info: strawberry.Info,
        provider_uuid: str,
        include_in_combined_reporting: bool,
        notes: str = "",
    ) -> PilotMutationOutcome:
        def _run():
            try:
                access = _access_for_info(info)
                rule = set_provider_source_inclusion(
                    access,
                    provider_uuid=provider_uuid,
                    include_in_combined_reporting=include_in_combined_reporting,
                    notes=notes,
                )
                return PilotMutationOutcome(ok=True, target_uuid=str(rule.uuid))
            except Exception as exc:  # noqa: BLE001
                return PilotMutationOutcome(ok=False, error=str(exc))

        return await sync_to_async(_run, thread_sensitive=True)()

    @strawberry.mutation(permission_classes=[StrictIsAuthenticated])
    async def preview_provider_csv_import(
        self,
        info: strawberry.Info,
        provider_uuid: str,
        csv_text: str,
        mapping_uuid: str | None = None,
        preset_key: str | None = None,
        column_map_json: str | None = None,
    ) -> ImportPreviewResult:
        def _run():
            access = _access_for_info(info)
            cmap = json.loads(column_map_json) if column_map_json else None
            res = preview_provider_import(
                access,
                provider_uuid=provider_uuid,
                csv_text=csv_text,
                mapping_uuid=mapping_uuid,
                column_map=cmap,
                preset_key=preset_key,
            )
            return ImportPreviewResult(
                headers=res["headers"],
                resolved_map_json=json.dumps(res["resolved_map"]),
                total_rows=res["total_rows"],
                created_count=res["created_count"],
                updated_count=res["updated_count"],
                unchanged_count=res["unchanged_count"],
                review_count=res["review_count"],
                error_count=res["error_count"],
                rows=[
                    ImportRowPreviewItem(
                        row_uuid=None,
                        row_number=r["row_number"],
                        status=r["status"],
                        error_code=r["error_code"],
                        message=r["message"],
                        raw_row_json=json.dumps(r["raw_row"]),
                        normalized_row_json=json.dumps(
                            r["normalized_row"], default=str
                        ),
                    )
                    for r in res["rows"]
                ],
            )

        return await sync_to_async(_run, thread_sensitive=True)()

    @strawberry.mutation(permission_classes=[StrictIsAuthenticated])
    async def commit_provider_csv_import(
        self,
        info: strawberry.Info,
        provider_uuid: str,
        csv_text: str,
        filename: str = "upload.csv",
        mapping_uuid: str | None = None,
        preset_key: str | None = None,
        column_map_json: str | None = None,
        save_mapping_name: str | None = None,
        idempotency_key: str | None = None,
        is_historical: bool = False,
    ) -> PilotMutationOutcome:
        def _run():
            try:
                access = _access_for_info(info)
                cmap = json.loads(column_map_json) if column_map_json else None
                job = commit_provider_import(
                    access,
                    provider_uuid=provider_uuid,
                    csv_text=csv_text,
                    filename=filename,
                    mapping_uuid=mapping_uuid,
                    column_map=cmap,
                    preset_key=preset_key,
                    save_mapping_name=save_mapping_name,
                    idempotency_key=idempotency_key,
                    is_historical=is_historical,
                )
                return PilotMutationOutcome(ok=True, target_uuid=str(job.uuid))
            except Exception as exc:  # noqa: BLE001
                return PilotMutationOutcome(ok=False, error=str(exc))

        return await sync_to_async(_run, thread_sensitive=True)()

    @strawberry.mutation(permission_classes=[StrictIsAuthenticated])
    async def resolve_provider_import_row(
        self,
        info: strawberry.Info,
        row_uuid: str,
        retailer_id: int | None = None,
        confirmed_store_number: str | None = None,
        confirmed_address: str | None = None,
        confirmed_city: str | None = None,
        confirmed_state: str | None = None,
        save_store_alias: bool = False,
        product_id: int | None = None,
        save_product_alias: bool = True,
    ) -> PilotMutationOutcome:
        def _run():
            try:
                access = _access_for_info(info)
                r = resolve_provider_import_row(
                    access,
                    row_uuid=row_uuid,
                    retailer_id=retailer_id,
                    confirmed_store_number=confirmed_store_number,
                    confirmed_address=confirmed_address,
                    confirmed_city=confirmed_city,
                    confirmed_state=confirmed_state,
                    save_store_alias=save_store_alias,
                    product_id=product_id,
                    save_product_alias=save_product_alias,
                )
                return PilotMutationOutcome(ok=True, target_uuid=str(r.uuid))
            except Exception as exc:  # noqa: BLE001
                return PilotMutationOutcome(ok=False, error=str(exc))

        return await sync_to_async(_run, thread_sensitive=True)()

    @strawberry.mutation(permission_classes=[StrictIsAuthenticated])
    async def upsert_provider_agreement(
        self,
        info: strawberry.Info,
        provider_uuid: str,
        name: str,
        contracted_demo_fee: str | None = None,
        included_hours: str = "4.00",
        extra_hour_rate: str | None = None,
        mileage_rate: str | None = None,
        admin_fee_percent: str = "0.00",
        admin_fee_bases: list[str] | None = None,
        tax_percent: str = "0.00",
    ) -> PilotMutationOutcome:
        def _run():
            try:
                access = _access_for_info(info)
                ag = upsert_provider_agreement(
                    access,
                    provider_uuid=provider_uuid,
                    name=name,
                    contracted_demo_fee=_opt_dec(contracted_demo_fee),
                    included_hours=Decimal(included_hours),
                    extra_hour_rate=_opt_dec(extra_hour_rate),
                    mileage_rate=_opt_dec(mileage_rate),
                    admin_fee_percent=Decimal(admin_fee_percent),
                    admin_fee_bases=admin_fee_bases,
                    tax_percent=Decimal(tax_percent),
                )
                return PilotMutationOutcome(ok=True, target_uuid=str(ag.uuid))
            except Exception as exc:  # noqa: BLE001
                return PilotMutationOutcome(ok=False, error=str(exc))

        return await sync_to_async(_run, thread_sensitive=True)()

    @strawberry.mutation(permission_classes=[StrictIsAuthenticated])
    async def upsert_brand_cost_line(
        self,
        info: strawberry.Info,
        visit_uuid: str,
        category: str,
        source_type: str = "invoice_line",
        amount: str | None = None,
        is_confirmed_zero: bool = False,
        quantity: str | None = None,
        unit_rate: str | None = None,
        description: str = "",
        settlement_status: str = "invoiced",
        evidence_status: str | None = None,
        provider_uuid: str | None = None,
        agreement_uuid: str | None = None,
        line_uuid: str | None = None,
    ) -> PilotMutationOutcome:
        def _run():
            try:
                access = _access_for_info(info)
                line = upsert_brand_cost_line(
                    access,
                    visit_uuid=visit_uuid,
                    category=category,
                    source_type=source_type,
                    amount=_opt_dec(amount),
                    is_confirmed_zero=is_confirmed_zero,
                    quantity=_opt_dec(quantity),
                    unit_rate=_opt_dec(unit_rate),
                    description=description,
                    settlement_status=settlement_status,
                    evidence_status=evidence_status,
                    provider_uuid=provider_uuid,
                    agreement_uuid=agreement_uuid,
                    line_uuid=line_uuid,
                )
                return PilotMutationOutcome(ok=True, target_uuid=str(line.uuid))
            except Exception as exc:  # noqa: BLE001
                return PilotMutationOutcome(ok=False, error=str(exc))

        return await sync_to_async(_run, thread_sensitive=True)()

    @strawberry.mutation(permission_classes=[StrictIsAuthenticated])
    async def link_cost_evidence(
        self,
        info: strawberry.Info,
        cost_line_uuid: str,
        proof_url: str = "",
        label: str = "Receipt proof",
        recap_file_id: int | None = None,
        receipt_reading_id: int | None = None,
        verified_amount: str | None = None,
        is_itemized: bool = True,
    ) -> PilotMutationOutcome:
        def _run():
            try:
                access = _access_for_info(info)
                ev = link_receipt_evidence_to_cost_line(
                    access,
                    cost_line_uuid=cost_line_uuid,
                    proof_url=proof_url,
                    label=label,
                    recap_file_id=recap_file_id,
                    receipt_reading_id=receipt_reading_id,
                    verified_amount=_opt_dec(verified_amount),
                    is_itemized=is_itemized,
                )
                return PilotMutationOutcome(ok=True, target_uuid=str(ev.uuid))
            except Exception as exc:  # noqa: BLE001
                return PilotMutationOutcome(ok=False, error=str(exc))

        return await sync_to_async(_run, thread_sensitive=True)()

    @strawberry.mutation(permission_classes=[StrictIsAuthenticated])
    async def allocate_shared_visit_cost(
        self,
        info: strawberry.Info,
        total_amount: str,
        category: str,
        allocations_json: str,
        allocation_mode: str = "percentage",
        description: str = "",
        provider_uuid: str | None = None,
    ) -> PilotMutationOutcome:
        def _run():
            try:
                access = _access_for_info(info)
                if not access.can_edit_finance and not access.is_brand_admin:
                    raise PermissionError(
                        "Only brand administrators or finance reviewers can allocate shared costs."
                    )
                allocs = json.loads(allocations_json)
                # Ensure non-Ignite callers only allocate across visits in their own brand.
                if not access.is_ignite_admin:
                    for item in allocs:
                        v = CanonicalVisit.objects.filter(
                            uuid=item.get("visit_uuid")
                        ).first()
                        if v is None or v.tenant_id != access.tenant.id:
                            raise PermissionError(
                                "Cannot allocate costs onto visits outside your brand workspace."
                            )
                prov = (
                    ProviderOrganization.objects.filter(
                        uuid=provider_uuid
                    ).first()
                    if provider_uuid
                    else None
                )
                src, _ = allocate_shared_cost(
                    actor=access.user,
                    total_amount=Decimal(total_amount),
                    category=category,
                    allocations=allocs,
                    allocation_mode=allocation_mode,
                    provider=prov,
                    description=description,
                )
                return PilotMutationOutcome(ok=True, target_uuid=str(src.uuid))
            except Exception as exc:  # noqa: BLE001
                return PilotMutationOutcome(ok=False, error=str(exc))

        return await sync_to_async(_run, thread_sensitive=True)()

    @strawberry.mutation(permission_classes=[StrictIsAuthenticated])
    async def assign_finance_exception(
        self,
        info: strawberry.Info,
        exception_uuid: str,
        owner_user_id: int | None = None,
    ) -> PilotMutationOutcome:
        def _run():
            try:
                access = _access_for_info(info)
                exc = assign_finance_exception_owner(
                    access,
                    exception_uuid=exception_uuid,
                    owner_user_id=owner_user_id,
                )
                return PilotMutationOutcome(ok=True, target_uuid=str(exc.uuid))
            except Exception as exc:  # noqa: BLE001
                return PilotMutationOutcome(ok=False, error=str(exc))

        return await sync_to_async(_run, thread_sensitive=True)()

    @strawberry.mutation(permission_classes=[StrictIsAuthenticated])
    async def attach_finance_exception_proof(
        self,
        info: strawberry.Info,
        exception_uuid: str,
        proof_url: str = "",
        label: str = "Uploaded proof",
        is_itemized: bool = True,
        verified_amount: str | None = None,
    ) -> PilotMutationOutcome:
        def _run():
            try:
                access = _access_for_info(info)
                exc = attach_proof_to_finance_exception(
                    access,
                    exception_uuid=exception_uuid,
                    proof_url=proof_url,
                    label=label,
                    is_itemized=is_itemized,
                    verified_amount=_opt_dec(verified_amount),
                )
                return PilotMutationOutcome(ok=True, target_uuid=str(exc.uuid))
            except Exception as exc:  # noqa: BLE001
                return PilotMutationOutcome(ok=False, error=str(exc))

        return await sync_to_async(_run, thread_sensitive=True)()

    @strawberry.mutation(permission_classes=[StrictIsAuthenticated])
    async def fix_finance_exception_match(
        self,
        info: strawberry.Info,
        exception_uuid: str,
        visit_uuid: str,
        cost_line_uuid: str | None = None,
        category: str = "product",
    ) -> PilotMutationOutcome:
        def _run():
            try:
                access = _access_for_info(info)
                exc = fix_finance_exception_match(
                    access,
                    exception_uuid=exception_uuid,
                    visit_uuid=visit_uuid,
                    cost_line_uuid=cost_line_uuid,
                    category=category,
                )
                return PilotMutationOutcome(ok=True, target_uuid=str(exc.uuid))
            except Exception as exc:  # noqa: BLE001
                return PilotMutationOutcome(ok=False, error=str(exc))

        return await sync_to_async(_run, thread_sensitive=True)()

    @strawberry.mutation(permission_classes=[StrictIsAuthenticated])
    async def request_finance_exception_clarification(
        self, info: strawberry.Info, exception_uuid: str, note: str
    ) -> PilotMutationOutcome:
        def _run():
            try:
                access = _access_for_info(info)
                exc = request_finance_clarification(
                    access, exception_uuid=exception_uuid, note=note
                )
                return PilotMutationOutcome(ok=True, target_uuid=str(exc.uuid))
            except Exception as exc:  # noqa: BLE001
                return PilotMutationOutcome(ok=False, error=str(exc))

        return await sync_to_async(_run, thread_sensitive=True)()

    @strawberry.mutation(permission_classes=[StrictIsAuthenticated])
    async def resolve_finance_exception(
        self,
        info: strawberry.Info,
        exception_uuid: str,
        resolution_status: str,
        reason: str,
        updated_amount: str | None = None,
    ) -> PilotMutationOutcome:
        def _run():
            try:
                access = _access_for_info(info)
                exc = resolve_finance_exception(
                    access,
                    exception_uuid=exception_uuid,
                    resolution_status=resolution_status,
                    reason=reason,
                    updated_amount=_opt_dec(updated_amount),
                )
                return PilotMutationOutcome(ok=True, target_uuid=str(exc.uuid))
            except Exception as exc:  # noqa: BLE001
                return PilotMutationOutcome(ok=False, error=str(exc))

        return await sync_to_async(_run, thread_sensitive=True)()

    @strawberry.mutation(permission_classes=[StrictIsAuthenticated])
    async def import_retailer_pos_sales(
        self,
        info: strawberry.Info,
        retailer_banner: str,
        csv_text: str,
        filename: str = "pos.csv",
        feed_completeness: str = "complete",
        column_map_json: str | None = None,
    ) -> PilotMutationOutcome:
        def _run():
            try:
                access = _access_for_info(info)
                cmap = json.loads(column_map_json) if column_map_json else None
                imp = import_retailer_pos_csv(
                    access,
                    retailer_banner=retailer_banner,
                    csv_text=csv_text,
                    filename=filename,
                    column_map=cmap,
                    feed_completeness=feed_completeness,
                )
                return PilotMutationOutcome(ok=True, target_uuid=str(imp.uuid))
            except Exception as exc:  # noqa: BLE001
                return PilotMutationOutcome(ok=False, error=str(exc))

        return await sync_to_async(_run, thread_sensitive=True)()

    @strawberry.mutation(permission_classes=[StrictIsAuthenticated])
    async def resolve_unmatched_pos_row(
        self,
        info: strawberry.Info,
        row_uuid: str,
        retailer_id: int | None = None,
        store_key: str | None = None,
        store_label: str | None = None,
        product_id: int | None = None,
        save_product_alias: bool = True,
    ) -> PilotMutationOutcome:
        def _run():
            try:
                access = _access_for_info(info)
                row = resolve_pos_sales_row(
                    access,
                    row_uuid=row_uuid,
                    retailer_id=retailer_id,
                    store_key=store_key,
                    store_label=store_label,
                    product_id=product_id,
                    save_product_alias=save_product_alias,
                )
                return PilotMutationOutcome(ok=True, target_uuid=str(row.uuid))
            except Exception as exc:  # noqa: BLE001
                return PilotMutationOutcome(ok=False, error=str(exc))

        return await sync_to_async(_run, thread_sensitive=True)()

    @strawberry.mutation(permission_classes=[StrictIsAuthenticated])
    async def link_crew_purchase_adjustment(
        self,
        info: strawberry.Info,
        visit_uuid: str,
        product_id: int,
        units_purchased: int,
        purchase_date: str | None = None,
        retailer_banner: str = "",
        transaction_ref: str = "",
    ) -> PilotMutationOutcome:
        def _run():
            try:
                access = _access_for_info(info)
                adj = record_crew_purchase_adjustment(
                    access,
                    visit_uuid=visit_uuid,
                    product_id=product_id,
                    units_purchased=units_purchased,
                    purchase_date=_opt_date(purchase_date),
                    retailer_banner=retailer_banner,
                    transaction_ref=transaction_ref,
                )
                return PilotMutationOutcome(ok=True, target_uuid=str(adj.uuid))
            except Exception as exc:  # noqa: BLE001
                return PilotMutationOutcome(ok=False, error=str(exc))

        return await sync_to_async(_run, thread_sensitive=True)()

    @strawberry.mutation(permission_classes=[StrictIsAuthenticated])
    async def set_workspace_default_contribution(
        self, info: strawberry.Info, contribution_per_unit: str
    ) -> PilotMutationOutcome:
        def _run():
            try:
                access = _access_for_info(info)
                if not access.can_manage_pos:
                    raise PermissionError(
                        "You do not have permission to configure contribution margin."
                    )
                profile = ensure_workspace_profile(access.tenant)
                profile.default_contribution_per_unit = quantize_money(
                    contribution_per_unit
                )
                profile.save(
                    update_fields=[
                        "default_contribution_per_unit",
                        "updated_at",
                    ]
                )
                return PilotMutationOutcome(
                    ok=True, target_uuid=str(profile.uuid)
                )
            except Exception as exc:  # noqa: BLE001
                return PilotMutationOutcome(ok=False, error=str(exc))

        return await sync_to_async(_run, thread_sensitive=True)()

    @strawberry.mutation(permission_classes=[StrictIsAuthenticated])
    async def create_store_insight_action(
        self,
        info: strawberry.Info,
        finding_id: str,
        action_type: str,
        selected_store_key: str | None = None,
        idempotency_key: str | None = None,
        provider_uuid: str | None = None,
        owner_user_id: int | None = None,
        target_date: str | None = None,
        due_date: str | None = None,
        budget_amount: str | None = None,
        hold_reason: str = "",
        next_review_date: str | None = None,
        notes: str = "",
    ) -> PilotMutationOutcome:
        def _run():
            try:
                access = _access_for_info(info)
                act = create_store_recommendation_action(
                    access,
                    finding_id=finding_id,
                    action_type=action_type,
                    selected_store_key=selected_store_key,
                    idempotency_key=idempotency_key,
                    provider_uuid=provider_uuid,
                    owner_user_id=owner_user_id,
                    target_date=_opt_date(target_date),
                    due_date=_opt_date(due_date),
                    budget_amount=_opt_dec(budget_amount),
                    hold_reason=hold_reason,
                    next_review_date=_opt_date(next_review_date),
                    notes=notes,
                )
                return PilotMutationOutcome(ok=True, target_uuid=str(act.uuid))
            except Exception as exc:  # noqa: BLE001
                return PilotMutationOutcome(ok=False, error=str(exc))

        return await sync_to_async(_run, thread_sensitive=True)()

    @strawberry.mutation(permission_classes=[StrictIsAuthenticated])
    async def respond_store_insight_action(
        self,
        info: strawberry.Info,
        action_uuid: str,
        decision: str,
        note: str = "",
    ) -> PilotMutationOutcome:
        def _run():
            try:
                access = _access_for_info(info)
                act = respond_store_action(
                    access,
                    action_uuid=action_uuid,
                    decision=decision,
                    note=note,
                )
                return PilotMutationOutcome(ok=True, target_uuid=str(act.uuid))
            except Exception as exc:  # noqa: BLE001
                return PilotMutationOutcome(ok=False, error=str(exc))

        return await sync_to_async(_run, thread_sensitive=True)()

    @strawberry.mutation(permission_classes=[StrictIsAuthenticated])
    async def reassign_store_insight_action_provider(
        self,
        info: strawberry.Info,
        action_uuid: str,
        new_provider_uuid: str,
        note: str = "",
    ) -> PilotMutationOutcome:
        def _run():
            try:
                access = _access_for_info(info)
                act = reassign_store_action_provider(
                    access,
                    action_uuid=action_uuid,
                    new_provider_uuid=new_provider_uuid,
                    note=note,
                )
                return PilotMutationOutcome(ok=True, target_uuid=str(act.uuid))
            except Exception as exc:  # noqa: BLE001
                return PilotMutationOutcome(ok=False, error=str(exc))

        return await sync_to_async(_run, thread_sensitive=True)()

    @strawberry.mutation(permission_classes=[StrictIsAuthenticated])
    async def verify_restock_store_action(
        self,
        info: strawberry.Info,
        action_uuid: str,
        verification_method: str,
        verification_note: str = "",
        recap_file_id: int | None = None,
        followup_visit_uuid: str | None = None,
    ) -> PilotMutationOutcome:
        def _run():
            try:
                access = _access_for_info(info)
                act = verify_restock_action(
                    access,
                    action_uuid=action_uuid,
                    verification_method=verification_method,
                    verification_note=verification_note,
                    recap_file_id=recap_file_id,
                    followup_visit_uuid=followup_visit_uuid,
                )
                return PilotMutationOutcome(ok=True, target_uuid=str(act.uuid))
            except Exception as exc:  # noqa: BLE001
                return PilotMutationOutcome(ok=False, error=str(exc))

        return await sync_to_async(_run, thread_sensitive=True)()

    @strawberry.mutation(permission_classes=[StrictIsAuthenticated])
    async def complete_rebook_store_action(
        self,
        info: strawberry.Info,
        action_uuid: str,
        samples_distributed: int | None = None,
        consumers_sampled: int | None = None,
        rep_observed_units: int | None = None,
        custom_recap_id: int | None = None,
        notes: str = "",
    ) -> PilotMutationOutcome:
        def _run():
            try:
                access = _access_for_info(info)
                act = complete_rebook_return_visit(
                    access,
                    action_uuid=action_uuid,
                    samples_distributed=samples_distributed,
                    consumers_sampled=consumers_sampled,
                    rep_observed_units=rep_observed_units,
                    custom_recap_id=custom_recap_id,
                    notes=notes,
                )
                return PilotMutationOutcome(ok=True, target_uuid=str(act.uuid))
            except Exception as exc:  # noqa: BLE001
                return PilotMutationOutcome(ok=False, error=str(exc))

        return await sync_to_async(_run, thread_sensitive=True)()

    @strawberry.mutation(permission_classes=[StrictIsAuthenticated])
    async def create_workspace_shared_link(
        self,
        info: strawberry.Info,
        resource_type: str = "visit",
        visit_uuid: str | None = None,
    ) -> PilotMutationOutcome:
        def _run():
            try:
                access = _access_for_info(info)
                link = create_workspace_shared_link(
                    access,
                    resource_type=resource_type,
                    visit_uuid=visit_uuid,
                )
                return PilotMutationOutcome(ok=True, target_uuid=link.token)
            except Exception as exc:  # noqa: BLE001
                return PilotMutationOutcome(ok=False, error=str(exc))

        return await sync_to_async(_run, thread_sensitive=True)()

    @strawberry.mutation(permission_classes=[StrictIsAuthenticated])
    async def revoke_workspace_shared_link(
        self, info: strawberry.Info, token: str
    ) -> PilotMutationOutcome:
        def _run():
            try:
                access = _access_for_info(info)
                link = revoke_workspace_shared_link(access, token=token)
                return PilotMutationOutcome(ok=True, target_uuid=link.token)
            except Exception as exc:  # noqa: BLE001
                return PilotMutationOutcome(ok=False, error=str(exc))

        return await sync_to_async(_run, thread_sensitive=True)()

    @strawberry.mutation(permission_classes=[])
    async def start_self_serve_onboarding(
        self,
        info: strawberry.Info,
        email: str = "",
        password: str = "",
        first_name: str = "",
        last_name: str = "",
        brand_name: str = "",
        business_timezone: str = "America/Chicago",
        accept_terms: bool = True,
        force_new_workspace: bool = False,
    ) -> OnboardingSessionPayload:
        def _run():
            try:
                req = info.context.request
                actor_user = (
                    req.user
                    if getattr(req, "user", None) is not None
                    and req.user.is_authenticated
                    else None
                )
                ip_addr = (
                    req.META.get("HTTP_X_FORWARDED_FOR", "").split(",")[0].strip()
                    or req.META.get("REMOTE_ADDR", "127.0.0.1")
                )
                ua = req.META.get("HTTP_USER_AGENT", "spark-self-serve")
                res = start_or_resume_self_serve_onboarding(
                    authenticated_user=actor_user,
                    email=email,
                    password=password,
                    first_name=first_name,
                    last_name=last_name,
                    brand_name=brand_name,
                    business_timezone=business_timezone,
                    accept_terms=accept_terms,
                    force_new_workspace=force_new_workspace,
                    client_ip=ip_addr,
                    user_agent=ua,
                )
                return OnboardingSessionPayload(
                    ok=True,
                    token=res["token"],
                    refresh_token=res["refresh_token"],
                    tenant_uuid=str(res["tenant"].uuid),
                    tenant_name=res["tenant"].name,
                    tenant_slug=res["tenant"].slug,
                    membership_uuid=(
                        str(res["membership"].uuid)
                        if res["membership"] is not None
                        else None
                    ),
                    workspace_role=(
                        res["membership"].workspace_role
                        if res["membership"] is not None
                        else "brand_admin"
                    ),
                    resumed_existing=res["resumed_existing"],
                    resolved_managed=res["resolved_managed"],
                    joined_invitation=res["joined_invitation"],
                )
            except Exception as exc:  # noqa: BLE001
                return OnboardingSessionPayload(ok=False, error=str(exc))

        return await sync_to_async(_run, thread_sensitive=True)()

    @strawberry.mutation(permission_classes=[StrictIsAuthenticated])
    async def select_onboarding_starter_program(
        self,
        info: strawberry.Info,
        starter_key: str,
        custom_program_name: str = "",
        starter_sku_name: str = "",
    ) -> PilotMutationOutcome:
        def _run():
            try:
                access = _access_for_info(info)
                progress = select_onboarding_starter_program(
                    access,
                    starter_key=starter_key,
                    custom_program_name=custom_program_name,
                    starter_sku_name=starter_sku_name,
                )
                return PilotMutationOutcome(
                    ok=True,
                    target_uuid=(
                        progress.starter_assignment.submission_code
                        if progress.starter_assignment
                        else ""
                    ),
                    status=(
                        progress.starter_event_type.name
                        if progress.starter_event_type
                        else ""
                    ),
                )
            except Exception as exc:  # noqa: BLE001
                return PilotMutationOutcome(ok=False, error=str(exc))

        return await sync_to_async(_run, thread_sensitive=True)()

    @strawberry.mutation(permission_classes=[StrictIsAuthenticated])
    async def onboarding_invite_contributor(
        self,
        info: strawberry.Info,
        email: str,
        workspace_role: str = "field_contributor",
        provider_name: str = "",
    ) -> PilotMutationOutcome:
        def _run():
            try:
                access = _access_for_info(info)
                inv = onboarding_invite_contributor(
                    access,
                    email=email,
                    workspace_role=workspace_role,
                    provider_name=provider_name,
                )
                return PilotMutationOutcome(
                    ok=True,
                    target_uuid=inv.token,
                    status=inv.workspace_role,
                )
            except Exception as exc:  # noqa: BLE001
                return PilotMutationOutcome(ok=False, error=str(exc))

        return await sync_to_async(_run, thread_sensitive=True)()

    @strawberry.mutation(permission_classes=[StrictIsAuthenticated])
    async def schedule_standalone_visit(
        self,
        info: strawberry.Info,
        store_name: str,
        visit_date: str,
        address: str = "",
        city: str = "",
        state_code: str = "",
        provider_uuid: str | None = None,
        event_type_id: int | None = None,
        notes: str = "",
    ) -> PilotMutationOutcome:
        def _run():
            try:
                access = _access_for_info(info)
                visit, assignment = schedule_standalone_visit(
                    access,
                    store_name=store_name,
                    visit_date=date.fromisoformat(visit_date),
                    address=address,
                    city=city,
                    state_code=state_code,
                    provider_uuid=provider_uuid,
                    event_type_id=event_type_id,
                    notes=notes,
                )
                return PilotMutationOutcome(
                    ok=True,
                    target_uuid=str(visit.uuid),
                    status=assignment.submission_code,
                )
            except Exception as exc:  # noqa: BLE001
                return PilotMutationOutcome(ok=False, error=str(exc))

        return await sync_to_async(_run, thread_sensitive=True)()

    @strawberry.mutation(permission_classes=[StrictIsAuthenticated])
    async def approve_canonical_visit_recap(
        self, info: strawberry.Info, visit_uuid: str
    ) -> PilotMutationOutcome:
        def _run():
            try:
                access = _access_for_info(info)
                visit, shared_link = approve_canonical_visit_recap(
                    access, visit_uuid=visit_uuid
                )
                return PilotMutationOutcome(
                    ok=True,
                    target_uuid=shared_link.token,
                    status=visit.status,
                )
            except Exception as exc:  # noqa: BLE001
                return PilotMutationOutcome(ok=False, error=str(exc))

        return await sync_to_async(_run, thread_sensitive=True)()

    @strawberry.mutation(permission_classes=[StrictIsAuthenticated])
    async def reverse_canonical_visit(
        self,
        info: strawberry.Info,
        visit_uuid: str,
        new_status: str = "cancelled",
        reason: str = "",
    ) -> PilotMutationOutcome:
        def _run():
            try:
                access = _access_for_info(info)
                visit = reverse_canonical_visit_completion(
                    access,
                    visit_uuid=visit_uuid,
                    new_status=new_status,
                    reason=reason,
                )
                return PilotMutationOutcome(
                    ok=True,
                    target_uuid=str(visit.uuid),
                    status=visit.status,
                )
            except Exception as exc:  # noqa: BLE001
                return PilotMutationOutcome(ok=False, error=str(exc))

        return await sync_to_async(_run, thread_sensitive=True)()

    @strawberry.mutation(permission_classes=[StrictIsAuthenticated])
    async def create_subscription_checkout(
        self,
        info: strawberry.Info,
        target_plan_code: str,
        explicit_terms_accepted: bool,
        idempotency_key: str | None = None,
        success_url: str | None = None,
        cancel_url: str | None = None,
    ) -> CheckoutSessionOutcome:
        def _run():
            try:
                access = _access_for_info(info)
                record = create_subscription_checkout_session(
                    access,
                    target_plan_code=target_plan_code,
                    explicit_terms_accepted=explicit_terms_accepted,
                    idempotency_key=idempotency_key,
                    success_url=success_url,
                    cancel_url=cancel_url,
                )
                return CheckoutSessionOutcome(
                    ok=True,
                    stripe_session_id=record.stripe_session_id,
                    checkout_url=record.checkout_url,
                    mode=record.mode,
                    already_completed=(
                        record.status == record.STATUS_COMPLETED
                    ),
                    disclosure=record.effective_summary,
                )
            except Exception as exc:  # noqa: BLE001
                return CheckoutSessionOutcome(ok=False, error=str(exc))

        return await sync_to_async(_run, thread_sensitive=True)()

    @strawberry.mutation(permission_classes=[StrictIsAuthenticated])
    async def complete_test_subscription_checkout(
        self, info: strawberry.Info, stripe_session_id: str
    ) -> PilotMutationOutcome:
        def _run():
            try:
                access = _access_for_info(info)
                res = complete_test_mode_checkout(
                    access, stripe_session_id=stripe_session_id
                )
                return PilotMutationOutcome(
                    ok=True,
                    target_uuid=stripe_session_id,
                    status=res.get("status") or "processed",
                )
            except Exception as exc:  # noqa: BLE001
                return PilotMutationOutcome(ok=False, error=str(exc))

        return await sync_to_async(_run, thread_sensitive=True)()

    @strawberry.mutation(permission_classes=[StrictIsAuthenticated])
    async def create_subscription_portal_session(
        self, info: strawberry.Info, return_url: str | None = None
    ) -> PortalSessionOutcome:
        def _run():
            try:
                access = _access_for_info(info)
                res = create_customer_portal_session(
                    access, return_url=return_url
                )
                return PortalSessionOutcome(
                    ok=True,
                    portal_url=res["portal_url"],
                    mode=res.get("mode", "test"),
                )
            except Exception as exc:  # noqa: BLE001
                return PortalSessionOutcome(ok=False, error=str(exc))

        return await sync_to_async(_run, thread_sensitive=True)()

    @strawberry.mutation(permission_classes=[StrictIsAuthenticated])
    async def change_subscription_plan(
        self,
        info: strawberry.Info,
        target_plan_code: str,
        explicit_terms_accepted: bool = False,
    ) -> PilotMutationOutcome:
        def _run():
            try:
                access = _access_for_info(info)
                sub = change_subscription_plan(
                    access,
                    target_plan_code=target_plan_code,
                    explicit_terms_accepted=explicit_terms_accepted,
                )
                return PilotMutationOutcome(
                    ok=True,
                    target_uuid=sub.plan_code,
                    status=sub.status,
                )
            except Exception as exc:  # noqa: BLE001
                return PilotMutationOutcome(ok=False, error=str(exc))

        return await sync_to_async(_run, thread_sensitive=True)()

    @strawberry.mutation(permission_classes=[StrictIsAuthenticated])
    async def cancel_subscription(
        self,
        info: strawberry.Info,
        immediate: bool = False,
        reason: str = "",
    ) -> PilotMutationOutcome:
        def _run():
            try:
                access = _access_for_info(info)
                sub = cancel_brand_subscription(
                    access, immediate=immediate, reason=reason
                )
                return PilotMutationOutcome(
                    ok=True,
                    target_uuid=(
                        sub.current_period_end.date().isoformat()
                        if sub.current_period_end
                        else ""
                    ),
                    status=sub.status,
                )
            except Exception as exc:  # noqa: BLE001
                return PilotMutationOutcome(ok=False, error=str(exc))

        return await sync_to_async(_run, thread_sensitive=True)()

    @strawberry.mutation(permission_classes=[StrictIsAuthenticated])
    async def approve_volume_band_or_overage(
        self,
        info: strawberry.Info,
        agreed_volume_band_visits: int | None = None,
        overage_agreement_approved: bool = False,
        overage_unit_price_usd: str | None = None,
    ) -> PilotMutationOutcome:
        def _run():
            try:
                access = _access_for_info(info)
                sub = approve_overage_or_volume_band(
                    access,
                    agreed_volume_band_visits=agreed_volume_band_visits,
                    overage_agreement_approved=overage_agreement_approved,
                    overage_unit_price_usd=_opt_dec(overage_unit_price_usd),
                )
                return PilotMutationOutcome(
                    ok=True,
                    target_uuid=str(sub.uuid),
                    status=sub.plan_code,
                )
            except Exception as exc:  # noqa: BLE001
                return PilotMutationOutcome(ok=False, error=str(exc))

        return await sync_to_async(_run, thread_sensitive=True)()

    @strawberry.mutation(permission_classes=[StrictIsAuthenticated])
    async def provision_pro_pilot_workspace(
        self,
        info: strawberry.Info,
        tenant_uuid: str,
        pilot_fee_usd: str = "1500.00",
        duration_days: int = 60,
        included_visits_monthly: int = 100,
    ) -> PilotMutationOutcome:
        def _run():
            try:
                access = _access_for_info(info)
                target_tenant = Tenant.objects.filter(
                    uuid=tenant_uuid
                ).first()
                if target_tenant is None:
                    raise ValueError("Target brand workspace not found.")
                sub = provision_pro_pilot(
                    actor=access.user,
                    tenant=target_tenant,
                    pilot_fee_usd=quantize_money(pilot_fee_usd),
                    duration_days=duration_days,
                    included_visits_monthly=included_visits_monthly,
                )
                return PilotMutationOutcome(
                    ok=True,
                    target_uuid=str(sub.uuid),
                    status=sub.status,
                )
            except Exception as exc:  # noqa: BLE001
                return PilotMutationOutcome(ok=False, error=str(exc))

        return await sync_to_async(_run, thread_sensitive=True)()
