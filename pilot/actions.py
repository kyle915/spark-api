"""Feature 6 service layer: Store actions (Rebook, Hold, Restock) from published
BrandInsights, chain-scope store picker, spend-approver & draft/AI guards,
idempotent execution, provider response & reassignment, restock verification,
return-visit outcome linkage, and weekly buyer budget-meeting store report."""

from __future__ import annotations

import base64
import csv
import io
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from typing import Any

import openpyxl
from django.db import IntegrityError, transaction
from django.utils import timezone

from brand_insights.facts import store_identity
from brand_insights.models import BrandInsight
from events.models import (
    Event,
    EventType,
    Request,
    RequestStatus,
    RequestType,
    Retailer,
    SchedulingStatus,
)
from events.retailer_aliases import chain_key as make_chain_key
from pilot.costs import compute_visit_cost_summary, quantize_money
from pilot.imports import brand_included_visits_queryset
from pilot.models import (
    CanonicalVisit,
    ProviderAssignment,
    ProviderOrganization,
    StoreRecommendationAction,
    WorkspaceMembership,
)
from pilot.permissions import ResolvedWorkspaceAccess, log_workspace_audit
from pilot.pos import (
    RETAILER_UNAVAILABLE_MESSAGE,
    build_retailer_pos_measurement,
)
from pilot.workspaces import _mint_submission_code
from recaps.models import CustomRecap, CustomRecapFile
from tenants.models import Tenant, User


def parse_brand_insight_id(raw_id: str | int | None) -> int | None:
    if raw_id is None:
        return None
    val = str(raw_id).strip()
    if val.startswith("BrandInsight:"):
        val = val.split(":", 1)[1]
    if not val.isdigit():
        return None
    return int(val)


def eligible_stores_for_insight(insight: BrandInsight) -> list[dict[str, Any]]:
    """Return eligible canonical stores for a BrandInsight:
    - `store:<key>` -> that exact store
    - `chain:<key>` -> all known stores of that chain in this brand workspace
    - `brand` -> all known stores in this brand workspace."""
    tenant = insight.tenant
    scope = insight.scope_key or ""

    known_stores: dict[str, dict[str, Any]] = {}

    for v in CanonicalVisit.objects.filter(tenant=tenant).exclude(store_key=""):
        known_stores.setdefault(
            v.store_key,
            {
                "store_key": v.store_key,
                "store_label": v.store_label or v.address or v.store_key,
                "chain_key": v.chain_key or make_chain_key(v.store_label),
                "retailer_id": v.retailer_id,
                "address": v.address or "",
                "market": v.market or v.city or v.state_code or "",
            },
        )

    for r in Retailer.objects.select_related("location", "location__state").filter(
        tenant=tenant
    ):
        loc = r.location if getattr(r, "location_id", None) else None
        rcity = loc.name if loc else ""
        rstate = (
            loc.state.code
            if loc and getattr(loc, "state_id", None) and loc.state
            else ""
        )
        s_key, s_label, c_key, _ = store_identity(
            r.name, r.address or "", rcity, rstate
        )
        if s_key:
            known_stores.setdefault(
                s_key,
                {
                    "store_key": s_key,
                    "store_label": s_label or r.name,
                    "chain_key": c_key or make_chain_key(r.name),
                    "retailer_id": r.id,
                    "address": r.address or "",
                    "market": rcity or rstate or "",
                },
            )

    if scope.startswith("store:"):
        target_key = scope[len("store:") :]
        if target_key in known_stores:
            return [known_stores[target_key]]
        return [
            {
                "store_key": target_key,
                "store_label": insight.subject or target_key,
                "chain_key": make_chain_key(insight.subject or target_key),
                "retailer_id": None,
                "address": "",
                "market": "",
            }
        ]

    if scope.startswith("chain:"):
        target_chain = scope[len("chain:") :]
        matches = [
            s
            for s in known_stores.values()
            if s["chain_key"] == target_chain
        ]
        matches.sort(key=lambda item: item["store_label"])
        return matches

    all_list = list(known_stores.values())
    all_list.sort(key=lambda item: item["store_label"])
    return all_list


