"""GraphQL surface for the Client Campaign Report (clients schema).

Exposes a single tenant-scoped query — ``campaignReport(requestId: ID!)``
— that returns the aggregate report for one :class:`events.models.Request`
plus a signed share token the web client can turn into a public link /
PDF URL.

Tenant scoping copies the receipts posture
(``receipts.queries._require_admin_or_client`` /
``_enforce_client_tenant``): a client-role user is pinned to their own
tenant; admins (spark-admin / staff / superuser / @igniteproductions.co)
pass through to any tenant. Out-of-scope or missing requests resolve to
``null`` rather than an error, matching the other single-record client
resolvers (``request`` / ``client`` / ``event``).

All heavy lifting lives in :mod:`recaps.report_service`; this module is
just the type mapping + the auth/scoping shell, run via ``sync_to_async``
because the aggregation is synchronous Django ORM.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta

import strawberry
from asgiref.sync import sync_to_async
from django.db import transaction
from django.utils import timezone

from recaps import report_service
from recaps.recap_quality import recap_quality_flags
from recaps.report_tokens import make_report_token
from recaps.tenant_ba_leaderboard import tenant_ba_leaderboard
from recaps.tenant_insights import build_insight_buckets
from recaps.tenant_sentiment import get_or_refresh_tenant_sentiment
from recaps.field_sampling_report import (
    build_field_sampling_report,
    generate_ai_callout_summary,
)
from recaps.tenant_overview import (
    build_tenant_overview,
    tenant_event_recap_counts,
    tenant_kpi_comparison,
    tenant_kpi_totals,
    tenant_market_performance,
    tenant_metro_breakdown,
    tenant_monthly_trend,
)
from utils.ai_text import (
    AiUnavailable,
    generate_structured_answer,
    generate_summary,
)
from utils.graphql.inputs import SparkGraphQLInput
from utils.graphql.mixins import SparkGraphQLMixin, resolve_id_to_int
from utils.graphql.permissions import (
    StrictIsAuthenticated,
    _is_admin_access,
    resolve_request_user_access,
)

logger = logging.getLogger(__name__)

# System prompt for the executive-summary generator. Pins the persona +
# the hard constraint that the model must only use the numbers we hand it
# (so it can't hallucinate KPIs the campaign didn't hit).
_AI_SUMMARY_SYSTEM_PROMPT = (
    "You are a field-marketing analyst writing a concise, upbeat but "
    "factual executive summary of a sampling campaign for the brand "
    "client. 2-3 short paragraphs. Use only the numbers provided; do not "
    "invent figures."
)

# Cap the number of consumer quotes we feed the model — a representative
# sample, not the whole highlight reel, keeps the prompt small.
_AI_SUMMARY_MAX_QUOTES = 5

# System prompt for the freeform Q&A generator. Pins the model to the
# provided campaign data only (no invented or estimated numbers) and to a
# concise answer, with an explicit escape hatch when the data can't answer
# the question.
_AI_ANSWER_SYSTEM_PROMPT = (
    "You answer questions about a single field-marketing campaign using "
    "ONLY the provided campaign data. Give a complete, well-structured "
    "answer: lead with a direct response, then back it with the specific "
    "numbers, products, and consumer quotes from the data. Use short "
    "paragraphs or bullet points when they aid clarity. Be thorough but do "
    "not pad or repeat. Never invent or estimate numbers that are not "
    "present in the data. If the data does not contain the answer, say "
    "plainly that it cannot be determined from this campaign's data. "
    "Include a `chart` ONLY when the question is naturally answered by a "
    "visualization — a trend over time, a comparison, or a breakdown — and "
    "build it ONLY from numbers that appear in the provided data (never "
    "invented or estimated). Pick `bar` for comparisons/breakdowns and "
    "`line` for trends over time; `labels` are the categories or time "
    "buckets and each series' `data` aligns one-to-one with them. The "
    "`answer` text must stand on its own and still state the numbers even "
    "when a chart is present. In every other case set `chart` to null."
)

# Hard cap on the inbound question length — keeps the prompt bounded and
# blocks a pathologically long question from blowing up the request.
_AI_ANSWER_MAX_QUESTION_CHARS = 1000

# System prompt for the TENANT-WIDE freeform Q&A generator — the
# client-level sibling of ``_AI_ANSWER_SYSTEM_PROMPT``. Pins the model to
# the single client's aggregated program data only (no invented or
# estimated numbers) and to a concise answer, with an explicit escape
# hatch when the data can't answer the question.
_AI_TENANT_ANSWER_SYSTEM_PROMPT = (
    "You answer questions about ONE field-marketing client's program using "
    "ONLY the provided data, which aggregates all of that client's "
    "campaigns, events, and recaps. Give a complete, well-structured "
    "answer: lead with a direct response, then support it with the "
    "specific numbers, trends, products, and consumer quotes from the "
    "data. Use short paragraphs or bullet points when they aid clarity. Be "
    "thorough but do not pad or repeat. Never invent or estimate numbers "
    "that are not present in the data. If the data does not contain the "
    "answer, say plainly that it cannot be determined from this client's "
    "data. Include a `chart` ONLY when the question is naturally answered "
    "by a visualization — a trend over time, a comparison, or a breakdown — "
    "and build it ONLY from numbers that appear in the provided data (never "
    "invented or estimated). Pick `bar` for comparisons/breakdowns and "
    "`line` for trends over time; `labels` are the categories or time "
    "buckets and each series' `data` aligns one-to-one with them. The "
    "`answer` text must stand on its own and still state the numbers even "
    "when a chart is present. In every other case set `chart` to null."
)


@strawberry.type
class CampaignReportKpis:
    events: int
    recaps: int
    consumers_reached: int
    samples_distributed: int
    products_sold: int
    cans_sold: int
    packs_sold: int
    total_engagements: int
    first_time_consumers: int
    brand_aware_consumers: int
    willing_to_purchase: int


@strawberry.type
class CampaignReportPhoto:
    url: str
    caption: str | None = None


@strawberry.type
class CampaignReportEventRow:
    id: strawberry.ID
    name: str | None
    date: str | None
    location_name: str | None
    city: str | None
    state: str | None
    coordinates: str | None
    recap_count: int


@strawberry.type
class CampaignReportBa:
    name: str
    is_external: bool
    event_count: int


@strawberry.type
class CampaignReportQuote:
    text: str
    source: str | None = None


@strawberry.type
class CampaignReport:
    request_id: strawberry.ID
    brand_name: str
    title: str
    date_range: str | None
    generated_at: str
    share_token: str
    kpis: CampaignReportKpis
    events: list[CampaignReportEventRow]
    photos: list[CampaignReportPhoto]
    ambassadors: list[CampaignReportBa]
    highlights: list[CampaignReportQuote]


@strawberry.type
class CampaignReportAiSummary:
    """Result of the on-demand AI executive summary.

    ``ok`` is the only field a caller must branch on: when ``true``,
    ``summary`` holds the generated copy and ``reason`` is null; when
    ``false`` (request out of scope, AI unconfigured, or the upstream
    call failed), ``summary`` is ``""`` and ``reason`` carries a short,
    human-readable explanation. The resolver never raises — degradation
    is always a value, never a GraphQL error.
    """

    ok: bool
    summary: str
    reason: str | None = None


@strawberry.type
class AiChartSeries:
    """One named data series within an :class:`AiChart`.

    ``data`` aligns positionally with the parent chart's ``labels`` (one
    value per label). The model builds these ONLY from numbers present in
    the provided data.
    """

    label: str
    data: list[float]


@strawberry.type
class AiChart:
    """An optional, AI-chosen visualization accompanying an answer.

    Returned alongside the text answer ONLY when a question is naturally
    answered by a chart (a trend, comparison, or breakdown); otherwise the
    answer's ``chart`` field is null. ``type`` is ``"bar"`` or ``"line"``;
    ``labels`` are the shared x-axis categories/buckets and each entry of
    ``series`` carries one line/bar's values. The frontend renders this in
    a separate task — the text ``answer`` always stands on its own.
    """

    type: str
    title: str | None
    labels: list[str]
    series: list[AiChartSeries]


# Chart shapes the frontend can render. Anything else from the model is
# treated as "no chart" rather than surfaced raw.
_AI_CHART_TYPES = frozenset({"bar", "line"})


def _build_ai_chart(chart: dict | None) -> AiChart | None:
    """Coerce a model-supplied chart dict into an :class:`AiChart`, or None.

    Defensive on purpose: the chart is a bonus on top of the text answer, so
    a missing, wrong-typed, or malformed chart NEVER raises — it just yields
    ``None`` (the answer is returned regardless). A chart is only built when
    every piece is well-formed: a known ``type``, string ``labels``, and at
    least one series whose ``data`` are all numbers. ``title`` is optional.
    """
    if not isinstance(chart, dict):
        return None

    chart_type = chart.get("type")
    if chart_type not in _AI_CHART_TYPES:
        return None

    raw_labels = chart.get("labels")
    if not isinstance(raw_labels, list) or not all(
        isinstance(label, str) for label in raw_labels
    ):
        return None
    labels = list(raw_labels)

    raw_series = chart.get("series")
    if not isinstance(raw_series, list) or not raw_series:
        return None

    series: list[AiChartSeries] = []
    for item in raw_series:
        if not isinstance(item, dict):
            return None
        series_label = item.get("label")
        raw_data = item.get("data")
        if not isinstance(series_label, str):
            return None
        if not isinstance(raw_data, list) or not all(
            # bool is an int subclass; exclude it so True/False can't pose as
            # a data point. Numbers are normalised to float for the schema.
            isinstance(value, (int, float)) and not isinstance(value, bool)
            for value in raw_data
        ):
            return None
        series.append(
            AiChartSeries(label=series_label, data=[float(v) for v in raw_data])
        )

    raw_title = chart.get("title")
    title = raw_title if isinstance(raw_title, str) else None

    return AiChart(type=chart_type, title=title, labels=labels, series=series)


@strawberry.type
class CampaignReportAiAnswer:
    """Result of an on-demand freeform Q&A over one campaign's report.

    Mirrors :class:`CampaignReportAiSummary`: ``ok`` is the only field a
    caller must branch on. When ``true``, ``answer`` holds the generated
    response and ``reason`` is null; when ``false`` (no question, request
    out of scope, AI unconfigured, or the upstream call failed),
    ``answer`` is ``""`` and ``reason`` carries a short, human-readable
    explanation. The resolver never raises — degradation is always a
    value, never a GraphQL error.

    ``chart`` is an OPTIONAL visualization the model chose to include when
    the question is naturally answered by one; it is null whenever no chart
    is warranted (or ``ok`` is false). A present-but-garbled chart from the
    model also yields null — the text ``answer`` is never affected.
    """

    ok: bool
    answer: str
    reason: str | None = None
    chart: AiChart | None = None


@strawberry.type
class TenantAiAnswer:
    """Result of an on-demand freeform Q&A over ONE client's whole dataset.

    The client-level sibling of :class:`CampaignReportAiAnswer`: instead of
    one campaign, the answer draws on the tenant's aggregated activity
    (every campaign, event, and recap). ``ok`` is the only field a caller
    must branch on. When ``true``, ``answer`` holds the generated response
    and ``reason`` is null; when ``false`` (no question, tenant out of
    scope / not found, AI unconfigured, or the upstream call failed),
    ``answer`` is ``""`` and ``reason`` carries a short, human-readable
    explanation. The resolver never raises — degradation is always a
    value, never a GraphQL error.

    ``chart`` is an OPTIONAL visualization the model chose to include when
    the question is naturally answered by one; it is null whenever no chart
    is warranted (or ``ok`` is false). A present-but-garbled chart from the
    model also yields null — the text ``answer`` is never affected.
    """

    ok: bool
    answer: str
    reason: str | None = None
    chart: AiChart | None = None


@strawberry.type
class TenantKpiMonth:
    """One calendar month of a tenant's activity for the dashboard trend.

    The structured, chart-friendly companion to a single point on the
    ``tenantKpis.monthlyTrend`` line. ``month`` is ``"YYYY-MM"``; the three
    metrics are database aggregates over that month across both recap
    shapes (see :func:`recaps.tenant_overview.tenant_monthly_trend`).
    """

    month: str
    recaps: int
    engagements: int
    samples: int


@strawberry.type
class TenantKpis:
    """Structured per-tenant KPI roll-up — the visual companion to
    :class:`TenantAiAnswer` (which answers the same tenant's data as text).

    ``events`` / ``recaps`` are headline counts; the nine summable KPIs
    mirror the per-campaign :class:`CampaignReportKpis` field-for-field but
    aggregated across the WHOLE tenant (every campaign, event, and recap,
    both legacy and custom shapes). ``monthly_trend`` is the last twelve
    calendar months of activity, oldest → newest, for the dashboard/pop-up
    line chart. All numbers come from the same
    :func:`recaps.tenant_overview.tenant_kpi_totals` source of truth the
    text overview uses, so the chart and the prose can never disagree.
    """

    events: int
    recaps: int
    consumers_reached: int
    samples_distributed: int
    products_sold: int
    cans_sold: int
    packs_sold: int
    total_engagements: int
    first_time_consumers: int
    brand_aware_consumers: int
    willing_to_purchase: int
    monthly_trend: list[TenantKpiMonth]


def _zeroed_tenant_kpis() -> TenantKpis:
    """An all-zero :class:`TenantKpis` with an empty trend.

    The degradation value the ``tenantKpis`` resolver returns when the
    tenant is missing or out of scope — the resolver NEVER raises, matching
    the rest of the report surface.
    """
    return TenantKpis(
        events=0,
        recaps=0,
        consumers_reached=0,
        samples_distributed=0,
        products_sold=0,
        cans_sold=0,
        packs_sold=0,
        total_engagements=0,
        first_time_consumers=0,
        brand_aware_consumers=0,
        willing_to_purchase=0,
        monthly_trend=[],
    )


def _build_tenant_kpis(tenant_id: int, year: int | None = None) -> TenantKpis:
    """Assemble the structured :class:`TenantKpis` for one tenant.

    Synchronous Django ORM (the resolver wraps it in ``sync_to_async``).
    Pulls the headline counts, the nine summable KPIs, and the monthly
    trend from the shared helpers in :mod:`recaps.tenant_overview` so the
    figures match the plaintext overview exactly.

    ``year=None`` rolls up the tenant's WHOLE history (all-time totals +
    the trailing-twelve-month trend). ``year=Y`` restricts every figure to
    calendar year ``Y`` (and the trend to that year's months), passed
    straight through to the three shared helpers.
    """
    event_count, recap_count = tenant_event_recap_counts(tenant_id, year)
    totals = tenant_kpi_totals(tenant_id, year)
    trend = tenant_monthly_trend(tenant_id, year)
    return TenantKpis(
        events=event_count,
        recaps=recap_count,
        consumers_reached=totals.consumers_reached,
        samples_distributed=totals.samples_distributed,
        products_sold=totals.products_sold,
        cans_sold=totals.cans_sold,
        packs_sold=totals.packs_sold,
        total_engagements=totals.total_engagements,
        first_time_consumers=totals.first_time_consumers,
        brand_aware_consumers=totals.brand_aware_consumers,
        willing_to_purchase=totals.willing_to_purchase,
        monthly_trend=[
            TenantKpiMonth(
                month=m.month,
                recaps=m.recaps,
                engagements=m.engagements,
                samples=m.samples,
            )
            for m in trend
        ],
    )


@strawberry.type
class TenantKpiTotals:
    """A lean, period-scopable KPI roll-up for ONE tenant over ONE window.

    The building block of :class:`TenantKpiComparison`'s two periods. Carries
    the same headline counts + nine summable KPIs as :class:`TenantKpis` but
    WITHOUT the monthly trend (a comparison shows two discrete periods, not a
    series). The figures come from the same
    :func:`recaps.tenant_overview` source of truth the year-scoped
    ``tenantKpis`` uses, so a period here reconciles with that roll-up over a
    matching span. The frontend computes the % deltas between the two periods.
    """

    events: int
    recaps: int
    consumers_reached: int
    samples_distributed: int
    products_sold: int
    cans_sold: int
    packs_sold: int
    total_engagements: int
    first_time_consumers: int
    brand_aware_consumers: int
    willing_to_purchase: int


@strawberry.type
class TenantKpiComparison:
    """"This period vs last" — two COMPLETE periods of a tenant's KPIs.

    The backend answer to month-over-month (and quarter-/year-over-) deltas
    that the year-only ``tenantKpis`` can't express alone: it returns BOTH
    the most recent COMPLETE period of the requested granularity and the
    complete period immediately before it, each a full
    :class:`TenantKpiTotals`, leaving the % deltas to the frontend.

    ``period`` echoes the requested granularity (``"month"`` / ``"quarter"``
    / ``"year"``). ``current_label`` / ``previous_label`` are human labels
    ("May 2026" vs "Apr 2026"; "Q2 2026" vs "Q1 2026"; "2025" vs "2024").

    COMPLETE periods only: ``current`` is the most recent period that has
    fully ELAPSED — never the in-progress month/quarter/year, which would
    manufacture a false drop — and ``previous`` is the complete period before
    it (see :func:`recaps.tenant_overview.tenant_kpi_comparison`).
    """

    period: str
    current_label: str
    previous_label: str
    current: TenantKpiTotals
    previous: TenantKpiTotals


def _kpi_totals_from_dict(data: dict) -> TenantKpiTotals:
    """Map one period's plain-dict roll-up onto the :class:`TenantKpiTotals` type.

    The dict is what
    :func:`recaps.tenant_overview.tenant_kpi_comparison` puts in each period
    slot (``events`` / ``recaps`` + the nine KPI keys); every value is
    coerced to ``int`` (defaulting to 0) so a missing key can never raise.
    """
    return TenantKpiTotals(
        events=int(data.get("events", 0) or 0),
        recaps=int(data.get("recaps", 0) or 0),
        consumers_reached=int(data.get("consumers_reached", 0) or 0),
        samples_distributed=int(data.get("samples_distributed", 0) or 0),
        products_sold=int(data.get("products_sold", 0) or 0),
        cans_sold=int(data.get("cans_sold", 0) or 0),
        packs_sold=int(data.get("packs_sold", 0) or 0),
        total_engagements=int(data.get("total_engagements", 0) or 0),
        first_time_consumers=int(data.get("first_time_consumers", 0) or 0),
        brand_aware_consumers=int(data.get("brand_aware_consumers", 0) or 0),
        willing_to_purchase=int(data.get("willing_to_purchase", 0) or 0),
    )


def _build_tenant_kpi_comparison(
    tenant_id: int, period: str
) -> TenantKpiComparison:
    """Assemble the :class:`TenantKpiComparison` for one tenant + granularity.

    Synchronous Django ORM (the resolver wraps it in ``sync_to_async``).
    Delegates the complete-period selection + both roll-ups to
    :func:`recaps.tenant_overview.tenant_kpi_comparison`, then mirrors the
    returned dict onto the Strawberry types. The builder normalises an
    unknown ``period`` to ``"month"``, so ``data["period"]`` is always one of
    the valid granularities.
    """
    data = tenant_kpi_comparison(tenant_id, period)
    return TenantKpiComparison(
        period=data["period"],
        current_label=data["current_label"],
        previous_label=data["previous_label"],
        current=_kpi_totals_from_dict(data["current"]),
        previous=_kpi_totals_from_dict(data["previous"]),
    )


@strawberry.type
class MarketPerformance:
    """One US state's KPI roll-up for the geographic performance heatmap.

    The per-state companion to :class:`TenantKpis`: instead of one
    tenant-wide total, the ``tenantMarketPerformance`` query returns a list
    of these — one per state the tenant has activity in — so the frontend
    can color a US map. ``state`` is the 2-letter code
    (``events.models.State.code``, e.g. ``"CA"``); the counts and the four
    summable KPIs come from the same source of truth as
    :class:`TenantKpis`, aggregated across BOTH recap shapes but GROUPED BY
    the event's state (see
    :func:`recaps.tenant_overview.tenant_market_performance`).
    """

    state: str
    event_count: int
    recap_count: int
    consumers_reached: int
    samples_distributed: int
    products_sold: int
    total_engagements: int


def _build_market_performance(
    tenant_id: int, year: int | None = None
) -> list[MarketPerformance]:
    """Map the per-state dicts to :class:`MarketPerformance` GraphQL rows.

    Synchronous Django ORM (the resolver wraps it in ``sync_to_async``).
    Delegates the aggregation to
    :func:`recaps.tenant_overview.tenant_market_performance` so the numbers
    share the tenant KPI source of truth, then mirrors each dict field-for-
    field onto the strawberry type.
    """
    return [
        MarketPerformance(
            state=row["state"],
            event_count=row["event_count"],
            recap_count=row["recap_count"],
            consumers_reached=row["consumers_reached"],
            samples_distributed=row["samples_distributed"],
            products_sold=row["products_sold"],
            total_engagements=row["total_engagements"],
        )
        for row in tenant_market_performance(tenant_id, year)
    ]


@strawberry.type
class MetroWeekCell:
    """One metro market's KPI roll-up for one ISO week, within a
    :class:`MetroWeek` row of a :class:`TenantMetroBreakdown`.
    """

    metro: str
    event_count: int
    recap_count: int
    consumers_reached: int
    samples_distributed: int
    products_sold: int
    total_engagements: int


@strawberry.type
class MetroWeek:
    """One ISO week's per-metro roll-up rows, within a
    :class:`TenantMetroBreakdown`.
    """

    iso_year: int
    iso_week: int
    week_start: str  # ISO date (YYYY-MM-DD) — the Monday of this ISO week.
    cells: list[MetroWeekCell]


@strawberry.type
class TenantMetroBreakdown:
    """Week-by-metro-market KPI breakdown for tenants whose events follow
    the "<Market> — <Corridor> · <date>" naming convention — Feel Free's
    Guerrilla Field Sampling program, which runs identical weekly shifts
    across several metro markets with no structured city/market field to
    group by (see :func:`recaps.tenant_overview.tenant_metro_breakdown`).

    Deliberately distinct from :class:`MarketPerformance` (US state) and
    the dashboard's retailer-keyed ``marketAnalysis`` — "market" already
    means two other things in this schema, so this type/query is named
    "metro" throughout to avoid colliding with either.

    ``metros`` is every distinct metro label found in the requested window,
    sorted; EMPTY when the tenant's events in-window don't follow the
    naming convention — the frontend hides the whole section on an empty
    list, so this doubles as "does this feature apply to this tenant."
    ``weeks`` is sorted oldest-to-newest.
    """

    metros: list[str]
    weeks: list[MetroWeek]


def _build_metro_breakdown(
    tenant_id: int,
    start: datetime,
    end: datetime,
    event_type_id: int | None = None,
) -> TenantMetroBreakdown:
    """Map :func:`tenant_metro_breakdown`'s dict shape to GraphQL rows."""
    data = tenant_metro_breakdown(tenant_id, start, end, event_type_id)
    weeks = [
        MetroWeek(
            iso_year=w["iso_year"],
            iso_week=w["iso_week"],
            week_start=w["week_start"].isoformat(),
            cells=[
                MetroWeekCell(metro=metro, **kpis)
                for metro, kpis in w["cells"].items()
            ],
        )
        for w in data["weeks"]
    ]
    return TenantMetroBreakdown(metros=data["metros"], weeks=weeks)


