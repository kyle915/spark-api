"""Short “why Needs Eyes” blurbs for the admin Needs-review list.

The Clean / Needs Eyes label stays in :mod:`recaps.review_triage`. This module
only writes a sentence, and only for Needs Eyes:

* Clean — no sentence and no model call.
* One reason — a fixed sentence from that reason's own label.
* Several reasons — the model may join those labels into one line. The line
  is dropped if it adds a number that was not in the labels. The fixed join
  is shown instead. Nothing here approves, rejects, or edits a recap.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from concurrent.futures import ThreadPoolExecutor

from django.conf import settings
from django.core.cache import cache

from recaps.review_triage import LABEL_NEEDS_EYES

logger = logging.getLogger(__name__)

MAX_WHY_CHARS = 280
MAX_MODEL_CALLS = 6
_MODEL_CACHE_SECONDS = 30 * 24 * 3600
_RULES_CACHE_SECONDS = 6 * 3600

_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["why"],
    "properties": {"why": {"type": "string"}},
}

_SYSTEM = (
    "You help an operations reviewer at a field-marketing agency. A report "
    "was marked Needs Eyes by automatic checks. You are given those checks "
    "as short labels, most important first. Write ONE sentence, or two short "
    "ones, that says why it needs a look, in the order given. Plain words, "
    "sentence case, no lists, no advice, no opinion on who is at fault. Use "
    "only the facts in the labels. Do not add, round, or compute any number. "
    "Do not mention names, stores, or brands — you are not given any. At most "
    "40 words."
)

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
    "eleven": "11",
    "twelve": "12",
}


def _sentence(label: str) -> str:
    text = " ".join(str(label or "").split()).strip().rstrip(".")
    if not text:
        return ""
    return text[:1].upper() + text[1:] + "."


def rules_why(reasons: list[dict]) -> str:
    """Fixed sentence from the reason labels. No model."""
    parts = []
    for item in reasons or []:
        if not isinstance(item, dict):
            continue
        sentence = _sentence(str(item.get("label") or ""))
        if sentence:
            parts.append(sentence.rstrip("."))
    if not parts:
        return ""
    if len(parts) == 1:
        return parts[0] + "."
    tail = parts[-1][:1].lower() + parts[-1][1:]
    if len(parts) == 2:
        return f"{parts[0]}, and {tail}."
    return f"{'; '.join(parts[:-1])}; and {tail}."


def _prompt(reasons: list[dict]) -> str:
    lines = ["Checks, most important first:"]
    for item in reasons:
        lines.append(f"- {item.get('label') or item.get('code') or 'check'}")
    return "\n".join(lines)


def _allowed_numbers(reasons: list[dict]) -> set[str]:
    allowed: set[str] = set()
    text = " ".join(str(item.get("label") or "") for item in reasons)
    for found in _NUMBER_RE.findall(text):
        allowed.add(found)
        allowed.add(found.replace(",", ""))
    return allowed


def _clean_model_line(raw, reasons: list[dict]) -> str | None:
    if not isinstance(raw, dict) or not isinstance(raw.get("why"), str):
        return None
    text = " ".join(raw["why"].split())
    if not text or len(text) > MAX_WHY_CHARS:
        return None
    allowed = _allowed_numbers(reasons)
    for found in _NUMBER_RE.findall(text):
        if found not in allowed and found.replace(",", "") not in allowed:
            return None
    low = text.lower()
    for word, digits in _NUMBER_WORDS.items():
        if re.search(rf"\b{word}\b", low) and digits not in allowed:
            return None
    return _sentence(text)


def _cache_key(reasons: list[dict]) -> str:
    raw = json.dumps(
        [
            {"code": item.get("code"), "label": item.get("label")}
            for item in reasons
            if isinstance(item, dict)
        ],
        sort_keys=True,
        default=str,
    )
    return "triage_why:" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:40]


def _model_why(reasons: list[dict]) -> str | None:
    from utils.ai_text import AiUnavailable, generate_json

    if not (getattr(settings, "OPENAI_API_KEY", "") or "").strip():
        return None
    key = _cache_key(reasons)
    hit = cache.get(key)
    if isinstance(hit, dict) and hit.get("source") == "model" and hit.get("text"):
        return str(hit["text"])
    try:
        raw = generate_json(
            _SYSTEM,
            _prompt(reasons),
            schema=_SCHEMA,
            max_tokens=200,
            small=True,
            timeout=8,
        )
    except AiUnavailable:
        return None
    except Exception:
        logger.info("needs-eyes blurb failed", exc_info=True)
        return None
    text = _clean_model_line(raw, reasons)
    if not text:
        return None
    cache.set(key, {"text": text, "source": "model"}, _MODEL_CACHE_SECONDS)
    return text


def why_for_reasons(reasons: list[dict], *, allow_model: bool) -> tuple[str | None, str]:
    """``(sentence, source)``. ``source`` is ``rules`` or ``model``."""
    if not reasons:
        return None, ""
    if len(reasons) == 1 or not allow_model:
        text = rules_why(reasons)
        return (text or None), ("rules" if text else "")
    key = _cache_key(reasons)
    hit = cache.get(key)
    if isinstance(hit, dict) and isinstance(hit.get("text"), str) and hit["text"]:
        source = hit.get("source") if hit.get("source") in ("rules", "model") else "rules"
        return hit["text"], source
    drafted = _model_why(reasons)
    if drafted:
        return drafted, "model"
    text = rules_why(reasons)
    if text:
        cache.set(key, {"text": text, "source": "rules"}, _RULES_CACHE_SECONDS)
    return (text or None), ("rules" if text else "")


def attach_triage_why(by_id: dict) -> None:
    """Set ``why`` / ``why_source`` on each triage dict. Never raises.

    Clean rows stay blank. Model calls are capped so a long Needs-review
    page does not wait on one sentence per row; the rest get the fixed join.
    """
    try:
        _attach(by_id)
    except Exception:
        logger.warning("needs-eyes blurbs failed", exc_info=True)
        for row in by_id.values():
            if not isinstance(row, dict):
                continue
            if row.get("label") != LABEL_NEEDS_EYES:
                row["why"] = None
                row["why_source"] = ""
                continue
            if not row.get("why"):
                text = rules_why(row.get("reasons") or [])
                row["why"] = text or None
                row["why_source"] = "rules" if text else ""


def _rules_line(row: dict, reasons: list[dict]) -> None:
    text = rules_why(reasons)
    row["why"] = text or None
    row["why_source"] = "rules" if text else ""


def _attach(by_id: dict) -> None:
    pending: list[tuple[dict, list[dict]]] = []
    for row in by_id.values():
        if not isinstance(row, dict):
            continue
        reasons = [item for item in (row.get("reasons") or []) if isinstance(item, dict)]
        if row.get("label") != LABEL_NEEDS_EYES or not reasons:
            row["why"] = None
            row["why_source"] = ""
            continue
        cached = cache.get(_cache_key(reasons)) if len(reasons) > 1 else None
        if isinstance(cached, dict) and isinstance(cached.get("text"), str) and cached["text"]:
            source = cached.get("source") if cached.get("source") in ("rules", "model") else "rules"
            row["why"] = cached["text"]
            row["why_source"] = source
            continue
        if len(reasons) == 1 or len(pending) >= MAX_MODEL_CALLS:
            _rules_line(row, reasons)
            continue
        pending.append((row, reasons))

    if not pending:
        return
    if not (getattr(settings, "OPENAI_API_KEY", "") or "").strip():
        for row, reasons in pending:
            _rules_line(row, reasons)
        return

    # One round of model calls, not one per row. A miss falls back to the
    # fixed join so the list still renders.
    with ThreadPoolExecutor(max_workers=4) as pool:
        drafted = list(
            pool.map(lambda item: why_for_reasons(item[1], allow_model=True), pending)
        )
    for (row, _), (why, source) in zip(pending, drafted):
        row["why"] = why
        row["why_source"] = source or "rules"
