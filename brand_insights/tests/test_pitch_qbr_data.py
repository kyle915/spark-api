"""Unit tests for Buyer Pitch Deck QBR / Line-Review enrichments.

Covers:
- State code extraction and activation footprint aggregation
- 2–4 week post-demo POS lift halo computation and deduplication
- Fallback footprint synthesis for legacy pitch deck snapshots
- Public pitch payload backward compatibility & QBR keys presence
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from types import SimpleNamespace

from brand_insights.pitch_qbr_data import (
    build_pitch_footprint,
    compute_pos_halo_from_rows,
    extract_state_code,
    synthesize_fallback_footprint,
)


def test_extract_state_code_handles_explicit_and_addresses():
    assert extract_state_code("tx") == "TX"
    assert extract_state_code("", "Austin, TX") == "TX"
    assert extract_state_code("", "", "123 Main St, Los Angeles, CA 90001") == "CA"
    assert extract_state_code("", "Denver, CO 80202-1234") == "CO"
    assert extract_state_code("", "Unknown Place") == ""
    assert extract_state_code("XX") == ""


def test_build_pitch_footprint_aggregates_states_and_markets():
    visits = [
        SimpleNamespace(
            id=1,
            store_key="s1",
            state_code="TX",
            market="Austin, TX",
            city="Austin",
            address="100 Congress Ave, Austin, TX 78701",
            samples_distributed=120,
            rep_observed_units=24,
        ),
        SimpleNamespace(
            id=2,
            store_key="s1",
            state_code="TX",
            market="Austin, TX",
            city="Austin",
            address="100 Congress Ave, Austin, TX 78701",
            samples_distributed=80,
            rep_observed_units=16,
        ),
        SimpleNamespace(
            id=3,
            store_key="s2",
            state_code="",
            market="Los Angeles, CA",
            city="Los Angeles",
            address="500 Sunset Blvd, Los Angeles, CA 90028",
            samples_distributed=150,
            rep_observed_units=30,
        ),
    ]
    fp = build_pitch_footprint(visits, adj_units_by_store={"s1": 52})

    assert fp["statesCount"] == 2
    assert fp["marketsCount"] == 2
    assert fp["states"][0]["state"] == "TX"
    assert fp["states"][0]["stores"] == 1
    assert fp["states"][0]["visits"] == 2
    assert fp["states"][0]["samples"] == 200
    assert fp["states"][0]["repUnits"] == 40
    assert fp["states"][0]["registerUnits"] == 52
    assert fp["states"][1]["state"] == "CA"
    assert fp["states"][1]["registerUnits"] is None


def test_compute_pos_halo_2_to_4_weeks_post_demo():
    demo_week = date(2026, 10, 5)  # Monday
    demo_weeks_by_store = {"heb-1": [demo_week]}
    sales_rows = [
        # Pre-demo baseline (-2w and -1w -> avg 20)
        {
            "store_key": "heb-1",
            "product_id": 10,
            "week_start": date(2026, 9, 21),
            "register_units": 18,
            "baseline_units": None,
        },
        {
            "store_key": "heb-1",
            "product_id": 10,
            "week_start": date(2026, 9, 28),
            "register_units": 22,
            "baseline_units": None,
        },
        # Demo week (offset 0 -> 40, +100%)
        {
            "store_key": "heb-1",
            "product_id": 10,
            "week_start": date(2026, 10, 5),
            "register_units": 40,
            "baseline_units": Decimal("20.00"),
        },
        # Post-demo +1w (32, +60%)
        {
            "store_key": "heb-1",
            "product_id": 10,
            "week_start": date(2026, 10, 12),
            "register_units": 32,
            "baseline_units": None,
        },
        # Post-demo +2w (28, +40%)
        {
            "store_key": "heb-1",
            "product_id": 10,
            "week_start": date(2026, 10, 19),
            "register_units": 28,
            "baseline_units": None,
        },
        # Post-demo +3w (24, +20%)
        {
            "store_key": "heb-1",
            "product_id": 10,
            "week_start": date(2026, 10, 26),
            "register_units": 24,
            "baseline_units": None,
        },
        # Post-demo +4w (24, +20%)
        {
            "store_key": "heb-1",
            "product_id": 10,
            "week_start": date(2026, 11, 2),
            "register_units": 24,
            "baseline_units": None,
        },
    ]

    halo = compute_pos_halo_from_rows(demo_weeks_by_store, sales_rows)
    assert halo["available"] is True
    assert halo["baselineAvgUnits"] == 20.0
    assert halo["demoWeekLiftPct"] == 100.0
    # Post-demo avg across +1..+4 = (32+28+24+24)/4 = 27.0 -> +35.0%
    assert halo["postDemoHaloPct"] == 35.0
    assert halo["matchedStores"] == 1
    assert [w["offset"] for w in halo["weeks"]] == [-1, 0, 1, 2, 3, 4]
    assert halo["weeks"][2]["label"] == "Week +1"
    assert halo["weeks"][2]["liftPct"] == 60.0


def test_compute_pos_halo_empty_is_honestly_unavailable():
    halo = compute_pos_halo_from_rows({}, [])
    assert halo["available"] is False
    assert halo["weeks"] == []
    assert halo["postDemoHaloPct"] is None


def test_synthesize_fallback_footprint_handles_legacy_decks():
    fp = synthesize_fallback_footprint(
        [
            {
                "storeLabel": "HEB #725",
                "market": "Houston, TX",
                "visits": 3,
                "samplesDistributed": 300,
                "repObservedUnits": 45,
            },
            {
                "storeLabel": "Ralphs #101",
                "market": "San Diego, CA",
                "visits": 2,
                "samplesDistributed": 180,
                "repObservedUnits": 30,
            },
        ]
    )
    assert fp["statesCount"] == 2
    assert [s["state"] for s in fp["states"]] == ["TX", "CA"]
