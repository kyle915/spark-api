"""
Keyless GPS map-matching via OSRM (https://project-osrm.org).

The GPS mileage tracker records a raw breadcrumb trail; summing straight-line
(haversine) hops between sparse points UNDERSHOOTS real road distance and the
path doesn't follow streets. OSRM's `/match` service snaps a noisy GPS trace
onto the road network and returns BOTH the matched road distance and the
snapped geometry — so we get accurate reimbursement mileage AND a "where they
drove" route to draw on a map.

Keyless, no API key, no per-use billing (same posture as the Photon geocoder
in utils/geocoding.py). `OSRM_BASE_URL` defaults to the public demo server and
can be pointed at a self-hosted OSRM via env var for production volume.

The single network function, :func:`osrm_match`, is the ONLY place that talks
to the wire, so the mileage stop path stays testable (tests stub this call).
It NEVER raises — on any failure it returns ``None`` and the caller falls back
to the haversine sum.
"""

from __future__ import annotations

import logging
import os

import httpx

logger = logging.getLogger(__name__)

# Public OSRM demo server by default; override with a self-hosted instance for
# production volume (the demo server is rate-limited + best-effort).
OSRM_BASE_URL = os.environ.get(
    "OSRM_BASE_URL", "https://router.project-osrm.org"
).rstrip("/")
DEFAULT_TIMEOUT_SECONDS = 6.0
_METERS_PER_MILE = 1609.344
# OSRM's /match caps coordinates per request (public demo = 100). Downsample
# longer traces evenly so a long drive still matches in one call.
_MAX_POINTS = 100


def _downsample(points: list, limit: int = _MAX_POINTS) -> list:
    """Evenly thin ``points`` to at most ``limit``, always keeping the first
    and last fix so the route's endpoints are preserved."""
    n = len(points)
    if n <= limit:
        return list(points)
    step = (n - 1) / (limit - 1)
    idxs = sorted({round(i * step) for i in range(limit)})
    return [points[i] for i in idxs]


def osrm_match(
    points: list,
    *,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
) -> dict | None:
    """Map-match an ordered GPS trail to roads via OSRM, best-effort.

    ``points`` is an ordered list of ``(lat, lng)`` pairs. Returns
    ``{"miles": float, "route": [[lat, lng], ...]}`` — the matched road
    distance in miles and the snapped road geometry (lat,lng order, to match
    how Spark stores coordinates) — or ``None`` when there aren't enough
    points, the request fails/times out, or OSRM can't match the trace.
    NEVER raises: the caller treats ``None`` as "fall back to haversine".
    """
    # Need at least two points to form a path.
    pts = [
        (float(p[0]), float(p[1]))
        for p in (points or [])
        if p is not None and len(p) >= 2
    ]
    if len(pts) < 2:
        return None
    pts = _downsample(pts)

    # OSRM wants lng,lat;lng,lat;... order.
    coord_str = ";".join(f"{lng:.6f},{lat:.6f}" for (lat, lng) in pts)
    url = f"{OSRM_BASE_URL}/match/v1/driving/{coord_str}"

    try:
        resp = httpx.get(
            url,
            params={
                "overview": "full",
                "geometries": "geojson",  # avoids polyline decoding
                "tidy": "true",  # clean noisy / duplicated GPS fixes
                "gaps": "ignore",  # don't split the trace on time gaps
            },
            timeout=timeout,
            headers={"User-Agent": "spark-api/mileage-match"},
        )
        resp.raise_for_status()
        data = resp.json()
    except (httpx.HTTPError, ValueError) as exc:
        logger.warning("OSRM match failed (%d pts): %s", len(pts), exc)
        return None

    if (data or {}).get("code") != "Ok":
        return None
    matchings = data.get("matchings") or []
    if not matchings:
        return None

    total_meters = 0.0
    route: list[list[float]] = []
    for m in matchings:
        try:
            total_meters += float(m.get("distance") or 0.0)
        except (TypeError, ValueError):
            pass
        coords = ((m.get("geometry") or {}).get("coordinates")) or []
        for c in coords:
            # GeoJSON LineString points are [lng, lat]; store [lat, lng].
            if isinstance(c, (list, tuple)) and len(c) >= 2:
                try:
                    route.append([float(c[1]), float(c[0])])
                except (TypeError, ValueError):
                    continue

    if total_meters <= 0 or not route:
        return None

    return {"miles": round(total_meters / _METERS_PER_MILE, 2), "route": route}


