"""Feature 4 service layer: Assignable financial exception queue decoupled from
recap approval, idempotent evaluation with auto-reopen on changed evidence,
audit history, advisory operational notices, and single-pass cost recompute."""

from __future__ import annotations

import hashlib
import json
from datetime import date
from decimal import Decimal
from typing import Any

from django.db import IntegrityError, transaction
from django.utils import timezone

from billing.models import InvoiceLineItem
from pilot.costs import (
    REIMBURSABLE_CATEGORIES,
    attach_receipt_evidence_to_cost_line,
    link_receipt_evidence_to_cost_line,
    quantize_money,
    recompute_visit_cost_summary,
)
from pilot.entitlements import require_plan_feature
from pilot.models import (
    BrandCostLine,
    CanonicalVisit,
    CostEvidenceLink,
    FinanceException,
    FinanceExceptionAudit,
    SharedCostSource,
)
from pilot.permissions import ResolvedWorkspaceAccess, log_workspace_audit
from recaps.models import ReceiptReading, RecapFlag
from tenants.models import Tenant, User

# Same band as recaps.receipt_reader.compare_spend_to_receipts: $1 or 5%.
AUTO_MATCH_TOLERANCE_ABS = Decimal("1.00")
AUTO_MATCH_TOLERANCE_PCT = Decimal("0.05")
AUTO_MATCH_RECAP_SPEND_SOURCE_PREFIX = "recap-product-spend:"
AUTO_MATCH_LINK_LABEL = "Auto-matched recap receipt"


def amounts_within_auto_match_tolerance(
    left: Decimal | None, right: Decimal | None
) -> bool:
    """True when two money amounts agree within $1 absolute or 5% of either."""
    if left is None or right is None:
        return False
    a = quantize_money(left)
    b = quantize_money(right)
    if a <= Decimal("0.00") or b <= Decimal("0.00"):
        return False
    gap = abs(a - b)
    allowed = max(
        AUTO_MATCH_TOLERANCE_ABS,
        (a * AUTO_MATCH_TOLERANCE_PCT).quantize(Decimal("0.01")),
        (b * AUTO_MATCH_TOLERANCE_PCT).quantize(Decimal("0.01")),
    )
    return gap <= allowed


def is_auto_match_evidence_link(link: CostEvidenceLink) -> bool:
    """True for system auto-match links (never for manually linked proof)."""
    return link.linked_by_id is None and (link.label or "") == AUTO_MATCH_LINK_LABEL


def withdraw_auto_match_evidence_link(link: CostEvidenceLink) -> None:
    """Delete an auto-match CostEvidenceLink and roll evidence_status back if needed."""
    if not is_auto_match_evidence_link(link):
        return
    line = link.cost_line
    link.delete()
    if not CostEvidenceLink.objects.filter(cost_line=line).exists():
        if line.evidence_status != BrandCostLine.EVIDENCE_MISSING:
            line.evidence_status = BrandCostLine.EVIDENCE_MISSING
            line.save(update_fields=["evidence_status", "updated_at"])
            if line.canonical_visit_id is not None:
                recompute_visit_cost_summary(line.canonical_visit)


def _auto_link_still_valid(
    link: CostEvidenceLink, reading: ReceiptReading
) -> bool:
    if reading.status != ReceiptReading.STATUS_READ:
        return False
    if reading.total is None or reading.total <= Decimal("0.00"):
        return False
    return amounts_within_auto_match_tolerance(reading.total, link.cost_line.amount)


def _load_reading_for_auto_match(
    reading: ReceiptReading, *, for_update: bool = False
) -> ReceiptReading | None:
    if reading is None or reading.pk is None:
        return None
    if for_update:
        # Lock the reading row alone — select_related on nullable FKs cannot
        # use FOR UPDATE (Postgres rejects outer-join locks).
        locked = (
            ReceiptReading.objects.select_for_update()
            .filter(pk=reading.pk)
            .first()
        )
        if locked is None:
            return None
        reading = locked
    return (
        ReceiptReading.objects.select_related(
            "custom_recap_file",
            "custom_recap_file__custom_recap",
            "custom_recap_file__custom_recap__tenant",
            "custom_recap_file__custom_recap__event",
        )
        .filter(pk=reading.pk)
        .first()
    )


def _existing_evidence_for_reading(
    reading_id: int, *, for_update: bool = False
) -> CostEvidenceLink | None:
    if for_update:
        locked_ids = list(
            CostEvidenceLink.objects.select_for_update()
            .filter(receipt_reading_id=reading_id)
            .order_by("id")
            .values_list("id", flat=True)
        )
        if not locked_ids:
            return None
        return (
            CostEvidenceLink.objects.select_related("cost_line")
            .filter(id=locked_ids[0])
            .first()
        )
    return (
        CostEvidenceLink.objects.select_related("cost_line")
        .filter(receipt_reading_id=reading_id)
        .order_by("id")
        .first()
    )


def sync_auto_match_after_ocr(reading: ReceiptReading) -> CostEvidenceLink | None:
    """Reconcile auto-match after every OCR persist (success or failure).

    Successful read → match/revalidate. Failed / unreadable / zero total →
    withdraw auto-links only (manual links are preserved).
    """
    reading = _load_reading_for_auto_match(reading)
    if reading is None:
        return None
    if (
        reading.status == ReceiptReading.STATUS_READ
        and reading.total is not None
        and reading.total > Decimal("0.00")
    ):
        return maybe_auto_match_receipt_reading(reading)

    for link in list(
        CostEvidenceLink.objects.select_related("cost_line").filter(
            receipt_reading_id=reading.id
        )
    ):
        if is_auto_match_evidence_link(link):
            withdraw_auto_match_evidence_link(link)
    return None


