"""Dismissed missing recaps: "no recap is owed here".

Staff dismiss a Missing Recaps row that will never get a recap (no-show,
test shift, duplicate event, filed outside Spark, client said not needed).
The record is a :class:`recaps.models.RecapDismissal`; this module is the
one place that says what a dismissal means, so every list, count and
reminder that chases a missing recap asks the same question.

Two shapes:

* **pair** — one ambassador on one event. That ambassador is no longer
  chased for that event.
* **event** — ``ambassador`` NULL. Nobody is chased for that event.

An event drops off Missing Recaps when it is dismissed as a whole, or when
at least one ambassador on it was dismissed and nobody assigned to it is
left un-dismissed. Assign someone new and the event is owed again.

Time-sheet-only brands (``Tenant.checkin_timesheet_only``) owe no recap
for ANY shift: their link is a plain time clock. They are answered here as if
every event were dismissed as a whole, so Missing Recaps, its badge, the
recap texts and nudges, brand dashboards and digests all skip them without
each learning a second rule. No RecapDismissal row is written for them.

A dismissal never blocks filing. If a recap is filed afterwards the event
was no longer "missing" anyway and the dismissal simply stops mattering.
Hours, payroll and KPIs never read this module.
"""

from __future__ import annotations

from django.db import IntegrityError, transaction
from django.db.models import Exists, OuterRef, Q, QuerySet
from django.utils import timezone


class DismissalError(ValueError):
    """A dismiss / restore request that cannot be carried out as asked."""


def active_dismissals() -> QuerySet:
    """Dismissals in force (not restored)."""
    from recaps.models import RecapDismissal

    return RecapDismissal.objects.filter(restored_at__isnull=True)


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------


def is_recap_dismissed(*, ambassador_id: int | None, event_id: int) -> bool:
    """True when this ambassador is not owed-for on this event: their own
    row was dismissed, or the event was dismissed as a whole. One indexed
    lookup."""
    q = Q(ambassador__isnull=True)
    if ambassador_id is not None:
        q |= Q(ambassador_id=ambassador_id)
    if active_dismissals().filter(event_id=event_id).filter(q).exists():
        return True
    return _timesheet_only_event_ids([event_id]) != set()


def _timesheet_only_event_ids(event_ids) -> set[int]:
    """The subset of ``event_ids`` whose brand runs a time-sheet-only link."""
    from events.models import Event

    ids = {i for i in event_ids if i is not None}
    if not ids:
        return set()
    return set(
        Event.objects.filter(
            id__in=ids, tenant__checkin_timesheet_only=True
        ).values_list("id", flat=True)
    )


class DismissedLookup:
    """Dismissals for a batch of events, answered in memory.

    One query for the whole batch — for sweeps that already hold a list of
    (event, ambassador) pairs and must not ask per row.
    """

    def __init__(self, event_ids):
        ids = {i for i in event_ids if i is not None}
        self.pairs: set[tuple[int, int]] = set()
        self.events: set[int] = set()
        if not ids:
            return
        # Time-sheet-only brands: the whole event is owed nothing.
        self.events |= _timesheet_only_event_ids(ids)
        for event_id, ambassador_id in active_dismissals().filter(
            event_id__in=ids
        ).values_list("event_id", "ambassador_id"):
            if ambassador_id is None:
                self.events.add(event_id)
            else:
                self.pairs.add((event_id, ambassador_id))

    def covers(self, event_id: int, ambassador_id: int | None) -> bool:
        return event_id in self.events or (event_id, ambassador_id) in self.pairs

    def event_is_dismissed(self, event_id: int, roster_ambassador_ids) -> bool:
        """Same rule as :func:`event_dismissed_q`, off loaded rows."""
        if event_id in self.events:
            return True
        dismissed = {a for (e, a) in self.pairs if e == event_id}
        if not dismissed:
            return False
        return all(a in dismissed for a in roster_ambassador_ids)


def event_dismissed_q() -> Q:
    """Filter for an ``Event`` queryset: the event owes no recap because of
    dismissals. Indexed ``EXISTS`` subqueries only; nothing is joined in.
    """
    from ambassadors.models import AmbassadorEvent

    in_force = active_dismissals().filter(event_id=OuterRef("pk"))
    whole_event = Exists(in_force.filter(ambassador__isnull=True))
    any_pair = Exists(in_force.filter(ambassador__isnull=False))
    # Someone assigned to the event whose own row was not dismissed.
    still_owed = Exists(
        AmbassadorEvent.objects.filter(event_id=OuterRef("pk")).exclude(
            Exists(
                active_dismissals().filter(
                    event_id=OuterRef("event_id"),
                    ambassador_id=OuterRef("ambassador_id"),
                )
            )
        )
    )
    return (
        Q(whole_event)
        | (Q(any_pair) & ~Q(still_owed))
        # Time-sheet-only brand: a time clock, never a recap.
        | Q(tenant__checkin_timesheet_only=True)
    )


