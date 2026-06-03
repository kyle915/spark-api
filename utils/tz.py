"""DST-aware timezone helpers shared by GraphQL serializers + email mailers.

Why this exists
---------------
The `TimeZone` model carries a fixed integer `offset` field (sometimes in
hours like `-7`, sometimes in minutes like `-420`). Anywhere the codebase
adds that offset to a UTC datetime to produce a "local time" string for
display, it gets DST wrong: a Pacific row stored with offset `-480` is
correct for PST but under-shifts during PDT (March–November), producing
times that are exactly 1 hour earlier than what the user actually
selected. Kyle saw this concretely: editing a Liquid Death request to
12pm Pacific saved as 11am Pacific (May 29 is firmly in PDT).

This module wraps `zoneinfo.ZoneInfo` so the offset comes from a real
tzdata lookup against the **specific datetime being serialized** — not
from the static row. The DB row's `offset` field becomes a fallback
only for tz rows that don't map cleanly to an IANA zone (e.g.
exotic test fixtures).

Resolution order
----------------
1. `TimeZone.code` mapped through a curated dict (EST, PDT, PST, etc.)
2. `TimeZone.name` mapped through the same dict (PACIFIC, EASTERN, etc.)
3. `TimeZone.name` tried directly as an IANA zone (e.g. "America/Denver")
4. `Americas/X` → `America/X` typo-fix (we have at least one test fixture
   with that exact typo, and the bad value lives in real tenants too)
5. Fixed-offset fallback using `TimeZone.offset`, treating values with
   `abs(offset) > 24` as minutes and others as hours (matches the
   detection logic already in `events/queries.py::_filter_events_for_local_today`)
"""

from __future__ import annotations

import datetime as _dt
from datetime import timedelta, timezone as dt_timezone
from typing import Any, Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

# Curated mapping from app-specific TimeZone.code / TimeZone.name values
# to IANA zones. Mirrors the dict in tenants/calendar/service.py so the
# calendar-invite path and the GraphQL/email paths agree on what
# "Pacific" means. Don't drift these apart — if a new region is added,
# update both.
_APP_TZ_TO_IANA: dict[str, str] = {
    "EASTERN": "America/New_York",
    "CENTRAL": "America/Chicago",
    "MOUNTAIN": "America/Denver",
    "PACIFIC": "America/Los_Angeles",
    "ALASKA": "America/Anchorage",
    "HAWAII-ALEUTIAN": "Pacific/Honolulu",
    "HAWAII–ALEUTIAN": "Pacific/Honolulu",  # en-dash variant
    "EST": "America/New_York",
    "EDT": "America/New_York",
    "CST": "America/Chicago",
    "CDT": "America/Chicago",
    "MST": "America/Denver",
    "MDT": "America/Denver",
    "PST": "America/Los_Angeles",
    "PDT": "America/Los_Angeles",
    "AKST": "America/Anchorage",
    "AKDT": "America/Anchorage",
    "HST": "Pacific/Honolulu",
    "HDT": "Pacific/Honolulu",
}


# US state/territory → IANA zone, dominant zone per state. A best-effort
# FALLBACK for when a request carries no TimeZone row but we do know the
# state (from request.state, retailer.location.state, or parsed from the
# address) — used so emails render the activation's LOCAL time instead of
# raw UTC. Multi-zone states map to their dominant zone; AZ uses Phoenix
# (no DST). Not authoritative scheduling data — just a display fallback.
_US_STATE_TO_IANA: dict[str, str] = {
    # Eastern
    "CT": "America/New_York", "DE": "America/New_York", "FL": "America/New_York",
    "GA": "America/New_York", "IN": "America/New_York", "ME": "America/New_York",
    "MD": "America/New_York", "MA": "America/New_York", "MI": "America/New_York",
    "NH": "America/New_York", "NJ": "America/New_York", "NY": "America/New_York",
    "NC": "America/New_York", "OH": "America/New_York", "PA": "America/New_York",
    "RI": "America/New_York", "SC": "America/New_York", "VT": "America/New_York",
    "VA": "America/New_York", "WV": "America/New_York", "DC": "America/New_York",
    # Central
    "AL": "America/Chicago", "AR": "America/Chicago", "IL": "America/Chicago",
    "IA": "America/Chicago", "KS": "America/Chicago", "KY": "America/Chicago",
    "LA": "America/Chicago", "MN": "America/Chicago", "MS": "America/Chicago",
    "MO": "America/Chicago", "NE": "America/Chicago", "ND": "America/Chicago",
    "OK": "America/Chicago", "SD": "America/Chicago", "TN": "America/Chicago",
    "TX": "America/Chicago", "WI": "America/Chicago",
    # Mountain (AZ = Phoenix, no DST)
    "AZ": "America/Phoenix", "CO": "America/Denver", "ID": "America/Denver",
    "MT": "America/Denver", "NM": "America/Denver", "UT": "America/Denver",
    "WY": "America/Denver",
    # Pacific
    "CA": "America/Los_Angeles", "NV": "America/Los_Angeles",
    "OR": "America/Los_Angeles", "WA": "America/Los_Angeles",
    # Non-contiguous
    "AK": "America/Anchorage", "HI": "Pacific/Honolulu",
}


def _try_zone(name: str) -> Optional[ZoneInfo]:
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError, KeyError):
        return None


