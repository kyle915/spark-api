"""Smoke tests for events/routing.py.

Locks in the state-code regex behavior so we don't regress REQ-926.
Tests are intentionally I/O-free — no DB, no Django setup — so they
run fast and don't get skipped when the test DB is slow to set up.
"""

from types import SimpleNamespace

import pytest

from events.routing import (
    extract_state_code,
    public_form_rmm_emails,
    territory_emails_for_state,
)


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
        # State glued to its zip (sheet imports).
        ("3954A PEACHTREE ROAD NE, , BROOKHAVEN, GA30319", "GA"),
        ("2955 COBB PKWY NW STE 308, ATLANTA, GA30339-1234", "GA"),
        ("3954A Peachtree Rd NE Brookhaven GA30319", "GA"),
        # A street name before the real state never wins.
        ("13657 Washington St, Columbus, OH 43215", "OH"),
        ("100 Georgia Ave, Silver Spring, MD 20910", "MD"),
        ("3954A Peachtree Rd NE, Atlanta, GA 30319", "GA"),
        ("4 Pennsylvania Plaza, New York, New York", "NY"),
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
        # Street names / directions are not states.
        "13657 Washington St",
        "13657 Washington Street",
        "3954A Peachtree Rd NE",
        "3954A Peachtree Rd NE 242",
        "100 Georgia Ave",
        "3101 Texas Sage",
        "Ohio state university",
        "Kroger - Indiana Ave",
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


def _public_request(request_type: str, address: str):
    return SimpleNamespace(
        request_type=SimpleNamespace(name=request_type),
        address=address,
        state=None,
        location=None,
        retailer=None,
    )


def test_public_ld_retail_sampling_goes_to_the_three_rmms_in_any_state():
    expected = [
        "l.giaccio@liquiddeath.com",
        "ross@liquiddeath.com",
        "pat@liquiddeath.com",
    ]
    for address in ("1885 Halite Dr, Sparks, NV 89436", "Columbia, Missouri", "1608 Broadway St"):
        req = _public_request("Retail Sampling", address)
        assert public_form_rmm_emails("ighn-liquid-death", req) == expected


def test_departed_rmm_states_go_to_the_remaining_three():
    remaining = [
        "l.giaccio@liquiddeath.com",
        "ross@liquiddeath.com",
        "pat@liquiddeath.com",
    ]
    for state in ("CA", "FL", "WI"):
        assert territory_emails_for_state("ighn-liquid-death", state) == remaining
    assert territory_emails_for_state("ighn-liquid-death", "DE") == ["pat@liquiddeath.com"]


def test_street_named_after_a_state_does_not_pick_the_territory():
    # Old parser: "Washington St" → WA (pat@), "Peachtree Rd NE" → NE.
    req = _public_request("Event Activation", "13657 Washington St, Edmond, OK 73034")
    assert public_form_rmm_emails("ighn-liquid-death", req) == ["ross@liquiddeath.com"]

    req = _public_request("Event Activation", "13657 Washington St")
    assert public_form_rmm_emails("ighn-liquid-death", req) == []

    req = _public_request("Event Activation", "13657 Washington St")
    req.state = SimpleNamespace(code="tx")
    assert public_form_rmm_emails("ighn-liquid-death", req) == ["ross@liquiddeath.com"]

    req = _public_request("Event Activation", "3954A Peachtree Rd NE")
    req.location = SimpleNamespace(state=SimpleNamespace(code="NY"))
    assert public_form_rmm_emails("ighn-liquid-death", req) == ["l.giaccio@liquiddeath.com"]


def test_public_ld_other_types_keep_territory_routing():
    req = _public_request("Event Activation", "EDMOND, OK, 73034")
    assert public_form_rmm_emails("ighn-liquid-death", req) == ["ross@liquiddeath.com"]
    assert public_form_rmm_emails("some-other-tenant", _public_request("Retail Sampling", "Chino, CA")) == []
