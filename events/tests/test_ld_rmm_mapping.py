"""The Liquid Death RMM-by-state map stays in sync with the routing territory.

ld_summary_export inverts events.routing.LIQUID_DEATH_TERRITORY to attribute
each recap to an RMM by state. This pins the inversion (incl. the DE tie-break
and the unmapped-state fallback) and guards against the two maps drifting.
"""
from __future__ import annotations

from events.routing import LIQUID_DEATH_TERRITORY
from recaps.ld_summary_export import RMM_EMAIL_TO_NAME, STATE_TO_RMM


def test_known_states_map_to_expected_rmm():
    assert STATE_TO_RMM["CA"] == "Kristyn"
    assert STATE_TO_RMM["NY"] == "Lauren"
    assert STATE_TO_RMM["TX"] == "Ross"
    assert STATE_TO_RMM["FL"] == "Manuela"
    assert STATE_TO_RMM["WA"] == "Pat"
    assert STATE_TO_RMM["OH"] == "Timothy"


def test_de_tiebreak_is_deterministic_manuela():
    # DE is listed under both Manuela and Pat; first-wins by RMM_ORDER → Manuela.
    assert STATE_TO_RMM["DE"] == "Manuela"


def test_unmapped_state_is_none():
    assert STATE_TO_RMM.get("ZZ") is None


def test_every_territory_state_is_mapped():
    all_states = {s for states in LIQUID_DEATH_TERRITORY.values() for s in states}
    missing = [s for s in all_states if s not in STATE_TO_RMM]
    assert not missing, f"states missing from STATE_TO_RMM: {missing}"


def test_every_territory_email_has_a_display_name():
    missing = [e for e in LIQUID_DEATH_TERRITORY if e not in RMM_EMAIL_TO_NAME]
    assert not missing, f"emails missing a display name: {missing}"
