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
from django.utils import timezone

from recaps import report_service
from recaps.report_tokens import make_report_token
from recaps.tenant_overview import (
    build_tenant_overview,
    tenant_event_recap_counts,
    tenant_kpi_totals,
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


def _build_tenant_kpis(tenant_id: int) -> TenantKpis:
    """Assemble the structured :class:`TenantKpis` for one tenant.

    Synchronous Django ORM (the resolver wraps it in ``sync_to_async``).
    Pulls the headline counts, the nine summable KPIs, and the monthly
    trend from the shared helpers in :mod:`recaps.tenant_overview` so the
    figures match the plaintext overview exactly.
    """
    event_count, recap_count = tenant_event_recap_counts(tenant_id)
    totals = tenant_kpi_totals(tenant_id)
    trend = tenant_monthly_trend(tenant_id)
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
    ) -> TenantKpis:
        """Structured per-tenant KPI roll-up for dashboard / pop-up charts.

        The visual companion to :meth:`tenant_ai_answer`: same tenant data,
        but returned as numbers (headline counts, the nine summable KPIs,
        and a twelve-month activity trend) instead of an AI prose answer.
        Numbers come from the shared
        :func:`recaps.tenant_overview.tenant_kpi_totals` source of truth, so
        they match the text overview exactly.

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
            return _build_tenant_kpis(target_tenant_id)

        try:
            data = await sync_to_async(_build, thread_sensitive=True)()
        except Exception:
            return _zeroed_tenant_kpis()

        if data is None:
            return _zeroed_tenant_kpis()
        return data