@strawberry.type
class BaLeaderboardEntry:
    """One Brand Ambassador's performance row in a tenant's leaderboard.

    The per-BA companion to :class:`TenantKpis` (which rolls a tenant's whole
    program into one total): instead of one row per tenant, the
    ``tenantBaLeaderboard`` query returns one of these per BA who worked for
    the tenant, ranked by performance. Every metric is SCOPED TO THIS TENANT
    (a BA's work for other brands is not counted), aggregated by
    :func:`recaps.tenant_ba_leaderboard.tenant_ba_leaderboard`.

    * ``ba_id`` — the :class:`ambassadors.models.Ambassador` pk.
    * ``name`` — "First Last" (else email, else a placeholder).
    * ``shifts_worked`` — tenant roster rows
      (:class:`ambassadors.models.AmbassadorEvent`).
    * ``recaps_filed`` — legacy + custom recaps the BA filed for the tenant.
    * ``avg_rating`` — mean 1-5 gig rating, or ``null`` when the BA is unrated.
    * ``ratings_count`` — number of ratings behind ``avg_rating`` (0 when unrated).
    * ``reliability_pct`` — on-time %, or ``null``. Always ``null`` today: the
      attendance data does not cleanly support it (see
      :data:`recaps.tenant_ba_leaderboard.RELIABILITY_SUPPORTED`); the field is
      carried so a clean signal can light it up later with no schema change.
    """

    ba_id: strawberry.ID
    name: str
    shifts_worked: int
    recaps_filed: int
    avg_rating: float | None
    ratings_count: int
    reliability_pct: int | None = None


def _build_ba_leaderboard(
    tenant_id: int, year: int | None = None
) -> list[BaLeaderboardEntry]:
    """Map the per-BA leaderboard dicts to :class:`BaLeaderboardEntry` rows.

    Synchronous Django ORM (the resolver wraps it in ``sync_to_async``).
    Delegates the tenant-scoped aggregation + ranking to
    :func:`recaps.tenant_ba_leaderboard.tenant_ba_leaderboard`, then mirrors
    each already-sorted dict field-for-field onto the strawberry type
    (preserving the builder's order).
    """
    return [
        BaLeaderboardEntry(
            ba_id=strawberry.ID(str(row["ba_id"])),
            name=row["name"],
            shifts_worked=row["shifts_worked"],
            recaps_filed=row["recaps_filed"],
            avg_rating=row["avg_rating"],
            ratings_count=row["ratings_count"],
            reliability_pct=row["reliability_pct"],
        )
        for row in tenant_ba_leaderboard(tenant_id, year)
    ]


# The headline KPIs that have a per-client annual target on
# ``tenants.models.TenantGoal``. Each tuple is
# ``(metric_key, human_label, TenantGoal target field, TenantKpiTotals
# current field)``. ``metric_key`` and ``current`` field share the same
# name as the matching ``TenantKpiTotals`` attribute, so the "current"
# actual is read straight off the year-filtered totals — keeping the
# goal items and the ``tenantKpis`` roll-up on the same source of truth.
#
# Only these four headline KPIs are goal-tracked because ``TenantGoal``
# stores exactly these four target columns (and ``setTenantGoals`` writes
# exactly these four). The other headline KPIs in ``tenant_kpi_totals``
# (first_time_consumers, brand_aware_consumers, willing_to_purchase) have
# no target column and so are not part of the pace-to-target view.
_TENANT_GOAL_METRICS: tuple[tuple[str, str, str, str], ...] = (
    (
        "consumers_reached",
        "Consumers reached",
        "target_consumers_reached",
        "consumers_reached",
    ),
    (
        "samples_distributed",
        "Samples distributed",
        "target_samples_distributed",
        "samples_distributed",
    ),
    (
        "products_sold",
        "Products sold",
        "target_products_sold",
        "products_sold",
    ),
    (
        "total_engagements",
        "Total engagements",
        "target_total_engagements",
        "total_engagements",
    ),
)


