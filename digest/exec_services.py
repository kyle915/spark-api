"""
Executive summary aggregation — weekly top-line stats for a tenant.

Pairs with the daily admin digest (`digest/services.py`). The daily
digest is action-oriented ("you have 5 pending approvals"); this one
is read-oriented ("here's what the team did last week").

What goes in:
  - Total recaps filed in the window
  - Total consumers reached (sum of total_consumer across recaps)
  - Total product samples distributed
  - Top 3 stores by consumer reach
  - Top 3 BAs by recaps filed
  - Week-over-week deltas on the two headline numbers (recaps + reach)

The aggregation runs sync (Django ORM) — cheap for typical tenant
sizes (<10k recaps/week). If a tenant grows past that we can move
to background prefetch.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone as _tz
from typing import Iterable

from django.db.models import Count, Sum

from recaps.models import Recap
from tenants.models import Tenant


@dataclass
class TopRow:
    """One row in the 'Top stores' / 'Top BAs' tables."""

    label: str
    primary_metric: int  # the value that ranks the row
    secondary_metric: str | None = None  # supporting context


@dataclass
class ExecutiveSummary:
    """One tenant's weekly rollup. Rendered straight into the
    executive_summary.html template — no further computation in the
    template layer.
    """

    tenant_id: int
    tenant_name: str
    period_label: str  # "Week of May 13 – May 19, 2026"
    recap_count: int
    consumer_reach: int
    samples_distributed: int
    top_stores: list[TopRow] = field(default_factory=list)
    top_bas: list[TopRow] = field(default_factory=list)
    # Week-over-week deltas. Positive means up vs previous period;
    # None means no comparison available (e.g. tenant is brand new).
    recap_count_delta: int | None = None
    consumer_reach_delta: int | None = None

    @property
    def is_empty(self) -> bool:
        return self.recap_count == 0

    def delta_chip(self, kind: str) -> str | None:
        """Human-readable chip text like '↑ 12% vs last week' or
        '↓ 3' for the email template to render alongside each
        headline number.
        """
        if kind == "recaps":
            cur, delta = self.recap_count, self.recap_count_delta
        elif kind == "reach":
            cur, delta = self.consumer_reach, self.consumer_reach_delta
        else:
            return None
        if delta is None:
            return None
        prev = cur - delta
        if prev <= 0:
            if delta > 0:
                return f"new this week"
            return None
        pct = round((delta / prev) * 100)
        arrow = "↑" if delta >= 0 else "↓"
        return f"{arrow} {abs(pct)}% vs prior week"


def _window(now: datetime, days: int = 7) -> tuple[datetime, datetime]:
    """Returns (start, end) for the trailing `days`-day window ending
    at `now`. Inclusive of `start`, exclusive of `end`.
    """
    return (now - timedelta(days=days), now)


def _format_period(start: datetime, end: datetime) -> str:
    # "Week of May 13 – May 19, 2026"
    return f"Week of {start.strftime('%b %d')} – {(end - timedelta(seconds=1)).strftime('%b %d, %Y')}"


def _ranked(
    rows: Iterable[tuple[str, int, str | None]],
    *,
    top_n: int = 3,
) -> list[TopRow]:
    """Sort + cap a list of (label, primary, secondary) triples into
    TopRow objects. Stable sort on the secondary field so ties read
    deterministically.
    """
    triples = [t for t in rows if t[0]]  # drop unlabeled rows
    triples.sort(key=lambda t: (-t[1], t[0].lower()))
    return [TopRow(label=l, primary_metric=p, secondary_metric=s)
            for (l, p, s) in triples[:top_n]]


def build_executive_summary(
    tenant: Tenant,
    *,
    now: datetime | None = None,
    window_days: int = 7,
) -> ExecutiveSummary:
    """Build one tenant's weekly executive summary.

    `now` defaults to wall-clock UTC; pass it explicitly in tests to
    pin the window deterministically.
    """
    now = now or datetime.now(_tz.utc)
    start, end = _window(now, days=window_days)
    prev_start, prev_end = (start - timedelta(days=window_days), start)

    base_qs = Recap.objects.filter(
        event__tenant=tenant,
        created_at__gte=start,
        created_at__lt=end,
    ).select_related(
        "event",
        "event__retailer",
        "ambassador",
        "ambassador__user",
    ).prefetch_related("consumer_engagements", "product_samples")

    recap_count = base_qs.count()

    # Headline reach: sum total_consumer across first-engagement rows
    # per recap. Doing this via a Sum() join is cleaner than a Python
    # loop but the join double-counts when a recap has multiple
    # consumer_engagements rows. In practice recaps have exactly one
    # row; we use that path for now and revisit if data shape changes.
    consumer_reach = (
        base_qs.aggregate(
            total=Sum("consumer_engagements__total_consumer")
        )["total"]
        or 0
    )
    samples_distributed = (
        base_qs.aggregate(total=Sum("product_samples__quantity"))["total"]
        or 0
    )

    # Top stores by consumer reach (event.retailer.name → reach).
    store_qs = (
        base_qs.values("event__retailer__name")
        .annotate(
            reach=Sum("consumer_engagements__total_consumer"),
            samplings=Count("id", distinct=True),
        )
        .order_by("-reach", "event__retailer__name")
    )
    top_stores = _ranked(
        (
            (
                row["event__retailer__name"] or "(unknown store)",
                int(row["reach"] or 0),
                f"{row['samplings']} sampling{'s' if row['samplings'] != 1 else ''}",
            )
            for row in store_qs[:10]  # over-fetch so _ranked can sort
        ),
        top_n=3,
    )

    # Top BAs by recaps filed.
    ba_qs = (
        base_qs.values(
            "ambassador__id",
            "ambassador__user__first_name",
            "ambassador__user__last_name",
            "ambassador__user__email",
        )
        .annotate(recaps_filed=Count("id"))
        .order_by("-recaps_filed")
    )
    top_bas = _ranked(
        (
            (
                (
                    " ".join(
                        filter(
                            None,
                            [
                                row.get("ambassador__user__first_name"),
                                row.get("ambassador__user__last_name"),
                            ],
                        )
                    ).strip()
                    or row.get("ambassador__user__email")
                    or "(unassigned)"
                ),
                int(row["recaps_filed"] or 0),
                None,
            )
            for row in ba_qs[:10]
        ),
        top_n=3,
    )

    # Week-over-week deltas. Skip if the prior window is empty —
    # "↑ ∞%" is not a useful chip.
    prev_count = Recap.objects.filter(
        event__tenant=tenant,
        created_at__gte=prev_start,
        created_at__lt=prev_end,
    ).count()
    prev_reach = (
        Recap.objects.filter(
            event__tenant=tenant,
            created_at__gte=prev_start,
            created_at__lt=prev_end,
        ).aggregate(total=Sum("consumer_engagements__total_consumer"))["total"]
        or 0
    )

    return ExecutiveSummary(
        tenant_id=tenant.id,
        tenant_name=tenant.name,
        period_label=_format_period(start, end),
        recap_count=recap_count,
        consumer_reach=int(consumer_reach),
        samples_distributed=int(samples_distributed),
        top_stores=top_stores,
        top_bas=top_bas,
        recap_count_delta=(
            (recap_count - prev_count) if prev_count > 0 else None
        ),
        consumer_reach_delta=(
            (int(consumer_reach) - int(prev_reach))
            if prev_reach > 0
            else None
        ),
    )
