"""Unit tests for the Shelf Compliance, Pricing & Competitor Voice Hub."""

from __future__ import annotations

from recaps.shelf_voice_hub import (
    classify_mention_trend,
    classify_price_drift_pct,
    compute_before_after_pairs,
)


def test_classify_price_drift_pct():
    assert classify_price_drift_pct(2.99, 2.49) == 20.1
    assert classify_price_drift_pct(2.24, 2.49) == -10.0
    assert classify_price_drift_pct(2.49, 2.49) == 0.0
    assert classify_price_drift_pct(None, 2.49) is None
    assert classify_price_drift_pct(2.49, None) is None
    assert classify_price_drift_pct(2.49, 0.0) is None


def test_classify_mention_trend():
    assert classify_mention_trend(4, 0) == "new"
    assert classify_mention_trend(5, 2) == "rising"
    assert classify_mention_trend(3, 3) == "steady"
    assert classify_mention_trend(1, 4) == "cooling"
    assert classify_mention_trend(0, 0) == "steady"


def test_compute_before_after_pairs_orders_chronologically():
    photos = [
        {
            "file_id": "20",
            "store_key": "heb:725",
            "store_name": "HEB #725",
            "market": "Houston, TX",
            "state_code": "TX",
            "event_date": "2026-11-12",
            "photo_url": "https://example.com/after.jpg",
            "facings": 5,
        },
        {
            "file_id": "10",
            "store_key": "heb:725",
            "store_name": "HEB #725",
            "market": "Houston, TX",
            "state_code": "TX",
            "event_date": "2026-10-02",
            "photo_url": "https://example.com/before.jpg",
            "facings": 2,
        },
        {
            "file_id": "30",
            "store_key": "kroger:11",
            "store_name": "Kroger #11",
            "market": "Dallas, TX",
            "state_code": "TX",
            "event_date": "2026-11-01",
            "photo_url": "https://example.com/only.jpg",
            "facings": 3,
        },
    ]

    pairs = compute_before_after_pairs(photos)
    assert len(pairs) == 1
    assert pairs[0]["store_name"] == "HEB #725"
    assert pairs[0]["photo_count"] == 2
    assert pairs[0]["before_photo"]["file_id"] == "10"
    assert pairs[0]["after_photo"]["file_id"] == "20"
