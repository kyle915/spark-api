"""Which events are cancelled or deleted, for anything that messages a BA.

`events/demo_cancel.py` moves a cancelled demo's request to the tenant's
declined/cancelled status and each linked event to its suspended/declined/
cancelled one. It deliberately leaves the bookings (AmbassadorEvent /
AmbassadorJob) in place, so every reminder, nudge and text that selects
"approved bookings starting soon" has to exclude cancelled events itself or
the BA is told to show up for a demo that is not happening.

One definition here so the shift texts, the 24h/3h/15m reminders and the
no-show nudge agree on what "cancelled" means.
"""
from __future__ import annotations

from django.db.models import Q

CANCELLED_REQUEST_SLUGS = ("declined", "cancelled", "canceled")
CANCELLED_EVENT_SLUGS = ("suspended", "declined", "cancelled", "canceled")


def live_event_q(prefix: str = "event__") -> Q:
    """Events whose request was not deleted (standalone events always pass)."""
    return Q(**{f"{prefix}request__isnull": True}) | Q(
        **{f"{prefix}request__deleted_at__isnull": True}
    )


def not_cancelled_q(prefix: str = "event__") -> Q:
    """Events that were not cancelled, directly or through their request."""
    return ~Q(**{f"{prefix}status__slug__in": CANCELLED_EVENT_SLUGS}) & ~Q(
        **{f"{prefix}request__status__slug__in": CANCELLED_REQUEST_SLUGS}
    )


def active_event_q(prefix: str = "event__") -> Q:
    """Not cancelled and not deleted: still a shift someone should work."""
    return live_event_q(prefix) & not_cancelled_q(prefix)


def _slug(obj) -> str:
    return (getattr(getattr(obj, "status", None), "slug", None) or "").strip().lower()


def event_is_called_off(event) -> bool:
    """Python-side twin of ``active_event_q`` for one already-loaded event.

    For senders that re-check a row right before delivering, since a demo can
    be cancelled between the cron's select and the send.
    """
    if event is None:
        return True
    if _slug(event) in CANCELLED_EVENT_SLUGS:
        return True
    request = getattr(event, "request", None) if getattr(event, "request_id", None) else None
    if request is not None:
        if getattr(request, "deleted_at", None) is not None:
            return True
        if _slug(request) in CANCELLED_REQUEST_SLUGS:
            return True
    return False
