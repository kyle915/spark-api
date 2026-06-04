"""
Keyless geocoding via the Photon API (https://photon.komoot.io).

Spark has NO server-side Google geocoder and NO server Google API key. The web
public-request form already geocodes addresses with Photon (Komoot's open,
keyless geocoder built on OpenStreetMap), so the backfill commands reuse the
same free service for parity — no key, no billing, no new dependency.

The single function here, :func:`photon_geocode`, is the ONLY place that talks
to the network, which keeps the management commands testable (tests stub this
one call and never hit the wire).

Returned coordinates are ``[lat, lng]`` to match the order stored in the
``Event.coordinates`` / ``Ambassador.coordinates`` / ``Request.coordinates``
ArrayFields (lat first), NOT Photon/GeoJSON's native ``[lng, lat]``.
"""

from __future__ import annotations

import logging

import httpx

logger = logging.getLogger(__name__)

PHOTON_URL = "https://photon.komoot.io/api/"
DEFAULT_TIMEOUT_SECONDS = 10.0


def has_valid_coordinates(coords) -> bool:
    """True when ``coords`` is a usable ``[lat, lng]`` pair.

    Treats the three "needs backfill" shapes as INVALID: ``None`` /
    empty (``[]``), and the ``[0, 0]`` null-island sentinel that some legacy
    rows carry. Used both to pick candidate rows and as the idempotency guard
    so a re-run skips rows that already have real coordinates.
    """
    if not coords or not isinstance(coords, (list, tuple)) or len(coords) < 2:
        return False
    try:
        lat = float(coords[0])
        lng = float(coords[1])
    except (TypeError, ValueError):
        return False
    # [0, 0] is the null-island sentinel — treat as "not geocoded".
    if lat == 0.0 and lng == 0.0:
        return False
    return True


def photon_geocode(
    address: str,
    *,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
) -> list[float] | None:
    """Geocode ``address`` to ``[lat, lng]`` via Photon, best-effort.

    Returns ``[lat, lng]`` (floats) on success, or ``None`` when the address
    is empty, the request fails/times out, or Photon returns no usable
    feature. NEVER raises — callers (backfill commands) treat ``None`` as
    "skip this row" so one bad address can't abort a run.

    Photon's GeoJSON returns coordinates as ``[lng, lat]``; we swap to
    ``[lat, lng]`` to match how Spark stores them.
    """
    address = (address or "").strip()
    if not address:
        return None

    try:
        resp = httpx.get(
            PHOTON_URL,
            params={"q": address, "limit": 1},
            timeout=timeout,
            headers={"User-Agent": "spark-api/geocode-backfill"},
        )
        resp.raise_for_status()
        data = resp.json()
    except (httpx.HTTPError, ValueError) as exc:
        # httpx.HTTPError covers transport + status errors; ValueError covers
        # a non-JSON body. Best-effort: log and signal "skip".
        logger.warning("Photon geocode failed for %r: %s", address, exc)
        return None

    features = (data or {}).get("features") or []
    if not features:
        return None

    geometry = (features[0] or {}).get("geometry") or {}
    coords = geometry.get("coordinates") or []
    # GeoJSON Point: [lng, lat]. Need two finite numbers.
    if not isinstance(coords, (list, tuple)) or len(coords) < 2:
        return None
    try:
        lng = float(coords[0])
        lat = float(coords[1])
    except (TypeError, ValueError):
        return None

    return [lat, lng]


def photon_state_for_address(
    address: str,
    *,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
) -> str | None:
    """Return the US state NAME (e.g. ``"California"``) for ``address`` via
    Photon, or ``None``.

    Lets the RMM-routing backfill resolve a territory for addresses whose
    2-letter state code the regex can't parse but Photon CAN derive (Photon
    already geocoded these for the coordinate backfill). The caller maps the
    returned name to a ``State`` row (and only US states exist there, so an
    international result simply won't match — no bad routing). NEVER raises.
    """
    address = (address or "").strip()
    if not address:
        return None

    try:
        resp = httpx.get(
            PHOTON_URL,
            params={"q": address, "limit": 1},
            timeout=timeout,
            headers={"User-Agent": "spark-api/rmm-routing-backfill"},
        )
        resp.raise_for_status()
        data = resp.json()
    except (httpx.HTTPError, ValueError) as exc:
        logger.warning("Photon state lookup failed for %r: %s", address, exc)
        return None

    features = (data or {}).get("features") or []
    if not features:
        return None
    props = (features[0] or {}).get("properties") or {}
    state = (props.get("state") or "").strip()
    return state or None