@transaction.atomic
def maybe_auto_match_receipt_reading(
    reading: ReceiptReading,
) -> CostEvidenceLink | None:
    """Conservatively attach a successful OCR receipt to a visit product line.

    Rules:
    - status=read and total > 0 required; otherwise withdraw stale auto-links.
    - Manual evidence links are never removed.
    - Auto-links are revalidated when OCR is reread; withdrawn if total drifts
      out of tolerance or status is no longer a successful read.
    - Same recap event → CanonicalVisit scope only; visit row is locked before
      creating a recap-spend product line (plus DB uniqueness on source_ref /
      receipt_reading) so concurrent OCR+evaluate cannot double-charge.
    - Prefer a single positive CAT_PRODUCT line whose amount matches within
      tolerance and still needs evidence; never invent a link when multiple
      product lines match.
    - If no product line exists but the recap has a single positive typed
      product-spend amount that matches OCR, create one SOURCE_EXPENSE_RECEIPT
      product line then attach proof (Link Visit happy path, no double charge).
      Reused auto lines refresh amount from current recap spend.
    """
    reading = _load_reading_for_auto_match(reading, for_update=True)
    if reading is None:
        return None

    existing = _existing_evidence_for_reading(reading.id, for_update=True)
    if existing is not None:
        if not is_auto_match_evidence_link(existing):
            # Preserve manually linked proof even if OCR later drifts.
            return existing
        if _auto_link_still_valid(existing, reading):
            ocr_total = quantize_money(reading.total)
            if existing.verified_amount != ocr_total:
                existing.verified_amount = ocr_total
                existing.save(update_fields=["verified_amount"])
            return existing
        withdraw_auto_match_evidence_link(existing)
        # Fall through and attempt a fresh match when still readable.

    if reading.status != ReceiptReading.STATUS_READ:
        return None
    if reading.total is None or reading.total <= Decimal("0.00"):
        return None

    recap_file = reading.custom_recap_file
    if recap_file is None:
        return None
    recap = recap_file.custom_recap
    if recap is None:
        return None
    tenant = recap.tenant
    if tenant is None:
        return None

    visit: CanonicalVisit | None = None
    if recap.event_id:
        visit = (
            CanonicalVisit.objects.select_for_update()
            .filter(tenant=tenant, event_id=recap.event_id)
            .first()
        )

    ocr_total = quantize_money(reading.total)
    candidates: list[BrandCostLine] = []
    if visit is not None:
        product_lines = list(
            BrandCostLine.objects.select_for_update()
            .filter(
                tenant=tenant,
                canonical_visit=visit,
                category=BrandCostLine.CAT_PRODUCT,
                amount__isnull=False,
                amount__gt=Decimal("0.00"),
                evidence_status=BrandCostLine.EVIDENCE_MISSING,
            )
            .order_by("id")
        )
        candidates = [
            ln
            for ln in product_lines
            if amounts_within_auto_match_tolerance(ln.amount, ocr_total)
        ]

    if len(candidates) > 1:
        return None

    target_line: BrandCostLine | None = candidates[0] if candidates else None

    if target_line is None:
        # No matching product line: optionally book one from clear recap spend.
        from recaps.receipt_gate import custom_recap_spend
        from pilot.workspaces import ensure_canonical_visit_for_event

        spend = custom_recap_spend(recap)
        if not spend or spend <= 0:
            return None
        spend_amt = quantize_money(spend)
        if not amounts_within_auto_match_tolerance(spend_amt, ocr_total):
            return None
        if recap.event_id is None:
            return None
        if visit is None:
            visit = ensure_canonical_visit_for_event(recap.event)
            visit = (
                CanonicalVisit.objects.select_for_update().filter(pk=visit.pk).get()
            )
        source_ref = f"{AUTO_MATCH_RECAP_SPEND_SOURCE_PREFIX}{recap.id}"

        # Under the visit lock: re-check for races with concurrent OCR/evaluate.
        raced = _existing_evidence_for_reading(reading.id, for_update=True)
        if raced is not None:
            if not is_auto_match_evidence_link(raced):
                return raced
            if _auto_link_still_valid(raced, reading):
                return raced
            withdraw_auto_match_evidence_link(raced)

        target_line = (
            BrandCostLine.objects.select_for_update()
            .filter(tenant=tenant, source_reference=source_ref)
            .first()
        )
        if target_line is None:
            # Refuse if another product line already exists — creating would
            # risk a second charge when amounts simply did not align.
            if BrandCostLine.objects.filter(
                tenant=tenant,
                canonical_visit=visit,
                category=BrandCostLine.CAT_PRODUCT,
                amount__isnull=False,
                amount__gt=Decimal("0.00"),
            ).exists():
                return None
            try:
                with transaction.atomic():
                    target_line = BrandCostLine.objects.create(
                        tenant=tenant,
                        canonical_visit=visit,
                        provider=visit.primary_provider,
                        category=BrandCostLine.CAT_PRODUCT,
                        source_type=BrandCostLine.SOURCE_EXPENSE_RECEIPT,
                        description=(
                            f"Product spend ({reading.merchant or 'receipt'})"
                        )[:300],
                        amount=spend_amt,
                        is_confirmed_zero=False,
                        settlement_status=BrandCostLine.SETTLEMENT_ESTIMATED,
                        evidence_status=BrandCostLine.EVIDENCE_MISSING,
                        source_reference=source_ref,
                    )
            except IntegrityError:
                target_line = (
                    BrandCostLine.objects.select_for_update()
                    .filter(tenant=tenant, source_reference=source_ref)
                    .first()
                )
                if target_line is None:
                    return None

        # Refresh amount when the BA corrected recap product spend after booking.
        if quantize_money(target_line.amount or Decimal("0.00")) != spend_amt:
            target_line.amount = spend_amt
            target_line.is_confirmed_zero = False
            target_line.save(
                update_fields=["amount", "is_confirmed_zero", "updated_at"]
            )
            if target_line.canonical_visit_id is not None:
                recompute_visit_cost_summary(target_line.canonical_visit)

        if (
            target_line.evidence_status != BrandCostLine.EVIDENCE_MISSING
            and not CostEvidenceLink.objects.filter(
                cost_line=target_line, receipt_reading_id=reading.id
            ).exists()
        ):
            # Already supported by other proof — do not invent a second link.
            return None

    try:
        with transaction.atomic():
            return attach_receipt_evidence_to_cost_line(
                tenant=tenant,
                cost_line=target_line,
                recap_file=recap_file,
                receipt_reading=reading,
                label=AUTO_MATCH_LINK_LABEL,
                verified_amount=ocr_total,
            )
    except IntegrityError:
        return _existing_evidence_for_reading(reading.id)

