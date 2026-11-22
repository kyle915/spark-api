"""Public HTTP endpoints for Standalone Spark mobile provider submission
(`/api/public/provider-submit/<code>`) and revocable workspace shared links
(`/api/public/workspace-share/<token>`)."""

from __future__ import annotations

import json
from datetime import date
from decimal import Decimal

from django.http import HttpRequest, JsonResponse
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods

from brand_insights.facts import store_identity
from events.models import Product
from pilot.costs import quantize_money, recompute_visit_cost_summary
from pilot.models import (
    BrandCostLine,
    CanonicalVisit,
    CostEvidenceLink,
    ProviderAssignment,
    ProviderSourceInclusionRule,
    VisitSubmissionSource,
    WorkspaceMembership,
    WorkspaceSharedLink,
)
from pilot.workspaces import resolve_assignment_by_submission_code


@csrf_exempt
@require_http_methods(["GET", "POST"])
def public_provider_submission_view(
    request: HttpRequest, code: str
) -> JsonResponse:
    assignment = resolve_assignment_by_submission_code(code)
    if assignment is None:
        return JsonResponse(
            {
                "error": "assignment_unavailable",
                "message": "This submission link is invalid or has been revoked.",
            },
            status=403,
        )

    tenant = assignment.tenant
    provider = assignment.provider

    if request.method == "GET":
        products = [
            {"id": p.id, "name": p.name}
            for p in Product.objects.filter(tenant=tenant).order_by("name")
        ]
        visit_obj = (
            {
                "uuid": str(assignment.canonical_visit.uuid),
                "store_label": assignment.canonical_visit.store_label,
                "address": assignment.canonical_visit.address,
                "city": assignment.canonical_visit.city,
                "state_code": assignment.canonical_visit.state_code,
                "visit_date": assignment.canonical_visit.visit_date.isoformat(),
            }
            if assignment.canonical_visit
            else None
        )
        return JsonResponse(
            {
                "ok": True,
                "submissionCode": assignment.submission_code,
                "submission_code": assignment.submission_code,
                "brandName": tenant.name,
                "brand_name": tenant.name,
                "providerName": provider.name,
                "provider_name": provider.name,
                "scopeType": assignment.scope_type,
                "scope_type": assignment.scope_type,
                "eventTypeId": assignment.event_type_id,
                "programName": (
                    assignment.event_type.name if assignment.event_type else None
                ),
                "program_name": (
                    assignment.event_type.name if assignment.event_type else None
                ),
                "due_date": (
                    assignment.due_date.isoformat() if assignment.due_date else None
                ),
                "visitUuid": (
                    str(assignment.canonical_visit.uuid)
                    if assignment.canonical_visit
                    else None
                ),
                "storeLabel": (
                    assignment.canonical_visit.store_label
                    if assignment.canonical_visit
                    else None
                ),
                "visitDate": (
                    assignment.canonical_visit.visit_date.isoformat()
                    if assignment.canonical_visit
                    else None
                ),
                "visit": visit_obj,
                "allowMultiplePhotos": True,
                "allowMultipleReceipts": True,
                "products": products,
            }
        )

    content_type = (request.content_type or "").lower()
    if content_type.startswith("multipart/form-data"):
        raw_payload = request.POST.get("payload") or "{}"
        try:
            payload = json.loads(raw_payload)
        except ValueError:
            return JsonResponse(
                {"error": "invalid_json", "message": "Invalid multipart payload JSON."},
                status=400,
            )
        uploaded_photos = [f.name for f in request.FILES.getlist("photos")]
        uploaded_receipts = [f.name for f in request.FILES.getlist("receipts")]
    else:
        try:
            payload = json.loads(request.body.decode("utf-8") or "{}")
        except (ValueError, UnicodeDecodeError):
            return JsonResponse(
                {"error": "invalid_json", "message": "Invalid JSON body."},
                status=400,
            )
        uploaded_photos = []
        uploaded_receipts = []

    # Strictly enforce assignment scope — never allow submitting to another
    # program or visit!
    req_visit_uuid = (payload.get("visitUuid") or "").strip()
    req_event_type_id = payload.get("eventTypeId")

    if assignment.scope_type == ProviderAssignment.SCOPE_VISIT:
        if assignment.canonical_visit is None:
            return JsonResponse(
                {
                    "error": "invalid_assignment",
                    "message": "Visit assignment has no target visit.",
                },
                status=400,
            )
        if req_visit_uuid and req_visit_uuid != str(
            assignment.canonical_visit.uuid
        ):
            return JsonResponse(
                {
                    "error": "scope_violation",
                    "message": "This link can only submit for its assigned visit.",
                },
                status=403,
            )
        if (
            req_event_type_id is not None
            and assignment.event_type_id is not None
            and int(req_event_type_id) != assignment.event_type_id
        ):
            return JsonResponse(
                {
                    "error": "scope_violation",
                    "message": "This link cannot submit to a different program.",
                },
                status=403,
            )
        visit = assignment.canonical_visit
    else:
        if req_visit_uuid:
            return JsonResponse(
                {
                    "error": "scope_violation",
                    "message": "Program submission links cannot overwrite arbitrary visit UUIDs.",
                },
                status=403,
            )
        if (
            req_event_type_id is not None
            and assignment.event_type_id is not None
            and int(req_event_type_id) != assignment.event_type_id
        ):
            return JsonResponse(
                {
                    "error": "scope_violation",
                    "message": "This link is restricted to its assigned program.",
                },
                status=403,
            )
        raw_date = (
            payload.get("visitDate") or payload.get("visit_date") or ""
        ).strip()
        visit_date = (
            date.fromisoformat(raw_date)
            if raw_date
            else timezone.now().date()
        )
        store_name = (
            payload.get("storeName")
            or payload.get("store_banner")
            or ""
        ).strip()
        store_num = (payload.get("store_number") or "").strip()
        if store_name and store_num and f"#{store_num}" not in store_name:
            store_name = f"{store_name} #{store_num}"
        address = (payload.get("address") or "").strip()
        city = (payload.get("city") or "").strip()
        state_code = (
            payload.get("stateCode") or payload.get("state") or ""
        ).strip().upper()
        if not store_name and not address:
            return JsonResponse(
                {
                    "error": "missing_store",
                    "message": "Store name or address is required.",
                },
                status=400,
            )
        s_key, s_label, c_key, c_label = store_identity(
            store_name, address, city, state_code
        )
        shift_label = (payload.get("shift_key") or "default").strip() or "default"
        visit = CanonicalVisit.objects.create(
            tenant=tenant,
            primary_provider=provider,
            event_type=assignment.event_type,
            store_key=s_key,
            store_label=s_label,
            chain_key=c_key,
            chain_label=c_label,
            store_number=store_num,
            address=address,
            city=city,
            state_code=state_code,
            market=city or state_code,
            visit_date=visit_date,
            shift_key=f"mobile:{assignment.submission_code}:{shift_label}:{timezone.now().strftime('%H%M%S')}",
            status=CanonicalVisit.STATUS_NEEDS_REVIEW,
        )

    photos = (
        payload.get("photos")
        if isinstance(payload.get("photos"), list)
        else uploaded_photos
    )
    receipts = (
        payload.get("receipts")
        if isinstance(payload.get("receipts"), list)
        else uploaded_receipts
    )
    samples = (
        payload.get("samplesDistributed")
        if payload.get("samplesDistributed") is not None
        else payload.get("samples_distributed")
    )
    consumers = (
        payload.get("consumersSampled")
        if payload.get("consumersSampled") is not None
        else payload.get("consumers_sampled")
    )
    observed_units = (
        payload.get("repObservedUnits")
        if payload.get("repObservedUnits") is not None
        else payload.get("rep_observed_units")
    )
    notes = (payload.get("notes") or "").strip()

    rule = ProviderSourceInclusionRule.objects.filter(
        tenant=tenant, provider=provider
    ).first()
    excluded = bool(rule and not rule.include_in_combined_reporting)

    sub = VisitSubmissionSource.objects.create(
        tenant=tenant,
        canonical_visit=visit,
        provider=provider,
        source_kind=VisitSubmissionSource.KIND_WALKUP_CHECKIN,
        recap_status=VisitSubmissionSource.RECAP_SUBMITTED,
        exclude_from_aggregates=excluded,
        samples_distributed=int(samples) if samples is not None else None,
        consumers_sampled=int(consumers) if consumers is not None else None,
        rep_observed_units=(
            int(observed_units) if observed_units is not None else None
        ),
        sku_breakdown=payload.get("skuBreakdown") or [],
        notes=notes,
        raw_row={"photos": photos, "receipts": receipts, **payload},
    )

    # All field submissions leave approved=False / needs_review until brand/admin review!
    visit.status = CanonicalVisit.STATUS_NEEDS_REVIEW
    visit.samples_distributed = sub.samples_distributed
    visit.consumers_sampled = sub.consumers_sampled
    visit.rep_observed_units = sub.rep_observed_units
    visit.total_engagements = sub.consumers_sampled
    visit.save(
        update_fields=[
            "status",
            "samples_distributed",
            "consumers_sampled",
            "rep_observed_units",
            "total_engagements",
            "updated_at",
        ]
    )

    expense_lines = (
        payload.get("expense_lines")
        if isinstance(payload.get("expense_lines"), list)
        else []
    )
    if payload.get("productSpend") is not None:
        expense_lines = [
            {
                "category": "product",
                "amount": payload["productSpend"],
                "description": f"{provider.name} mobile field product spend",
                "is_itemized": bool(payload.get("receiptsItemized", True)),
            },
            *expense_lines,
        ]

    for exp in expense_lines:
        if not isinstance(exp, dict) or exp.get("amount") is None:
            continue
        spend_amt = quantize_money(Decimal(str(exp["amount"])))
        cat_raw = (exp.get("category") or "product").strip().lower()
        cat = (
            BrandCostLine.CAT_SUPPLIES
            if cat_raw in {"supplies", "supply_kit"}
            else BrandCostLine.CAT_PRODUCT
        )
        line = BrandCostLine.objects.create(
            tenant=tenant,
            canonical_visit=visit,
            provider=provider,
            category=cat,
            source_type=BrandCostLine.SOURCE_EXPENSE_RECEIPT,
            description=(
                exp.get("description")
                or f"{provider.name} mobile field {cat_raw} spend"
            )[:255],
            amount=spend_amt,
            is_confirmed_zero=spend_amt == Decimal("0.00"),
            settlement_status=BrandCostLine.SETTLEMENT_ESTIMATED,
            evidence_status=(
                BrandCostLine.EVIDENCE_SUPPORTED
                if receipts or spend_amt == Decimal("0.00")
                else BrandCostLine.EVIDENCE_MISSING
            ),
        )
        for idx, rec_url in enumerate(receipts, start=1):
            CostEvidenceLink.objects.create(
                cost_line=line,
                proof_url=str(rec_url)[:500],
                label=f"Mobile receipt #{idx}",
                verified_amount=spend_amt if len(receipts) == 1 else None,
                is_itemized=bool(exp.get("is_itemized", True)),
            )
    if expense_lines:
        recompute_visit_cost_summary(visit)

    return JsonResponse(
        {
            "ok": True,
            "visitUuid": str(visit.uuid),
            "visit_uuid": str(visit.uuid),
            "submissionUuid": str(sub.uuid),
            "submission_uuid": str(sub.uuid),
            "status": visit.status,
            "photoCount": len(photos),
            "receiptCount": len(receipts),
        }
    )


