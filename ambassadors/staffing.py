"""Smart staffing — ranked BA suggestions + fill counts for one event.

Read-only GraphQL surface (clients schema) backing the "Smart staffing"
panel: given an event, return how many BAs are assigned / confirmed and a
ranked shortlist of the tenant's BAs to invite next.

Ranking (best first):
  1. available for the event's weekday/time, when known
  2. nearest to the event (haversine on [lat, lng])
  3. highest rating

Everything is tenant-scoped with the same posture as receipts/recaps: a
client-role caller is pinned to their own tenant (and may only inspect an
event in that tenant); admins (spark-admin / staff / super /
@igniteproductions.co) can inspect any tenant's event. The event handle may
be the event UUID (what the web app routes by) OR its numeric pk — mirroring
``recaps.report_service.get_report_request``.

There is no headcount / "ambassadors needed" field on Event or its Request
(verified against the model), so ``needed`` is always null and ``fillRate``
is null with it. The shape carries them so a headcount field can light them
up later with no schema change.
"""

from __future__ import annotations

import math
import uuid as uuid_lib
from typing import List

import strawberry
from asgiref.sync import sync_to_async
from graphql import GraphQLError

from django.db.models import Prefetch

from ambassadors.models import Ambassador, AmbassadorEvent
from ambassadors.staffing_suggestions import suggest_ambassadors_for_event
from availability.models import AmbassadorAvailability
from events.models import Event
from utils.graphql.mixins import SparkGraphQLMixin
from utils.graphql.permissions import (
    IGNITE_EMAIL_DOMAIN,
    StrictIsAuthenticated,
    resolve_request_user_access,
)

# Earth mean radius in miles — for the haversine great-circle distance.
_EARTH_RADIUS_MILES = 3958.7613


# ---------------------------------------------------------------------------
# GraphQL types
# ---------------------------------------------------------------------------
@strawberry.type
class SuggestedAmbassador:
    """One BA in the ranked shortlist for an event."""

    id: strawberry.ID
    uuid: str
    name: str
    rating: int
    # haversine(event.coordinates, ambassador.coordinates); null when either
    # side is missing/malformed. Rounded to 1 decimal place (miles).
    distance_miles: float | None = None
    # weekday/time vs the BA's availability slots; null when the event has no
    # date/time to match against (unknown), False when no slot covers it.
    is_available: bool | None = None
    already_invited: bool
    already_assigned: bool
    email: str | None = None
    phone: str | None = None


@strawberry.type
class EventStaffing:
    """Fill counts + ranked suggestions for one event."""

    event_id: strawberry.ID
    assigned: int
    confirmed: int
    # Headcount target if such a field exists, else null (none exists today).
    needed: int | None = None
    # confirmed/needed when needed>0, else null.
    fill_rate: float | None = None
    suggestions: List[SuggestedAmbassador]


# ---------------------------------------------------------------------------
# Plain-Python helpers
# ---------------------------------------------------------------------------
def _valid_coords(coords) -> tuple[float, float] | None:
    """Return (lat, lng) floats from a ``[lat, lng]`` array, or None.

    Guards the order/shape we store: a 2-element sequence of finite numbers.
    Ambassador.coordinates defaults to ``[]`` (not null) and Event.coordinates
    may be null, so both the empty and missing cases collapse to None here.
    """
    if not coords:
        return None
    try:
        if len(coords) != 2:
            return None
        lat = float(coords[0])
        lng = float(coords[1])
    except (TypeError, ValueError):
        return None
    if math.isnan(lat) or math.isnan(lng) or math.isinf(lat) or math.isinf(lng):
        return None
    # Reject out-of-range values (bad data) rather than emit a bogus distance.
    if not (-90.0 <= lat <= 90.0) or not (-180.0 <= lng <= 180.0):
        return None
    return (lat, lng)


