"""Aisle shelf coach: nudges + optional ShelfReading persist."""

from __future__ import annotations

from decimal import Decimal
from unittest.mock import patch

import pytest
from django.test import override_settings

from recaps.shelf_check import NO, UNCLEAR, YES
from recaps.shelf_coach import (
    SHELF_COACH_UNAVAILABLE,
    coach_nudges,
    coach_shelf_photo,
    pack_response,
    persist_shelf_reading,
)


def test_coach_nudges_product_missing_and_no_price_tag():
    nudges = coach_nudges(
        {
            "status": "read",
            "product_on_shelf": NO,
            "facings": None,
            "price_tag_visible": NO,
            "price": None,
            "out_of_stock_gap": UNCLEAR,
        }
    )
    codes = [n["code"] for n in nudges]
    assert "product_missing" in codes
    assert "no_price_tag" in codes


def test_coach_nudges_unreadable_photo():
    nudges = coach_nudges({"status": "unreadable"})
    assert len(nudges) == 1
    assert nudges[0]["code"] == "not_shelf"


def test_coach_nudges_oos_gap():
    nudges = coach_nudges(
        {
            "status": "read",
            "product_on_shelf": YES,
            "facings": 3,
            "price_tag_visible": YES,
            "price": Decimal("3.49"),
            "out_of_stock_gap": YES,
        }
    )
    assert any(n["code"] == "oos_gap" for n in nudges)


@override_settings(OPENAI_API_KEY="")
def test_coach_unavailable_without_key():
    result = coach_shelf_photo(b"jpeg", brand="Torch", product_names=["5MG"])
    assert result["available"] is False
    assert result["reason"] == SHELF_COACH_UNAVAILABLE


@override_settings(OPENAI_API_KEY="test-key")
def test_coach_returns_nudges_from_vision():
    with patch(
        "recaps.shelf_coach.generate_vision_json",
        create=True,
    ):
        with patch(
            "utils.ai_text.generate_vision_json",
            return_value={
                "isShelfPhoto": True,
                "productOnShelf": "no",
                "facings": 0,
                "facingsCountClear": False,
                "priceTagVisible": "no",
                "price": "",
                "priceLegible": False,
                "outOfStockGap": "unclear",
            },
        ):
            result = coach_shelf_photo(
                b"jpeg-bytes", brand="Torch", product_names=["5MG"]
            )
    assert result["available"] is True
    assert result["status"] == "read"
    assert result["answers"]["productOnShelf"] == "no"
    assert any(n["code"] == "product_missing" for n in result["nudges"])
    packed = pack_response(result, persisted=False)
    assert "_answer" not in packed
    assert packed["available"] is True


def test_persist_skipped_without_file():
    assert persist_shelf_reading(None, {"status": "read", "product_on_shelf": YES}) is False


def test_persist_skips_unknown_status():
    class _File:
        id = 1

    assert persist_shelf_reading(_File(), {"status": "weird"}) is False
