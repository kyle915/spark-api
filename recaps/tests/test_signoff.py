"""Clerk sign-off values: one letter, a real stroke, and nothing for other brands."""

from __future__ import annotations

import base64
from io import BytesIO
from types import SimpleNamespace

import pytest
from PIL import Image

from recaps.excel import _format_field_value
from recaps.pdf import _format_field_value as pdf_format_field_value
from recaps.signoff import (
    FIRST_NAME_MESSAGE,
    LAST_INITIAL_MESSAGE,
    SIGNATURE_MISSING_MESSAGE,
    SIGNATURE_UNREADABLE_MESSAGE,
    SignoffIncomplete,
    signature_data_url,
    signature_has_ink,
    signature_pdf_html,
    stored_signoff_value,
)
from recaps.voice_draft import field_kind


def _png(dark: bool) -> str:
    image = Image.new("L", (80, 40), 255)
    if dark:
        for x in range(10, 70):
            image.putpixel((x, 20), 20)
    buf = BytesIO()
    image.save(buf, format="PNG")
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()


def _field(name: str, kind: str, *, section: str = "Store sign-off", required: bool = True):
    return SimpleNamespace(
        name=name,
        required=required,
        custom_field_type=SimpleNamespace(name=kind),
        recap_section=SimpleNamespace(name=section),
    )


def test_letter_and_name_normalize():
    initial = _field("Last initial", "letter")
    assert stored_signoff_value(initial, "m", enforce_required=True) == "M"
    with pytest.raises(SignoffIncomplete, match=LAST_INITIAL_MESSAGE):
        stored_signoff_value(initial, "mm", enforce_required=True)
    with pytest.raises(SignoffIncomplete, match=LAST_INITIAL_MESSAGE):
        stored_signoff_value(initial, "3", enforce_required=True)

    first = _field("First name", "text")
    assert stored_signoff_value(first, "  Mary Ann ", enforce_required=True) == "Mary Ann"
    assert stored_signoff_value(first, "José", enforce_required=True) == "José"
    with pytest.raises(SignoffIncomplete, match=FIRST_NAME_MESSAGE):
        stored_signoff_value(first, "Alex2", enforce_required=True)
    with pytest.raises(SignoffIncomplete, match=FIRST_NAME_MESSAGE):
        stored_signoff_value(first, "", enforce_required=True)


def _data_url(image: Image.Image) -> str:
    buf = BytesIO()
    image.save(buf, format="PNG")
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()


def test_a_transparent_blank_canvas_is_not_ink():
    blank = _data_url(Image.new("RGBA", (80, 40), (0, 0, 0, 0)))
    stroke = Image.new("RGBA", (80, 40), (0, 0, 0, 0))
    for x in range(10, 70):
        stroke.putpixel((x, 20), (20, 20, 20, 255))
    assert not signature_has_ink(blank)
    assert signature_has_ink(_data_url(stroke))
    field = _field("Signature", "signature")
    with pytest.raises(SignoffIncomplete, match=SIGNATURE_UNREADABLE_MESSAGE):
        stored_signoff_value(field, blank, enforce_required=True)


def test_signature_must_be_an_inked_png():
    field = _field("Signature", "signature")
    inked = _png(dark=True)
    blank = _png(dark=False)
    assert signature_data_url(inked) == inked
    assert signature_has_ink(inked)
    assert not signature_has_ink(blank)
    assert stored_signoff_value(field, inked, enforce_required=True) == inked
    with pytest.raises(SignoffIncomplete, match=SIGNATURE_MISSING_MESSAGE):
        stored_signoff_value(field, "", enforce_required=True)
    with pytest.raises(SignoffIncomplete, match=SIGNATURE_UNREADABLE_MESSAGE):
        stored_signoff_value(field, blank, enforce_required=True)
    # An older recap can be saved without backfilling the signature.
    assert stored_signoff_value(field, "", enforce_required=False) == ""


def test_other_fields_pass_through_and_text_exports_do_not_dump_the_png():
    notes = _field("Account Feedback", "longtext", section="Shift Overview")
    assert stored_signoff_value(notes, "Busy aisle", enforce_required=True) == "Busy aisle"
    inked = _png(dark=True)
    assert _format_field_value(inked) == "Signed"
    assert pdf_format_field_value(inked) == "Signed"
    html = signature_pdf_html("Signature", inked)
    assert "<img " in html
    assert "<p>" not in html
    assert "data:image/png;base64," in html


def test_voice_does_not_fill_the_clerk_signoff():
    assert field_kind("First name", "text", []) is None
    assert field_kind("Last initial", "letter", []) is None
    assert field_kind("Signature", "signature", []) is None
    assert field_kind("Account Feedback", "longtext", []) == "text"