def build_store_evidence_for_insight(
    access: ResolvedWorkspaceAccess, insight: BrandInsight, store_key: str
) -> dict[str, Any]:
    """Assemble cost + POS + visit count snapshot for `(insight, store_key)`."""
    visits = list(
        brand_included_visits_queryset(access.tenant)
        .prefetch_related("cost_lines")
        .filter(
            store_key=store_key,
            visit_date__gte=insight.period_start - timedelta(days=60),
            visit_date__lte=insight.period_end,
        )
    )
    supported_cost = Decimal("0.00")
    incomplete_cost = False
    rep_units = 0
    for v in visits:
        csum = compute_visit_cost_summary(v, list(v.cost_lines.all()))
        supported_cost += csum.supported_cost
        if csum.is_incomplete:
            incomplete_cost = True
        rep_units += v.rep_observed_units or 0

    pos_data = build_retailer_pos_measurement(
        access,
        period_start=insight.period_start - timedelta(days=28),
        period_end=insight.period_end,
        store_key=store_key,
    )
    return {
        "visit_count": (insight.facts or {}).get("visits")
        or (insight.facts or {}).get("recaps")
        or len(visits),
        "comparison_period": f"{insight.period_start.isoformat()} to {insight.period_end.isoformat()}",
        "supported_brand_cost": str(quantize_money(supported_cost)),
        "has_incomplete_cost": incomplete_cost,
        "rep_observed_units": rep_units,
        "pos_available": pos_data["available"],
        "adjusted_register_units": pos_data["totals"]["adjusted_register_units"],
        "pos_coverage_ratio": pos_data["coverage"]["coverage_ratio"],
    }


def _ensure_retail_sampling_types(
    tenant: Tenant, actor: User
) -> tuple[RequestType, RequestStatus, EventType]:
    req_type = RequestType.objects.filter(tenant=tenant).order_by("id").first()
    if req_type is None:
        req_type = RequestType.objects.create(
            tenant=tenant, name="Retail Sampling", created_by=actor
        )
    req_status = (
        RequestStatus.objects.filter(tenant=tenant, is_default=True).first()
        or RequestStatus.objects.filter(tenant=tenant).order_by("id").first()
    )
    if req_status is None:
        req_status = RequestStatus.objects.create(
            tenant=tenant,
            name="Pending",
            slug="pending",
            is_default=True,
            created_by=actor,
        )
    ev_type = (
        EventType.objects.filter(tenant=tenant, is_default=True).first()
        or EventType.objects.filter(tenant=tenant).order_by("id").first()
    )
    if ev_type is None:
        ev_type = EventType.objects.create(
            tenant=tenant,
            name="Retail Sampling",
            slug="retail-sampling",
            is_default=True,
            created_by=actor,
        )
    return req_type, req_status, ev_type