@csrf_exempt
@require_http_methods(["GET"])
def public_workspace_shared_link_view(
    request: HttpRequest, token: str
) -> JsonResponse:
    link = (
        WorkspaceSharedLink.objects.select_related(
            "tenant", "canonical_visit", "created_by_membership"
        )
        .filter(token=(token or "").strip())
        .first()
    )
    if link is None:
        return JsonResponse(
            {"error": "not_found", "message": "Shared link not found."},
            status=404,
        )
    if link.revoked_at is not None:
        return JsonResponse(
            {
                "error": "revoked",
                "message": "This shared link has been revoked.",
            },
            status=403,
        )
    if (
        link.created_by_membership is not None
        and link.created_by_membership.status
        != WorkspaceMembership.STATUS_ACTIVE
    ):
        return JsonResponse(
            {
                "error": "revoked",
                "message": "Access for the workspace member who issued this link was revoked.",
            },
            status=403,
        )
    if link.expires_at < timezone.now():
        return JsonResponse(
            {"error": "expired", "message": "This shared link has expired."},
            status=410,
        )

    if link.canonical_visit is not None:
        v = link.canonical_visit
        return JsonResponse(
            {
                "ok": True,
                "resourceType": link.resource_type,
                "brandName": link.tenant.name,
                "visit": {
                    "uuid": str(v.uuid),
                    "storeLabel": v.store_label,
                    "visitDate": v.visit_date.isoformat(),
                    "samplesDistributed": v.samples_distributed,
                    "consumersSampled": v.consumers_sampled,
                    "repObservedUnits": v.rep_observed_units,
                },
            }
        )

    return JsonResponse(
        {
            "ok": True,
            "resourceType": link.resource_type,
            "brandName": link.tenant.name,
            "reportParams": link.report_params,
        }
    )