def _haversine_miles(a_coords, b_coords) -> float | None:
    """Great-circle distance in miles between two ``[lat, lng]`` points.

    Returns None when either point is missing/malformed. Rounded to 1dp.
    """
    a = _valid_coords(a_coords)
    b = _valid_coords(b_coords)
    if a is None or b is None:
        return None

    lat1, lng1 = a
    lat2, lng2 = b
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    d_phi = math.radians(lat2 - lat1)
    d_lambda = math.radians(lng2 - lng1)

    h = (
        math.sin(d_phi / 2) ** 2
        + math.cos(phi1) * math.cos(phi2) * math.sin(d_lambda / 2) ** 2
    )
    c = 2 * math.atan2(math.sqrt(h), math.sqrt(1 - h))
    return round(_EARTH_RADIUS_MILES * c, 1)


def _event_when(event: Event):
    """Return (on_date, start_time, end_time) for availability matching.

    Uses the event's start_time/end_time when present (preferred — they carry
    the wall-clock window the BA's availability slots are modeled against),
    falling back to `date` for the day. Returns ``(None, None, None)`` when
    there's nothing to match — the caller maps that to ``is_available=None``.
    """
    start = getattr(event, "start_time", None)
    end = getattr(event, "end_time", None) or getattr(event, "new_end_time", None)
    day = start or getattr(event, "date", None) or end
    if day is None or start is None or end is None:
        return (None, None, None)
    return (day.date(), start.time(), end.time())


def _ba_is_available(slots, on_date, start_t, end_t) -> bool | None:
    """True/False from the BA's availability slots, or None when unknown.

    `slots` is the BA's prefetched availability rows. When the event has no
    usable date/time window (start_t/end_t None) we can't decide → None.
    Otherwise True iff any slot ``covers`` the event window (reusing the
    model's own ``covers`` logic so recurring-weekday vs one-off-date rules
    stay in one place).
    """
    if on_date is None or start_t is None or end_t is None:
        return None
    for slot in slots:
        try:
            if slot.covers(on_date, start_t, end_t):
                return True
        except Exception:  # noqa: BLE001 — never let one bad row blank the field
            continue
    return False


# ---------------------------------------------------------------------------
# Query
# ---------------------------------------------------------------------------
class _StaffingService(SparkGraphQLMixin):
    """Tenant-scoping shell — clients pinned to their own tenant, admins any."""

    async def resolve_scope_tenant_id(self, info: strawberry.Info) -> int | None:
        """Tenant id to scope the event lookup by, or None for an admin."""
        user = await self.get_user(info)
        role_slug, is_staff, is_super, email = await resolve_request_user_access(user)
        is_admin = (
            is_staff
            or is_super
            or role_slug == "spark-admin"
            or (email or "").lower().endswith(IGNITE_EMAIL_DOMAIN)
        )
        if is_admin:
            return None
        # Client (or anything else that got past StrictIsAuthenticated): pin to
        # their own tenant. get_user_tenant raises if they have no tenant.
        tenant = await self.get_user_tenant(info, user=user)
        return tenant.id


