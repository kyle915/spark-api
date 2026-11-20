"""Turn what an ambassador said about a shift into answers for the recap form.

The ambassador talks (or types) about the shift. This module asks the model
to map those words onto the fields of the ambassador's own recap template:
counts, dropdowns, multi-selects (including the product list), yes/no
questions and text boxes. The result is a set of *proposals*. Nothing is
saved here, no recap is created, ``approved`` is never set, and Submit on the
page is still the only way to file. The page writes a proposal into a box
only when the box is empty, and the ambassador can edit every one.

Rules, all enforced in code after the model answers:

* only fields of this template (and its product list) can come back; the
  model answers with a reference from the list it was handed and anything
  else is dropped;
* photo, file, date, mileage, phone and e-mail fields are never sent and
  never filled;
* a count must parse as a plain, plausible number (money fields may carry
  cents). A range, a guess or words that are not a number are dropped;
* a dropdown answer must be one of that field's options, and every
  multi-select choice must be one of its options. Case and spacing are
  forgiven; anything else is dropped;
* every answer carries the ambassador's own words that support it. When
  those words are not in the transcript the answer is dropped, so a field
  the ambassador did not talk about stays blank;
* answers the model marks low confidence are dropped;
* the same reference answered twice with different values is dropped.

When ``OPENAI_API_KEY`` is unset the result is ``available=False`` with
``Voice draft unavailable``; the page keeps the transcript.
"""

from __future__ import annotations

import logging
import re
import unicodedata

from django.conf import settings

from utils.ai_text import AiUnavailable, generate_json

logger = logging.getLogger(__name__)

VOICE_DRAFT_UNAVAILABLE = "Voice draft unavailable"

MAX_TRANSCRIPT_CHARS = 8000
MAX_TEXT_CHARS = 2000
MAX_FIELDS = 80
MAX_PRODUCTS = 80
MAX_OPTIONS = 120
_MAX_LABEL_CHARS = 160
# A count above this on one shift is a mishearing, not a count.
MAX_PLAUSIBLE_COUNT = 100_000
# Per HTTP attempt. Someone is waiting on the page.
_MODEL_TIMEOUT_SECONDS = 25.0
_MODEL_MAX_TOKENS = 3000

# Field kinds the draft can write.
KIND_TEXT = "text"
KIND_NUMBER = "number"
KIND_SELECT = "select"
KIND_MULTI = "multiselect"
KIND_YESNO = "yesno"

_CONFIDENCE = ("high", "medium", "low")
_KEEP_CONFIDENCE = {"high", "medium"}

_SKIP_TYPE = re.compile(
    r"photo|image|file|upload|signature|video|date|time|geo|location", re.I
)
_NUMBER_TYPE = re.compile(r"number|numeric|int|decimal|float|currency|money", re.I)
_DECIMAL_TYPE = re.compile(r"decimal|float|currency|money", re.I)
_MULTI_TYPE = re.compile(r"multi|checkboxes", re.I)
_SELECT_TYPE = re.compile(r"select|dropdown|radio|choice", re.I)
_YESNO_TYPE = re.compile(r"bool|yes.?no|checkbox|toggle|switch", re.I)
_TEXT_TYPE = re.compile(r"text|string|area|paragraph|long|input|short", re.I)
# Fields filled some other way, or that hold someone's contact details.
_SKIP_NAME = re.compile(
    r"mileage|\bmiles?\b|odometer|phone|e-?mail|drop[- ]?off locations?|"
    r"signature|\bphoto|\bpicture",
    re.I,
)
_MONEY_NAME = re.compile(r"spend|spent|price|cost|\$|amount|total\s+sales|revenue", re.I)
_WHOLE_RE = re.compile(r"\d{1,6}")
_MONEY_RE = re.compile(r"\d{1,6}(\.\d{1,2})?")
_WORD_RE = re.compile(r"[a-z0-9]+")
_YES = {"yes", "y", "true"}
_NO = {"no", "n", "false"}