@strawberry.type
class TenantGoalItem:
    """Pace-to-target for ONE headline KPI in a given year.

    ``metric`` is the machine key (matches the ``TenantKpiTotals`` field);
    ``label`` is the human-readable name for the UI. ``target`` is the
    client's annual goal for this KPI (0 when none is set) and ``current``
    is the live actual for the same year (from
    :func:`recaps.tenant_overview.tenant_kpi_totals`). ``pace_pct`` is
    ``current / target * 100`` rounded to one decimal, or ``0.0`` when no
    target is set (target ``<= 0``) — it is NOT capped at 100, so a beaten
    goal reads above 100%.
    """

    metric: str
    label: str
    target: int
    current: int
    pace_pct: float


@strawberry.type
class TenantGoals:
    """A client's pace-to-target across the goal-tracked headline KPIs.

    ``year`` is the calendar year the targets and actuals are scoped to;
    ``items`` holds one :class:`TenantGoalItem` per goal-tracked headline
    KPI (see :data:`_TENANT_GOAL_METRICS`). The resolver NEVER raises — a
    missing/out-of-scope tenant resolves to ``TenantGoals(year, items=[])``,
    matching the rest of the report surface.
    """

    year: int
    items: list[TenantGoalItem]


def _pace_pct(current: int, target: int) -> float:
    """``current / target * 100`` to 1 dp, or ``0.0`` when target ``<= 0``.

    Uncapped on purpose: a client that beat its goal should read above
    100% rather than be clamped.
    """
    if target <= 0:
        return 0.0
    return round(current / target * 100, 1)


def _build_tenant_goals(tenant_id: int, year: int) -> TenantGoals:
    """Assemble :class:`TenantGoals` for one tenant + calendar year.

    Synchronous Django ORM (the resolver wraps it in ``sync_to_async``).
    Loads the (tenant, year) :class:`tenants.models.TenantGoal` — a MISSING
    row is treated as all-zero targets, never an error — and the live
    actuals for that same year via
    :func:`recaps.tenant_overview.tenant_kpi_totals`, then builds one
    :class:`TenantGoalItem` per goal-tracked headline KPI.
    """
    from tenants.models import TenantGoal

    goal = TenantGoal.objects.filter(tenant_id=tenant_id, year=year).first()
    totals = tenant_kpi_totals(tenant_id, year=year)

    items: list[TenantGoalItem] = []
    for metric, label, target_field, current_field in _TENANT_GOAL_METRICS:
        target = int(getattr(goal, target_field, 0) or 0) if goal else 0
        current = int(getattr(totals, current_field, 0) or 0)
        items.append(
            TenantGoalItem(
                metric=metric,
                label=label,
                target=target,
                current=current,
                pace_pct=_pace_pct(current, target),
            )
        )
    return TenantGoals(year=year, items=items)


@strawberry.type
class TenantInsightItem:
    """One proactive "what's notable" insight for a client's dashboard.

    ``title`` is a short headline and ``detail`` a single-sentence
    explanation. ``sentiment`` is one of ``"positive"`` / ``"neutral"`` /
    ``"attention"`` (good / neutral / needs-attention), letting the frontend
    pick a badge colour. ``metric`` is an OPTIONAL short headline figure
    (e.g. ``"12,400"`` or ``"▲ 12% vs Apr"``), null when no single figure
    fits.

    ``key`` is the OPTIONAL stable bucket identifier (``"reach"``,
    ``"sampling"``, ``"sales"``, ``"new_audience"``, ``"momentum"``) the
    deterministic builder emits, so the frontend can pin an icon/order to a
    bucket; it is null for any legacy item that predates the fixed buckets.
    """

    title: str
    detail: str
    sentiment: str
    metric: str | None = None
    key: str | None = None


@strawberry.type
class TenantInsights:
    """Deterministic, computed-live proactive insights for ONE client's program.

    Surfaced on the dashboard WITHOUT the user asking: a short, FIXED list of
    headline buckets (reach, sampling, sales, new audience, momentum) computed
    from the same aggregated numbers as :class:`TenantKpis` (see
    :func:`recaps.tenant_insights.build_insight_buckets`). No AI call — the
    buckets are templated and reconcile exactly with the live ``tenantKpis``
    charts, so the cards and the charts can never disagree.

    ``generated_at`` is the ISO-8601 timestamp the buckets were computed (now),
    or null when degraded. ``items`` is the (possibly empty) list of insights.
    The resolver NEVER raises — a missing/out-of-scope tenant or any failure
    resolves to ``TenantInsights(generated_at=None, items=[])``.
    """

    generated_at: str | None
    items: list[TenantInsightItem]


def _empty_tenant_insights() -> TenantInsights:
    """The degradation value: no timestamp, no items, never an error."""
    return TenantInsights(generated_at=None, items=[])


def _build_tenant_insights_type(
    items: list[dict], generated_at
) -> TenantInsights:
    """Map the deterministic bucket dicts + timestamp onto the Strawberry type.

    Defensive: each item is only surfaced when it has a string ``title`` and
    ``detail`` (the builder produces clean dicts, but we never trust the shape
    blindly). ``key`` is carried through when present (one of the stable bucket
    identifiers), null otherwise. ``generated_at`` is rendered as ISO-8601, or
    null when absent.
    """
    out: list[TenantInsightItem] = []
    for item in items or []:
        if not isinstance(item, dict):
            continue
        title = item.get("title")
        detail = item.get("detail")
        if not isinstance(title, str) or not isinstance(detail, str):
            continue
        sentiment = item.get("sentiment")
        if not isinstance(sentiment, str):
            sentiment = "neutral"
        metric = item.get("metric")
        if not isinstance(metric, str):
            metric = None
        key = item.get("key")
        if not isinstance(key, str):
            key = None
        out.append(
            TenantInsightItem(
                title=title,
                detail=detail,
                sentiment=sentiment,
                metric=metric,
                key=key,
            )
        )
    return TenantInsights(
        generated_at=generated_at.isoformat() if generated_at else None,
        items=out,
    )


@strawberry.type
class SentimentTheme:
    """One recurring theme in a tenant's consumer feedback.

    ``label`` is a short human-readable theme (e.g. ``"Loved the flavor"``);
    ``tone`` is one of ``"positive"`` / ``"neutral"`` / ``"negative"`` so the
    frontend can colour it.
    """

    label: str
    tone: str


@strawberry.type
class SentimentQuote:
    """One representative consumer quote, selected VERBATIM from real feedback.

    ``text`` is the quote word-for-word as a consumer gave it (the backend
    drops any quote that isn't present in the gathered feedback, so this is
    never fabricated); ``tone`` is one of ``"positive"`` / ``"neutral"`` /
    ``"negative"``.
    """

    text: str
    tone: str


@strawberry.type
class TenantSentiment:
    """"What people are saying" — AI-summarized consumer sentiment for a client.

    A compact read on how CONSUMERS reacted across a tenant's activations,
    distilled by OpenAI from the free-text feedback on the tenant's recaps and
    cached daily (see :func:`recaps.tenant_sentiment.build_tenant_sentiment`).
    The resolver returns ``null`` (not this type) when there isn't enough
    feedback to summarize or the service is unconfigured/failed, so a populated
    ``TenantSentiment`` always reflects real data.

    * ``overallSentiment`` — ``"positive"`` / ``"mixed"`` / ``"negative"``.
    * ``positivePct`` — estimated share of positive feedback (0-100).
    * ``summary`` — a 1-2 sentence plain-language summary.
    * ``themes`` — up to five recurring themes (label + tone).
    * ``quotes`` — up to three verbatim consumer quotes (text + tone).
    * ``sampleSize`` — how many feedback snippets the summary was built from.
    * ``generatedAt`` — when the cached snapshot was generated (null if absent).
    """

    overall_sentiment: str
    positive_pct: int
    summary: str
    themes: list[SentimentTheme]
    quotes: list[SentimentQuote]
    sample_size: int
    generated_at: datetime | None


def _build_tenant_sentiment_type(
    payload: dict, sample_size: int, generated_at
) -> TenantSentiment | None:
    """Map a cleaned sentiment payload + metadata onto the Strawberry type.

    Defensive even though :func:`recaps.tenant_sentiment.build_tenant_sentiment`
    already cleans the payload: a non-dict payload or one missing a string
    ``summary`` yields ``None`` (so the query resolves to null rather than a
    half-built object). ``themes`` / ``quotes`` are filtered to well-formed
    entries and each tone falls back to ``"neutral"``; ``overallSentiment``
    falls back to ``"mixed"`` and ``positivePct`` is clamped 0-100.
    """
    if not isinstance(payload, dict):
        return None
    summary = payload.get("summary")
    if not isinstance(summary, str) or not summary.strip():
        return None

    overall = payload.get("overall_sentiment")
    if not isinstance(overall, str) or overall not in ("positive", "mixed", "negative"):
        overall = "mixed"

    try:
        pct = int(payload.get("positive_pct"))
    except (TypeError, ValueError):
        pct = 0
    pct = max(0, min(100, pct))

    def _tone(value) -> str:
        if isinstance(value, str) and value in ("positive", "neutral", "negative"):
            return value
        return "neutral"

    themes: list[SentimentTheme] = []
    for item in payload.get("themes") or []:
        if not isinstance(item, dict):
            continue
        label = item.get("label")
        if not isinstance(label, str) or not label.strip():
            continue
        themes.append(SentimentTheme(label=label.strip(), tone=_tone(item.get("tone"))))

    quotes: list[SentimentQuote] = []
    for item in payload.get("quotes") or []:
        if not isinstance(item, dict):
            continue
        text = item.get("text")
        if not isinstance(text, str) or not text.strip():
            continue
        quotes.append(SentimentQuote(text=text.strip(), tone=_tone(item.get("tone"))))

    return TenantSentiment(
        overall_sentiment=overall,
        positive_pct=pct,
        summary=summary.strip(),
        themes=themes,
        quotes=quotes,
        sample_size=int(sample_size or 0),
        generated_at=generated_at if isinstance(generated_at, datetime) else None,
    )


@strawberry.type
class RecapQualityFlag:
    """One quality problem found on a recap.

    * ``code`` — a stable machine code (e.g. ``"no_photos"``,
      ``"sold_exceeds_sampled"``) the frontend can switch on / localize.
    * ``label`` — a human-readable description safe to show as-is.
    * ``severity`` — ``"high"`` / ``"medium"`` / ``"low"``.
    """

    code: str
    label: str
    severity: str


@strawberry.type
class RecapQualityResult:
    """The quality read for one recap: a 0-100 score plus the flags raised.

    ``score`` is ``100`` minus the summed per-severity penalties (floored at
    ``0``); ``100`` with an empty ``flags`` list means the recap looks clean
    (and is also what the resolver returns on a missing / out-of-scope recap, so
    the frontend never has to special-case an error). Flags are ordered
    worst-severity-first. See :func:`recaps.recap_quality.recap_quality_flags`.
    """

    score: int
    flags: list[RecapQualityFlag]


def _build_recap_quality_type(result: dict) -> RecapQualityResult:
    """Map a :func:`recaps.recap_quality.recap_quality_flags` dict onto the type.

    Defensive even though the producer already returns a well-formed dict: a
    non-dict / missing ``score`` falls back to a clean ``100``; only well-formed
    flag dicts (string ``code`` + ``label`` + ``severity``) are kept.
    """
    if not isinstance(result, dict):
        return RecapQualityResult(score=100, flags=[])

    try:
        score = int(result.get("score"))
    except (TypeError, ValueError):
        score = 100
    score = max(0, min(100, score))

    flags: list[RecapQualityFlag] = []
    for item in result.get("flags") or []:
        if not isinstance(item, dict):
            continue
        code = item.get("code")
        label = item.get("label")
        severity = item.get("severity")
        if not (isinstance(code, str) and code):
            continue
        if not (isinstance(label, str) and label):
            continue
        if severity not in ("high", "medium", "low"):
            severity = "low"
        flags.append(
            RecapQualityFlag(code=code, label=label, severity=severity)
        )

    return RecapQualityResult(score=score, flags=flags)


def _compose_ai_summary_prompt(data: report_service.CampaignReportData) -> str:
    """Render a compact, plain-text view of the report for the LLM.

    Keeps the prompt small — headline identity, the date range, the KPI
    block, the event count, and up to ``_AI_SUMMARY_MAX_QUOTES`` consumer
    quotes. The system prompt forbids inventing figures, so this is the
    only source of numbers the model gets.
    """
    k = data.kpis
    lines = [
        f"Brand: {data.brand_name or 'N/A'}",
        f"Campaign: {data.title or 'N/A'}",
        f"Date range: {data.date_range or 'N/A'}",
        f"Events: {k.events}",
        "",
        "KPIs:",
        f"- Consumers reached: {k.consumers_reached}",
        f"- Samples distributed: {k.samples_distributed}",
        f"- Products sold: {k.products_sold}",
        f"- Cans sold: {k.cans_sold}",
        f"- Packs sold: {k.packs_sold}",
        f"- Total engagements: {k.total_engagements}",
        f"- First-time consumers: {k.first_time_consumers}",
        f"- Brand-aware consumers: {k.brand_aware_consumers}",
        f"- Willing to purchase: {k.willing_to_purchase}",
    ]

    quotes = [q.text for q in data.highlights if q.text][:_AI_SUMMARY_MAX_QUOTES]
    if quotes:
        lines.append("")
        lines.append("Consumer quotes:")
        lines.extend(f'- "{text}"' for text in quotes)

    return "\n".join(lines)


