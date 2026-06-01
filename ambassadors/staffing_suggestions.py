"""Smart staffing SUGGESTIONS — a transparent weighted best-fit score for BAs.

Given one of a tenant's events, this ranks the tenant's Brand Ambassadors by
how good a fit they are to staff that gig, using ONLY signals that already
exist in the data — no AI, no learned model. Every BA's score is a plain
weighted sum of normalized (0-1) sub-scores, so the number is auditable and
each BA carries a short list of human ``reasons`` ("4.8★", "12 gigs for this
brand", "available", "favorited", "8 mi away") explaining WHY it ranked.

This is the scoring sibling of :mod:`ambassadors.staffing` (the existing
``event_staffing`` panel, which sorts available-then-nearest-then-rating and
also returns fill counts). That module answers "who's free + close + decent?";
THIS one answers "who's the best overall fit?" with a single tunable score and
explicit reasons, reusing the same per-tenant scoping posture and the same
availability / haversine primitives.

Signals scored (each present-or-omitted — a missing signal contributes
nothing and never crashes):

* **Rating** — the BA's average :class:`ambassadors.models.AmbassadorRating`
  score (1-5) ON THIS TENANT's gigs (the same tenant-scoped aggregation the
  BA leaderboard uses in :mod:`recaps.tenant_ba_leaderboard`), falling back to
  the denormalized :attr:`ambassadors.models.Ambassador.rating` (1-5) when the
  BA has no ratings for this tenant. Normalized ``(avg - 1) / 4`` → 0-1.
* **Brand experience** — count of this BA's past
  :class:`ambassadors.models.AmbassadorEvent` roster rows FOR THIS TENANT
  (``tenant_id`` — the leaderboard's ``_shifts_worked``; a BA's work for OTHER
  brands is never counted). Normalized ``min(gigs, CAP) / CAP``.
* **Availability** — does any of the BA's recurring
  :class:`availability.models.AmbassadorAvailability` slots ``cover`` the
  event's weekday/time window (feature #193)? ``True`` → full sub-score,
  ``False`` → 0, unknown (event has no usable date/time) → the signal is
  OMITTED from the weighted average for that BA rather than penalizing them.
* **Favorited** — is the BA on the tenant's shortlist
  (:class:`jobs.models.TenantFavoriteAmbassador`)? Boolean sub-score.
* **Proximity** — haversine miles between the event's and the BA's
  ``coordinates`` (``[lat, lng]``); both exist as real columns. Closer is
  better, linearly to ``MAX_DISTANCE_MILES``. When either side has no usable
  coordinates the signal is OMITTED (we never geocode or guess).

DELIBERATELY SKIPPED — **reliability / on-time / recency.** The BA leaderboard
already established (see
:data:`recaps.tenant_ba_leaderboard.RELIABILITY_SUPPORTED`) that the
attendance data does not cleanly support an on-time metric, so we don't force
one into the fit score.

Scoring weights (sum to 1.0 over the signals that EXIST for a BA):

    rating        0.35   — past quality on this brand is the strongest signal
    availability  0.25   — a BA who can't make the date isn't a fit
    brand_exp     0.20   — knows this brand's playbook / products
    proximity     0.15   — less travel = more reliable show-up, lower cost
    favorited     0.05   — a light "the tenant likes them" nudge, not a thumb

The weights are RE-NORMALIZED per BA over only the signals present for that BA
(a missing signal drops out of both numerator and denominator), so a BA isn't
punished for data we simply don't have — e.g. when the event has no date the
availability weight is removed and the remaining four weights are rescaled to
sum to 1.0. ``score`` is that 0-1 weighted average rendered as an int 0-100.

Everything here is synchronous Django ORM. The single entry point
:func:`suggest_ambassadors_for_event` is wrapped in ``sync_to_async`` by the
GraphQL resolver in :mod:`ambassadors.staffing`. Like the leaderboard and the
report surface, it NEVER raises out of the builder: a failing sub-query
degrades that one signal (or returns an empty list) rather than blowing up.
"""

from __future__ import annotations

import logging
import math
import uuid as uuid_lib

from django.db.models import Avg, Count

