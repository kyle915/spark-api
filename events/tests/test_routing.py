"""Smoke tests for events/routing.py.

Locks in the state-code regex behavior so we don't regress REQ-926.
Tests are intentionally I/O-free — no DB, no Django setup — so they
run fast and don't get skipped when the test DB is slow to set up.
"""

import pytest

from events.routing import extract_state_code, territory_emails_for_state


@pytest.mark.parametrize(
    "address,expected",
    [
        # Google Places canonical form (state SPACE zip)
        ("1885 Halite Dr, Sparks, NV 89436, USA", "NV"),
        ("Chino, CA 91710", "CA"),
        ("123 Main St, Brooklyn, NY 11201", "NY"),
        # Manual-entry comma form — the one that broke REQ-926
        ("1225 W I 35 FRONTAGE RD, EDMOND, OK, 73034", "OK"),
        ("123 Main St, Sparks, NV, 89436", "NV"),
        # Zip+4
        ("123 Main St, Austin, TX 78701-1234", "TX"),
        ("123 Main St, Austin, TX, 78701-1234", "TX"),
        # No zip — defensible default
        ("Brooklyn, NY", "NY"),
        ("Some City, FL", "FL"),
        # Lowercase state still gets normalized (the regex requires
        # uppercase via [A-Z]{2}, but we Google-Places-style input
        # always uses uppercase; explicit lowercase test confirms
        # we don't accidentally match a lowercased "ny" inside a
        # word like "Anytown").
        ("Anytown, NY 12345", "NY"),
        # Real forms from the unrouted LD backlog the old regex missed:
        ("11650 s 73rd st papillion, ne 68046", "NE"),  # lowercase code
        ("405 East Nifong Boulevard, Columbia, Missouri", "MO"),  # full name
        (
            "Walmart Supercenter, Telegraph Road, 13310, Santa Fe Springs, "
            "California, United States",
            "CA",
        ),  # full name + country suffix
        ("85 NH-101A, Amherst, NH 03031, United States", "NH"),  # code + country
        ("1839 MOLALLA AVE\tOREGON CITY\tOR\t97045\t242", "OR"),  # tab + name
        ("625 US-40, Blue Springs, MO 64014, United States", "MO"),
        ("Indiana, PA", "PA"),  # state-named city — trailing code wins
        # State code SPACE a short trailing number — spreadsheet/CSV imports
        # strip the leading zero off New-England ZIPs (03894 -> 3894) or carry
        # a store number after the state. The old \d{5}-only regex missed the
        # state on every one of these and they dropped off the RMM sheet.
        ("670 CENTER ST WOLFEBORO NH 3894", "NH"),  # 03894 stripped
        ("1024 COVE RD NEW BEDFORD MA 2744", "MA"),  # 02744 stripped
        ("122 122-128 CAMBRIDGE ST BOSTON MA 2114", "MA"),  # 02114 stripped
        ("85 S MAIN ST MANCHESTER NH 3102", "NH"),
        ("1360 Eastlake Pkwy Chula Vista CA 3516", "CA"),  # store no. after code
    ],
)
def test_extract_state_code_ok(address, expected):
    assert extract_state_code(address) == expected


@pytest.mark.parametrize(
    "address",
    [
        None,
        "",
        # Just a venue name — the failure mode that motivated the
        # routing fallback in the first place (REQ-925).
        "1608 Broadway St",
        "Walmart Supercenter 389",
        # Genuinely stateless backlog forms: a city with no state token, a
        # highway name where "US" must NOT be mistaken for a state, and a
        # bare venue. The widened trailing-number regex must not invent a
        # state for these (they go to manual edit, not a wrong RMM).
        "2150 West ISB, Daytona",
        "1717 South US 17",
        "Madison Square Garden",
    ],
)
def test_extract_state_code_returns_none(address):
    assert extract_state_code(address) is None


def test_extract_state_code_non_us_returns_none():
    """A non-US address resolves to None: the trailing 2-letter code is
    validated against the real US-state set (so "UK" is rejected) and no
    full US state name matches. Downstream still routes Ignite-only — the
    same safe outcome as any unparseable address."""
    assert extract_state_code("10 Downing St, London, SW1A 2AA, UK") is None
    assert territory_emails_for_state("ighn-liquid-death", None) == []


def test_territory_emails_for_state_known_state_returns_only_owner():
    """A covered state returns just that owner — no fanout."""
    emails = territory_emails_for_state("ighn-liquid-death", "OK")
    assert emails == ["ross@liquiddeath.com"]


def test_territory_emails_for_state_unknown_state_returns_empty():
    """Unknown state returns [] — caller falls back to Ignite-only.

    Pre-PR-564 behavior was to fan out to every reviewer here, which
    is what caused REQ-925 to go to Lauren (first dict key). Empty
    return signals "no territory match" so the mutation sends an
    Ignite-only email instead.
    """
    assert territory_emails_for_state("ighn-liquid-death", "ZZ") == []
    assert territory_emails_for_state("ighn-liquid-death", None) == []
    assert territory_emails_for_state("ighn-liquid-death", "") == []


def test_territory_emails_for_state_other_tenant_returns_empty():
    """Non-routed tenants always return [] regardless of state."""
    assert territory_emails_for_state("some-other-tenant", "NY") == []
