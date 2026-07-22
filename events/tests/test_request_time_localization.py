"""Naive request wall-clock times get localized to the request's timezone.

Guards the fix for the "9 AM Central demo showed as 4 AM" bug — a form path
that couldn't resolve the venue offset posted naive times that Django stored
as UTC.
"""
import datetime

from events.mutations import _localize_naive_to_offset


def test_naive_localized_to_central_cdt():
    # 9 AM entered for a Central (CDT, -300 min) venue → 14:00 UTC, not 09:00.
    naive = datetime.datetime(2026, 7, 31, 9, 0, 0)
    out = _localize_naive_to_offset(naive, -300)
    assert out.utcoffset() == datetime.timedelta(minutes=-300)
    assert out.astimezone(datetime.timezone.utc).hour == 14


def test_aware_value_untouched():
    # A correct client already baked the offset — don't double-shift it.
    aware = datetime.datetime(
        2026, 7, 31, 9, 0, 0,
        tzinfo=datetime.timezone(datetime.timedelta(minutes=-300)),
    )
    out = _localize_naive_to_offset(aware, -300)
    assert out is aware


def test_none_and_missing_offset_passthrough():
    assert _localize_naive_to_offset(None, -300) is None
    naive = datetime.datetime(2026, 7, 31, 9, 0, 0)
    assert _localize_naive_to_offset(naive, None) is naive


# --- String path (the real case: request inputs are `str`-typed) -------------

def test_naive_iso_string_localized_to_cdt():
    # The bug in the wild: FE couldn't resolve the offset, posted "...T09:00:00".
    out = _localize_naive_to_offset("2026-07-31T09:00:00", -300)
    parsed = datetime.datetime.fromisoformat(out)
    assert parsed.utcoffset() == datetime.timedelta(minutes=-300)
    assert parsed.astimezone(datetime.timezone.utc).hour == 14
    # Matches exactly what the FE emits when it works (buildDateTimeWithOffset).
    assert out == "2026-07-31T09:00:00-05:00"


def test_offset_aware_string_untouched():
    # A correct client already baked the offset — return the string verbatim.
    s = "2026-07-31T09:00:00-05:00"
    assert _localize_naive_to_offset(s, -300) == s


def test_z_suffixed_utc_string_untouched():
    # "Z" is an explicit offset (UTC) — must not be re-stamped.
    s = "2026-07-31T14:00:00Z"
    assert _localize_naive_to_offset(s, -300) == s


def test_bare_date_string_passthrough():
    # A "YYYY-MM-DD" with no time parses to midnight; localizing it to the
    # venue offset keeps it on the intended calendar day.
    out = _localize_naive_to_offset("2026-07-31", -300)
    assert out == "2026-07-31T00:00:00-05:00"


def test_unparseable_string_passthrough():
    assert _localize_naive_to_offset("not a date", -300) == "not a date"
    assert _localize_naive_to_offset("", -300) == ""


def test_string_missing_offset_passthrough():
    # No tz resolved → leave the naive string for Django to handle as before.
    s = "2026-07-31T09:00:00"
    assert _localize_naive_to_offset(s, None) == s
