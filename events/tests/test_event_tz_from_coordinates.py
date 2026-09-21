"""Event timezone must come from the address's COORDINATES, not its state.

A state is not a timezone. Fourteen-odd US states span two zones, and three
of them are live markets: Knoxville TN is Eastern while "TN" maps to Central,
Pensacola FL is Central while "FL" maps to Eastern, El Paso TX is Mountain
while "TX" maps to Central. Resolving by state told BAs at those addresses to
arrive an hour off, so the coordinate answer must win whenever we have one —
and the state map must still be there when we don't.

No network: both the geocoder and the coordinate lookup are injected.
"""
import pytest

from events.sheet_event_confirmations import resolve_timezone_for_row

# lat/lng are what Photon would return; the fake coord_tz keys off them.
KNOXVILLE = {"lat": 35.9207, "lng": -84.0129, "state": "Tennessee"}
PENSACOLA = {"lat": 30.4996, "lng": -87.2169, "state": "Florida"}
EL_PASO = {"lat": 31.7619, "lng": -106.4850, "state": "Texas"}
CHICAGO = {"lat": 41.9100, "lng": -87.6516, "state": "Illinois"}

TRUE_ZONE = {
    (35.9207, -84.0129): "America/New_York",   # Knoxville — NOT Central
    (30.4996, -87.2169): "America/Chicago",    # Pensacola — NOT Eastern
    (31.7619, -106.4850): "America/Denver",    # El Paso  — NOT Central
    (41.9100, -87.6516): "America/Chicago",
}


def _coord_tz(lat, lng):
    return TRUE_ZONE.get((lat, lng))


def _geo(feature):
    return lambda _addr: feature


@pytest.mark.parametrize(
    "feature,state_hint,expected",
    [
        (KNOXVILLE, "TN", "America/New_York"),
        (PENSACOLA, "FL", "America/Chicago"),
        (EL_PASO, "TX", "America/Denver"),
        (CHICAGO, "IL", "America/Chicago"),
    ],
)
def test_coordinates_beat_the_state_map(feature, state_hint, expected):
    tz, _note = resolve_timezone_for_row(
        "any address", state_hint, geocode=_geo(feature), coord_tz=_coord_tz
    )
    assert tz == expected


def test_split_state_without_coordinates_is_flagged_not_silent():
    """Guards the regression: with no coordinate answer, TN really does fall
    back to Central — wrong for Knoxville. That is allowed (a send must not
    die on timezone resolution) but it must SAY SO, so the note is the only
    signal anyone gets that the time may be an hour off."""
    tz, note = resolve_timezone_for_row(
        "any address", "TN", geocode=_geo(KNOXVILLE), coord_tz=lambda *_: None
    )
    assert tz == "America/Chicago"
    assert "spans two zones" in note
    assert "may be an hour off" in note


def test_single_zone_state_fallback_is_exact_and_unflagged():
    """Illinois has one zone, so the state answer IS the right answer — it
    must not carry a scary note that would train people to ignore them."""
    tz, note = resolve_timezone_for_row(
        "any address", "IL", geocode=_geo(CHICAGO), coord_tz=lambda *_: None
    )
    assert tz == "America/Chicago"
    assert note == ""


def test_a_raising_coord_lookup_never_blocks_a_send():
    def boom(*_a, **_k):
        raise RuntimeError("timezone api down")

    tz, note = resolve_timezone_for_row(
        "any address", "IL", geocode=_geo(CHICAGO), coord_tz=boom
    )
    assert tz == "America/Chicago"
    assert "coord→tz error" in note


def test_geocode_failure_still_uses_the_sheet_state():
    tz, note = resolve_timezone_for_row(
        "any address", "TX", geocode=lambda _a: None, coord_tz=_coord_tz
    )
    assert tz == "America/Chicago"
    assert "geocode failed" in note


def test_no_address_and_no_state_still_returns_a_usable_zone():
    tz, _note = resolve_timezone_for_row(
        "", "", geocode=lambda _a: None, coord_tz=_coord_tz
    )
    assert tz  # never empty — a send must not die on timezone resolution