def offset_minutes_for_state(
    state_code: str | None, at: _dt.datetime | None = None
) -> Optional[int]:
    """DST-aware UTC offset (minutes) for a US state's dominant zone at `at`.

    A display-only FALLBACK for rows with no TimeZone relation. Returns None
    when the state is unknown so callers can fall through to their own
    default (e.g. UTC) rather than guessing.
    """
    code = (state_code or "").strip().upper()
    iana = _US_STATE_TO_IANA.get(code)
    if not iana:
        return None
    zi = _try_zone(iana)
    if zi is None:
        return None
    when = at or _dt.datetime.now(_dt.timezone.utc)
    if when.tzinfo is None:
        when = when.replace(tzinfo=_dt.timezone.utc)
    return int(zi.utcoffset(when).total_seconds() // 60)


def resolve_zoneinfo(tz_row: Any | None) -> Optional[ZoneInfo]:
    """Return a `ZoneInfo` for the given TimeZone row, or None if unresolvable.

    Does NOT raise — call sites are expected to fall back to fixed-offset
    arithmetic when None is returned (see `naive_local_iso` below).
    """
    if tz_row is None:
        return None

    code = (getattr(tz_row, "code", "") or "").strip().upper()
    if code and (zi := _try_zone(_APP_TZ_TO_IANA.get(code, ""))):
        return zi

    raw_name = (getattr(tz_row, "name", "") or "").strip()
    name_upper = raw_name.upper()
    if name_upper and (zi := _try_zone(_APP_TZ_TO_IANA.get(name_upper, ""))):
        return zi

    if raw_name:
        # Try the name directly as an IANA zone (works for fixtures like
        # "America/Denver"). Then patch the common typo we have in older
        # rows ("Americas/Mexico_City" → "America/Mexico_City").
        if zi := _try_zone(raw_name):
            return zi
        if raw_name.startswith("Americas/"):
            patched = "America/" + raw_name[len("Americas/"):]
            if zi := _try_zone(patched):
                return zi

    return None


def fixed_offset_minutes(tz_row: Any | None) -> int:
    """Fallback for rows that don't map to an IANA zone.

    Mirrors the hours-vs-minutes auto-detect in
    `events/queries.py::_filter_events_for_local_today`: values with
    |offset| > 24 are treated as minutes, otherwise as hours and
    multiplied by 60.
    """
    raw = getattr(tz_row, "offset", None) if tz_row is not None else None
    if raw is None:
        return 0
    try:
        raw_int = int(raw)
    except (TypeError, ValueError):
        return 0
    return raw_int if abs(raw_int) > 24 else raw_int * 60


def offset_minutes_for(tz_row: Any | None, at: _dt.datetime | None = None) -> int:
    """Effective offset in minutes for `tz_row` at moment `at`.

    DST-aware when `tz_row` resolves to an IANA zone, otherwise falls
    back to the static `TimeZone.offset` field. `at` defaults to
    `datetime.now(UTC)` — call sites that care about a specific
    datetime (e.g. when serializing a future shift's start_time) should
    pass it explicitly so DST transitions are computed for the right
    instant.
    """
    zi = resolve_zoneinfo(tz_row)
    if zi is not None:
        when = at or _dt.datetime.now(_dt.timezone.utc)
        if when.tzinfo is None:
            when = when.replace(tzinfo=_dt.timezone.utc)
        offset = zi.utcoffset(when.astimezone(zi).replace(tzinfo=None))
        if offset is not None:
            return int(offset.total_seconds() // 60)
    return fixed_offset_minutes(tz_row)


def naive_local_iso(
    value: _dt.datetime | _dt.date | None,
    tz_row: Any | None,
) -> Optional[str]:
    """Render `value` as a naive ISO string in `tz_row`'s local time.

    "Naive" means no Z suffix and no offset — what the frontend already
    expects from `_serialize_dt`. The string represents the wall-clock
    time in the request's/event's timezone. DST is correct because we
    use `ZoneInfo` for the conversion (not a static minutes offset).

    Returns None for falsy input.
    """
    if not value:
        return None

    # `date` objects are passed through unchanged — they have no
    # time-of-day so no tz conversion is meaningful.
    if isinstance(value, _dt.date) and not isinstance(value, _dt.datetime):
        return value.isoformat()

    # Normalize to a UTC-aware datetime so subsequent math is unambiguous.
    if value.tzinfo is None:
        value = value.replace(tzinfo=_dt.timezone.utc)
    else:
        value = value.astimezone(_dt.timezone.utc)

    zi = resolve_zoneinfo(tz_row)
    if zi is not None:
        local = value.astimezone(zi)
        return local.replace(tzinfo=None).isoformat()

    # Fallback: same fixed-offset behavior the old `_serialize_dt`
    # used, so rows without an IANA mapping (rare/test-only) still
    # render the same as before this change.
    minutes = fixed_offset_minutes(tz_row)
    shifted = value + timedelta(minutes=minutes)
    return shifted.replace(tzinfo=None).isoformat()


def apply_dst_aware_offset(
    value: _dt.datetime | None,
    tz_row: Any | None,
) -> Optional[_dt.datetime]:
    """Variant of `naive_local_iso` returning a naive datetime instead.

    Useful for email formatters that strftime the result with a
    custom format string (see events/envelopes.py::_format_dt_no_tz).
    """
    if not value:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=_dt.timezone.utc)
    else:
        value = value.astimezone(_dt.timezone.utc)

    zi = resolve_zoneinfo(tz_row)
    if zi is not None:
        return value.astimezone(zi).replace(tzinfo=None)

    minutes = fixed_offset_minutes(tz_row)
    return (value + timedelta(minutes=minutes)).replace(tzinfo=None)


__all__ = [
    "apply_dst_aware_offset",
    "fixed_offset_minutes",
    "naive_local_iso",
    "offset_minutes_for",
    "offset_minutes_for_state",
    "resolve_zoneinfo",
]