VOICE_SYSTEM = (
    "You fill in a brand ambassador's shift recap form from what they said "
    "about the shift. You are given the form's fields, each with a reference, "
    "a kind, and (for choice fields) the only allowed options. Return an "
    "answer ONLY for a field the ambassador actually talked about. Leave out "
    "every field they did not mention; a blank field is always better than a "
    "guess. Rules by kind: number: digits only, and only when the ambassador "
    "stated that count or amount. Spoken numbers become digits (\"about "
    "forty\" is 40 when it is clearly that field's count). Never add numbers "
    "together, never estimate, never answer a range or a vague amount "
    "(\"a bunch\", \"twenty or thirty\"). select: exactly one of the listed "
    "options, copied exactly. multiselect: only listed options, copied "
    "exactly. yesno: Yes or No. text: the ambassador's own words for that "
    "question, lightly cleaned up (fix filler words and grammar, keep their "
    "meaning, do not add facts). For product counts, match the product the "
    "ambassador named to the listed product names. For every answer, put the "
    "ambassador's exact words that support it in quote, copied from the "
    "transcript. Set confidence to low when you are unsure the ambassador "
    "meant that field. Do not name, describe or identify any consumer."
)


def _label(text: object) -> str:
    return " ".join(str(text or "").split())[:_MAX_LABEL_CHARS]


def _options(raw: object) -> list[str]:
    if not isinstance(raw, list):
        return []
    out: list[str] = []
    seen: set[str] = set()
    for item in raw:
        if not isinstance(item, str):
            continue
        option = item.strip()
        key = option.casefold()
        if not option or key in seen:
            continue
        seen.add(key)
        out.append(option)
        if len(out) >= MAX_OPTIONS:
            break
    return out


def field_kind(name: str, field_type: str, options: list[str]) -> str | None:
    """Which kind of answer the draft may give this field, or None to skip it."""
    kind = (field_type or "").strip().lower()
    if _SKIP_TYPE.search(kind) or _SKIP_NAME.search(name or ""):
        return None
    if _MULTI_TYPE.search(kind):
        return KIND_MULTI if options else None
    if _SELECT_TYPE.search(kind):
        return KIND_SELECT if options else None
    if _YESNO_TYPE.search(kind):
        return KIND_YESNO
    if _NUMBER_TYPE.search(kind):
        return KIND_NUMBER
    if not kind or _TEXT_TYPE.search(kind):
        return KIND_TEXT
    return None


def voice_targets(fields: list[dict], products: list[dict] | None = None) -> list[dict]:
    """The lines the model may answer: ``[{ref, kind, id, label, options,
    money, product}]``. Fields first in form order, then products."""
    targets: list[dict] = []
    for field in fields or []:
        if not isinstance(field, dict):
            continue
        field_id = str(field.get("id") or "").strip()
        name = _label(field.get("name"))
        if not field_id or not name:
            continue
        options = _options(field.get("options"))
        kind = field_kind(name, str(field.get("type") or ""), options)
        if kind is None:
            continue
        if len(targets) >= MAX_FIELDS:
            break
        ftype = str(field.get("type") or "")
        targets.append(
            {
                "ref": f"f{len(targets) + 1}",
                "kind": kind,
                "id": field_id,
                "label": name,
                "options": options if kind in (KIND_SELECT, KIND_MULTI) else [],
                "money": kind == KIND_NUMBER
                and bool(_MONEY_NAME.search(name) or _DECIMAL_TYPE.search(ftype)),
                "product": False,
            }
        )
    count = 0
    for product in products or []:
        if not isinstance(product, dict) or count >= MAX_PRODUCTS:
            continue
        product_id = str(product.get("id") or "").strip()
        name = _label(product.get("name"))
        if not product_id or not name:
            continue
        count += 1
        targets.append(
            {
                "ref": f"p{count}",
                "kind": KIND_NUMBER,
                "id": product_id,
                "label": name,
                "options": [],
                "money": False,
                "product": True,
            }
        )
    return targets