OPERATIONAL_ADVISORY_FLAG_KINDS = (
    RecapFlag.KIND_SHORT_CLOCK,
    RecapFlag.KIND_LATE_STAFF_FILING,
    RecapFlag.KIND_STORE_MISMATCH,
    RecapFlag.KIND_DUPLICATE_PHOTO,
)


def _fingerprint(payload: dict[str, Any]) -> str:
    serialized = json.dumps(payload, sort_keys=True, default=str)
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def _record_audit(
    exc: FinanceException,
    *,
    actor: Any,
    action: str,
    note: str = "",
    before_state: dict[str, Any] | None = None,
    after_state: dict[str, Any] | None = None,
) -> FinanceExceptionAudit:
    actor_user = (
        actor if actor and getattr(actor, "is_authenticated", False) else None
    )
    return FinanceExceptionAudit.objects.create(
        exception=exc,
        actor=actor_user,
        action=action,
        note=(note or "").strip(),
        before_state=before_state or {},
        after_state=after_state or {},
    )


def _snapshot_exc(exc: FinanceException) -> dict[str, Any]:
    return {
        "status": exc.status,
        "owner_id": exc.owner_id,
        "affected_amount": (
            str(exc.affected_amount) if exc.affected_amount is not None else None
        ),
        "evidence_fingerprint": exc.evidence_fingerprint,
        "resolution_reason": exc.resolution_reason,
    }


