"""Onsite BA headcount stored on Request.ba_count.

Express forms used to write only a ``BA count: N`` line into notes.
That line stays as a fallback for older rows. Event Activation on
Wook Water, FAMUS Energy, and Alcohol Armor persists the integer.
"""

from __future__ import annotations

import re

BA_COUNT_MIN = 1
BA_COUNT_MAX = 20
_NOTES_RE = re.compile(r"BA count:\s*(\d+)", re.I)


class BaCountError(ValueError):
    pass


def normalize_ba_count(value) -> int | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        raise BaCountError("BA count must be a whole number.")
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise BaCountError("BA count must be a whole number.") from exc
    if number < BA_COUNT_MIN or number > BA_COUNT_MAX:
        raise BaCountError(
            f"BA count must be between {BA_COUNT_MIN} and {BA_COUNT_MAX}."
        )
    return number


def ba_count_from_request(req) -> int | None:
    stored = getattr(req, "ba_count", None)
    if isinstance(stored, bool):
        stored = None
    if isinstance(stored, int) and not isinstance(stored, bool) and stored > 0:
        return stored
    notes = getattr(req, "notes", None) or ""
    match = _NOTES_RE.search(notes)
    if match:
        return int(match.group(1))
    return None