@transaction.atomic
def create_store_recommendation_action(
    access: ResolvedWorkspaceAccess,
    *,
    finding_id: str,
    action_type: str,
    selected_store_key: str | None = None,
    idempotency_key: str | None = None,
    provider_uuid: str | None = None,
    owner_user_id: int | None = None,
    target_date: date | None = None,
    due_date: date | None = None,
    budget_amount: Decimal | None = None,
    hold_reason: str = "",
    next_review_date: date | None = None,
    notes: str = "",
) -> StoreRecommendationAction:
    """Create an idempotent `Rebook`, `Hold`, or `Restock` action from a
    PUBLISHED `BrandInsight` finding. Rejects draft/hidden/stale findings and
    ad-hoc AI chat strings."""
    if not access.can_edit_brand_ops:
        raise PermissionError(
            "Only brand administrators or reviewers can create store actions."
        )

    parsed_id = parse_brand_insight_id(finding_id)
    if parsed_id is None:
        raise PermissionError(
            "Ad-hoc AI answers cannot book visits or trigger store actions; "
            "select a published BrandInsight finding."
        )

    insight = (
        BrandInsight.objects.select_related("tenant")
        .filter(id=parsed_id, tenant=access.tenant)
        .first()
    )
    if insight is None:
        raise PermissionError("BrandInsight finding not found in this workspace.")
    if insight.status != BrandInsight.STATUS_PUBLISHED:
        raise PermissionError(
            f"Cannot act on a '{insight.status}' finding: only published BrandInsights can trigger store actions."
        )

    if action_type not in {
        StoreRecommendationAction.ACTION_REBOOK,
        StoreRecommendationAction.ACTION_HOLD,
        StoreRecommendationAction.ACTION_RESTOCK,
    }:
        raise ValueError(f"Unsupported store action type: {action_type}")

    if (
        action_type == StoreRecommendationAction.ACTION_REBOOK
        and not access.can_approve_spend
    ):
        raise PermissionError(
            "Rebook requires spend approval authority in this brand workspace."
        )

    eligible = eligible_stores_for_insight(insight)
    if insight.scope_key.startswith("chain:") or insight.scope_key == "brand":
        if not selected_store_key:
            raise ValueError(
                "Chain-scoped findings require selecting an eligible store before acting."
            )
        chosen = next(
            (s for s in eligible if s["store_key"] == selected_store_key), None
        )
        if chosen is None:
            raise ValueError(
                f"Store '{selected_store_key}' is not an eligible store for finding {insight.scope_key}."
            )
    else:
        chosen = eligible[0]
        if selected_store_key and selected_store_key != chosen["store_key"]:
            raise ValueError(
                f"Store '{selected_store_key}' does not match finding store '{chosen['store_key']}'."
            )

    store_key = chosen["store_key"]
    store_label = chosen["store_label"]
    retailer_id = chosen.get("retailer_id")
    store_address = chosen.get("address") or store_label

    idem = (idempotency_key or "").strip() or (
        f"insight:{insight.id}:store:{store_key}:action:{action_type}:"
        f"date:{(target_date or due_date or next_review_date or insight.period_end).isoformat()}"
    )

    existing = StoreRecommendationAction.objects.filter(
        tenant=access.tenant, idempotency_key=idem
    ).first()
    if existing is not None:
        return existing

    provider = (
        ProviderOrganization.objects.filter(uuid=provider_uuid).first()
        if provider_uuid
        else None
    )
    owner = (
        User.objects.filter(id=owner_user_id).first()
        if owner_user_id is not None
        else access.user
    )

    if action_type == StoreRecommendationAction.ACTION_HOLD:
        clean_hold = (hold_reason or "").strip()
        if not clean_hold or next_review_date is None:
            raise ValueError(
                "Hold requires both a hold_reason and a next_review_date."
            )
    elif action_type == StoreRecommendationAction.ACTION_RESTOCK:
        if due_date is None:
            raise ValueError("Restock requires a due_date.")
    elif action_type == StoreRecommendationAction.ACTION_REBOOK:
        if target_date is None:
            target_date = timezone.now().date() + timedelta(days=7)

    evidence = build_store_evidence_for_insight(access, insight, store_key)
    decision_snapshot = {
        "finding_id": f"BrandInsight:{insight.id}",
        "kind": insight.kind,
        "code": insight.code,
        "scope_key": insight.scope_key,
        "subject": insight.subject,
        "body": insight.body,
        "period_start": insight.period_start.isoformat(),
        "period_end": insight.period_end.isoformat(),
        "facts": insight.facts,
        "store_key": store_key,
        "store_label": store_label,
        "evidence_at_decision": evidence,
        "decided_at": timezone.now().isoformat(),
    }

    linked_req: Request | None = None
    linked_assign: ProviderAssignment | None = None
    scheduled_visit: CanonicalVisit | None = None

    if action_type == StoreRecommendationAction.ACTION_REBOOK:
        assert target_date is not None
        req_type, req_status, ev_type = _ensure_retail_sampling_types(
            access.tenant, access.user
        )
        start_dt = timezone.make_aware(
            datetime.combine(target_date, time(11, 0)),
            timezone.get_current_timezone(),
        )
        end_dt = timezone.make_aware(
            datetime.combine(target_date, time(15, 0)),
            timezone.get_current_timezone(),
        )
        action_notes = (
            f"Rebooked from BrandInsight:{insight.id} ({insight.code}) at {store_label}. "
            + (notes or "").strip()
        ).strip()
        linked_req = Request.objects.create(
            tenant=access.tenant,
            name=f"Rebook · {store_label}"[:255],
            date=start_dt,
            start_time=start_dt,
            end_time=end_dt,
            address=store_address,
            retailer_id=retailer_id,
            retailer_name=store_label[:50],
            request_type=req_type,
            status=req_status,
            requestor_email=access.user.email,
            notes=action_notes,
            scheduling_status=SchedulingStatus.NEEDS_SCHEDULING,
        )
        ev = Event.objects.create(
            tenant=access.tenant,
            request=linked_req,
            event_type=ev_type,
            retailer_id=retailer_id,
            name=linked_req.name,
            date=start_dt,
            start_time=start_dt,
            end_time=end_dt,
            address=store_address,
            notes=action_notes,
            created_by=access.user,
        )
        scheduled_visit = CanonicalVisit.objects.create(
            tenant=access.tenant,
            event=ev,
            request=linked_req,
            primary_provider=provider,
            retailer_id=retailer_id,
            event_type=ev_type,
            store_key=store_key,
            store_label=store_label,
            chain_key=chosen.get("chain_key") or make_chain_key(store_label),
            chain_label=insight.subject or store_label,
            address=store_address,
            market=chosen.get("market") or "",
            visit_date=target_date,
            start_time=start_dt,
            end_time=end_dt,
            shift_key=f"rebook:{ev.id}",
            status=CanonicalVisit.STATUS_SCHEDULED,
        )
        if provider is not None:
            linked_assign = ProviderAssignment.objects.create(
                tenant=access.tenant,
                provider=provider,
                scope_type=ProviderAssignment.SCOPE_VISIT,
                event_type=ev_type,
                request=linked_req,
                event=ev,
                canonical_visit=scheduled_visit,
                assigned_owner=owner,
                status=ProviderAssignment.STATUS_ASSIGNED,
                submission_code=_mint_submission_code(),
                due_date=target_date,
                notes=action_notes,
                created_by=access.user,
            )
        initial_status = StoreRecommendationAction.STATUS_ASSIGNED
    elif action_type == StoreRecommendationAction.ACTION_HOLD:
        initial_status = StoreRecommendationAction.STATUS_ON_HOLD
    else:
        initial_status = StoreRecommendationAction.STATUS_AWAITING_VERIFICATION

    history_entry = {
        "at": timezone.now().isoformat(),
        "actor": access.user.email,
        "event": f"created_{action_type}",
        "provider": provider.name if provider else None,
        "status": initial_status,
    }

    try:
        action = StoreRecommendationAction.objects.create(
            tenant=access.tenant,
            brand_insight=insight,
            finding_id_snapshot=f"BrandInsight:{insight.id}",
            finding_code=insight.code,
            finding_scope_key=insight.scope_key,
            store_key=store_key,
            store_label=store_label,
            retailer_id=retailer_id,
            action_type=action_type,
            idempotency_key=idem,
            status=initial_status,
            decision_snapshot=decision_snapshot,
            provider=provider,
            owner=owner,
            spend_approved_by=(
                access.user
                if action_type == StoreRecommendationAction.ACTION_REBOOK
                else None
            ),
            budget_amount=(
                quantize_money(budget_amount)
                if budget_amount is not None
                else None
            ),
            target_date=target_date,
            due_date=due_date or target_date,
            hold_reason=(hold_reason or "").strip(),
            next_review_date=next_review_date,
            linked_request=linked_req,
            provider_assignment=linked_assign,
            outcome_visit=scheduled_visit,
            history_log=[history_entry],
            created_by=access.user,
        )
    except IntegrityError:
        existing = StoreRecommendationAction.objects.filter(
            tenant=access.tenant, idempotency_key=idem
        ).first()
        if existing is not None:
            return existing
        raise

    log_workspace_audit(
        tenant=access.tenant,
        actor=access.user,
        category="insight_action",
        action=f"store_action_{action_type}",
        target_type="StoreRecommendationAction",
        target_id=str(action.uuid),
        summary=(
            f"Created {action_type.upper()} action for {store_label} "
            f"from BrandInsight:{insight.id} ({insight.code})."
        ),
    )
    return action