def _to_graphql(data: report_service.CampaignReportData) -> CampaignReport:
    """Map the framework-free dataclass onto the Strawberry types."""
    return CampaignReport(
        request_id=strawberry.ID(str(data.request_id)),
        brand_name=data.brand_name,
        title=data.title,
        date_range=data.date_range,
        generated_at=data.generated_at,
        share_token=make_report_token(data.request_id),
        kpis=CampaignReportKpis(
            events=data.kpis.events,
            recaps=data.kpis.recaps,
            consumers_reached=data.kpis.consumers_reached,
            samples_distributed=data.kpis.samples_distributed,
            products_sold=data.kpis.products_sold,
            cans_sold=data.kpis.cans_sold,
            packs_sold=data.kpis.packs_sold,
            total_engagements=data.kpis.total_engagements,
            first_time_consumers=data.kpis.first_time_consumers,
            brand_aware_consumers=data.kpis.brand_aware_consumers,
            willing_to_purchase=data.kpis.willing_to_purchase,
        ),
        events=[
            CampaignReportEventRow(
                id=strawberry.ID(row.id),
                name=row.name,
                date=row.date,
                location_name=row.location_name,
                city=row.city,
                state=row.state,
                coordinates=row.coordinates,
                recap_count=row.recap_count,
            )
            for row in data.events
        ],
        photos=[
            CampaignReportPhoto(url=p.url, caption=p.caption)
            for p in data.photos
        ],
        ambassadors=[
            CampaignReportBa(
                name=ba.name,
                is_external=ba.is_external,
                event_count=ba.event_count,
            )
            for ba in data.ambassadors
        ],
        highlights=[
            CampaignReportQuote(text=q.text, source=q.source)
            for q in data.highlights
        ],
    )


class _CampaignReportService(SparkGraphQLMixin):
    """Tenant-scoping shell. Same posture as receipts: clients pinned to
    their own tenant, admins pass through."""

    async def resolve_scope_tenant_id(self, info: strawberry.Info) -> int | None:
        """Return the tenant id to scope by, or None for an unrestricted
        admin (sees any tenant's request)."""
        user = await self.get_user(info)
        role_slug = self.get_role_slug(user)
        if role_slug == "client":
            tenant = await self.get_user_tenant(info)
            return tenant.id
        return None

    async def resolve_target_tenant_id(
        self, info: strawberry.Info, requested_tenant_id
    ) -> int | None:
        """Resolve the CONCRETE tenant id to aggregate over, or None.

        Unlike :meth:`resolve_scope_tenant_id` (single-record lookups,
        where None means "no restriction"), the tenant overview needs one
        explicit tenant to roll up. Scoping per the spec:

        * **Client role** — always pinned to their OWN tenant; the
          ``requested_tenant_id`` argument is ignored/overridden so a
          client can never aggregate another brand's data.
        * **Admins** (spark-admin / staff / superuser /
          ``@igniteproductions.co``) — may target ANY tenant via
          ``requested_tenant_id``.

        Returns None when the caller is an admin who passed no usable
        tenant id (the resolver turns that into a degradation reason),
        rather than raising.
        """
        user = await self.get_user(info)
        role_slug, is_staff, is_super, email = await resolve_request_user_access(
            user
        )

        if not _is_admin_access(role_slug, is_staff, is_super, email):
            # Non-admins (clients) are pinned to their own tenant, full stop.
            tenant = await self.get_user_tenant(info)
            return tenant.id

        # Admin: honor the requested tenant id (global id or int).
        if requested_tenant_id is None:
            return None
        raw = str(requested_tenant_id).strip()
        if not raw:
            return None
        try:
            return resolve_id_to_int(raw)
        except Exception:
            return None


@strawberry.type
class SkuTotal:
    """One SKU's total within a :class:`SkuBreakdown` — see that type's
    ``mode`` for whether ``total`` is a real unit count or a session count.
    """

    product: str
    total: int


@strawberry.type
class SkuBreakdown:
    """Per-SKU sample totals for one tenant/window (see
    :func:`recaps.field_sampling_report.sku_breakdown`).

    ``mode`` tells you what ``items[].total`` actually counts:

    * ``"quantity"`` — real summed unit counts (the tenant's template logs
      per-SKU quantities).
    * ``"sessions"`` — a FALLBACK: how many distinct recap sessions
      selected that SKU via a "which products were sampled" choice field
      — NOT a unit count. Shown differently in the UI so it's never
      mistaken for real volume.
    * ``"none"`` — neither mechanism has any data in the window.
    """

    mode: str
    items: list[SkuTotal]


@strawberry.type
class SamplesPerHour:
    """Total samples ÷ total labor hours for one tenant/window (see
    :func:`recaps.field_sampling_report.samples_per_hour`). ``estimated``
    is True when any contributing shift had no real clock-in/out pair and
    fell back to its scheduled duration (mirrors ``events.pnl``'s
    per-event flag).
    """

    samples: int
    hours: float
    per_hour: float | None
    estimated: bool


@strawberry.type
class LocationVisit:
    """One stop actually run in-window, within
    :class:`FieldSamplingReport.locations_hit`.
    """

    market: str | None
    corridor: str | None
    date: str | None
    address: str | None


@strawberry.type
class UpcomingShift:
    """One scheduled-but-not-yet-run shift, within :class:`UpcomingShifts`."""

    market: str | None
    corridor: str | None
    name: str | None
    start_time: str | None
    address: str | None


@strawberry.type
class UpcomingShifts:
    """The tenant's next 7 days of scheduled shifts (see
    :func:`recaps.field_sampling_report.upcoming_shifts`). ``total`` is the
    real count; ``items`` may be a capped prefix of it.
    """

    total: int
    items: list[UpcomingShift]


@strawberry.type
class FieldCallout:
    """One free-text BA note/feedback snippet, within
    :class:`FieldSamplingReport.callouts` — the deterministic, no-AI
    "things to note" feed (see
    :func:`recaps.field_sampling_report.field_callouts`).
    """

    market: str | None
    corridor: str | None
    date: str | None
    text: str


@strawberry.type
class FieldSamplingReport:
    """Consolidated Field Sampling Report for one tenant/window — samples
    per hour, YTD + selected-window SKU breakdowns, locations hit, the
    next 7 days' upcoming shifts, and the deterministic call-outs feed.
    See :func:`recaps.field_sampling_report.build_field_sampling_report`.

    Built for programs like Feel Free's Guerrilla Field Sampling, which
    run across metro markets with no structured city field to group by —
    same "metro" derivation as :class:`TenantMetroBreakdown`.
    """

    samples_per_hour: SamplesPerHour
    ytd_sku_breakdown: SkuBreakdown
    week_sku_breakdown: SkuBreakdown
    locations_hit: list[LocationVisit]
    upcoming: UpcomingShifts
    callouts: list[FieldCallout]


def _empty_field_sampling_report() -> FieldSamplingReport:
    """The degrade-to shape for a missing/out-of-scope tenant, an
    unparseable date, or any aggregation error — never a GraphQL error.
    """
    return FieldSamplingReport(
        samples_per_hour=SamplesPerHour(
            samples=0, hours=0.0, per_hour=None, estimated=False
        ),
        ytd_sku_breakdown=SkuBreakdown(mode="none", items=[]),
        week_sku_breakdown=SkuBreakdown(mode="none", items=[]),
        locations_hit=[],
        upcoming=UpcomingShifts(total=0, items=[]),
        callouts=[],
    )


def _build_field_sampling_report_type(
    tenant_id: int,
    start: datetime,
    end: datetime,
    event_type_id: int | None = None,
    market: str | None = None,
) -> FieldSamplingReport:
    """Map :func:`recaps.field_sampling_report.build_field_sampling_report`'s
    dict shape to GraphQL rows.
    """
    data = build_field_sampling_report(tenant_id, start, end, event_type_id, market)
    return FieldSamplingReport(
        samples_per_hour=SamplesPerHour(**data["samples_per_hour"]),
        ytd_sku_breakdown=SkuBreakdown(
            mode=data["ytd_sku_breakdown"]["mode"],
            items=[SkuTotal(**i) for i in data["ytd_sku_breakdown"]["items"]],
        ),
        week_sku_breakdown=SkuBreakdown(
            mode=data["week_sku_breakdown"]["mode"],
            items=[SkuTotal(**i) for i in data["week_sku_breakdown"]["items"]],
        ),
        locations_hit=[LocationVisit(**loc) for loc in data["locations_hit"]],
        upcoming=UpcomingShifts(
            total=data["upcoming"]["total"],
            items=[UpcomingShift(**item) for item in data["upcoming"]["items"]],
        ),
        callouts=[FieldCallout(**c) for c in data["callouts"]],
    )