@transaction.atomic
def evaluate_finance_exceptions(
    tenant: Tenant,
    *,
    visit: CanonicalVisit | None = None,
) -> list[FinanceException]:
    """Evaluate financial exceptions for `tenant` (or a single `visit`)
    idempotently without modifying recap approval status."""
    visits_qs = (
        CanonicalVisit.objects.select_related("primary_provider")
        .filter(tenant=tenant)
        .exclude(status=CanonicalVisit.STATUS_CANCELLED)
    )
    if visit is not None:
        visits_qs = visits_qs.filter(pk=visit.pk)
    visits = list(visits_qs)
    visit_map = {v.id: v for v in visits}

    # Heal clear OCR→product matches before detecting exceptions so
    # missing_proof and unmatched_transaction both see linked evidence.
    if visit is None:
        already_linked = set(
            CostEvidenceLink.objects.filter(
                cost_line__tenant=tenant, receipt_reading__isnull=False
            ).values_list("receipt_reading_id", flat=True)
        )
        heal_qs = ReceiptReading.objects.select_related(
            "custom_recap_file", "custom_recap_file__custom_recap"
        ).filter(
            custom_recap_file__custom_recap__tenant=tenant,
            status=ReceiptReading.STATUS_READ,
            total__gt=Decimal("0.00"),
        ).exclude(id__in=already_linked)
        for reading in heal_qs.iterator(chunk_size=100):
            maybe_auto_match_receipt_reading(reading)

    lines_qs = (
        BrandCostLine.objects.select_related("provider", "shared_source")
        .prefetch_related("evidence_links", "evidence_links__receipt_reading")
        .filter(tenant=tenant)
    )
    if visit is not None:
        lines_qs = lines_qs.filter(canonical_visit=visit)
    lines = list(lines_qs)

    detected: dict[tuple[str, str], dict[str, Any]] = {}

    def _emit(
        exc_type: str,
        dedupe_key: str,
        *,
        severity: str,
        explanation: str,
        fp_data: dict[str, Any],
        canonical_visit: CanonicalVisit | None = None,
        cost_line: BrandCostLine | None = None,
        shared_source: SharedCostSource | None = None,
        provider_id: int | None = None,
        recap_file_id: int | None = None,
        invoice_line_id: int | None = None,
        affected_amount: Decimal | None = None,
        source_label: str = "",
    ) -> None:
        detected[(exc_type, dedupe_key)] = {
            "exception_type": exc_type,
            "dedupe_key": dedupe_key,
            "severity": severity,
            "explanation": explanation,
            "evidence_fingerprint": _fingerprint(fp_data),
            "canonical_visit": canonical_visit,
            "cost_line": cost_line,
            "shared_source": shared_source,
            "provider_id": provider_id,
            "recap_file_id": recap_file_id,
            "invoice_line_id": invoice_line_id,
            "affected_amount": affected_amount,
            "source_label": source_label[:200],
        }

    lines_by_visit: dict[int, list[BrandCostLine]] = {v.id: [] for v in visits}
    for ln in lines:
        if ln.canonical_visit_id in lines_by_visit:
            lines_by_visit[ln.canonical_visit_id].append(ln)

    # 1. Completed visit with provider but zero cost lines -> missing rate/amount.
    for v in visits:
        v_lines = lines_by_visit.get(v.id, [])
        if (
            v.status == CanonicalVisit.STATUS_COMPLETED
            and not v_lines
            and v.primary_provider_id is not None
        ):
            _emit(
                FinanceException.TYPE_MISSING_RATE,
                f"visit-no-cost:{v.id}",
                severity=FinanceException.SEV_HIGH,
                explanation=(
                    f"Completed visit at {v.store_label or 'store'} on "
                    f"{v.visit_date.isoformat()} has no contracted rate or cost lines."
                ),
                fp_data={"visit_id": v.id, "line_count": 0},
                canonical_visit=v,
                provider_id=v.primary_provider_id,
                affected_amount=None,
                source_label=v.store_label or "Visit cost",
            )

    # 2. Per-cost-line checks: missing amount, missing proof, unitemized proof.
    for ln in lines:
        v_obj = (
            visit_map.get(ln.canonical_visit_id)
            if ln.canonical_visit_id
            else None
        )
        ev_links = list(ln.evidence_links.all())

        if ln.amount is None:
            _emit(
                FinanceException.TYPE_MISSING_RATE,
                f"line-missing-amt:{ln.id}",
                severity=FinanceException.SEV_HIGH,
                explanation=(
                    f"Cost line '{ln.get_category_display()}' is missing a rate or dollar amount."
                ),
                fp_data={
                    "line_id": ln.id,
                    "category": ln.category,
                    "quantity": str(ln.quantity),
                    "unit_rate": str(ln.unit_rate),
                },
                canonical_visit=v_obj,
                cost_line=ln,
                provider_id=ln.provider_id,
                affected_amount=None,
                source_label=ln.description or ln.get_category_display(),
            )
            continue

        if (
            ln.category in REIMBURSABLE_CATEGORIES
            and ln.amount > Decimal("0.00")
            and not ev_links
            and ln.evidence_status == BrandCostLine.EVIDENCE_MISSING
        ):
            _emit(
                FinanceException.TYPE_MISSING_PROOF,
                f"line-missing-proof:{ln.id}",
                severity=FinanceException.SEV_MEDIUM,
                explanation=(
                    f"${ln.amount} {ln.get_category_display()} charge has no supporting receipt or proof attached."
                ),
                fp_data={
                    "line_id": ln.id,
                    "amount": str(ln.amount),
                    "evidence_status": ln.evidence_status,
                    "link_count": 0,
                },
                canonical_visit=v_obj,
                cost_line=ln,
                provider_id=ln.provider_id,
                affected_amount=ln.amount,
                source_label=ln.description or ln.get_category_display(),
            )

        # Unitemized proof check: stays visible EVEN WHEN total matches!
        for link in ev_links:
            reading = link.receipt_reading
            is_unitemized = not link.is_itemized or (
                reading is not None and not (reading.line_items or [])
            )
            if is_unitemized and ln.amount > Decimal("0.00"):
                _emit(
                    FinanceException.TYPE_UNITEMIZED_PROOF,
                    f"line-unitemized:{ln.id}:link:{link.id}",
                    severity=FinanceException.SEV_MEDIUM,
                    explanation=(
                        f"Supporting receipt for ${ln.amount} {ln.get_category_display()} "
                        f"is unitemized (summary slip only, even though total ${link.verified_amount or ln.amount} matches)."
                    ),
                    fp_data={
                        "line_id": ln.id,
                        "link_id": link.id,
                        "amount": str(ln.amount),
                        "verified_amount": str(link.verified_amount),
                        "is_itemized": link.is_itemized,
                    },
                    canonical_visit=v_obj,
                    cost_line=ln,
                    provider_id=ln.provider_id,
                    recap_file_id=link.recap_file_id,
                    affected_amount=ln.amount,
                    source_label=link.label or ln.get_category_display(),
                )

    # 3. Suspected duplicate charges on the same visit (two separate cost lines
    # of the same category & positive amount without shared evidence).
    for v in visits:
        v_lines = [
            ln
            for ln in lines_by_visit.get(v.id, [])
            if ln.amount is not None
            and ln.amount > Decimal("0.00")
            and ln.category != BrandCostLine.CAT_CREDIT
        ]
        seen_by_cat_amt: dict[tuple[str, Decimal], BrandCostLine] = {}
        for ln in v_lines:
            assert ln.amount is not None
            key = (ln.category, quantize_money(ln.amount))
            prior = seen_by_cat_amt.get(key)
            if prior is not None and prior.source_type != ln.source_type:
                _emit(
                    FinanceException.TYPE_SUSPECTED_DUPLICATE,
                    f"dup-lines:{min(prior.id, ln.id)}:{max(prior.id, ln.id)}",
                    severity=FinanceException.SEV_HIGH,
                    explanation=(
                        f"Two separate ${ln.amount} {ln.get_category_display()} charges "
                        f"({prior.source_type} and {ln.source_type}) exist on the same visit; "
                        "confirm whether the receipt backs the invoice line rather than a second charge."
                    ),
                    fp_data={
                        "line_a": prior.id,
                        "line_b": ln.id,
                        "amount": str(ln.amount),
                        "cat": ln.category,
                    },
                    canonical_visit=v,
                    cost_line=ln,
                    provider_id=ln.provider_id or prior.provider_id,
                    affected_amount=ln.amount,
                    source_label=f"{ln.get_category_display()} (${ln.amount})",
                )
            else:
                seen_by_cat_amt[key] = ln

        # Also surface OCR duplicate_receipt RecapFlag on the visit's event.
        if v.event_id:
            dup_flags = RecapFlag.objects.filter(
                tenant=tenant,
                custom_recap__event_id=v.event_id,
                kind=RecapFlag.KIND_DUPLICATE_RECEIPT,
                resolved_at__isnull=True,
            )
            for flag in dup_flags:
                _emit(
                    FinanceException.TYPE_SUSPECTED_DUPLICATE,
                    f"recap-dup-receipt:{flag.id}",
                    severity=FinanceException.SEV_HIGH,
                    explanation=flag.explanation or "Receipt matches another filed receipt.",
                    fp_data={"flag_id": flag.id, "explanation": flag.explanation},
                    canonical_visit=v,
                    provider_id=v.primary_provider_id,
                    recap_file_id=(flag.details or {}).get("file_id"),
                    source_label="Duplicate receipt OCR match",
                )

    # 4. Unmatched transactions (workspace InvoiceLineItems or ReceiptReadings
    # not linked to any BrandCostLine / CostEvidenceLink).
    if visit is None:
        linked_inv_ids = set(
            BrandCostLine.objects.filter(
                tenant=tenant, invoice_line__isnull=False
            ).values_list("invoice_line_id", flat=True)
        )
        unmatched_inv_lines = InvoiceLineItem.objects.select_related(
            "invoice"
        ).filter(
            invoice__tenant=tenant,
            invoice__deleted_at__isnull=True,
        ).exclude(
            invoice__status="void"
        ).exclude(
            id__in=linked_inv_ids
        )
        for inv_line in unmatched_inv_lines:
            matched_visit = (
                CanonicalVisit.objects.filter(
                    tenant=tenant, event_id=inv_line.event_id
                ).first()
                if inv_line.event_id
                else None
            )
            inv_num = inv_line.invoice.number or str(inv_line.invoice_id)
            _emit(
                FinanceException.TYPE_UNMATCHED_TXN,
                f"unmatched-inv-line:{inv_line.id}",
                severity=FinanceException.SEV_MEDIUM,
                explanation=(
                    f"Invoice {inv_num} line "
                    f"'{inv_line.description}' (${inv_line.amount}) is not matched to a visit cost line."
                ),
                fp_data={
                    "invoice_line_id": inv_line.id,
                    "amount": str(inv_line.amount),
                    "event_id": inv_line.event_id,
                },
                canonical_visit=matched_visit,
                invoice_line_id=inv_line.id,
                affected_amount=quantize_money(inv_line.amount),
                source_label=f"Invoice {inv_num}",
            )

        linked_reading_ids = set(
            CostEvidenceLink.objects.filter(
                cost_line__tenant=tenant, receipt_reading__isnull=False
            ).values_list("receipt_reading_id", flat=True)
        )
        unmatched_readings = ReceiptReading.objects.select_related(
            "custom_recap_file", "custom_recap_file__custom_recap"
        ).filter(
            custom_recap_file__custom_recap__tenant=tenant,
            status=ReceiptReading.STATUS_READ,
        ).exclude(id__in=linked_reading_ids)
        for reading in unmatched_readings:
            if reading.total is None or reading.total <= Decimal("0.00"):
                continue
            # Heal clear cases before emitting unmatched_transaction.
            if maybe_auto_match_receipt_reading(reading) is not None:
                continue
            recap_obj = reading.custom_recap_file.custom_recap
            ev_id = recap_obj.event_id if recap_obj else None
            matched_visit = (
                CanonicalVisit.objects.filter(
                    tenant=tenant, event_id=ev_id
                ).first()
                if ev_id
                else None
            )
            _emit(
                FinanceException.TYPE_UNMATCHED_TXN,
                f"unmatched-receipt:{reading.id}",
                severity=FinanceException.SEV_MEDIUM,
                explanation=(
                    f"Receipt from {reading.merchant or 'merchant'} (${reading.total}) "
                    "is not matched to a visit cost line."
                ),
                fp_data={
                    "reading_id": reading.id,
                    "total": str(reading.total),
                    "merchant": reading.merchant,
                },
                canonical_visit=matched_visit,
                recap_file_id=reading.custom_recap_file_id,
                affected_amount=quantize_money(reading.total),
                source_label=reading.merchant or "Receipt upload",
            )

    # 5. Allocation discrepancies on SharedCostSources touching this tenant.
    source_ids = {
        ln.shared_source_id for ln in lines if ln.shared_source_id is not None
    }
    if source_ids:
        sources = SharedCostSource.objects.prefetch_related(
            "allocated_lines"
        ).filter(id__in=source_ids)
        for src in sources:
            all_alloc_lines = list(src.allocated_lines.all())
            alloc_sum = sum(
                (quantize_money(al.amount or Decimal("0.00")) for al in all_alloc_lines),
                Decimal("0.00"),
            )
            if alloc_sum != quantize_money(src.total_amount):
                tenant_line = next(
                    (al for al in all_alloc_lines if al.tenant_id == tenant.id),
                    None,
                )
                v_obj = (
                    visit_map.get(tenant_line.canonical_visit_id)
                    if tenant_line and tenant_line.canonical_visit_id
                    else None
                )
                delta = abs(quantize_money(src.total_amount) - alloc_sum)
                _emit(
                    FinanceException.TYPE_ALLOCATION_DISCREPANCY,
                    f"shared-alloc-disc:{src.id}",
                    severity=FinanceException.SEV_HIGH,
                    explanation=(
                        f"Shared {src.category} source (${src.total_amount}) has "
                        f"an unallocated/overallocated discrepancy of ${delta}."
                    ),
                    fp_data={
                        "source_id": src.id,
                        "total": str(src.total_amount),
                        "allocated_sum": str(alloc_sum),
                    },
                    canonical_visit=v_obj,
                    cost_line=tenant_line,
                    shared_source=src,
                    provider_id=src.provider_id,
                    affected_amount=delta,
                    source_label=src.description or "Shared cost allocation",
                )

    # Reconcile detected items against persisted FinanceExceptions.
    existing_qs = FinanceException.objects.filter(tenant=tenant)
    if visit is not None:
        existing_qs = existing_qs.filter(canonical_visit=visit)
    existing_map = {
        (e.exception_type, e.dedupe_key): e for e in existing_qs
    }

    active_results: list[FinanceException] = []
    for key, spec in detected.items():
        existing = existing_map.pop(key, None)
        if existing is None:
            created = FinanceException.objects.create(
                tenant=tenant,
                canonical_visit=spec["canonical_visit"],
                cost_line=spec["cost_line"],
                shared_source=spec["shared_source"],
                provider_id=spec["provider_id"],
                recap_file_id=spec["recap_file_id"],
                invoice_line_id=spec["invoice_line_id"],
                exception_type=spec["exception_type"],
                severity=spec["severity"],
                status=FinanceException.STATUS_OPEN,
                affected_amount=spec["affected_amount"],
                source_label=spec["source_label"],
                explanation=spec["explanation"],
                dedupe_key=spec["dedupe_key"],
                evidence_fingerprint=spec["evidence_fingerprint"],
            )
            _record_audit(
                created,
                actor=None,
                action="detected",
                note=spec["explanation"],
                after_state=_snapshot_exc(created),
            )
            active_results.append(created)
        else:
            if existing.status in {
                FinanceException.STATUS_RESOLVED,
                FinanceException.STATUS_EXCEPTION_GRANTED,
            }:
                if (
                    existing.evidence_fingerprint
                    and existing.evidence_fingerprint
                    != spec["evidence_fingerprint"]
                ):
                    before = _snapshot_exc(existing)
                    existing.status = FinanceException.STATUS_OPEN
                    existing.evidence_fingerprint = spec["evidence_fingerprint"]
                    existing.affected_amount = spec["affected_amount"]
                    existing.explanation = spec["explanation"]
                    existing.resolved_at = None
                    existing.resolved_by = None
                    existing.save()
                    _record_audit(
                        existing,
                        actor=None,
                        action="reopened_evidence_changed",
                        note="Underlying cost or receipt evidence changed after resolution.",
                        before_state=before,
                        after_state=_snapshot_exc(existing),
                    )
                active_results.append(existing)
            elif existing.status == FinanceException.STATUS_AUTO_CLEARED:
                before = _snapshot_exc(existing)
                existing.status = FinanceException.STATUS_OPEN
                existing.evidence_fingerprint = spec["evidence_fingerprint"]
                existing.affected_amount = spec["affected_amount"]
                existing.explanation = spec["explanation"]
                existing.save()
                _record_audit(
                    existing,
                    actor=None,
                    action="reopened",
                    note=spec["explanation"],
                    before_state=before,
                    after_state=_snapshot_exc(existing),
                )
                active_results.append(existing)
            else:
                # Open or clarification_requested -> update in place without duplicating!
                if (
                    existing.evidence_fingerprint != spec["evidence_fingerprint"]
                    or existing.explanation != spec["explanation"]
                    or existing.affected_amount != spec["affected_amount"]
                ):
                    existing.evidence_fingerprint = spec["evidence_fingerprint"]
                    existing.explanation = spec["explanation"]
                    existing.affected_amount = spec["affected_amount"]
                    existing.save(
                        update_fields=[
                            "evidence_fingerprint",
                            "explanation",
                            "affected_amount",
                            "updated_at",
                        ]
                    )
                active_results.append(existing)

    # Auto-clear any previously open exceptions whose condition no longer holds!
    for leftover in existing_map.values():
        if leftover.status in {
            FinanceException.STATUS_OPEN,
            FinanceException.STATUS_CLARIFICATION,
        }:
            before = _snapshot_exc(leftover)
            leftover.status = FinanceException.STATUS_AUTO_CLEARED
            leftover.resolved_at = timezone.now()
            leftover.save(
                update_fields=["status", "resolved_at", "updated_at"]
            )
            _record_audit(
                leftover,
                actor=None,
                action="auto_cleared",
                note="Underlying condition resolved by linked proof or cost update.",
                before_state=before,
                after_state=_snapshot_exc(leftover),
            )

    return active_results


