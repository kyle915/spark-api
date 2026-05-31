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


def _legacy_kpis(tenant_id: int) -> dict[str, int]:
    """Sum the legacy :class:`recaps.models.Recap` KPIs for one tenant.

    Recap has no direct tenant FK, so every queryset is scoped through the
    event (``…__event__tenant_id`` / ``event__tenant_id``) — the same join
    ``tenants.insights`` and the recap lists use. Each line is a single
    aggregate query; no Recap row is loaded into Python.
    """
    recaps = Recap.objects.filter(event__tenant_id=tenant_id)
    engagements = ConsumerEngagements.objects.filter(
        recap__event__tenant_id=tenant_id
    )
    samples = ProductSamples.objects.filter(recap__event__tenant_id=tenant_id)
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


def _custom_kpis(tenant_id: int) -> dict[str, int]:
    """Sum the custom-template :class:`recaps.models.CustomRecap` KPIs.

    ``total_engagements`` is a typed column → summed in the DB. The four
    consumer metrics + sold units live in free-text ``CustomFieldValue``
    rows; we fetch only the KPI-relevant rows (``custom_field__name``
    matched by :data:`_CUSTOM_KPI_NAME_RE` in SQL), grouped per recap, and
    apply the same label/parse rules the per-campaign report uses so the
    totals agree. We never load a CustomRecap object — only the matched
    (recap_id, name, value) value rows.
    """
    out = {
        "total_engagements": _sum(
            CustomRecap.objects.filter(tenant_id=tenant_id), "total_engagements"
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
        CustomRecapProductSample.objects.filter(
            custom_recap__tenant_id=tenant_id
        ),
        "quantity",
    )

    # Pull only the KPI-relevant free-text value rows, grouped by recap so
    # the per-recap "consumers sampled" fallback (sold units + samples)
    # matches the campaign report's per-recap accumulation.
    rows = (
        CustomFieldValue.objects.filter(
            custom_recap__tenant_id=tenant_id,
            custom_field__name__iregex=_CUSTOM_KPI_NAME_RE.pattern,
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


def tenant_kpi_totals(tenant_id: int) -> TenantKpiTotals:
    """Legacy + custom KPI totals for one tenant, summed field-by-field.

    THE single source of truth for this tenant's nine summable KPIs. Both
    :func:`build_tenant_overview` (the plaintext Q&A block) and the
    structured ``tenantKpis`` GraphQL resolver call this, so the text and
    the chart numbers can never diverge. All sums are database aggregates
    (see :func:`_legacy_kpis` / :func:`_custom_kpis`); no recap row is
    loaded into Python beyond the bounded free-text custom value rows.
    """
    legacy = _legacy_kpis(tenant_id)
    custom = _custom_kpis(tenant_id)
    return TenantKpiTotals(
        **{
            f.name: int(legacy.get(f.name, 0)) + int(custom.get(f.name, 0))
            for f in dataclass_fields(TenantKpiTotals)
        }
    )


def tenant_event_recap_counts(tenant_id: int) -> tuple[int, int]:
    """``(event_count, recap_count)`` for one tenant — both DB counts.

    Shared by :func:`build_tenant_overview` (the headline line) and the
    structured ``tenantKpis`` resolver so the ``events`` / ``recaps``
    figures match the text block. ``recap_count`` unions BOTH shapes
    (legacy ``Recap`` joined through the event + custom ``CustomRecap`` via
    its direct tenant FK), exactly like the overview's headline. Each line
    is a single ``COUNT(*)``; no rows enter Python.
    """
    event_count = Event.objects.filter(tenant_id=tenant_id).count()
    legacy_recap_count = Recap.objects.filter(event__tenant_id=tenant_id).count()
    custom_recap_count = CustomRecap.objects.filter(tenant_id=tenant_id).count()
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


def tenant_monthly_trend(tenant_id: int) -> list[TenantKpiMonth]:
    """Last :data:`MONTHLY_TREND_MONTHS` calendar months of activity, zero-filled.

    Oldest → newest, one :class:`TenantKpiMonth` per month in the window
    (months with no activity are zero-filled, so the series is always a
    bounded, contiguous <=12-point set the frontend can chart directly).

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

    All five querysets are scoped to ``tenant_id`` and floored to the trend
    window so the GROUP BY never returns more than the window's months.
    """
    start = _trend_window_start()

    legacy_recaps = Recap.objects.filter(
        event__tenant_id=tenant_id, created_at__gte=start
    )
    custom_recaps = CustomRecap.objects.filter(
        tenant_id=tenant_id, created_at__gte=start
    )
    legacy_samples = ProductSamples.objects.filter(
        recap__event__tenant_id=tenant_id, created_at__gte=start
    )
    custom_samples = CustomRecapProductSample.objects.filter(
        custom_recap__tenant_id=tenant_id, created_at__gte=start
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
    year, month = start.year, start.month
    for _ in range(MONTHLY_TREND_MONTHS):
        key = f"{year:04d}-{month:02d}"
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
            year += 1
    return months


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