@transaction.atomic
def respond_store_action(
    access: ResolvedWorkspaceAccess,
    *,
    action_uuid: str,
    decision: str,
    note: str = "",
) -> StoreRecommendationAction:
    """Allow the assigned provider manager (or brand admin) to acknowledge
    (`accepted`) or `declined` a store action."""
    action = (
        StoreRecommendationAction.objects.select_related(
            "provider", "provider_assignment"
        )
        .filter(tenant=access.tenant, uuid=action_uuid)
        .first()
    )
    if action is None:
        raise ValueError("Store recommendation action not found.")

    can_respond = access.is_brand_admin or (
        access.workspace_role == WorkspaceMembership.ROLE_PROVIDER_MANAGER
        and access.provider is not None
        and action.provider_id == access.provider.id
    )
    if not can_respond:
        raise PermissionError(
            "Only the assigned provider manager or brand admin can respond."
        )
    if decision not in {"accepted", "declined"}:
        raise ValueError("decision must be 'accepted' or 'declined'.")

    now = timezone.now()
    action.provider_response_status = decision
    action.provider_response_note = (note or "").strip()
    action.provider_responded_by = access.user
    action.provider_responded_at = now
    if action.action_type == StoreRecommendationAction.ACTION_REBOOK:
        action.status = (
            StoreRecommendationAction.STATUS_ACCEPTED
            if decision == "accepted"
            else StoreRecommendationAction.STATUS_DECLINED
        )
    hist = list(action.history_log or [])
    hist.append(
        {
            "at": now.isoformat(),
            "actor": access.user.email,
            "event": f"provider_{decision}",
            "provider": action.provider.name if action.provider else None,
            "note": action.provider_response_note,
        }
    )
    action.history_log = hist
    action.save()

    if action.provider_assignment is not None:
        pa = action.provider_assignment
        pa.status = (
            ProviderAssignment.STATUS_ACCEPTED
            if decision == "accepted"
            else ProviderAssignment.STATUS_DECLINED
        )
        pa.response_note = action.provider_response_note
        pa.responded_by = access.user
        pa.responded_at = now
        pa.save(
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
        category="insight_action",
        action=f"store_action_{decision}",
        target_type="StoreRecommendationAction",
        target_id=str(action.uuid),
        summary=f"Provider {decision} {action.action_type} at {action.store_label}.",
    )
    return action