def list_finance_exceptions(
    access: ResolvedWorkspaceAccess,
    *,
    exception_type: str | None = None,
    status: str | None = None,
    provider_uuid: str | None = None,
    store_key: str | None = None,
    period_start: date | None = None,
    period_end: date | None = None,
) -> list[FinanceException]:
    if not access.can_view_brand_consolidated:
        raise PermissionError(
            "Outside providers cannot view the brand finance exception queue."
        )
    require_plan_feature(
        access.tenant,
        "allows_finance_review_queue",
        feature_label="Finance review queue",
        required_plan="pro",
    )
    qs = FinanceException.objects.select_related(
        "canonical_visit",
        "cost_line",
        "provider",
        "owner",
        "resolved_by",
        "recap_file",
    ).prefetch_related(
        "audits",
        "audits__actor",
        "cost_line__evidence_links",
        "cost_line__evidence_links__receipt_reading",
    ).filter(tenant=access.tenant)
    if exception_type:
        qs = qs.filter(exception_type=exception_type)
    if status:
        qs = qs.filter(status=status)
    if provider_uuid:
        qs = qs.filter(provider__uuid=provider_uuid)
    if store_key:
        qs = qs.filter(canonical_visit__store_key=store_key)
    if period_start:
        qs = qs.filter(canonical_visit__visit_date__gte=period_start)
    if period_end:
        qs = qs.filter(canonical_visit__visit_date__lte=period_end)
    return list(qs)