from ambassadors.models import Ambassador, AmbassadorEvent, AmbassadorRating
from availability.models import AmbassadorAvailability
from events.models import Event
from jobs.models import TenantFavoriteAmbassador

log = logging.getLogger(__name__)

# Earth mean radius in miles — for the haversine great-circle distance
# (matches ambassadors.staffing._EARTH_RADIUS_MILES).
_EARTH_RADIUS_MILES = 3958.7613

# Hard cap on candidates returned regardless of the caller's ``limit`` — a
# tenant with thousands of BAs still returns a bounded, sortable shortlist
# (mirrors recaps.tenant_ba_leaderboard.MAX_LEADERBOARD_ROWS).
MAX_CANDIDATES = 100

# --- normalization knobs ---------------------------------------------------
# Ratings are 1-5; (avg - 1) / RATING_SPAN maps [1,5] -> [0,1].
RATING_MIN = 1.0
RATING_SPAN = 4.0  # 5 - 1
# Brand-experience saturates: this many gigs for the tenant == a full
# brand-experience sub-score. Beyond it there's no extra credit (a 40-gig and
# a 12-gig veteran are both "deeply experienced" for ranking purposes).
BRAND_EXPERIENCE_CAP = 10
# Proximity decays linearly to zero at this distance; a BA farther than this
# contributes a 0 proximity sub-score (but is NOT excluded).
MAX_DISTANCE_MILES = 100.0

# --- weights (see module docstring) ----------------------------------------
# Documented inline; re-normalized per BA over the signals that exist.
WEIGHT_RATING = 0.35
WEIGHT_AVAILABILITY = 0.25
WEIGHT_BRAND_EXPERIENCE = 0.20
WEIGHT_PROXIMITY = 0.15
WEIGHT_FAVORITED = 0.05


# ---------------------------------------------------------------------------
# Coordinate / distance helpers (same contract as ambassadors.staffing)
# ---------------------------------------------------------------------------
def _valid_coords(coords) -> tuple[float, float] | None:
    """Return ``(lat, lng)`` floats from a ``[lat, lng]`` array, or None.

    Guards the shape we store: a 2-element sequence of finite, in-range
    numbers. ``Ambassador.coordinates`` defaults to ``[]`` (not null) and
    ``Event.coordinates`` may be null, so both empty/missing collapse to None.
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
    d_phi = math.radians(lat2 - lat1)
    d_lambda = math.radians(lng2 - lng1)
    h = (
        math.sin(d_phi / 2) ** 2
        + math.cos(math.radians(lat1))
        * math.cos(math.radians(lat2))
        * math.sin(d_lambda / 2) ** 2
    )
    c = 2 * math.atan2(math.sqrt(h), math.sqrt(1 - h))
    return round(_EARTH_RADIUS_MILES * c, 1)


def _event_when(event: Event):
    """Return ``(on_date, start_time, end_time)`` for availability matching.

    Uses the event's ``start_time``/``end_time`` (the wall-clock window the
    BA's availability slots are modeled against), falling back to ``new_end_time``
    for the end. Returns ``(None, None, None)`` when there's no usable window —
    the caller maps that to ``is_available=None`` AND drops the availability
    weight for every BA. Mirrors ``ambassadors.staffing._event_when``.
    """
    start = getattr(event, "start_time", None)
    end = getattr(event, "end_time", None) or getattr(event, "new_end_time", None)
    day = start or getattr(event, "date", None) or end
    if day is None or start is None or end is None:
        return (None, None, None)
    return (day.date(), start.time(), end.time())


def _ba_is_available(slots, on_date, start_t, end_t) -> bool | None:
    """True/False from the BA's availability slots, or None when unknown.

    ``slots`` is the BA's prefetched recurring availability rows. When the
    event has no usable window we can't decide → None. Otherwise True iff any
    slot ``covers`` the event window (reusing the model's own ``covers`` so the
    recurring-weekday rule lives in one place). A single bad row never blanks
    the field. Mirrors ``ambassadors.staffing._ba_is_available``.
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
# Tenant-scoped signal aggregations (one GROUP BY each, never load all rows)
# ---------------------------------------------------------------------------
def _brand_experience(tenant_id: int) -> dict[int, int]:
    """``{ambassador_id: gigs_for_this_tenant}`` from AmbassadorEvent rows.

    One roster row == one gig the BA worked for this tenant. Scoped by the
    roster row's own ``tenant_id`` (non-null, indexed, equal to the event's
    tenant), so a BA's roster rows for OTHER brands are never counted — the
    same scoping as :func:`recaps.tenant_ba_leaderboard._shifts_worked`.
    Returns an empty map on any failure so this one signal degrades to "no
    brand experience" rather than raising.
    """
    out: dict[int, int] = {}
    try:
        rows = (
            AmbassadorEvent.objects.filter(tenant_id=tenant_id)
            .values("ambassador_id")
            .annotate(_n=Count("id"))
            .values_list("ambassador_id", "_n")
        )
        for ambassador_id, n in rows:
            if ambassador_id is None:
                continue
            out[int(ambassador_id)] = int(n or 0)
    except Exception:  # noqa: BLE001 — degrade this signal, never raise.
        log.exception("staffing_suggestions: brand-experience aggregation failed")
        return {}
    return out


