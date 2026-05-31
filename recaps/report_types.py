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
from utils.ai_text import AiUnavailable, generate_summary
from utils.graphql.mixins import SparkGraphQLMixin
from utils.graphql.permissions import StrictIsAuthenticated

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
    "ONLY the provided campaign data. Be concise (2-4 sentences). Never "
    "invent or estimate numbers that are not present in the data. If the "
    "data does not contain the answer, say plainly that it cannot be "
    "determined from this campaign's data."
)

# Hard cap on the inbound question length — keeps the prompt bounded and
# blocks a pathologically long question from blowing up the request.
_AI_ANSWER_MAX_QUESTION_CHARS = 1000


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
class CampaignReportAiAnswer:
    """Result of an on-demand freeform Q&A over one campaign's report.

    Mirrors :class:`CampaignReportAiSummary`: ``ok`` is the only field a
    caller must branch on. When ``true``, ``answer`` holds the generated
    response and ``reason`` is null; when ``false`` (no question, request
    out of scope, AI unconfigured, or the upstream call failed),
    ``answer`` is ``""`` and ``reason`` carries a short, human-readable
    explanation. The resolver never raises — degradation is always a
    value, never a GraphQL error.
    """

    ok: bool
    answer: str
    reason: str | None = None


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
        the aggregate report, composes a prompt, and calls Gemini.

        Never raises: an out-of-scope/missing request, an unconfigured
        ``GEMINI_API_KEY``, or any upstream failure all resolve to
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
        the same compact report prompt, and calls Gemini.

        Never raises: an empty question, an out-of-scope/missing request,
        an unconfigured ``GEMINI_API_KEY``, or any upstream failure all
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
            answer = await sync_to_async(generate_summary, thread_sensitive=True)(
                _AI_ANSWER_SYSTEM_PROMPT, user_prompt
            )
        except AiUnavailable as exc:
            return CampaignReportAiAnswer(ok=False, answer="", reason=str(exc))
        except Exception:
            # Belt-and-suspenders: generate_summary already funnels every
            # failure through AiUnavailable, but never let an unexpected
            # error escape the resolver.
            return CampaignReportAiAnswer(
                ok=False,
                answer="",
                reason="The answer could not be generated.",
            )

        return CampaignReportAiAnswer(ok=True, answer=answer, reason=None)