@strawberry.type
class StaffingQueries:
    @strawberry.field(permission_classes=[StrictIsAuthenticated])
    async def event_staffing(
        self,
        info: strawberry.Info,
        event_id: strawberry.ID,
        limit: int | None = 20,
    ) -> EventStaffing | None:
        """Fill counts + a ranked BA shortlist for one event.

        `event_id` accepts the event UUID or its numeric pk. Tenant-scoped:
        the event must belong to the caller's tenant (clients) / any tenant
        (admins); returns null when the event doesn't exist or is out of
        scope. Suggestions exclude BAs already assigned to the event and are
        ranked available-first, then nearest, then highest rating, capped at
        `limit`.
        """
        identifier = str(event_id).strip()
        if not identifier:
            return None

        service = _StaffingService()
        scope_tenant_id = await service.resolve_scope_tenant_id(info)

        # Clamp limit to a sane window (the panel shows a shortlist).
        try:
            cap = int(limit)
        except (TypeError, ValueError):
            cap = 20
        cap = max(0, min(cap, 100))

        def _build() -> EventStaffing | None:
            # --- resolve the event (uuid OR pk), tenant-scoped --------------
            event_qs = Event.objects.all()
            if scope_tenant_id is not None:
                event_qs = event_qs.filter(tenant_id=scope_tenant_id)
            try:
                uuid_lib.UUID(identifier)
                event = event_qs.filter(uuid=identifier).first()
            except (ValueError, AttributeError, TypeError):
                try:
                    event = event_qs.filter(id=int(identifier)).first()
                except (ValueError, TypeError):
                    event = None
            if event is None:
                return None

            tenant_id = event.tenant_id

            # --- fill counts ------------------------------------------------
            event_ae = AmbassadorEvent.objects.filter(event_id=event.id)
            assigned = event_ae.count()
            confirmed = event_ae.filter(is_approved=True).count()

            # No headcount field exists on Event/Request → needed/fillRate null.
            needed: int | None = None
            fill_rate: float | None = None

            # --- ranked suggestions ----------------------------------------
            # Ambassadors already assigned to THIS event (any approval state)
            # are excluded from the shortlist.
            assigned_ba_ids = set(
                event_ae.values_list("ambassador_id", flat=True)
            )
            # Ambassadors already invited to this event (== assigned set here,
            # since an invite is an AmbassadorEvent row); kept as its own set
            # so the per-BA flags read clearly even though, post-exclusion,
            # candidates are never in it.
            invited_ba_ids = assigned_ba_ids

            event_coords = getattr(event, "coordinates", None)
            on_date, start_t, end_t = _event_when(event)

            # Tenant's BAs, scoped exactly like the `ambassadors` list query
            # (TenantedUser membership), excluding those already on the event.
            # select_related('user') for name/email; prefetch recurring
            # availability rows so the per-BA availability check is in-memory.
            candidates_qs = (
                Ambassador.objects.filter(
                    user__tenanted_users__tenant_id=tenant_id,
                    user__tenanted_users__is_active=True,
                )
                .exclude(id__in=assigned_ba_ids)
                .select_related("user")
                .prefetch_related(
                    Prefetch(
                        "availability",
                        queryset=AmbassadorAvailability.objects.filter(
                            is_recurring=True
                        ),
                        to_attr="_recurring_availability",
                    )
                )
                .distinct()
            )

            scored: list[tuple] = []
            for ba in candidates_qs:
                distance = _haversine_miles(event_coords, ba.coordinates)
                slots = getattr(ba, "_recurring_availability", []) or []
                available = _ba_is_available(slots, on_date, start_t, end_t)

                user = getattr(ba, "user", None)
                full_name = (user.get_full_name() if user else "") or ""
                email = (getattr(user, "email", None) if user else None) or None
                name = full_name or email or "BA"
                rating = int(getattr(ba, "rating", 0) or 0)

                suggestion = SuggestedAmbassador(
                    id=strawberry.ID(str(ba.id)),
                    uuid=str(ba.uuid),
                    name=name,
                    rating=rating,
                    distance_miles=distance,
                    is_available=available,
                    already_invited=ba.id in invited_ba_ids,
                    already_assigned=ba.id in assigned_ba_ids,
                    email=email,
                    phone=getattr(ba, "phone", None) or None,
                )

                # Sort key (ascending): available-first, then nearest, then
                # highest rating.
                #   available: True→0, unknown(None)→1, False→2
                #   distance:  known→value, unknown→+inf (sinks below known)
                #   rating:    negated so higher sorts earlier
                avail_rank = 0 if available is True else (1 if available is None else 2)
                dist_rank = distance if distance is not None else float("inf")
                scored.append(
                    ((avail_rank, dist_rank, -rating), suggestion)
                )

            scored.sort(key=lambda pair: pair[0])
            suggestions = [s for _, s in scored[:cap]]

            return EventStaffing(
                event_id=strawberry.ID(str(event.uuid)),
                assigned=assigned,
                confirmed=confirmed,
                needed=needed,
                fill_rate=fill_rate,
                suggestions=suggestions,
            )

        return await sync_to_async(_build, thread_sensitive=True)()