@strawberry.type
class CampaignReportQueries:
    @strawberry.field(permission_classes=[StrictIsAuthenticated])
    async def campaign_report(
        self,
        info: strawberry.Info,
        request_id: strawberry.ID,
    ) -> CampaignReport | None:
        """Aggregate campaign report for one Request.

        Tenant-scoped: client-role users only see their own tenant's
        requests; admins see any. Returns ``null`` when the request
        doesn't exist or is out of scope.
        """
        identifier = str(request_id).strip()
        if not identifier:
            return None

        service = _CampaignReportService()
        scope_tenant_id = await service.resolve_scope_tenant_id(info)
        generated_at = timezone.now().isoformat()

        def _build():
            request_obj = report_service.get_report_request(
                identifier, tenant_id=scope_tenant_id
            )
            if request_obj is None:
                return None
            return report_service.build_campaign_report(
                request_obj, generated_at=generated_at
            )

        data = await sync_to_async(_build, thread_sensitive=True)()
        if data is None:
            return None
        return _to_graphql(data)

    @strawberry.field(permission_classes=[StrictIsAuthenticated])
    async def campaign_report_ai_summary(
        self,
        info: strawberry.Info,
        request_id: strawberry.ID,
    ) -> CampaignReportAiSummary:
        """On-demand AI executive summary for one Request's campaign report.

        Tenant-scoped exactly like :meth:`campaign_report` (uuid or pk,
        clients pinned to their own tenant, admins pass through). Builds
        the aggregate report, composes a prompt, and calls OpenAI.

        Never raises: an out-of-scope/missing request, an unconfigured
        ``OPENAI_API_KEY``, or any upstream failure all resolve to
        ``ok=false`` + ``summary=""`` + a human-readable ``reason``.
        """
        identifier = str(request_id).strip()
        if not identifier:
            return CampaignReportAiSummary(
                ok=False, summary="", reason="A request id is required."
            )

        service = _CampaignReportService()
        scope_tenant_id = await service.resolve_scope_tenant_id(info)
        generated_at = timezone.now().isoformat()

        def _build():
            request_obj = report_service.get_report_request(
                identifier, tenant_id=scope_tenant_id
            )
            if request_obj is None:
                return None
            return report_service.build_campaign_report(
                request_obj, generated_at=generated_at
            )

        try:
            data = await sync_to_async(_build, thread_sensitive=True)()
        except Exception:
            return CampaignReportAiSummary(
                ok=False,
                summary="",
                reason="Could not load the campaign report.",
            )

        if data is None:
            return CampaignReportAiSummary(
                ok=False,
                summary="",
                reason="Report not found or not accessible.",
            )

        user_prompt = _compose_ai_summary_prompt(data)
        try:
            summary = await sync_to_async(generate_summary, thread_sensitive=True)(
                _AI_SUMMARY_SYSTEM_PROMPT, user_prompt
            )
        except AiUnavailable as exc:
            return CampaignReportAiSummary(ok=False, summary="", reason=str(exc))
        except Exception:
            # Belt-and-suspenders: generate_summary already funnels every
            # failure through AiUnavailable, but never let an unexpected
            # error escape the resolver.
            return CampaignReportAiSummary(
                ok=False,
                summary="",
                reason="The AI summary could not be generated.",
            )

        return CampaignReportAiSummary(ok=True, summary=summary, reason=None)

    @strawberry.field(permission_classes=[StrictIsAuthenticated])
    async def campaign_report_ai_answer(
        self,
        info: strawberry.Info,
        request_id: strawberry.ID,
        question: str,
    ) -> CampaignReportAiAnswer:
        """On-demand freeform Q&A over one Request's campaign report.

        Tenant-scoped exactly like :meth:`campaign_report_ai_summary` (uuid
        or pk, clients pinned to their own tenant, admins pass through).
        Builds the aggregate report, appends the caller's ``question`` to
        the same compact report prompt, and calls OpenAI.

        Never raises: an empty question, an out-of-scope/missing request,
        an unconfigured ``OPENAI_API_KEY``, or any upstream failure all
        resolve to ``ok=false`` + ``answer=""`` + a human-readable
        ``reason``.
        """
        identifier = str(request_id).strip()
        if not identifier:
            return CampaignReportAiAnswer(
                ok=False, answer="", reason="A request id is required."
            )

        question = (question or "").strip()
        if not question:
            return CampaignReportAiAnswer(
                ok=False, answer="", reason="A question is required."
            )
        question = question[:_AI_ANSWER_MAX_QUESTION_CHARS]

        service = _CampaignReportService()
        scope_tenant_id = await service.resolve_scope_tenant_id(info)
        generated_at = timezone.now().isoformat()

        def _build():
            request_obj = report_service.get_report_request(
                identifier, tenant_id=scope_tenant_id
            )
            if request_obj is None:
                return None
            return report_service.build_campaign_report(
                request_obj, generated_at=generated_at
            )

        try:
            data = await sync_to_async(_build, thread_sensitive=True)()
        except Exception:
            return CampaignReportAiAnswer(
                ok=False,
                answer="",
                reason="Could not load the campaign report.",
            )

        if data is None:
            return CampaignReportAiAnswer(
                ok=False,
                answer="",
                reason="Report not found or not accessible.",
            )

        user_prompt = _compose_ai_summary_prompt(data) + "\n\nQuestion: " + question
        try:
            answer, chart = await sync_to_async(
                generate_structured_answer, thread_sensitive=True
            )(_AI_ANSWER_SYSTEM_PROMPT, user_prompt, max_tokens=8000)
        except AiUnavailable as exc:
            return CampaignReportAiAnswer(ok=False, answer="", reason=str(exc))
        except Exception:
            # Belt-and-suspenders: generate_structured_answer already funnels
            # every failure through AiUnavailable (or its text fallback), but
            # never let an unexpected error escape the resolver.
            return CampaignReportAiAnswer(
                ok=False,
                answer="",
                reason="The answer could not be generated.",
            )

        # A missing/garbled chart just yields chart=None — the text answer is
        # always returned (see _build_ai_chart).
        return CampaignReportAiAnswer(
            ok=True, answer=answer, reason=None, chart=_build_ai_chart(chart)
        )

    @strawberry.field(permission_classes=[StrictIsAuthenticated])
    async def tenant_ai_answer(
        self,
        info: strawberry.Info,
        tenant_id: strawberry.ID,
        question: str,
    ) -> TenantAiAnswer:
        """On-demand freeform Q&A over ONE client's aggregated activity.

        The tenant-wide sibling of :meth:`campaign_report_ai_answer`:
        instead of one Request, it builds a compact overview of the whole
        tenant's program (every campaign, event, and recap via
        :func:`recaps.tenant_overview.build_tenant_overview`), appends the
        caller's ``question``, and calls OpenAI.

        Tenant scoping (see :meth:`_CampaignReportService.resolve_target_tenant_id`):
        client-role users may ONLY ask about their own tenant — the
        ``tenant_id`` argument is ignored/overridden to their own; admins
        (spark-admin / staff / superuser / ``@igniteproductions.co``) may
        target any tenant via ``tenant_id``.

        Never raises: an empty question, a missing/out-of-scope tenant, an
        unconfigured ``OPENAI_API_KEY``, or any upstream failure all
        resolve to ``ok=false`` + ``answer=""`` + a human-readable
        ``reason``.
        """
        question = (question or "").strip()
        if not question:
            return TenantAiAnswer(
                ok=False, answer="", reason="A question is required."
            )
        question = question[:_AI_ANSWER_MAX_QUESTION_CHARS]

        service = _CampaignReportService()
        target_tenant_id = await service.resolve_target_tenant_id(info, tenant_id)
        if target_tenant_id is None:
            return TenantAiAnswer(
                ok=False,
                answer="",
                reason="A valid tenant id is required.",
            )

        def _build():
            return build_tenant_overview(target_tenant_id)

        try:
            overview = await sync_to_async(_build, thread_sensitive=True)()
        except Exception:
            # Missing tenant (Tenant.DoesNotExist) or any aggregation error.
            return TenantAiAnswer(
                ok=False,
                answer="",
                reason="Tenant not found or not accessible.",
            )

        user_prompt = overview + "\n\nQuestion: " + question
        try:
            answer, chart = await sync_to_async(
                generate_structured_answer, thread_sensitive=True
            )(_AI_TENANT_ANSWER_SYSTEM_PROMPT, user_prompt, max_tokens=8000)
        except AiUnavailable as exc:
            return TenantAiAnswer(ok=False, answer="", reason=str(exc))
        except Exception:
            # Belt-and-suspenders: generate_structured_answer already funnels
            # every failure through AiUnavailable (or its text fallback), but
            # never let an unexpected error escape the resolver.
            return TenantAiAnswer(
                ok=False,
                answer="",
                reason="The answer could not be generated.",
            )

        # A missing/garbled chart just yields chart=None — the text answer is
        # always returned (see _build_ai_chart).
        return TenantAiAnswer(
            ok=True, answer=answer, reason=None, chart=_build_ai_chart(chart)
        )

    @strawberry.field(permission_classes=[StrictIsAuthenticated])
    async def tenant_kpis(
        self,
        info: strawberry.Info,
        tenant_id: strawberry.ID,
        year: int | None = None,
    ) -> TenantKpis:
        """Structured per-tenant KPI roll-up for dashboard / pop-up charts.

        The visual companion to :meth:`tenant_ai_answer`: same tenant data,
        but returned as numbers (headline counts, the nine summable KPIs,
        and an activity trend) instead of an AI prose answer. Numbers come
        from the shared :func:`recaps.tenant_overview.tenant_kpi_totals`
        source of truth, so they match the text overview exactly.

        The optional ``year`` filter scopes the whole roll-up to one
        calendar year (the dashboard's This-year / specific-year selector):

        * **Omitted / null** — ALL-TIME: every figure spans the tenant's
          whole history and ``monthly_trend`` is the trailing twelve months
          (the original, unchanged behavior the Ask-AI overview and the
          Insights cron also rely on).
        * **A year ``Y``** — every figure is restricted to recaps whose
          ``created_at`` falls in calendar year ``Y`` and ``monthly_trend``
          becomes that year's months (Jan→Dec for a past year, Jan→current
          month for the current year).

        Tenant scoping is identical to :meth:`tenant_ai_answer`
        (:meth:`_CampaignReportService.resolve_target_tenant_id`):
        client-role users may ONLY read their own tenant — the ``tenant_id``
        argument is overridden to their own; admins (spark-admin / staff /
        superuser / ``@igniteproductions.co``) may target any tenant.

        Never raises: a missing or out-of-scope tenant, or any aggregation
        error, resolves to a zeroed :class:`TenantKpis` (empty
        ``monthly_trend``) rather than a GraphQL error.
        """
        service = _CampaignReportService()
        target_tenant_id = await service.resolve_target_tenant_id(info, tenant_id)
        if target_tenant_id is None:
            return _zeroed_tenant_kpis()

        def _build():
            # Guard the tenant's existence the same way build_tenant_overview
            # does (Tenant.objects.get), so an admin passing an unknown id
            # degrades to zeros instead of returning counts over no rows.
            from tenants.models import Tenant

            if not Tenant.objects.filter(id=target_tenant_id).exists():
                return None
            return _build_tenant_kpis(target_tenant_id, year)

        try:
            data = await sync_to_async(_build, thread_sensitive=True)()
        except Exception:
            return _zeroed_tenant_kpis()

        if data is None:
            return _zeroed_tenant_kpis()
        return data

    @strawberry.field(permission_classes=[StrictIsAuthenticated])
    async def tenant_kpi_comparison(
        self,
        info: strawberry.Info,
        tenant_id: strawberry.ID,
        period: str = "month",
    ) -> TenantKpiComparison | None:
        """"This period vs last" KPI deltas for one tenant — both periods.

        The backend companion to :meth:`tenant_kpis`: where that scopes only
        by calendar YEAR (so the frontend can't build a month-over-month
        delta across the full nine-KPI set alone), this returns BOTH the most
        recent COMPLETE period of the requested granularity and the complete
        period immediately before it, each a full :class:`TenantKpiTotals`,
        leaving the % deltas to the frontend.

        ``period`` is ``"month"`` (default), ``"quarter"``, or ``"year"``;
        any other value is treated as ``"month"`` by the underlying builder.

        COMPLETE periods only: ``current`` is the most recent period that has
        fully ELAPSED (never the in-progress current month/quarter/year,
        which would manufacture a false drop), and ``previous`` is the
        complete period before it — e.g. for a mid-June-2026 "now",
        ``month`` → "May 2026" vs "Apr 2026" (see
        :func:`recaps.tenant_overview.tenant_kpi_comparison`).

        Tenant scoping is identical to :meth:`tenant_kpis`
        (:meth:`_CampaignReportService.resolve_target_tenant_id`):
        client-role users may ONLY read their own tenant — the ``tenant_id``
        argument is overridden to their own; admins (spark-admin / staff /
        superuser / ``@igniteproductions.co``) may target any tenant.

        Never raises: a missing or out-of-scope tenant, or any aggregation
        error, resolves to ``null`` rather than a GraphQL error.
        """
        service = _CampaignReportService()
        target_tenant_id = await service.resolve_target_tenant_id(info, tenant_id)
        if target_tenant_id is None:
            return None

        def _build():
            # Guard the tenant's existence the same way tenant_kpis does, so
            # an admin passing an unknown id degrades to null instead of
            # comparing periods over no tenant.
            from tenants.models import Tenant

            if not Tenant.objects.filter(id=target_tenant_id).exists():
                return None
            return _build_tenant_kpi_comparison(target_tenant_id, period)

        try:
            return await sync_to_async(_build, thread_sensitive=True)()
        except Exception:
            return None

    @strawberry.field(permission_classes=[StrictIsAuthenticated])
    async def tenant_market_performance(
        self,
        info: strawberry.Info,
        tenant_id: strawberry.ID,
        year: int | None = None,
    ) -> list[MarketPerformance]:
        """Per-US-state KPI roll-up for the geographic performance heatmap.

        Returns one :class:`MarketPerformance` per state the tenant has
        activity in (rows whose event has no US state are omitted), so the
        frontend can color a map. The counts and four summable KPIs come from
        the same :func:`recaps.tenant_overview` aggregation the
        :meth:`tenant_kpis` roll-up uses, grouped by the event's state.

        The optional ``year`` scopes every figure to one calendar year,
        identical to :meth:`tenant_kpis`:

        * **Omitted / null** — ALL-TIME across the tenant's whole history.
        * **A year ``Y``** — only recaps/events whose ``created_at`` falls in
          calendar year ``Y``.

        Tenant scoping is identical to :meth:`tenant_kpis`
        (:meth:`_CampaignReportService.resolve_target_tenant_id`):
        client-role users may ONLY read their own tenant — the ``tenant_id``
        argument is overridden to their own; admins (spark-admin / staff /
        superuser / ``@igniteproductions.co``) may target any tenant.

        Never raises: a missing or out-of-scope tenant, or any aggregation
        error, resolves to an empty list rather than a GraphQL error.
        """
        service = _CampaignReportService()
        target_tenant_id = await service.resolve_target_tenant_id(info, tenant_id)
        if target_tenant_id is None:
            return []

        def _build():
            # Guard the tenant's existence the same way tenant_kpis does, so
            # an admin passing an unknown id degrades to an empty list instead
            # of aggregating over no tenant.
            from tenants.models import Tenant

            if not Tenant.objects.filter(id=target_tenant_id).exists():
                return None
            return _build_market_performance(target_tenant_id, year)

        try:
            data = await sync_to_async(_build, thread_sensitive=True)()
        except Exception:
            return []

        if data is None:
            return []
        return data

    @strawberry.field(permission_classes=[StrictIsAuthenticated])
    async def tenant_metro_breakdown(
        self,
        info: strawberry.Info,
        tenant_id: strawberry.ID,
        start_date: str,
        end_date: str,
        event_type_id: strawberry.ID | None = None,
    ) -> TenantMetroBreakdown:
        """Week-by-metro-market KPI breakdown for tenants whose events are
        named "<Market> — <Corridor> · <date>" (see
        :func:`recaps.tenant_overview.tenant_metro_breakdown`) — built for
        Feel Free's Guerrilla Field Sampling program, which runs identical
        weekly shifts across several metro markets with no structured
        city/market field to group by.

        ``start_date`` / ``end_date`` are ISO date strings (YYYY-MM-DD);
        the window is INCLUSIVE of both days. ``event_type_id`` optionally
        restricts to one EventType (e.g. Feel Free's "Field Sampling").

        Tenant scoping is identical to :meth:`tenant_kpis`
        (:meth:`_CampaignReportService.resolve_target_tenant_id`).

        Never raises: a missing/out-of-scope tenant, an unparseable date,
        or any aggregation error resolves to an EMPTY breakdown (``metros:
        []``) rather than a GraphQL error — the frontend hides the section
        on an empty ``metros`` list either way.
        """
        empty = TenantMetroBreakdown(metros=[], weeks=[])
        service = _CampaignReportService()
        target_tenant_id = await service.resolve_target_tenant_id(info, tenant_id)
        if target_tenant_id is None:
            return empty

        try:
            start_d = datetime.fromisoformat(start_date).date()
            end_d = datetime.fromisoformat(end_date).date()
        except (ValueError, TypeError):
            return empty

        et_id: int | None = None
        if event_type_id is not None:
            raw = str(event_type_id).strip()
            if raw:
                try:
                    et_id = resolve_id_to_int(raw)
                except Exception:
                    et_id = None

        def _build():
            from tenants.models import Tenant

            if not Tenant.objects.filter(id=target_tenant_id).exists():
                return None
            # Half-open [start, end) window with end_date INCLUSIVE — mirrors
            # tenant_overview._year_bounds' half-open convention, anchored on
            # timezone.now() so both bounds carry the active tzinfo.
            anchor = timezone.now().replace(
                hour=0, minute=0, second=0, microsecond=0
            )
            start_dt = anchor.replace(
                year=start_d.year, month=start_d.month, day=start_d.day
            )
            end_dt = anchor.replace(
                year=end_d.year, month=end_d.month, day=end_d.day
            ) + timedelta(days=1)
            return _build_metro_breakdown(target_tenant_id, start_dt, end_dt, et_id)

        try:
            data = await sync_to_async(_build, thread_sensitive=True)()
        except Exception:
            return empty

        return data if data is not None else empty

    @strawberry.field(permission_classes=[StrictIsAuthenticated])
    async def tenant_field_sampling_report(
        self,
        info: strawberry.Info,
        tenant_id: strawberry.ID,
        start_date: str,
        end_date: str,
        event_type_id: strawberry.ID | None = None,
        market: str | None = None,
    ) -> FieldSamplingReport:
        """Consolidated Field Sampling Report: samples/hour, YTD +
        selected-window SKU breakdowns, locations hit, next-7-days
        upcoming shifts, and the deterministic call-outs feed. See
        :func:`recaps.field_sampling_report.build_field_sampling_report`.

        ``start_date``/``end_date`` are ISO date strings (YYYY-MM-DD)
        scoping the "this window" figures (SKU breakdown, samples/hour,
        locations, call-outs) — INCLUSIVE of both days. ``event_type_id``
        optionally restricts to one EventType; ``market`` optionally
        restricts to one metro label (see
        :func:`recaps.tenant_overview.tenant_metro_breakdown` for how
        metro labels are derived). YTD is always Jan 1 of the current
        year through now; ``upcoming`` is always the real next 7 days from
        now — both independent of ``start_date``/``end_date``/``market``.

        Tenant scoping is identical to :meth:`tenant_kpis`
        (:meth:`_CampaignReportService.resolve_target_tenant_id`).

        Never raises: a missing/out-of-scope tenant, an unparseable date,
        or any aggregation error resolves to an EMPTY report (see
        :func:`_empty_field_sampling_report`) rather than a GraphQL error.
        """
        service = _CampaignReportService()
        target_tenant_id = await service.resolve_target_tenant_id(info, tenant_id)
        if target_tenant_id is None:
            return _empty_field_sampling_report()

        try:
            start_d = datetime.fromisoformat(start_date).date()
            end_d = datetime.fromisoformat(end_date).date()
        except (ValueError, TypeError):
            return _empty_field_sampling_report()

        et_id: int | None = None
        if event_type_id is not None:
            raw = str(event_type_id).strip()
            if raw:
                try:
                    et_id = resolve_id_to_int(raw)
                except Exception:
                    et_id = None

        def _build():
            from tenants.models import Tenant

            if not Tenant.objects.filter(id=target_tenant_id).exists():
                return None
            anchor = timezone.now().replace(
                hour=0, minute=0, second=0, microsecond=0
            )
            start_dt = anchor.replace(
                year=start_d.year, month=start_d.month, day=start_d.day
            )
            end_dt = anchor.replace(
                year=end_d.year, month=end_d.month, day=end_d.day
            ) + timedelta(days=1)
            return _build_field_sampling_report_type(
                target_tenant_id, start_dt, end_dt, et_id, market
            )

        try:
            data = await sync_to_async(_build, thread_sensitive=True)()
        except Exception:
            return _empty_field_sampling_report()

        return data if data is not None else _empty_field_sampling_report()

    @strawberry.field(permission_classes=[StrictIsAuthenticated])
    async def tenant_ba_leaderboard(
        self,
        info: strawberry.Info,
        tenant_id: strawberry.ID,
        year: int | None = None,
    ) -> list[BaLeaderboardEntry]:
        """Per-BA performance leaderboard for ONE tenant.

        Returns one :class:`BaLeaderboardEntry` per Brand Ambassador who
        worked for the tenant — anyone with a recap filed for the tenant's
        events, a roster/shift on them, or a rating on the tenant's gigs —
        ranked by ``avg_rating`` desc (unrated last), then ``recaps_filed``
        desc, then ``shifts_worked`` desc, capped at the builder's top-N.
        EVERY metric is scoped to this tenant (a BA's work for other brands is
        not counted), via
        :func:`recaps.tenant_ba_leaderboard.tenant_ba_leaderboard`.

        The optional ``year`` scopes every metric to one calendar year,
        identical to :meth:`tenant_kpis`:

        * **Omitted / null** — ALL-TIME across the tenant's whole history.
        * **A year ``Y``** — only activity whose ``created_at`` falls in
          calendar year ``Y``.

        Tenant scoping is identical to :meth:`tenant_kpis`
        (:meth:`_CampaignReportService.resolve_target_tenant_id`):
        client-role users may ONLY read their own tenant — the ``tenant_id``
        argument is overridden to their own; admins (spark-admin / staff /
        superuser / ``@igniteproductions.co``) may target any tenant.

        Never raises: a missing or out-of-scope tenant, or any aggregation
        error, resolves to an empty list rather than a GraphQL error.
        """
        service = _CampaignReportService()
        target_tenant_id = await service.resolve_target_tenant_id(info, tenant_id)
        if target_tenant_id is None:
            return []

        def _build():
            # Guard the tenant's existence the same way tenant_kpis does, so
            # an admin passing an unknown id degrades to an empty list instead
            # of aggregating over no tenant.
            from tenants.models import Tenant

            if not Tenant.objects.filter(id=target_tenant_id).exists():
                return None
            return _build_ba_leaderboard(target_tenant_id, year)

        try:
            data = await sync_to_async(_build, thread_sensitive=True)()
        except Exception:
            return []

        if data is None:
            return []
        return data

    @strawberry.field(permission_classes=[StrictIsAuthenticated])
    async def tenant_goals(
        self,
        info: strawberry.Info,
        tenant_id: strawberry.ID,
        year: int | None = None,
    ) -> TenantGoals:
        """Pace-to-target for one client's headline KPIs in a given year.

        Loads the client's annual targets (``tenants.models.TenantGoal``
        for the (tenant, year) pair — a MISSING row is treated as all-zero
        targets) and the live actuals for that same year (via
        :func:`recaps.tenant_overview.tenant_kpi_totals`), and returns one
        :class:`TenantGoalItem` per goal-tracked headline KPI with its
        target, current actual, and pace percentage.

        ``year`` defaults to the CURRENT calendar year when omitted/null.

        Tenant scoping is identical to :meth:`tenant_kpis`
        (:meth:`_CampaignReportService.resolve_target_tenant_id`):
        client-role users may ONLY read their own tenant — the ``tenant_id``
        argument is overridden to their own; admins (spark-admin / staff /
        superuser / ``@igniteproductions.co``) may target any tenant.

        Never raises: a missing or out-of-scope tenant, or any aggregation
        error, resolves to ``TenantGoals(year=<year>, items=[])`` rather
        than a GraphQL error.
        """
        resolved_year = year if year is not None else timezone.now().year

        service = _CampaignReportService()
        target_tenant_id = await service.resolve_target_tenant_id(info, tenant_id)
        if target_tenant_id is None:
            return TenantGoals(year=resolved_year, items=[])

        def _build():
            # Guard the tenant's existence the same way tenant_kpis does, so
            # an admin passing an unknown id degrades to empty items instead
            # of reporting actuals/targets over no tenant.
            from tenants.models import Tenant

            if not Tenant.objects.filter(id=target_tenant_id).exists():
                return None
            return _build_tenant_goals(target_tenant_id, resolved_year)

        try:
            data = await sync_to_async(_build, thread_sensitive=True)()
        except Exception:
            return TenantGoals(year=resolved_year, items=[])

        if data is None:
            return TenantGoals(year=resolved_year, items=[])
        return data

    @strawberry.field(permission_classes=[StrictIsAuthenticated])
    async def tenant_insights(
        self,
        info: strawberry.Info,
        tenant_id: strawberry.ID,
    ) -> TenantInsights:
        """Proactive "what's notable" insights for a client's dashboard.

        A FIXED, deterministic set of headline buckets about the tenant's
        whole program (reach, sampling, sales, new audience, momentum),
        surfaced WITHOUT the user asking. Computed LIVE via
        :func:`recaps.tenant_insights.build_insight_buckets` — there is no AI
        call and no snapshot read — so the buckets stay in lockstep with the
        live ``tenantKpis`` charts (a cached/stale bucket beside a live chart
        would mismatch). ``generated_at`` is simply the time of this read.

        Tenant scoping is identical to :meth:`tenant_kpis`
        (:meth:`_CampaignReportService.resolve_target_tenant_id`):
        client-role users may ONLY read their own tenant — the ``tenant_id``
        argument is overridden to their own; admins (spark-admin / staff /
        superuser / ``@igniteproductions.co``) may target any tenant.

        Never raises: a missing or out-of-scope tenant, or any failure,
        resolves to ``TenantInsights(generated_at=None, items=[])`` rather than
        a GraphQL error.
        """
        service = _CampaignReportService()
        target_tenant_id = await service.resolve_target_tenant_id(info, tenant_id)
        if target_tenant_id is None:
            return _empty_tenant_insights()

        generated_at = timezone.now()

        def _build():
            # Guard the tenant's existence so an admin passing an unknown id
            # degrades to empty rather than computing buckets over no tenant.
            from tenants.models import Tenant

            if not Tenant.objects.filter(id=target_tenant_id).exists():
                return None
            return build_insight_buckets(target_tenant_id)

        try:
            items = await sync_to_async(_build, thread_sensitive=True)()
        except Exception:
            return _empty_tenant_insights()

        if items is None:
            return _empty_tenant_insights()

        return _build_tenant_insights_type(items, generated_at)

    @strawberry.field(permission_classes=[StrictIsAuthenticated])
    async def tenant_sentiment(
        self,
        info: strawberry.Info,
        tenant_id: strawberry.ID,
        year: int | None = None,
    ) -> TenantSentiment | None:
        """"What people are saying" — AI-summarized consumer sentiment.

        Returns a compact read on how CONSUMERS reacted across the tenant's
        activations (overall sentiment, positive %, a one-line summary,
        recurring themes, and a few verbatim quotes), distilled by OpenAI from
        the free-text feedback on the tenant's recaps. Because the read costs an
        AI call it is served from a daily-refreshed cache (see
        :func:`recaps.tenant_sentiment.get_or_refresh_tenant_sentiment`): a
        fresh snapshot is served as-is, otherwise it is regenerated + persisted,
        otherwise the last good snapshot is used.

        The optional ``year`` scopes the summarized feedback to one calendar
        year, identical to :meth:`tenant_kpis`:

        * **Omitted / null** — ALL-TIME across the tenant's whole history.
        * **A year ``Y``** — only feedback whose ``created_at`` falls in
          calendar year ``Y``.

        Tenant scoping is identical to :meth:`tenant_kpis`
        (:meth:`_CampaignReportService.resolve_target_tenant_id`):
        client-role users may ONLY read their own tenant — the ``tenant_id``
        argument is overridden to their own; admins (spark-admin / staff /
        superuser / ``@igniteproductions.co``) may target any tenant.

        Returns ``null`` (not an error) when the tenant is missing/out-of-scope,
        when there is too little feedback to summarize, when the AI service is
        unconfigured/failed, or on any other error — so the frontend can simply
        hide the card. Never raises.
        """
        service = _CampaignReportService()
        target_tenant_id = await service.resolve_target_tenant_id(info, tenant_id)
        if target_tenant_id is None:
            return None

        def _build():
            # Guard the tenant's existence so an admin passing an unknown id
            # degrades to null rather than summarizing over no tenant.
            from tenants.models import Tenant, TenantSentimentSnapshot

            if not Tenant.objects.filter(id=target_tenant_id).exists():
                return None

            # Ensure a fresh (or last-good) snapshot exists, then read the
            # persisted row so we carry the authoritative sample_size. The
            # front door never raises and amortises the AI call across the day.
            payload, _generated_at = get_or_refresh_tenant_sentiment(
                target_tenant_id, year=year
            )
            if payload is None:
                return None
            snapshot = (
                TenantSentimentSnapshot.objects.filter(
                    tenant_id=target_tenant_id, year=year
                )
                .order_by("-generated_at")
                .first()
            )
            if snapshot is None:
                return None
            return _build_tenant_sentiment_type(
                snapshot.payload, snapshot.sample_size, snapshot.generated_at
            )

        try:
            return await sync_to_async(_build, thread_sensitive=True)()
        except Exception:
            return None

    @strawberry.field(permission_classes=[StrictIsAuthenticated])
    async def recap_quality_flags(
        self,
        info: strawberry.Info,
        recap_id: strawberry.ID,
        is_custom: bool = False,
    ) -> RecapQualityResult:
        """Quality-check ONE recap and flag it for review if it looks thin.

        Returns a ``RecapQualityResult`` — a 0-100 ``score`` plus the ``flags``
        raised (missing/low photos, blank feedback, inconsistent or all-zero
        numbers, etc.). Handles BOTH recap shapes: ``isCustom=false`` (the
        default) reads a legacy :class:`recaps.models.Recap`, ``isCustom=true``
        a custom-template :class:`recaps.models.CustomRecap`. The core checks
        are deterministic; when the recap has free-text feedback a small CACHED
        OpenAI pass may add a "thin feedback" flag (see
        :func:`recaps.recap_quality.recap_quality_flags`).

        Tenant scoping matches :meth:`tenant_sentiment`
        (:meth:`_CampaignReportService.resolve_target_tenant_id`): a client-role
        user may ONLY read a recap belonging to their OWN tenant; admins
        (spark-admin / staff / superuser / ``@igniteproductions.co``) may read
        any. A legacy recap's tenant is reached through its event
        (``recap.event.tenant_id``); a custom recap's is its direct tenant FK.

        Degrades to a clean, neutral ``{score: 100, flags: []}`` (NOT an error)
        when the recap is missing, out-of-scope for the caller, or on any
        failure — so the frontend can render the badge without special-casing.
        Never raises.
        """
        service = _CampaignReportService()
        scope_tenant_id = await service.resolve_scope_tenant_id(info)

        try:
            rid = resolve_id_to_int(str(recap_id).strip())
        except Exception:
            return RecapQualityResult(score=100, flags=[])

        def _build() -> RecapQualityResult:
            from recaps.models import CustomRecap, Recap

            # Resolve the recap's tenant from its own shape, then enforce the
            # caller's scope: a client may only read their own tenant's recap;
            # an admin (scope_tenant_id is None) passes through. An
            # out-of-scope or missing recap degrades to the neutral result.
            if is_custom:
                row_tenant_id = (
                    CustomRecap.objects.filter(id=rid)
                    .values_list("tenant_id", flat=True)
                    .first()
                )
            else:
                row_tenant_id = (
                    Recap.objects.filter(id=rid)
                    .values_list("event__tenant_id", flat=True)
                    .first()
                )

            if row_tenant_id is None:
                return RecapQualityResult(score=100, flags=[])
            if scope_tenant_id is not None and row_tenant_id != scope_tenant_id:
                # Client asking about another tenant's recap — treat as
                # not-found rather than leaking its existence.
                return RecapQualityResult(score=100, flags=[])

            result = recap_quality_flags(rid, is_custom=is_custom)
            return _build_recap_quality_type(result)

        try:
            return await sync_to_async(_build, thread_sensitive=True)()
        except Exception:
            return RecapQualityResult(score=100, flags=[])


