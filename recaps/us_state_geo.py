"""Which US state a GPS point is in, offline.

Used by ``recaps.recap_location`` to read a recap's state from the point the
BA clocked in at. No network: the state shapes ship with the code.

Data
----
``recaps/data/us_states_10m.topo.json`` is ``states-10m.json`` from the
``us-atlas`` npm package (v3.0.1, ISC licence, Mike Bostock), byte for byte
the file the web app's state heat map already draws
(``spark-front-client/src/components/ai/TenantGeoHeatmap.tsx``). It is the
U.S. Census Bureau cartographic boundary file (public domain), simplified
for 1:10,000,000 and quantized. Coordinates are [lng, lat] in WGS84.

That simplification moves a border by up to roughly a kilometre, so a point
close to another state's line is not trusted: :func:`state_at` reports how
far the point is from the nearest other state and the caller decides.

Pure Python (ray casting plus point-to-segment distance); shapely is not a
dependency of this project.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from events.us_states import US_STATE_NAME_TO_CODE

DATA_PATH = Path(__file__).resolve().parent / "data" / "us_states_10m.topo.json"

# Points outside every shape (a store on a coast or a lake shore the
# simplified outline cut off, e.g. Navy Pier) are given to the nearest state
# when it is this close. Kept small: the file has no Canada or Mexico, so a
# point just across the national border would otherwise land in a US state.
OFFSHORE_KM = 1.5

# Look this far around a point for other states' borders. Callers only care
# about borders closer than this; past it the distance is reported as the
# cap.
_SEARCH_KM = 25.0
_KM_PER_DEG_LAT = 111.32

Ring = list[tuple[float, float]]


@dataclass(frozen=True)
class _Shape:
    code: str
    rings: tuple[tuple[tuple[float, float], ...], ...]  # (lng, lat), closed
    bbox: tuple[float, float, float, float]  # min_lng, min_lat, max_lng, max_lat


@dataclass(frozen=True)
class StateHit:
    """``code``: the state the point is in. ``border_km``: how far it is from
    the nearest OTHER state's outline (capped at the search radius).
    ``inside``: False when the point fell just outside every shape and was
    given to the nearest state."""

    code: str
    border_km: float
    inside: bool = True


def _decode_arcs(topo: dict) -> list[list[tuple[float, float]]]:
    sx, sy = topo["transform"]["scale"]
    tx, ty = topo["transform"]["translate"]
    out = []
    for arc in topo["arcs"]:
        x = y = 0
        pts = []
        for dx, dy in arc:
            x += dx
            y += dy
            pts.append((x * sx + tx, y * sy + ty))
        out.append(pts)
    return out


def _ring(arc_ids: list[int], arcs: list[list[tuple[float, float]]]) -> Ring:
    pts: Ring = []
    for i in arc_ids:
        arc = arcs[i] if i >= 0 else list(reversed(arcs[~i]))
        # Consecutive arcs share their joining point.
        pts.extend(arc if not pts else arc[1:])
    return pts


@lru_cache(maxsize=1)
def _shapes() -> tuple[_Shape, ...]:
    topo = json.loads(DATA_PATH.read_text())
    arcs = _decode_arcs(topo)
    shapes = []
    for geom in topo["objects"]["states"]["geometries"]:
        code = US_STATE_NAME_TO_CODE.get((geom.get("properties") or {}).get("name", "").lower())
        if not code:  # territories: not a state this system knows
            continue
        polys = geom["arcs"] if geom["type"] == "MultiPolygon" else [geom["arcs"]]
        rings = tuple(tuple(_ring(r, arcs)) for poly in polys for r in poly)
        lngs = [p[0] for r in rings for p in r]
        lats = [p[1] for r in rings for p in r]
        shapes.append(_Shape(code, rings, (min(lngs), min(lats), max(lngs), max(lats))))
    return tuple(shapes)


def _in_rings(lng: float, lat: float, rings) -> bool:
    """Even-odd rule over every ring of a state, so holes work."""
    inside = False
    for ring in rings:
        j = len(ring) - 1
        for i in range(len(ring)):
            xi, yi = ring[i]
            xj, yj = ring[j]
            if (yi > lat) != (yj > lat):
                x = xi + (lat - yi) * (xj - xi) / (yj - yi)
                if lng < x:
                    inside = not inside
            j = i
    return inside


def _ring_distance_km(lng: float, lat: float, rings, cap_km: float) -> float:
    """Shortest distance from the point to any edge, in km (local flat
    projection, fine at these distances)."""
    kx = _KM_PER_DEG_LAT * math.cos(math.radians(lat))
    ky = _KM_PER_DEG_LAT
    best = cap_km
    for ring in rings:
        prev = None
        for vx, vy in ring:
            p = ((vx - lng) * kx, (vy - lat) * ky)
            if prev is not None:
                ax, ay = prev
                bx, by = p
                dx, dy = bx - ax, by - ay
                seg = dx * dx + dy * dy
                t = 0.0 if seg == 0 else max(0.0, min(1.0, -(ax * dx + ay * dy) / seg))
                cx, cy = ax + t * dx, ay + t * dy
                d = math.hypot(cx, cy)
                if d < best:
                    best = d
            prev = p
    return best


def _near_bbox(shape: _Shape, lng: float, lat: float, km: float) -> bool:
    dlat = km / _KM_PER_DEG_LAT
    dlng = km / max(_KM_PER_DEG_LAT * math.cos(math.radians(lat)), 1e-6)
    min_lng, min_lat, max_lng, max_lat = shape.bbox
    return min_lng - dlng <= lng <= max_lng + dlng and min_lat - dlat <= lat <= max_lat + dlat


def state_at(lat: float, lng: float) -> StateHit | None:
    """The state containing ``(lat, lng)``, or ``None`` when the point is in
    no state (abroad, at sea, a territory) or in two at once."""
    try:
        lat, lng = float(lat), float(lng)
    except (TypeError, ValueError):
        return None
    if not (-90.0 <= lat <= 90.0 and -180.0 <= lng <= 180.0) or (lat == 0.0 and lng == 0.0):
        return None
    shapes = _shapes()
    containing = [s for s in shapes if _near_bbox(s, lng, lat, 0) and _in_rings(lng, lat, s.rings)]
    nearby = [s for s in shapes if _near_bbox(s, lng, lat, _SEARCH_KM)]
    if len(containing) > 1:
        return None
    if len(containing) == 1:
        home = containing[0]
        others = [
            _ring_distance_km(lng, lat, s.rings, _SEARCH_KM) for s in nearby if s.code != home.code
        ]
        return StateHit(home.code, min(others, default=_SEARCH_KM))
    # Outside every shape: the nearest state, if it is close and clearly
    # nearer than the runner-up.
    dists = sorted(
        (_ring_distance_km(lng, lat, s.rings, _SEARCH_KM), s.code) for s in nearby
    )
    if not dists or dists[0][0] > OFFSHORE_KM:
        return None
    runner_up = next((d for d, c in dists[1:] if c != dists[0][1]), _SEARCH_KM)
    return StateHit(dists[0][1], runner_up, inside=False)
