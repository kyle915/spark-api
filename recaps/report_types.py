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

import strawberry
from asgiref.sync import sync_to_async
from django.db import transaction
from django.utils import timezone

from recaps import report_service
from recaps.report_tokens import make_report_token
from recaps.tenant_insights import get_or_refresh_tenant_insights
from recaps.tenant_overview import (
    build_tenant_overview,
    tenant_event_recap_counts,
    tenant_kpi_totals,
    tenant_market_performance,
    tenant_monthly_trend,
)
from utils.ai_text import (
    AiUnavailable,
    generate_structured_answer,
    generate_summary,
)
from utils.graphql.mixins import SparkGraphQLMixin, resolve_id_to_int
from utils.graphql.permissions import (
    StrictIsAuthenticated,
    _is_admin_access,
    resolve_request_user_access,
)

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
    pick a badge colour. ``metric`` is an OPTIONAL short figure the model
    lifted from the data (e.g. ``"+42% MoM"``), null when no single figure
    fits.
    """

    title: str
    detail: str
    sentiment: str
    metric: str | None = None


@strawberry.type
class TenantInsights:
    """Cached, auto-generated proactive insights for ONE client's program.

    Surfaced on the dashboard WITHOUT the user asking: a short list of
    headline observations (wins, trends, standouts, things needing attention)
    generated from the same aggregated numbers as :class:`TenantKpis`, cached
    server-side, and refreshed on read when stale (see
    :func:`recaps.tenant_insights.get_or_refresh_tenant_insights`).

    ``generated_at`` is the ISO-8601 timestamp of the served snapshot, or null
    when there are no insights to show. ``items`` is the (possibly empty) list
    of insights. The resolver NEVER raises — a missing/out-of-scope tenant, an
    unconfigured AI key, or any failure resolves to
    ``TenantInsights(generated_at=None, items=[])``.
    """

    generated_at: str | None
    items: list[TenantInsightItem]


def _empty_tenant_insights() -> TenantInsights:
    """The degradation value: no timestamp, no items, never an error."""
    return TenantInsights(generated_at=None, items=[])


def _build_tenant_insights_type(
    items: list[dict], generated_at
) -> TenantInsights:
    """Map the cached insight dicts + timestamp onto the Strawberry type.

    Defensive: each item is only surfaced when it has a string ``title`` and
    ``detail`` (the cache stores cleaned dicts, but we never trust the shape
    blindly). ``generated_at`` is rendered as ISO-8601, or null when absent.
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
        out.append(
            TenantInsightItem(
                title=title,
                detail=detail,
                sentiment=sentiment,
                metric=metric,
            )
        )
    return TenantInsights(
        generated_at=generated_at.isoformat() if generated_at else None,
        items=out,
    )


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

        Auto-generated headline observations about the tenant's whole program
        (wins, trends, standouts, things needing attention), cached
        server-side and surfaced WITHOUT the user asking. Served from the
        latest fresh :class:`tenants.models.TenantInsightSnapshot`, regenerated
        on read when stale (see
        :func:`recaps.tenant_insights.get_or_refresh_tenant_insights`); a daily
        cron precomputes them so dashboard reads stay fast.

        Tenant scoping is identical to :meth:`tenant_kpis`
        (:meth:`_CampaignReportService.resolve_target_tenant_id`):
        client-role users may ONLY read their own tenant — the ``tenant_id``
        argument is overridden to their own; admins (spark-admin / staff /
        superuser / ``@igniteproductions.co``) may target any tenant.

        Never raises: a missing or out-of-scope tenant, an unconfigured AI
        key, or any failure resolves to ``TenantInsights(generated_at=None,
        items=[])`` rather than a GraphQL error.
        """
        service = _CampaignReportService()
        target_tenant_id = await service.resolve_target_tenant_id(info, tenant_id)
        if target_tenant_id is None:
            return _empty_tenant_insights()

        def _build():
            # Guard the tenant's existence so an admin passing an unknown id
            # degrades to empty rather than caching insights for no tenant.
            from tenants.models import Tenant

            if not Tenant.objects.filter(id=target_tenant_id).exists():
                return None
            return get_or_refresh_tenant_insights(target_tenant_id)

        try:
            result = await sync_to_async(_build, thread_sensitive=True)()
        except Exception:
            return _empty_tenant_insights()

        if result is None:
            return _empty_tenant_insights()

        items, generated_at = result
        return _build_tenant_insights_type(items, generated_at)


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