def _avg_ratings(tenant_id: int) -> dict[int, float]:
    """``{ambassador_id: avg_score}`` over this tenant's gig ratings (1-5).

    :class:`AmbassadorRating` carries its own ``tenant_id`` (captured from the
    gig at create time), so scoping by it keeps a BA's ratings from OTHER
    brands out — the same posture as
    :func:`recaps.tenant_ba_leaderboard._ratings`. Both admin- and
    client-authored ratings count toward the mean. Returns an empty map on
    failure (callers fall back to the denormalized ``Ambassador.rating``).
    """
    out: dict[int, float] = {}
    try:
        rows = (
            AmbassadorRating.objects.filter(tenant_id=tenant_id)
            .values("ambassador_id")
            .annotate(_avg=Avg("score"))
            .values_list("ambassador_id", "_avg")
        )
        for ambassador_id, avg_score in rows:
            if ambassador_id is None or avg_score is None:
                continue
            out[int(ambassador_id)] = float(avg_score)
    except Exception:  # noqa: BLE001 — degrade ratings, never raise.
        log.exception("staffing_suggestions: rating aggregation failed")
        return {}
    return out


def _favorited_ids(tenant_id: int) -> set[int]:
    """Set of ambassador ids the tenant has shortlisted.

    :class:`jobs.models.TenantFavoriteAmbassador` is unique per
    (tenant, ambassador). Returns an empty set on failure so the "favorited"
    signal degrades to "nobody favorited" rather than raising.
    """
    try:
        return {
            int(a)
            for a in TenantFavoriteAmbassador.objects.filter(
                tenant_id=tenant_id
            ).values_list("ambassador_id", flat=True)
            if a is not None
        }
    except Exception:  # noqa: BLE001 — degrade this signal, never raise.
        log.exception("staffing_suggestions: favorites lookup failed")
        return set()


# ---------------------------------------------------------------------------
# Per-BA scoring
# ---------------------------------------------------------------------------
def _ba_display_name(ambassador) -> str:
    """Human BA name from the linked user, with stable fallbacks.

    "First Last" when present, else email, else a generic placeholder — the
    same resolution the leaderboard / report roster use, so a BA reads the
    same name across surfaces.
    """
    user = getattr(ambassador, "user", None)
    if user is not None:
        full = " ".join(
            part
            for part in (
                (getattr(user, "first_name", "") or "").strip(),
                (getattr(user, "last_name", "") or "").strip(),
            )
            if part
        ).strip()
        if full:
            return full
        email = getattr(user, "email", None)
        if email:
            return email
    return "(ambassador)"


