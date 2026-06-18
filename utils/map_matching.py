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
