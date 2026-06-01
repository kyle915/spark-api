"""Per-BA performance leaderboard for ONE tenant (pure aggregation, no AI).

The Brand-Ambassador sibling of :mod:`recaps.tenant_overview`: instead of
rolling a tenant's whole program into KPI totals, this ranks the *people*
who worked for that tenant. For every BA with any activity FOR THIS TENANT
— a recap filed for the tenant's events, a roster row
(:class:`ambassadors.models.AmbassadorEvent`) on the tenant's events, or a
rating (:class:`ambassadors.models.AmbassadorRating`) on the tenant's gigs
— we compute a compact per-BA dict and sort it into a leaderboard.

Design rules (mirroring :mod:`recaps.tenant_overview`):

* **Every metric is SCOPED TO THIS TENANT.** A BA who also works for other
  brands must not have that activity counted here. Each source is filtered
  to the tenant before it is grouped:

  - recaps — legacy :class:`recaps.models.Recap` through the event
    (``event__tenant_id`` — Recap has no direct tenant FK, the same join
    :mod:`recaps.tenant_overview` uses) + custom
    :class:`recaps.models.CustomRecap` via its direct ``tenant_id``;
  - shifts — :class:`ambassadors.models.AmbassadorEvent` via its own
    ``tenant_id`` (the roster row's tenant, equal to the event's tenant);
  - ratings — :class:`ambassadors.models.AmbassadorRating` via its own
    ``tenant_id`` (set from the gig's tenant at create time).

* **Efficient ORM aggregation, never load every row into Python.** Each
  metric is a single ``.values("ambassador_id").annotate(Count/Avg)``
  GROUP BY evaluated in the database; we only ever materialise the small
  per-BA result maps and the bounded BA-name lookup, never the full recap /
  roster / rating tables.

* **Bounded output.** Only real Spark BAs (an ``Ambassador`` FK) are ranked
  — free-text ``external_ba_name`` credits have no stable id to rank or
  de-duplicate across sources, so they're intentionally excluded (they
  still show on the per-campaign report roster). The result is hard-capped
  at :data:`MAX_LEADERBOARD_ROWS`.

* **Defensive: never raise out of the builder.** Each sub-query is wrapped
  so a single failing metric degrades to "no contribution" (an empty map)
  rather than blowing up the whole leaderboard, matching the never-raise
  posture of the report surface.

``year`` reuses :func:`recaps.tenant_overview._filter_year`: ``None`` is the
all-time path (no extra ``WHERE``), ``Y`` restricts every source to its own
``created_at`` within calendar year ``Y`` (the identical half-open window the
KPI roll-up, monthly trend, and market heatmap use).

On the deliberately-omitted reliability / on-time metric, see
:data:`RELIABILITY_SUPPORTED`.

Everything here is synchronous Django ORM — the GraphQL resolver in
:mod:`recaps.report_types` wraps the single entry point
:func:`tenant_ba_leaderboard` in ``sync_to_async``.
"""

from __future__ import annotations

import logging

from django.db.models import Avg, Count

from ambassadors.models import Ambassador, AmbassadorEvent, AmbassadorRating
from recaps.models import CustomRecap, Recap
from recaps.tenant_overview import _filter_year

log = logging.getLogger(__name__)

# Hard cap on the number of BAs returned. A tenant with thousands of BAs
# still returns a bounded, sortable list; we log when the tail is dropped.
MAX_LEADERBOARD_ROWS = 100

# Reliability / on-time metric: DELIBERATELY OMITTED for the MVP.
#
# The leaderboard shape carries ``reliability_pct`` (always None today) so a
# clean on-time signal can light it up later with no schema change — but the
# current attendance data does NOT cleanly support it:
#
#   * ``ambassadors.models.Attendance`` stamps ``clock_time`` with
#     ``timezone.now()`` at clock-in and the create path
#     (``_record_attendance``) leaves ``attendace_type`` / ``attendance_status``
#     NULL, so neither the clock kind nor a status is reliably populated.
#   * ``AttendanceStatus`` is a free-form, per-tenant lookup whose default
#     templates are administrative (Pending / Approved / Declined) — there is
#     no fixed on-time / late / no-show vocabulary to read.
#   * There is no scheduled-start field stored on the attendance row to
#     compare a clock-in against; ``Event.start_time`` is nullable, so any
#     "late" derivation would need a nullable join + an arbitrary grace window
#     — exactly the messy metric we were asked NOT to force.
#   * ``Recap.late`` is a self/admin-flagged per-recap boolean, not an
#     attendance-derived on-time measure.
#
# So reliability stays None/omitted rather than shipping a misleading number.
RELIABILITY_SUPPORTED = False


