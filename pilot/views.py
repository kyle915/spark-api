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
    BrandOnboardingProgress,
    CanonicalVisit,
    CostEvidenceLink,
    ProviderAssignment,
    ProviderSourceInclusionRule,
    VisitSubmissionSource,
    WorkspaceMembership,
    WorkspaceSharedLink,
)
from pilot.subscriptions import process_stripe_webhook
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
        BrandOnboardingProgress.objects.filter(
            tenant=tenant, step_mobile_link_opened=False
        ).update(step_mobile_link_opened=True, updated_at=timezone.now())
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

    progress = BrandOnboardingProgress.objects.filter(tenant=tenant).first()
    if progress is not None:
        update_fields = ["updated_at"]
        if not progress.step_mobile_link_opened:
            progress.step_mobile_link_opened = True
            update_fields.append("step_mobile_link_opened")
        if progress.first_visit_id is None:
            progress.first_visit = visit
            update_fields.append("first_visit")
        if len(update_fields) > 1:
            progress.save(update_fields=update_fields)

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


@csrf_exempt
@require_http_methods(["POST"])
def stripe_billing_webhook_view(request: HttpRequest) -> JsonResponse:
    """Signed Stripe Billing webhook endpoint (`/api/public/stripe-billing/webhook`).
    Verifies HMAC signature + timestamp tolerance, enforces test-vs-live safety,
    deduplicates replayed event IDs, and ignores stale out-of-order deliveries."""
    sig_header = request.META.get("HTTP_STRIPE_SIGNATURE", "")
    try:
        outcome = process_stripe_webhook(request.body, sig_header)
        return JsonResponse(outcome, status=200)
    except PermissionError as exc:
        return JsonResponse(
            {"ok": False, "error": "billing_mode_violation", "message": str(exc)},
            status=403,
        )
    except ValueError as exc:
        return JsonResponse(
            {"ok": False, "error": "invalid_webhook", "message": str(exc)},
            status=400,
        )


@csrf_exempt
@require_http_methods(["GET", "POST"])
def public_pos_feed_drop_view(request: HttpRequest, token: str) -> JsonResponse:
    """Inbound automated POS CSV drop for a ``PosFeedConfig.ingest_token``.

    POST JSON ``{csvText|csv_text, filename?, fromEmail?, messageId?}`` or
    multipart form with a ``file`` / ``csv`` upload. Optionally processes
    immediately (``process=true``) so brands don't wait for the hourly cron.
    """
    from pilot.ingest import enqueue_pos_feed_drop, process_pos_feed_drop
    from pilot.models import PosFeedConfig

    cfg = PosFeedConfig.objects.filter(ingest_token=token).first()
    if cfg is None or not cfg.enabled:
        return JsonResponse(
            {
                "error": "invalid_token",
                "message": "This POS feed token is invalid or disabled.",
            },
            status=403,
        )

    if request.method == "GET":
        return JsonResponse(
            {
                "ok": True,
                "brandName": cfg.tenant.name,
                "retailerBanner": cfg.retailer_banner,
                "presetKey": cfg.preset_key,
                "sourceKind": cfg.source_kind,
                "enabled": cfg.enabled,
            }
        )

    csv_text = ""
    filename = "pos.csv"
    from_email = ""
    message_id = ""
    process_now = False

    content_type = (request.content_type or "").lower()
    if "application/json" in content_type:
        try:
            payload = json.loads(request.body.decode("utf-8") or "{}")
        except (UnicodeDecodeError, json.JSONDecodeError):
            return JsonResponse(
                {"error": "invalid_json", "message": "Body must be JSON."},
                status=400,
            )
        if not isinstance(payload, dict):
            return JsonResponse(
                {"error": "invalid_json", "message": "Body must be a JSON object."},
                status=400,
            )
        csv_text = str(
            payload.get("csvText")
            or payload.get("csv_text")
            or payload.get("csv")
            or ""
        )
        filename = str(payload.get("filename") or payload.get("fileName") or "pos.csv")
        from_email = str(
            payload.get("fromEmail") or payload.get("from_email") or ""
        )
        message_id = str(
            payload.get("messageId") or payload.get("message_id") or ""
        )
        process_now = str(payload.get("process") or "").lower() in {
            "1",
            "true",
            "yes",
            "on",
        }
    else:
        uploaded = request.FILES.get("file") or request.FILES.get("csv")
        if uploaded is not None:
            filename = getattr(uploaded, "name", None) or "pos.csv"
            csv_text = uploaded.read().decode("utf-8", errors="replace")
        else:
            csv_text = (
                request.POST.get("csvText")
                or request.POST.get("csv_text")
                or request.POST.get("csv")
                or ""
            )
            filename = request.POST.get("filename") or filename
        from_email = request.POST.get("fromEmail") or request.POST.get("from_email") or ""
        message_id = (
            request.POST.get("messageId") or request.POST.get("message_id") or ""
        )
        process_now = str(
            request.POST.get("process") or request.GET.get("process") or ""
        ).lower() in {"1", "true", "yes", "on"}

    try:
        drop = enqueue_pos_feed_drop(
            ingest_token=token,
            csv_text=csv_text,
            filename=filename,
            from_email=from_email,
            message_id=message_id,
        )
    except PermissionError as exc:
        return JsonResponse({"error": "forbidden", "message": str(exc)}, status=403)
    except ValueError as exc:
        return JsonResponse({"error": "invalid_payload", "message": str(exc)}, status=400)

    if process_now and drop.status == drop.STATUS_PENDING:
        drop = process_pos_feed_drop(drop)

    return JsonResponse(
        {
            "ok": True,
            "dropUuid": str(drop.uuid),
            "status": drop.status,
            "filename": drop.filename,
            "salesImportUuid": (
                str(drop.sales_import.uuid) if drop.sales_import_id else None
            ),
            "errorMessage": drop.error_message or None,
            "matchedRows": (
                drop.sales_import.matched_rows if drop.sales_import_id else None
            ),
            "unmatchedRows": (
                drop.sales_import.unmatched_rows if drop.sales_import_id else None
            ),
        }
    )