def _schema(refs: list[str]) -> dict:
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["answers"],
        "properties": {
            "answers": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["ref", "value", "choices", "quote", "confidence"],
                    "properties": {
                        "ref": {"type": "string", "enum": refs},
                        # number / select / yesno / text answers.
                        "value": {"type": "string"},
                        # multiselect answers; empty for every other kind.
                        "choices": {"type": "array", "items": {"type": "string"}},
                        "quote": {"type": "string"},
                        "confidence": {"type": "string", "enum": list(_CONFIDENCE)},
                    },
                },
            }
        },
    }


def compose_prompt(targets: list[dict], transcript: str) -> str:
    lines = ["Form fields:"]
    for target in targets:
        if target["product"]:
            lines.append(
                f"- [{target['ref']}] number: how many of this product were "
                f"sampled or handed out: {target['label']}"
            )
            continue
        line = f"- [{target['ref']}] {target['kind']}: {target['label']}"
        if target["kind"] == KIND_NUMBER and target["money"]:
            line += " (dollars, cents allowed)"
        if target["options"]:
            line += " | options: " + " ; ".join(target["options"])
        lines.append(line)
    lines.append("")
    lines.append("What the ambassador said:")
    lines.append(transcript[:MAX_TRANSCRIPT_CHARS])
    return "\n".join(lines)


def parse_number(raw: object, *, money: bool = False) -> str | None:
    """A spoken-then-written number → the string for the box, or None."""
    text = str(raw if raw is not None else "").replace(",", "").replace("$", "").strip()
    if not (_MONEY_RE if money else _WHOLE_RE).fullmatch(text):
        return None
    try:
        value = float(text)
    except ValueError:
        return None
    if value < 0 or value > MAX_PLAUSIBLE_COUNT:
        return None
    if money and "." in text:
        return f"{value:.2f}"
    return str(int(value))


def _norm(text: str) -> str:
    folded = unicodedata.normalize("NFKC", text or "").casefold()
    return " ".join(folded.split())


def match_option(raw: object, options: list[str]) -> str | None:
    """The canonical option the answer names, or None. Exact after case and
    spacing; nothing fuzzier."""
    if not isinstance(raw, str):
        return None
    wanted = _norm(raw)
    if not wanted:
        return None
    for option in options:
        if _norm(option) == wanted:
            return option
    return None


def _words(text: str) -> list[str]:
    return _WORD_RE.findall(_norm(text))


def quote_supported(quote: object, transcript: str) -> bool:
    """True when the quoted words are really in the transcript.

    Most of the quote's words must appear in the transcript (the model may
    trim filler or fix a word). An empty quote is never support.
    """
    if not isinstance(quote, str):
        return False
    said = set(_words(transcript))
    words = _words(quote)
    if not words or not said:
        return False
    hits = sum(1 for word in words if word in said)
    return hits / len(words) >= 0.6


def _clean_text(raw: object) -> str | None:
    if not isinstance(raw, str):
        return None
    text = raw.strip()
    if not text:
        return None
    return text[:MAX_TEXT_CHARS]