@transaction.atomic
def reassign_store_action_provider(
    access: ResolvedWorkspaceAccess,
    *,
    action_uuid: str,
    new_provider_uuid: str,
    note: str = "",
) -> StoreRecommendationAction:
    """Switch a store action to another agency while preserving decision-date
    snapshot, prior provider attribution, and full store history."""
    if not access.can_edit_brand_ops:
        raise PermissionError(
            "Only brand administrators or reviewers can reassign providers."
        )
    action = (
        StoreRecommendationAction.objects.select_related(
            "provider", "provider_assignment", "outcome_visit", "linked_request"
        )
        .filter(tenant=access.tenant, uuid=action_uuid)
        .first()
    )
    if action is None:
        raise ValueError("Store recommendation action not found.")
    new_provider = ProviderOrganization.objects.filter(
        uuid=new_provider_uuid
    ).first()
    if new_provider is None:
        raise ValueError("Target provider organization not found.")

    old_provider = action.provider
    if action.provider_assignment is not None:
        action.provider_assignment.status = ProviderAssignment.STATUS_REVOKED
        action.provider_assignment.save(update_fields=["status", "updated_at"])

    new_pa: ProviderAssignment | None = None
    if action.outcome_visit is not None:
        action.outcome_visit.primary_provider = new_provider
        action.outcome_visit.save(
            update_fields=["primary_provider", "updated_at"]
        )
        new_pa = ProviderAssignment.objects.create(
            tenant=access.tenant,
            provider=new_provider,
            scope_type=ProviderAssignment.SCOPE_VISIT,
            event_type=action.outcome_visit.event_type,
            request=action.linked_request,
            event=action.outcome_visit.event,
            canonical_visit=action.outcome_visit,
            assigned_owner=action.owner,
            status=ProviderAssignment.STATUS_ASSIGNED,
            submission_code=_mint_submission_code(),
            due_date=action.due_date,
            notes=(note or f"Reassigned to {new_provider.name}").strip(),
            created_by=access.user,
        )

    action.previous_provider = old_provider
    action.provider = new_provider
    action.provider_assignment = new_pa
    action.provider_response_status = "pending"
    action.provider_response_note = ""
    action.status = StoreRecommendationAction.STATUS_REASSIGNED
    hist = list(action.history_log or [])
    hist.append(
        {
            "at": timezone.now().isoformat(),
            "actor": access.user.email,
            "event": "provider_reassigned",
            "from_provider": old_provider.name if old_provider else None,
            "to_provider": new_provider.name,
            "note": (note or "").strip(),
        }
    )
    action.history_log = hist
    action.save()
    return action


@transaction.atomic
def verify_restock_action(
    access: ResolvedWorkspaceAccess,
    *,
    action_uuid: str,
    verification_method: str,
    verification_note: str = "",
    recap_file_id: int | None = None,
    followup_visit_uuid: str | None = None,
) -> StoreRecommendationAction:
    """Verify a Restock action via `note`, `photo`, or `followup_visit`."""
    action = StoreRecommendationAction.objects.filter(
        tenant=access.tenant, uuid=action_uuid
    ).first()
    if action is None:
        raise ValueError("Store recommendation action not found.")
    if action.action_type != StoreRecommendationAction.ACTION_RESTOCK:
        raise ValueError("Only Restock actions use restock verification.")

    can_verify = access.can_edit_brand_ops or (
        access.workspace_role == WorkspaceMembership.ROLE_PROVIDER_MANAGER
        and access.provider is not None
        and action.provider_id == access.provider.id
    )
    if not can_verify:
        raise PermissionError(
            "You do not have permission to verify this restock action."
        )

    if verification_method not in {
        StoreRecommendationAction.VERIFY_NOTE,
        StoreRecommendationAction.VERIFY_PHOTO,
        StoreRecommendationAction.VERIFY_FOLLOWUP_VISIT,
    }:
        raise ValueError(
            "verification_method must be 'note', 'photo', or 'followup_visit'."
        )

    clean_note = (verification_note or "").strip()
    if (
        verification_method == StoreRecommendationAction.VERIFY_NOTE
        and not clean_note
    ):
        raise ValueError("Note verification requires a non-empty note.")

    photo_file: CustomRecapFile | None = None
    if verification_method == StoreRecommendationAction.VERIFY_PHOTO:
        if recap_file_id is not None:
            photo_file = CustomRecapFile.objects.filter(
                id=recap_file_id, custom_recap__tenant=access.tenant
            ).first()
        if photo_file is None and not clean_note:
            raise ValueError(
                "Photo verification requires a recap photo file or photo URL note."
            )

    if verification_method == StoreRecommendationAction.VERIFY_FOLLOWUP_VISIT:
        if not followup_visit_uuid:
            raise ValueError("followup_visit_uuid is required.")
        fv = CanonicalVisit.objects.filter(
            tenant=access.tenant, uuid=followup_visit_uuid
        ).first()
        if fv is None:
            raise ValueError("Follow-up canonical visit not found.")
        action.outcome_visit = fv

    now = timezone.now()
    action.restock_verification_method = verification_method
    action.restock_verification_note = clean_note
    action.restock_photo_file = photo_file
    action.restock_verified_by = access.user
    action.restock_verified_at = now
    action.status = StoreRecommendationAction.STATUS_COMPLETED
    hist = list(action.history_log or [])
    hist.append(
        {
            "at": now.isoformat(),
            "actor": access.user.email,
            "event": f"restock_verified_{verification_method}",
            "note": clean_note,
        }
    )
    action.history_log = hist
    action.save()

    log_workspace_audit(
        tenant=access.tenant,
        actor=access.user,
        category="insight_action",
        action="restock_verified",
        target_type="StoreRecommendationAction",
        target_id=str(action.uuid),
        summary=f"Verified restock at {action.store_label} via {verification_method}.",
    )
    return action


