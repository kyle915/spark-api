"""Store-clerk sign-off on a recap: first name, one-letter last initial, signature.

The field types are global (`letter`, `signature`) so a brand can opt in by
putting them on its template. Only the Famus templates do that today. Clock-out
never calls this module — a missing sign-off blocks filing the recap, not
ending the shift, and a template without these fields is unchanged.
"""

from __future__ import annotations

import base64
import binascii
import re
from html import escape
from io import BytesIO

from PIL import Image

STORE_SIGNOFF_SECTION = "Store sign-off"
CLERK_FIRST_NAME = "First name"
CLERK_LAST_INITIAL = "Last initial"
CLERK_SIGNATURE = "Signature"
LETTER_TYPE = "letter"
SIGNATURE_TYPE = "signature"

_DATA_PREFIX = "data:image/png;base64,"
_MAX_URL_CHARS = 550_000
_MAX_PNG_BYTES = 400_000
_MIN_PNG_BYTES = 40
_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
_NAME_RE = re.compile(r"^[^\W\d_](?:[^\W\d_]|[ .'\-]){0,39}$", re.UNICODE)

FIRST_NAME_MESSAGE = "Enter the clerk's first name."
LAST_INITIAL_MESSAGE = "Last initial is one letter."
SIGNATURE_MISSING_MESSAGE = "The store clerk needs to sign."
SIGNATURE_UNREADABLE_MESSAGE = (
    "That signature couldn't be read. Ask the clerk to sign again."
)


class SignoffIncomplete(ValueError):
    """A required clerk sign-off answer is missing or not usable."""


def _type_name(field) -> str:
    return (
        getattr(getattr(field, "custom_field_type", None), "name", "") or ""
    ).strip().lower()


def _section_name(field) -> str:
    return (
        getattr(getattr(field, "recap_section", None), "name", "") or ""
    ).strip().lower()


def _field_name(field) -> str:
    return (getattr(field, "name", "") or "").strip().lower()


def is_signature_type_name(type_name: str | None) -> bool:
    return (type_name or "").strip().lower() == SIGNATURE_TYPE


def is_signature_field(field) -> bool:
    return is_signature_type_name(_type_name(field))


def is_letter_field(field) -> bool:
    return _type_name(field) == LETTER_TYPE


def is_clerk_first_name_field(field) -> bool:
    return (
        _section_name(field) == STORE_SIGNOFF_SECTION.lower()
        and _field_name(field) == CLERK_FIRST_NAME.lower()
    )


def is_signoff_field(field) -> bool:
    return is_signature_field(field) or is_letter_field(field) or is_clerk_first_name_field(field)


def signature_data_url(value) -> str | None:
    """A PNG data URL small enough to store, or None."""
    text = str(value or "").strip()
    if not text.startswith(_DATA_PREFIX) or len(text) > _MAX_URL_CHARS:
        return None
    payload = text[len(_DATA_PREFIX) :]
    if not payload or re.search(r"\s", payload):
        return None
    try:
        raw = base64.b64decode(payload, validate=True)
    except (binascii.Error, ValueError):
        return None
    if not _MIN_PNG_BYTES <= len(raw) <= _MAX_PNG_BYTES:
        return None
    if not raw.startswith(_PNG_MAGIC):
        return None
    return text


def signature_has_ink(data_url: str) -> bool:
    """True when the PNG has a dark stroke, not a blank pad.

    A cleared canvas is often transparent black. ``convert("L")`` drops the
    alpha and would read that as ink, so the drawing is composited onto white
    first.
    """
    raw = base64.b64decode(data_url.split(",", 1)[1], validate=True)
    try:
        with Image.open(BytesIO(raw)) as image:
            width, height = image.size
            if width > 2000 or height > 800 or width < 8 or height < 8:
                return False
            rgba = image.convert("RGBA")
            paper = Image.new("RGBA", rgba.size, (255, 255, 255, 255))
            darkest, _lightest = Image.alpha_composite(paper, rgba).convert("L").getextrema()
    except Exception:
        return False
    return darkest < 200


def signature_pdf_html(label: str, data_url: str) -> str:
    safe_label = escape(label or CLERK_SIGNATURE)
    return (
        f'<div class="signoff"><span>{safe_label}</span>'
        f'<figure><img src="{data_url}" alt="{safe_label}" '
        f'style="background:#ffffff;max-height:160px" /></figure></div>'
    )


def stored_signoff_value(field, value, *, enforce_required: bool):
    """Value to persist. Non-sign-off fields pass through unchanged.

    ``enforce_required`` is for filing a recap (walk-up submit and create).
    An admin edit of an older recap passes False so a blank historical
    answer does not block a correction. A non-blank answer is still checked.
    """
    if not is_signoff_field(field):
        return value
    required = bool(getattr(field, "required", False)) and enforce_required
    raw = "" if value is None else str(value).strip()
    if is_signature_field(field):
        if not raw:
            if required:
                raise SignoffIncomplete(SIGNATURE_MISSING_MESSAGE)
            return ""
        url = signature_data_url(raw)
        if url is None or not signature_has_ink(url):
            raise SignoffIncomplete(SIGNATURE_UNREADABLE_MESSAGE)
        return url
    if is_letter_field(field):
        if not raw:
            if required:
                raise SignoffIncomplete(LAST_INITIAL_MESSAGE)
            return ""
        letter = raw.upper()
        if len(letter) != 1 or not letter.isalpha():
            raise SignoffIncomplete(LAST_INITIAL_MESSAGE)
        return letter
    name = " ".join(raw.split())
    if not name:
        if required:
            raise SignoffIncomplete(FIRST_NAME_MESSAGE)
        return ""
    if _NAME_RE.fullmatch(name) is None:
        raise SignoffIncomplete(FIRST_NAME_MESSAGE)
    return name


def assert_recap_signoff(template, recap) -> None:
    """Refuse a filed recap whose template requires a clerk sign-off and is missing one.

    No-op when the template has no sign-off fields, so every other brand
    files exactly as before. Not used by clock-out.
    """
    from recaps.models import CustomField, CustomFieldValue

    fields = [
        field
        for field in CustomField.objects.filter(
            custom_recap_template=template, required=True
        )
        .select_related("custom_field_type", "recap_section")
        .order_by("recap_section__order", "order", "id")
        if is_signoff_field(field)
    ]
    if not fields:
        return
    values = {
        row.custom_field_id: row.value
        for row in CustomFieldValue.objects.filter(
            custom_recap=recap, custom_field__in=fields
        )
    }
    for field in fields:
        stored_signoff_value(field, values.get(field.id, ""), enforce_required=True)
