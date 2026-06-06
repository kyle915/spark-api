"""BA reliability scoring — a single dependability signal from shift history.

A BA's reliability is computed from three behaviors, all already recorded:

* **completed** — approved :class:`AmbassadorEvent` rows whose event is in the
  past. A BA who drops a booked shift has that assignment *deleted*
  (``release_my_shift`` calls ``ae.delete()``), so a surviving past+approved
  assignment means they actually worked it.
* **dropped** — :class:`OpenShift` rows they *released* (``released_by``). The
  one negative signal.
* **claimed** — :class:`OpenShift` rows they *picked up* (``claimed_by``):
  stepping in to cover someone else's freed slot. A strong positive signal.

    score = round(100 * (completed + claimed) / (completed + claimed + dropped))

…or ``None`` when the BA has no history yet ("New"). The score is monotonic:
more completed/claimed pushes it up, more dropped pulls it down. Surfaced on
the admin BA detail page and used to order open-shift alerts (most reliable
first), so when a fan-out is capped the dependable BAs are the ones pinged.

Everything is DB aggregates (three grouped COUNTs for a batch of users); no row
enters Python. Use :func:`reliability_for_users` for batches (e.g. ranking an
alert pool) and :func:`compute_reliability` for a single BA.
"""

from __future__ import annotations

from dataclasses import dataclass

from django.db.models import Count
from django.utils import timezone

# New BAs (no completed/dropped/claimed history) have score=None. When ordering
# an alert pool we can't sort None, so they slot in at this neutral rank —
# benefit of the doubt: below a proven-reliable BA, above a known dropper.
NEUTRAL_SORT_SCORE = 70


@dataclass(frozen=True)
class Reliability:
    """One BA's reliability snapshot. ``score`` is 0–100, or ``None`` if the BA
    has no shift history yet (label ``"New"``)."""

    completed: int
    dropped: int
    claimed: int
    score: int | None
    label: str

    @property
    def sort_score(self) -> int:
        """Sort key for ranking pools — new BAs get the neutral rank."""
        return self.score if self.score is not None else NEUTRAL_SORT_SCORE


def _label(score: int | None) -> str:
    if score is None:
        return "New"
    if score >= 90:
        return "Excellent"
    if score >= 70:
        return "Reliable"
    if score >= 40:
        return "Mixed"
    return "Needs attention"


def _score(completed: int, dropped: int, claimed: int) -> int | None:
    denom = completed + claimed + dropped
    if denom <= 0:
        return None
    return round(100 * (completed + claimed) / denom)


def reliability_for_users(
    user_ids, now=None
) -> dict[int, Reliability]:
    """Reliability for many users at once — three grouped COUNTs, no N+1.

    Returns a ``{user_id: Reliability}`` map covering every distinct truthy id
    passed in (a user with no history maps to an all-zero ``Reliability`` with
    ``score=None`` / label ``"New"``).
    """
    from ambassadors.models import AmbassadorEvent, OpenShift

    now = now or timezone.now()
    ids = sorted({uid for uid in user_ids if uid})
    out: dict[int, Reliability] = {}
    if not ids:
        return out

    # completed — approved, past assignments grouped by the BA's user id.
    completed_counts: dict[int, int] = {}
    for row in (
        AmbassadorEvent.objects.filter(
            ambassador__user_id__in=ids,
            is_approved=True,
            event__start_time__lt=now,
        )
        .values("ambassador__user_id")
        .annotate(c=Count("id"))
    ):
        completed_counts[row["ambassador__user_id"]] = row["c"]

    # dropped — open shifts they released.
    dropped_counts: dict[int, int] = {}
    for row in (
        OpenShift.objects.filter(released_by_id__in=ids)
        .values("released_by_id")
        .annotate(c=Count("id"))
    ):
        dropped_counts[row["released_by_id"]] = row["c"]

    # claimed — open shifts they picked up.
    claimed_counts: dict[int, int] = {}
    for row in (
        OpenShift.objects.filter(claimed_by_id__in=ids)
        .values("claimed_by_id")
        .annotate(c=Count("id"))
    ):
        claimed_counts[row["claimed_by_id"]] = row["c"]

    for uid in ids:
        completed = completed_counts.get(uid, 0)
        dropped = dropped_counts.get(uid, 0)
        claimed = claimed_counts.get(uid, 0)
        score = _score(completed, dropped, claimed)
        out[uid] = Reliability(
            completed=completed,
            dropped=dropped,
            claimed=claimed,
            score=score,
            label=_label(score),
        )
    return out


def compute_reliability(user_id: int, now=None) -> Reliability:
    """Reliability for a single BA (by user id)."""
    result = reliability_for_users([user_id], now=now)
    return result.get(
        user_id,
        Reliability(completed=0, dropped=0, claimed=0, score=None, label="New"),
    )
