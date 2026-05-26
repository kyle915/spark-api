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
    ],
)
def test_extract_state_code_returns_none(address):
    assert extract_state_code(address) is None


def test_extract_state_code_international_address_is_handled_downstream():
    """A UK address surfaces 'UK' here because it pattern-matches
    [A-Z]{2} at the end of the string. We don't filter it at the regex
    level because (a) maintaining a state allowlist creates a separate
    drift hazard and (b) downstream `territory_emails_for_state`
    returns [] for any non-US code, which then routes the request to
    Ignite-only via the same path as an unparseable address. The full
    chain is what produces the correct outcome, not this single step.
    """
    assert extract_state_code("10 Downing St, London, SW1A 2AA, UK") == "UK"
    # The important downstream guarantee:
    assert territory_emails_for_state("ighn-liquid-death", "UK") == []


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
