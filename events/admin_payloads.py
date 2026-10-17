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

from events.models import Event, Request, Retailer
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
    """Badge integers that used to be tallied client-side off 2,000 rows.

    ``done_30d`` is filed (submitted) recaps in the last 30 days — legacy +
    custom, empty clock-out shells excluded — not Request rows with a Done
    status. Torch-style programs stay APPROVED with filed recaps; counting
    Done-status requests under-reports to zero.
    """
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

    req_ids = qs.values("pk")
    # Event.date window = program activity in the last 30 calendar days.
    event_in_window = Q(
        event__date__date__gte=lookback30, event__date__date__lt=today
    ) | Q(
        # Some events only carry start_time; fall back so they still count.
        event__date__isnull=True,
        event__start_time__date__gte=lookback30,
        event__start_time__date__lt=today,
    )
    done_30d = (
        Recap.objects.filter(event__request_id__in=req_ids)
        .filter(legacy_filed_q())
        .filter(event_in_window)
        .count()
        + CustomRecap.objects.filter(event__request_id__in=req_ids)
        .filter(custom_filed_q())
        .filter(event_in_window)
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
    )
    return SidebarRequestCountsData(
        tracker=int(agg["tracker"] or 0),
        approvals=int(agg["approvals"] or 0),
        approvals_sla_breach=int(agg["approvals_sla_breach"] or 0),
        upcoming=int(agg["upcoming"] or 0),
        done_30d=int(done_30d),
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


COVERAGE_SAMPLED = "sampled"
COVERAGE_REPEAT = "repeat"
COVERAGE_NEVER = "never_visited"


def coverage_class_for(visit_count: int) -> str:
    """Classify a roster account by distinct events with a filed recap."""
    if visit_count > 1:
        return COVERAGE_REPEAT
    if visit_count == 1:
        return COVERAGE_SAMPLED
    return COVERAGE_NEVER


@dataclass(frozen=True)
class AccountMapCoverageData:
    roster_total: int
    sampled: int
    repeat: int
    never_visited: int
    planned: int


def _filed_event_retailer_rows(tenant_id: int | None):
    """(retailer_id, event_id, request_id) for filed, non-archived recaps."""
    legacy = Recap.objects.filter(legacy_filed_q(), archived_at__isnull=True)
    custom = CustomRecap.objects.filter(custom_filed_q(), archived_at__isnull=True)
    if tenant_id is not None:
        legacy = legacy.filter(event__tenant_id=tenant_id)
        custom = custom.filter(event__tenant_id=tenant_id)
    fields = ("retailer_id", "event__retailer_id", "event__request__retailer_id", "event_id", "event__request_id")
    for qs in (legacy, custom):
        yield from qs.values_list(*fields)


def retailer_visit_counts(tenant_id: int | None = None) -> dict[int, int]:
    """Distinct filed-recap events per roster Retailer id. Nothing estimated."""
    events_by_retailer: dict[int, set[int]] = {}
    for retailer_id, event_retailer_id, request_retailer_id, event_id, _req in _filed_event_retailer_rows(tenant_id):
        rid = retailer_id or event_retailer_id or request_retailer_id
        if rid and event_id:
            events_by_retailer.setdefault(int(rid), set()).add(int(event_id))
    return {rid: len(ids) for rid, ids in events_by_retailer.items()}


def request_visit_counts(tenant_id: int | None = None) -> dict[int, int]:
    counts: dict[int, set[int]] = {}
    for _r, _er, _rr, event_id, request_id in _filed_event_retailer_rows(tenant_id):
        if request_id and event_id:
            counts.setdefault(int(request_id), set()).add(int(event_id))
    return {rid: len(ids) for rid, ids in counts.items()}


def planned_retailer_ids(tenant_id: int | None = None, *, now=None) -> set[int]:
    """Retailers with a future event/request — labelled Planned, never Sampled."""
    now = now or timezone.now()
    out: set[int] = set()
    events = Event.objects.filter(start_time__gte=now)
    requests = Request.objects.filter(deleted_at__isnull=True, date__gte=now).filter(
        _status_q("approved", "scheduled")
    )
    if tenant_id is not None:
        events = events.filter(tenant_id=tenant_id)
        requests = requests.filter(tenant_id=tenant_id)
    for rid in events.values_list("retailer_id", "request__retailer_id"):
        out.update(int(v) for v in rid if v)
    for rid in requests.values_list("retailer_id", flat=True):
        if rid:
            out.add(int(rid))
    return out


def compute_account_map_coverage(tenant_id: int | None = None) -> AccountMapCoverageData:
    """Roster-wide coverage counts for one tenant (all tenants when None)."""
    roster = Retailer.objects.all()
    if tenant_id is not None:
        roster = roster.filter(tenant_id=tenant_id)
    roster_ids = list(roster.values_list("id", flat=True))
    visits = retailer_visit_counts(tenant_id)
    planned = planned_retailer_ids(tenant_id)
    sampled = repeat = never = 0
    for rid in roster_ids:
        cls = coverage_class_for(visits.get(int(rid), 0))
        if cls == COVERAGE_REPEAT:
            repeat += 1
        elif cls == COVERAGE_SAMPLED:
            sampled += 1
        else:
            never += 1
    return AccountMapCoverageData(
        roster_total=len(roster_ids),
        sampled=sampled,
        repeat=repeat,
        never_visited=never,
        planned=sum(1 for rid in roster_ids if int(rid) in planned),
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
    coverage_class: str = COVERAGE_NEVER
    visit_count: int = 0
    planned: bool = False


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
    visits_by_retailer = retailer_visit_counts()
    visits_by_request = request_visit_counts()
    planned_ids = planned_retailer_ids()
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
                coverage_class=coverage_class_for(
                    visits_by_retailer.get(retailer.id, 0)
                    if retailer
                    else visits_by_request.get(row.pk, 0)
                ),
                visit_count=(
                    visits_by_retailer.get(retailer.id, 0)
                    if retailer
                    else visits_by_request.get(row.pk, 0)
                ),
                planned=bool(retailer and retailer.id in planned_ids),
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
                "state__code",
                "location__state__code",
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
