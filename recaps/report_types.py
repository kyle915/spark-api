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
from utils.graphql.mixins import SparkGraphQLMixin, resolve_id_to_int
from utils.graphql.permissions import StrictIsAuthenticated


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
        try:
            resolved_id = resolve_id_to_int(request_id)
        except Exception:
            return None

        service = _CampaignReportService()
        scope_tenant_id = await service.resolve_scope_tenant_id(info)
        generated_at = timezone.now().isoformat()

        def _build():
            request_obj = report_service.get_report_request(
                resolved_id, tenant_id=scope_tenant_id
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
