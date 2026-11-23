"""One line on why a CSV row matches more than one store.

The rules decide that the row is ambiguous and list the candidate stores.
This only rewords that list. It never picks a store, and a line that names
exactly one of several candidates is thrown away.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re

from django.core.cache import cache

logger = logging.getLogger(__name__)

_MAX_CHARS = 300
_CACHE_SECONDS = 14 * 24 * 3600
_NUMBER_RE = re.compile(r"\d+(?:[.,]\d+)*")
_NUMBER_WORDS = {
    "two": "2",
    "three": "3",
    "four": "4",
    "five": "5",
    "six": "6",
    "seven": "7",
    "eight": "8",
    "nine": "9",
    "ten": "10",
}

_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["why"],
    "properties": {"why": {"type": "string"}},
}

_SYSTEM = (
    "You explain to an operations coordinator why a spreadsheet row cannot "
    "be matched to one store yet. You are given the banner written on the "
    "row and the candidate stores (name, city, state). Write ONE short "
    "sentence. Do not choose a store. Do not add a store that is not in the "
    "list. Do not add numbers that are not in the list. Do not give advice "
    "beyond the fact that a person has to pick. At most 30 words."
)


def _facts(banner: str, candidates: list[dict]) -> str:
    lines = [f"Banner on the row: {banner}", f"Candidate count: {len(candidates)}"]
    for item in candidates[:8]:
        name = item.get("name") or ""
        city = item.get("city") or ""
        state = item.get("state") or ""
        lines.append(f"- {name} | {city} | {state}")
    return "\n".join(lines)


def _allowed_numbers(banner: str, candidates: list[dict]) -> set[str]:
    text = banner + " " + str(len(candidates))
    for item in candidates:
        text += f" {item.get('name') or ''} {item.get('city') or ''} {item.get('state') or ''}"
    allowed = set()
    for found in _NUMBER_RE.findall(text):
        allowed.add(found)
        allowed.add(found.replace(",", ""))
    return allowed


def _safe(text: str, banner: str, candidates: list[dict]) -> str | None:
    cleaned = " ".join(text.split())
    if not cleaned or len(cleaned) > _MAX_CHARS:
        return None
    allowed = _allowed_numbers(banner, candidates)
    for found in _NUMBER_RE.findall(cleaned):
        if found not in allowed and found.replace(",", "") not in allowed:
            return None
    low = cleaned.lower()
    for word, digits in _NUMBER_WORDS.items():
        if re.search(rf"\b{word}\b", low) and digits not in allowed:
            return None
    names = [str(item.get("name") or "").strip() for item in candidates]
    names = [name for name in names if name]
    mentioned = [name for name in names if name.lower() in low]
    if len(candidates) > 1 and len(mentioned) == 1:
        return None
    return cleaned


def plain_store_match_reason(banner: str, candidates: list[dict]) -> str:
    """A one-line explanation, or ``""`` when AI is off or the line is unsafe.

    Never selects a store. Callers still have to confirm.
    """
    from django.conf import settings

    from utils.ai_text import AiUnavailable, generate_json

    if len(candidates) < 2:
        return ""
    if not (getattr(settings, "OPENAI_API_KEY", "") or "").strip():
        return ""
    raw_key = json.dumps(
        {
            "banner": banner,
            "candidates": [
                [c.get("id"), c.get("name"), c.get("city"), c.get("state")]
                for c in candidates
            ],
        },
        sort_keys=True,
        default=str,
    )
    key = "store_match_why:" + hashlib.sha256(raw_key.encode()).hexdigest()[:40]
    hit = cache.get(key)
    if isinstance(hit, str):
        return hit
    try:
        raw = generate_json(
            _SYSTEM,
            _facts(banner, candidates),
            schema=_SCHEMA,
            max_tokens=160,
            small=True,
            timeout=8,
        )
    except AiUnavailable:
        return ""
    except Exception:
        logger.info("store match reason failed", exc_info=True)
        return ""
    text = ""
    if isinstance(raw, dict) and isinstance(raw.get("why"), str):
        text = _safe(raw["why"], banner, candidates) or ""
    # Keep a rejected or empty line only briefly so a later preview can retry.
    cache.set(key, text, _CACHE_SECONDS if text else 3600)
    return text
