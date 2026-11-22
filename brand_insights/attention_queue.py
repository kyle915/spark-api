"""Today's Attention composite queue for the brand/ops dashboard strip.

Four buckets, each capped so the strip stays scannable:

* ``receipt_checks`` — open finance exceptions (brand-visible cost proof
  issues). Staff also see unresolved recap staff flags.
* ``missing_recaps`` — events that owe a filed recap (same definition as
  ``/recaps/missing``).
* ``unstaffed`` — events starting within the next 48 hours with nobody
  assigned (skip cancelled requests and co-activation host copies).
* ``rebooks`` — open high-velocity store rebook actions → ``/buyer-report``.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from typing import Literal

from django.db.models import Exists, OuterRef, Q
from django.utils import timezone

from events.missing_recaps_gate import missing_recaps_disabled_for_tenant
from pilot.models import FinanceException, StoreRecommendationAction
from pilot.permissions import ResolvedWorkspaceAccess, is_ignite_staff_user

Severity = Literal["high", "medium", "low"]
Bucket = Literal["receipt_checks", "missing_recaps", "unstaffed", "rebooks"]

RECEIPT_CAP = 5
OTHER_CAP = 5
UNSTAFFED_HOURS = 48


@dataclass(frozen=True)
class AttentionItem:
    bucket: Bucket
    title: str
    subtitle: str
    severity: Severity
    action_url: str
    action_label: str
    ref_id: str


@dataclass(frozen=True)
class AttentionBucket:
    key: Bucket
    label: str
    count: int
    items: list[AttentionItem]


@dataclass(frozen=True)
class AttentionQueue:
    buckets: list[AttentionBucket]
    total_count: int


def _receipt_items(
    access: ResolvedWorkspaceAccess, *, is_staff: bool
) -> tuple[int, list[AttentionItem]]:
    items: list[AttentionItem] = []

    fin_qs = FinanceException.objects.filter(
        tenant=access.tenant,
        status__in={
            FinanceException.STATUS_OPEN,
            FinanceException.STATUS_CLARIFICATION,
        },
    ).select_related("canonical_visit").order_by("-severity", "-updated_at", "-id")

    if access.is_provider_scoped and access.provider_id:
        fin_qs = fin_qs.filter(
            Q(canonical_visit__primary_provider_id=access.provider_id)
            | Q(provider_id=access.provider_id)
        )

    total = fin_qs.count()
    for ex in fin_qs[:RECEIPT_CAP]:
        visit_label = ""
        if ex.canonical_visit_id:
            visit_label = (
                ex.canonical_visit.store_label
                or ex.canonical_visit.store_key
                or ""
            )
        sev: Severity = "high" if ex.severity == FinanceException.SEV_HIGH else (
            "medium" if ex.severity == FinanceException.SEV_MEDIUM else "low"
        )
        items.append(
            AttentionItem(
                bucket="receipt_checks",
                title=ex.get_exception_type_display(),
                subtitle=visit_label or (ex.explanation or "")[:120],
                severity=sev,
                action_url="/finance-queue",
                action_label="Open finance queue",
                ref_id=str(ex.uuid),
            )
        )

    if is_staff and len(items) < RECEIPT_CAP:
        try:
            from recaps.receipt_audit_schema import list_flags

            flags = list_flags(
                tenant_id=access.tenant.id,
                date_from=None,
                date_to=None,
                kind=None,
                unresolved_only=True,
                first=RECEIPT_CAP,
            )
        except Exception:  # noqa: BLE001
            flags = []
        for flag in flags:
            if len(items) >= RECEIPT_CAP:
                break
            ref = str(getattr(flag, "id", "") or "")
            title = (
                getattr(flag, "kind_label", None)
                or getattr(flag, "kind", None)
                or "Receipt flag"
            )
            items.append(
                AttentionItem(
                    bucket="receipt_checks",
                    title=str(title),
                    subtitle=str(
                        getattr(flag, "explanation", None)
                        or getattr(flag, "event_name", None)
                        or ""
                    )[:120],
                    severity="high"
                    if str(getattr(flag, "severity", "")).lower() == "high"
                    else "medium",
                    action_url="/receipt-checks",
                    action_label="Review receipt",
                    ref_id=ref or f"flag-{len(items)}",
                )
            )
        if flags:
            total = max(total, len(flags))

    return total, items[:RECEIPT_CAP]


def _missing_recap_items(
    access: ResolvedWorkspaceAccess,
) -> tuple[int, list[AttentionItem]]:
    if missing_recaps_disabled_for_tenant(access.tenant):
        return 0, []

    from recaps.filed import missing_recap_events_queryset

    qs = missing_recap_events_queryset(tenant_id=access.tenant.id)
    qs = qs.filter(access.allowed_events_q()).select_related(
        "tenant", "request", "retailer"
    ).order_by("-end_time", "-id")
    total = qs.count()
    items: list[AttentionItem] = []
    for event in qs[:OTHER_CAP]:
        name = event.name or (event.retailer.name if event.retailer_id else "Event")
        when = ""
        if event.end_time:
            when = timezone.localtime(event.end_time).strftime("%b %-d")
        items.append(
            AttentionItem(
                bucket="missing_recaps",
                title=f"Recap due · {name}",
                subtitle=when or "Wrapped — still needs a filed recap",
                severity="high",
                action_url="/recaps/missing",
                action_label="File recap",
                ref_id=str(event.uuid),
            )
        )
    return total, items


def _unstaffed_items(
    access: ResolvedWorkspaceAccess,
) -> tuple[int, list[AttentionItem]]:
    from ambassadors.models import AmbassadorEvent
    from events.models import Event

    now = timezone.now()
    until = now + timedelta(hours=UNSTAFFED_HOURS)

    # Only an approved assignment counts as staffed (pending join requests
    # should still surface as needing attention).
    staffed = Exists(
        AmbassadorEvent.objects.filter(
            event_id=OuterRef("pk"), is_approved=True
        )
    )
    qs = (
        Event.objects.filter(access.allowed_events_q())
        .filter(
            start_time__gte=now,
            start_time__lte=until,
        )
        .exclude(request__deleted_at__isnull=False)
        .filter(
            Q(request__isnull=True)
            | Q(request__deleted_at__isnull=True)
        )
        # Skip co-activation host copies (staff the guest/lead event).
        .filter(co_activation_host__isnull=True)
        .filter(~staffed)
        .select_related("retailer", "request", "tenant")
        .order_by("start_time", "id")
    )

    # Also exclude cancelled request statuses when the status slug is known.
    qs = qs.exclude(
        request__status__slug__in={"cancelled", "canceled", "rejected", "declined"}
    )

    total = qs.count()
    items: list[AttentionItem] = []
    for event in qs[:OTHER_CAP]:
        name = event.name or (event.retailer.name if event.retailer_id else "Shift")
        when = (
            timezone.localtime(event.start_time).strftime("%b %-d · %-I:%M %p")
            if event.start_time
            else "Soon"
        )
        items.append(
            AttentionItem(
                bucket="unstaffed",
                title=f"Unstaffed · {name}",
                subtitle=when,
                severity="high",
                action_url="/schedule",
                action_label="Staff shift",
                ref_id=str(event.uuid),
            )
        )
    return total, items


def _rebook_items(
    access: ResolvedWorkspaceAccess,
) -> tuple[int, list[AttentionItem]]:
    qs = StoreRecommendationAction.objects.filter(
        tenant=access.tenant,
        action_type=StoreRecommendationAction.ACTION_REBOOK,
    ).exclude(
        status__in={
            StoreRecommendationAction.STATUS_COMPLETED,
            StoreRecommendationAction.STATUS_CANCELLED,
            StoreRecommendationAction.STATUS_DECLINED,
        }
    ).order_by("-created_at", "-id")
    if access.is_provider_scoped and access.provider_id:
        qs = qs.filter(provider_id=access.provider_id)

    total = qs.count()
    items: list[AttentionItem] = []
    for action in qs[:OTHER_CAP]:
        items.append(
            AttentionItem(
                bucket="rebooks",
                title=f"Rebook · {action.store_label or action.store_key}",
                subtitle=(
                    f"Due {action.due_date.isoformat()}"
                    if action.due_date
                    else (action.finding_code or "High-velocity store")
                ),
                severity="medium",
                action_url="/buyer-report",
                action_label="Open buyer report",
                ref_id=str(action.uuid),
            )
        )
    return total, items


def build_today_attention_queue(
    access: ResolvedWorkspaceAccess,
) -> AttentionQueue:
    is_staff = is_ignite_staff_user(access.user)

    # Brand members who cannot see consolidated costs still get ops buckets.
    receipt_total, receipt_items = (0, [])
    if access.can_view_brand_consolidated or is_staff or access.can_edit_finance:
        receipt_total, receipt_items = _receipt_items(access, is_staff=is_staff)

    missing_total, missing_items = _missing_recap_items(access)
    unstaffed_total, unstaffed_items = _unstaffed_items(access)
    rebook_total, rebook_items = _rebook_items(access)

    buckets = [
        AttentionBucket(
            key="receipt_checks",
            label="Receipt checks",
            count=receipt_total,
            items=receipt_items,
        ),
        AttentionBucket(
            key="missing_recaps",
            label="Missing recaps",
            count=missing_total,
            items=missing_items,
        ),
        AttentionBucket(
            key="unstaffed",
            label="Unstaffed (48h)",
            count=unstaffed_total,
            items=unstaffed_items,
        ),
        AttentionBucket(
            key="rebooks",
            label="Rebook stores",
            count=rebook_total,
            items=rebook_items,
        ),
    ]
    return AttentionQueue(
        buckets=buckets,
        total_count=sum(b.count for b in buckets),
    )
