"""Unit tests for structured Store Signal extraction (`recaps.store_signals`)."""

from __future__ import annotations

from unittest.mock import patch

from django.test import override_settings

from recaps.store_signals import (
    STORE_SIGNAL_SCHEMA,
    extract_store_signals_from_text,
    heuristic_extract_store_signals,
    normalize_store_signals,
)


def test_store_signal_schema_is_strict_and_forbids_additional_properties():
    assert STORE_SIGNAL_SCHEMA["additionalProperties"] is False
    for key in ("stockout_risk", "reorder_opportunity", "manager_contact", "competitor_promo"):
        sub = STORE_SIGNAL_SCHEMA["properties"][key]
        assert sub["additionalProperties"] is False
        assert set(sub["required"]) == set(sub["properties"])


def test_heuristic_extractor_catches_stockout_reorder_and_scrubs_pii():
    note = (
        "Store manager Mike (512-555-0199, mike@heb.test) wants 4 more cases of Mango; "
        "down to 6 cans in back and competitor had a BOGO endcap promo."
    )
    sig = heuristic_extract_store_signals(note)
    assert sig["has_signal"] is True
    assert sig["stockout_risk"]["flagged"] is True
    assert "down to 6 cans" in sig["stockout_risk"]["detail"].lower()
    assert sig["reorder_opportunity"]["flagged"] is True
    assert "4 more cases" in sig["reorder_opportunity"]["detail"].lower()
    assert sig["manager_contact"]["flagged"] is True
    assert "store manager mike" in sig["manager_contact"]["contact"].lower()
    assert sig["competitor_promo"]["flagged"] is True
    # Phone and email scrubbed
    blob = str(sig)
    assert "555-0199" not in blob
    assert "mike@heb.test" not in blob


@override_settings(OPENAI_API_KEY="test-key")
def test_extract_store_signals_uses_small_model_and_normalizes():
    with patch(
        "recaps.store_signals.generate_json",
        return_value={
            "has_signal": False,  # recomputed to True because reorder_opportunity is flagged
            "stockout_risk": {"flagged": False, "detail": ""},
            "reorder_opportunity": {
                "flagged": True,
                "detail": "Grocery buyer requested 5 cases of Peach before Friday.",
            },
            "manager_contact": {
                "flagged": True,
                "contact": "Grocery buyer",
                "detail": "Asked distributor rep to deliver Tuesday morning.",
            },
            "competitor_promo": {"flagged": True, "competitor": "RivalCo", "detail": ""},
        },
    ) as gen:
        sig = extract_store_signals_from_text(
            "Grocery buyer requested 5 cases of Peach before Friday.",
            brand="Pivot Energy",
        )
    assert gen.call_args.kwargs["small"] is True
    assert sig["has_signal"] is True
    assert sig["reorder_opportunity"]["flagged"] is True
    assert sig["manager_contact"]["flagged"] is True
    # Flagged with empty detail is normalized to False so blank badges never appear.
    assert sig["competitor_promo"]["flagged"] is False


def test_normalize_handles_non_dict_cleanly():
    sig = normalize_store_signals(None)
    assert sig["has_signal"] is False
