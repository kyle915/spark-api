"""The Master-Tracker mirror renders LOCAL wall-clock, DST-aware.

The `TimeZone` model carries one static `offset` (Pacific is stored as -480).
Adding that to a stored UTC instant under-shifts by an hour for the ~8 months
of DST — and because `date` is stored at LOCAL MIDNIGHT, that extra hour also
rolls the calendar date back a day. Liquid Death's REQ-1615 (Aug 9 2026,
12p-4p, San Bernardino) mirrored into the client sheet as Sat 8/8 11a-3p:
one day AND one hour early, from that single cause.

The request-edit, GraphQL and email paths were fixed for this (utils/tz.py);
the sheet mirror is a separate write path that never received it. These tests
pin the mirror to the same DST-aware conversion the approval EMAIL uses, since
the email is the reference implementation the client compares against.
"""
from __future__ import annotations

import datetime as dt
from types import SimpleNamespace

from utils.sheets_mirror import (
    _fmt_date,
    _fmt_time_ld,
    _ld_retail_row,
    _tz_for_request,
    _weekday_ld,
)

UTC = dt.timezone.utc

# The Pacific row as actually stored in prod: a single STANDARD-time offset,
# with no way to represent PDT (see events/tests/test_dedupe_timezones.py).
PACIFIC = SimpleNamespace(name="Pacific", code="PST", offset=-480)


def _request(date, start, end, timezone=PACIFIC, **extra):
    return SimpleNamespace(
        timezone=timezone, date=date, start_time=start, end_time=end, **extra
    )


def _rendered(req):
    """(weekday, date, start, end) as the LD tracker columns B/C/E/F."""
    tz = _tz_for_request(req)
    return (
        _weekday_ld(req.date, tz),
        _fmt_date(req.date, tz),
        _fmt_time_ld(req.start_time, tz),
        _fmt_time_ld(req.end_time, tz),
    )


def test_req_1615_midday_pdt_matches_the_approval_email():
    # Stored as aware UTC: the FE bakes the venue offset in, so local midnight
    # is 07:00Z, noon PDT is 19:00Z and 4 PM PDT is 23:00Z.
    req = _request(
        dt.datetime(2026, 8, 9, 7, 0, tzinfo=UTC),
        dt.datetime(2026, 8, 9, 19, 0, tzinfo=UTC),
        dt.datetime(2026, 8, 9, 23, 0, tzinfo=UTC),
    )
    # Before the fix this was ("Saturday", "8/8/2026", "11a", "3p").
    assert _rendered(req) == ("Sunday", "8/9/2026", "12p", "4p")


def test_evening_pacific_activation_does_not_roll_the_date_back():
    """An evening PT activation crosses midnight in UTC — a naive-UTC or
    under-shifted read lands on the wrong calendar day, while a midday test
    would still pass. 6p PDT Aug 9 = 01:00Z Aug 10."""
    req = _request(
        dt.datetime(2026, 8, 9, 7, 0, tzinfo=UTC),
        dt.datetime(2026, 8, 10, 1, 0, tzinfo=UTC),
        dt.datetime(2026, 8, 10, 5, 0, tzinfo=UTC),
    )
    assert _rendered(req) == ("Sunday", "8/9/2026", "6p", "10p")


def test_winter_activation_is_unchanged():
    """In PST the static offset was already right — the fix must not move it."""
    req = _request(
        dt.datetime(2026, 1, 10, 8, 0, tzinfo=UTC),
        dt.datetime(2026, 1, 10, 20, 0, tzinfo=UTC),
        dt.datetime(2026, 1, 11, 0, 0, tzinfo=UTC),
    )
    assert _rendered(req) == ("Saturday", "1/10/2026", "12p", "4p")


def test_dst_transition_day_converts_each_value_independently():
    """On fall-back day the date (local midnight, still PDT) and start_time
    (already PST) sit on opposite sides of the transition. One shared offset
    taken from start_time would render the date as 10/31."""
    req = _request(
        dt.datetime(2026, 11, 1, 7, 0, tzinfo=UTC),   # midnight PDT
        dt.datetime(2026, 11, 1, 20, 0, tzinfo=UTC),  # noon PST
        dt.datetime(2026, 11, 2, 0, 0, tzinfo=UTC),   # 4 PM PST
    )
    assert _rendered(req) == ("Sunday", "11/1/2026", "12p", "4p")


def test_no_timezone_row_falls_back_to_state_not_raw_utc():
    """Matches the email's no-TimeZone fallback: render the activation's local
    time via its state rather than dumping UTC into the client's sheet."""
    req = _request(
        dt.datetime(2026, 8, 9, 7, 0, tzinfo=UTC),
        dt.datetime(2026, 8, 9, 19, 0, tzinfo=UTC),
        dt.datetime(2026, 8, 9, 23, 0, tzinfo=UTC),
        timezone=None,
        state=SimpleNamespace(code="CA"),
    )
    assert _rendered(req) == ("Sunday", "8/9/2026", "12p", "4p")


def test_unresolvable_timezone_still_renders_something():
    """A tz row that maps to no IANA zone degrades to its static offset
    rather than raising — a Sheets miss must never break a save."""
    exotic = SimpleNamespace(name="Nowhere/Unknown", code="ZZZ", offset=-480)
    req = _request(
        dt.datetime(2026, 8, 9, 7, 0, tzinfo=UTC),
        dt.datetime(2026, 8, 9, 19, 0, tzinfo=UTC),
        dt.datetime(2026, 8, 9, 23, 0, tzinfo=UTC),
        timezone=exotic,
    )
    assert _rendered(req) == ("Saturday", "8/8/2026", "11a", "3p")


def test_ld_row_columns_carry_the_corrected_values():
    """End-to-end through the real row builder, so a future refactor that
    reintroduces a shared static offset fails here too."""
    req = _request(
        dt.datetime(2026, 8, 9, 7, 0, tzinfo=UTC),
        dt.datetime(2026, 8, 9, 19, 0, tzinfo=UTC),
        dt.datetime(2026, 8, 9, 23, 0, tzinfo=UTC),
        tenant=SimpleNamespace(name="Liquid Death"),
        state=SimpleNamespace(code="CA"),
        retailer=SimpleNamespace(name="Stater Bros. Markets"),
        address="977 Kendall Dr, San Bernardino, CA 92407",
        notes="",
    )
    row = _ld_retail_row(req)
    assert row[1] == "Sunday"      # B weekday
    assert row[2] == "8/9/2026"    # C date
    assert row[4] == "12p"         # E start
    assert row[5] == "4p"          # F end