def osrm_route(
    start: tuple,
    end: tuple,
    *,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
) -> dict | None:
    """Driving distance + road geometry between TWO points, best-effort.

    The odometer counterpart to :func:`osrm_match`. Map-matching needs a dense
    trace to snap; a browser can't produce one (mobile Safari suspends JS and
    geolocation the moment the screen locks, which is most of a drive), so the
    web mileage flow records only a start fix and an end fix and asks OSRM to
    ROUTE between them. That yields real distance over real streets instead of
    a straight line through buildings — for a store-to-store or home-to-event
    leg it's within a few percent of the driven distance.

    It is NOT the same claim as the app's breadcrumb trail: this is "the road
    distance between where they started and where they stopped", which assumes
    a sensible route and no detours. Callers stamp ``route_source="osrm_route"``
    so a reader can tell the two apart later.

    ``start`` / ``end`` are ``(lat, lng)``. Returns
    ``{"miles": float, "route": [[lat, lng], ...]}`` or ``None`` on any
    failure. NEVER raises.
    """
    try:
        pts = [
            (float(start[0]), float(start[1])),
            (float(end[0]), float(end[1])),
        ]
    except (TypeError, ValueError, IndexError):
        return None
    if any(lat == 0.0 and lng == 0.0 for lat, lng in pts):
        return None  # null island = no fix

    coord_str = ";".join(f"{lng:.6f},{lat:.6f}" for (lat, lng) in pts)
    url = f"{OSRM_BASE_URL}/route/v1/driving/{coord_str}"

    try:
        resp = httpx.get(
            url,
            params={"overview": "full", "geometries": "geojson"},
            timeout=timeout,
            headers={"User-Agent": "spark-api/mileage-route"},
        )
        resp.raise_for_status()
        data = resp.json()
    except (httpx.HTTPError, ValueError) as exc:
        logger.warning("OSRM route failed: %s", exc)
        return None

    if (data or {}).get("code") != "Ok":
        return None
    routes = data.get("routes") or []
    if not routes:
        return None

    best = routes[0]
    try:
        meters = float(best.get("distance") or 0.0)
    except (TypeError, ValueError):
        return None
    if meters <= 0:
        return None

    route: list[list[float]] = []
    for c in ((best.get("geometry") or {}).get("coordinates")) or []:
        if isinstance(c, (list, tuple)) and len(c) >= 2:
            try:
                route.append([float(c[1]), float(c[0])])  # [lng,lat] -> [lat,lng]
            except (TypeError, ValueError):
                continue

    return {"miles": round(meters / _METERS_PER_MILE, 2), "route": route}


def _google_maps_api_key() -> str:
    try:
        from django.conf import settings

        return (getattr(settings, "GOOGLE_MAPS_API_KEY", None) or "").strip()
    except Exception:  # noqa: BLE001 — settings may be unavailable in some tests
        return (os.environ.get("GOOGLE_MAPS_API_KEY") or "").strip()


def google_directions_route_miles(
    points: list,
    *,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
) -> dict | None:
    """Driving distance via Google Directions API (ordered waypoints).

    Same contract as :func:`osrm_route_waypoints`: returns
    ``{"miles": float, "route": [[lat, lng], ...]}`` or ``None``. NEVER raises.

    Uses the recommended Google driving route (what Maps would show for
    Storage → stop1 → stop2 → …). Still not a GPS trail of what the BA
    actually drove through construction — BAs can bump miles on the recap.
    """
    key = _google_maps_api_key()
    if not key:
        return None
    try:
        pts = [
            (float(p[0]), float(p[1]))
            for p in (points or [])
            if p is not None and len(p) >= 2
        ]
    except (TypeError, ValueError, IndexError):
        return None
    pts = [(lat, lng) for lat, lng in pts if not (lat == 0.0 and lng == 0.0)]
    if len(pts) < 2:
        return None
    # Directions caps intermediate waypoints; Feel Free sampling days stay small.
    if len(pts) > 27:  # origin + dest + 25 intermediates
        pts = [pts[0], *pts[1:26], pts[-1]]

    origin = f"{pts[0][0]:.6f},{pts[0][1]:.6f}"
    destination = f"{pts[-1][0]:.6f},{pts[-1][1]:.6f}"
    # No departure_time — reimbursement wants the stable recommended route
    # distance, not a traffic-aware alternate that changes for late filings.
    params: dict = {
        "origin": origin,
        "destination": destination,
        "mode": "driving",
        "units": "metric",
        "key": key,
    }
    if len(pts) > 2:
        params["waypoints"] = "|".join(f"{lat:.6f},{lng:.6f}" for lat, lng in pts[1:-1])

    try:
        resp = httpx.get(
            "https://maps.googleapis.com/maps/api/directions/json",
            params=params,
            timeout=timeout,
            headers={"User-Agent": "spark-api/payable-mileage"},
        )
        resp.raise_for_status()
        data = resp.json()
    except (httpx.HTTPError, ValueError) as exc:
        logger.warning("Google Directions failed (%d pts): %s", len(pts), exc)
        return None

    if (data or {}).get("status") != "OK":
        logger.warning(
            "Google Directions status=%s (%d pts)",
            (data or {}).get("status"),
            len(pts),
        )
        return None
    routes = data.get("routes") or []
    if not routes:
        return None
    legs = routes[0].get("legs") or []
    meters = 0.0
    for leg in legs:
        try:
            meters += float((leg.get("distance") or {}).get("value") or 0)
        except (TypeError, ValueError):
            continue
    if meters <= 0:
        return None

    route: list[list[float]] = [[pts[0][0], pts[0][1]]]
    for leg in legs:
        end = leg.get("end_location") or {}
        try:
            route.append([float(end["lat"]), float(end["lng"])])
        except (KeyError, TypeError, ValueError):
            continue

    return {"miles": round(meters / _METERS_PER_MILE, 2), "route": route}