def list_operational_field_notices(
    access: ResolvedWorkspaceAccess,
    *,
    visit_uuid: str | None = None,
) -> list[dict[str, Any]]:
    """Return non-financial operational field flags (`short_clock`,
    `late_staff_filing`, `store_mismatch`, `duplicate_photo`) as separate
    advisory items, never mixed into financial exceptions."""
    if not access.can_view_brand_consolidated:
        return []
    flags_qs = RecapFlag.objects.select_related("custom_recap").filter(
        tenant=access.tenant,
        kind__in=OPERATIONAL_ADVISORY_FLAG_KINDS,
    )
    if visit_uuid:
        visit = CanonicalVisit.objects.filter(
            tenant=access.tenant, uuid=visit_uuid
        ).first()
        if visit is None or visit.event_id is None:
            return []
        flags_qs = flags_qs.filter(custom_recap__event_id=visit.event_id)

    notices: list[dict[str, Any]] = []
    for f in flags_qs.order_by("-created_at")[:100]:
        notices.append(
            {
                "id": f.id,
                "kind": f.kind,
                "status": "resolved" if f.resolved_at else "open",
                "detail": f.explanation,
                "event_id": f.custom_recap.event_id if f.custom_recap_id else None,
                "created_at": f.created_at.isoformat(),
                "is_advisory_only": True,
            }
        )
    return notices


