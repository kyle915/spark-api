"""Optional one-line plain-English reason per flag, written by OpenAI.

The rules decide everything. This only rewords a flag's computed facts into
one sentence for a busy reader, and only when staff ask for it. The model
sees the rule title and its facts (hours, minutes, miles) — never names,
emails, addresses or coordinates. Any failure (no key, timeout, bad reply)
returns None and the page shows the rule's own summary instead.
"""

from __future__ import annotations

import hashlib
import json
from concurrent.futures import ThreadPoolExecutor

from django.core.cache import cache

SYSTEM = (
    "You rewrite one payroll hours-check flag as ONE short plain-English "
    "sentence (max 30 words) for an operations coordinator. Use ONLY the "
    "facts given. Do not add numbers, guesses, accusations or advice. Do not "
    "say the worker did anything wrong; describe what the records show."
)
_CACHE_SECONDS = 30 * 24 * 3600
MAX_FLAGS = 40


def _prompt(flag) -> str:
    facts = "\n".join(f"- {k}: {v}" for k, v in flag.facts)
    return f"Check: {flag.title}\nFacts:\n{facts}"


def _cache_key(flag) -> str:
    raw = json.dumps([flag.code, flag.facts], sort_keys=True, default=str)
    return "hours_check:reason:" + hashlib.sha256(raw.encode()).hexdigest()[:40]


def _one(flag) -> str | None:
    from utils.ai_text import AiUnavailable, generate_summary

    key = _cache_key(flag)
    hit = cache.get(key)
    if hit:
        return hit
    try:
        text = generate_summary(SYSTEM, _prompt(flag), max_tokens=120, small=True)
    except AiUnavailable:
        return None
    except Exception:  # noqa: BLE001
        return None
    text = " ".join((text or "").split())
    if not text:
        return None
    text = text[:300]
    cache.set(key, text, _CACHE_SECONDS)
    return text


def explain(flags: list) -> dict[int, str]:
    """{id(flag): sentence} for up to MAX_FLAGS flags (highest severity
    first, which is the order callers pass them in)."""
    from django.conf import settings

    if not (getattr(settings, "OPENAI_API_KEY", "") or "").strip():
        return {}
    todo = flags[:MAX_FLAGS]
    out: dict[int, str] = {}
    with ThreadPoolExecutor(max_workers=6) as pool:
        for flag, text in zip(todo, pool.map(_one, todo)):
            if text:
                out[id(flag)] = text
    return out