def _resolve_authenticated_request_user(request: HttpRequest):
    """Return the authenticated user from gqlauth JWT middleware or Django auth."""
    try:
        from gqlauth.core.middlewares import USER_OR_ERROR_KEY, get_user_or_error

        uoe = getattr(request, USER_OR_ERROR_KEY, None)
        if uoe is None:
            uoe = get_user_or_error(request)
        jwt_user = getattr(uoe, "user", None)
        if jwt_user is not None and getattr(jwt_user, "is_authenticated", False):
            return jwt_user
    except Exception:  # noqa: BLE001
        pass

    req_user = getattr(request, "user", None)
    if req_user is not None and getattr(req_user, "is_authenticated", False):
        return req_user
    return None


def _is_sandbox_admin_user(user) -> bool:
    """Authoritative Ignite-admin check matching Spark GraphQL permissions."""
    from django.contrib.auth import get_user_model
    from utils.graphql.permissions import (
        UntrustedIgniteEmail,
        _demote_if_excluded,
        _is_admin_access,
        ignite_domain_account_trusted,
        is_ignite_domain_email,
    )

    if not user or not getattr(user, "is_authenticated", False):
        return False

    db_user = None
    pk = getattr(user, "pk", None)
    req_email = (getattr(user, "email", "") or "").strip()
    try:
        User = get_user_model()
        if pk is not None:
            db_user = User.objects.select_related("role").filter(pk=pk).first()
        if db_user is None and req_email:
            db_user = (
                User.objects.select_related("role")
                .filter(email__iexact=req_email)
                .first()
            )
    except Exception:  # noqa: BLE001
        db_user = None

    if db_user is not None:
        if not db_user.is_active:
            return False
        role_slug = (
            (getattr(db_user.role, "slug", "") or "").lower()
            if db_user.role
            else None
        )
        email = (db_user.email or "").lower()
        if is_ignite_domain_email(email):
            from gqlauth.models import UserStatus

            verified = UserStatus.objects.filter(
                user_id=db_user.pk, verified=True
            ).exists()
            if not ignite_domain_account_trusted(
                role_slug=role_slug,
                is_active=db_user.is_active,
                verified=verified,
            ):
                email = UntrustedIgniteEmail(email)
        role_slug, is_staff, is_super, email = _demote_if_excluded(
            role_slug,
            bool(db_user.is_staff),
            bool(db_user.is_superuser),
            email,
        )
        return _is_admin_access(role_slug, is_staff, is_super, email)

    # Fallback when no DB row exists (e.g. unit-test SimpleNamespace user):
    # never grant domain-only access without DB verification.
    if getattr(user, "is_active", True) is False:
        return False
    raw_role = getattr(user, "role", None)
    role_slug = (getattr(raw_role, "slug", None) or "").lower() or None
    email = req_email.lower()
    if is_ignite_domain_email(email):
        email = UntrustedIgniteEmail(email)
    role_slug, is_staff, is_super, email = _demote_if_excluded(
        role_slug,
        bool(getattr(user, "is_staff", False)),
        bool(getattr(user, "is_superuser", False)),
        email,
    )
    return _is_admin_access(role_slug, is_staff, is_super, email)


def public_sandbox_brand_view(request: HttpRequest) -> JsonResponse:
    """GET `/api/public/sandbox-brand` — admin-only fictional Sample Brand Sandbox."""
    if request.method != "GET":
        return JsonResponse({"error": "method_not_allowed"}, status=405)

    user = _resolve_authenticated_request_user(request)
    if user is None:
        return JsonResponse(
            {
                "error": "authentication_required",
                "message": "Ignite admin authentication is required to preview the sandbox.",
            },
            status=401,
        )
    if not _is_sandbox_admin_user(user):
        return JsonResponse(
            {
                "error": "forbidden",
                "message": "Sample Brand Sandbox is currently restricted to Ignite admins.",
            },
            status=403,
        )

    from pilot.sandbox import build_sandbox_sample_brand

    resp = JsonResponse(build_sandbox_sample_brand())
    resp["Cache-Control"] = "private, no-store"
    return resp