@transaction.atomic
def assign_finance_exception_owner(
    access: ResolvedWorkspaceAccess,
    *,
    exception_uuid: str,
    owner_user_id: int | None,
) -> FinanceException:
    if not access.can_edit_finance:
        raise PermissionError(
            "Only brand administrators or finance reviewers can assign finance exceptions."
        )
    require_plan_feature(
        access.tenant,
        "allows_finance_review_queue",
        feature_label="Finance review queue",
        required_plan="pro",
    )
    exc = FinanceException.objects.filter(
        tenant=access.tenant, uuid=exception_uuid
    ).first()
    if exc is None:
        raise ValueError("Finance exception not found.")

    owner = (
        User.objects.filter(id=owner_user_id).first()
        if owner_user_id is not None
        else None
    )
    before = _snapshot_exc(exc)
    exc.owner = owner
    exc.save(update_fields=["owner", "updated_at"])
    _record_audit(
        exc,
        actor=access.user,
        action="owner_assigned",
        note=f"Assigned owner to {owner.email if owner else 'unassigned'}.",
        before_state=before,
        after_state=_snapshot_exc(exc),
    )
    log_workspace_audit(
        tenant=access.tenant,
        actor=access.user,
        category="finance",
        action="exception_owner_assigned",
        target_type="FinanceException",
        target_id=str(exc.uuid),
        summary=f"Assigned finance exception ({exc.exception_type}) to {owner.email if owner else 'unassigned'}.",
    )
    return exc


@transaction.atomic
def attach_proof_to_finance_exception(
    access: ResolvedWorkspaceAccess,
    *,
    exception_uuid: str,
    proof_url: str = "",
    label: str = "Uploaded proof",
    recap_file_id: int | None = None,
    receipt_reading_id: int | None = None,
    verified_amount: Decimal | None = None,
    is_itemized: bool = True,
) -> FinanceException:
    if not access.can_edit_finance:
        raise PermissionError(
            "Only brand administrators or finance reviewers can attach finance proof."
        )
    require_plan_feature(
        access.tenant,
        "allows_finance_review_queue",
        feature_label="Finance review queue",
        required_plan="pro",
    )
    exc = (
        FinanceException.objects.select_related("cost_line", "canonical_visit")
        .filter(tenant=access.tenant, uuid=exception_uuid)
        .first()
    )
    if exc is None:
        raise ValueError("Finance exception not found.")
    if exc.cost_line is None:
        raise ValueError(
            "This exception has no linked cost line; match it to a visit/cost line first."
        )

    before = _snapshot_exc(exc)
    link_receipt_evidence_to_cost_line(
        access,
        cost_line_uuid=str(exc.cost_line.uuid),
        recap_file_id=recap_file_id,
        receipt_reading_id=receipt_reading_id,
        proof_url=proof_url,
        label=label,
        verified_amount=verified_amount,
        is_itemized=is_itemized,
    )
    _record_audit(
        exc,
        actor=access.user,
        action="proof_attached",
        note=f"Attached proof ({label}, itemized={is_itemized}).",
        before_state=before,
        after_state=_snapshot_exc(exc),
    )
    if exc.canonical_visit is not None:
        evaluate_finance_exceptions(access.tenant, visit=exc.canonical_visit)
        recompute_visit_cost_summary(exc.canonical_visit)
    exc.refresh_from_db()
    return exc


