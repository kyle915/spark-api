"""One number reader for every recap answer (recaps.numbers).

"1,234" used to read as 1234 on the recap list and dashboard but as 1 on the
campaign report; "12.5" read as 125 and "sampled 50, sold 20" as 5020. These
pin the single rule set every path now shares.
"""

import pytest

from recaps import pdf, recap_quality, report_service
from recaps.numbers import is_clean_count, parse_count
from recaps.types import (
    _consumers_sampled_from_fields,
    _parse_recap_int,
    _sold_units_from_fields,
)


@pytest.mark.parametrize(
    "value, expected",
    [
        ("40", 40),
        (" 40 ", 40),
        ("1,234", 1234),
        ("1,234,567", 1234567),
        ("1 234", 1234),
        ("1 234", 1234),
        ("~30", 30),
        ("≈ 30", 30),
        ("about 12", 12),
        ("approx. 1,500", 1500),
        ("estimated 40", 40),
        ("50+", 50),
        ("+50", 50),
        ("20-30", 20),
        ("20 – 30", 20),
        ("20 to 30", 20),
        ("12.5", 12),
        ("1,234.9", 1234),
        ("70 cans", 70),
        ("12/25", 12),
        ("0", 0),
        (7, 7),
        ("$ 87", 87),
        # Negatives stay negative so the review checks can flag them.
        ("-5", -5),
    ],
)
def test_counts(value, expected):
    assert parse_count(value) == expected


@pytest.mark.parametrize(
    "value",
    [
        None, "", "   ", "-", "--5", "N/A", "yes",
        "We sampled 50 people",  # prose: was 50 (first digits) or 5020-style mash
        "12,5",  # ambiguous: twelve and a half, or 125?
        "1,2345",
        "12.5.6",
        "3 120ml cans",
        "established 2020",
        True,
    ],
)
def test_not_counts(value):
    assert parse_count(value) is None


@pytest.mark.parametrize(
    "value, clean",
    [
        ("1,234", True), ("~30", True), ("50+", True), ("20-30", True),
        (" 40 ", True), ("70 cans", False), ("19 to mid-60s", False),
        ("We sampled 50", False), ("", False),
        # Words, decimals and negatives are a guess or a typo, not a count.
        ("about 30", False), ("roughly 40 to 60", False), ("12.5", False),
        ("-5", False),
    ],
)
def test_clean_count(value, clean):
    assert is_clean_count(value) is clean


@pytest.mark.parametrize("value", ["1,234", "~1,234", "1,234+", "1234", " 1 234 "])
def test_every_reader_agrees(value):
    """The readers that disagreed now return the same number."""
    readers = [
        _parse_recap_int,
        report_service._leading_int,
        pdf._leading_int,
        recap_quality._leading_int,
    ]
    assert {reader(value) for reader in readers} == {1234}


def test_thousands_reach_the_kpi_matchers():
    pairs = [
        ("Total number of consumers sampled", "1,234"),
        ("Single Cans Sold", "1,050"),
    ]
    assert _consumers_sampled_from_fields(pairs) == 1234
    assert _sold_units_from_fields(pairs) == 1050


def test_prose_no_longer_mashes_into_a_count():
    # The old strip reader turned this into 5020.
    assert _sold_units_from_fields([("Cans sold", "sold 50, then 20 more")]) is None
