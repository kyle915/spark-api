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

from django.db.models import Count, Max, Min, Sum
from django.db.models.functions import TruncMonth
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
from recaps.types import _consumers_sampled_from_fields, _sold_units_from_fields
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


def _filter_year(queryset, date_field: str, year: int | None):
    """Narrow ``queryset`` to ``date_field`` within calendar ``year``.

    ``year=None`` is the all-time path: the queryset is returned UNTOUCHED
    (no extra ``WHERE`` clause), so callers that pass no year keep their
    exact previous SQL — this is what preserves the byte-for-byte all-time
    behavior the Ask-AI and Insights callers depend on. When a year is
    given, apply the half-open :func:`_year_bounds` window on ``date_field``.
    """
    if year is None:
        return queryset
    start, end = _year_bounds(year)
    return queryset.filter(
        **{f"{date_field}__gte": start, f"{date_field}__lt": end}
    )


def _legacy_kpis(tenant_id: int, year: int | None = None) -> dict[str, int]:
    """Sum the legacy :class:`recaps.models.Recap` KPIs for one tenant.

    Recap has no direct tenant FK, so every queryset is scoped through the
    event (``…__event__tenant_id`` / ``event__tenant_id``) — the same join
    ``tenants.insights`` and the recap lists use. Each line is a single
    aggregate query; no Recap row is loaded into Python.

    When ``year`` is given, each queryset is additionally narrowed to its
    own ``created_at`` within that calendar year (the same date field and
    half-open window the monthly trend uses); ``year=None`` leaves every
    queryset untouched, so the all-time SQL is unchanged.
    """
    recaps = _filter_year(
        Recap.objects.filter(event__tenant_id=tenant_id), "created_at", year
    )
    engagements = _filter_year(
        ConsumerEngagements.objects.filter(recap__event__tenant_id=tenant_id),
        "created_at",
        year,
    )
    samples = _filter_year(
        ProductSamples.objects.filter(recap__event__tenant_id=tenant_id),
        "created_at",
        year,
    )
    return {
        "total_engagements": _sum(recaps, "total_engagements"),
        "products_sold": _sum(recaps, "products_sold"),
        "cans_sold": _sum(recaps, "total_cans_sold"),
        "packs_sold": _sum(recaps, "total_packs_sold"),
        "consumers_reached": _sum(engagements, "total_consumer"),
        "first_time_consumers": _sum(engagements, "first_time_consumers"),
        "brand_aware_consumers": _sum(engagements, "brand_aware_consumers"),
        "willing_to_purchase": _sum(engagements, "willing_to_purchase_consumers"),
        "samples_distributed": _sum(samples, "quantity"),
    }


# Custom recaps keep most KPIs as free-text CustomFieldValue rows keyed by
# the field NAME. They can't be summed in SQL, so we pull ONLY the
# KPI-relevant value rows (filtered by a name regex in the DB) and parse
# them in Python — a bounded slice, not the full recap tree. The patterns
# mirror recaps.report_service._custom_engagement_totals +
# recaps.types._sold_units_from_fields / _consumers_sampled_from_fields.
_CUSTOM_KPI_NAME_RE = re.compile(
    r"consumers sampled|first time|knew about|willing to purchase|cans?|packs?",
    re.IGNORECASE,
)


