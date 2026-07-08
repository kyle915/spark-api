"""Tenant-wide activity overview for the freeform Q&A feature.

This is the client-level sibling of :mod:`recaps.report_service` (which
rolls up ONE :class:`events.models.Request` into a campaign report). Here
we summarize a *whole tenant's* program — every campaign, every event,
every recap — into a single compact plaintext block that
:func:`recaps.report_types.tenant_ai_answer` hands to Gemini as the only
source of truth for the model's answer.

Design rules:

* **Efficient ORM aggregation, never load every recap into Python.** The
  headline counts and the summable KPIs come from ``Count`` / ``Sum``
  annotations evaluated in the database. A tenant with 50k recaps does
  NOT pull 50k rows into the request process. The only rows we ever
  materialize are the small, hard-capped tails: the ten most recent
  events and (where they can't be summed in SQL) the KPI-relevant
  custom-field VALUE rows + the recent consumer quotes.
* **Mirror the per-campaign KPI math.** The same nine KPIs
  ``report_service`` sums per campaign (consumers_reached,
  samples_distributed, products_sold, cans_sold, packs_sold,
  total_engagements, first_time_consumers, brand_aware_consumers,
  willing_to_purchase) are summed here across BOTH recap shapes — legacy
  :class:`recaps.models.Recap` (typed columns + the consumer/sample
  children) and custom-template :class:`recaps.models.CustomRecap` (one
  typed column + free-text ``CustomFieldValue`` rows, label-matched with
  the exact rules ``report_service`` / ``recaps.types`` use) — so the
  tenant block agrees with the campaign reports.
* **Bounded output regardless of tenant size.** Counts and sums are O(1)
  lines. The recent-events and quotes sections are hard-capped
  (:data:`MAX_RECENT_EVENTS` / :data:`MAX_RECENT_QUOTES`), and each quote
  is length-trimmed, so the whole block stays a few dozen lines and the
  Gemini prompt stays small whether the tenant ran 3 events or 30,000.

Everything here is synchronous Django ORM — the GraphQL resolver wraps the
single entry point :func:`build_tenant_overview` in ``sync_to_async``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, fields as dataclass_fields
from datetime import date, timedelta

from django.db.models import Count, Max, Min, Sum
from django.db.models.functions import Coalesce, TruncMonth
from django.utils import timezone

from events.models import Event, Request
from recaps.models import (
    ConsumerEngagements,
    ConsumerFeedback,
    CustomFieldValue,
    CustomRecap,
    CustomRecapProductSample,
    ProductSamples,
    Recap,
)
from recaps.report_service import _format_date_range, _leading_int
from recaps.types import (
    _consumers_sampled_from_fields,
    _samples_given_from_fields,
    _sold_units_from_fields,
)
from tenants.models import Tenant

# Hard caps so the prompt stays small no matter how big the tenant is.
MAX_RECENT_EVENTS = 10
MAX_RECENT_QUOTES = 10

# Trim any single quote so one rambling note can't dominate the block.
MAX_QUOTE_CHARS = 240

# How many trailing calendar months the monthly trend covers. Bounding the
# window keeps the structured KPI query's GROUP BY result to <=12 rows
# regardless of how long the tenant has been active.
MONTHLY_TREND_MONTHS = 12


@dataclass(frozen=True)
class TenantKpiTotals:
    """The nine summable per-tenant KPIs — the single source of truth.

    Identical field set to the per-campaign ``CampaignReportKpis`` KPI
    block. Produced once by :func:`tenant_kpi_totals` (legacy ``Recap`` +
    custom ``CustomRecap``, summed field-by-field) and consumed by BOTH the
    plaintext :func:`build_tenant_overview` (the text Q&A) and the
    structured ``tenantKpis`` GraphQL resolver, so the two can never drift.
    """

    consumers_reached: int = 0
    samples_distributed: int = 0
    products_sold: int = 0
    cans_sold: int = 0
    packs_sold: int = 0
    total_engagements: int = 0
    first_time_consumers: int = 0
    brand_aware_consumers: int = 0
    willing_to_purchase: int = 0


@dataclass(frozen=True)
class TenantKpiMonth:
    """One calendar month of a tenant's activity for the trend chart.

    ``month`` is ``"YYYY-MM"``. ``recaps`` / ``engagements`` / ``samples``
    are database aggregates over that month (see :func:`tenant_monthly_trend`).
    """

    month: str
    recaps: int = 0
    engagements: int = 0
    samples: int = 0


def _sum(queryset, field: str) -> int:
    """Database-side ``Sum`` of one nullable integer column, coerced to int.

    Returns 0 for an empty queryset / all-null column (``Sum`` yields
    ``None`` there). The aggregation runs in Postgres — the rows never
    enter Python.
    """
    total = queryset.aggregate(_t=Sum(field))["_t"]
    return int(total or 0)


def _year_bounds(year: int) -> tuple:
    """Half-open, timezone-aware ``[start, end)`` for calendar ``year``.

    ``start`` is Jan 1 00:00 of ``year`` and ``end`` is Jan 1 00:00 of the
    NEXT year, both in the active timezone (built off ``timezone.now()`` so
    they carry the same tzinfo the rest of this module uses). Half-open so a
    ``created_at`` exactly at the year boundary lands in exactly one year
    (``__gte start`` / ``__lt end``).
    """
    anchor = timezone.now().replace(
        month=1, day=1, hour=0, minute=0, second=0, microsecond=0
    )
    start = anchor.replace(year=year)
    end = anchor.replace(year=year + 1)
    return start, end


def _filter_window(queryset, date_field: str, window: tuple | None):
    """Narrow ``queryset`` to ``date_field`` within a half-open ``window``.

    ``window`` is a ``(start, end)`` pair of tz-aware datetimes (the shape
    :func:`_year_bounds` returns) applied as ``date_field__gte=start`` /
    ``date_field__lt=end`` — the SAME field/lookup convention
    :func:`_filter_year` has always used, factored out so the period
    comparison can window by an arbitrary ``[start, end)`` (a month,
    quarter, or year) and still reconcile exactly with
    :func:`tenant_kpi_totals`. ``window=None`` returns the queryset
    UNTOUCHED — the all-time path that keeps the byte-for-byte SQL the
    Ask-AI / Insights callers depend on.
    """
    if window is None:
        return queryset
    start, end = window
    return queryset.filter(
        **{f"{date_field}__gte": start, f"{date_field}__lt": end}
    )


def _filter_year(queryset, date_field: str, year: int | None):
    """Narrow ``queryset`` to ``date_field`` within calendar ``year``.

    ``year=None`` is the all-time path: the queryset is returned UNTOUCHED
    (no extra ``WHERE`` clause), so callers that pass no year keep their
    exact previous SQL — this is what preserves the byte-for-byte all-time
    behavior the Ask-AI and Insights callers depend on. When a year is
    given, apply the half-open :func:`_year_bounds` window on ``date_field``.

    A thin wrapper over :func:`_filter_window`: a year is just the
    :func:`_year_bounds` ``[start, end)`` window, so the period comparison
    (which windows by month/quarter/year) and the year filter share one
    field/lookup code path and can never drift.
    """
    window = None if year is None else _year_bounds(year)
    return _filter_window(queryset, date_field, window)


def _event_date_expr(prefix: str):
    """The effective EVENT datetime used to window a row, as a DB expression.

    Coalesces the event's own ``date``, then its ``start_time``, then its
    request's ``date`` — the SAME three fields, in the same priority, the
    Event Dashboard hero windows on (``tenants/dashboard/queries.py``). Using
    it here puts the tenant-KPI rollup on the same basis as the hero and the
    per-campaign reports: a row counts toward the period its EVENT happened
    in, not the period its recap row was created/imported in (``created_at``,
    which drifts for backfilled data and made "this year" read lower than a
    30-day window).

    ``prefix`` is the ORM path from the row to its Event:
      * ``""``                       on Event itself
      * ``"event__"``                on Recap / CustomRecap
      * ``"recap__event__"``         on ConsumerEngagements / ProductSamples
      * ``"custom_recap__event__"``  on CustomRecap children / CustomFieldValue
    """
    return Coalesce(
        f"{prefix}date", f"{prefix}start_time", f"{prefix}request__date"
    )


def _filter_event_window(queryset, prefix: str, window: tuple | None):
    """Narrow ``queryset`` to rows whose effective EVENT date is in ``window``.

    ``window`` is a half-open ``(start, end)`` pair of tz-aware datetimes (or
    ``(start, None)`` for an open-ended floor, which the trailing trend uses).
    ``window=None`` returns the queryset UNTOUCHED — the all-time path, so
    all-time totals are unchanged by the event-date basis; only WINDOWED
    (year / quarter / month / week) queries move off ``created_at``. Rows
    whose event has none of date/start_time/request.date are excluded (no
    date to place them in a period — same as the hero).
    """
    if window is None:
        return queryset
    start, end = window
    qs = queryset.annotate(_evtdate=_event_date_expr(prefix)).filter(
        _evtdate__gte=start
    )
    if end is not None:
        qs = qs.filter(_evtdate__lt=end)
    return qs


def _filter_event_year(queryset, prefix: str, year: int | None):
    """Narrow ``queryset`` to rows whose effective EVENT date is in ``year``.

    The event-date sibling of :func:`_filter_year`: a calendar year is just
    the :func:`_year_bounds` ``[start, end)`` window applied on the coalesced
    event date (:func:`_event_date_expr`) instead of ``created_at``.
    ``year=None`` returns the queryset UNTOUCHED (all-time), so all-time
    rollups keep their exact previous SQL — only windowed reads move onto the
    event-date basis, matching the hero + program KPIs (#839).
    """
    window = None if year is None else _year_bounds(year)
    return _filter_event_window(queryset, prefix, window)


def _legacy_kpis_window(tenant_id: int, window: tuple | None) -> dict[str, int]:
    """Sum the legacy :class:`recaps.models.Recap` KPIs over a ``[start, end)``.

    The window-based core of :func:`_legacy_kpis`. Recap has no direct
    tenant FK, so every queryset is scoped through the event
    (``…__event__tenant_id`` / ``event__tenant_id``) — the same join
    ``tenants.insights`` and the recap lists use. Each line is a single
    aggregate query; no Recap row is loaded into Python.

    ``window`` is a ``(start, end)`` half-open pair applied per-source on the
    effective EVENT date (see :func:`_filter_event_window` — event date /
    start_time / request date, the same basis as the hero), or ``None`` for
    the all-time path that leaves every queryset untouched. The period
    comparison passes a month /
    quarter / year window here so its figures reconcile with
    :func:`tenant_kpi_totals` for the matching window.
    """
    recaps = _filter_event_window(
        Recap.objects.filter(event__tenant_id=tenant_id), "event__", window
    )
    engagements = _filter_event_window(
        ConsumerEngagements.objects.filter(recap__event__tenant_id=tenant_id),
        "recap__event__",
        window,
    )
    samples = _filter_event_window(
        ProductSamples.objects.filter(recap__event__tenant_id=tenant_id),
        "recap__event__",
        window,
    )
    consumers_reached = _sum(engagements, "total_consumer")
    return {
        "total_engagements": _sum(recaps, "total_engagements"),
        "products_sold": _sum(recaps, "products_sold"),
        "cans_sold": _sum(recaps, "total_cans_sold"),
        "packs_sold": _sum(recaps, "total_packs_sold"),
        "consumers_reached": consumers_reached,
        "first_time_consumers": _sum(engagements, "first_time_consumers"),
        "brand_aware_consumers": _sum(engagements, "brand_aware_consumers"),
        "willing_to_purchase": _sum(engagements, "willing_to_purchase_consumers"),
        # Samples distributed = consumers sampled (kyle's rule: one sample per
        # person sampled). Fall back to the structured ProductSamples quantity
        # only for a tenant that logs no consumer count.
        "samples_distributed": consumers_reached or _sum(samples, "quantity"),
    }


def _legacy_kpis(tenant_id: int, year: int | None = None) -> dict[str, int]:
    """Sum the legacy :class:`recaps.models.Recap` KPIs for one tenant/year.

    The calendar-year wrapper over :func:`_legacy_kpis_window`: ``year``
    becomes its :func:`_year_bounds` window (``year=None`` → no window,
    untouched all-time SQL), so the year filter and the period comparison
    share one aggregation code path and can never drift.
    """
    window = None if year is None else _year_bounds(year)
    return _legacy_kpis_window(tenant_id, window)


# Custom recaps keep most KPIs as free-text CustomFieldValue rows keyed by
# the field NAME. They can't be summed in SQL, so we pull ONLY the
# KPI-relevant value rows (filtered by a name regex in the DB) and parse
# them in Python — a bounded slice, not the full recap tree. The patterns
# mirror recaps.report_service._custom_engagement_totals +
# recaps.types._sold_units_from_fields / _consumers_sampled_from_fields.
_CUSTOM_KPI_NAME_RE = re.compile(
    r"consumers sampled|first time|knew about|willing to purchase"
    r"|cans?|packs?"
    # SOLD fallback vocabulary (mirror of recaps.types._SOLD_FALLBACK_RE):
    # non-drink tenants log sales as "...did consumers PURCHASE...",
    # "...bought...", or "...sold" with no cans/packs. This gate runs in SQL
    # BEFORE the per-recap matchers, so it MUST stay a SUPERSET of every
    # downstream matcher's vocabulary — otherwise rows are dropped before
    # _sold_units_from_fields sees them, which silently zeroed "Products Sold"
    # for Stone House Bread (bread). Intent rows ("willing to purchase") are
    # still fetched here but excluded from the sold total by _SOLD_EXCLUDE_RE.
    r"|sold|bought|purchase[ds]?"
    # Girl Beer vocabulary: demographics sampled totals + free-text
    # samples headline (see recaps.types._SAMPLED_TOTAL_RE/_SAMPLES_GIVEN_RE)
    r"|who sampled|samples? (given|distributed|handed)",
    re.IGNORECASE,
)


def _custom_kpis_window(tenant_id: int, window: tuple | None) -> dict[str, int]:
    """Sum the custom-template :class:`recaps.models.CustomRecap` KPIs over a window.

    The window-based core of :func:`_custom_kpis`. ``total_engagements`` is
    a typed column → summed in the DB. The four consumer metrics + sold
    units live in free-text ``CustomFieldValue`` rows; we fetch only the
    KPI-relevant rows (``custom_field__name`` matched by
    :data:`_CUSTOM_KPI_NAME_RE` in SQL), grouped per recap, and apply the
    same label/parse rules the per-campaign report uses so the totals agree.
    We never load a CustomRecap object — only the matched (recap_id, name,
    value) value rows.

    ``window`` is a ``(start, end)`` half-open pair applied per-source on the
    effective EVENT date (see :func:`_filter_event_window`; the custom value
    rows are windowed via ``custom_recap__event__``, consistent with the
    structured sums and the monthly trend), or ``None`` for the all-time path
    that leaves every queryset untouched. The period comparison passes a month
    / quarter / year window here so its figures reconcile with
    :func:`tenant_kpi_totals` for the matching window.
    """
    out = {
        "total_engagements": _sum(
            _filter_event_window(
                CustomRecap.objects.filter(tenant_id=tenant_id),
                "event__",
                window,
            ),
            "total_engagements",
        ),
        "consumers_reached": 0,
        "first_time_consumers": 0,
        "brand_aware_consumers": 0,
        "willing_to_purchase": 0,
        "products_sold": 0,
        "cans_sold": 0,
        "packs_sold": 0,
        "samples_distributed": 0,
    }

    # Structured custom samples sum cleanly in SQL.
    structured_samples = _sum(
        _filter_event_window(
            CustomRecapProductSample.objects.filter(
                custom_recap__tenant_id=tenant_id
            ),
            "custom_recap__event__",
            window,
        ),
        "quantity",
    )

    # Pull only the KPI-relevant free-text value rows, grouped by recap so
    # the per-recap "consumers sampled" fallback (sold units + samples)
    # matches the campaign report's per-recap accumulation.
    rows = (
        _filter_event_window(
            CustomFieldValue.objects.filter(
                custom_recap__tenant_id=tenant_id,
                custom_field__name__iregex=_CUSTOM_KPI_NAME_RE.pattern,
            ),
            "custom_recap__event__",
            window,
        )
        .values_list("custom_recap_id", "custom_field__name", "value")
        .order_by("custom_recap_id")
    )

    per_recap: dict[int, list[tuple[str | None, str | None]]] = {}
    for recap_id, name, value in rows.iterator():
        per_recap.setdefault(recap_id, []).append((name, value))

    sampled_total = 0
    samples_given_total = 0
    for pairs in per_recap.values():
        for name, value in pairs:
            label = (name or "").lower()
            val = _leading_int(value)
            if val is None:
                continue
            if "first time" in label:
                out["first_time_consumers"] += val
            elif "knew about" in label:
                out["brand_aware_consumers"] += val
            elif "willing to purchase" in label and "not" not in label:
                out["willing_to_purchase"] += val

        consumers_sampled = _consumers_sampled_from_fields(pairs)
        if consumers_sampled is not None:
            out["consumers_reached"] += int(consumers_sampled)
            sampled_total += int(consumers_sampled)

        samples_given = _samples_given_from_fields(pairs)
        if samples_given is not None:
            samples_given_total += int(samples_given)

        sold = _sold_units_from_fields(pairs)
        if sold is not None:
            out["products_sold"] += int(sold)
        for name, value in pairs:
            label = (name or "").lower()
            parsed = _leading_int(value)
            if parsed is None:
                continue
            if re.search(r"\bcans?\b", label):
                out["cans_sold"] += parsed
            elif re.search(r"\bpacks?\b", label):
                out["packs_sold"] += parsed

    # Samples distributed = consumers sampled (kyle's rule: one sample per
    # person sampled, fleet-wide). `out["consumers_reached"]` is the summed
    # "consumers sampled" headline; fall back to the explicit "samples given
    # out" free-text headline, then structured per-SKU quantities, only when a
    # template logs no consumers-sampled count.
    out["samples_distributed"] = (
        out["consumers_reached"] or samples_given_total or structured_samples
    )
    return out


def _custom_kpis(tenant_id: int, year: int | None = None) -> dict[str, int]:
    """Sum the custom :class:`recaps.models.CustomRecap` KPIs for one tenant/year.

    The calendar-year wrapper over :func:`_custom_kpis_window`: ``year``
    becomes its :func:`_year_bounds` window (``year=None`` → no window,
    untouched all-time SQL), so the year filter and the period comparison
    share one aggregation code path and can never drift.
    """
    window = None if year is None else _year_bounds(year)
    return _custom_kpis_window(tenant_id, window)


def _tenant_kpi_totals_window(
    tenant_id: int, window: tuple | None
) -> TenantKpiTotals:
    """Legacy + custom KPI totals over a ``[start, end)`` window, field-by-field.

    The window-based core of :func:`tenant_kpi_totals`: sums the legacy and
    custom KPIs over the same ``window`` and adds them field-by-field.
    ``window=None`` is the all-time roll-up. The period comparison calls
    this once per period; the public :func:`tenant_kpi_totals` is the
    calendar-year wrapper, so a comparison period and a year filter over the
    same span return identical totals.
    """
    legacy = _legacy_kpis_window(tenant_id, window)
    custom = _custom_kpis_window(tenant_id, window)
    return TenantKpiTotals(
        **{
            f.name: int(legacy.get(f.name, 0)) + int(custom.get(f.name, 0))
            for f in dataclass_fields(TenantKpiTotals)
        }
    )


def tenant_kpi_totals(tenant_id: int, year: int | None = None) -> TenantKpiTotals:
    """Legacy + custom KPI totals for one tenant, summed field-by-field.

    THE single source of truth for this tenant's nine summable KPIs. Both
    :func:`build_tenant_overview` (the plaintext Q&A block) and the
    structured ``tenantKpis`` GraphQL resolver call this, so the text and
    the chart numbers can never diverge. All sums are database aggregates
    (see :func:`_legacy_kpis` / :func:`_custom_kpis`); no recap row is
    loaded into Python beyond the bounded free-text custom value rows.

    ``year=None`` (the default, and what the Ask-AI overview + Insights
    callers pass) sums over ALL of the tenant's recaps unchanged. ``year=Y``
    restricts every underlying aggregate to recaps whose ``created_at`` falls
    in calendar year ``Y``.

    The calendar-year wrapper over :func:`_tenant_kpi_totals_window`, so the
    year filter and the period comparison share one aggregation code path.
    """
    window = None if year is None else _year_bounds(year)
    return _tenant_kpi_totals_window(tenant_id, window)


def _tenant_event_recap_counts_window(
    tenant_id: int, window: tuple | None
) -> tuple[int, int]:
    """``(event_count, recap_count)`` over a ``[start, end)`` window — both COUNTs.

    The window-based core of :func:`tenant_event_recap_counts`.
    ``recap_count`` unions BOTH shapes (legacy ``Recap`` joined through the
    event + custom ``CustomRecap`` via its direct tenant FK), exactly like
    the overview's headline. Each line is a single ``COUNT(*)``; no rows
    enter Python.

    ``window`` is a ``(start, end)`` half-open pair applied per-source on the
    effective EVENT date (see :func:`_filter_event_window` — events by their
    own date, each recap shape via ``event__`` — the same basis the totals
    and trend use), or ``None`` to leave every count unfiltered. The period
    comparison passes a month / quarter / year window here.
    """
    legacy_recaps = _filter_event_window(
        Recap.objects.filter(event__tenant_id=tenant_id), "event__", window
    )
    custom_recaps = _filter_event_window(
        CustomRecap.objects.filter(tenant_id=tenant_id), "event__", window
    )
    legacy_recap_count = legacy_recaps.count()
    custom_recap_count = custom_recaps.count()
    # "Events" = events that actually produced a recap (kyle: "we only did 6
    # events" = the 6 with recaps), NOT every scheduled event — a tenant whose
    # season was bulk-imported (Stone House Bread, 75 scheduled) should show
    # the events it ran, not its calendar. Distinct event ids across both recap
    # shapes; the id sets are small (bounded by recap count), so no recap row
    # is materialized.
    event_ids = set(
        legacy_recaps.values_list("event_id", flat=True)
    ) | set(custom_recaps.values_list("event_id", flat=True))
    return len(event_ids), legacy_recap_count + custom_recap_count


def tenant_event_recap_counts(
    tenant_id: int, year: int | None = None
) -> tuple[int, int]:
    """``(event_count, recap_count)`` for one tenant — both DB counts.

    Shared by :func:`build_tenant_overview` (the headline line) and the
    structured ``tenantKpis`` resolver so the ``events`` / ``recaps``
    figures match the text block. ``recap_count`` unions BOTH shapes
    (legacy ``Recap`` joined through the event + custom ``CustomRecap`` via
    its direct tenant FK), exactly like the overview's headline. Each line
    is a single ``COUNT(*)``; no rows enter Python.

    When ``year`` is given, each count is narrowed to its own ``created_at``
    within that calendar year (events by ``Event.created_at``, each recap
    shape by its own ``created_at`` — the same anchor the trend uses);
    ``year=None`` leaves every count unfiltered, so the all-time SQL is
    unchanged.

    The calendar-year wrapper over :func:`_tenant_event_recap_counts_window`,
    so the year filter and the period comparison share one count code path.
    """
    window = None if year is None else _year_bounds(year)
    return _tenant_event_recap_counts_window(tenant_id, window)


# ---------------------------------------------------------------------------
# Period-over-period comparison ("this period vs last").
#
# The dashboard's ``tenantKpis`` only scopes by calendar YEAR, so the
# frontend cannot build a month-over-month (or quarter-over-quarter) delta on
# its own across the full nine-KPI set. ``tenant_kpi_comparison`` returns BOTH
# the most-recent-COMPLETE period and the one immediately before it, each as a
# full KPI roll-up, leaving the % deltas to the frontend.
#
# CRITICAL — complete periods only. We deliberately NEVER use the in-progress
# current month / quarter / year: comparing a partial current period against a
# full prior one manufactures a misleading drop (the same "-100% halted"
# partial-period distortion the Momentum insight bucket already had to correct
# for). So "current" is always the most recent period that has fully ELAPSED,
# and "previous" is the complete period before that.
# ---------------------------------------------------------------------------

# The period granularities ``tenant_kpi_comparison`` accepts.
COMPARISON_PERIODS = ("month", "quarter", "year")

# Abbreviated month names for the human labels ("May 2026"), indexed 1-12.
# Hard-coded (not ``strftime``) so the label is locale-independent and stable
# across environments.
_MONTH_ABBR = (
    "",
    "Jan",
    "Feb",
    "Mar",
    "Apr",
    "May",
    "Jun",
    "Jul",
    "Aug",
    "Sep",
    "Oct",
    "Nov",
    "Dec",
)


def _month_start(year: int, month: int):
    """Tz-aware midnight of the first day of ``year``-``month``.

    Built off ``timezone.now()`` so it carries the same tzinfo the rest of
    this module uses (matching :func:`_year_bounds`), then pinned to the
    given year/month. ``month`` is 1-12.
    """
    return timezone.now().replace(
        year=year,
        month=month,
        day=1,
        hour=0,
        minute=0,
        second=0,
        microsecond=0,
    )


def _add_months(year: int, month: int, delta: int) -> tuple:
    """Shift the ``(year, month)`` pair by ``delta`` whole months (1-12 month).

    Pure integer math (no dateutil), the same approach
    :func:`_trend_window_start` uses: collapse to a month ordinal, add the
    delta, expand back. ``delta`` may be negative.
    """
    total = (year * 12 + (month - 1)) + delta
    y, m = divmod(total, 12)
    return y, m + 1


def _month_comparison_windows() -> tuple:
    """The two COMPLETE-month windows + labels: ``(cur_window, cur_label, prev_window, prev_label)``.

    "Current" is the most recent FULLY-ELAPSED calendar month — i.e. the
    month BEFORE the one ``now`` falls in (the current calendar month is
    still in progress, so it is never used). "Previous" is the month before
    that. Each window is the half-open ``[first-of-month, first-of-next)``
    pair, identical in shape to :func:`_year_bounds`, so the totals reconcile
    with :func:`tenant_kpi_totals`. E.g. for a mid-June-2026 "now":
    current = May 2026, previous = Apr 2026.
    """
    now = timezone.now()
    # Most recent complete month = one month before the current (in-progress) one.
    cur_y, cur_m = _add_months(now.year, now.month, -1)
    prev_y, prev_m = _add_months(cur_y, cur_m, -1)

    cur_start = _month_start(cur_y, cur_m)
    cur_end = _month_start(*_add_months(cur_y, cur_m, 1))
    prev_start = _month_start(prev_y, prev_m)
    prev_end = cur_start  # previous month ends where the current one begins

    cur_label = f"{_MONTH_ABBR[cur_m]} {cur_y}"
    prev_label = f"{_MONTH_ABBR[prev_m]} {prev_y}"
    return (cur_start, cur_end), cur_label, (prev_start, prev_end), prev_label


def _quarter_comparison_windows() -> tuple:
    """The two COMPLETE-quarter windows + labels.

    Quarters are Q1=Jan-Mar, Q2=Apr-Jun, Q3=Jul-Sep, Q4=Oct-Dec. "Current"
    is the most recent FULLY-ELAPSED quarter — the quarter BEFORE the one
    ``now`` falls in (the in-progress quarter is never used) — and "previous"
    is the quarter before that (rolling back across the year boundary as
    needed). Each window is the half-open ``[first-of-quarter,
    first-of-next-quarter)`` pair, so the totals reconcile with
    :func:`tenant_kpi_totals`. E.g. for a "now" in Q2 2026 (Apr-Jun):
    current = Q1 2026, previous = Q4 2025.
    """
    now = timezone.now()
    current_q = (now.month - 1) // 3  # 0-based index of the in-progress quarter
    # Most recent complete quarter = the one before the in-progress quarter.
    # Work in a 0-based "quarter ordinal" (year*4 + quarter_index) so the
    # year rollover is plain integer math.
    cur_ord = (now.year * 4 + current_q) - 1
    prev_ord = cur_ord - 1

    def _q_window_and_label(ordinal: int) -> tuple:
        y, q = divmod(ordinal, 4)  # q is 0-based (0..3)
        start_month = q * 3 + 1
        start = _month_start(y, start_month)
        end = _month_start(*_add_months(y, start_month, 3))
        return (start, end), f"Q{q + 1} {y}"

    cur_window, cur_label = _q_window_and_label(cur_ord)
    prev_window, prev_label = _q_window_and_label(prev_ord)
    return cur_window, cur_label, prev_window, prev_label


def _year_comparison_windows() -> tuple:
    """The two COMPLETE-year windows + labels.

    "Current" is the most recent FULLY-ELAPSED calendar year — i.e. LAST
    year, since the current calendar year is still in progress and is never
    used — and "previous" is the year before that. Each window is the
    half-open :func:`_year_bounds` pair, so the totals reconcile with
    ``tenant_kpi_totals(year=...)`` for the matching year. E.g. for a 2026
    "now": current = 2025, previous = 2024.
    """
    now = timezone.now()
    cur_year = now.year - 1
    prev_year = cur_year - 1
    return (
        _year_bounds(cur_year),
        str(cur_year),
        _year_bounds(prev_year),
        str(prev_year),
    )


# Dispatch table: granularity -> the function returning its two complete
# windows + labels. ``tenant_kpi_comparison`` looks the period up here.
_COMPARISON_WINDOW_BUILDERS = {
    "month": _month_comparison_windows,
    "quarter": _quarter_comparison_windows,
    "year": _year_comparison_windows,
}


def _period_totals(tenant_id: int, window: tuple) -> dict:
    """The full KPI roll-up for one period window as a plain dict.

    Combines the event/recap COUNTs and the nine summable KPIs over the SAME
    ``window``, reusing the window-based cores
    (:func:`_tenant_event_recap_counts_window` /
    :func:`_tenant_kpi_totals_window`) so a period's figures reconcile with
    :func:`tenant_kpi_totals` for a matching span. Returns the eleven keys
    the ``TenantKpiTotals`` GraphQL type carries (``events`` / ``recaps`` +
    the nine KPI fields).
    """
    event_count, recap_count = _tenant_event_recap_counts_window(tenant_id, window)
    totals = _tenant_kpi_totals_window(tenant_id, window)
    out = {"events": event_count, "recaps": recap_count}
    for f in dataclass_fields(TenantKpiTotals):
        out[f.name] = int(getattr(totals, f.name, 0) or 0)
    return out


def tenant_kpi_comparison(tenant_id: int, period: str = "month") -> dict:
    """"This period vs last" KPI deltas for one tenant — both periods, full set.

    Picks the most recent COMPLETE period of the requested granularity and
    the complete period immediately before it, then computes the FULL KPI
    roll-up (event/recap counts + the nine summable KPIs, the same set as
    :func:`tenant_kpi_totals`) for BOTH, scoped to each period's half-open
    ``created_at`` window. The frontend computes the % deltas from the two.

    ``period`` is one of :data:`COMPARISON_PERIODS` (``"month"`` /
    ``"quarter"`` / ``"year"``); anything else falls back to ``"month"``.

    COMPLETE periods only — see the module comment above: "current" is the
    most recent period that has fully ELAPSED (never the in-progress current
    month/quarter/year, which would manufacture a false drop), and
    "previous" is the complete period before it. For a mid-June-2026 "now":

    * ``month``   → current "May 2026", previous "Apr 2026"
    * ``quarter`` → current "Q1 2026", previous "Q4 2025"
    * ``year``    → current "2025", previous "2024"

    Returns a plain dict (no framework types) the resolver maps onto
    ``TenantKpiComparison``::

        {
            "period": "month",
            "current_label": "May 2026",
            "previous_label": "Apr 2026",
            "current":  {<events, recaps, + nine KPIs>},
            "previous": {<events, recaps, + nine KPIs>},
        }

    All figures are database aggregates over the bounded windows (no recap
    row materialized beyond the small free-text custom value rows
    :func:`_custom_kpis_window` already pulls), so the cost is independent of
    tenant size.
    """
    if period not in _COMPARISON_WINDOW_BUILDERS:
        period = "month"
    builder = _COMPARISON_WINDOW_BUILDERS[period]
    cur_window, cur_label, prev_window, prev_label = builder()
    return {
        "period": period,
        "current_label": cur_label,
        "previous_label": prev_label,
        "current": _period_totals(tenant_id, cur_window),
        "previous": _period_totals(tenant_id, prev_window),
    }


def _trend_window_start():
    """First day (midnight, tz-aware) of the oldest month in the trend.

    Anchored to *now* so the window is the current month plus the
    preceding ``MONTHLY_TREND_MONTHS - 1`` — e.g. with 12 and a May 2026
    "now", it starts at 2026-06-01 minus 12 months = 2025-06-01. We floor
    to the first of the month so ``TruncMonth`` buckets line up with the
    window edge.
    """
    now = timezone.now()
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    # Step back (MONTHLY_TREND_MONTHS - 1) whole months from this month's
    # first day, doing the year/month math by hand to avoid a dateutil dep.
    total = (month_start.year * 12 + (month_start.month - 1)) - (
        MONTHLY_TREND_MONTHS - 1
    )
    year, month = divmod(total, 12)
    return month_start.replace(year=year, month=month + 1)


def _month_key(value) -> str | None:
    """Format a ``TruncMonth`` bucket value as ``"YYYY-MM"`` (None if null)."""
    if value is None:
        return None
    return f"{value.year:04d}-{value.month:02d}"


def _bucket_counts(
    queryset, date_field: str, *, count: bool = False, sum_field: str | None = None
) -> dict[str, int]:
    """Group ``queryset`` by ``TruncMonth(date_field)`` and aggregate per month.

    Returns ``{"YYYY-MM": value}``. Either counts rows (``count=True``) or
    sums ``sum_field``. Pure database aggregation: the GROUP BY runs in the
    DB and only the (<=12, after windowing) month buckets come back.
    """
    if count:
        agg = Count("id")
    else:
        agg = Sum(sum_field)
    rows = (
        queryset.annotate(_m=TruncMonth(date_field))
        .values("_m")
        .annotate(_v=agg)
        .values_list("_m", "_v")
    )
    out: dict[str, int] = {}
    for bucket, value in rows:
        key = _month_key(bucket)
        if key is None:
            continue
        out[key] = out.get(key, 0) + int(value or 0)
    return out


def _trend_window(year: int | None):
    """``(start, end, num_months)`` describing the trend's month window.

    * ``year=None`` — the trailing window: ``start`` is
      :func:`_trend_window_start` (this month minus
      ``MONTHLY_TREND_MONTHS - 1``), ``end`` is ``None`` (no upper bound,
      so the SQL stays exactly ``created_at >= start``), and ``num_months``
      is :data:`MONTHLY_TREND_MONTHS`.
    * ``year=Y`` — a calendar-year window: ``start`` / ``end`` are the
      half-open :func:`_year_bounds` of ``Y``. A PAST year spans all twelve
      months (Jan→Dec); the CURRENT year stops at the current month
      (Jan→this month, no future padding); a FUTURE year yields zero months.
    """
    if year is None:
        return _trend_window_start(), None, MONTHLY_TREND_MONTHS

    start, end = _year_bounds(year)
    now = timezone.now()
    if year < now.year:
        num_months = 12
    elif year == now.year:
        num_months = now.month
    else:
        # Future year: nothing to chart yet.
        num_months = 0
    return start, end, num_months


def tenant_monthly_trend(
    tenant_id: int, year: int | None = None
) -> list[TenantKpiMonth]:
    """Calendar months of a tenant's activity, oldest → newest, zero-filled.

    One :class:`TenantKpiMonth` per month in the window (months with no
    activity are zero-filled, so the series is always a bounded, contiguous
    set the frontend can chart directly). The window depends on ``year``
    (see :func:`_trend_window`):

    * ``year=None`` — the last :data:`MONTHLY_TREND_MONTHS` calendar months
      (the trailing window), UNCHANGED from the original behavior.
    * ``year=Y`` — the months of calendar year ``Y``: Jan→Dec for a past
      year, Jan→current month for the current year (future months are NOT
      padded), and an empty series for a future year.

    Each metric is a database ``TruncMonth`` + ``Count``/``Sum`` over BOTH
    recap shapes, anchored to the recap's ``created_at`` (the non-null
    timestamp both shapes share and that the recap lists order by):

    * ``recaps``      — COUNT of legacy ``Recap`` + custom ``CustomRecap``.
    * ``engagements`` — SUM of the typed ``total_engagements`` column on
      each shape (mirrors :func:`tenant_kpi_totals`' engagement math).
    * ``samples``     — SUM of the STRUCTURED sample quantities
      (``ProductSamples`` + ``CustomRecapProductSample``). Unlike the
      headline ``samples_distributed`` in :func:`tenant_kpi_totals`, this
      deliberately omits the custom free-text "consumers sampled" fallback:
      that value lives in per-recap ``CustomFieldValue`` rows that can't be
      bucketed by month in pure SQL, and loading them per row would break
      the "never materialize every recap" rule. The monthly series is an
      activity *shape* for charting, not the authoritative grand total — so
      it stays SQL-only and bounded; the headline KPIs remain exact.

    All five querysets are scoped to ``tenant_id`` and floored to the window
    so the GROUP BY never returns more than the window's months.
    """
    start, end, num_months = _trend_window(year)

    def _windowed(queryset, prefix):
        """Floor to ``start`` (cap at ``end`` for a year window) on the
        effective EVENT date, annotating ``_evtdate`` so the per-month
        ``TruncMonth`` buckets group by event date too — the same basis the
        headline totals and the hero use. Leaving the upper bound off for the
        trailing (``year=None``) window keeps it open-ended.
        """
        queryset = queryset.annotate(_evtdate=_event_date_expr(prefix))
        queryset = queryset.filter(_evtdate__gte=start)
        if end is not None:
            queryset = queryset.filter(_evtdate__lt=end)
        return queryset

    legacy_recaps = _windowed(
        Recap.objects.filter(event__tenant_id=tenant_id), "event__"
    )
    custom_recaps = _windowed(
        CustomRecap.objects.filter(tenant_id=tenant_id), "event__"
    )
    legacy_samples = _windowed(
        ProductSamples.objects.filter(recap__event__tenant_id=tenant_id),
        "recap__event__",
    )
    custom_samples = _windowed(
        CustomRecapProductSample.objects.filter(custom_recap__tenant_id=tenant_id),
        "custom_recap__event__",
    )

    recap_counts: dict[str, int] = {}
    for src in (legacy_recaps, custom_recaps):
        for key, val in _bucket_counts(src, "_evtdate", count=True).items():
            recap_counts[key] = recap_counts.get(key, 0) + val

    engagement_counts: dict[str, int] = {}
    for src in (legacy_recaps, custom_recaps):
        for key, val in _bucket_counts(
            src, "_evtdate", sum_field="total_engagements"
        ).items():
            engagement_counts[key] = engagement_counts.get(key, 0) + val

    sample_counts: dict[str, int] = {}
    for src in (legacy_samples, custom_samples):
        for key, val in _bucket_counts(
            src, "_evtdate", sum_field="quantity"
        ).items():
            sample_counts[key] = sample_counts.get(key, 0) + val

    # Zero-fill every month in the window, oldest -> newest, so the series
    # is contiguous and bounded regardless of which months had activity.
    months: list[TenantKpiMonth] = []
    year_cursor, month = start.year, start.month
    for _ in range(num_months):
        key = f"{year_cursor:04d}-{month:02d}"
        months.append(
            TenantKpiMonth(
                month=key,
                recaps=recap_counts.get(key, 0),
                engagements=engagement_counts.get(key, 0),
                samples=sample_counts.get(key, 0),
            )
        )
        month += 1
        if month > 12:
            month = 1
            year_cursor += 1
    return months


# Field path from each recap shape to its event's 2-letter US state code.
# Events carry a dedicated ``state`` FK (``events.models.Event.state`` -> a
# ``State`` whose ``code`` is the "CA"-style abbreviation the Master
# Tracker's MARKET column and ``_recent_event_lines`` use). Both recap
# shapes reach it through their (non-null) ``event`` FK, so grouping by this
# path is a single join and yields the code the frontend map keys on.
_LEGACY_STATE_PATH = "event__state__code"
_CUSTOM_STATE_PATH = "event__state__code"

# The six summable per-state KPIs. ``event_count`` / ``recap_count`` are
# COUNTs handled separately; these are SUMs that mirror the matching
# ``TenantKpiTotals`` fields (same DB columns / parse rules as
# :func:`tenant_kpi_totals`) so a state's row agrees with the tenant total
# when summed across states.
_MARKET_KPI_FIELDS = (
    "consumers_reached",
    "samples_distributed",
    "products_sold",
    "total_engagements",
)


def _blank_market_row(state: str) -> dict:
    """A zeroed per-state row keyed by the 2-letter ``state`` code."""
    row = {
        "state": state,
        "event_count": 0,
        "recap_count": 0,
    }
    for field in _MARKET_KPI_FIELDS:
        row[field] = 0
    return row


def _grouped_sum(queryset, group_path: str, sum_field: str) -> dict[str, int]:
    """``{state_code: SUM(sum_field)}`` grouping ``queryset`` by ``group_path``.

    A single ``.values(group_path).annotate(Sum(...))`` — the GROUP BY runs
    in the database and only the (<= ~50) per-state buckets come back; no row
    is loaded into Python. Rows whose state code is null/blank are dropped so
    they never become a map bucket.
    """
    rows = (
        queryset.values(group_path)
        .annotate(_v=Sum(sum_field))
        .values_list(group_path, "_v")
    )
    out: dict[str, int] = {}
    for code, value in rows:
        if not code:
            continue
        out[code] = out.get(code, 0) + int(value or 0)
    return out


def _grouped_count(queryset, group_path: str) -> dict[str, int]:
    """``{state_code: COUNT(*)}`` grouping ``queryset`` by ``group_path``.

    Database-side ``GROUP BY`` + ``COUNT`` (see :func:`_grouped_sum`); blank
    state codes are skipped.
    """
    rows = (
        queryset.values(group_path)
        .annotate(_v=Count("id"))
        .values_list(group_path, "_v")
    )
    out: dict[str, int] = {}
    for code, value in rows:
        if not code:
            continue
        out[code] = out.get(code, 0) + int(value or 0)
    return out


def _legacy_market_kpis(tenant_id: int, year: int | None = None) -> dict[str, dict]:
    """Per-state legacy :class:`recaps.models.Recap` KPIs for one tenant.

    The per-state sibling of :func:`_legacy_kpis`: the identical KPI columns
    (``total_engagements`` / ``products_sold`` off ``Recap``,
    ``consumers_reached`` off ``ConsumerEngagements``, ``samples_distributed``
    off ``ProductSamples``) summed in the DB but GROUPED BY the recap's
    ``event``'s state code instead of rolled into one tenant total. Every
    queryset is scoped through the event (``…__event__tenant_id``) and
    year-filtered on the EVENT date (:func:`_filter_event_year`, same basis
    as the hero + :func:`_legacy_kpis`), so a state's row counts the period
    its events happened in — not when the recap rows were created/imported;
    ``year=None`` leaves the SQL unfiltered. Returns
    ``{state_code: {kpi: value, …}}`` (recap/event counts added by the caller).
    """
    recaps = _filter_event_year(
        Recap.objects.filter(event__tenant_id=tenant_id), "event__", year
    )
    engagements = _filter_event_year(
        ConsumerEngagements.objects.filter(recap__event__tenant_id=tenant_id),
        "recap__event__",
        year,
    )
    samples = _filter_event_year(
        ProductSamples.objects.filter(recap__event__tenant_id=tenant_id),
        "recap__event__",
        year,
    )

    eng_path = f"recap__{_LEGACY_STATE_PATH}"
    sample_path = f"recap__{_LEGACY_STATE_PATH}"

    per_state: dict[str, dict] = {}

    def _merge(code_map: dict[str, int], field: str) -> None:
        for code, value in code_map.items():
            per_state.setdefault(code, {})[field] = (
                per_state.get(code, {}).get(field, 0) + value
            )

    _merge(_grouped_sum(recaps, _LEGACY_STATE_PATH, "total_engagements"),
           "total_engagements")
    _merge(_grouped_sum(recaps, _LEGACY_STATE_PATH, "products_sold"),
           "products_sold")
    _merge(_grouped_sum(engagements, eng_path, "total_consumer"),
           "consumers_reached")
    _merge(_grouped_sum(samples, sample_path, "quantity"),
           "samples_distributed")
    return per_state


def _custom_market_kpis(tenant_id: int, year: int | None = None) -> dict[str, dict]:
    """Per-state custom :class:`recaps.models.CustomRecap` KPIs for one tenant.

    The per-state sibling of :func:`_custom_kpis`, attributing each metric to
    the recap's ``event``'s state code:

    * ``total_engagements`` — typed column, summed in the DB grouped by the
      event state path.
    * ``samples_distributed`` — STRUCTURED ``CustomRecapProductSample``
      quantities, summed in the DB grouped by state; falls back PER STATE to
      that state's summed free-text "consumers sampled" when it has no
      structured samples (mirrors :func:`_custom_kpis` /
      ``report_service._accumulate_custom``).
    * ``consumers_reached`` / ``products_sold`` — parsed from the bounded
      KPI-relevant ``CustomFieldValue`` rows (same name regex + parse rules
      as :func:`_custom_kpis`), accumulated per recap and attributed to that
      recap's event state via a recap_id -> state_code map. We never load a
      CustomRecap object — only the matched value rows + the small id->state
      map.

    ``year`` narrows every queryset to its EVENT date within the calendar
    year (:func:`_filter_event_year`, the hero + :func:`_custom_kpis` basis),
    so a state's numbers count the period its events happened in — not when
    the recap rows were created/imported; ``year=None`` leaves the SQL
    unfiltered. Returns ``{state_code: {kpi: value, …}}``.
    """
    # Address-based state fallback for events created without a State FK
    # (a complete address like "…, Austin, TX 78759" still resolves to TX).
    from events.routing import extract_state_code

    custom_recaps = _filter_event_year(
        CustomRecap.objects.filter(tenant_id=tenant_id), "event__", year
    )
    structured_samples_qs = _filter_event_year(
        CustomRecapProductSample.objects.filter(custom_recap__tenant_id=tenant_id),
        "custom_recap__event__",
        year,
    )

    per_state: dict[str, dict] = {}

    def _bucket(code: str) -> dict:
        return per_state.setdefault(code, {})

    # Typed engagement column groups cleanly in SQL.
    for code, value in _grouped_sum(
        custom_recaps, _CUSTOM_STATE_PATH, "total_engagements"
    ).items():
        _bucket(code)["total_engagements"] = (
            _bucket(code).get("total_engagements", 0) + value
        )

    # Structured custom samples group cleanly in SQL.
    structured_by_state = _grouped_sum(
        structured_samples_qs,
        f"custom_recap__{_CUSTOM_STATE_PATH}",
        "quantity",
    )

    # Free-text KPI value rows: pull only the KPI-relevant rows (name regex
    # matched in SQL), each carrying its recap id + the recap's event state
    # code, so we can group per recap (for the per-recap parse rules) and
    # attribute the result to the right state. Bounded slice, never the full
    # recap tree.
    rows = (
        _filter_event_year(
            CustomFieldValue.objects.filter(
                custom_recap__tenant_id=tenant_id,
                custom_field__name__iregex=_CUSTOM_KPI_NAME_RE.pattern,
            ),
            "custom_recap__event__",
            year,
        )
        .values_list(
            "custom_recap_id",
            f"custom_recap__{_CUSTOM_STATE_PATH}",
            "custom_recap__event__address",
            "custom_field__name",
            "value",
        )
        .order_by("custom_recap_id")
    )

    per_recap: dict[int, dict] = {}
    for recap_id, state_code, address, name, value in rows.iterator():
        entry = per_recap.setdefault(
            recap_id, {"state": state_code, "address": address, "pairs": []}
        )
        entry["pairs"].append((name, value))

    # Per-state free-text "consumers sampled" total, the structured-sample
    # fallback source (kept separate from consumers_reached so the fallback
    # mirrors _custom_kpis' sampled_total).
    sampled_by_state: dict[str, int] = {}

    for entry in per_recap.values():
        # Prefer the event's State FK; when it's blank, fall back to parsing
        # the state out of the event address ("…, Austin, TX 78759, USA" ->
        # "TX"). Events created without a State FK but WITH a full address
        # were otherwise silently dropped from the map (the Neutonic Austin
        # recap case).
        code = entry["state"] or extract_state_code(entry.get("address"))
        if not code:
            # No event state AND none parseable from the address -> can't
            # place this recap on the map; its KPIs are dropped from the
            # per-state view (it still counts in the whole-tenant
            # tenant_kpi_totals, which doesn't group by state).
            continue
        pairs = entry["pairs"]
        bucket = _bucket(code)

        consumers_sampled = _consumers_sampled_from_fields(pairs)
        if consumers_sampled is not None:
            bucket["consumers_reached"] = (
                bucket.get("consumers_reached", 0) + int(consumers_sampled)
            )
            sampled_by_state[code] = (
                sampled_by_state.get(code, 0) + int(consumers_sampled)
            )

        sold = _sold_units_from_fields(pairs)
        if sold is not None:
            bucket["products_sold"] = bucket.get("products_sold", 0) + int(sold)

    # samples_distributed per state prefers that state's structured
    # quantities, else falls back to its summed free-text "consumers
    # sampled" (mirrors _custom_kpis.out["samples_distributed"]).
    for code in set(structured_by_state) | set(sampled_by_state):
        if not code:
            continue
        _bucket(code)["samples_distributed"] = (
            structured_by_state.get(code, 0) or sampled_by_state.get(code, 0)
        )
    return per_state


def tenant_market_performance(
    tenant_id: int, year: int | None = None
) -> list[dict]:
    """Per-US-state KPI roll-up for one tenant, for the geographic heatmap.

    The geographic sibling of :func:`tenant_kpi_totals`: instead of one
    tenant-wide total, it returns ONE row per US state the tenant has
    activity in, so the frontend can color a map. Each row is a plain dict::

        {
            "state": "CA",            # 2-letter code (events.State.code)
            "event_count": int,       # tenant events in that state
            "recap_count": int,       # legacy + custom recaps in that state
            "consumers_reached": int,
            "samples_distributed": int,
            "products_sold": int,
            "total_engagements": int,
        }

    Grouping is by **the event's US state** — ``Event.state.code`` reached
    through each recap shape's (non-null) ``event`` FK and through the
    tenant's events directly — the same dedicated state FK
    :func:`_recent_event_lines` reads. Rows whose state code is null/blank
    are skipped (they can't be placed on the map).

    The four summable KPIs reuse the exact column / parse rules of
    :func:`tenant_kpi_totals` across BOTH recap shapes (legacy ``Recap`` +
    custom ``CustomRecap``), so summing a KPI across the returned rows equals
    that tenant's all-state total — with the one documented caveat that
    recaps whose event has no state are excluded here (they have no map
    bucket). Aggregation is database-side ``GROUP BY`` per state plus the
    same bounded free-text custom value rows the tenant total parses; no full
    recap tree is materialized, and the result is at most ~50 rows.

    ``year=None`` rolls up the tenant's whole history; ``year=Y`` restricts
    every figure (counts AND KPIs) to recaps/events whose EVENT date falls in
    calendar year ``Y`` — the same event-date basis the hero + KPI totals use,
    so a state's year numbers reflect when its events happened, not when the
    rows were created/imported.

    Rows are returned sorted by state code for a stable, deterministic order.
    """
    # Per-state event + recap counts (each a DB GROUP BY + COUNT), windowed
    # on the EVENT date so the counts agree with the KPIs below (same basis).
    event_counts = _grouped_count(
        _filter_event_year(
            Event.objects.filter(tenant_id=tenant_id), "", year
        ),
        "state__code",
    )
    legacy_recap_counts = _grouped_count(
        _filter_event_year(
            Recap.objects.filter(event__tenant_id=tenant_id), "event__", year
        ),
        _LEGACY_STATE_PATH,
    )
    custom_recap_counts = _grouped_count(
        _filter_event_year(
            CustomRecap.objects.filter(tenant_id=tenant_id), "event__", year
        ),
        _CUSTOM_STATE_PATH,
    )

    legacy_kpis = _legacy_market_kpis(tenant_id, year)
    custom_kpis = _custom_market_kpis(tenant_id, year)

    # Union every state code that showed up in any of the per-state maps.
    codes = (
        set(event_counts)
        | set(legacy_recap_counts)
        | set(custom_recap_counts)
        | set(legacy_kpis)
        | set(custom_kpis)
    )

    rows: list[dict] = []
    for code in sorted(codes):
        if not code:
            continue
        row = _blank_market_row(code)
        row["event_count"] = event_counts.get(code, 0)
        row["recap_count"] = legacy_recap_counts.get(
            code, 0
        ) + custom_recap_counts.get(code, 0)
        for field in _MARKET_KPI_FIELDS:
            row[field] = legacy_kpis.get(code, {}).get(field, 0) + custom_kpis.get(
                code, {}
            ).get(field, 0)
        rows.append(row)
    return rows


def _metro_from_event_name(name: str | None) -> str | None:
    """The metro-market label from an event named "<Market> — <Corridor> ·
    <date>", e.g. ``"Miami — Wynwood · 9/24"`` -> ``"Miami"``.

    Some tenants (Feel Free's Guerrilla Field Sampling program) name every
    event this way rather than carrying a structured city/market field —
    their imported events have no ``Location``/``State`` FK, and even their
    address text doesn't reliably resolve to the right label (Ft.
    Lauderdale's warehouse address is in Plantation, FL; Tampa/St. Pete's is
    in Pinellas Park, FL — see ``events/management/commands/
    import_event_schedule.py``). This convention-based prefix is the ONLY
    reliable metro signal for these tenants.

    Splits on the FIRST `` — `` (em dash WITH surrounding spaces, so a plain
    hyphen inside a venue name like "7-Eleven — Main St" still splits
    correctly — only the em-dash separator matters). Returns ``None`` for
    names that don't contain it, so tenants without this convention simply
    produce no metro rows (see :func:`tenant_metro_breakdown`).
    """
    if not name:
        return None
    parts = name.split(" — ", 1)
    if len(parts) != 2:
        return None
    metro = parts[0].strip()
    return metro or None


def tenant_metro_breakdown(
    tenant_id: int,
    start,
    end,
    event_type_id: int | None = None,
) -> dict:
    """Week-by-metro-market KPI roll-up for tenants whose events follow the
    "<Market> — <Corridor> · <date>" naming convention (see
    :func:`_metro_from_event_name`) — built for Feel Free's Guerrilla Field
    Sampling program, which runs identical weekly Thu–Sun shifts across
    several metro markets with no structured city/market field to GROUP BY.

    Buckets every tenant :class:`recaps.models.CustomRecap` (+ its event)
    whose effective EVENT date (:func:`_event_date_expr` — the SAME
    date/start_time/request-date priority the hero + per-state rollup use)
    falls in the half-open ``[start, end)`` window into (ISO year, ISO week,
    metro) cells, using the SAME free-text KPI parsing
    (:func:`recaps.types._consumers_sampled_from_fields` /
    ``_sold_units_from_fields``) :func:`_custom_market_kpis` uses — so a
    metro's numbers agree with the tenant's all-time / per-state totals.
    ``event_type_id`` optionally restricts to one :class:`events.models.
    EventType` (e.g. Feel Free's "Field Sampling").

    Unlike the per-state rollup, grouping happens in PYTHON, not SQL — the
    metro label comes from free-text ``Event.name``, not a DB column. Volume
    is bounded (one tenant, one caller-chosen date window — at most a few
    hundred rows for a summer-long weekly program), so this never approaches
    the whole-tenant-history scale the per-state functions are built to
    avoid loading into Python.

    Returns ``{"metros": [str, ...], "weeks": [{"iso_year", "iso_week",
    "week_start" (date, the Monday of that ISO week), "cells": {metro:
    {event_count, recap_count, consumers_reached, samples_distributed,
    products_sold, total_engagements}}}, ...]}``. ``metros`` is every
    distinct label seen, sorted; ``weeks`` is sorted by (iso_year,
    iso_week) ascending. A tenant whose events in-window don't follow the
    naming convention returns ``{"metros": [], "weeks": []}`` — the
    frontend uses an empty ``metros`` list to hide the whole section.
    """
    base = CustomRecap.objects.filter(tenant_id=tenant_id)
    if event_type_id is not None:
        base = base.filter(event__event_type_id=event_type_id)
    windowed = _filter_event_window(base, "event__", (start, end))

    # One bounded pass: id + the two fields needed to place the row, plus
    # the typed engagement column (cheap — no need for a second query just
    # to sum it, since grouping can't happen in SQL anyway).
    recap_rows = list(
        windowed.values_list("id", "event_id", "event__name", "_evtdate", "total_engagements")
    )
    if not recap_rows:
        return {"metros": [], "weeks": []}

    # recap_id -> (metro, iso_year, iso_week); rows whose event name doesn't
    # match the convention (or has no placeable date, which _filter_event_window
    # already excludes) are dropped — same "can't place this row" posture
    # _custom_market_kpis takes for a stateless event.
    placement: dict[int, tuple[str, int, int]] = {}
    event_ids: dict[int, int] = {}
    week_starts: dict[tuple[int, int], date] = {}
    engagement_totals: dict[tuple[str, int, int], int] = {}
    for recap_id, event_id, name, evtdate, total_engagements in recap_rows:
        metro = _metro_from_event_name(name)
        if not metro:
            continue
        d = evtdate.date()
        iso_year, iso_week, _ = d.isocalendar()
        key = (metro, iso_year, iso_week)
        placement[recap_id] = key
        event_ids[recap_id] = event_id
        week_starts.setdefault((iso_year, iso_week), d - timedelta(days=d.isoweekday() - 1))
        engagement_totals[key] = engagement_totals.get(key, 0) + int(total_engagements or 0)

    if not placement:
        return {"metros": [], "weeks": []}

    recap_ids = list(placement.keys())

    # Event + recap counts per bucket. event_count is DISTINCT events (a
    # market/week usually has one event per corridor stop, but don't assume
    # 1:1 with recaps).
    counts: dict[tuple[str, int, int], dict] = {}
    events_seen: dict[tuple[str, int, int], set] = {}
    for recap_id, key in placement.items():
        counts.setdefault(key, {"event_count": 0, "recap_count": 0})
        counts[key]["recap_count"] += 1
        events_seen.setdefault(key, set()).add(event_ids[recap_id])
    for key, ev_ids in events_seen.items():
        counts[key]["event_count"] = len(ev_ids)

    # Structured product-sample quantities, bounded to the placed recaps —
    # mirrors _custom_market_kpis' structured-samples-per-state pass.
    structured_totals: dict[tuple[str, int, int], int] = {}
    for recap_id, quantity in CustomRecapProductSample.objects.filter(
        custom_recap_id__in=recap_ids
    ).values_list("custom_recap_id", "quantity"):
        key = placement.get(recap_id)
        if key is None:
            continue
        structured_totals[key] = structured_totals.get(key, 0) + int(quantity or 0)

    # Free-text KPI value rows, bounded to the placed recaps + the same
    # KPI-relevant name regex _custom_market_kpis filters on — grouped per
    # recap (for the per-recap parse rules), then attributed to that
    # recap's (metro, week) bucket.
    per_recap_pairs: dict[int, list[tuple[str, str]]] = {}
    for recap_id, cf_name, value in (
        CustomFieldValue.objects.filter(
            custom_recap_id__in=recap_ids,
            custom_field__name__iregex=_CUSTOM_KPI_NAME_RE.pattern,
        )
        .values_list("custom_recap_id", "custom_field__name", "value")
        .order_by("custom_recap_id")
    ):
        per_recap_pairs.setdefault(recap_id, []).append((cf_name, value))

    sampled_totals: dict[tuple[str, int, int], int] = {}
    products_totals: dict[tuple[str, int, int], int] = {}
    for recap_id, pairs in per_recap_pairs.items():
        key = placement.get(recap_id)
        if key is None:
            continue
        consumers_sampled = _consumers_sampled_from_fields(pairs)
        if consumers_sampled is not None:
            sampled_totals[key] = sampled_totals.get(key, 0) + int(consumers_sampled)
        sold = _sold_units_from_fields(pairs)
        if sold is not None:
            products_totals[key] = products_totals.get(key, 0) + int(sold)

    metros = sorted({metro for metro, _, _ in placement.values()})
    week_keys = sorted({(iso_year, iso_week) for _, iso_year, iso_week in placement.values()})

    weeks = []
    for iso_year, iso_week in week_keys:
        cells = {}
        for metro in metros:
            key = (metro, iso_year, iso_week)
            if key not in counts:
                continue
            consumers_reached = sampled_totals.get(key, 0)
            cells[metro] = {
                "event_count": counts[key]["event_count"],
                "recap_count": counts[key]["recap_count"],
                "consumers_reached": consumers_reached,
                # Prefers structured sample quantities; falls back to the
                # free-text sampled-consumer count when this bucket logged
                # no structured samples — mirrors _custom_market_kpis.
                "samples_distributed": structured_totals.get(key, 0) or consumers_reached,
                "products_sold": products_totals.get(key, 0),
                "total_engagements": engagement_totals.get(key, 0),
            }
        weeks.append(
            {
                "iso_year": iso_year,
                "iso_week": iso_week,
                "week_start": week_starts[(iso_year, iso_week)],
                "cells": cells,
            }
        )

    return {"metros": metros, "weeks": weeks}


def _recent_event_lines(tenant_id: int) -> list[str]:
    """Up to :data:`MAX_RECENT_EVENTS` recent events as 'name · date · city, ST'.

    Ordered most-recent-first by event date (NULL dates last). Only the
    capped slice is materialized; ``select_related`` avoids per-row joins.
    """
    events = (
        Event.objects.filter(tenant_id=tenant_id)
        .select_related("location", "state")
        .order_by("-date", "-id")[:MAX_RECENT_EVENTS]
    )
    lines: list[str] = []
    for ev in events:
        name = (getattr(ev, "name", None) or "").strip() or "(event)"
        date_val = getattr(ev, "date", None)
        date_str = date_val.strftime("%b %-d, %Y") if date_val else "no date"
        city = getattr(getattr(ev, "location", None), "name", None)
        state = getattr(getattr(ev, "state", None), "name", None)
        where = ", ".join(p for p in (city, state) if p) or "location n/a"
        lines.append(f"- {name} · {date_str} · {where}")
    return lines


def _clean_quote(text: str | None) -> str | None:
    """Collapse whitespace + length-trim a single quote (None if empty)."""
    if not text:
        return None
    cleaned = " ".join(str(text).split()).strip()
    if not cleaned:
        return None
    if len(cleaned) > MAX_QUOTE_CHARS:
        cleaned = cleaned[: MAX_QUOTE_CHARS - 1].rstrip() + "…"
    return cleaned


def _recent_quote_lines(tenant_id: int) -> list[str]:
    """Up to :data:`MAX_RECENT_QUOTES` recent consumer quotes/highlights.

    Pulls only the ``quotes`` / ``positive_stories`` text columns from the
    most recent legacy :class:`recaps.models.ConsumerFeedback` rows (scoped
    through ``recap__event__tenant_id``), deduped on cleaned text. Custom
    recaps store highlights as free-text fields with no reliable typed
    column, so — to keep this bounded and cheap — the quotes section draws
    from legacy feedback only; the KPI sums above still cover both shapes.
    """
    rows = (
        ConsumerFeedback.objects.filter(recap__event__tenant_id=tenant_id)
        .order_by("-created_at", "-id")
        .values_list("quotes", "positive_stories")
    )
    lines: list[str] = []
    seen: set[str] = set()
    for quotes, stories in rows.iterator():
        for raw in (quotes, stories):
            cleaned = _clean_quote(raw)
            if not cleaned:
                continue
            key = cleaned.lower()
            if key in seen:
                continue
            seen.add(key)
            lines.append(f'- "{cleaned}"')
            if len(lines) >= MAX_RECENT_QUOTES:
                return lines
    return lines


def build_tenant_overview(tenant_id: int) -> str:
    """Render a compact plaintext summary of ONE tenant's whole dataset.

    Includes the brand/tenant name; headline totals (# campaigns, #
    events, # recaps, overall activity date range); the nine summed KPIs
    (same field set as the per-campaign report, across both recap shapes);
    up to ten most recent events; and up to ten recent consumer quotes.

    Every total is a database aggregate and the two list sections are
    hard-capped, so the output stays a few dozen lines regardless of how
    much activity the tenant has — keeping the downstream Gemini prompt
    small. Raises :class:`tenants.models.Tenant.DoesNotExist` if no tenant
    matches ``tenant_id`` (the resolver translates that to a degradation
    reason).
    """
    tenant = Tenant.objects.get(id=tenant_id)

    # Headline counts — each a single COUNT(*) in the DB. Event/recap
    # counts come from the shared helper the structured resolver also uses.
    campaign_count = Request.objects.filter(
        tenant_id=tenant_id, deleted_at__isnull=True
    ).count()
    event_count, recap_count = tenant_event_recap_counts(tenant_id)

    # Overall activity date range over the tenant's events (reuse the
    # campaign report's label formatter). Min/Max are computed in the DB —
    # a single aggregate query, no event rows pulled into Python.
    span = Event.objects.filter(tenant_id=tenant_id, date__isnull=False).aggregate(
        _lo=Min("date"), _hi=Max("date")
    )
    lo, hi = span["_lo"], span["_hi"]
    if lo and hi:
        # Synthesize the two endpoints into the shape _format_date_range
        # expects (objects exposing a `.date` datetime).
        class _D:
            def __init__(self, d):
                self.date = d

        date_range = _format_date_range([_D(lo), _D(hi)])
    else:
        date_range = None

    kpis = tenant_kpi_totals(tenant_id)

    lines = [
        f"Brand: {tenant.name or 'N/A'}",
        f"Campaigns (requests): {campaign_count}",
        f"Events: {event_count}",
        f"Recaps: {recap_count}",
        f"Activity date range: {date_range or 'N/A'}",
        "",
        "Aggregate KPIs across all recaps:",
        f"- Consumers reached: {kpis.consumers_reached}",
        f"- Samples distributed: {kpis.samples_distributed}",
        f"- Products sold: {kpis.products_sold}",
        f"- Cans sold: {kpis.cans_sold}",
        f"- Packs sold: {kpis.packs_sold}",
        f"- Total engagements: {kpis.total_engagements}",
        f"- First-time consumers: {kpis.first_time_consumers}",
        f"- Brand-aware consumers: {kpis.brand_aware_consumers}",
        f"- Willing to purchase: {kpis.willing_to_purchase}",
    ]

    event_lines = _recent_event_lines(tenant_id)
    if event_lines:
        lines.append("")
        lines.append(f"Most recent events (up to {MAX_RECENT_EVENTS}):")
        lines.extend(event_lines)

    quote_lines = _recent_quote_lines(tenant_id)
    if quote_lines:
        lines.append("")
        lines.append(f"Recent consumer quotes (up to {MAX_RECENT_QUOTES}):")
        lines.extend(quote_lines)

    return "\n".join(lines)