@transaction.atomic
def complete_rebook_return_visit(
    access: ResolvedWorkspaceAccess,
    *,
    action_uuid: str,
    samples_distributed: int | None = None,
    consumers_sampled: int | None = None,
    rep_observed_units: int | None = None,
    custom_recap_id: int | None = None,
    notes: str = "",
) -> StoreRecommendationAction:
    """Record or link completion & recap outcome of a Rebook return visit so
    the finding -> action -> assignment -> return visit -> recap chain stays
    intact in store history across weekly refreshes."""
    action = (
        StoreRecommendationAction.objects.select_related(
            "outcome_visit", "provider_assignment", "provider"
        )
        .filter(tenant=access.tenant, uuid=action_uuid)
        .first()
    )
    if action is None:
        raise ValueError("Store recommendation action not found.")

    can_complete = access.can_edit_brand_ops or (
        access.workspace_role == WorkspaceMembership.ROLE_PROVIDER_MANAGER
        and access.provider is not None
        and action.provider_id == access.provider.id
    )
    if not can_complete:
        raise PermissionError(
            "You do not have permission to complete this return visit."
        )

    visit = action.outcome_visit
    if visit is None:
        raise ValueError("Action has no scheduled return visit.")

    now = timezone.now()
    if samples_distributed is not None:
        visit.samples_distributed = samples_distributed
    if consumers_sampled is not None:
        visit.consumers_sampled = consumers_sampled
        visit.total_engagements = consumers_sampled
    if rep_observed_units is not None:
        visit.rep_observed_units = rep_observed_units
    visit.status = CanonicalVisit.STATUS_COMPLETED
    visit.completed_at = now
    visit.save()

    recap_obj: CustomRecap | None = None
    if custom_recap_id is not None:
        recap_obj = CustomRecap.objects.filter(
            id=custom_recap_id, tenant=access.tenant
        ).first()
        action.outcome_recap = recap_obj

    if action.provider_assignment is not None:
        action.provider_assignment.status = ProviderAssignment.STATUS_COMPLETED
        action.provider_assignment.save(update_fields=["status", "updated_at"])

    action.status = StoreRecommendationAction.STATUS_COMPLETED
    action.outcome_summary = {
        "completed_at": now.isoformat(),
        "visit_uuid": str(visit.uuid),
        "recap_id": recap_obj.id if recap_obj else None,
        "samples_distributed": visit.samples_distributed,
        "consumers_sampled": visit.consumers_sampled,
        "rep_observed_units": visit.rep_observed_units,
        "notes": (notes or "").strip(),
    }
    hist = list(action.history_log or [])
    hist.append(
        {
            "at": now.isoformat(),
            "actor": access.user.email,
            "event": "return_visit_completed",
            "visit_uuid": str(visit.uuid),
            "rep_observed_units": visit.rep_observed_units,
        }
    )
    action.history_log = hist
    action.save()

    log_workspace_audit(
        tenant=access.tenant,
        actor=access.user,
        category="insight_action",
        action="rebook_visit_completed",
        target_type="StoreRecommendationAction",
        target_id=str(action.uuid),
        summary=f"Completed rebooked return visit at {action.store_label}.",
    )
    return action