def interpret_answers(parsed: object, targets: list[dict], transcript: str) -> dict:
    """Model output → validated proposals. Pure."""
    empty = {"values": [], "productSamples": []}
    if not isinstance(parsed, dict):
        return empty
    answers = parsed.get("answers")
    if not isinstance(answers, list):
        return empty
    by_ref = {t["ref"]: t for t in targets}
    kept: dict[str, dict] = {}
    dropped: set[str] = set()
    for answer in answers:
        if not isinstance(answer, dict):
            continue
        ref = str(answer.get("ref") or "")
        target = by_ref.get(ref)
        if target is None or ref in dropped:
            continue
        confidence = str(answer.get("confidence") or "").lower()
        if confidence not in _KEEP_CONFIDENCE:
            continue
        if not quote_supported(answer.get("quote"), transcript):
            continue
        kind = target["kind"]
        value: str | list[str] | None
        if kind == KIND_NUMBER:
            value = parse_number(answer.get("value"), money=target["money"])
        elif kind == KIND_SELECT:
            value = match_option(answer.get("value"), target["options"])
        elif kind == KIND_MULTI:
            raw = answer.get("choices")
            if not isinstance(raw, list) or not raw:
                raw = [answer.get("value")]
            picked: list[str] = []
            for item in raw:
                option = match_option(item, target["options"])
                if option is not None and option not in picked:
                    picked.append(option)
            value = picked or None
        elif kind == KIND_YESNO:
            word = _norm(str(answer.get("value") or ""))
            value = "Yes" if word in _YES else "No" if word in _NO else None
        else:
            value = _clean_text(answer.get("value"))
        if value is None:
            continue
        if ref in kept and kept[ref]["value"] != value:
            # Two different answers for one field: neither is trustworthy.
            kept.pop(ref)
            dropped.add(ref)
            continue
        kept[ref] = {"value": value, "confidence": confidence}

    values = []
    samples = []
    for target in targets:
        hit = kept.get(target["ref"])
        if hit is None:
            continue
        if target["product"]:
            samples.append(
                {
                    "productId": target["id"],
                    "value": hit["value"],
                    "confidence": hit["confidence"],
                }
            )
        else:
            values.append(
                {
                    "fieldId": target["id"],
                    "kind": target["kind"],
                    "value": hit["value"],
                    "confidence": hit["confidence"],
                }
            )
    return {"values": values, "productSamples": samples}


def _result(values: list[dict], samples: list[dict]) -> dict:
    # ``fills`` keeps pages that predate full-form drafts working: they only
    # read text answers from it.
    fills = [
        {"fieldId": row["fieldId"], "text": row["value"]}
        for row in values
        if row["kind"] == KIND_TEXT
    ]
    return {
        "available": True,
        "reason": "",
        "values": values,
        "productSamples": samples,
        "fills": fills,
        "filledCount": len(values) + len(samples),
    }


def _unavailable() -> dict:
    return {
        "available": False,
        "reason": VOICE_DRAFT_UNAVAILABLE,
        "values": [],
        "productSamples": [],
        "fills": [],
        "filledCount": 0,
    }


def transcription_hint(fields: list[dict], products: list[dict] | None = None) -> str:
    """Product and option names, so speech-to-text spells them the brand's way."""
    names: list[str] = []
    for target in voice_targets(fields, products):
        if target["product"]:
            names.append(target["label"])
        names.extend(target["options"])
    seen: set[str] = set()
    out: list[str] = []
    for name in names:
        key = name.casefold()
        if key in seen or key in _YES or key in _NO:
            continue
        seen.add(key)
        out.append(name)
    return ", ".join(out)[:800]


def structure_transcript(
    transcript: str, fields: list[dict], products: list[dict] | None = None
) -> dict:
    """Map a transcript onto the recap template. Never raises. Never files.

    Returns ``{available, reason, values: [{fieldId, kind, value,
    confidence}], productSamples: [{productId, value, confidence}], fills,
    filledCount}``. ``value`` is a list for multi-select fields and a string
    otherwise.
    """
    prose = (transcript or "").strip()[:MAX_TRANSCRIPT_CHARS]
    if not prose:
        return _result([], [])
    targets = voice_targets(fields, products)
    if not targets:
        return _result([], [])
    if not (getattr(settings, "OPENAI_API_KEY", "") or "").strip():
        return _unavailable()
    try:
        parsed = generate_json(
            VOICE_SYSTEM,
            compose_prompt(targets, prose),
            schema=_schema([t["ref"] for t in targets]),
            max_tokens=_MODEL_MAX_TOKENS,
            timeout=_MODEL_TIMEOUT_SECONDS,
        )
    except AiUnavailable:
        return _unavailable()
    except Exception:  # noqa: BLE001 — advisory; the form works without it
        logger.exception("voice draft failed")
        return _unavailable()
    if not isinstance(parsed, dict):
        return _unavailable()
    out = interpret_answers(parsed, targets, prose)
    return _result(out["values"], out["productSamples"])
