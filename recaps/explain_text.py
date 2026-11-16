"""Sentences for the dashboards: Spark does the sums, the model only words them.

Every caller here hands over two things per sentence: a ``template`` (a
finished, plain sentence built in Python) and ``facts`` (the lines of
numbers that sentence was built from). The model is asked to reword the
template and nothing else. Its text is kept only when

* every number in it is a number that appears in the facts or the template
  ("1,284" and "1284" are the same number; "18%" is 18), and
* it uses no number word or ratio word ("twelve", "doubled", "half") that
  would be a figure nobody supplied, and
* it does not say a measure went the other way.

Otherwise the template is used. The template is also what a reader gets
when the model is unavailable, so every feature works without it.

Text is kept in :class:`recaps.models.DashboardText` per (tenant, kind,
scope) with a hash of its inputs, so a page load reads a row instead of
calling the model. The model is asked again only when the numbers change,
or, for a row the template wrote, after :data:`RETRY_AFTER`.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from dataclasses import dataclass, field
from datetime import timedelta

from django.utils import timezone

logger = logging.getLogger(__name__)

# Bump when a prompt or a rule here changes, so old rows are rewritten.
PROMPT_VERSION = "1"

MODEL_TIMEOUT_SECONDS = 12.0
RETRY_AFTER = timedelta(hours=6)
MAX_SENTENCE_CHARS = 400

SOURCE_MODEL = "model"
SOURCE_TEMPLATE = "template"

_NUMBER_RE = re.compile(r"(?<![\w.])\d[\d,]*(?:\.\d+)?")

_NUMBER_WORDS = {
    "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7,
    "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12,
    "thirteen": 13, "fourteen": 14, "fifteen": 15, "sixteen": 16,
    "seventeen": 17, "eighteen": 18, "nineteen": 19, "twenty": 20,
    "thirty": 30, "forty": 40, "fifty": 50, "sixty": 60, "seventy": 70,
    "eighty": 80, "ninety": 90,
}  # fmt: skip
_NUMBER_WORD_RE = re.compile(
    r"\b(" + "|".join(_NUMBER_WORDS) + r")\b", re.IGNORECASE
)
# A ratio or a scale stated in words is a computed figure.
_RATIO_WORD_RE = re.compile(
    r"\b(double[ds]?|doubling|triple[ds]?|tripling|quadrupl\w*|half|halved|"
    r"halving|twice|thrice|third|thirds|quarter|quarters|tenfold|\w+fold|"
    r"dozens?|hundreds?|thousands?|millions?|majority|minority)\b",
    re.IGNORECASE,
)

_UP_RE = re.compile(
    r"\b(up|rose|risen|rise|rising|increase[ds]?|increasing|grew|grown|"
    r"growth|higher|climb(?:ed|ing)?|gain(?:ed|s)?|jump(?:ed)?|more)\b",
    re.IGNORECASE,
)
_DOWN_RE = re.compile(
    r"\b(down|fell|fallen|fall|falling|drop(?:ped|s)?|decline[ds]?|"
    r"declining|decrease[ds]?|lower|slid|slipped|shrank|fewer|less)\b",
    re.IGNORECASE,
)


def numbers_in(text: str) -> list[float]:
    """Every number written in ``text`` with digits, signs and % dropped."""
    out: list[float] = []
    for match in _NUMBER_RE.findall(text or ""):
        try:
            out.append(float(match.replace(",", "")))
        except ValueError:
            continue
    return out


def _same(a: float, b: float) -> bool:
    return abs(a - b) < 1e-9


def unsupported_numbers(text: str, supplied: str) -> list[str]:
    """What ``text`` states that ``supplied`` does not: numbers in digits,
    numbers in words, and ratio words. Empty means every figure checks out."""
    allowed = numbers_in(supplied)
    bad: list[str] = []
    for value in numbers_in(text):
        if not any(_same(value, ok) for ok in allowed):
            bad.append(f"{value:g}")
    supplied_words = {w.lower() for w in _NUMBER_WORD_RE.findall(supplied or "")}
    for word in _NUMBER_WORD_RE.findall(text or ""):
        value = float(_NUMBER_WORDS[word.lower()])
        if word.lower() in supplied_words:
            continue
        if not any(_same(value, ok) for ok in allowed):
            bad.append(word.lower())
    supplied_ratio = {w.lower() for w in _RATIO_WORD_RE.findall(supplied or "")}
    for word in _RATIO_WORD_RE.findall(text or ""):
        if word.lower() not in supplied_ratio:
            bad.append(word.lower())
    return bad


def wrong_direction(text: str, directions: dict[str, str]) -> bool:
    """True when a sentence about one measure says it moved the other way.

    ``directions`` maps a word that names a measure ("consumers",
    "conversion") to "up" or "down". A sentence naming two measures is not
    judged: it may rightly hold both words.
    """
    if not directions:
        return False
    for sentence in re.split(r"(?<=[.!?;])\s+", text or ""):
        lowered = sentence.lower()
        named = [word for word in directions if word.lower() in lowered]
        if len(named) != 1:
            continue
        want = directions[named[0]]
        says_up = bool(_UP_RE.search(sentence))
        says_down = bool(_DOWN_RE.search(sentence))
        if want == "up" and says_down and not says_up:
            return True
        if want == "down" and says_up and not says_down:
            return True
    return False


@dataclass(frozen=True)
class Draft:
    """One sentence to word: the finished template and the facts behind it."""

    key: str
    template: str
    facts: str
    directions: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class Written:
    key: str
    text: str
    source: str


def accept(text: str | None, draft: Draft) -> bool:
    """Whether the model's ``text`` may replace ``draft.template``."""
    cleaned = " ".join(str(text or "").split())
    if not cleaned or len(cleaned) > MAX_SENTENCE_CHARS:
        return False
    supplied = draft.facts + "\n" + draft.template
    if unsupported_numbers(cleaned, supplied):
        return False
    if wrong_direction(cleaned, draft.directions):
        return False
    return True