# Maps each ``setTenantGoals`` mutation argument to its ``TenantGoal``
# column. Only arguments the caller actually provides (non-null) are
# written, so a partial update leaves the other targets untouched.
_SET_TENANT_GOAL_FIELDS: tuple[tuple[str, str], ...] = (
    ("target_consumers_reached", "target_consumers_reached"),
    ("target_samples_distributed", "target_samples_distributed"),
    ("target_products_sold", "target_products_sold"),
    ("target_total_engagements", "target_total_engagements"),
)


@strawberry.type
class TenantGoalsMutations:
    """Mutation surface for per-client KPI targets (the clients schema).

    The write side of the ``tenantGoals`` query: upserts one client's
    annual targets and returns the refreshed pace-to-target view. Tenant
    scoping matches the read resolvers exactly
    (:meth:`_CampaignReportService.resolve_target_tenant_id`): admins may
    target any tenant; clients are pinned to their own.
    """

    @strawberry.mutation(permission_classes=[StrictIsAuthenticated])
    async def set_tenant_goals(
        self,
        info: strawberry.Info,
        tenant_id: strawberry.ID,
        year: int,
        target_consumers_reached: int | None = None,
        target_samples_distributed: int | None = None,
        target_products_sold: int | None = None,
        target_total_engagements: int | None = None,
    ) -> TenantGoals:
        """Upsert one client's annual KPI targets; return the refreshed goals.

        Creates or updates the (tenant, year)
        :class:`tenants.models.TenantGoal`, writing ONLY the targets the
        caller supplies (a null argument leaves that target as-is — newly
        created rows keep the model default of 0 for any target left null).
        Returns the same shape as the ``tenantGoals`` query, recomputed
        against the live actuals for ``year``.

        Tenant scoping (see
        :meth:`_CampaignReportService.resolve_target_tenant_id`):
        client-role users may ONLY write their own tenant — the
        ``tenant_id`` argument is overridden to their own; admins
        (spark-admin / staff / superuser / ``@igniteproductions.co``) may
        target any tenant.

        Degrades like the read side rather than raising: an out-of-scope /
        unusable tenant id, or an unknown tenant, resolves to
        ``TenantGoals(year=<year>, items=[])``.
        """
        service = _CampaignReportService()
        target_tenant_id = await service.resolve_target_tenant_id(info, tenant_id)
        if target_tenant_id is None:
            return TenantGoals(year=year, items=[])

        # Collect only the targets the caller actually provided (non-null),
        # so a partial mutation never clobbers untouched targets.
        provided = {
            field: value
            for (arg, field), value in zip(
                _SET_TENANT_GOAL_FIELDS,
                (
                    target_consumers_reached,
                    target_samples_distributed,
                    target_products_sold,
                    target_total_engagements,
                ),
            )
            if value is not None
        }

        def _upsert():
            from tenants.models import Tenant, TenantGoal

            if not Tenant.objects.filter(id=target_tenant_id).exists():
                return None

            with transaction.atomic():
                goal, _created = TenantGoal.objects.get_or_create(
                    tenant_id=target_tenant_id,
                    year=year,
                )
                if provided:
                    update_fields = ["updated_at"]
                    for field, value in provided.items():
                        setattr(goal, field, value)
                        update_fields.append(field)
                    goal.save(update_fields=update_fields)

            return _build_tenant_goals(target_tenant_id, year)

        try:
            data = await sync_to_async(_upsert, thread_sensitive=True)()
        except Exception:
            return TenantGoals(year=year, items=[])

        if data is None:
            return TenantGoals(year=year, items=[])
        return data


