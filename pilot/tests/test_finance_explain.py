"""Finance dispute explainers — rules pack + grounded model polish."""

from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import patch

from pilot.finance_explain import (
    _clean_model_line,
    facts_as_lines,
    finance_dispute_pack,
    rules_summary,
    suggested_next_step,
)


def _exc(**kwargs):
    defaults = dict(
        uuid="11111111-1111-1111-1111-111111111111",
        exception_type="missing_proof",
        severity="medium",
        status="open",
        affected_amount=Decimal("64.20"),
        source_label="Product spend",
        explanation=(
            "$64.20 Product Spend charge has no supporting receipt or proof attached."
        ),
        recap_file_id=None,
        cost_line_id=None,
        cost_line=None,
        canonical_visit_id=None,
        canonical_visit=None,
    )
    defaults.update(kwargs)
    return SimpleNamespace(**defaults)


def test_suggested_next_step_per_type():
    assert "itemized" in suggested_next_step("missing_proof").lower()
    assert "match" in suggested_next_step("unmatched_transaction").lower()


def test_rules_summary_keeps_dollar_from_explanation():
    text = rules_summary(_exc())
    assert "$64.20" in text
    assert text.endswith(".")


def test_model_line_rejects_invented_amount():
    allowed = {"64.20", "64"}
    assert (
        _clean_model_line({"why": "The $99.00 charge lacks a receipt"}, allowed)
        is None
    )
    assert _clean_model_line(
        {"why": "The $64.20 product spend has no receipt attached"},
        allowed,
    )


def test_finance_dispute_pack_rules_only_without_ocr():
    with patch("pilot.finance_explain._should_ask_model", return_value=False):
        pack = finance_dispute_pack(_exc())
    assert pack["source"] == "rules"
    assert "$64.20" in pack["summary"]
    assert pack["suggested_next_step"]
    assert any("Affected amount" in line for line in pack["facts"])


def test_finance_dispute_pack_uses_model_when_grounded():
    exc = _exc(
        explanation="Receipt from H-E-B ($64.20) is not matched to a visit cost line.",
        exception_type="unmatched_transaction",
    )
    facts = [
        {"code": "exception_type", "value": "unmatched_transaction"},
        {"code": "affected_amount", "value": "64.20"},
        {"code": "ocr_merchant", "value": "H-E-B"},
        {"code": "ocr_total", "value": "64.20"},
        {"code": "ocr_line_items", "value": "0"},
    ]
    with (
        patch("pilot.finance_explain.gather_dispute_facts", return_value=facts),
        patch(
            "pilot.finance_explain._model_summary",
            return_value=(
                "H-E-B receipt totaling $64.20 is not matched to a visit cost line."
            ),
        ),
    ):
        pack = finance_dispute_pack(exc)
    assert pack["source"] == "model"
    assert "H-E-B" in pack["summary"]
    assert "$64.20" in pack["summary"]
    lines = facts_as_lines(facts)
    assert any("OCR merchant: H-E-B" in line for line in lines)
