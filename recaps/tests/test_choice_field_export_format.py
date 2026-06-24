"""Multiselect choice values render as readable lists in the exports.

A multiselect recap answer is persisted as a JSON array of the chosen
option strings (e.g. '["Detroit", "Lansing"]'). The PDF and Excel exports
must show that as "Detroit, Lansing" — not the raw JSON — while leaving
single-select / text / number values untouched. This pins the small
formatter both export paths use.
"""
from __future__ import annotations

from recaps.excel import _format_field_value as excel_format
from recaps.pdf import _format_field_value as pdf_format


class TestChoiceValueFormatting:
    def test_multiselect_json_becomes_comma_list(self):
        raw = '["Detroit", "Grand Rapids", "Lansing"]'
        assert pdf_format(raw) == "Detroit, Grand Rapids, Lansing"
        assert excel_format(raw) == "Detroit, Grand Rapids, Lansing"

    def test_single_element_array(self):
        assert pdf_format('["Solo"]') == "Solo"
        assert excel_format('["Solo"]') == "Solo"

    def test_plain_string_passes_through(self):
        # Single-select / text answers are already display-ready.
        assert pdf_format("Detroit") == "Detroit"
        assert excel_format("Detroit") == "Detroit"

    def test_number_string_passes_through(self):
        assert pdf_format("42") == "42"
        assert excel_format("42") == "42"

    def test_malformed_bracketed_string_is_left_alone(self):
        # Looks array-ish but isn't valid JSON — don't mangle it.
        assert pdf_format("[not json]") == "[not json]"
        assert excel_format("[not json]") == "[not json]"

    def test_empty_value(self):
        assert pdf_format("") == ""
        # Excel coerces None/"" to "" via _safe; PDF preserves None.
        assert excel_format("") == ""
        assert excel_format(None) == ""
