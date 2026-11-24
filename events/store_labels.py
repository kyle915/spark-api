"""Store names for maps and menus, without the dates people type into them.

Requests are usually named "<store> · <date>" ("BP · 2027-01-11",
"FOOD GARDEN FRAN-05/17/2026"). Most requests have no linked Retailer, so the
Account Map used the request name as the store's name, and its retailer menu
listed one entry per visit instead of one per store or chain.

:func:`clean_store_label` takes the dates (and shift times) back out. The
front end has the same rules in ``src/components/pages/map/accountStores.ts``
(``cleanStoreLabel``); keep the two in step.
"""

from __future__ import annotations

import re

_MONTH = (
    r"(?:jan|january|feb|february|mar|march|apr|april|may|jun|june|jul|july|aug|august|sep|sept|september|oct|october|nov|november|dec|december)\.?"
)
_DATE_PATTERNS = [
    # 2027-01-11, 2027/1/11
    r"\b\d{4}[-/.]\d{1,2}[-/.]\d{1,2}\b",
    # 05/16/2026, 5/16/26, 5/16 (a slash is a date; "7-11" is a store)
    r"\b\d{1,2}/\d{1,2}(?:/\d{2,4})?\b",
    # 05-16-2026, 5.16.26 (dash / dot only with all three parts)
    r"\b\d{1,2}[-.]\d{1,2}[-.]\d{2,4}\b",
    # May 17, Jan 9th 2027, Sept. 3, 2026
    rf"\b{_MONTH}\s+\d{{1,2}}(?:st|nd|rd|th)?(?:,?\s*\d{{4}})?\b",
    # 10am-2pm, 10:00 AM - 2:00 PM, 3pm
    r"\b\d{1,2}(?::\d{2})?\s*(?:am|pm)?\s*[-–—]\s*\d{1,2}(?::\d{2})?\s*(?:am|pm)\b",
    r"\b\d{1,2}(?::\d{2})?\s*(?:am|pm)\b",
]
# "Sat 11/12", "Saturday, May 17": the weekday goes with its date.
_WEEKDAY = r"(?:\b(?:mon|monday|tue|tues|tuesday|wed|weds|wednesday|thu|thur|thurs|thursday|fri|friday|sat|saturday|sun|sunday)\b\.?,?\s+)?"
_DATES = re.compile(
    _WEEKDAY + "(?:" + "|".join(f"(?:{p})" for p in _DATE_PATTERNS) + ")",
    re.IGNORECASE,
)
_DATE_ONLY = re.compile(r"^(?:" + _WEEKDAY + "(?:" + "|".join(_DATE_PATTERNS) + r")[\s,]*)+$", re.IGNORECASE)
# What is left hanging where a date was cut: " · ", " - ", "|", "()", ",".
_EDGE = re.compile(r"^[\s·•|,;:\-–—/]+|[\s·•|,;:\-–—/(]+$")
_EMPTY_PARENS = re.compile(r"\(\s*\)|\[\s*\]")
_SEP_RUN = re.compile(r"\s*([·•|])\s*(?:[·•|\-–—]\s*)+")
_SPACES = re.compile(r"\s+")


def clean_store_label(text) -> str:
    """``text`` without dates or shift times, and without the separators they
    leave behind. Returns the original (trimmed) text when nothing would be
    left, so a label is never blank because it was only a date."""
    raw = " ".join(str(text or "").split())
    if not raw:
        return ""
    out = _DATES.sub(" ", raw)
    out = _EMPTY_PARENS.sub(" ", out)
    out = _SEP_RUN.sub(r" \1 ", out)
    out = _SPACES.sub(" ", out)
    previous = None
    while previous != out:
        previous = out
        out = _EDGE.sub("", out).strip()
    return out or raw


def is_date_only(text) -> bool:
    """True for a label that is nothing but a date ("05/17/2026")."""
    raw = " ".join(str(text or "").split())
    return bool(raw) and bool(_DATE_ONLY.match(raw))
