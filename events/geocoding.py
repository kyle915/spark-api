"""Street address → map position, via the Google Geocoding API.

Used only to put an event's pin on the staff maps (see ``events.map_points``).
The result is a *looked-up* position, never a surveyed one, so it is stored
apart from ``Event.coordinates`` — that column feeds clock-in distance checks,
nearby-gig pushes and recap roll-ups, none of which should start acting on a
guess.

Everything that talks to the network goes through :func:`lookup_address`, so
tests patch ``requests.get`` (or that one function) and never hit the wire.
The API key is never logged, printed or returned: every message that could
carry it passes through :func:`_redact`.
"""

from __future__ import annotations

import logging
import re

import requests

logger = logging.getLogger(__name__)

GEOCODE_URL = "https://maps.googleapis.com/maps/api/geocode/json"
TIMEOUT_SECONDS = 6.0

# A fixed, well-known address for the "is the geocoder working" probe.
PROBE_ADDRESS = "1600 Amphitheatre Parkway, Mountain View, CA 94043"

# Google says how it placed the point. ROOFTOP and RANGE_INTERPOLATED are a
# spot on a street. GEOMETRIC_CENTER is the middle of *something* — fine when
# that something is a building or a business, useless when it is a city.
# APPROXIMATE is always city / postcode / state level.
_PRECISE = {"ROOFTOP", "RANGE_INTERPOLATED"}
_PLACE_TYPES = {
    "street_address",
    "premise",
    "subpremise",
    "establishment",
    "point_of_interest",
}

# Outcomes of one lookup.
OK = "ok"
NOT_FOUND = "not_found"  # Google answered; this address has no usable spot.
NO_KEY = "no_key"  # No GOOGLE_MAPS_API_KEY configured.
ERROR = "error"  # Network / HTTP / quota / refused key — try again later.

_KEY_PARAM = re.compile(r"(key=)[^&\s\"']+", re.IGNORECASE)


def _api_key() -> str:
    from utils.map_matching import _google_maps_api_key

    return _google_maps_api_key()


def _redact(text, key: str = "") -> str:
    """``text`` with the API key (and any ``key=…`` query value) removed."""
    out = str(text or "")
    if key:
        out = out.replace(key, "[key]")
    return _KEY_PARAM.sub(r"\1[key]", out)


def valid_lat_lng(lat, lng) -> bool:
    """A real point on Earth that is not the (0, 0) "no fix" placeholder."""
    try:
        lat, lng = float(lat), float(lng)
    except (TypeError, ValueError):
        return False
    if lat != lat or lng != lng:  # NaN
        return False
    if not (-90.0 <= lat <= 90.0 and -180.0 <= lng <= 180.0):
        return False
    return not (lat == 0.0 and lng == 0.0)


def _usable_point(result) -> tuple[float, float] | None:
    """The position in one Google result, or None when it is too vague."""
    if not isinstance(result, dict):
        return None
    geometry = result.get("geometry") or {}
    if not isinstance(geometry, dict):
        return None
    location_type = str(geometry.get("location_type") or "").upper()
    types = {str(t) for t in (result.get("types") or [])}
    if location_type not in _PRECISE and not (
        location_type == "GEOMETRIC_CENTER" and types & _PLACE_TYPES
    ):
        return None
    location = geometry.get("location") or {}
    if not isinstance(location, dict):
        return None
    lat, lng = location.get("lat"), location.get("lng")
    if not valid_lat_lng(lat, lng):
        return None
    return float(lat), float(lng)


def _lookup(address: str) -> tuple[str, tuple[float, float] | None, str, str]:
    """``(outcome, point, reason, message)`` — see :func:`lookup_address`.

    ``reason`` is ours ("HTTP 403", "REQUEST_DENIED"); ``message`` is Google's
    own explanation with the key removed (often empty).
    """
    try:
        address = " ".join(str(address or "").split())
        if not address:
            return NOT_FOUND, None, "empty address", ""
        key = _api_key()
        if not key:
            return NO_KEY, None, "no GOOGLE_MAPS_API_KEY set", ""
        try:
            resp = requests.get(
                GEOCODE_URL,
                params={"address": address, "key": key},
                timeout=TIMEOUT_SECONDS,
            )
        except Exception as exc:  # noqa: BLE001 — the message can hold the URL + key
            return ERROR, None, f"request failed: {type(exc).__name__}", ""

        try:
            data = resp.json()
        except Exception:  # noqa: BLE001
            data = None
        if not isinstance(data, dict):
            data = {}
        message = _redact(data.get("error_message") or "", key).strip()

        if resp.status_code != 200:
            return ERROR, None, f"HTTP {resp.status_code}", message

        status = str(data.get("status") or "").upper()
        if status == "ZERO_RESULTS":
            return NOT_FOUND, None, "no results", ""
        if status != "OK":
            # REQUEST_DENIED, OVER_QUERY_LIMIT, INVALID_REQUEST, UNKNOWN_ERROR…
            return ERROR, None, status or "unreadable reply", message

        for result in data.get("results") or []:
            point = _usable_point(result)
            if point is not None:
                return OK, point, "found", ""
        return NOT_FOUND, None, "only an approximate (city-level) match", ""
    except Exception as exc:  # noqa: BLE001 — a geocoder must never break a caller
        logger.warning("Geocode lookup failed (%s)", type(exc).__name__)
        return ERROR, None, f"unexpected failure: {type(exc).__name__}", ""


def lookup_address(address: str) -> tuple[str, tuple[float, float] | None, str]:
    """Look one address up. Returns ``(outcome, point, detail)``. Never raises.

    ``outcome`` is :data:`OK` (``point`` is ``(lat, lng)``), :data:`NOT_FOUND`
    (Google answered and there is no street-level spot — safe to remember),
    :data:`NO_KEY`, or :data:`ERROR` (anything transient — do not remember).
    ``detail`` is a short, key-free explanation for logs.
    """
    outcome, point, reason, message = _lookup(address)
    return outcome, point, f"{reason}. {message}".strip() if message else reason


def geocode_address(address: str) -> tuple[float, float] | None:
    """``(lat, lng)`` for a street address, or None. Never raises.

    None when there is no key, on any HTTP/JSON failure, when Google has no
    result, when the best result is only city/state level, or for (0, 0).
    """
    outcome, point, _detail = lookup_address(address)
    return point if outcome == OK else None


def geocoding_status() -> str:
    """One plain sentence on whether Google geocoding works right now."""
    outcome, _point, reason, message = _lookup(PROBE_ADDRESS)
    if outcome == OK:
        return "Google geocoding: working."
    if outcome == NO_KEY:
        return "Google geocoding: off (no GOOGLE_MAPS_API_KEY set)."
    return f"Google geocoding: not available ({reason}). {message}".strip()