def build_weekly_buyer_store_report(
    access: ResolvedWorkspaceAccess,
    *,
    period_start: date | None = None,
    period_end: date | None = None,
    retailer_filter: str | None = None,
    market_filter: str | None = None,
    provider_uuid: str | None = None,
) -> dict[str, Any]:
    """Build the weekly buyer & budget-meeting store table combining:
    visits, supported brand cost, observed units, matched retailer sales &
    coverage, published recommendation, and current action owner/status."""
    if not access.can_view_brand_consolidated:
        raise PermissionError(
            "Outside providers cannot view the consolidated weekly buyer report."
        )

    eff_end = period_end or timezone.now().date()
    eff_start = period_start or (eff_end - timedelta(days=27))

    visits_qs = brand_included_visits_queryset(access.tenant).select_related(
        "primary_provider"
    ).prefetch_related("cost_lines").filter(
        visit_date__gte=eff_start,
        visit_date__lte=eff_end,
    )
    if provider_uuid:
        visits_qs = visits_qs.filter(primary_provider__uuid=provider_uuid)
    if market_filter:
        mf = market_filter.strip().lower()
        visits_qs = [
            v
            for v in visits_qs
            if mf in (v.market or "").lower()
            or mf in (v.city or "").lower()
            or mf in (v.state_code or "").lower()
        ]
    else:
        visits_qs = list(visits_qs)

    if retailer_filter:
        rf = retailer_filter.strip().lower()
        visits_qs = [
            v
            for v in visits_qs
            if rf in (v.chain_label or "").lower()
            or rf in (v.store_label or "").lower()
            or rf == (v.chain_key or "").lower()
        ]

    pos_data = build_retailer_pos_measurement(
        access, period_start=eff_start, period_end=eff_end
    )
    pos_by_store: dict[str, dict[str, Any]] = {}
    if pos_data["available"]:
        for sw in pos_data["store_weeks"]:
            sk = sw["store_key"]
            b = pos_by_store.setdefault(
                sk,
                {
                    "matched_cells": 0,
                    "eligible_cells": 0,
                    "register_units": 0,
                    "adjusted_units": 0,
                    "has_any_match": False,
                },
            )
            for cell in sw["sku_cells"]:
                b["eligible_cells"] += 1
                if cell["register_units"] is not None:
                    b["matched_cells"] += 1
                    b["has_any_match"] = True
                    b["register_units"] += int(cell["register_units"])
                    b["adjusted_units"] += int(cell["adjusted_register_units"])

    published_insights = list(
        BrandInsight.objects.filter(
            tenant=access.tenant,
            status=BrandInsight.STATUS_PUBLISHED,
        ).order_by("-period_start", "rank")
    )
    insight_by_store: dict[str, BrandInsight] = {}
    chain_insights: dict[str, BrandInsight] = {}
    for ins in published_insights:
        if ins.scope_key.startswith("store:"):
            sk = ins.scope_key[len("store:") :]
            insight_by_store.setdefault(sk, ins)
        elif ins.scope_key.startswith("chain:"):
            ck = ins.scope_key[len("chain:") :]
            chain_insights.setdefault(ck, ins)

    latest_actions: dict[str, StoreRecommendationAction] = {}
    for act in StoreRecommendationAction.objects.select_related(
        "owner", "provider"
    ).filter(tenant=access.tenant).order_by("-created_at", "-id"):
        latest_actions.setdefault(act.store_key, act)

    store_buckets: dict[str, dict[str, Any]] = {}
    for v in visits_qs:
        sk = v.store_key or f"visit:{v.id}"
        sb = store_buckets.setdefault(
            sk,
            {
                "store_key": sk,
                "store_label": v.store_label or v.address or "Store",
                "retailer_banner": v.chain_label or v.store_label or "",
                "chain_key": v.chain_key or "",
                "market": v.market or v.city or v.state_code or "",
                "visit_count": 0,
                "supported_brand_cost": Decimal("0.00"),
                "total_brand_cost": Decimal("0.00"),
                "has_incomplete_cost": False,
                "samples_distributed": 0,
                "rep_observed_units": 0,
                "providers": set(),
            },
        )
        sb["visit_count"] += 1
        csum = compute_visit_cost_summary(v, list(v.cost_lines.all()))
        sb["supported_brand_cost"] += csum.supported_cost
        if csum.is_incomplete or csum.total_cost is None:
            sb["has_incomplete_cost"] = True
        else:
            sb["total_brand_cost"] += csum.total_cost
        sb["samples_distributed"] += v.samples_distributed or 0
        sb["rep_observed_units"] += v.rep_observed_units or 0
        if v.primary_provider:
            sb["providers"].add(v.primary_provider.name)

    # Also include stores that have an active StoreRecommendationAction in the
    # workspace (e.g. Hold with 0 visits) when not filtered out!
    for sk, act in latest_actions.items():
        if sk in store_buckets:
            continue
        if provider_uuid and (
            not act.provider or str(act.provider.uuid) != provider_uuid
        ):
            continue
        if retailer_filter and retailer_filter.strip().lower() not in act.store_label.lower():
            continue
        if market_filter:
            continue
        store_buckets[sk] = {
            "store_key": sk,
            "store_label": act.store_label,
            "retailer_banner": act.store_label,
            "chain_key": make_chain_key(act.store_label),
            "market": "",
            "visit_count": 0,
            "supported_brand_cost": Decimal("0.00"),
            "total_brand_cost": Decimal("0.00"),
            "has_incomplete_cost": False,
            "samples_distributed": 0,
            "rep_observed_units": 0,
            "providers": {act.provider.name} if act.provider else set(),
        }

    rows_out: list[dict[str, Any]] = []
    for sk, sb in store_buckets.items():
        pos_b = pos_by_store.get(sk)
        if not pos_data["available"]:
            matched_reg = None
            adj_reg = None
            cov_label = RETAILER_UNAVAILABLE_MESSAGE
        elif pos_b is None or pos_b["eligible_cells"] == 0:
            matched_reg = None
            adj_reg = None
            cov_label = "0/0 (no eligible POS rows)"
        else:
            matched_reg = (
                pos_b["register_units"] if pos_b["has_any_match"] else None
            )
            adj_reg = (
                pos_b["adjusted_units"] if pos_b["has_any_match"] else None
            )
            pct = int(
                round((pos_b["matched_cells"] / pos_b["eligible_cells"]) * 100)
            )
            cov_label = (
                f"{pos_b['matched_cells']}/{pos_b['eligible_cells']} ({pct}%)"
            )

        ins = insight_by_store.get(sk) or chain_insights.get(sb["chain_key"])
        act = latest_actions.get(sk)

        rows_out.append(
            {
                "store_key": sk,
                "store_label": sb["store_label"],
                "retailer_banner": sb["retailer_banner"],
                "market": sb["market"],
                "providers": sorted(sb["providers"]),
                "visit_count": sb["visit_count"],
                "supported_brand_cost": str(
                    quantize_money(sb["supported_brand_cost"])
                ),
                "total_brand_cost": (
                    None
                    if sb["has_incomplete_cost"]
                    else str(quantize_money(sb["total_brand_cost"]))
                ),
                "has_incomplete_cost": sb["has_incomplete_cost"],
                "samples_distributed": sb["samples_distributed"],
                "rep_observed_units": sb["rep_observed_units"],
                "matched_register_units": matched_reg,
                "adjusted_register_units": adj_reg,
                "pos_coverage_label": cov_label,
                "recommendation_id": (
                    f"BrandInsight:{ins.id}" if ins else None
                ),
                "recommendation_code": ins.code if ins else None,
                "recommendation_summary": ins.body if ins else None,
                "current_action_uuid": str(act.uuid) if act else None,
                "current_action_type": act.action_type if act else None,
                "current_action_status": act.status if act else None,
                "current_action_owner": (
                    act.owner.email
                    if act and act.owner
                    else (act.provider.name if act and act.provider else None)
                ),
                "current_action_provider": (
                    act.provider.name if act and act.provider else None
                ),
            }
        )

    rows_out.sort(key=lambda r: (-r["visit_count"], r["store_label"]))
    return {
        "period_start": eff_start.isoformat(),
        "period_end": eff_end.isoformat(),
        "pos_available": pos_data["available"],
        "total_visits": sum(r["visit_count"] for r in rows_out),
        "total_supported_cost": str(
            quantize_money(
                sum(
                    (Decimal(r["supported_brand_cost"]) for r in rows_out),
                    Decimal("0.00"),
                )
            )
        ),
        "rows": rows_out,
    }


