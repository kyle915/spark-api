"""Per-tenant weekly digest data builder.

Assembles the three-section payload for the client weekly digest email:

  1. **This week at a glance** — KPI totals + activation / recap counts over the
     trailing 7 days. KPIs are windowed on ``created_at`` (the same anchor the
     period-comparison card uses), so the digest can never drift from the
     in-app numbers.
  2. **Coming up (next 7 days)** — upcoming events ordered by ``start_time``.
  3. **Needs your approval** — requests still awaiting sign-off
     (``reviewed=False``), the same signal the Requests list exposes via its
     ``reviewed`` filter, so the count matches what the client can filter to.

Pure read path: every total is a DB aggregate; only the bounded "coming up" /
"needs approval" preview rows (capped) ever enter Python. No new model and no
migration — the caller (``send_client_weekly_digest``) gates on
``Tenant.scheduled_report_enabled``, so this whole feature is inert until a
tenant opts in.
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass, field

from django.utils import timezone

from events.models import Event, Request
from recaps.tenant_overview import (
    TenantKpiTotals,
    _tenant_event_recap_counts_window,
    _tenant_kpi_totals_window,
)

# Cap the preview lists so a busy week can't produce a 200-row email. The
# totals (``upcoming_total`` / ``pending_total``) are unbounded COUNTs, so the
# email can still say "+ N more" honestly without loading every row.
_MAX_UPCOMING = 12
_MAX_PENDING = 12


@dataclass(frozen=True)
class UpcomingActivation:
    name: str
    when: datetime.datetime | None
    address: str


@dataclass(frozen=True)
class PendingApproval:
    uuid: str
    name: str
    when: datetime.datetime | None


@dataclass(frozen=True)
class WeeklyDigest:
    """The full payload for one tenant's weekly digest email."""

    tenant_id: int
    start: datetime.datetime
    end: datetime.datetime

    # Section 1 — this week at a glance
    kpis: TenantKpiTotals
    completed_activations: int
    recaps_filed: int

    # Section 2 — coming up (next 7 days)
    upcoming: list[UpcomingActivation] = field(default_factory=list)
    upcoming_total: int = 0

    # Section 3 — needs your approval
    pending: list[PendingApproval] = field(default_factory=list)
    pending_total: int = 0

    @property
    def has_content(self) -> bool:
        """True when there's at least one thing worth emailing about.

        A tenant with a totally quiet week (nothing ran, nothing's coming up,
        nothing pending) gets skipped by the command rather than mailed an
        empty report.
        """
        return bool(
            self.completed_activations
            or self.recaps_filed
            or self.upcoming_total
            or self.pending_total
            or self.kpis.total_engagements
            or self.kpis.samples_distributed
        )

    @property
    def upcoming_overflow(self) -> int:
        return max(0, self.upcoming_total - len(self.upcoming))

    @property
    def pending_overflow(self) -> int:
        return max(0, self.pending_total - len(self.pending))


def build_weekly_digest(
    tenant_id: int, now: datetime.datetime | None = None
) -> WeeklyDigest:
    """Build the three-section weekly-digest payload for one tenant.

    ``now`` is injectable for tests / deterministic runs; defaults to
    ``timezone.now()``. The trailing window is ``[now-7d, now)`` and the
    look-ahead window is ``[now, now+7d)``.
    """
    now = now or timezone.now()
    start = now - datetime.timedelta(days=7)
    horizon = now + datetime.timedelta(days=7)
    window = (start, now)

    # Section 1 — KPI totals + recap count over the trailing window. These are
    # the same aggregates the period-comparison card and Insights use.
    kpis = _tenant_kpi_totals_window(tenant_id, window)
    _events_created, recaps_filed = _tenant_event_recap_counts_window(
        tenant_id, window
    )

    # "Completed activations" = events that actually HAPPENED this week
    # (``start_time`` in the trailing window) — what a client reads as "what
    # ran". Deliberately distinct from the created_at-anchored recap count
    # above (a recap can be filed days after the event).
    completed_activations = Event.objects.filter(
        tenant_id=tenant_id,
        start_time__gte=start,
        start_time__lt=now,
    ).count()

    # Section 2 — coming up in the next 7 days, soonest first.
    upcoming_qs = Event.objects.filter(
        tenant_id=tenant_id,
        start_time__gte=now,
        start_time__lt=horizon,
    ).order_by("start_time")
    upcoming_total = upcoming_qs.count()
    upcoming = [
        UpcomingActivation(
            name=(e.name or "Untitled activation"),
            when=e.start_time,
            address=(e.address or "").strip(),
        )
        for e in upcoming_qs[:_MAX_UPCOMING]
    ]

    # Section 3 — requests still awaiting sign-off. ``reviewed=False`` is the
    # exact signal the Requests list filters on, so this count matches what the
    # client sees in-app. Newest first (most recently submitted at the top).
    pending_qs = Request.objects.filter(
        tenant_id=tenant_id,
        reviewed=False,
        deleted_at__isnull=True,
    ).order_by("-date")
    pending_total = pending_qs.count()
    pending = [
        PendingApproval(
            uuid=str(r.uuid),
            name=(r.name or "Untitled request"),
            when=(r.date or r.start_time),
        )
        for r in pending_qs[:_MAX_PENDING]
    ]

    return WeeklyDigest(
        tenant_id=tenant_id,
        start=start,
        end=now,
        kpis=kpis,
        completed_activations=completed_activations,
        recaps_filed=recaps_filed,
        upcoming=upcoming,
        upcoming_total=upcoming_total,
        pending=pending,
        pending_total=pending_total,
    )