@transaction.atomic
def fix_finance_exception_match(
    access: ResolvedWorkspaceAccess,
    *,
    exception_uuid: str,
    visit_uuid: str,
    cost_line_uuid: str | None = None,
    category: str = BrandCostLine.CAT_PRODUCT,
) -> FinanceException:
    if not access.can_edit_finance:
        raise PermissionError(
            "Only brand administrators or finance reviewers can match finance items."
        )
    require_plan_feature(
        access.tenant,
        "allows_finance_review_queue",
        feature_label="Finance review queue",
        required_plan="pro",
    )
    exc = FinanceException.objects.filter(
        tenant=access.tenant, uuid=exception_uuid
    ).first()
    if exc is None:
        raise ValueError("Finance exception not found.")
    visit = CanonicalVisit.objects.filter(
        tenant=access.tenant, uuid=visit_uuid
    ).first()
    if visit is None:
        raise ValueError("Target canonical visit not found.")

    before = _snapshot_exc(exc)
    target_line: BrandCostLine | None = None
    if cost_line_uuid:
        target_line = BrandCostLine.objects.filter(
            tenant=access.tenant, canonical_visit=visit, uuid=cost_line_uuid
        ).first()
        if target_line is None:
            raise ValueError("Target cost line not found on visit.")
        if exc.invoice_line_id and target_line.invoice_line_id is None:
            target_line.invoice_line_id = exc.invoice_line_id
            target_line.save(update_fields=["invoice_line", "updated_at"])
        if exc.recap_file_id:
            link_receipt_evidence_to_cost_line(
                access,
                cost_line_uuid=str(target_line.uuid),
                recap_file_id=exc.recap_file_id,
                label="Matched receipt proof",
            )
    else:
        target_line = BrandCostLine.objects.create(
            tenant=access.tenant,
            canonical_visit=visit,
            provider=visit.primary_provider,
            invoice_line_id=exc.invoice_line_id,
            category=category,
            source_type=(
                BrandCostLine.SOURCE_INVOICE_LINE
                if exc.invoice_line_id
                else BrandCostLine.SOURCE_EXPENSE_RECEIPT
            ),
            description=exc.source_label or "Matched transaction",
            amount=exc.affected_amount,
            is_confirmed_zero=exc.affected_amount == Decimal("0.00"),
            settlement_status=BrandCostLine.SETTLEMENT_INVOICED,
            evidence_status=BrandCostLine.EVIDENCE_SUPPORTED,
            created_by=access.user,
        )
        if exc.recap_file_id:
            link_receipt_evidence_to_cost_line(
                access,
                cost_line_uuid=str(target_line.uuid),
                recap_file_id=exc.recap_file_id,
                label="Matched receipt transaction",
            )

    exc.canonical_visit = visit
    exc.cost_line = target_line
    exc.status = FinanceException.STATUS_RESOLVED
    exc.resolution_reason = f"Matched to visit {visit.store_label} ({visit.visit_date.isoformat()})."
    exc.resolved_by = access.user
    exc.resolved_at = timezone.now()
    exc.save()

    _record_audit(
        exc,
        actor=access.user,
        action="match_corrected",
        note=exc.resolution_reason,
        before_state=before,
        after_state=_snapshot_exc(exc),
    )
    recompute_visit_cost_summary(visit)
    log_workspace_audit(
        tenant=access.tenant,
        actor=access.user,
        category="finance",
        action="exception_match_fixed",
        target_type="FinanceException",
        target_id=str(exc.uuid),
        summary=exc.resolution_reason,
    )
    return exc


@transaction.atomic
def request_finance_clarification(
    access: ResolvedWorkspaceAccess,
    *,
    exception_uuid: str,
    note: str,
) -> FinanceException:
    if not access.can_edit_finance:
        raise PermissionError(
            "Only brand administrators or finance reviewers can request clarification."
        )
    require_plan_feature(
        access.tenant,
        "allows_finance_review_queue",
        feature_label="Finance review queue",
        required_plan="pro",
    )
    clean_note = (note or "").strip()
    if not clean_note:
        raise ValueError("Clarification note is required.")

    exc = FinanceException.objects.filter(
        tenant=access.tenant, uuid=exception_uuid
    ).first()
    if exc is None:
        raise ValueError("Finance exception not found.")

    before = _snapshot_exc(exc)
    exc.status = FinanceException.STATUS_CLARIFICATION
    exc.clarification_note = clean_note
    exc.save(update_fields=["status", "clarification_note", "updated_at"])
    _record_audit(
        exc,
        actor=access.user,
        action="clarification_requested",
        note=clean_note,
        before_state=before,
        after_state=_snapshot_exc(exc),
    )
    return exc


@transaction.atomic
def resolve_finance_exception(
    access: ResolvedWorkspaceAccess,
    *,
    exception_uuid: str,
    resolution_status: str,
    reason: str,
    updated_amount: Decimal | None = None,
) -> FinanceException:
    """Resolve a finance exception or grant a documented policy exception,
    immediately recomputing visit and store cost rollups in a single pass."""
    if not access.can_edit_finance:
        raise PermissionError(
            "Only brand administrators or finance reviewers can resolve finance exceptions."
        )
    require_plan_feature(
        access.tenant,
        "allows_finance_review_queue",
        feature_label="Finance review queue",
        required_plan="pro",
    )
    if resolution_status not in {
        FinanceException.STATUS_RESOLVED,
        FinanceException.STATUS_EXCEPTION_GRANTED,
    }:
        raise ValueError(
            "resolution_status must be 'resolved' or 'exception_granted'."
        )
    clean_reason = (reason or "").strip()
    if not clean_reason:
        raise ValueError("A resolution or exception reason is required.")

    exc = (
        FinanceException.objects.select_related("cost_line", "canonical_visit")
        .filter(tenant=access.tenant, uuid=exception_uuid)
        .first()
    )
    if exc is None:
        raise ValueError("Finance exception not found.")

    before = _snapshot_exc(exc)
    if exc.cost_line is not None:
        line = exc.cost_line
        if updated_amount is not None:
            amt_q = quantize_money(abs(updated_amount))
            line.amount = amt_q
            line.is_confirmed_zero = amt_q == Decimal("0.00")
        if resolution_status in {
            FinanceException.STATUS_RESOLVED,
            FinanceException.STATUS_EXCEPTION_GRANTED,
        } and line.amount is not None:
            line.evidence_status = BrandCostLine.EVIDENCE_SUPPORTED
        line.save()

    exc.status = resolution_status
    exc.resolution_reason = clean_reason
    exc.resolved_by = access.user
    exc.resolved_at = timezone.now()
    exc.save()

    if exc.canonical_visit is not None:
        recompute_visit_cost_summary(exc.canonical_visit)

    _record_audit(
        exc,
        actor=access.user,
        action=resolution_status,
        note=clean_reason,
        before_state=before,
        after_state=_snapshot_exc(exc),
    )
    log_workspace_audit(
        tenant=access.tenant,
        actor=access.user,
        category="finance",
        action=f"exception_{resolution_status}",
        target_type="FinanceException",
        target_id=str(exc.uuid),
        summary=f"{exc.exception_type} -> {resolution_status}: {clean_reason}",
    )
    return exc
