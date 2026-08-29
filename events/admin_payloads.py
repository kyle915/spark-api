"""Skinny admin payloads: sidebar badge counts and Account Map pins.

The sidebar used to download up to 2,000 fat Request rows on every admin
page just to tally a handful of integers. Account Map reused the Master
Tracker connection (products, open shifts, recap rollups) for lat/lng
pins. These helpers query only what those surfaces need.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta

from django.db.models import Count, Exists, F, OuterRef, Q, QuerySet
from django.db.models.functions import Coalesce
from django.utils import timezone
from strawberry.relay import to_base64

from events.models import Event, Request
from events.types import _serialize_dt
from recaps.filed import custom_filed_q, legacy_filed_q
from recaps.models import CustomRecap, Recap

DONE_SLUGS = ("done", "completed")
APPROVAL_SLUGS = ("pending", "queue")
UPCOMING_SLUGS = ("approved", "scheduled")
ALERT_REVIEW_SLUGS = ("approved", "declined")

ACCOUNT_MAP_CAP = 2000


def _status_q(*slugs: str) -> Q:
    q = Q()
    for slug in slugs:
        q |= Q(status__slug__iexact=slug) | Q(status__name__iexact=slug)
    return q


def scoped_requests(tenant_id: int | None) -> QuerySet[Request]:
    qs = Request.objects.filter(deleted_at__isnull=True)
    if tenant_id is not None:
        qs = qs.filter(tenant_id=tenant_id)
    return qs


@dataclass(frozen=True)
class SidebarRequestCountsData:
    tracker: int
    approvals: int
    approvals_sla_breach: int
    upcoming: int
    done_30d: int
    recaps_due: int


def compute_sidebar_request_counts(
    qs: QuerySet[Request],
    *,
    now=None,
) -> SidebarRequestCountsData:
    """Badge integers that used to be tallied client-side off 2,000 rows."""
    now = now or timezone.now()
    today = now.date()
    cutoff72 = now - timedelta(hours=72)
    horizon14 = now + timedelta(days=14)
    lookback30 = today - timedelta(days=30)
    done_q = _status_q(*DONE_SLUGS)
    approval_q = _status_q(*APPROVAL_SLUGS)
    upcoming_q = _status_q(*UPCOMING_SLUGS)

    has_event = Exists(Event.objects.filter(request_id=OuterRef("pk")))
    has_filed_legacy = Exists(
        Recap.objects.filter(event__request_id=OuterRef("pk")).filter(legacy_filed_q())
    )
    has_filed_custom = Exists(
        CustomRecap.objects.filter(event__request_id=OuterRef("pk")).filter(
            custom_filed_q()
        )
    )

    recaps_due = (
        qs.filter(date__date__gte=lookback30, date__date__lt=today)
        .filter(has_event)
        .filter(~has_filed_legacy, ~has_filed_custom)
        .count()
    )

    agg = qs.aggregate(
        tracker=Count("pk", filter=~done_q),
        approvals=Count("pk", filter=approval_q),
        approvals_sla_breach=Count(
            "pk", filter=approval_q & Q(created_at__lte=cutoff72)
        ),
        upcoming=Count(
            "pk", filter=upcoming_q & Q(date__gt=now) & Q(date__lt=horizon14)
        ),
        done_30d=Count(
            "pk",
            filter=done_q
            & Q(date__gt=now - timedelta(days=30))
            & Q(date__lt=now),
        ),
    )
    return SidebarRequestCountsData(
        tracker=int(agg["tracker"] or 0),
        approvals=int(agg["approvals"] or 0),
        approvals_sla_breach=int(agg["approvals_sla_breach"] or 0),
        upcoming=int(agg["upcoming"] or 0),
        done_30d=int(agg["done_30d"] or 0),
        recaps_due=recaps_due,
    )


@dataclass(frozen=True)
class AlertCandidate:
    id: str
    created_at: str
    updated_at: str
    status_slug: str


def list_alert_candidates(qs: QuerySet[Request], *, now=None) -> list[AlertCandidate]:
    """Rows the sidebar unread-alerts chip still keys off localStorage for."""
    now = now or timezone.now()
    cutoff48 = now - timedelta(hours=48)
    pending = qs.filter(_status_q("pending"), created_at__gte=cutoff48)
    reviewed = qs.filter(
        _status_q(*ALERT_REVIEW_SLUGS),
        updated_at__gte=cutoff48,
        updated_at__gt=F("created_at") + timedelta(seconds=5),
    )
    rows = list(
        (pending | reviewed)
        .select_related("status")
        .order_by("-updated_at")[:200]
    )
    out: list[AlertCandidate] = []
    for row in rows:
        slug = (row.status.slug or row.status.name or "") if row.status else ""
        out.append(
            AlertCandidate(
                id=to_base64("Request", row.pk),
                created_at=_serialize_dt(row.created_at, offset_minutes=0) or "",
                updated_at=_serialize_dt(row.updated_at, offset_minutes=0) or "",
                status_slug=slug.lower(),
            )
        )
    return out


def is_plottable_lat_lng(lat: float, lng: float) -> bool:
    return (
        lat == lat
        and lng == lng
        and not (lat == 0 and lng == 0)
        and -90 <= lat <= 90
        and -180 <= lng <= 180
    )


@dataclass(frozen=True)
class AccountMapPinData:
    id: str
    name: str
    address: str
    lat: float
    lng: float
    status_slug: str
    date: str | None
    retailer_name: str
    location_name: str
    state_code: str


def list_account_map_pins(
    qs: QuerySet[Request],
    *,
    limit: int = ACCOUNT_MAP_CAP,
) -> list[AccountMapPinData]:
    """Lat/lng + label fields for Account Map. No products / shifts / recaps."""
    rows = (
        qs.exclude(coordinates=[])
        .select_related(
            "status",
            "retailer",
            "retailer__location",
            "retailer__location__state",
            "location",
            "state",
        )
        .order_by("-date")[:limit]
    )
    pins: list[AccountMapPinData] = []
    for row in rows:
        coords = row.coordinates or []
        if len(coords) < 2:
            continue
        try:
            lat = float(coords[0])
            lng = float(coords[1])
        except (TypeError, ValueError):
            continue
        if not is_plottable_lat_lng(lat, lng):
            continue
        status = row.status
        slug = ((status.slug or status.name) if status else "") or ""
        retailer = row.retailer
        loc = (retailer.location if retailer else None) or row.location
        state = (loc.state if loc else None) or row.state
        name = (retailer.name if retailer else None) or row.name or "—"
        pins.append(
            AccountMapPinData(
                id=to_base64("Request", row.pk),
                name=name,
                address=row.address or "",
                lat=lat,
                lng=lng,
                status_slug=slug.lower(),
                date=_serialize_dt(row.date, offset_minutes=0),
                retailer_name=(retailer.name if retailer else None) or "",
                location_name=(loc.name if loc else None) or "",
                state_code=(state.code if state else None) or "",
            )
        )
    return pins


@dataclass(frozen=True)
class TrackerStatusBucketData:
    slug: str
    count: int


@dataclass(frozen=True)
class TrackerStatusCountsData:
    """Master Tracker status chips + market dropdown without downloading rows."""

    total: int
    buckets: list[TrackerStatusBucketData]
    market_codes: list[str]


def compute_tracker_status_counts(
    qs: QuerySet[Request],
    *,
    market_qs: QuerySet[Request] | None = None,
) -> TrackerStatusCountsData:
    """Aggregate status slug counts (+ optional market codes) for the tracker.

    `qs` should already have tenant + refine filters applied, but NOT a
    status chip filter — chips need every bucket. `market_qs` defaults to
    `qs` and should omit `state_code` so the Market dropdown stays full.
    """
    total = qs.count()
    rows = qs.values("status__slug").annotate(c=Count("pk"))
    buckets = [
        TrackerStatusBucketData(
            slug=((row["status__slug"] or "unknown").strip().lower() or "unknown"),
            count=int(row["c"] or 0),
        )
        for row in rows
        if int(row["c"] or 0) > 0
    ]
    buckets.sort(key=lambda b: (-b.count, b.slug))

    source = market_qs if market_qs is not None else qs
    raw_codes = (
        source.annotate(
            market_code=Coalesce(
                "retailer__location__state__code",
                "location__state__code",
                "state__code",
            )
        )
        .exclude(market_code__isnull=True)
        .exclude(market_code="")
        .values_list("market_code", flat=True)
        .distinct()
    )
    market_codes = sorted({(c or "").strip().upper() for c in raw_codes if c})
    return TrackerStatusCountsData(
        total=total,
        buckets=buckets,
        market_codes=market_codes,
    )