# ---------------------------------------------------------------------------
# Smart staffing SUGGESTIONS — transparent weighted best-fit ranking
# ---------------------------------------------------------------------------
@strawberry.type
class StaffingSuggestion:
    """One BA in the weighted best-fit ranking for an event.

    Backed by :func:`ambassadors.staffing_suggestions.suggest_ambassadors_for_event`.
    ``score`` is a transparent 0-100 weighted sum of the signals that exist for
    the BA; ``reasons`` are the short human strings explaining the score
    ("4.8★", "12 gigs for this brand", "available", "favorited", "8 mi away").
    A missing signal is omitted (its field is null / a bool default) — never
    fabricated.
    """

    ba_id: strawberry.ID
    name: str
    score: int
    # This tenant's avg rating (else the denormalized rating), null when the
    # BA has no rating at all.
    avg_rating: float | None = None
    # AmbassadorEvent roster rows the BA has for THIS tenant.
    gigs_for_brand: int = 0
    is_favorited: bool = False
    # Availability vs the event's window; null when the event has no date/time.
    is_available: bool | None = None
    # Haversine miles; null when either the event or the BA lacks coordinates.
    distance_mi: float | None = None
    reasons: List[str] = strawberry.field(default_factory=list)


@strawberry.type
class StaffingSuggestionQueries:
    @strawberry.field(permission_classes=[StrictIsAuthenticated])
    async def staffing_suggestions(
        self,
        info: strawberry.Info,
        event_id: strawberry.ID,
        limit: int | None = 20,
    ) -> List[StaffingSuggestion]:
        """Ranked best-fit BA suggestions for one event (weighted, transparent).

        ``event_id`` accepts the event UUID or its numeric pk. Tenant-scoped via
        the event's tenant: a client-role caller may only inspect events in
        their OWN tenant (``resolve_target_tenant_id`` posture), admins
        (spark-admin / staff / superuser / ``@igniteproductions.co``) may
        inspect any tenant's event. An out-of-scope or unknown event returns an
        EMPTY list (deny/empty, never an error). Every signal — rating, brand
        experience, availability, favorited, proximity — is scoped to the
        event's tenant, so a BA's history for other brands never leaks in.

        Never raises: out-of-scope/missing events and any internal failure all
        degrade to an empty list.
        """
        identifier = str(event_id).strip()
        if not identifier:
            return []

        service = _StaffingService()
        scope_tenant_id = await service.resolve_scope_tenant_id(info)

        try:
            cap = int(limit)
        except (TypeError, ValueError):
            cap = 20

        def _build() -> List[StaffingSuggestion]:
            # Resolve the event (uuid OR pk), tenant-scoped exactly like
            # event_staffing: clients pinned to their own tenant, admins any.
            event_qs = Event.objects.all()
            if scope_tenant_id is not None:
                event_qs = event_qs.filter(tenant_id=scope_tenant_id)
            try:
                uuid_lib.UUID(identifier)
                event = event_qs.filter(uuid=identifier).only(
                    "id", "tenant_id"
                ).first()
            except (ValueError, AttributeError, TypeError):
                try:
                    event = event_qs.filter(id=int(identifier)).only(
                        "id", "tenant_id"
                    ).first()
                except (ValueError, TypeError):
                    event = None
            if event is None:
                return []

            # Score against the event's OWN tenant (the concrete target tenant).
            rows = suggest_ambassadors_for_event(
                event.id, event.tenant_id, limit=cap
            )
            return [
                StaffingSuggestion(
                    ba_id=strawberry.ID(str(row["ba_id"])),
                    name=row["name"],
                    score=row["score"],
                    avg_rating=row["avg_rating"],
                    gigs_for_brand=row["gigs_for_brand"],
                    is_favorited=row["is_favorited"],
                    is_available=row["is_available"],
                    distance_mi=row["distance_mi"],
                    reasons=row["reasons"],
                )
                for row in rows
            ]

        return await sync_to_async(_build, thread_sensitive=True)()
