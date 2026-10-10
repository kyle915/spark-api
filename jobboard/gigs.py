"""Which gigs a brand's job board lists, and how they read publicly.

A gig is an upcoming Event whose Request came from a live field marketing
plan. Admin overrides (open shifts, listed) live on ``JobBoardGig``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from django.db.models import Count, Q
from django.utils import timezone

from ambassadors.checkin_web import _brand_payload
from ambassadors.models import AmbassadorEvent
from events.field_marketing import MARKET_TIMEZONES, SAMPLING_LABELS
from events.market import market_for
from events.models import Event, FieldMarketingEvent
from jobboard.config import JobBoardConfig, job_board_config_for
from jobboard.models import JobBoard, JobBoardGig
from utils.tz import resolve_zoneinfo

STAFFING_EMAIL = "staffing@igniteproductions.co"

DEAD_STATUS_SLUGS = {"cancelled", "canceled", "declined", "rejected"}

GIG_TYPE_LABELS = {
    FieldMarketingEvent.ACTIVITY_EVENT_ACTIVATION: "Event Activation",
    FieldMarketingEvent.ACTIVITY_SPONSORSHIP: "Event Activation",
    FieldMarketingEvent.ACTIVITY_GUERILLA: "Guerilla",
    FieldMarketingEvent.ACTIVITY_FULL_CAN: "Guerilla",
    FieldMarketingEvent.ACTIVITY_POUR: "Guerilla",
    FieldMarketingEvent.ACTIVITY_PRODUCT_SEEDING: "Product Seeding",
    FieldMarketingEvent.ACTIVITY_RETAIL_SUPPORT: "Retail Support",
    FieldMarketingEvent.ACTIVITY_SALES_SUPPORT: "Sales Support",
}

_BA_COUNT_RE = re.compile(r"BA count:\s*(\d+)", re.IGNORECASE)


# ── Gigs ──────────────────────────────────────────────────────────────


@dataclass
class Gig:
    event: Event
    plan: FieldMarketingEvent
    override: JobBoardGig | None
    booked: int

    @property
    def open_shifts(self) -> int:
        if self.override is not None:
            return self.override.open_shifts
        return default_open_shifts(self.plan)

    @property
    def listed(self) -> bool:
        return self.override.listed if self.override is not None else True

    @property
    def spots_left(self) -> int:
        return max(self.open_shifts - self.booked, 0)

    @property
    def on_board(self) -> bool:
        return self.listed and self.open_shifts > 0

    @property
    def zone(self) -> ZoneInfo:
        return gig_zone(self.event, self.plan)

    @property
    def starts_at(self) -> datetime:
        return self.event.start_time or self.event.date

    @property
    def ends_at(self) -> datetime | None:
        end = self.event.end_time
        if end and self.starts_at and end > self.starts_at:
            return end
        return None

    @property
    def type_label(self) -> str:
        return GIG_TYPE_LABELS.get(self.plan.activity, "Event Activation")

    @property
    def venue(self) -> str:
        return (self.plan.name or self.event.name or "").strip()

    @property
    def area(self) -> str:
        label = market_for(self.event).label
        return "" if label == "Unknown" else label

    @property
    def skus(self) -> list[str]:
        return [s for s in (self.plan.sku_names or []) if str(s).strip()]


def default_open_shifts(plan: FieldMarketingEvent) -> int:
    if plan.ambassador_count and plan.ambassador_count > 0:
        return int(plan.ambassador_count)
    notes = getattr(plan.request, "notes", None) or ""
    match = _BA_COUNT_RE.search(notes)
    if match and int(match.group(1)) > 0:
        return int(match.group(1))
    return 1


def gig_zone(event: Event, plan: FieldMarketingEvent) -> ZoneInfo:
    market_zone = MARKET_TIMEZONES.get((plan.market or "").strip().lower())
    if market_zone:
        return ZoneInfo(market_zone)
    return resolve_zoneinfo(event.timezone) or ZoneInfo("America/New_York")


def _live_plans(tenant) -> dict[int, FieldMarketingEvent]:
    plans = (
        FieldMarketingEvent.objects.filter(
            tenant=tenant,
            deleted_at__isnull=True,
            request__isnull=False,
            request__deleted_at__isnull=True,
        )
        .select_related("request", "request__status")
        .order_by("id")
    )
    by_request: dict[int, FieldMarketingEvent] = {}
    for plan in plans:
        status = getattr(plan.request.status, "slug", None) or ""
        if status.lower() in DEAD_STATUS_SLUGS:
            continue
        by_request.setdefault(plan.request_id, plan)
    return by_request


def list_gigs(tenant, *, now: datetime | None = None, event_ids=None) -> list[Gig]:
    """Every upcoming plan gig for the brand, listed or not, by start time."""
    now = now or timezone.now()
    plans = _live_plans(tenant)
    if not plans:
        return []
    events = (
        Event.objects.filter(tenant=tenant, request_id__in=list(plans))
        .filter(
            Q(start_time__gt=now) | Q(start_time__isnull=True, date__gt=now)
        )
        .exclude(status__slug__in=DEAD_STATUS_SLUGS)
        .select_related("timezone", "state", "location", "status")
    )
    if event_ids is not None:
        events = events.filter(id__in=list(event_ids))
    events = list(events)
    ids = [e.id for e in events]
    overrides = {g.event_id: g for g in JobBoardGig.objects.filter(event_id__in=ids)}
    booked = dict(
        AmbassadorEvent.objects.filter(event_id__in=ids, is_approved=True)
        .values("event_id")
        .annotate(n=Count("id"))
        .values_list("event_id", "n")
    )
    gigs = [
        Gig(
            event=e,
            plan=plans[e.request_id],
            override=overrides.get(e.id),
            booked=booked.get(e.id, 0),
        )
        for e in events
    ]
    gigs.sort(key=lambda g: (g.starts_at, g.event.id))
    return gigs


def pending_plans(tenant, *, now: datetime | None = None) -> list[FieldMarketingEvent]:
    """Plans sent to Ignite that have no event yet (waiting on approval)."""
    now = now or timezone.now()
    plans = _live_plans(tenant)
    with_events = set(
        Event.objects.filter(tenant=tenant, request_id__in=list(plans)).values_list(
            "request_id", flat=True
        )
    )
    today = timezone.localdate(now)
    return [
        p
        for rid, p in plans.items()
        if rid not in with_events and (p.starts_on is None or p.starts_on >= today)
    ]


# ── Labels ────────────────────────────────────────────────────────────


def _clock(dt: datetime) -> str:
    return dt.strftime("%-I:%M %p")


def date_label(gig: Gig) -> str:
    start = gig.starts_at.astimezone(gig.zone)
    label = start.strftime("%a, %b %-d")
    days = int(gig.plan.days or 1)
    if days > 1:
        last = start + timedelta(days=days - 1)
        label = f"{label} – {last.strftime('%a, %b %-d')}"
    return label


def time_label(gig: Gig) -> str:
    if gig.event.start_time is None:
        return "Time TBD"
    start = gig.starts_at.astimezone(gig.zone)
    end = gig.ends_at.astimezone(gig.zone) if gig.ends_at else None
    tz = start.strftime("%Z")
    if end is None:
        return f"{_clock(start)} {tz}".strip()
    return f"{_clock(start)} – {_clock(end)} {tz}".strip()


def public_gig(gig: Gig) -> dict:
    """What anyone with the link may see. No address beyond city, no people."""
    return {
        "id": str(gig.event.uuid),
        "startsAt": gig.starts_at.isoformat(),
        "dateLabel": date_label(gig),
        "timeLabel": time_label(gig),
        "market": gig.area,
        "venue": gig.venue,
        "typeLabel": gig.type_label,
        "sampling": SAMPLING_LABELS.get(gig.plan.sampling_format or "", ""),
        "skus": gig.skus,
        "brief": (gig.plan.support_scope or "").strip(),
        "openShifts": gig.open_shifts,
        "spotsLeft": gig.spots_left,
        "full": gig.spots_left <= 0,
    }


# ── Board ─────────────────────────────────────────────────────────────


def board_for_tenant(tenant) -> JobBoard | None:
    if job_board_config_for(tenant) is None:
        return None
    board, _ = JobBoard.objects.get_or_create(tenant=tenant)
    return board


def board_for_token(token: str) -> tuple[JobBoard, JobBoardConfig] | None:
    token = (token or "").strip()
    if not token:
        return None
    board = JobBoard.objects.select_related("tenant").filter(token=token).first()
    if board is None:
        return None
    config = job_board_config_for(board.tenant)
    if config is None:
        return None
    return board, config


def public_board_payload(board: JobBoard, config: JobBoardConfig, *, now=None) -> dict:
    gigs = [g for g in list_gigs(board.tenant, now=now) if g.on_board]
    markets = sorted({g.area for g in gigs if g.area})
    return {
        "brand": {**_brand_payload(board.tenant), "label": config.brand_label},
        "gigs": [public_gig(g) for g in gigs],
        "markets": markets,
        "contactEmail": STAFFING_EMAIL,
    }
