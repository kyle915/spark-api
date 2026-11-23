"""Feature 3 service layer: Brand-visible actual cost per demo, contracted
agreement fee rules, receipt-to-invoice evidence linking without double-counting,
penny-exact shared allocations across visits/brands, and guarded rollups."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal, ROUND_HALF_UP
from typing import Any

from django.db import transaction
from django.db.models import Q

from billing.models import InvoiceLineItem
from pilot.entitlements import require_plan_feature
from pilot.imports import brand_included_visits_queryset
from pilot.models import (
    BrandCostLine,
    CanonicalVisit,
    CostEvidenceLink,
    ProviderAgreement,
    ProviderOrganization,
    SharedCostSource,
)
from pilot.permissions import ResolvedWorkspaceAccess, log_workspace_audit
from recaps.models import CustomRecapFile, ReceiptReading

TWOPLACES = Decimal("0.01")

REIMBURSABLE_CATEGORIES = frozenset(
    {
        BrandCostLine.CAT_PRODUCT,
        BrandCostLine.CAT_PARKING,
        BrandCostLine.CAT_SHIPPING,
        BrandCostLine.CAT_RETAILER_FEE,
    }
)


def quantize_money(val: Decimal | int | float | str) -> Decimal:
    return Decimal(str(val)).quantize(TWOPLACES, rounding=ROUND_HALF_UP)


def signed_line_amount(line: BrandCostLine) -> Decimal | None:
    """Signed brand cost contribution: credits subtract, unknown stays None."""
    if line.amount is None:
        return None
    amt = quantize_money(abs(line.amount))
    if line.category == BrandCostLine.CAT_CREDIT:
        return -amt
    return amt


@dataclass(frozen=True)
class VisitCostSummary:
    visit_uuid: str
    total_cost: Decimal | None
    supported_cost: Decimal
    invoiced_cost: Decimal
    paid_cost: Decimal
    estimated_cost: Decimal
    is_incomplete: bool
    is_confirmed_zero: bool
    settlement_status: str
    evidence_status: str
    cost_per_sample: Decimal | None
    cost_per_observed_unit: Decimal | None
    efficiency_withheld_reason: str | None
    category_totals: dict[str, str]
    line_count: int


def compute_visit_cost_summary(
    visit: CanonicalVisit,
    lines: list[BrandCostLine] | None = None,
) -> VisitCostSummary:
    cost_lines = (
        lines
        if lines is not None
        else list(
            BrandCostLine.objects.filter(
                tenant=visit.tenant, canonical_visit=visit
            ).order_by("id")
        )
    )

    if not cost_lines:
        return VisitCostSummary(
            visit_uuid=str(visit.uuid),
            total_cost=None,
            supported_cost=Decimal("0.00"),
            invoiced_cost=Decimal("0.00"),
            paid_cost=Decimal("0.00"),
            estimated_cost=Decimal("0.00"),
            is_incomplete=True,
            is_confirmed_zero=False,
            settlement_status=BrandCostLine.SETTLEMENT_ESTIMATED,
            evidence_status=BrandCostLine.EVIDENCE_MISSING,
            cost_per_sample=None,
            cost_per_observed_unit=None,
            efficiency_withheld_reason="Visit cost is missing or incomplete.",
            category_totals={},
            line_count=0,
        )

    has_unknown = any(ln.amount is None for ln in cost_lines)
    running_total = Decimal("0.00")
    supported_total = Decimal("0.00")
    invoiced_total = Decimal("0.00")
    paid_total = Decimal("0.00")
    estimated_total = Decimal("0.00")
    cat_totals: dict[str, Decimal] = {}

    for line in cost_lines:
        signed = signed_line_amount(line)
        if signed is None:
            continue
        running_total += signed
        cat_totals[line.category] = (
            cat_totals.get(line.category, Decimal("0.00")) + signed
        )
        if line.evidence_status == BrandCostLine.EVIDENCE_SUPPORTED or (
            line.category
            in {
                BrandCostLine.CAT_DEMO_FEE,
                BrandCostLine.CAT_EXTRA_HOURS,
                BrandCostLine.CAT_MILEAGE,
                BrandCostLine.CAT_SERVICE_ADMIN_FEE,
                BrandCostLine.CAT_TAX,
                BrandCostLine.CAT_CREDIT,
            }
            and line.evidence_status != BrandCostLine.EVIDENCE_MISSING
        ):
            supported_total += signed

        if line.settlement_status == BrandCostLine.SETTLEMENT_PAID:
            paid_total += signed
        elif line.settlement_status == BrandCostLine.SETTLEMENT_INVOICED:
            invoiced_total += signed
        else:
            estimated_total += signed

    running_total = quantize_money(running_total)
    supported_total = quantize_money(supported_total)
    invoiced_total = quantize_money(invoiced_total)
    paid_total = quantize_money(paid_total)
    estimated_total = quantize_money(estimated_total)

    settlements = {ln.settlement_status for ln in cost_lines}
    if settlements == {BrandCostLine.SETTLEMENT_PAID}:
        agg_settlement = BrandCostLine.SETTLEMENT_PAID
    elif BrandCostLine.SETTLEMENT_ESTIMATED in settlements:
        agg_settlement = BrandCostLine.SETTLEMENT_ESTIMATED
    else:
        agg_settlement = BrandCostLine.SETTLEMENT_INVOICED

    evidences = {ln.evidence_status for ln in cost_lines}
    if has_unknown or BrandCostLine.EVIDENCE_MISSING in evidences:
        agg_evidence = BrandCostLine.EVIDENCE_MISSING
    elif BrandCostLine.EVIDENCE_ESTIMATED in evidences:
        agg_evidence = BrandCostLine.EVIDENCE_ESTIMATED
    else:
        agg_evidence = BrandCostLine.EVIDENCE_SUPPORTED

    is_incomplete = has_unknown or (
        agg_evidence == BrandCostLine.EVIDENCE_MISSING
    )
    total_cost_out = None if has_unknown else running_total
    is_confirmed_zero = (
        not has_unknown
        and running_total == Decimal("0.00")
        and all(
            ln.is_confirmed_zero or ln.category == BrandCostLine.CAT_CREDIT
            for ln in cost_lines
        )
    )

    cps: Decimal | None = None
    cpu: Decimal | None = None
    withheld_reason: str | None = None

    if is_incomplete or total_cost_out is None:
        withheld_reason = (
            "Efficiency metrics withheld until missing rates and required proof are resolved."
        )
    else:
        if visit.samples_distributed and visit.samples_distributed > 0:
            cps = quantize_money(
                total_cost_out / Decimal(visit.samples_distributed)
            )
        if visit.rep_observed_units and visit.rep_observed_units > 0:
            cpu = quantize_money(
                total_cost_out / Decimal(visit.rep_observed_units)
            )
        if cps is None and cpu is None:
            withheld_reason = (
                "Efficiency metrics withheld because sample/unit counts are zero or unrecorded."
            )

    return VisitCostSummary(
        visit_uuid=str(visit.uuid),
        total_cost=total_cost_out,
        supported_cost=supported_total,
        invoiced_cost=invoiced_total,
        paid_cost=paid_total,
        estimated_cost=estimated_total,
        is_incomplete=is_incomplete,
        is_confirmed_zero=is_confirmed_zero,
        settlement_status=agg_settlement,
        evidence_status=agg_evidence,
        cost_per_sample=cps,
        cost_per_observed_unit=cpu,
        efficiency_withheld_reason=withheld_reason,
        category_totals={k: str(quantize_money(v)) for k, v in cat_totals.items()},
        line_count=len(cost_lines),
    )


def sync_agreement_percentage_fees(visit: CanonicalVisit) -> None:
    """Recompute auto-generated percentage admin/service fees and tax lines
    on `visit` from its active ProviderAgreement (e.g. 5% admin fee on
    `["product"]` only)."""
    lines = list(
        BrandCostLine.objects.select_related("agreement").filter(
            tenant=visit.tenant, canonical_visit=visit
        )
    )
    agreement: ProviderAgreement | None = None
    for ln in lines:
        if ln.agreement is not None:
            agreement = ln.agreement
            break
    if agreement is None and visit.primary_provider_id:
        qs = ProviderAgreement.objects.filter(
            tenant=visit.tenant,
            provider_id=visit.primary_provider_id,
            is_active=True,
        )
        if visit.event_type_id:
            agreement = (
                qs.filter(
                    Q(event_type_id=visit.event_type_id)
                    | Q(event_type__isnull=True)
                )
                .order_by("-event_type_id", "-id")
                .first()
            )
        else:
            agreement = qs.order_by("-id").first()

    if agreement is None:
        return

    base_lines = [ln for ln in lines if not ln.is_auto_generated_fee]

    # Admin / service fee on configured bases only (never applied to wrong base!)
    if agreement.admin_fee_percent > Decimal("0.00") and agreement.admin_fee_bases:
        allowed_bases = set(agreement.admin_fee_bases)
        relevant = [ln for ln in base_lines if ln.category in allowed_bases]
        if any(ln.amount is None for ln in relevant):
            fee_amt = None
            ev_status = BrandCostLine.EVIDENCE_MISSING
        else:
            base_sum = sum(
                (signed_line_amount(ln) or Decimal("0.00")) for ln in relevant
            )
            fee_amt = quantize_money(
                max(base_sum, Decimal("0.00"))
                * (agreement.admin_fee_percent / Decimal("100"))
            )
            ev_status = BrandCostLine.EVIDENCE_SUPPORTED
        settlement = (
            BrandCostLine.SETTLEMENT_INVOICED
            if any(
                ln.settlement_status
                in {
                    BrandCostLine.SETTLEMENT_INVOICED,
                    BrandCostLine.SETTLEMENT_PAID,
                }
                for ln in relevant
            )
            else BrandCostLine.SETTLEMENT_ESTIMATED
        )
        BrandCostLine.objects.update_or_create(
            tenant=visit.tenant,
            canonical_visit=visit,
            category=BrandCostLine.CAT_SERVICE_ADMIN_FEE,
            is_auto_generated_fee=True,
            defaults={
                "provider": agreement.provider,
                "agreement": agreement,
                "source_type": BrandCostLine.SOURCE_AGREEMENT,
                "description": (
                    f"{agreement.admin_fee_percent}% admin fee on "
                    f"{', '.join(sorted(allowed_bases))}"
                ),
                "amount": fee_amt,
                "is_confirmed_zero": fee_amt == Decimal("0.00"),
                "settlement_status": settlement,
                "evidence_status": ev_status,
            },
        )
    else:
        BrandCostLine.objects.filter(
            tenant=visit.tenant,
            canonical_visit=visit,
            category=BrandCostLine.CAT_SERVICE_ADMIN_FEE,
            is_auto_generated_fee=True,
        ).delete()

    if agreement.tax_percent > Decimal("0.00") and agreement.tax_bases:
        tax_bases = set(agreement.tax_bases)
        relevant_tax = [ln for ln in base_lines if ln.category in tax_bases]
        if any(ln.amount is None for ln in relevant_tax):
            tax_amt = None
            ev_status = BrandCostLine.EVIDENCE_MISSING
        else:
            tax_base_sum = sum(
                (signed_line_amount(ln) or Decimal("0.00"))
                for ln in relevant_tax
            )
            tax_amt = quantize_money(
                max(tax_base_sum, Decimal("0.00"))
                * (agreement.tax_percent / Decimal("100"))
            )
            ev_status = BrandCostLine.EVIDENCE_SUPPORTED
        BrandCostLine.objects.update_or_create(
            tenant=visit.tenant,
            canonical_visit=visit,
            category=BrandCostLine.CAT_TAX,
            is_auto_generated_fee=True,
            defaults={
                "provider": agreement.provider,
                "agreement": agreement,
                "source_type": BrandCostLine.SOURCE_AGREEMENT,
                "description": (
                    f"{agreement.tax_percent}% tax on {', '.join(sorted(tax_bases))}"
                ),
                "amount": tax_amt,
                "is_confirmed_zero": tax_amt == Decimal("0.00"),
                "settlement_status": BrandCostLine.SETTLEMENT_INVOICED,
                "evidence_status": ev_status,
            },
        )


def recompute_visit_cost_summary(visit: CanonicalVisit) -> VisitCostSummary:
    """Single-pass recomputation updating cached visit cost fields immediately."""
    sync_agreement_percentage_fees(visit)
    summary = compute_visit_cost_summary(visit)
    visit.cached_total_cost = summary.total_cost
    visit.cached_supported_cost = summary.supported_cost
    visit.cached_cost_incomplete = summary.is_incomplete
    visit.cached_settlement_status = summary.settlement_status
    visit.cached_evidence_status = summary.evidence_status
    visit.save(
        update_fields=[
            "cached_total_cost",
            "cached_supported_cost",
            "cached_cost_incomplete",
            "cached_settlement_status",
            "cached_evidence_status",
            "updated_at",
        ]
    )
    return summary


@transaction.atomic
def upsert_provider_agreement(
    access: ResolvedWorkspaceAccess,
    *,
    provider_uuid: str,
    name: str,
    contracted_demo_fee: Decimal | None = None,
    included_hours: Decimal = Decimal("4.00"),
    extra_hour_rate: Decimal | None = None,
    mileage_rate: Decimal | None = None,
    admin_fee_percent: Decimal = Decimal("0.00"),
    admin_fee_bases: list[str] | None = None,
    tax_percent: Decimal = Decimal("0.00"),
    tax_bases: list[str] | None = None,
    event_type_id: int | None = None,
) -> ProviderAgreement:
    if not access.can_edit_finance and not access.is_brand_admin:
        raise PermissionError(
            "Only brand administrators or finance reviewers can edit agreements."
        )
    require_plan_feature(
        access.tenant,
        "allows_brand_cost_tracking",
        feature_label="Brand-paid demo cost tracking",
        required_plan="pro",
    )
    provider = ProviderOrganization.objects.filter(uuid=provider_uuid).first()
    if provider is None:
        raise ValueError("Provider organization not found.")

    valid_cats = {c[0] for c in BrandCostLine.CATEGORY_CHOICES}
    clean_admin_bases = [b for b in (admin_fee_bases or []) if b in valid_cats]
    clean_tax_bases = [b for b in (tax_bases or []) if b in valid_cats]

    agreement, _ = ProviderAgreement.objects.update_or_create(
        tenant=access.tenant,
        provider=provider,
        name=(name or "Standard Agreement").strip(),
        defaults={
            "event_type_id": event_type_id,
            "contracted_demo_fee": (
                quantize_money(contracted_demo_fee)
                if contracted_demo_fee is not None
                else None
            ),
            "included_hours": Decimal(str(included_hours)),
            "extra_hour_rate": (
                quantize_money(extra_hour_rate)
                if extra_hour_rate is not None
                else None
            ),
            "mileage_rate": (
                Decimal(str(mileage_rate)) if mileage_rate is not None else None
            ),
            "admin_fee_percent": Decimal(str(admin_fee_percent)),
            "admin_fee_bases": clean_admin_bases,
            "tax_percent": Decimal(str(tax_percent)),
            "tax_bases": clean_tax_bases,
            "is_active": True,
            "created_by": access.user,
        },
    )
    log_workspace_audit(
        tenant=access.tenant,
        actor=access.user,
        category="cost",
        action="agreement_upserted",
        target_type="ProviderAgreement",
        target_id=str(agreement.uuid),
        summary=f"Saved provider agreement '{agreement.name}' for {provider.name}.",
    )
    return agreement


@transaction.atomic
def upsert_brand_cost_line(
    access: ResolvedWorkspaceAccess,
    *,
    visit_uuid: str,
    category: str,
    source_type: str = BrandCostLine.SOURCE_INVOICE_LINE,
    amount: Decimal | None = None,
    is_confirmed_zero: bool = False,
    quantity: Decimal | None = None,
    unit_rate: Decimal | None = None,
    description: str = "",
    settlement_status: str = BrandCostLine.SETTLEMENT_INVOICED,
    evidence_status: str | None = None,
    provider_uuid: str | None = None,
    agreement_uuid: str | None = None,
    invoice_line_id: int | None = None,
    source_reference: str = "",
    line_uuid: str | None = None,
) -> BrandCostLine:
    if not access.can_edit_finance and not access.can_edit_brand_ops:
        raise PermissionError(
            "You do not have permission to edit brand demo costs."
        )
    require_plan_feature(
        access.tenant,
        "allows_brand_cost_tracking",
        feature_label="Brand-paid demo cost tracking",
        required_plan="pro",
    )
    visit = CanonicalVisit.objects.filter(
        tenant=access.tenant, uuid=visit_uuid
    ).first()
    if visit is None:
        raise ValueError("Canonical visit not found.")

    valid_cats = {c[0] for c in BrandCostLine.CATEGORY_CHOICES}
    if category not in valid_cats:
        raise ValueError(f"Unsupported cost category: {category}")

    provider = (
        ProviderOrganization.objects.filter(uuid=provider_uuid).first()
        if provider_uuid
        else visit.primary_provider
    )
    agreement = (
        ProviderAgreement.objects.filter(
            tenant=access.tenant, uuid=agreement_uuid
        ).first()
        if agreement_uuid
        else None
    )
    invoice_line = (
        InvoiceLineItem.objects.filter(
            id=invoice_line_id, invoice__tenant=access.tenant
        ).first()
        if invoice_line_id
        else None
    )

    resolved_amount: Decimal | None = None
    if amount is not None:
        resolved_amount = quantize_money(abs(amount))
    elif quantity is not None and unit_rate is not None:
        resolved_amount = quantize_money(
            Decimal(str(quantity)) * Decimal(str(unit_rate))
        )

    if resolved_amount == Decimal("0.00") and not is_confirmed_zero:
        is_confirmed_zero = True

    if evidence_status is None:
        if resolved_amount is None:
            evidence_status = BrandCostLine.EVIDENCE_MISSING
        elif (
            category in REIMBURSABLE_CATEGORIES
            and resolved_amount > Decimal("0.00")
        ):
            evidence_status = BrandCostLine.EVIDENCE_MISSING
        elif source_type in {
            BrandCostLine.SOURCE_INVOICE_LINE,
            BrandCostLine.SOURCE_CREDIT,
        }:
            evidence_status = BrandCostLine.EVIDENCE_SUPPORTED
        else:
            evidence_status = BrandCostLine.EVIDENCE_ESTIMATED

    defaults = {
        "provider": provider,
        "agreement": agreement,
        "invoice_line": invoice_line,
        "category": category,
        "source_type": source_type,
        "description": (description or "").strip()[:300],
        "quantity": Decimal(str(quantity)) if quantity is not None else None,
        "unit_rate": Decimal(str(unit_rate)) if unit_rate is not None else None,
        "amount": resolved_amount,
        "is_confirmed_zero": bool(
            is_confirmed_zero and resolved_amount == Decimal("0.00")
        ),
        "settlement_status": settlement_status,
        "evidence_status": evidence_status,
        "source_reference": (source_reference or "").strip()[:120],
        "created_by": access.user,
    }

    if line_uuid:
        line = BrandCostLine.objects.filter(
            tenant=access.tenant, canonical_visit=visit, uuid=line_uuid
        ).first()
        if line is None:
            raise ValueError("Cost line not found.")
        for k, v in defaults.items():
            setattr(line, k, v)
        line.save()
    else:
        line = BrandCostLine.objects.create(
            tenant=access.tenant,
            canonical_visit=visit,
            **defaults,
        )

    recompute_visit_cost_summary(visit)
    return line


def attach_receipt_evidence_to_cost_line(
    *,
    tenant: Any,
    cost_line: BrandCostLine,
    recap_file: CustomRecapFile | None = None,
    receipt_reading: ReceiptReading | None = None,
    proof_url: str = "",
    label: str = "",
    verified_amount: Decimal | None = None,
    is_itemized: bool | None = None,
    linked_by: Any = None,
) -> CostEvidenceLink:
    """Create CostEvidenceLink on an existing BrandCostLine without inventing a
    second charge. Permission checks belong to the caller."""
    if receipt_reading is not None and recap_file is None:
        recap_file = receipt_reading.custom_recap_file

    resolved_itemized = True
    if is_itemized is not None:
        resolved_itemized = bool(is_itemized)
    elif receipt_reading is not None:
        items = getattr(receipt_reading, "line_items", None) or []
        resolved_itemized = len(items) > 0

    resolved_verified = (
        quantize_money(verified_amount)
        if verified_amount is not None
        else (
            quantize_money(receipt_reading.total)
            if receipt_reading is not None and receipt_reading.total is not None
            else cost_line.amount
        )
    )

    link = CostEvidenceLink.objects.create(
        cost_line=cost_line,
        recap_file=recap_file,
        receipt_reading=receipt_reading,
        proof_url=(proof_url or "").strip()[:500],
        label=(label or "Receipt proof").strip()[:200],
        verified_amount=resolved_verified,
        is_itemized=resolved_itemized,
        linked_by=linked_by,
    )

    cost_line.evidence_status = BrandCostLine.EVIDENCE_SUPPORTED
    cost_line.save(update_fields=["evidence_status", "updated_at"])

    if cost_line.canonical_visit is not None:
        recompute_visit_cost_summary(cost_line.canonical_visit)

    log_workspace_audit(
        tenant=tenant,
        actor=linked_by,
        category="cost",
        action="evidence_linked",
        target_type="BrandCostLine",
        target_id=str(cost_line.uuid),
        summary=(
            f"Linked supporting proof ({link.label}) to {cost_line.category} "
            f"(${cost_line.amount}); itemized={resolved_itemized}."
        ),
    )
    return link


@transaction.atomic
def link_receipt_evidence_to_cost_line(
    access: ResolvedWorkspaceAccess,
    *,
    cost_line_uuid: str,
    recap_file_id: int | None = None,
    receipt_reading_id: int | None = None,
    proof_url: str = "",
    label: str = "",
    verified_amount: Decimal | None = None,
    is_itemized: bool | None = None,
) -> CostEvidenceLink:
    """Attach a receipt / proof to an existing BrandCostLine (e.g. a $40 store
    receipt backing a $40 product invoice line) so proof is linked WITHOUT
    double-counting the $40 charge."""
    if not access.can_edit_finance and not access.can_edit_brand_ops:
        raise PermissionError(
            "You do not have permission to link cost evidence."
        )
    line = (
        BrandCostLine.objects.select_related("canonical_visit")
        .filter(tenant=access.tenant, uuid=cost_line_uuid)
        .first()
    )
    if line is None:
        raise ValueError("Cost line not found.")

    recap_file: CustomRecapFile | None = None
    reading: ReceiptReading | None = None

    if recap_file_id is not None:
        recap_file = CustomRecapFile.objects.filter(
            id=recap_file_id,
            custom_recap__tenant=access.tenant,
        ).first()
        if recap_file is None:
            raise ValueError("Receipt file not found in this brand workspace.")
        reading = getattr(recap_file, "receipt_reading", None)

    if receipt_reading_id is not None:
        reading = (
            ReceiptReading.objects.select_related(
                "custom_recap_file", "custom_recap_file__custom_recap"
            )
            .filter(
                id=receipt_reading_id,
                custom_recap_file__custom_recap__tenant=access.tenant,
            )
            .first()
        )
        if reading is None:
            raise ValueError("Receipt reading not found in this brand workspace.")
        if recap_file is None:
            recap_file = reading.custom_recap_file

    return attach_receipt_evidence_to_cost_line(
        tenant=access.tenant,
        cost_line=line,
        recap_file=recap_file,
        receipt_reading=reading,
        proof_url=proof_url,
        label=label,
        verified_amount=verified_amount,
        is_itemized=is_itemized,
        linked_by=access.user,
    )


def _split_total_by_percentages(
    total: Decimal, percentages: list[Decimal]
) -> list[Decimal]:
    """Penny-exact allocation using largest-remainder rounding so allocated
    shares always sum to `total` to the exact cent."""
    total_q = quantize_money(total)
    total_cents = int((total_q * 100).to_integral_value(rounding=ROUND_HALF_UP))
    pct_sum = sum(percentages)
    if pct_sum <= Decimal("0"):
        raise ValueError("Allocation percentages must be positive.")

    raw_cents = [
        (Decimal(total_cents) * pct) / Decimal("100") for pct in percentages
    ]
    floored = [int(rc // 1) for rc in raw_cents]
    remainders = [
        (rc - Decimal(fl), idx) for idx, (rc, fl) in enumerate(zip(raw_cents, floored))
    ]
    allocated_sum = sum(floored)
    # Only distribute sub-penny rounding remainders (<= len(percentages) cents)
    # when percentages sum to 100%; if percentages intentionally don't sum to
    # 100%, preserve the discrepancy so FinanceException catches it!
    if abs(pct_sum - Decimal("100")) <= Decimal("0.01"):
        diff = total_cents - allocated_sum
        remainders.sort(key=lambda item: item[0], reverse=True)
        for i in range(abs(diff)):
            target_idx = remainders[i % len(remainders)][1]
            floored[target_idx] += 1 if diff > 0 else -1
    return [quantize_money(Decimal(c) / Decimal(100)) for c in floored]


@transaction.atomic
def allocate_shared_cost(
    *,
    actor: Any,
    total_amount: Decimal,
    category: str,
    allocations: list[dict[str, Any]],
    allocation_mode: str = SharedCostSource.MODE_PERCENTAGE,
    provider: ProviderOrganization | None = None,
    description: str = "",
    settlement_status: str = BrandCostLine.SETTLEMENT_INVOICED,
    evidence_status: str = BrandCostLine.EVIDENCE_SUPPORTED,
    invoice_line: InvoiceLineItem | None = None,
    receipt_file: CustomRecapFile | None = None,
    strict_reconcile: bool = True,
) -> tuple[SharedCostSource, list[BrandCostLine]]:
    """Allocate a shared charge across multiple CanonicalVisits (within one
    brand or across co-branded tenants). Each brand only ever receives and sees
    its own BrandCostLine; co-brand identities are never exposed."""
    if not allocations:
        raise ValueError("At least one allocation target is required.")
    total_q = quantize_money(total_amount)
    if total_q < Decimal("0.00"):
        raise ValueError("Shared cost total cannot be negative.")

    visits: list[CanonicalVisit] = []
    for item in allocations:
        v = item.get("visit")
        if v is None:
            v = CanonicalVisit.objects.select_related("tenant").filter(
                uuid=item["visit_uuid"]
            ).first()
        if v is None:
            raise ValueError(f"Allocation target visit not found: {item}")
        visits.append(v)

    if allocation_mode == SharedCostSource.MODE_PERCENTAGE:
        pcts = [Decimal(str(item["percentage"])) for item in allocations]
        pct_sum = sum(pcts)
        if strict_reconcile and abs(pct_sum - Decimal("100")) > Decimal("0.01"):
            raise ValueError(
                f"Allocation percentages sum to {pct_sum}%, expected 100.00%."
            )
        amounts = _split_total_by_percentages(total_q, pcts)
    elif allocation_mode == SharedCostSource.MODE_AMOUNT:
        amounts = [quantize_money(item["amount"]) for item in allocations]
        amt_sum = sum(amounts, Decimal("0.00"))
        if strict_reconcile and amt_sum != total_q:
            raise ValueError(
                f"Allocated amounts sum to ${amt_sum}, expected ${total_q}."
            )
        pcts = [
            (
                ((amt / total_q) * Decimal("100")).quantize(Decimal("0.0001"))
                if total_q > Decimal("0.00")
                else Decimal("0.0000")
            )
            for amt in amounts
        ]
    else:
        raise ValueError(f"Unsupported allocation_mode: {allocation_mode}")

    source = SharedCostSource.objects.create(
        provider=provider,
        category=category,
        source_type=BrandCostLine.SOURCE_ALLOCATION,
        description=(description or "Shared cost allocation")[:300],
        total_amount=total_q,
        allocation_mode=allocation_mode,
        settlement_status=settlement_status,
        evidence_status=evidence_status,
        invoice_line=invoice_line,
        receipt_file=receipt_file,
        created_by=actor if getattr(actor, "is_authenticated", False) else None,
    )

    created_lines: list[BrandCostLine] = []
    affected_visits: dict[int, CanonicalVisit] = {}
    for visit, amt, pct in zip(visits, amounts, pcts):
        line = BrandCostLine.objects.create(
            tenant=visit.tenant,
            canonical_visit=visit,
            provider=provider or visit.primary_provider,
            shared_source=source,
            category=category,
            source_type=BrandCostLine.SOURCE_ALLOCATION,
            description=(
                description
                or f"Allocated {category} share ({quantize_money(pct)}%)"
            )[:300],
            amount=amt,
            is_confirmed_zero=amt == Decimal("0.00"),
            allocation_percentage=pct,
            settlement_status=settlement_status,
            evidence_status=evidence_status,
            created_by=(
                actor if getattr(actor, "is_authenticated", False) else None
            ),
        )
        if receipt_file is not None:
            CostEvidenceLink.objects.create(
                cost_line=line,
                recap_file=receipt_file,
                receipt_reading=getattr(receipt_file, "receipt_reading", None),
                label="Shared receipt allocation proof",
                verified_amount=amt,
                is_itemized=True,
                linked_by=(
                    actor if getattr(actor, "is_authenticated", False) else None
                ),
            )
        created_lines.append(line)
        affected_visits[visit.id] = visit

    for v in affected_visits.values():
        recompute_visit_cost_summary(v)

    return source, created_lines


def build_brand_cost_rollups(
    access: ResolvedWorkspaceAccess,
    *,
    group_by: str = "store",
    period_start: date | None = None,
    period_end: date | None = None,
    provider_uuid: str | None = None,
) -> list[dict[str, Any]]:
    """Aggregate brand-visible demo costs by `store`, `program`, `provider`, or
    `month`, strictly enforcing brand workspace visibility and keeping
    incomplete visits explicitly counted rather than treated as $0."""
    if not access.can_view_brand_consolidated:
        raise PermissionError(
            "Outside providers cannot view consolidated brand cost rollups."
        )
    require_plan_feature(
        access.tenant,
        "allows_brand_cost_tracking",
        feature_label="Brand-paid demo cost tracking",
        required_plan="pro",
    )
    qs = brand_included_visits_queryset(access.tenant).select_related(
        "primary_provider", "event_type"
    )
    if period_start:
        qs = qs.filter(visit_date__gte=period_start)
    if period_end:
        qs = qs.filter(visit_date__lte=period_end)
    if provider_uuid:
        qs = qs.filter(primary_provider__uuid=provider_uuid)

    visits = list(qs)
    visit_ids = [v.id for v in visits]
    lines_by_visit: dict[int, list[BrandCostLine]] = {vid: [] for vid in visit_ids}
    for line in BrandCostLine.objects.filter(
        tenant=access.tenant, canonical_visit_id__in=visit_ids
    ).order_by("id"):
        if line.canonical_visit_id is not None:
            lines_by_visit[line.canonical_visit_id].append(line)

    buckets: dict[str, dict[str, Any]] = {}
    for v in visits:
        if group_by == "store":
            b_key = v.store_key or f"visit:{v.id}"
            b_label = v.store_label or v.address or "Unspecified Store"
        elif group_by == "program":
            b_key = str(v.event_type_id or "unassigned")
            b_label = v.event_type.name if v.event_type else "General Sampling"
        elif group_by == "provider":
            b_key = (
                str(v.primary_provider.uuid)
                if v.primary_provider
                else "internal"
            )
            b_label = (
                v.primary_provider.name
                if v.primary_provider
                else "Brand / Internal"
            )
        elif group_by == "month":
            b_key = v.visit_date.strftime("%Y-%m")
            b_label = b_key
        else:
            raise ValueError(f"Unsupported rollup dimension: {group_by}")

        bucket = buckets.setdefault(
            b_key,
            {
                "key": b_key,
                "label": b_label,
                "visit_count": 0,
                "complete_visit_count": 0,
                "incomplete_visit_count": 0,
                "known_total_cost": Decimal("0.00"),
                "supported_cost": Decimal("0.00"),
                "invoiced_cost": Decimal("0.00"),
                "paid_cost": Decimal("0.00"),
                "estimated_cost": Decimal("0.00"),
                "samples_on_complete_visits": 0,
                "units_on_complete_visits": 0,
            },
        )
        summary = compute_visit_cost_summary(v, lines_by_visit.get(v.id, []))
        bucket["visit_count"] += 1
        bucket["supported_cost"] += summary.supported_cost
        bucket["invoiced_cost"] += summary.invoiced_cost
        bucket["paid_cost"] += summary.paid_cost
        bucket["estimated_cost"] += summary.estimated_cost

        if summary.is_incomplete or summary.total_cost is None:
            bucket["incomplete_visit_count"] += 1
        else:
            bucket["complete_visit_count"] += 1
            bucket["known_total_cost"] += summary.total_cost
            if v.samples_distributed:
                bucket["samples_on_complete_visits"] += v.samples_distributed
            if v.rep_observed_units:
                bucket["units_on_complete_visits"] += v.rep_observed_units

    results: list[dict[str, Any]] = []
    for b in buckets.values():
        all_complete = (
            b["visit_count"] > 0 and b["incomplete_visit_count"] == 0
        )
        cps = (
            quantize_money(
                b["known_total_cost"] / Decimal(b["samples_on_complete_visits"])
            )
            if all_complete and b["samples_on_complete_visits"] > 0
            else None
        )
        cpu = (
            quantize_money(
                b["known_total_cost"] / Decimal(b["units_on_complete_visits"])
            )
            if all_complete and b["units_on_complete_visits"] > 0
            else None
        )
        results.append(
            {
                "key": b["key"],
                "label": b["label"],
                "visit_count": b["visit_count"],
                "complete_visit_count": b["complete_visit_count"],
                "incomplete_visit_count": b["incomplete_visit_count"],
                "total_cost": (
                    str(quantize_money(b["known_total_cost"]))
                    if all_complete
                    else None
                ),
                "known_total_cost": str(quantize_money(b["known_total_cost"])),
                "supported_cost": str(quantize_money(b["supported_cost"])),
                "invoiced_cost": str(quantize_money(b["invoiced_cost"])),
                "paid_cost": str(quantize_money(b["paid_cost"])),
                "estimated_cost": str(quantize_money(b["estimated_cost"])),
                "has_incomplete_costs": not all_complete,
                "cost_per_sample": str(cps) if cps is not None else None,
                "cost_per_observed_unit": str(cpu) if cpu is not None else None,
            }
        )
    results.sort(key=lambda r: r["label"])
    return results
