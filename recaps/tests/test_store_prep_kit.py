"""Store Prep Kit composition helpers."""

from __future__ import annotations

from recaps.store_prep_kit import prep_kit_is_empty, _stock_risk


def test_prep_kit_is_empty_when_blank():
    assert prep_kit_is_empty({}) is True
    assert prep_kit_is_empty(None) is True
    assert prep_kit_is_empty({"logisticsNotes": "", "skus": [], "resources": []}) is True


def test_prep_kit_not_empty_with_logistics_or_risk():
    assert prep_kit_is_empty({"logisticsNotes": "Cooler aisle 4"}) is False
    assert prep_kit_is_empty({"skus": ["Mango"]}) is False
    assert (
        prep_kit_is_empty(
            {"stockRisk": {"level": "high", "reason": "Last visit OOS"}}
        )
        is False
    )
    assert (
        prep_kit_is_empty(
            {"stockRisk": {"level": "none", "reason": "On shelf last time"}}
        )
        is True
    )


def test_stock_risk_levels():
    assert _stock_risk(None, None) is None
    high = _stock_risk(
        None,
        {
            "eventDate": "2026-11-01",
            "productOnShelf": "no",
            "priceTagVisible": "unclear",
        },
    )
    assert high["level"] == "high"
    watch = _stock_risk(
        None,
        {
            "eventDate": "2026-11-01",
            "productOnShelf": "yes",
            "priceTagVisible": "no",
        },
    )
    assert watch["level"] == "watch"
    ok = _stock_risk(
        None,
        {
            "eventDate": "2026-11-01",
            "productOnShelf": "yes",
            "priceTagVisible": "yes",
        },
    )
    assert ok["level"] == "none"