def _custom_kpis(tenant_id: int, year: int | None = None) -> dict[str, int]:
    """Sum the custom-template :class:`recaps.models.CustomRecap` KPIs.

    ``total_engagements`` is a typed column → summed in the DB. The four
    consumer metrics + sold units live in free-text ``CustomFieldValue``
    rows; we fetch only the KPI-relevant rows (``custom_field__name``
    matched by :data:`_CUSTOM_KPI_NAME_RE` in SQL), grouped per recap, and
    apply the same label/parse rules the per-campaign report uses so the
    totals agree. We never load a CustomRecap object — only the matched
    (recap_id, name, value) value rows.

    When ``year`` is given, each queryset is narrowed to its own
    ``created_at`` within that calendar year (the custom value rows are
    filtered on their OWN ``created_at``, consistent with the structured
    sums and the monthly trend); ``year=None`` leaves every queryset
    untouched, so the all-time SQL is unchanged.
    """
    out = {
        "total_engagements": _sum(
            _filter_year(
                CustomRecap.objects.filter(tenant_id=tenant_id),
                "created_at",
                year,
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
        _filter_year(
            CustomRecapProductSample.objects.filter(
                custom_recap__tenant_id=tenant_id
            ),
            "created_at",
            year,
        ),
        "quantity",
    )

    # Pull only the KPI-relevant free-text value rows, grouped by recap so
    # the per-recap "consumers sampled" fallback (sold units + samples)
    # matches the campaign report's per-recap accumulation.
    rows = (
        _filter_year(
            CustomFieldValue.objects.filter(
                custom_recap__tenant_id=tenant_id,
                custom_field__name__iregex=_CUSTOM_KPI_NAME_RE.pattern,
            ),
            "created_at",
            year,
        )
        .values_list("custom_recap_id", "custom_field__name", "value")
        .order_by("custom_recap_id")
    )

    per_recap: dict[int, list[tuple[str | None, str | None]]] = {}
    for recap_id, name, value in rows.iterator():
        per_recap.setdefault(recap_id, []).append((name, value))

    sampled_total = 0
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

    # samplesDistributed prefers structured quantities; fall back to the
    # summed "consumers sampled" headline when no structured samples exist
    # (mirrors report_service._accumulate_custom).
    out["samples_distributed"] = structured_samples or sampled_total
    return out


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
    """
    legacy = _legacy_kpis(tenant_id, year)
    custom = _custom_kpis(tenant_id, year)
    return TenantKpiTotals(
        **{
            f.name: int(legacy.get(f.name, 0)) + int(custom.get(f.name, 0))
            for f in dataclass_fields(TenantKpiTotals)
        }
    )


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
    """
    event_count = _filter_year(
        Event.objects.filter(tenant_id=tenant_id), "created_at", year
    ).count()
    legacy_recap_count = _filter_year(
        Recap.objects.filter(event__tenant_id=tenant_id), "created_at", year
    ).count()
    custom_recap_count = _filter_year(
        CustomRecap.objects.filter(tenant_id=tenant_id), "created_at", year
    ).count()
    return event_count, legacy_recap_count + custom_recap_count


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

    def _windowed(queryset):
        """Floor to ``start``; cap at ``end`` only when a year was given.

        Leaving the upper bound off for the trailing (``year=None``) window
        keeps that query's SQL exactly ``created_at >= start`` as before.
        """
        queryset = queryset.filter(created_at__gte=start)
        if end is not None:
            queryset = queryset.filter(created_at__lt=end)
        return queryset

    legacy_recaps = _windowed(Recap.objects.filter(event__tenant_id=tenant_id))
    custom_recaps = _windowed(CustomRecap.objects.filter(tenant_id=tenant_id))
    legacy_samples = _windowed(
        ProductSamples.objects.filter(recap__event__tenant_id=tenant_id)
    )
    custom_samples = _windowed(
        CustomRecapProductSample.objects.filter(custom_recap__tenant_id=tenant_id)
    )

    recap_counts: dict[str, int] = {}
    for src in (legacy_recaps, custom_recaps):
        for key, val in _bucket_counts(src, "created_at", count=True).items():
            recap_counts[key] = recap_counts.get(key, 0) + val

    engagement_counts: dict[str, int] = {}
    for src in (legacy_recaps, custom_recaps):
        for key, val in _bucket_counts(
            src, "created_at", sum_field="total_engagements"
        ).items():
            engagement_counts[key] = engagement_counts.get(key, 0) + val

    sample_counts: dict[str, int] = {}
    for src in (legacy_samples, custom_samples):
        for key, val in _bucket_counts(
            src, "created_at", sum_field="quantity"
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
    year-filtered with the same half-open window; ``year=None`` leaves the
    SQL unfiltered. Returns ``{state_code: {kpi: value, …}}`` (recap/event
    counts are added by the caller).
    """
    recaps = _filter_year(
        Recap.objects.filter(event__tenant_id=tenant_id), "created_at", year
    )
    engagements = _filter_year(
        ConsumerEngagements.objects.filter(recap__event__tenant_id=tenant_id),
        "created_at",
        year,
    )
    samples = _filter_year(
        ProductSamples.objects.filter(recap__event__tenant_id=tenant_id),
        "created_at",
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

    ``year`` narrows every queryset to its own ``created_at`` within the
    calendar year, exactly like :func:`_custom_kpis`; ``year=None`` leaves the
    SQL unfiltered. Returns ``{state_code: {kpi: value, …}}``.
    """
    custom_recaps = _filter_year(
        CustomRecap.objects.filter(tenant_id=tenant_id), "created_at", year
    )
    structured_samples_qs = _filter_year(
        CustomRecapProductSample.objects.filter(custom_recap__tenant_id=tenant_id),
        "created_at",
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
        _filter_year(
            CustomFieldValue.objects.filter(
                custom_recap__tenant_id=tenant_id,
                custom_field__name__iregex=_CUSTOM_KPI_NAME_RE.pattern,
            ),
            "created_at",
            year,
        )
        .values_list(
            "custom_recap_id",
            f"custom_recap__{_CUSTOM_STATE_PATH}",
            "custom_field__name",
            "value",
        )
        .order_by("custom_recap_id")
    )

    per_recap: dict[int, dict] = {}
    for recap_id, state_code, name, value in rows.iterator():
        entry = per_recap.setdefault(
            recap_id, {"state": state_code, "pairs": []}
        )
        entry["pairs"].append((name, value))

    # Per-state free-text "consumers sampled" total, the structured-sample
    # fallback source (kept separate from consumers_reached so the fallback
    # mirrors _custom_kpis' sampled_total).
    sampled_by_state: dict[str, int] = {}

    for entry in per_recap.values():
        code = entry["state"]
        if not code:
            # No event state -> can't place this recap on the map; its KPIs
            # are dropped from the per-state view (it still counts in the
            # whole-tenant tenant_kpi_totals, which doesn't group by state).
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
    every figure to recaps/events whose ``created_at`` falls in calendar year
    ``Y`` — the identical half-open window the other tenant aggregates use.

    Rows are returned sorted by state code for a stable, deterministic order.
    """
    # Per-state event + recap counts (each a DB GROUP BY + COUNT).
    event_counts = _grouped_count(
        _filter_year(
            Event.objects.filter(tenant_id=tenant_id), "created_at", year
        ),
        "state__code",
    )
    legacy_recap_counts = _grouped_count(
        _filter_year(
            Recap.objects.filter(event__tenant_id=tenant_id), "created_at", year
        ),
        _LEGACY_STATE_PATH,
    )
    custom_recap_counts = _grouped_count(
        _filter_year(
            CustomRecap.objects.filter(tenant_id=tenant_id), "created_at", year
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