def _ba_display_name(ambassador) -> str:
    """Human-readable BA name from the linked user, with stable fallbacks.

    Copies :func:`recaps.report_service._ba_display_name`: "First Last" when
    present, else the user's email, else a generic placeholder — so a BA's
    name reads the same on the leaderboard as on the campaign report roster.
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


def _count_by_ambassador(queryset) -> dict[int, int]:
    """``{ambassador_id: COUNT(*)}`` grouping ``queryset`` by ``ambassador_id``.

    A single database ``GROUP BY`` + ``COUNT`` over the (already
    tenant-scoped) queryset; only the per-BA buckets come back, never the
    rows. Rows with a null ``ambassador_id`` are skipped — they can't be
    attributed to a rankable BA (e.g. an external typed-name recap). Returns
    an empty map on any failure so a single broken metric never raises.
    """
    out: dict[int, int] = {}
    try:
        rows = (
            queryset.values("ambassador_id")
            .annotate(_n=Count("id"))
            .values_list("ambassador_id", "_n")
        )
        for ambassador_id, n in rows:
            if ambassador_id is None:
                continue
            out[int(ambassador_id)] = out.get(int(ambassador_id), 0) + int(n or 0)
    except Exception:  # noqa: BLE001 — degrade this metric, never raise.
        log.exception("tenant_ba_leaderboard: count aggregation failed")
        return {}
    return out


def _ratings_by_ambassador(queryset) -> dict[int, tuple[float, int]]:
    """``{ambassador_id: (avg_score, count)}`` over a rating queryset.

    A single ``GROUP BY`` + ``Avg``/``Count`` on ``score`` in the database
    (the rating rows never enter Python). ``avg_score`` is a float; ``count``
    is the number of ratings. Null ``ambassador_id`` rows are skipped.
    Returns an empty map on any failure so a rating-query problem degrades
    the rating columns to "unrated" rather than raising.
    """
    out: dict[int, tuple[float, int]] = {}
    try:
        rows = (
            queryset.values("ambassador_id")
            .annotate(_avg=Avg("score"), _n=Count("id"))
            .values_list("ambassador_id", "_avg", "_n")
        )
        for ambassador_id, avg_score, n in rows:
            if ambassador_id is None or avg_score is None:
                continue
            out[int(ambassador_id)] = (float(avg_score), int(n or 0))
    except Exception:  # noqa: BLE001 — degrade ratings, never raise.
        log.exception("tenant_ba_leaderboard: rating aggregation failed")
        return {}
    return out


def _shifts_worked(tenant_id: int, year: int | None) -> dict[int, int]:
    """Per-BA shift count: tenant-scoped :class:`AmbassadorEvent` roster rows.

    One roster row == one BA assigned to one of the tenant's events == one
    shift worked. Scoped by the roster row's own ``tenant_id`` (non-null,
    indexed, equal to the event's tenant) and year-filtered on the row's
    ``created_at`` — so a BA's roster rows for OTHER brands are never counted.
    """
    return _count_by_ambassador(
        _filter_year(
            AmbassadorEvent.objects.filter(tenant_id=tenant_id),
            "created_at",
            year,
        )
    )


def _recaps_filed(tenant_id: int, year: int | None) -> dict[int, int]:
    """Per-BA recap count across BOTH recap shapes, tenant-scoped.

    Legacy :class:`recaps.models.Recap` is scoped through the event
    (``event__tenant_id`` — it has no direct tenant FK) and custom
    :class:`recaps.models.CustomRecap` via its direct ``tenant_id``; both are
    restricted to rows with a real ``ambassador`` FK (external typed-name
    credits have no rankable id) and year-filtered on their own
    ``created_at``. The two per-BA maps are summed so a BA who filed both
    shapes gets credit for each, exactly like the recap-count headline in
    :func:`recaps.tenant_overview.tenant_event_recap_counts`.
    """
    legacy = _count_by_ambassador(
        _filter_year(
            Recap.objects.filter(
                event__tenant_id=tenant_id, ambassador__isnull=False
            ),
            "created_at",
            year,
        )
    )
    custom = _count_by_ambassador(
        _filter_year(
            CustomRecap.objects.filter(
                tenant_id=tenant_id, ambassador__isnull=False
            ),
            "created_at",
            year,
        )
    )
    merged: dict[int, int] = dict(legacy)
    for ambassador_id, n in custom.items():
        merged[ambassador_id] = merged.get(ambassador_id, 0) + n
    return merged


def _ratings(tenant_id: int, year: int | None) -> dict[int, tuple[float, int]]:
    """Per-BA ``(avg_score, count)`` over the tenant's gig ratings.

    :class:`ambassadors.models.AmbassadorRating` carries its own ``tenant_id``
    (captured from the gig at create time), so scoping by it keeps a BA's
    ratings from OTHER brands out — even for the same BA. Year-filtered on the
    rating's own ``created_at``. Both admin- and client-authored ratings count
    toward the BA's average (the ``by_client`` flag only governs UI
    visibility, not the performance mean).
    """
    return _ratings_by_ambassador(
        _filter_year(
            AmbassadorRating.objects.filter(tenant_id=tenant_id),
            "created_at",
            year,
        )
    )


def _names_for(ambassador_ids: set[int]) -> dict[int, str]:
    """``{ambassador_id: display_name}`` for a set of BA ids, one query.

    ``select_related('user')`` so the name resolution
    (:func:`_ba_display_name`) reads first/last/email without a per-BA query.
    A bounded lookup over only the BAs that actually appear in the
    leaderboard. Returns an empty map on failure (callers fall back to a
    placeholder name rather than raising).
    """
    if not ambassador_ids:
        return {}
    try:
        rows = Ambassador.objects.filter(id__in=ambassador_ids).select_related(
            "user"
        )
        return {ba.id: _ba_display_name(ba) for ba in rows}
    except Exception:  # noqa: BLE001 — name lookup must never sink the list.
        log.exception("tenant_ba_leaderboard: name lookup failed")
        return {}


def _sort_key(entry: dict) -> tuple:
    """Leaderboard ordering key: avg_rating desc (unrated last), then
    recaps_filed desc, then shifts_worked desc.

    Python sorts ascending, so we negate the "desc" metrics. ``avg_rating``
    is ``None`` for unrated BAs; we map that to a sentinel that sorts AFTER
    every real average (rated BAs first, best first) while keeping rated BAs
    ordered by their score. The final ``ba_id`` tiebreak makes the order
    fully deterministic.
    """
    avg = entry["avg_rating"]
    # rated -> (0, -avg) sorts before unrated (1, 0); higher avg sorts first.
    rating_rank = (0, -avg) if avg is not None else (1, 0.0)
    return (
        rating_rank,
        -entry["recaps_filed"],
        -entry["shifts_worked"],
        entry["ba_id"],
    )


def tenant_ba_leaderboard(tenant_id: int, year: int | None = None) -> list[dict]:
    """Rank the BAs who worked for ONE tenant by performance.

    Returns a list of per-BA dicts, best first, each shaped::

        {
            "ba_id": int,
            "name": str,
            "shifts_worked": int,        # tenant roster rows
            "recaps_filed": int,         # legacy + custom recaps, this tenant
            "avg_rating": float | None,  # None when the BA has no rating
            "ratings_count": int,
            "reliability_pct": int | None,  # always None today (see module docs)
        }

    A BA is included when they have ANY activity FOR THIS TENANT — a recap
    filed for the tenant's events, a roster/shift on the tenant's events, OR
    a rating on the tenant's gigs. EVERY metric is scoped to this tenant, so
    a BA who also works for other brands contributes only their work for
    *this* one here.

    ``year=None`` is all-time; ``year=Y`` restricts every metric to its own
    ``created_at`` within calendar year ``Y`` (reusing
    :func:`recaps.tenant_overview._filter_year`).

    Ordering: ``avg_rating`` desc (unrated BAs last), then ``recaps_filed``
    desc, then ``shifts_worked`` desc, with a stable ``ba_id`` tiebreak. The
    result is capped at :data:`MAX_LEADERBOARD_ROWS`; the drop is logged.

    Never raises: a failing sub-query degrades that metric to zero/unrated
    (see the ``_*`` helpers) and an unexpected error yields an empty list,
    matching the never-raise posture of the rest of the report surface. Only
    real Spark BAs are ranked; free-text ``external_ba_name`` credits are
    excluded (no stable id to rank/de-dupe).
    """
    try:
        shifts = _shifts_worked(tenant_id, year)
        recaps = _recaps_filed(tenant_id, year)
        ratings = _ratings(tenant_id, year)
    except Exception:  # noqa: BLE001 — belt-and-suspenders; helpers already guard.
        log.exception(
            "tenant_ba_leaderboard: aggregation failed for tenant %s", tenant_id
        )
        return []

    # The BA universe is every id that showed up in ANY tenant-scoped source.
    ba_ids: set[int] = set(shifts) | set(recaps) | set(ratings)
    if not ba_ids:
        return []

    names = _names_for(ba_ids)

    entries: list[dict] = []
    for ba_id in ba_ids:
        avg_count = ratings.get(ba_id)
        if avg_count is not None:
            avg_rating: float | None = round(avg_count[0], 2)
            ratings_count = avg_count[1]
        else:
            avg_rating = None
            ratings_count = 0
        entries.append(
            {
                "ba_id": ba_id,
                "name": names.get(ba_id) or "(ambassador)",
                "shifts_worked": shifts.get(ba_id, 0),
                "recaps_filed": recaps.get(ba_id, 0),
                "avg_rating": avg_rating,
                "ratings_count": ratings_count,
                # Omitted for the MVP — see RELIABILITY_SUPPORTED.
                "reliability_pct": None,
            }
        )

    entries.sort(key=_sort_key)

    if len(entries) > MAX_LEADERBOARD_ROWS:
        log.info(
            "tenant_ba_leaderboard: tenant %s has %d BAs; truncating to %d",
            tenant_id,
            len(entries),
            MAX_LEADERBOARD_ROWS,
        )
        entries = entries[:MAX_LEADERBOARD_ROWS]

    return entries
