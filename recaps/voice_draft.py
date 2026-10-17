"""Turn a walk-up transcript into notes, feedback, and competitor text.

The draft fills those text fields only. Numeric fields stay blank. This
module does not create a recap, does not set ``approved``, and does not
write a number onto the recap. Submit is still the only way to file.

When ``OPENAI_API_KEY`` is unset, the result is ``available=False`` and
``Voice draft unavailable``. The caller can still show the transcript.
"""

from __future__ import annotations

import re
from typing import Literal

from django.conf import settings

from utils.ai_text import AiUnavailable, generate_json

VOICE_DRAFT_UNAVAILABLE = "Voice draft unavailable"

DraftSlot = Literal["notes", "feedback", "competitor"]

_TEXT_HINTS = ("text", "string", "textarea")
_BLOCKED_TYPES = (
    "number",
    "int",
    "photo",
    "image",
    "select",
    "date",
    "bool",
    "check",
    "file",
)
_NUMERIC_NAME = re.compile(
    r"how many|number of|\bcount\b|amount|spend|mileage|sample|engagement|"
    r"\bcans?\b|pours?|quantity|percent|awareness|tables distributed|\bsku\b|"
    r"store number",
    re.IGNORECASE,
)
_COMPETITOR_NAME = re.compile(
    r"competitor|competitive|why they like|consumers drink|\bwhich one\b|"
    r"what .{0,40}drink",
    re.IGNORECASE,
)
_FEEDBACK_NAME = re.compile(r"\bfeedback\b|\bcomments?\b", re.IGNORECASE)
_NOTES_NAME = re.compile(r"\bnotes?\b", re.IGNORECASE)
_BARE_NUMBER = re.compile(r"^\d+(\.\d+)?$")

_MAX_TRANSCRIPT = 8000
_MAX_FILL = 2000

VOICE_SYSTEM = (
    "You turn a brand ambassador's spoken shift notes into short prose for "
    "the text boxes listed. Fill only those field ids. "
    "Write notes, feedback, and competitor comments in the ambassador's words. "
    "Never invent counts, samples, spend, or SKU quantities. "
    "Never output a bare number as a field's whole value. "
    "Use an empty string when the ambassador did not talk about that box. "
    "Do not name or identify any consumer."
)

_DRAFT_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["fills"],
    "properties": {
        "fills": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["fieldId", "text"],
                "properties": {
                    "fieldId": {"type": "string"},
                    "text": {"type": "string"},
                },
            },
        }
    },
}


def draft_slot(name: str, field_type: str) -> DraftSlot | None:
    """Which prose box this field is, or None when the draft must leave it blank."""
    kind = (field_type or "").strip().lower()
    if any(token in kind for token in _BLOCKED_TYPES):
        return None
    if kind and not any(token in kind for token in _TEXT_HINTS):
        return None
    label = name or ""
    if _NUMERIC_NAME.search(label):
        return None
    if _COMPETITOR_NAME.search(label):
        return "competitor"
    if _FEEDBACK_NAME.search(label):
        return "feedback"
    if _NOTES_NAME.search(label):
        return "notes"
    return None


def eligible_fields(fields: list[dict]) -> list[dict]:
    """Template fields the draft is allowed to write. Each item keeps id, name, slot."""
    chosen: list[dict] = []
    for field in fields:
        if not isinstance(field, dict):
            continue
        field_id = str(field.get("id") or "").strip()
        name = str(field.get("name") or "")
        slot = draft_slot(name, str(field.get("type") or ""))
        if not field_id or slot is None:
            continue
        chosen.append({"id": field_id, "name": name, "slot": slot})
    return chosen


def sanitize_fills(fills: object, allowed_ids: set[str]) -> list[dict]:
    """Drop unknown ids, blank text, and values that are only a number."""
    if not isinstance(fills, list):
        return []
    cleaned: list[dict] = []
    seen: set[str] = set()
    for item in fills:
        if not isinstance(item, dict):
            continue
        field_id = str(item.get("fieldId") or item.get("field_id") or "").strip()
        text = item.get("text")
        if field_id not in allowed_ids or field_id in seen:
            continue
        if not isinstance(text, str):
            continue
        text = text.strip()
        if not text or _BARE_NUMBER.fullmatch(text):
            continue
        seen.add(field_id)
        cleaned.append({"fieldId": field_id, "text": text[:_MAX_FILL]})
    return cleaned


def _unavailable() -> dict:
    return {
        "available": False,
        "reason": VOICE_DRAFT_UNAVAILABLE,
        "fills": [],
    }


def structure_transcript(transcript: str, fields: list[dict]) -> dict:
    """Map a transcript onto eligible text fields. Never raises. Never files."""
    prose = (transcript or "").strip()
    if not prose:
        return {"available": True, "reason": "", "fills": []}
    allowed = eligible_fields(fields)
    if not allowed:
        return {"available": True, "reason": "", "fills": []}
    if not (getattr(settings, "OPENAI_API_KEY", "") or "").strip():
        return _unavailable()

    listing = "\n".join(
        f"- {row['id']} ({row['slot']}): {row['name']}" for row in allowed
    )
    user = (
        "Field boxes:\n"
        f"{listing}\n\n"
        "Transcript:\n"
        f"{prose[:_MAX_TRANSCRIPT]}"
    )
    try:
        parsed = generate_json(VOICE_SYSTEM, user, schema=_DRAFT_SCHEMA, max_tokens=1200)
    except AiUnavailable:
        return _unavailable()
    if not isinstance(parsed, dict):
        return _unavailable()
    fills = sanitize_fills(parsed.get("fills"), {row["id"] for row in allowed})
    return {"available": True, "reason": "", "fills": fills}