def _score_ba(
    ba: Ambassador,
    *,
    avg_rating: float | None,
    gigs_for_brand: int,
    is_favorited: bool,
    is_available: bool | None,
    distance_mi: float | None,
) -> dict:
    """Compute one BA's weighted fit dict from already-resolved signals.

    Each signal yields a 0-1 sub-score and contributes its weight ONLY when it
    exists for this BA; ``score`` is the weighted average over the present
    signals (weights re-normalized to sum to 1.0), rendered 0-100. Reasons are
    the short human strings for the signals that materially helped.

    Returns the per-BA dict described in
    :func:`suggest_ambassadors_for_event`.
    """
    # (weight, sub_score) pairs for the signals that EXIST for this BA.
    parts: list[tuple[float, float]] = []
    reasons: list[str] = []

    # --- rating (1-5) → (avg-1)/4 -----------------------------------------
    if avg_rating is not None:
        norm = (avg_rating - RATING_MIN) / RATING_SPAN
        norm = max(0.0, min(1.0, norm))
        parts.append((WEIGHT_RATING, norm))
        # Trim a trailing ".0" so 5.0 reads "5★" but 4.8 stays "4.8★".
        reasons.append(f"{round(avg_rating, 1):g}★")

    # --- availability: True full, False zero, None omitted ----------------
    if is_available is True:
        parts.append((WEIGHT_AVAILABILITY, 1.0))
        reasons.append("available")
    elif is_available is False:
        parts.append((WEIGHT_AVAILABILITY, 0.0))
    # is_available is None → unknown → availability weight omitted entirely.

    # --- brand experience: min(gigs, CAP)/CAP -----------------------------
    if gigs_for_brand > 0:
        norm = min(gigs_for_brand, BRAND_EXPERIENCE_CAP) / BRAND_EXPERIENCE_CAP
        parts.append((WEIGHT_BRAND_EXPERIENCE, norm))
        label = "1 gig for this brand" if gigs_for_brand == 1 else (
            f"{gigs_for_brand} gigs for this brand"
        )
        reasons.append(label)
    else:
        # Zero is a real, known value (not missing data): include it at 0 so a
        # brand-new BA scores below an experienced one, but add no reason.
        parts.append((WEIGHT_BRAND_EXPERIENCE, 0.0))

    # --- proximity: closer better, omitted when no coordinates ------------
    if distance_mi is not None:
        norm = 1.0 - min(distance_mi, MAX_DISTANCE_MILES) / MAX_DISTANCE_MILES
        norm = max(0.0, min(1.0, norm))
        parts.append((WEIGHT_PROXIMITY, norm))
        reasons.append(f"{distance_mi:g} mi away")

    # --- favorited: boolean ----------------------------------------------
    if is_favorited:
        parts.append((WEIGHT_FAVORITED, 1.0))
        reasons.append("favorited")
    else:
        parts.append((WEIGHT_FAVORITED, 0.0))

    # Weighted average over present signals (re-normalize weights to sum 1.0).
    total_weight = sum(w for w, _ in parts)
    if total_weight > 0:
        score01 = sum(w * s for w, s in parts) / total_weight
    else:
        score01 = 0.0
    score = int(round(max(0.0, min(1.0, score01)) * 100))

    return {
        "ba_id": int(ba.id),
        "name": _ba_display_name(ba),
        "score": score,
        "avg_rating": (round(avg_rating, 2) if avg_rating is not None else None),
        "gigs_for_brand": int(gigs_for_brand),
        "is_favorited": bool(is_favorited),
        "is_available": is_available,
        "distance_mi": distance_mi,
        "reasons": reasons,
    }