# ── Scheduled monthly client-report controls (clients schema) ──────────
#
# The write side the web admin's "Scheduled Reports" panel needs for the
# #698 feature (the `send_scheduled_client_reports` cron):
#
#   * setScheduledReportEnabled — flip a tenant's opt-in switch
#     (Tenant.scheduled_report_enabled) on/off.
#   * sendTestClientReport — a SAFE PREVIEW: generate one tenant's monthly
#     report PDF and email it to ONLY the requesting user's own address, so
#     Ignite can eyeball the deliverable WITHOUT ever emailing the client.
#
# Both are tenant-scoped exactly like the read side + TenantGoalsMutations
# (_CampaignReportService.resolve_target_tenant_id): clients are pinned to
# their own tenant; admins (spark-admin / staff / superuser /
# @igniteproductions.co) may target any tenant. Both NEVER raise — every
# failure path resolves to success=False + a human-readable message.


@strawberry.input
class SetScheduledReportEnabledInput(SparkGraphQLInput):
    """Flip one tenant's scheduled-monthly-report opt-in switch.

    ``tenantId`` is the brand to toggle (a client may only toggle their own —
    the value is overridden server-side); ``enabled`` is the new state.
    """

    tenant_id: strawberry.ID
    enabled: bool


@strawberry.type
class SetScheduledReportEnabledResponse:
    success: bool
    message: str
    # The persisted state after the mutation. Authoritative ONLY when
    # ``success`` is true; on a no-op / failure it echoes the requested value
    # (which was NOT applied), so callers should gate on ``success`` first.
    enabled: bool
    client_mutation_id: strawberry.ID | None = None


@strawberry.input
class SetClientWeeklyDigestEnabledInput(SparkGraphQLInput):
    """Flip one tenant's weekly client-digest opt-in switch.

    ``tenantId`` is the brand to toggle (a client may only toggle their own —
    the value is overridden server-side); ``enabled`` is the new state.
    """

    tenant_id: strawberry.ID
    enabled: bool


@strawberry.type
class SetClientWeeklyDigestEnabledResponse:
    success: bool
    message: str
    # The persisted state after the mutation. Authoritative ONLY when
    # ``success`` is true; on a no-op / failure it echoes the requested value
    # (which was NOT applied), so callers should gate on ``success`` first.
    enabled: bool
    client_mutation_id: strawberry.ID | None = None


@strawberry.input
class SendTestClientReportInput(SparkGraphQLInput):
    """Request a SAFE PREVIEW of a tenant's monthly report.

    ``tenantId`` is the brand to preview (tenant-scoped — a client may only
    preview their own). ``month`` optionally overrides the reporting period as
    ``"YYYY-MM"``; omitted/null → the prior COMPLETE month (the same period the
    cron defaults to). The PDF is emailed to ONLY the requesting user's own
    email — NEVER the tenant's client recipients.
    """

    tenant_id: strawberry.ID
    month: str | None = None


@strawberry.type
class SendTestClientReportResponse:
    success: bool
    message: str
    client_mutation_id: strawberry.ID | None = None


@strawberry.input
class ExportExpenseReceiptsInput:
    """Month-end expense receipts export. ``tenant_id`` follows the same
    admin-honored / client-pinned scoping as the report toggles. Dates
    are inclusive ``YYYY-MM-DD``."""
    start_date: str
    end_date: str
    tenant_id: strawberry.ID | None = None
    client_mutation_id: strawberry.ID | None = None


@strawberry.type
class ExportExpenseReceiptsResponse:
    success: bool
    message: str
    client_mutation_id: strawberry.ID | None = None
    # CSV text — the FE saves it as a Blob download (no extra round trip).
    csv_text: str | None = None
    # GCS public URL of the receipt-image PDF bundle.
    pdf_url: str | None = None
    recap_count: int = 0
    receipt_count: int = 0
    total_spend: float = 0.0


@strawberry.type
class FieldSamplingCalloutSummary:
    """The on-demand Gemini narrative over one Field Sampling Report
    window's deterministic call-outs feed — see
    :func:`recaps.field_sampling_report.generate_ai_callout_summary`.

    ``summary`` is None when Gemini is unavailable/fails (never an error —
    the caller already has the deterministic :class:`FieldCallout` feed to
    fall back to) OR when there were no call-outs to summarize.
    """

    summary: str | None


@strawberry.type
class FieldSamplingReportMutations:
    """Mutation surface for the Field Sampling Report's OPTIONAL AI layer.

    Deliberately separate from the (free, deterministic, always-on) read
    side: generating a narrative costs a real Gemini call, so it only
    happens when a user explicitly clicks "Summarize with AI" — never
    automatically on page load. Matches this codebase's own precedent
    (recaps/tenant_insights.py replaced free-form AI summaries with fixed
    deterministic buckets for reliability on numbers a client sees).
    """

    @strawberry.mutation(permission_classes=[StrictIsAuthenticated])
    async def generate_field_sampling_callout_summary(
        self,
        info: strawberry.Info,
        tenant_id: strawberry.ID,
        start_date: str,
        end_date: str,
        event_type_id: strawberry.ID | None = None,
        market: str | None = None,
    ) -> FieldSamplingCalloutSummary:
        """Ask Gemini to summarize this EXACT window's deterministic
        call-outs feed (same args as ``tenantFieldSamplingReport`` — the
        frontend calls this with whatever window it's currently showing).

        Never raises: an out-of-scope tenant, unparseable dates, no
        call-outs in window, or any Gemini failure all resolve to
        ``FieldSamplingCalloutSummary(summary=None)`` — the caller keeps
        showing the deterministic feed either way.
        """
        empty = FieldSamplingCalloutSummary(summary=None)
        service = _CampaignReportService()
        target_tenant_id = await service.resolve_target_tenant_id(info, tenant_id)
        if target_tenant_id is None:
            return empty

        try:
            start_d = datetime.fromisoformat(start_date).date()
            end_d = datetime.fromisoformat(end_date).date()
        except (ValueError, TypeError):
            return empty

        et_id: int | None = None
        if event_type_id is not None:
            raw = str(event_type_id).strip()
            if raw:
                try:
                    et_id = resolve_id_to_int(raw)
                except Exception:
                    et_id = None

        def _build() -> str | None:
            from tenants.models import Tenant
            from recaps.field_sampling_report import field_callouts, samples_per_hour

            tenant = Tenant.objects.filter(id=target_tenant_id).first()
            if tenant is None:
                return None
            anchor = timezone.now().replace(
                hour=0, minute=0, second=0, microsecond=0
            )
            start_dt = anchor.replace(
                year=start_d.year, month=start_d.month, day=start_d.day
            )
            end_dt = anchor.replace(
                year=end_d.year, month=end_d.month, day=end_d.day
            ) + timedelta(days=1)
            callouts = field_callouts(
                target_tenant_id, start_dt, end_dt, et_id, market
            )
            if not callouts:
                return None
            context = samples_per_hour(
                target_tenant_id, start_dt, end_dt, et_id, market
            )
            return generate_ai_callout_summary(tenant.name, callouts, context)

        try:
            summary = await sync_to_async(_build, thread_sensitive=True)()
        except Exception:
            return empty

        return FieldSamplingCalloutSummary(summary=summary)


