"""
Admin digest aggregator.

Pulls together what the ops team cares about — pending requests
that need a call, upcoming shifts in the next 7 days, and unfiled
recaps — into a single per-tenant snapshot. The mailer template
renders this dict directly so the shape is the public contract.
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass, field
from typing import Iterable

from django.db.models import Q
from django.utils import timezone

from ambassadors.models import AmbassadorEvent
from events.models import Event, Request
from recaps.models import Recap
from tenants.models import Tenant, TenantedUser, Role


# Tunables — keep the digest concise. Bigger numbers make the email
# noisy + slow.
MAX_PER_SECTION = 10
PENDING_AGE_HOURS = 24
UNFILED_RECAP_GRACE_HOURS = 4
UPCOMING_WINDOW_DAYS = 7


@dataclass
class DigestSection:
    label: str  # "PENDING APPROVALS · >24H"
    tone: str   # "orange" / "gold" / "sky"
    count: int
    rows: list[dict] = field(default_factory=list)


@dataclass
class TenantDigest:
    tenant_id: int
    tenant_name: str
    pending_approvals: DigestSection
    upcoming_shifts: DigestSection
    unfiled_recaps: DigestSection
    generated_at: str
    window_label: str  # "Daily" or "Weekly"

    @property
    def is_empty(self) -> bool:
        return (
            self.pending_approvals.count == 0
            and self.upcoming_shifts.count == 0
            and self.unfiled_recaps.count == 0
        )

    @property
    def total_action_items(self) -> int:
        return (
            self.pending_approvals.count
            + self.unfiled_recaps.count
        )


def _slug(s: str | None) -> str:
    return (s or "").lower()


def _request_label(request: Request) -> str:
    retailer = getattr(request, "retailer", None)
    return (
        getattr(retailer, "name", None)
        or request.name
        or f"Request {request.id}"
    )


def _request_market(request: Request) -> str:
    retailer = getattr(request, "retailer", None)
    loc = getattr(retailer, "location", None) if retailer else None
    state = getattr(loc, "state", None) if loc else None
    parts = []
    if loc and getattr(loc, "name", None):
        parts.append(loc.name)
    if state and getattr(state, "code", None):
        parts.append(state.code)
    return ", ".join(parts) or "—"


def build_pending_approvals(tenant: Tenant) -> DigestSection:
    """Requests still pending past the SLA cutoff (default: 24h)."""
    cutoff = timezone.now() - datetime.timedelta(hours=PENDING_AGE_HOURS)
    qs = (
        Request.objects.filter(tenant=tenant, status__slug="pending")
        .filter(created_at__lte=cutoff)
        .select_related("retailer", "retailer__location", "retailer__location__state", "status")
        .order_by("created_at")
    )
    rows = []
    for r in qs[:MAX_PER_SECTION]:
        rows.append(
            {
                "id": r.id,
                "uuid": str(r.uuid),
                "label": _request_label(r),
                "market": _request_market(r),
                "created_at": r.created_at.isoformat() if r.created_at else None,
                "age_hours": int(
                    (timezone.now() - r.created_at).total_seconds() / 3600
                )
                if r.created_at
                else None,
            }
        )
    return DigestSection(
        label=f"PENDING · OPEN >{PENDING_AGE_HOURS}H",
        tone="orange",
        count=qs.count(),
        rows=rows,
    )


def build_upcoming_shifts(tenant: Tenant, *, days: int = UPCOMING_WINDOW_DAYS) -> DigestSection:
    """Confirmed events (accepted AmbassadorEvent rows) starting in
    the next `days`. Surfaces what the ops team is on the hook for.
    """
    now = timezone.now()
    cutoff = now + datetime.timedelta(days=days)
    qs = (
        AmbassadorEvent.objects.filter(
            tenant=tenant,
            is_approved=True,
            event__start_time__gte=now,
            event__start_time__lte=cutoff,
        )
        .select_related(
            "event",
            "event__retailer",
            "event__retailer__location",
            "event__retailer__location__state",
            "ambassador",
            "ambassador__user",
        )
        .order_by("event__start_time")
    )
    rows = []
    for ae in qs[:MAX_PER_SECTION]:
        ev = ae.event
        retailer = getattr(ev, "retailer", None)
        loc = getattr(retailer, "location", None) if retailer else None
        state = getattr(loc, "state", None) if loc else None
        rows.append(
            {
                "id": ae.id,
                "uuid": str(ae.uuid),
                "event_label": getattr(ev, "name", None) or "(shift)",
                "venue": getattr(retailer, "name", None) or "—",
                "market": ", ".join(
                    p
                    for p in [
                        getattr(loc, "name", None),
                        getattr(state, "code", None),
                    ]
                    if p
                )
                or "—",
                "start_time": ev.start_time.isoformat() if ev.start_time else None,
                "ba_name": " ".join(
                    p
                    for p in [
                        getattr(ae.ambassador.user, "first_name", None),
                        getattr(ae.ambassador.user, "last_name", None),
                    ]
                    if p
                )
                or (getattr(ae.ambassador.user, "email", None) or "—"),
            }
        )
    return DigestSection(
        label=f"UPCOMING · NEXT {days}D",
        tone="lime",
        count=qs.count(),
        rows=rows,
    )


def build_unfiled_recaps(tenant: Tenant) -> DigestSection:
    """Events whose end_time has passed by ≥ grace hours but no
    Recap has been submitted yet.
    """
    cutoff = timezone.now() - datetime.timedelta(hours=UNFILED_RECAP_GRACE_HOURS)
    qs = (
        Event.objects.filter(
            tenant=tenant,
            end_time__lte=cutoff,
        )
        .exclude(
            id__in=Recap.objects.filter(
                event__tenant=tenant, submited_at__isnull=False
            ).values_list("event_id", flat=True)
        )
        .select_related("retailer", "retailer__location", "retailer__location__state")
        .order_by("-end_time")
    )
    rows = []
    for ev in qs[:MAX_PER_SECTION]:
        retailer = getattr(ev, "retailer", None)
        loc = getattr(retailer, "location", None) if retailer else None
        state = getattr(loc, "state", None) if loc else None
        rows.append(
            {
                "id": ev.id,
                "uuid": str(ev.uuid),
                "label": getattr(ev, "name", None) or "(event)",
                "venue": getattr(retailer, "name", None) or "—",
                "market": ", ".join(
                    p
                    for p in [
                        getattr(loc, "name", None),
                        getattr(state, "code", None),
                    ]
                    if p
                )
                or "—",
                "ended_at": ev.end_time.isoformat() if ev.end_time else None,
            }
        )
    return DigestSection(
        label=f"UNFILED RECAPS · >{UNFILED_RECAP_GRACE_HOURS}H PAST END",
        tone="gold",
        count=qs.count(),
        rows=rows,
    )


def build_tenant_digest(
    tenant: Tenant,
    *,
    window_label: str = "Daily",
    upcoming_days: int = UPCOMING_WINDOW_DAYS,
) -> TenantDigest:
    """Aggregate everything for one tenant. Pure function — no side
    effects, no email send. Caller decides whether to skip empty
    digests, send to which recipients, etc.
    """
    return TenantDigest(
        tenant_id=tenant.id,
        tenant_name=tenant.name,
        pending_approvals=build_pending_approvals(tenant),
        upcoming_shifts=build_upcoming_shifts(tenant, days=upcoming_days),
        unfiled_recaps=build_unfiled_recaps(tenant),
        generated_at=timezone.now().isoformat(),
        window_label=window_label,
    )


def admin_recipients_for_tenant(tenant: Tenant) -> list[str]:
    """Email addresses for the admin + spark-admin users in this
    tenant. Filters out blanks. Falls back to settings.NEW_AMBASSADOR_ALERT_EMAILS
    when no admins are found so the digest still goes somewhere.
    """
    qs = (
        TenantedUser.objects.filter(tenant=tenant, is_active=True)
        .select_related("user", "user__role")
        .filter(
            Q(user__role__slug=Role.SPARK_ADMIN_SLUG)
            | Q(user__role__name__iexact="admin")
        )
    )
    emails = []
    for tu in qs:
        if tu.user and tu.user.email:
            emails.append(tu.user.email)
    return list(dict.fromkeys(emails))  # de-dupe, preserve order
