"""Finance exception dispute explainers — flag_explanation pattern, no new models.

Each open FinanceException already has a rules ``explanation`` with grounded
dollars. This module packages a **dispute pack** for reviewers:

* ``summary`` — one plain line (rules always; optional model polish when OCR
  / multiple fact sources exist, same grounding rules as
  :mod:`recaps.flag_explanation`)
* ``facts`` — dollar / merchant / type lines from stored rows only
* ``suggested_next_step`` — fixed per exception type (never model-written)
* ``source`` — ``rules`` or ``model``

Never invents amounts. Empty OpenAI key → rules pack only.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from datetime import datetime, timedelta
from typing import Any

from django.core.cache import cache
from django.utils import timezone

logger = logging.getLogger(__name__)

MAX_SUMMARY_CHARS = 280
CACHE_SECONDS = 30 * 24 * 3600
RETRY_AFTER = timedelta(hours=6)

_NUMBER_RE = re.compile(r"\d+(?:[.,]\d+)*")
_MONEY_RE = re.compile(r"\$([\d,]+(?:\.\d{1,2})?)")

_NEXT_STEP: dict[str, str] = {
    "missing_rate_or_amount": "Enter the contracted rate or dollar amount, then re-run cost.",
    "missing_proof": "Attach an itemized receipt to the cost line before resolving.",
    "unitemized_proof": "Replace the summary slip with an itemized receipt, or grant a documented exception.",
    "unmatched_transaction": "Match this receipt or invoice line to the correct visit cost line.",
    "suspected_duplicate": "Confirm whether one charge is a duplicate; void or keep with an audit note.",
    "allocation_discrepancy": "Reconcile the shared cost allocation against the visit share.",
}

_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["why"],
    "properties": {"why": {"type": "string"}},
}

_SYSTEM = (
    "You help a finance reviewer at a field-marketing agency write a short "
    "dispute note. You are given an exception type, its rules explanation, "
    "and OCR / ledger facts with numbers. Write ONE sentence (or two short "
    "ones) that restates why the exception is open for a dispute or "
    "clarification email. Plain words, sentence case, no lists, no blame, "
    "no advice. Use only the numbers you are given, exactly as written; do "
    "not add, round, or compute any. Do not invent merchants or dates. At "
    "most 40 words. Do not say what to do next."
)


def _sentence(text: str) -> str:
    text = " ".join((text or "").split()).strip().rstrip(".")
    return (text[:1].upper() + text[1:] + ".") if text else ""


def _allowed_numbers(*parts: Any) -> set[str]:
    allowed: set[str] = set()
    blob = " ".join(json.dumps(p, default=str) for p in parts)
    for found in _NUMBER_RE.findall(blob):
        allowed.add(found)
        allowed.add(found.replace(",", ""))
    return allowed


def _clean_model_line(raw: Any, allowed: set[str]) -> str | None:
    if not isinstance(raw, dict) or not isinstance(raw.get("why"), str):
        return None
    text = " ".join(raw["why"].split())
    if not text or len(text) > MAX_SUMMARY_CHARS:
        return None
    for found in _NUMBER_RE.findall(text):
        if found not in allowed and found.replace(",", "") not in allowed:
            return None
    return _sentence(text)


def suggested_next_step(exception_type: str) -> str:
    return _NEXT_STEP.get(
        exception_type,
        "Review the evidence, add an audit note, then resolve or request clarification.",
    )


def gather_dispute_facts(exc) -> list[dict[str, Any]]:
    """Structured facts for one exception. Codes + numbers only where possible."""
    from recaps.models import ReceiptReading

    facts: list[dict[str, Any]] = [
        {"code": "exception_type", "value": exc.exception_type},
        {"code": "severity", "value": exc.severity},
        {"code": "status", "value": exc.status},
    ]
    if exc.affected_amount is not None:
        facts.append({"code": "affected_amount", "value": str(exc.affected_amount)})
    if exc.source_label:
        facts.append({"code": "source_label", "value": exc.source_label[:120]})
    if exc.canonical_visit_id:
        visit = exc.canonical_visit
        if visit is not None:
            if visit.store_label:
                facts.append({"code": "store_label", "value": visit.store_label[:120]})
            if visit.visit_date:
                facts.append(
                    {"code": "visit_date", "value": visit.visit_date.isoformat()}
                )

    # OCR receipt facts when the exception points at a recap file reading.
    reading = None
    if exc.recap_file_id:
        reading = ReceiptReading.objects.filter(
            custom_recap_file_id=exc.recap_file_id
        ).first()
    if reading is None and getattr(exc, "cost_line", None) is not None:
        link = (
            exc.cost_line.evidence_links.select_related("receipt_reading")
            .filter(receipt_reading__isnull=False)
            .first()
        )
        if link is not None:
            reading = link.receipt_reading
    if reading is not None:
        if reading.merchant:
            facts.append({"code": "ocr_merchant", "value": str(reading.merchant)[:80]})
        if reading.total is not None:
            facts.append({"code": "ocr_total", "value": str(reading.total)})
        if reading.receipt_date:
            facts.append(
                {"code": "ocr_receipt_date", "value": str(reading.receipt_date)[:10]}
            )
        line_count = len(reading.line_items or [])
        facts.append({"code": "ocr_line_items", "value": str(line_count)})

    # Pull any $ amounts already present in the rules explanation.
    for amount in _MONEY_RE.findall(exc.explanation or ""):
        facts.append({"code": "explanation_amount", "value": amount})

    return facts


def facts_as_lines(facts: list[dict[str, Any]]) -> list[str]:
    """Human-readable fact lines for the dispute pack UI / copy buffer."""
    labels = {
        "affected_amount": "Affected amount",
        "source_label": "Source",
        "store_label": "Store",
        "visit_date": "Visit date",
        "ocr_merchant": "OCR merchant",
        "ocr_total": "OCR receipt total",
        "ocr_receipt_date": "OCR receipt date",
        "ocr_line_items": "OCR line items",
        "exception_type": "Type",
        "severity": "Severity",
        "status": "Status",
    }
    lines: list[str] = []
    seen: set[str] = set()
    for fact in facts:
        code = fact.get("code") or ""
        if code in ("explanation_amount",) or code not in labels:
            continue
        value = fact.get("value")
        if value is None or value == "":
            continue
        if code in ("affected_amount", "ocr_total"):
            line = f"{labels[code]}: ${value}"
        else:
            line = f"{labels[code]}: {value}"
        if line not in seen:
            seen.add(line)
            lines.append(line)
    return lines


def rules_summary(exc, facts: list[dict[str, Any]] | None = None) -> str:
    """Rules one-liner: existing explanation, lightly normalized."""
    text = _sentence(exc.explanation or "")
    if not text:
        text = _sentence(
            f"{exc.exception_type.replace('_', ' ')} exception is open"
            + (
                f" for ${exc.affected_amount}"
                if exc.affected_amount is not None
                else ""
            )
        )
    return text[:MAX_SUMMARY_CHARS]


def _facts_hash(facts: list[dict[str, Any]]) -> str:
    raw = json.dumps(facts, sort_keys=True, default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _should_ask_model(facts: list[dict[str, Any]]) -> bool:
    """Model only when OCR / multiple money facts make a rewrite useful."""
    codes = {f.get("code") for f in facts}
    has_ocr = bool(codes & {"ocr_merchant", "ocr_total", "ocr_line_items"})
    money_facts = sum(
        1
        for f in facts
        if f.get("code") in ("affected_amount", "ocr_total", "explanation_amount")
    )
    return has_ocr or money_facts >= 2


def _prompt(exc, facts: list[dict[str, Any]]) -> str:
    lines = [
        f"Exception type: {exc.exception_type}",
        f"Rules explanation: {exc.explanation}",
        "Facts:",
    ]
    for fact in facts:
        lines.append(f"- {fact.get('code')}: {fact.get('value')}")
    return "\n".join(lines)


def _model_summary(exc, facts: list[dict[str, Any]]) -> str | None:
    from utils.ai_text import AiUnavailable, generate_json

    digest = _facts_hash(facts)
    key = f"finance_dispute:v1:{exc.uuid}:{digest}"
    hit = cache.get(key)
    if isinstance(hit, dict):
        text = hit.get("text")
        if isinstance(text, str) and text:
            return text
        try:
            tried = datetime.fromisoformat(str(hit.get("at")))
            if timezone.is_naive(tried):
                tried = timezone.make_aware(tried, timezone.get_current_timezone())
        except (TypeError, ValueError):
            tried = None
        if tried is not None and timezone.now() - tried < RETRY_AFTER:
            return None

    try:
        raw = generate_json(
            _SYSTEM, _prompt(exc, facts), schema=_SCHEMA, max_tokens=280, small=True
        )
    except AiUnavailable:
        return None
    except Exception:  # noqa: BLE001
        logger.warning("finance dispute explainer failed exc=%s", exc.uuid)
        cache.set(
            key,
            {"text": "", "at": timezone.now().isoformat()},
            CACHE_SECONDS,
        )
        return None

    allowed = _allowed_numbers(facts, exc.explanation, str(exc.affected_amount or ""))
    why = _clean_model_line(raw, allowed)
    cache.set(
        key,
        {"text": why or "", "at": timezone.now().isoformat()},
        CACHE_SECONDS,
    )
    return why


def finance_dispute_pack(exc) -> dict[str, Any]:
    """``{summary, source, facts, suggested_next_step}`` for one exception.

    Never raises. ``summary`` is always a non-empty rules or model line.
    """
    try:
        facts = gather_dispute_facts(exc)
        rules = rules_summary(exc, facts)
        source = "rules"
        summary = rules
        if _should_ask_model(facts):
            polished = _model_summary(exc, facts)
            if polished:
                summary = polished
                source = "model"
        return {
            "summary": summary[:MAX_SUMMARY_CHARS],
            "source": source,
            "facts": facts_as_lines(facts),
            "suggested_next_step": suggested_next_step(exc.exception_type),
        }
    except Exception:  # noqa: BLE001
        logger.warning("finance dispute pack failed exc=%s", getattr(exc, "uuid", None))
        return {
            "summary": rules_summary(exc),
            "source": "rules",
            "facts": [],
            "suggested_next_step": suggested_next_step(
                getattr(exc, "exception_type", "") or ""
            ),
        }