def suggest_ambassadors_for_event(
    event_id: int, tenant_id: int, limit: int = 20
) -> list[dict]:
    """Rank the tenant's BAs by best-fit for one event (transparent score).

    Args:
        event_id: PK of the event to staff. Must belong to ``tenant_id``
            (the resolver already tenant-scopes the event lookup); if it does
            not, an empty list is returned.
        tenant_id: The concrete tenant whose BA pool + brand-experience /
            rating / favorite signals are scored. Every signal is scoped to
            THIS tenant, so a BA's history for other brands never leaks in.
        limit: Max suggestions to return (clamped to ``[0, MAX_CANDIDATES]``).

    Returns a list of per-BA dicts, best first, each shaped::

        {
            "ba_id": int,
            "name": str,
            "score": int,               # 0-100 weighted fit
            "avg_rating": float | None, # this tenant's avg, else denorm, else None
            "gigs_for_brand": int,      # AmbassadorEvent rows for this tenant
            "is_favorited": bool,
            "is_available": bool | None,  # None when the event has no date/time
            "distance_mi": float | None,  # None when either side lacks coords
            "reasons": list[str],         # e.g. ["4.8★", "12 gigs for this brand"]
        }

    Candidates are the tenant's active BAs (TenantedUser membership, the same
    pool ``ambassadors.staffing`` uses) EXCLUDING anyone already on the event's
    roster (an :class:`AmbassadorEvent` row) — you don't suggest someone
    already booked. A missing signal is simply omitted from a BA's score and
    reasons (degrade gracefully — never crash, never fabricate).

    Ordering: ``score`` desc, then this-brand experience desc, then avg_rating
    desc (unrated last), with a stable ``ba_id`` tiebreak for determinism.

    Never raises: a failing sub-query degrades that signal (see the ``_*``
    helpers) and an unexpected error yields an empty list, matching the
    never-raise posture of the leaderboard / report surface.
    """
    try:
        cap = int(limit)
    except (TypeError, ValueError):
        cap = 20
    cap = max(0, min(cap, MAX_CANDIDATES))
    if cap == 0:
        return []

    try:
        # --- resolve the event WITHIN the tenant (defense in depth) --------
        # The resolver already scoped the lookup, but re-scoping here keeps
        # this callable safe to use directly and guarantees a BA's brand
        # signals are aggregated for the event's own tenant.
        event = (
            Event.objects.filter(id=event_id, tenant_id=tenant_id)
            .only("id", "tenant_id", "coordinates", "date", "start_time",
                  "end_time", "new_end_time")
            .first()
        )
        if event is None:
            return []

        event_coords = getattr(event, "coordinates", None)
        on_date, start_t, end_t = _event_when(event)

        # --- already-on-roster BAs are excluded from suggestions -----------
        assigned_ba_ids = set(
            AmbassadorEvent.objects.filter(event_id=event.id).values_list(
                "ambassador_id", flat=True
            )
        )

        # --- tenant-scoped signal maps (one GROUP BY / lookup each) --------
        brand_exp = _brand_experience(tenant_id)
        avg_ratings = _avg_ratings(tenant_id)
        favorited = _favorited_ids(tenant_id)

        # --- candidate BAs: the tenant's active pool, minus those booked ---
        # select_related('user') for names; prefetch recurring availability so
        # the per-BA availability check is in-memory (same as staffing.py).
        from django.db.models import Prefetch

        candidates = (
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

        results: list[dict] = []
        for ba in candidates:
            ba_id = int(ba.id)

            # rating: tenant avg first, else the denormalized Ambassador.rating
            # (1-5; 0 means "unset" on that int column → treat as no rating).
            avg_rating = avg_ratings.get(ba_id)
            if avg_rating is None:
                denorm = getattr(ba, "rating", 0) or 0
                avg_rating = float(denorm) if denorm > 0 else None

            slots = getattr(ba, "_recurring_availability", []) or []
            results.append(
                _score_ba(
                    ba,
                    avg_rating=avg_rating,
                    gigs_for_brand=brand_exp.get(ba_id, 0),
                    is_favorited=ba_id in favorited,
                    is_available=_ba_is_available(slots, on_date, start_t, end_t),
                    distance_mi=_haversine_miles(event_coords, ba.coordinates),
                )
            )

        # Ordering: score desc, brand experience desc, rating desc (unrated
        # last), then ba_id for a fully deterministic tiebreak.
        def _sort_key(row: dict) -> tuple:
            avg = row["avg_rating"]
            rating_rank = (0, -avg) if avg is not None else (1, 0.0)
            return (-row["score"], -row["gigs_for_brand"], rating_rank, row["ba_id"])

        results.sort(key=_sort_key)
        return results[:cap]
    except Exception:  # noqa: BLE001 — never raise out of the builder.
        log.exception(
            "staffing_suggestions: failed for event=%s tenant=%s",
            event_id,
            tenant_id,
        )
        return []
