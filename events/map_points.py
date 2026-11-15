"""Where an event goes on a map.

One answer for every map: the event's own ``coordinates`` when it has a real
pair (``source="event"``), otherwise the position looked up from its address
and stored in :class:`events.models.EventMapPoint` (``source="geocoded"``).

Nothing here writes to ``Event.coordinates`` — see ``EventMapPoint`` for why.
"""

from __future__ import annotations

import hashlib
import re

from events.geocoding import in_service_area

SOURCE_EVENT = "event"
SOURCE_GEOCODED = "geocoded"

_PUNCT = re.compile(r"[^\w\s#]+")


def normalise_address(address) -> str:
    """Case, punctuation and spacing folded away, so "123 Main St., Austin"
    and "123 main st austin" are the same address."""
    text = _PUNCT.sub(" ", str(address or "").lower())
    return " ".join(text.split())


def address_hash(address) -> str:
    return hashlib.sha256(normalise_address(address).encode("utf-8")).hexdigest()


def is_lookup_worthy(address) -> bool:
    """A street address, not "TBD" or a bare city: has a digit, length ≥ 8."""
    text = " ".join(str(address or "").split())
    return len(text) >= 8 and any(ch.isdigit() for ch in text)


def own_point(event) -> tuple[float, float] | None:
    """The event's own ``coordinates`` as ``(lat, lng)`` when they are a
    valid pair; None for null / ``[]`` / ``[0, 0]`` / out-of-range, and for
    a point outside North America (a wrong address match, see
    :func:`events.geocoding.in_service_area`)."""
    coords = getattr(event, "coordinates", None)
    if not coords or not isinstance(coords, (list, tuple)) or len(coords) < 2:
        return None
    if not in_service_area(coords[0], coords[1]):
        return None
    return float(coords[0]), float(coords[1])


def _stored_point(event, row) -> tuple[float, float] | None:
    """The looked-up position in ``row`` if it is still about this event's
    current address (an edited address makes the old point stale)."""
    if row is None or row.status != "ok":
        return None
    if row.address_hash != address_hash(getattr(event, "address", None)):
        return None
    if not in_service_area(row.lat, row.lng):
        return None
    return float(row.lat), float(row.lng)


def event_map_points(events) -> dict[int, tuple[float, float, str]]:
    """``{event_id: (lat, lng, source)}`` for the events that have a position.

    One query for the whole list, however long (none at all when every event
    has its own coordinates). Events with no position are absent.
    """
    from events.models import EventMapPoint

    out: dict[int, tuple[float, float, str]] = {}
    missing = {}
    for event in events:
        point = own_point(event)
        if point is not None:
            out[event.id] = (point[0], point[1], SOURCE_EVENT)
        else:
            missing[event.id] = event
    if missing:
        rows = EventMapPoint.objects.filter(
            event_id__in=list(missing), status=EventMapPoint.STATUS_OK
        )
        for row in rows:
            point = _stored_point(missing[row.event_id], row)
            if point is not None:
                out[row.event_id] = (point[0], point[1], SOURCE_GEOCODED)
    return out


def event_map_point(event) -> tuple[float, float, str] | None:
    """``(lat, lng, source)`` for one event, or None when it has no position.
    For a list of events use :func:`event_map_points` (one query, not N)."""
    return event_map_points([event]).get(event.id)
