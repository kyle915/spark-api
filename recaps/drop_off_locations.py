"""Product Seeding "Drop-off Locations" — a JSON list stored in a longtext
CustomFieldValue. Server twin of the front's ``src/recap/dropOffLocations.ts``.

Shape (array of locations)::

    {
      "placeName": str,
      "address": str | None,
      "lat": float | None,
      "lng": float | None,
      "source": "gps" | "places" | "manual",
      "skus": [{"productId": str, "productName": str | None, "cases": int}]
    }
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field

_FIELD_NAMES = {"drop-off locations", "drop off locations"}


@dataclass(frozen=True)
class DropOffSku:
    product_id: str
    product_name: str
    cases: int


@dataclass(frozen=True)
class DropOffLocation:
    place_name: str
    address: str
    lat: float | None
    lng: float | None
    skus: list[DropOffSku] = field(default_factory=list)

    @property
    def cases(self) -> int:
        return sum(s.cases for s in self.skus)

    @property
    def pin(self) -> str:
        if self.address:
            return self.address
        if self.lat is not None and self.lng is not None:
            return f"{self.lat:.5f}, {self.lng:.5f}"
        return ""


def is_drop_off_locations_field(name: str | None) -> bool:
    return (name or "").strip().lower() in _FIELD_NAMES


def _number(value) -> float | None:
    if value is None or value == "" or isinstance(value, bool):
        return None
    try:
        n = float(value)
    except (TypeError, ValueError):
        return None
    return n if math.isfinite(n) else None


def _sku(raw) -> DropOffSku | None:
    if not isinstance(raw, dict):
        return None
    product_id = str(raw.get("productId") or raw.get("product_id") or "").strip()
    if not product_id:
        return None
    cases = _number(raw.get("cases", raw.get("quantity", raw.get("qty"))))
    name = raw.get("productName") or raw.get("product_name") or ""
    return DropOffSku(
        product_id=product_id,
        product_name=name.strip() if isinstance(name, str) else "",
        cases=round(cases) if cases and cases > 0 else 0,
    )


def _location(raw) -> DropOffLocation | None:
    if not isinstance(raw, dict):
        return None
    skus_raw = raw.get("skus")
    if not isinstance(skus_raw, list):
        skus_raw = raw.get("products") if isinstance(raw.get("products"), list) else []
    return DropOffLocation(
        place_name=str(raw.get("placeName") or raw.get("place_name") or raw.get("name") or "").strip(),
        address=str(raw.get("address") or raw.get("formattedAddress") or "").strip(),
        lat=_number(raw.get("lat", raw.get("latitude"))),
        lng=_number(raw.get("lng", raw.get("longitude"))),
        skus=[s for s in (_sku(r) for r in skus_raw) if s is not None],
    )


def looks_like_drop_off_locations(parsed) -> bool:
    """A parsed JSON list of location dicts (placeName / skus keys)."""
    return (
        isinstance(parsed, list)
        and bool(parsed)
        and all(isinstance(item, dict) for item in parsed)
        and any("placeName" in item or "skus" in item for item in parsed)
    )


def parse_drop_off_locations(value) -> list[DropOffLocation]:
    """Stored value → locations. Legacy plain text becomes one named stop."""
    if value is None:
        return []
    if isinstance(value, (list, dict)):
        parsed = value
    else:
        text = str(value).strip()
        if not text:
            return []
        try:
            parsed = json.loads(text)
        except (TypeError, ValueError):
            return [DropOffLocation(place_name=text, address=text, lat=None, lng=None)]
    if isinstance(parsed, dict):
        parsed = [parsed]
    if not isinstance(parsed, list):
        return []
    return [loc for loc in (_location(item) for item in parsed) if loc is not None]


def sku_label(sku: DropOffSku) -> str:
    return sku.product_name or f"SKU {sku.product_id}"


def location_title(loc: DropOffLocation, index: int) -> str:
    return loc.place_name or loc.address or f"Location {index + 1}"


def drop_off_summary_lines(locations: list[DropOffLocation]) -> list[str]:
    """One plain-text line per stop — same parts as the front's
    ``formatDropOffLocationsSummary``."""
    lines = []
    for i, loc in enumerate(locations):
        name = location_title(loc, i)
        cases = loc.cases
        skus = [f"{sku_label(s)} × {s.cases}" for s in loc.skus if s.cases > 0]
        parts = [
            name,
            loc.pin if loc.pin and loc.pin != name else "",
            f"{cases} case{'' if cases == 1 else 's'}" if cases > 0 else "",
            ", ".join(skus),
        ]
        lines.append(" · ".join(p for p in parts if p))
    return lines
