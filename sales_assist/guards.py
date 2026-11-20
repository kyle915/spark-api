"""Checks every generated draft passes before it is kept, plus the AI budget.

* :func:`numbers_grounded` — every number in the text appears in the facts
  (the same rule as ``recaps.weekly_narration.numbers_are_grounded``, with
  ``+`` and ``%`` normalized away, so "1,400+" in the text matches "1,400+"
  in the facts).
* :func:`leaks` — a prospect draft must never name another client or carry
  an exact figure tied to one. The names are every tenant's name (and its
  distinctive core, "Torch" from "Torch THC"); the figures are each
  comparable's raw, unrounded numbers.
* :func:`banned` — Ignite's standing locks: no invented rates or prices, no
  "42k" roster figure, no Girl Beer, no kratom claims.
* :class:`AiBudget` — counts calls and estimates spend; refuses calls once a
  run's cap is reached.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from decimal import Decimal

from django.conf import settings

_NUMBER = re.compile(r"\d[\d,]*(?:\.\d+)?")

# Words dropped from a tenant name to find its distinctive core.
_GENERIC = {
    "inc", "llc", "co", "company", "corp", "brand", "brands", "beverage", "beverages",
    "drinks", "drink", "foods", "food", "the", "thc", "cbd", "usa", "us", "brewing",
    "brewery", "spirits", "wines", "wine", "water", "energy", "productions", "group",
    "international", "demo", "test", "tea", "coffee", "farms", "bakery", "bread",
}

BANNED_PATTERNS = (
    (re.compile(r"\$\s?\d"), "a price or rate"),
    (re.compile(r"\bper (?:hour|hr|demo|event|day)\b", re.I), "a rate"),
    (re.compile(r"\bhourly rate\b|\bour rates?\b|\bpricing\b", re.I), "pricing"),
    (re.compile(r"\b42\s?k\b|\b42,000\b", re.I), "the wrong roster figure"),
    (re.compile(r"girl\s*beer", re.I), "a banned brand mention"),
    (re.compile(r"kratom", re.I), "a banned claim"),
)


def _norm(token: str) -> str:
    return token.replace(",", "").rstrip(".")


def numbers_in(text: str) -> list[str]:
    return [_norm(t) for t in _NUMBER.findall(text or "")]


def numbers_grounded(text: str, facts: dict) -> tuple[bool, list[str]]:
    """``(ok, ungrounded_tokens)``."""
    allowed = set(numbers_in(json.dumps(facts, ensure_ascii=False, default=str)))
    missing = [t for t in numbers_in(text) if t not in allowed]
    return (not missing), missing


def banned(text: str) -> list[str]:
    return [label for pattern, label in BANNED_PATTERNS if pattern.search(text or "")]


# Ordinary English words that are also part of some client's name. On their
# own they are not treated as the name ("fresh" bread is not Fresh Vintage).
_COMMON_WORDS = {
    "fresh", "stone", "house", "liquid", "death", "resort", "golden", "eagle", "total",
    "wireless", "girl", "beer", "free", "feel", "sweet", "pure", "good", "green",
    "natural", "original", "classic", "craft", "wild", "happy", "simple", "true", "real",
    "organic", "daily", "urban", "modern", "little", "best", "mountain", "vintage",
    "farm", "sparkling", "crisp", "bold", "bright", "smart", "light", "fire", "spark",
    "ignite", "brew", "long", "drink", "mark", "anthony", "entertainment", "marketing",
}
# Whole names that are also everyday phrases: matched only with the brand's
# own capitalization ("Feel Free" the brand vs "feel free to reach out").
_PHRASE_NAMES = {"feel free"}


def name_variants(name: str) -> set[str]:
    """Lower-cased phrases that identify the tenant: its full name, its core
    (generic words dropped: "torch" from "Torch THC") and its first
    distinctive word ("volt" from "Volt Energy Co")."""
    name = " ".join((name or "").split())
    out: set[str] = set()
    if len(name) >= 3:
        out.add(name.casefold())
    words = [w for w in re.split(r"[^A-Za-z0-9']+", name) if w]
    core = [w for w in words if w.casefold() not in _GENERIC]
    if core:
        joined = " ".join(core).casefold()
        if len(joined) >= 4 and (len(core) > 1 or joined not in _COMMON_WORDS):
            out.add(joined)
        first = core[0].casefold()
        if len(first) >= 4 and first not in _COMMON_WORDS:
            out.add(first)
    return out


def _contains_phrase(text: str, phrase: str, *, original: str | None = None) -> bool:
    if original is not None:
        return re.search(r"(?<![A-Za-z0-9])" + re.escape(original) + r"(?![A-Za-z0-9])", text) is not None
    return re.search(r"(?<![A-Za-z0-9])" + re.escape(phrase) + r"(?![A-Za-z0-9])", text, re.I) is not None


def mentions_name(text: str, name: str) -> bool:
    for variant in name_variants(name):
        if variant in _PHRASE_NAMES:
            if _contains_phrase(text or "", variant, original=" ".join(name.split())):
                return True
        elif _contains_phrase(text or "", variant):
            return True
    return False


def leaks(text: str, *, forbidden_names: list[str], forbidden_numbers: list[int]) -> list[str]:
    """What in ``text`` would identify another client: names and exact figures."""
    found: list[str] = []
    for name in forbidden_names:
        if mentions_name(text, name):
            found.append(f"name:{name}")
    present = set(numbers_in(text))
    for value in forbidden_numbers:
        if int(value) >= 10 and str(int(value)) in present:
            found.append(f"number:{int(value)}")
    return found


def rounded(value: int) -> str:
    """A figure safe to show a prospect: floored to two significant digits
    with a "+" ("1,445" -> "1,400+", "87" -> "80+"). Under 20 -> ""."""
    value = int(value or 0)
    if value < 20:
        return ""
    digits = len(str(value))
    step = 10 ** max(digits - 2, 1)
    floored = (value // step) * step
    return f"{floored:,}+"


# ── AI budget ───────────────────────────────────────────────────────────

# Hidden reasoning tokens a reasoning model may spend per call, added to the
# estimate (they are billed as output).
_REASONING_ALLOWANCE = 1500


class BudgetExhausted(Exception):
    pass


@dataclass
class AiBudget:
    max_calls: int = 0
    max_usd: float = 0.0
    calls: int = 0
    usd: float = 0.0
    log: list = field(default_factory=list)

    @classmethod
    def from_settings(cls) -> "AiBudget":
        return cls(
            max_calls=int(getattr(settings, "SALES_ASSIST_MAX_AI_CALLS_PER_RUN", 40)),
            max_usd=float(getattr(settings, "SALES_ASSIST_MAX_USD_PER_RUN", 1.0)),
        )

    def check(self) -> None:
        if self.max_calls and self.calls >= self.max_calls:
            raise BudgetExhausted("AI call cap for this run reached")
        if self.max_usd and self.usd >= self.max_usd:
            raise BudgetExhausted("AI spend cap for this run reached")

    def record(self, prompt_chars: int, output_chars: int) -> float:
        tin = prompt_chars / 4
        tout = output_chars / 4 + _REASONING_ALLOWANCE
        cost = (
            tin * float(getattr(settings, "SALES_ASSIST_USD_PER_M_INPUT", 2.5))
            + tout * float(getattr(settings, "SALES_ASSIST_USD_PER_M_OUTPUT", 10.0))
        ) / 1_000_000
        self.calls += 1
        self.usd += cost
        return cost

    def call(self, fn, system: str, user: str, **kwargs):
        """Run one ``utils.ai_text`` helper under the budget."""
        self.check()
        result = fn(system, user, **kwargs)
        out = result if isinstance(result, str) else json.dumps(result or {})
        cost = self.record(len(system) + len(user), len(out))
        self.log.append(round(cost, 5))
        return result

    @property
    def usd_decimal(self) -> Decimal:
        return Decimal(str(round(self.usd, 4)))
