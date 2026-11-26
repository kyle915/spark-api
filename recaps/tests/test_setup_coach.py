"""Unit tests for setup-mode vision coaching in `recaps.shelf_coach`."""

from __future__ import annotations

from unittest.mock import patch

from django.test import override_settings

from recaps.shelf_coach import (
    MODE_SETUP,
    MODE_SHELF,
    coach_shelf_photo,
    detect_coach_mode,
    pack_response,
    setup_coach_nudges,
)


def test_detect_coach_mode_distinguishes_shelf_setup_and_receipt():
    assert detect_coach_mode(None, "Shelf / Cooler") == MODE_SHELF
    assert detect_coach_mode(None, "Before & After Stock") == MODE_SHELF
    assert detect_coach_mode(None, "Event Setup / Footprint") == MODE_SETUP
    assert detect_coach_mode(None, "Table Setup") == MODE_SETUP
    assert detect_coach_mode(None, "Demo Display") == MODE_SETUP
    assert detect_coach_mode("setup", "Photos") == MODE_SETUP
    # Receipts default safely and never trigger setup mode.
    assert detect_coach_mode(None, "Expense Receipt") == MODE_SHELF


def test_setup_coach_nudges_flags_blur_and_missing_product():
    nudges = setup_coach_nudges(
        {
            "status": "read",
            "photo_clear": "no",
            "product_visible": "no",
            "branding_visible": "unclear",
        }
    )
    codes = [n["code"] for n in nudges]
    assert "blurry_or_dark" in codes
    assert "product_missing_on_setup" in codes


def test_setup_coach_nudges_unreadable_photo():
    nudges = setup_coach_nudges({"status": "unreadable"})
    assert len(nudges) == 1
    assert nudges[0]["code"] == "not_setup"


@override_settings(OPENAI_API_KEY="test-key")
def test_coach_shelf_photo_setup_mode_never_emits_internal_shelf_answer():
    with patch(
        "utils.ai_text.generate_vision_json",
        return_value={
            "isSetupPhoto": True,
            "photoClear": "yes",
            "productVisible": "yes",
            "brandingVisible": "yes",
        },
    ) as vision:
        result = coach_shelf_photo(
            b"jpeg-bytes",
            brand="Torch",
            product_names=["5MG"],
            content_type="image/jpeg",
            mode=MODE_SETUP,
        )
    assert result["available"] is True
    assert result["mode"] == MODE_SETUP
    assert result["nudges"] == []
    assert "Setup looks sharp" in result["message"]
    # Setup mode must never include `_answer` so a ShelfReading is never persisted.
    assert "_answer" not in result
    assert "ignore them completely" in vision.call_args.args[0]
    packed = pack_response(result, persisted=False)
    assert packed["available"] is True
