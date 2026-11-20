"""Hours-check thresholds.

Defaults live here; ``settings.HOURS_CHECK_THRESHOLDS`` (env
``HOURS_CHECK_THRESHOLDS`` as JSON, e.g. ``{"far_miles": 0.5}``) overrides any
of them. Unknown keys are ignored and a bad value falls back to the default,
so a typo in the env can never take the payroll page down.
"""

from __future__ import annotations

from django.conf import settings

DEFAULTS: dict[str, float] = {
    # Worked longer than booked + approved extension by more than this.
    "over_tolerance_minutes": 15,
    # Clock-in / clock-out GPS fix farther than this from the venue.
    "far_miles": 1.0,
    # Clock-in this long before the booked start, or clock-out this long after
    # the booked end (+ approved extension).
    "early_minutes": 60,
    "late_minutes": 60,
    # One shift longer than this is worth a look.
    "long_shift_hours": 10,
    # Two shifts of the same BA overlapping by more than this.
    "overlap_minutes": 10,
    # Travel between consecutive shifts faster than this (straight line)…
    "max_travel_mph": 60,
    # …only judged when the venues are at least this far apart.
    "travel_min_miles": 5,
    # Recap filed more than this before the clock-out.
    "recap_early_minutes": 30,
    # A GPS ping within this many minutes of the recap submit time is taken
    # as "where they were when they filed".
    "recap_ping_window_minutes": 15,
    # Payable mileage claim above the GPS-tracked miles by more than this
    # share AND this many miles.
    "mileage_over_ratio": 0.25,
    "mileage_over_miles": 3,
}


def thresholds() -> dict[str, float]:
    out = dict(DEFAULTS)
    overrides = getattr(settings, "HOURS_CHECK_THRESHOLDS", None) or {}
    if isinstance(overrides, dict):
        for key, value in overrides.items():
            if key not in DEFAULTS:
                continue
            try:
                out[key] = float(value)
            except (TypeError, ValueError):
                continue
    return out