_REPORT_COLUMNS = [
    ("Store", "store_label"),
    ("Retailer", "retailer_banner"),
    ("Market", "market"),
    ("Providers", "providers_str"),
    ("Visits", "visit_count"),
    ("Supported Brand Cost ($)", "supported_brand_cost"),
    ("Total Brand Cost ($)", "total_brand_cost_display"),
    ("Samples Distributed", "samples_distributed"),
    ("Rep-Observed Units", "rep_observed_units"),
    ("Matched Register Units", "matched_register_display"),
    ("Adjusted Register Units", "adjusted_register_display"),
    ("POS Coverage", "pos_coverage_label"),
    ("Recommendation", "recommendation_summary"),
    ("Action", "current_action_type"),
    ("Action Status", "current_action_status"),
    ("Action Owner", "current_action_owner"),
]


def _format_export_rows(report: dict[str, Any]) -> list[dict[str, Any]]:
    formatted: list[dict[str, Any]] = []
    for r in report["rows"]:
        item = dict(r)
        item["providers_str"] = ", ".join(r.get("providers") or [])
        item["total_brand_cost_display"] = (
            "Incomplete"
            if r["has_incomplete_cost"]
            else (r["total_brand_cost"] or "0.00")
        )
        item["matched_register_display"] = (
            str(r["matched_register_units"])
            if r["matched_register_units"] is not None
            else RETAILER_UNAVAILABLE_MESSAGE
            if not report["pos_available"]
            else "Missing"
        )
        item["adjusted_register_display"] = (
            str(r["adjusted_register_units"])
            if r["adjusted_register_units"] is not None
            else RETAILER_UNAVAILABLE_MESSAGE
            if not report["pos_available"]
            else "Missing"
        )
        formatted.append(item)
    return formatted


def export_weekly_buyer_report_csv(report: dict[str, Any]) -> str:
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow([col[0] for col in _REPORT_COLUMNS])
    for row in _format_export_rows(report):
        writer.writerow([row.get(col[1]) or "" for col in _REPORT_COLUMNS])
    return buf.getvalue()


def export_weekly_buyer_report_xlsx_base64(report: dict[str, Any]) -> str:
    wb = openpyxl.Workbook()
    ws = wb.active
    assert ws is not None
    ws.title = "Weekly Buyer Store Report"
    ws.append([col[0] for col in _REPORT_COLUMNS])
    for row in _format_export_rows(report):
        ws.append([row.get(col[1]) if row.get(col[1]) is not None else "" for col in _REPORT_COLUMNS])
    bio = io.BytesIO()
    wb.save(bio)
    return base64.b64encode(bio.getvalue()).decode("ascii")