@strawberry.type
class ScheduledReportMutations:
    """Mutation surface for the scheduled monthly client-report controls
    (the clients schema). The write side of the #698 feature; tenant scoping
    matches the read resolvers + ``TenantGoalsMutations`` exactly
    (:meth:`_CampaignReportService.resolve_target_tenant_id`)."""

    @strawberry.mutation(permission_classes=[StrictIsAuthenticated])
    async def set_scheduled_report_enabled(
        self,
        info: strawberry.Info,
        input: SetScheduledReportEnabledInput,
    ) -> SetScheduledReportEnabledResponse:
        """Toggle a tenant's ``scheduled_report_enabled`` flag; return the new state.

        Resolves the target tenant via
        :meth:`_CampaignReportService.resolve_target_tenant_id` (client → own
        tenant only; admins → any). Persists the requested ``enabled`` state and
        returns it.

        Never raises: an out-of-scope / unusable tenant id, an unknown tenant,
        or any DB failure resolves to ``success=False`` + a clear message (the
        flag is left untouched).
        """
        service = _CampaignReportService()
        try:
            target_tenant_id = await service.resolve_target_tenant_id(
                info, input.tenant_id
            )
        except Exception:  # noqa: BLE001
            target_tenant_id = None

        if target_tenant_id is None:
            # Out of scope / unusable id — no-op, do not reveal whether the
            # tenant exists. Echo the (un-applied) requested value.
            return SetScheduledReportEnabledResponse(
                success=False,
                message="You do not have access to this brand.",
                enabled=input.enabled,
                client_mutation_id=input.client_mutation_id,
            )

        desired = bool(input.enabled)

        def _set_flag():
            from tenants.models import Tenant

            tenant = Tenant.objects.filter(id=target_tenant_id).first()
            if tenant is None:
                return None
            if tenant.scheduled_report_enabled != desired:
                tenant.scheduled_report_enabled = desired
                tenant.save(update_fields=["scheduled_report_enabled", "updated_at"])
            return tenant.scheduled_report_enabled

        try:
            new_state = await sync_to_async(_set_flag, thread_sensitive=True)()
        except Exception as exc:  # noqa: BLE001
            logger.exception(
                "setScheduledReportEnabled failed for tenant=%s: %s",
                target_tenant_id,
                exc,
            )
            return SetScheduledReportEnabledResponse(
                success=False,
                message="Could not update the scheduled-report setting.",
                enabled=input.enabled,
                client_mutation_id=input.client_mutation_id,
            )

        if new_state is None:
            return SetScheduledReportEnabledResponse(
                success=False,
                message="Brand not found.",
                enabled=input.enabled,
                client_mutation_id=input.client_mutation_id,
            )

        message = (
            "Scheduled monthly report turned ON for this brand."
            if new_state
            else "Scheduled monthly report turned OFF for this brand."
        )
        return SetScheduledReportEnabledResponse(
            success=True,
            message=message,
            enabled=bool(new_state),
            client_mutation_id=input.client_mutation_id,
        )

    @strawberry.mutation(permission_classes=[StrictIsAuthenticated])
    async def set_client_weekly_digest_enabled(
        self,
        info: strawberry.Info,
        input: SetClientWeeklyDigestEnabledInput,
    ) -> SetClientWeeklyDigestEnabledResponse:
        """Toggle a tenant's ``client_weekly_digest_enabled`` flag.

        The weekly digest's own opt-in — independent from the monthly report
        toggle above so each rolls out per tenant separately. Tenant scoping,
        no-op posture, and never-raise behavior mirror
        :meth:`set_scheduled_report_enabled` exactly.
        """
        service = _CampaignReportService()
        try:
            target_tenant_id = await service.resolve_target_tenant_id(
                info, input.tenant_id
            )
        except Exception:  # noqa: BLE001
            target_tenant_id = None

        if target_tenant_id is None:
            return SetClientWeeklyDigestEnabledResponse(
                success=False,
                message="You do not have access to this brand.",
                enabled=input.enabled,
                client_mutation_id=input.client_mutation_id,
            )

        desired = bool(input.enabled)

        def _set_flag():
            from tenants.models import Tenant

            tenant = Tenant.objects.filter(id=target_tenant_id).first()
            if tenant is None:
                return None
            if tenant.client_weekly_digest_enabled != desired:
                tenant.client_weekly_digest_enabled = desired
                tenant.save(
                    update_fields=["client_weekly_digest_enabled", "updated_at"]
                )
            return tenant.client_weekly_digest_enabled

        try:
            new_state = await sync_to_async(_set_flag, thread_sensitive=True)()
        except Exception as exc:  # noqa: BLE001
            logger.exception(
                "setClientWeeklyDigestEnabled failed for tenant=%s: %s",
                target_tenant_id,
                exc,
            )
            return SetClientWeeklyDigestEnabledResponse(
                success=False,
                message="Could not update the weekly-digest setting.",
                enabled=input.enabled,
                client_mutation_id=input.client_mutation_id,
            )

        if new_state is None:
            return SetClientWeeklyDigestEnabledResponse(
                success=False,
                message="Brand not found.",
                enabled=input.enabled,
                client_mutation_id=input.client_mutation_id,
            )

        message = (
            "Weekly client digest turned ON for this brand."
            if new_state
            else "Weekly client digest turned OFF for this brand."
        )
        return SetClientWeeklyDigestEnabledResponse(
            success=True,
            message=message,
            enabled=bool(new_state),
            client_mutation_id=input.client_mutation_id,
        )

    @strawberry.mutation(permission_classes=[StrictIsAuthenticated])
    async def export_expense_receipts(
        self,
        info: strawberry.Info,
        input: ExportExpenseReceiptsInput,
    ) -> ExportExpenseReceiptsResponse:
        """Month-end BA expense receipts → bookkeeping CSV + a GCS-hosted
        PDF bundle of every receipt image, captioned BA · event · amount.

        Collects BOTH recap families (custom receipt-category files /
        spend fields + legacy ``account_spend_amount``), scoped by event
        date. Image fetch fans out on a thread pool (same 16-worker
        sweet spot as the recap PDF) and the WeasyPrint render runs
        off-thread on pure dicts — no ORM mid-render.
        """
        from datetime import date as _date

        from recaps.receipts_export import (
            build_expense_rows_csv,
            build_receipts_bundle_pdf,
            collect_expense_rows,
        )

        fail = lambda msg: ExportExpenseReceiptsResponse(  # noqa: E731
            success=False,
            message=msg,
            client_mutation_id=input.client_mutation_id,
        )

        service = _CampaignReportService()
        try:
            target_tenant_id = await service.resolve_target_tenant_id(
                info, input.tenant_id
            )
        except Exception:  # noqa: BLE001
            target_tenant_id = None
        if target_tenant_id is None:
            return fail("You do not have access to this brand.")

        try:
            start = _date.fromisoformat(str(input.start_date))
            end = _date.fromisoformat(str(input.end_date))
        except (TypeError, ValueError):
            return fail("Dates must be YYYY-MM-DD.")
        if end < start:
            return fail("End date is before the start date.")

        try:
            rows = await sync_to_async(collect_expense_rows)(
                target_tenant_id, start, end
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception(
                "exportExpenseReceipts collect failed tenant=%s: %s",
                target_tenant_id,
                exc,
            )
            return fail("Couldn't collect receipts for that range.")

        if not rows:
            return fail("No expense receipts or spend in that range.")

        csv_text = build_expense_rows_csv(rows)

        # Fan out the image downloads, then render + upload.
        def _fetch_images() -> dict[str, bytes]:
            import concurrent.futures as _cf

            from utils.gcs import download_blob_bytes

            blobs = [f["blob"] for r in rows for f in r["files"]]

            def _one(blob: str):
                try:
                    data = download_blob_bytes(blob)
                except Exception:  # noqa: BLE001
                    return None
                if not data or len(data) > 25 * 1024 * 1024:
                    return None
                return (blob, data)

            out: dict[str, bytes] = {}
            if blobs:
                with _cf.ThreadPoolExecutor(max_workers=16) as pool:
                    for entry in pool.map(_one, blobs):
                        if entry is not None:
                            out[entry[0]] = entry[1]
            return out

        @sync_to_async(thread_sensitive=False)
        def _build_and_upload() -> str | None:
            from django.utils import timezone as _tz

            from tenants.models import Tenant
            from utils.gcs import public_url, upload_bytes

            tenant = Tenant.objects.filter(id=target_tenant_id).first()
            tenant_name = tenant.name if tenant else "Brand"
            images = _fetch_images()
            pdf = build_receipts_bundle_pdf(
                tenant_name=tenant_name,
                start=start,
                end=end,
                rows=rows,
                images_by_blob=images,
            )
            ts = _tz.now().strftime("%Y%m%d%H%M%S")
            blob = (
                f"exports/receipts/{target_tenant_id}/"
                f"{start.isoformat()}_{end.isoformat()}_{ts}.pdf"
            )
            upload_bytes(blob, pdf, content_type="application/pdf")
            return public_url(blob)

        try:
            pdf_url = await _build_and_upload()
        except Exception as exc:  # noqa: BLE001
            logger.exception(
                "exportExpenseReceipts pdf failed tenant=%s: %s",
                target_tenant_id,
                exc,
            )
            # CSV still has every number + link — degrade, don't fail.
            pdf_url = None

        total = sum(r["amount"] for r in rows if r["amount"] is not None)
        return ExportExpenseReceiptsResponse(
            success=True,
            message=(
                f"{len(rows)} recap(s), ${total:,.2f} total spend."
                + ("" if pdf_url else " (PDF bundle failed — CSV only.)")
            ),
            client_mutation_id=input.client_mutation_id,
            csv_text=csv_text,
            pdf_url=pdf_url,
            recap_count=len(rows),
            receipt_count=sum(len(r["files"]) for r in rows),
            total_spend=round(total, 2),
        )

    @strawberry.mutation(permission_classes=[StrictIsAuthenticated])
    async def send_test_client_report(
        self,
        info: strawberry.Info,
        input: SendTestClientReportInput,
    ) -> SendTestClientReportResponse:
        """SAFE PREVIEW: email a tenant's monthly report to the REQUESTER only.

        Generates the monthly performance PDF
        (:func:`recaps.client_report.build_client_monthly_report_pdf`) for the
        prior COMPLETE month (or ``input.month`` ``"YYYY-MM"``) and emails it via
        :class:`recaps.envelopes.ClientMonthlyReportMailer` to ONLY the
        requesting user's own email — NEVER the tenant's configured client
        recipients. This lets Ignite preview the deliverable without ever
        mailing the brand.

        Tenant-scoped via
        :meth:`_CampaignReportService.resolve_target_tenant_id` (a client may
        only test their own brand). ``include_sentiment=False`` is passed to the
        PDF builder so a preview never triggers a fresh (paid) AI sentiment call.

        Never raises. Returns ``success=False`` + a clear message when: the
        tenant is out of scope, the requesting user has no email on file, or PDF
        generation / send fails (wrapped in try/except + ``logger.exception``).
        """
        # The ONLY recipient — the requesting user's own email. Resolved up
        # front so we never even build a PDF if there's nowhere safe to send it.
        user = info.context.request.user
        requester_email = (getattr(user, "email", "") or "").strip()
        if not requester_email:
            return SendTestClientReportResponse(
                success=False,
                message=(
                    "Your account has no email address on file, so there's "
                    "nowhere to send the preview."
                ),
                client_mutation_id=input.client_mutation_id,
            )

        service = _CampaignReportService()
        try:
            target_tenant_id = await service.resolve_target_tenant_id(
                info, input.tenant_id
            )
        except Exception:  # noqa: BLE001
            target_tenant_id = None

        if target_tenant_id is None:
            return SendTestClientReportResponse(
                success=False,
                message="You do not have access to this brand.",
                client_mutation_id=input.client_mutation_id,
            )

        # Resolve the reporting period: the cron's helpers, so a preview
        # reports the SAME window the scheduled run would. An invalid override
        # degrades to a clear message rather than raising.
        from django.core.management.base import CommandError

        from tenants.management.commands.send_scheduled_client_reports import (
            _month_label,
            _parse_month_arg,
            _prior_complete_month,
        )

        raw_month = (input.month or "").strip()
        if raw_month:
            try:
                year, month = _parse_month_arg(raw_month)
            except CommandError:
                return SendTestClientReportResponse(
                    success=False,
                    message="Month must be in YYYY-MM format (e.g. 2026-05).",
                    client_mutation_id=input.client_mutation_id,
                )
        else:
            year, month = _prior_complete_month()
        period_label = _month_label(year, month)

        def _generate_and_send() -> str | None:
            """Build the PDF + email it to the requester only. Returns the
            tenant name on success, or None if the tenant vanished."""
            from tenants.management.commands.send_scheduled_client_reports import (
                Command as _ScheduledReportCommand,
            )
            from tenants.models import Tenant

            from recaps.client_report import build_client_monthly_report_pdf
            from recaps.envelopes import ClientMonthlyReportMailer

            tenant = Tenant.objects.filter(id=target_tenant_id).first()
            if tenant is None:
                return None

            # include_sentiment=False: a preview must never trigger a fresh
            # (paid) AI sentiment call on a cache miss.
            pdf_bytes = build_client_monthly_report_pdf(
                tenant.id, year, month, include_sentiment=False
            )
            filename = _ScheduledReportCommand._pdf_filename(tenant, year, month)

            mailer = ClientMonthlyReportMailer(
                # SAFETY-CRITICAL: the requesting user's own email is the ONLY
                # recipient — NEVER tenant.scheduled_report_recipients().
                recipients=[requester_email],
                tenant_name=tenant.name or "",
                period_label=period_label,
                pdf_bytes=pdf_bytes,
                pdf_filename=filename,
            )
            mailer.send()
            return tenant.name or "this brand"

        try:
            tenant_name = await sync_to_async(
                _generate_and_send, thread_sensitive=True
            )()
        except Exception as exc:  # noqa: BLE001 — includes ClientMonthlyReportError
            logger.exception(
                "sendTestClientReport failed for tenant=%s period=%s-%s: %s",
                target_tenant_id,
                year,
                month,
                exc,
            )
            return SendTestClientReportResponse(
                success=False,
                message=(
                    "Could not generate the preview report. Please try again."
                ),
                client_mutation_id=input.client_mutation_id,
            )

        if tenant_name is None:
            return SendTestClientReportResponse(
                success=False,
                message="Brand not found.",
                client_mutation_id=input.client_mutation_id,
            )

        return SendTestClientReportResponse(
            success=True,
            message=(
                f"Preview of the {period_label} report for {tenant_name} was "
                f"sent to {requester_email}."
            ),
            client_mutation_id=input.client_mutation_id,
        )