def exclude_dismissed_events(qs: QuerySet) -> QuerySet:
    """Drop events that owe no recap because of dismissals."""
    return qs.exclude(event_dismissed_q())


def request_dismissed_q() -> Q:
    """Filter for a ``Request`` queryset: it has events, and every one of
    them was dismissed — so the request owes no recap."""
    from events.models import Event

    events = Event.objects.filter(request_id=OuterRef("pk"))
    return Q(Exists(events.filter(event_dismissed_q()))) & ~Q(
        Exists(events.exclude(event_dismissed_q()))
    )


# ---------------------------------------------------------------------------
# Writes
# ---------------------------------------------------------------------------


def reason_label(reason: str) -> str:
    from recaps.models import RecapDismissal

    return dict(RecapDismissal.REASON_CHOICES).get(reason, reason)


def clean_reason_and_note(reason: str | None, note: str | None) -> tuple[str, str]:
    from recaps.models import RecapDismissal

    reason = (reason or "").strip()
    if reason not in dict(RecapDismissal.REASON_CHOICES):
        raise DismissalError("Choose a reason.")
    note = " ".join((note or "").split())
    if len(note) > RecapDismissal.NOTE_MAX_LENGTH:
        raise DismissalError(
            f"Keep the note under {RecapDismissal.NOTE_MAX_LENGTH} characters."
        )
    if reason == RecapDismissal.REASON_OTHER and not note:
        raise DismissalError("Add a short note for \"Other\".")
    return reason, note


def dismiss_missing_recaps(
    *,
    user,
    ambassador_event_uuids=(),
    event_uuids=(),
    reason: str | None,
    note: str | None = None,
) -> list:
    """Record dismissals; returns the rows now in force for what was asked.

    ``ambassador_event_uuids`` are the Missing Recaps rows (one ambassador
    on one event). ``event_uuids`` dismiss a whole event — for a shift with
    nobody assigned. A row already dismissed is returned as it stands (its
    reason is not rewritten). Nothing else is written.
    """
    from ambassadors.models import AmbassadorEvent
    from events.models import Event
    from recaps.models import RecapDismissal

    reason, note = clean_reason_and_note(reason, note)
    ae_uuids = list(dict.fromkeys(str(u) for u in (ambassador_event_uuids or []) if u))
    ev_uuids = list(dict.fromkeys(str(u) for u in (event_uuids or []) if u))
    if not ae_uuids and not ev_uuids:
        raise DismissalError("Nothing selected to dismiss.")
    if len(ae_uuids) + len(ev_uuids) > 500:
        raise DismissalError("Dismiss at most 500 rows at a time.")

    from django.core.exceptions import ValidationError

    try:
        rosters = list(
            AmbassadorEvent.objects.select_related("event").filter(
                uuid__in=ae_uuids, event__isnull=False, ambassador__isnull=False
            )
        )
        events = list(Event.objects.filter(uuid__in=ev_uuids))
    except (ValueError, TypeError, ValidationError) as exc:  # malformed uuid
        raise DismissalError("That shift could not be found.") from exc
    if len(rosters) != len(ae_uuids) or len(events) != len(ev_uuids):
        raise DismissalError(
            "One of those shifts could not be found. Refresh and try again."
        )

    targets: list[tuple[object, int | None]] = [
        (r.event, r.ambassador_id) for r in rosters
    ] + [(ev, None) for ev in events]

    out = []
    for event, ambassador_id in targets:
        if not getattr(event, "tenant_id", None):
            raise DismissalError("That shift has no brand and cannot be dismissed.")
        lookup = dict(
            event_id=event.id, ambassador_id=ambassador_id, restored_at__isnull=True
        )
        row = RecapDismissal.objects.filter(**lookup).first()
        if row is None:
            try:
                with transaction.atomic():
                    row = RecapDismissal.objects.create(
                        tenant_id=event.tenant_id,
                        event_id=event.id,
                        ambassador_id=ambassador_id,
                        reason=reason,
                        note=note,
                        dismissed_by=user,
                    )
            except IntegrityError:
                # Two people dismissed the same row at once: keep the first.
                row = RecapDismissal.objects.filter(**lookup).first()
                if row is None:
                    raise
        out.append(row)
    # Two bookings for the same person on the same event are one dismissal.
    return list({row.id: row for row in out}.values())


def restore_missing_recaps(*, user, dismissal_uuids) -> int:
    """Take dismissals out of force. Returns how many were restored; a row
    already restored counts for nothing. Rows are kept, never deleted."""
    uuids = list(dict.fromkeys(str(u) for u in (dismissal_uuids or []) if u))
    if not uuids:
        raise DismissalError("Nothing selected to restore.")
    from django.core.exceptions import ValidationError

    try:
        return active_dismissals().filter(uuid__in=uuids).update(
            restored_at=timezone.now(), restored_by=user
        )
    except (ValueError, TypeError, ValidationError) as exc:
        raise DismissalError("That dismissal could not be found.") from exc
