"""
Executive summary aggregation — weekly top-line stats for a tenant.

Pairs with the daily admin digest (`digest/services.py`). The daily
digest is action-oriented ("you have 5 pending approvals"); this one
is read-oriented ("here's what the team did last week").

What goes in:
  - Total recaps filed in the window
  - Total consumers reached
  - Total product samples distributed
  - Top 3 stores by consumer reach
  - Top 3 BAs by recaps filed
  - Week-over-week deltas on the two headline numbers (recaps + reach)

Both recap families count. A tenant on a custom recap template files
``CustomRecap`` rows and no legacy ``Recap`` rows at all; reading only the
legacy table reported such a tenant's week as zero recaps, and the email
was skipped as empty.

  - "Filed" is ``recaps.filed`` — an empty clock-out stub is not a recap.
  - Archived recaps, recaps on a soft-deleted request, and custom recaps
    flagged ``exclude_from_aggregates`` are left out, as on the dashboard.
  - Consumers reached = legacy ``total_consumer`` + the custom recap's
    "consumers sampled" answer read through the SAME shared matcher the
    dashboard and the recap list use (``_consumers_sampled_from_fields``),
    so this email cannot disagree with them. A custom template that asks
    no such question adds its recaps to the count and nothing to reach.
  - Samples = legacy product samples + custom recap product samples.

The aggregation runs sync (Django ORM) — cheap for typical tenant
sizes (<10k recaps/week). If a tenant grows past that we can move
to background prefetch.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone as _tz
from typing import Iterable

from django.db.models import Count, Sum

from recaps.filed import custom_filed_q, legacy_filed_q
from recaps.models import (
    CustomFieldValue,
    CustomRecap,
    CustomRecapProductSample,
    Recap,
)
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

    # Django templates cannot call a method with an argument, so the
    # template reads these two properties. Writing
    # ``summary.delta_chip:"recaps"`` in the template is a
    # TemplateSyntaxError at compile time -- it crashed every send.
    @property
    def recap_delta_chip(self) -> str | None:
        return self.delta_chip("recaps")

    @property
    def reach_delta_chip(self) -> str | None:
        return self.delta_chip("reach")

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


def _legacy_recaps(tenant: Tenant, start: datetime, end: datetime):
    """Filed, live legacy recaps created in [start, end)."""
    return (
        Recap.objects.filter(
            event__tenant=tenant,
            created_at__gte=start,
            created_at__lt=end,
            archived_at__isnull=True,
        )
        .filter(legacy_filed_q())
        .exclude(event__request__deleted_at__isnull=False)
    )


def _custom_recaps(tenant: Tenant, start: datetime, end: datetime):
    """Filed, live, aggregate-eligible custom recaps created in [start, end)."""
    return (
        CustomRecap.objects.filter(
            event__tenant=tenant,
            created_at__gte=start,
            created_at__lt=end,
            archived_at__isnull=True,
            exclude_from_aggregates=False,
        )
        .filter(custom_filed_q())
        .exclude(event__request__deleted_at__isnull=False)
    )


def _custom_reach_by_recap(recap_ids: list[int]) -> dict[int, int]:
    """Consumers sampled per custom recap, via the shared matcher."""
    # Local import: recaps.types pulls in the GraphQL layer.
    from recaps.types import _consumers_sampled_from_fields

    by_recap: dict[int, list[tuple[str | None, str | None]]] = {}
    from recaps import metric_mapping

    rows = list(
        CustomFieldValue.objects.filter(custom_recap_id__in=recap_ids)
        .order_by("custom_field__order", "custom_field_id", "id")
        .values_list("custom_recap_id", "custom_field_id", "custom_field__name", "value")
    )
    # Names carry staff's confirmed metric when there is one.
    confirmed = metric_mapping.confirmed_metrics(field_ids={row[1] for row in rows})
    for recap_id, field_id, name, value in rows:
        by_recap.setdefault(recap_id, []).append(
            (metric_mapping.tag_name(name, field_id, confirmed), value)
        )
    out: dict[int, int] = {}
    for recap_id, pairs in by_recap.items():
        reach = _consumers_sampled_from_fields(pairs)
        if reach is not None:
            out[recap_id] = int(reach)
    return out


def _ba_label(first: str | None, last: str | None, email: str | None) -> str:
    return " ".join(filter(None, [first, last])).strip() or email or "(unassigned)"


@dataclass
class _WindowStats:
    recap_count: int = 0
    consumer_reach: int = 0
    samples_distributed: int = 0
    # store name -> [reach, samplings]
    stores: dict[str, list[int]] = field(default_factory=dict)
    # ambassador id -> [label, recaps filed]
    bas: dict[int | None, list] = field(default_factory=dict)


def _window_stats(tenant: Tenant, start: datetime, end: datetime) -> _WindowStats:
    stats = _WindowStats()

    def add_store(name: str | None, reach: int, samplings: int) -> None:
        row = stats.stores.setdefault(name or "(unknown store)", [0, 0])
        row[0] += int(reach or 0)
        row[1] += int(samplings or 0)

    def add_ba(amb_id, first, last, email, filed: int) -> None:
        row = stats.bas.setdefault(amb_id, [_ba_label(first, last, email), 0])
        row[1] += int(filed or 0)

    # ---- legacy recaps -------------------------------------------------
    legacy = _legacy_recaps(tenant, start, end)
    stats.recap_count += legacy.count()
    # Reach: sum total_consumer across consumer_engagements rows. The join
    # would double-count a recap with several rows; in practice recaps
    # have exactly one.
    stats.consumer_reach += int(
        legacy.aggregate(total=Sum("consumer_engagements__total_consumer"))["total"]
        or 0
    )
    stats.samples_distributed += int(
        legacy.aggregate(total=Sum("product_samples__quantity"))["total"] or 0
    )
    for row in legacy.values("event__retailer__name").annotate(
        reach=Sum("consumer_engagements__total_consumer"),
        samplings=Count("id", distinct=True),
    ):
        add_store(row["event__retailer__name"], row["reach"], row["samplings"])
    for row in legacy.values(
        "ambassador__id",
        "ambassador__user__first_name",
        "ambassador__user__last_name",
        "ambassador__user__email",
    ).annotate(recaps_filed=Count("id", distinct=True)):
        add_ba(
            row["ambassador__id"],
            row["ambassador__user__first_name"],
            row["ambassador__user__last_name"],
            row["ambassador__user__email"],
            row["recaps_filed"],
        )

    # ---- custom recaps -------------------------------------------------
    custom_rows = list(
        _custom_recaps(tenant, start, end).values_list(
            "id",
            "event__retailer__name",
            "retailer__name",
            "ambassador_id",
            "ambassador__user__first_name",
            "ambassador__user__last_name",
            "ambassador__user__email",
        )
    )
    if custom_rows:
        custom_ids = [row[0] for row in custom_rows]
        reach_by_recap = _custom_reach_by_recap(custom_ids)
        stats.recap_count += len(custom_rows)
        stats.consumer_reach += sum(reach_by_recap.values())
        stats.samples_distributed += int(
            CustomRecapProductSample.objects.filter(
                custom_recap_id__in=custom_ids
            ).aggregate(total=Sum("quantity"))["total"]
            or 0
        )
        for recap_id, event_store, own_store, amb_id, first, last, email in custom_rows:
            add_store(event_store or own_store, reach_by_recap.get(recap_id, 0), 1)
            add_ba(amb_id, first, last, email, 1)

    return stats


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

    stats = _window_stats(tenant, start, end)
    prev = _window_stats(tenant, prev_start, prev_end)

    # Top stores by consumer reach.
    top_stores = _ranked(
        (
            (
                name,
                reach,
                f"{samplings} sampling{'s' if samplings != 1 else ''}",
            )
            for name, (reach, samplings) in stats.stores.items()
        ),
        top_n=3,
    )
    # Top BAs by recaps filed.
    top_bas = _ranked(
        ((label, filed, None) for label, filed in stats.bas.values()),
        top_n=3,
    )

    # Week-over-week deltas. Skip if the prior window is empty —
    # "↑ ∞%" is not a useful chip.
    return ExecutiveSummary(
        tenant_id=tenant.id,
        tenant_name=tenant.name,
        period_label=_format_period(start, end),
        recap_count=stats.recap_count,
        consumer_reach=stats.consumer_reach,
        samples_distributed=stats.samples_distributed,
        top_stores=top_stores,
        top_bas=top_bas,
        recap_count_delta=(
            (stats.recap_count - prev.recap_count) if prev.recap_count > 0 else None
        ),
        consumer_reach_delta=(
            (stats.consumer_reach - prev.consumer_reach)
            if prev.consumer_reach > 0
            else None
        ),
    )
