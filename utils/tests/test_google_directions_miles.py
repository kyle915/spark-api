"""Unit tests for Google Directions multi-stop mileage helper."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from utils.map_matching import google_directions_route_miles


def test_google_directions_sums_leg_meters():
    pts = [(30.2672, -97.7431), (30.2710, -97.7530), (30.2300, -97.7800)]
    fake = MagicMock()
    fake.raise_for_status = MagicMock()
    fake.json.return_value = {
        "status": "OK",
        "routes": [
            {
                "legs": [
                    {
                        "distance": {"value": 1609},  # 1 mile
                        "end_location": {"lat": 30.2710, "lng": -97.7530},
                    },
                    {
                        "distance": {"value": 3219},  # ~2 miles
                        "end_location": {"lat": 30.2300, "lng": -97.7800},
                    },
                ]
            }
        ],
    }
    with (
        patch("utils.map_matching._google_maps_api_key", return_value="test-key"),
        patch("utils.map_matching.httpx.get", return_value=fake) as get,
    ):
        out = google_directions_route_miles(pts)
    assert out is not None
    assert out["miles"] == 3.0  # (1609+3219)/1609.344 ≈ 3.00
    assert get.called
    params = get.call_args.kwargs["params"]
    assert params["origin"].startswith("30.2672")
    assert "waypoints" in params


def test_google_directions_skips_without_key():
    with patch("utils.map_matching._google_maps_api_key", return_value=""):
        assert google_directions_route_miles([(1.0, 2.0), (3.0, 4.0)]) is None