def osrm_route_waypoints(
    points: list,
    *,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
) -> dict | None:
    """Driving distance through an ordered list of waypoints, best-effort.

    Feel Free payable mileage is Storage → stop1 → stop2 → … (or stop1 →
    stop2 → … when the BA did not start at storage). OSRM's /route accepts
    multiple coordinates in one call; we sum the single returned route
    distance. Returns ``{"miles": float, "route": [[lat, lng], ...]}`` or
    ``None``. NEVER raises.
    """
    try:
        pts = [
            (float(p[0]), float(p[1]))
            for p in (points or [])
            if p is not None and len(p) >= 2
        ]
    except (TypeError, ValueError, IndexError):
        return None
    pts = [(lat, lng) for lat, lng in pts if not (lat == 0.0 and lng == 0.0)]
    if len(pts) < 2:
        return None

    coord_str = ";".join(f"{lng:.6f},{lat:.6f}" for (lat, lng) in pts)
    url = f"{OSRM_BASE_URL}/route/v1/driving/{coord_str}"

    try:
        resp = httpx.get(
            url,
            params={"overview": "full", "geometries": "geojson"},
            timeout=timeout,
            headers={"User-Agent": "spark-api/mileage-waypoints"},
        )
        resp.raise_for_status()
        data = resp.json()
    except (httpx.HTTPError, ValueError) as exc:
        logger.warning("OSRM multi-route failed (%d pts): %s", len(pts), exc)
        return None

    if (data or {}).get("code") != "Ok":
        return None
    routes = data.get("routes") or []
    if not routes:
        return None

    best = routes[0]
    try:
        meters = float(best.get("distance") or 0.0)
    except (TypeError, ValueError):
        return None
    if meters <= 0:
        return None

    route: list[list[float]] = []
    for c in ((best.get("geometry") or {}).get("coordinates")) or []:
        if isinstance(c, (list, tuple)) and len(c) >= 2:
            try:
                route.append([float(c[1]), float(c[0])])
            except (TypeError, ValueError):
                continue

    return {"miles": round(meters / _METERS_PER_MILE, 2), "route": route}


def haversine_route_miles(points: list) -> float | None:
    """Straight-line fallback when OSRM is unavailable. ``points`` = (lat,lng)."""
    import math

    try:
        pts = [
            (float(p[0]), float(p[1]))
            for p in (points or [])
            if p is not None and len(p) >= 2
        ]
    except (TypeError, ValueError, IndexError):
        return None
    pts = [(lat, lng) for lat, lng in pts if not (lat == 0.0 and lng == 0.0)]
    if len(pts) < 2:
        return None
    earth = 3958.7613
    total = 0.0
    for i in range(1, len(pts)):
        lat1, lng1 = pts[i - 1]
        lat2, lng2 = pts[i]
        p1, p2 = math.radians(lat1), math.radians(lat2)
        dphi = math.radians(lat2 - lat1)
        dlmb = math.radians(lng2 - lng1)
        h = (
            math.sin(dphi / 2) ** 2
            + math.cos(p1) * math.cos(p2) * math.sin(dlmb / 2) ** 2
        )
        total += earth * 2 * math.asin(min(1.0, math.sqrt(h)))
    return round(total, 2)
