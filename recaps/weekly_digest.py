"""Per-tenant weekly digest data builder.

Assembles the payload for the client weekly digest email:

  1. **This week at a glance** — KPI totals + activation / recap counts over the
     trailing 7 days. KPIs are windowed on ``created_at`` (the same anchor the
     period-comparison card uses), so the digest can never drift from the
     in-app numbers.
  2. **Coming up (next 7 days)** — upcoming events ordered by ``start_time``.

Pure read path: every total is a DB aggregate; only the bounded "coming up"
preview rows (capped) ever enter Python. No new model and no
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
class SharedRecapLink:
    name: str
    url: str
    retailer: str
    shared_at: datetime.datetime | None


@dataclass(frozen=True)
class StoreCoverage:
    """Roster coverage as of digest end — same definition as Account Map."""

    covered: int
    never_visited: int
    roster_total: int
    never_names: list[str] = field(default_factory=list)


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

    # Section 4 — recaps shared this week, each with its own /r/:token link
    shared_recaps: list[SharedRecapLink] = field(default_factory=list)
    # Section 5 — stores covered / never visited (roster, filed-recap based)
    coverage: StoreCoverage | None = None

    @property
    def has_content(self) -> bool:
        """True when there's at least one thing worth emailing about.

        A tenant with a totally quiet week (nothing ran, nothing's coming up)
        gets skipped by the command rather than mailed an empty report.
        """
        return bool(
            self.completed_activations
            or self.recaps_filed
            or self.upcoming_total
            or self.kpis.total_engagements
            or self.kpis.samples_distributed
            or self.shared_recaps
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
    """Build the weekly-digest payload for one tenant.

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

    shared_recaps = _shared_recaps_window(tenant_id, window)
    coverage = _store_coverage(tenant_id)

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
        shared_recaps=shared_recaps,
        coverage=coverage,
    )


_MAX_SHARED = 25
_MAX_NEVER_NAMES = 12


def _client_recap_base() -> str:
    from urllib.parse import urlparse

    from django.conf import settings

    raw = str(getattr(settings, "CLIENT_FRONTEND_URL", "") or "").strip().rstrip("/")
    host = (urlparse(raw).hostname or "").lower() if raw else ""
    if not raw or host in {"spark.igniteproductions.co", "admin.igniteproductions.co"}:
        return "https://client.igniteproductions.co"
    return raw


def _shared_recaps_window(tenant_id: int, window) -> list[SharedRecapLink]:
    """Approved, non-archived recaps whose shared_at lands in the window.

    Each gets its OWN signed /r/:token link — one digest email, many links
    (mirrors bulk share). Unapproved recaps are never linked to clients.
    """
    from recaps import models as recap_models
    from recaps.recap_tokens import make_recap_token

    start, end = window
    base = _client_recap_base()
    out: list[SharedRecapLink] = []
    legacy = (
        recap_models.Recap.objects.filter(
            event__tenant_id=tenant_id,
            approved=True,
            archived_at__isnull=True,
            shared_at__gte=start,
            shared_at__lt=end,
        )
        .select_related("event__retailer", "retailer")
        .order_by("-shared_at")
    )
    custom = (
        recap_models.CustomRecap.objects.filter(
            event__tenant_id=tenant_id,
            approved=True,
            archived_at__isnull=True,
            shared_at__gte=start,
            shared_at__lt=end,
        )
        .select_related("event__retailer", "retailer")
        .order_by("-shared_at")
    )
    rows = [(r.shared_at, "legacy", r) for r in legacy] + [
        (r.shared_at, "custom", r) for r in custom
    ]
    rows.sort(key=lambda t: t[0] or start, reverse=True)
    for shared_at, kind, recap in rows[:_MAX_SHARED]:
        retailer = (
            getattr(getattr(recap, "retailer", None), "name", None)
            or getattr(getattr(getattr(recap, "event", None), "retailer", None), "name", None)
            or ""
        )
        out.append(
            SharedRecapLink(
                name=(recap.name or "Recap"),
                url=f"{base}/r/{make_recap_token(kind, recap.pk)}",
                retailer=retailer,
                shared_at=shared_at,
            )
        )
    return out


def _store_coverage(tenant_id: int) -> StoreCoverage:
    from events.admin_payloads import compute_account_map_coverage

    data = compute_account_map_coverage(tenant_id)
    covered = data.sampled + data.repeat
    never_names: list[str] = []
    if data.never_visited:
        from events.admin_payloads import retailer_visit_counts
        from events.models import Retailer

        visits = retailer_visit_counts(tenant_id)
        never_names = list(
            Retailer.objects.filter(tenant_id=tenant_id)
            .exclude(id__in=[rid for rid, n in visits.items() if n > 0])
            .order_by("name")
            .values_list("name", flat=True)[:_MAX_NEVER_NAMES]
        )
    return StoreCoverage(
        covered=covered,
        never_visited=data.never_visited,
        roster_total=data.roster_total,
        never_names=never_names,
    )


def client_digest_recipients(tenant) -> list[str]:
    """Digest recipients: recap recipients + client-role users, deduped.

    Torch's Monday roll-up recipient override stays in the command — this
    helper is the non-Torch path. Girl Beer keeps its per-recap approved-mail
    exclusion in ``recaps.mutation_parts.notify``; the digest is opt-in only
    (``client_weekly_digest_enabled``), so no tenant is added here silently.
    """
    from tenants.models import Role, TenantedUser

    out: list[str] = []
    seen: set[str] = set()

    def _push(email):
        e = (email or "").strip()
        if e and e.lower() not in seen:
            seen.add(e.lower())
            out.append(e)

    for e in tenant.scheduled_report_recipients():
        _push(e)
    rows = (
        TenantedUser.objects.filter(
            tenant_id=tenant.id, is_active=True, user__role__slug=Role.CLIENT_SLUG
        )
        .exclude(user__email__in=["system@spark.local", "admin@spark.local"])
        .values_list("user__email", flat=True)
    )
    for e in rows:
        _push(e)
    return out