_SYSTEM = (
    "You reword short dashboard sentences for a field-marketing report. "
    "For each item you are given FACTS and a DRAFT sentence that is already "
    "correct. Return the draft reworded so it reads naturally, in plain "
    "sentence case, at most two sentences per item. Rules: use only the "
    "facts given; write every number in digits exactly as it appears in the "
    "facts; never add a number, a percentage, a ratio, a cause, a forecast "
    "or advice; never name a person; keep every store, state and product "
    "name exactly as written; no exclamation marks, no emoji, no marketing "
    "language. If you cannot improve the draft, return it unchanged."
)

_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["items"],
    "properties": {
        "items": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["key", "text"],
                "properties": {
                    "key": {"type": "string"},
                    "text": {"type": "string"},
                },
            },
        }
    },
}


def build_prompt(drafts: list[Draft]) -> str:
    blocks = []
    for draft in drafts:
        blocks.append(
            f"ITEM {draft.key}\nFACTS:\n{draft.facts}\nDRAFT: {draft.template}"
        )
    return "\n\n".join(blocks)


def inputs_hash(*parts) -> str:
    raw = json.dumps([PROMPT_VERSION, *parts], sort_keys=True, default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _ask_model(drafts: list[Draft]) -> dict[str, str]:
    """``{key: text}`` from the model; empty on any failure."""
    from utils.ai_text import AiUnavailable, generate_json

    try:
        parsed = generate_json(
            _SYSTEM,
            build_prompt(drafts),
            schema=_SCHEMA,
            max_tokens=1500,
            timeout=MODEL_TIMEOUT_SECONDS,
        )
    except AiUnavailable:
        return {}
    except Exception:  # noqa: BLE001 — a sentence is never worth a 500
        logger.exception("dashboard text: model call failed")
        return {}
    items = parsed.get("items") if isinstance(parsed, dict) else None
    if not isinstance(items, list):
        return {}
    out: dict[str, str] = {}
    for item in items:
        if isinstance(item, dict) and isinstance(item.get("text"), str):
            out[str(item.get("key"))] = item["text"]
    return out


def read_cached(tenant_id: int, kind: str, scope_key: str, digest: str):
    """The stored payload for these exact inputs, or None. A row the
    template wrote goes stale after :data:`RETRY_AFTER` so the model gets
    another try; a row the model wrote is good until the inputs change."""
    from recaps.models import DashboardText

    row = DashboardText.objects.filter(
        tenant_id=tenant_id, kind=kind, scope_key=scope_key[:200]
    ).first()
    if row is None or row.inputs_hash != digest:
        return None
    if row.source != SOURCE_MODEL and row.updated_at < timezone.now() - RETRY_AFTER:
        return None
    return row.payload


def store(
    tenant_id: int,
    kind: str,
    scope_key: str,
    digest: str,
    payload: dict,
    source: str,
    *,
    user_id: int | None = None,
) -> None:
    from recaps.models import DashboardText

    try:
        DashboardText.objects.update_or_create(
            tenant_id=tenant_id,
            kind=kind,
            scope_key=scope_key[:200],
            defaults={
                "inputs_hash": digest,
                "payload": payload,
                "source": source,
                "created_by_id": user_id,
            },
        )
    except Exception:  # noqa: BLE001 — failing to cache must not fail the page
        logger.exception("dashboard text: could not store %s", kind)


def write(
    tenant_id: int, kind: str, scope_key: str, drafts: list[Draft]
) -> list[Written]:
    """Word ``drafts``, from the cache when the inputs have not changed.

    One model call for the whole list, at most. Each sentence is checked on
    its own: one that fails falls back to its template without taking the
    others with it.
    """
    if not drafts:
        return []
    digest = inputs_hash([(d.key, d.template, d.facts) for d in drafts])
    cached = read_cached(tenant_id, kind, scope_key, digest)
    if isinstance(cached, dict):
        texts = cached.get("texts") or {}
        sources = cached.get("sources") or {}
        if all(d.key in texts for d in drafts):
            return [
                Written(d.key, texts[d.key], sources.get(d.key, SOURCE_TEMPLATE))
                for d in drafts
            ]

    answers = _ask_model(drafts)
    out: list[Written] = []
    for draft in drafts:
        text = answers.get(draft.key)
        if accept(text, draft):
            out.append(Written(draft.key, " ".join(text.split()), SOURCE_MODEL))
        else:
            out.append(Written(draft.key, draft.template, SOURCE_TEMPLATE))
    # The row counts as the model's only if the model answered at all: a
    # row the template wrote alone is retried after RETRY_AFTER.
    row_source = SOURCE_MODEL if answers else SOURCE_TEMPLATE
    store(
        tenant_id,
        kind,
        scope_key,
        digest,
        {
            "texts": {w.key: w.text for w in out},
            "sources": {w.key: w.source for w in out},
        },
        row_source,
    )
    return out


def window_key(start, end, today=None) -> str:
    """A name for a date window that does not change every day for the
    ranges a dashboard rolls forward ("31d", "ytd"), so the table stays
    small. Fixed dates keep their dates."""
    if start is None or end is None:
        return "all"
    if today is None:
        from events.live_board import _ops_today

        # The browser sends the UTC date; the ops day can be one behind it.
        rolling = end in {timezone.now().date(), _ops_today()}
    else:
        rolling = end == today
    if rolling:
        if start.month == 1 and start.day == 1 and start.year == end.year:
            return "ytd"
        return f"{(end - start).days + 1}d"
    return f"{start.isoformat()}:{end.isoformat()}"


def n(value) -> str:
    """1284 → "1,284"."""
    return f"{int(value):,}"


def pct(value: float) -> str:
    """14.0 → "14", 14.2 → "14.2"."""
    return f"{value:g}"


def plural(count: int, noun: str) -> str:
    return f"{n(count)} {noun}" if count == 1 else f"{n(count)} {noun}s"
