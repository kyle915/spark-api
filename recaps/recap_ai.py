"""Per-recap feedback tags and the ops reviewer summary.

Two jobs, both off the page-view path for the model:

* :func:`extract_recap_feedback` runs when a recap is submitted (walk-up or
  ops). It reads the verbatim consumer-feedback / notes already stored on
  the recap and, when there is enough text and an API key, stores structured
  tags. It never rewrites the BA's fields. Quotes and competitor names that
  are not in the source text are dropped. Thin text stores nothing.

* :func:`reviewer_summary` is the admin recap-detail read. A cached model
  summary is returned as-is. Opening a Needs-review recap (or an explicit
  Summarize) generates once and caches it. Rule-check labels come from
  :func:`recaps.recap_quality.deterministic_quality_flags` — no score is
  produced here. Numbers in model prose that were not filed are replaced
  with a fact-sheet sentence.

Neither path approves, rejects, archives, or edits metrics. Neither path
changes how sampled / reach / handed-out KPIs are computed. The extract is
shaped so a later tenant-sentiment refresh can read it; that card is not
rewired here.

A missing ``OPENAI_API_KEY`` skips the model quietly.
"""

from __future__ import annotations

import hashlib
import logging
import re
from datetime import datetime

import strawberry
from asgiref.sync import sync_to_async
from django.conf import settings
from django.utils import timezone

from recaps.recap_quality import (
    _CUSTOM_FEEDBACK_NAME_RE,
    _is_blank,
    deterministic_quality_flags,
)
from utils.ai_text import AiUnavailable, generate_json
from utils.graphql.mixins import resolve_id_to_int
from utils.graphql.permissions import (
    StrictIsAuthenticated,
    _is_admin_access,
    resolve_request_user_access,
)

logger = logging.getLogger(__name__)

MIN_FEEDBACK_CHARS = 40
MAX_THEMES = 5
MAX_COMPETITORS = 8
MAX_OBJECTIONS = 5
MAX_QUOTES = 3
MAX_NOTE_CHARS = 240
MAX_SUMMARY_CHARS = 700
MAX_QUESTION_CHARS = 280

_PURCHASE_INTENT = ("likely", "unlikely", "mixed", "not_stated")
_INTENT_CUES = re.compile(
    r"\b(buy|bought|purchase|purchasing|price|pricey|expensive|cheap|"
    r"take home|not interested|won't buy|will buy|willing)\b",
    re.IGNORECASE,
)
_EXTRA_NOTE_RE = re.compile(
    r"competitor|objection|purchase|why they|what they drink|drinks?",
    re.IGNORECASE,
)
_NUMBER_RE = re.compile(r"\d+(?:\.\d+)?")
_IMAGE_EXT_RE = re.compile(r"\.(jpe?g|png|webp|gif|heic|heif)(\?|$)", re.IGNORECASE)
_WORD_RE = re.compile(r"[a-z]{4,}")
_STOPWORDS = frozenset(
    """
    this that with from they them were been have what when your about into
    just like really very more some than then their there would could should
    consumers people product brand taste said says very good nice great
    really thing things other another
    """.split()
)

_EXTRACT_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "themes",
        "competitor_products",
        "objections",
        "purchase_intent",
        "quotes",
    ],
    "properties": {
        "themes": {
            "type": "array",
            "maxItems": MAX_THEMES,
            "items": {"type": "string"},
        },
        "competitor_products": {
            "type": "array",
            "maxItems": MAX_COMPETITORS,
            "items": {"type": "string"},
        },
        "objections": {
            "type": "array",
            "maxItems": MAX_OBJECTIONS,
            "items": {"type": "string"},
        },
        "purchase_intent": {"type": "string", "enum": list(_PURCHASE_INTENT)},
        "quotes": {
            "type": "array",
            "maxItems": MAX_QUOTES,
            "items": {"type": "string"},
        },
    },
}

_SUMMARY_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["what_happened", "follow_up_question"],
    "properties": {
        "what_happened": {"type": "string"},
        "follow_up_question": {"type": "string"},
    },
}

_EXTRACT_SYSTEM = (
    "You extract structured tags from REAL field-recap notes. Use ONLY the "
    "verbatim text in the user message. If a category is not supported by "
    "that text, return an empty list (or purchase_intent=not_stated). Never "
    "invent a theme, competitor, objection, or quote. Every quote and every "
    "competitor product name MUST be copied from the notes. Themes and "
    "objections must use words that appear in the notes. Do not rewrite the "
    "notes and do not add numbers."
)

_SUMMARY_SYSTEM = (
    "You write a short ops review note for one filed field-marketing recap. "
    "Use ONLY the filed facts in the user message. Do not invent numbers, "
    "venues, people, samples, or quotes. Do not compute a score. Do not "
    "approve, reject, or archive. what_happened is 2-3 factual sentences. "
    "follow_up_question is one question a reviewer could send the BA, "
    "grounded in a blank or a rule check when one is listed."
)


def _collapse(text: str | None) -> str:
    if not text:
        return ""
    return " ".join(str(text).split()).strip()


def _norm_num(token: str) -> str:
    if "." in token:
        token = token.rstrip("0").rstrip(".")
    stripped = token.lstrip("0")
    return stripped or "0"


def _numbers_in(text: str) -> set[str]:
    return {_norm_num(match) for match in _NUMBER_RE.findall(text or "")}


def _numbers_allowed(text: str, allowed: set[str]) -> bool:
    return _numbers_in(text).issubset(allowed)


def _verbatim_span(needle: str, haystack: str) -> str | None:
    cleaned = _collapse(needle)
    source = _collapse(haystack)
    if len(cleaned) < 2 or not source:
        return None
    idx = source.lower().find(cleaned.lower())
    if idx < 0:
        return None
    return source[idx : idx + len(cleaned)]


def _grounded_label(label: str, source: str) -> str | None:
    """Keep a short tag only when a content word from it is in the notes."""
    cleaned = _collapse(label)
    if not cleaned or len(cleaned) > 80:
        return None
    if _verbatim_span(cleaned, source):
        return cleaned
    source_words = set(_WORD_RE.findall(source.lower()))
    words = [
        word
        for word in _WORD_RE.findall(cleaned.lower())
        if word not in _STOPWORDS
    ]
    if words and all(word in source_words for word in words):
        return cleaned
    return None


def _dedupe(items: list[str], limit: int) -> list[str]:
    seen: set[str] = set()
    kept: list[str] = []
    for item in items:
        key = item.lower()
        if key in seen:
            continue
        seen.add(key)
        kept.append(item)
        if len(kept) >= limit:
            break
    return kept


def _api_key_configured() -> bool:
    return bool((getattr(settings, "OPENAI_API_KEY", "") or "").strip())


def _feedback_snippets(recap_id: int, is_custom: bool) -> list[str]:
    """Verbatim consumer-feedback / notes. Does not include demographics."""
    from recaps.models import (
        AccountFeedback,
        ConsumerFeedback,
        CustomFieldValue,
        CustomRecap,
        Recap,
    )

    texts: list[str] = []
    if is_custom:
        recap = CustomRecap.objects.filter(id=recap_id).first()
        if recap is None:
            return []
        pairs = CustomFieldValue.objects.filter(custom_recap=recap).values_list(
            "custom_field__name", "value"
        )
        for name, value in pairs:
            label = name or ""
            if _is_blank(value):
                continue
            if _CUSTOM_FEEDBACK_NAME_RE.search(label) or _EXTRA_NOTE_RE.search(label):
                texts.append(str(value).strip())
        return texts

    recap = Recap.objects.filter(id=recap_id).first()
    if recap is None:
        return []
    for row in ConsumerFeedback.objects.filter(recap=recap):
        for value in (
            row.quotes,
            row.positive_stories,
            row.feedback,
            row.reasons_to_decline,
        ):
            if not _is_blank(value):
                texts.append(str(value).strip())
    for row in AccountFeedback.objects.filter(recap=recap):
        for value in (row.feedback, row.do_differently_feedback):
            if not _is_blank(value):
                texts.append(str(value).strip())
    if not _is_blank(recap.competitive_presence):
        texts.append(str(recap.competitive_presence).strip())
    return texts


def _source_hash(snippets: list[str]) -> str:
    blob = "\n".join(_collapse(text) for text in snippets)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _clean_extract(raw: dict | None, source: str) -> dict | None:
    if not isinstance(raw, dict):
        return None
    themes = _dedupe(
        [
            kept
            for item in raw.get("themes") or []
            if isinstance(item, str)
            for kept in [_grounded_label(item, source)]
            if kept
        ],
        MAX_THEMES,
    )
    competitors = _dedupe(
        [
            span
            for item in raw.get("competitor_products") or []
            if isinstance(item, str)
            for span in [_verbatim_span(item, source)]
            if span
        ],
        MAX_COMPETITORS,
    )
    objections = _dedupe(
        [
            kept
            for item in raw.get("objections") or []
            if isinstance(item, str)
            for kept in [_grounded_label(item, source)]
            if kept
        ],
        MAX_OBJECTIONS,
    )
    quotes = _dedupe(
        [
            span
            for item in raw.get("quotes") or []
            if isinstance(item, str)
            for span in [_verbatim_span(item, source)]
            if span and len(span) >= 8
        ],
        MAX_QUOTES,
    )
    intent = raw.get("purchase_intent")
    if intent not in ("likely", "unlikely", "mixed"):
        intent = None
    elif not _INTENT_CUES.search(source):
        intent = None
    if not themes and not competitors and not objections and not quotes and not intent:
        return None
    return {
        "themes": themes,
        "competitor_products": competitors,
        "objections": objections,
        "purchase_intent": intent,
        "quotes": quotes,
    }


def _save_extract(recap_id: int, is_custom: bool, digest: str, payload: dict | None) -> None:
    from recaps.models import RecapAiReview

    row, _created = RecapAiReview.objects.get_or_create(
        recap_id=recap_id,
        is_custom=is_custom,
    )
    row.feedback_extract = payload
    row.feedback_source_hash = digest
    row.extracted_at = timezone.now()
    row.save(
        update_fields=["feedback_extract", "feedback_source_hash", "extracted_at"]
    )


def extract_recap_feedback(recap_id: int, is_custom: bool) -> None:
    """Extract tags for one submitted recap. Never raises. Never edits metrics."""
    try:
        snippets = _feedback_snippets(int(recap_id), bool(is_custom))
    except Exception:
        logger.warning("recap feedback extract failed to load recap=%s", recap_id)
        return
    source = _collapse(" ".join(snippets))
    if len(source) < MIN_FEEDBACK_CHARS:
        return
    if not _api_key_configured():
        return
    digest = _source_hash(snippets)
    try:
        from recaps.models import RecapAiReview

        existing = RecapAiReview.objects.filter(
            recap_id=recap_id, is_custom=is_custom
        ).first()
        if existing and existing.feedback_source_hash == digest and existing.extracted_at:
            return
    except Exception:
        logger.warning("recap feedback extract cache read failed recap=%s", recap_id)
        return

    try:
        raw = generate_json(
            _EXTRACT_SYSTEM,
            "NOTES:\n" + source[:4000],
            schema=_EXTRACT_SCHEMA,
            max_tokens=600,
        )
    except AiUnavailable:
        return
    except Exception:
        logger.warning("recap feedback extract model failed recap=%s", recap_id)
        return
    payload = _clean_extract(raw, source)
    try:
        _save_extract(int(recap_id), bool(is_custom), digest, payload)
    except Exception:
        logger.warning("recap feedback extract save failed recap=%s", recap_id)


def schedule_recap_feedback_extract(recap_id: int, is_custom: bool) -> None:
    """Queue extraction. Inline when Redis is unset (Cloud Run). Never raises."""
    from utils.queues import Queues

    try:
        Queues().default.add(extract_recap_feedback, int(recap_id), bool(is_custom))
    except Exception:
        logger.warning("recap feedback extract enqueue failed recap=%s", recap_id)
        try:
            extract_recap_feedback(int(recap_id), bool(is_custom))
        except Exception:
            logger.warning("recap feedback extract inline failed recap=%s", recap_id)


def kick_recap_feedback_extract(recap_id: int, is_custom: bool) -> None:
    """Submit hook. Failures never fail the recap save."""
    try:
        schedule_recap_feedback_extract(recap_id, is_custom)
    except Exception:
        logger.warning("recap feedback extract kick failed recap=%s", recap_id)


def _image_count(files, attr: str) -> int:
    count = 0
    for row in files:
        blob = str(getattr(row, attr, "") or "")
        if _IMAGE_EXT_RE.search(blob):
            count += 1
    return count


def _person_name(user) -> str:
    if user is None:
        return ""
    name = f"{getattr(user, 'first_name', '') or ''} {getattr(user, 'last_name', '') or ''}".strip()
    return name or (getattr(user, "email", "") or "")


def _when_label(value) -> str:
    if isinstance(value, datetime):
        return value.date().isoformat()
    if value is None:
        return ""
    return str(value)[:10]


class _ReviewFacts:
    def __init__(self) -> None:
        self.exists = False
        self.approved = False
        self.archived = False
        self.name = ""
        self.venue = ""
        self.when = ""
        self.ba = ""
        self.metrics: list[tuple[str, str]] = []
        self.photo_count = 0
        self.notes_preview = ""
        self.field_lines: list[str] = []
        self.extra_flags: list[dict] = []


def _add_metric(facts: _ReviewFacts, label: str, value) -> None:
    if value is None or value == "":
        return
    facts.metrics.append((label, str(value)))


def _load_review_facts(recap_id: int, is_custom: bool) -> _ReviewFacts:
    from recaps.models import CustomRecap, Recap

    facts = _ReviewFacts()
    if is_custom:
        recap = (
            CustomRecap.objects.filter(id=recap_id)
            .select_related(
                "event",
                "event__retailer",
                "retailer",
                "ambassador__user",
                "location",
            )
            .first()
        )
        if recap is None:
            return facts
        facts.exists = True
        facts.approved = bool(recap.approved)
        facts.archived = recap.archived_at is not None
        facts.name = _collapse(recap.name)
        retailer = recap.retailer or getattr(recap.event, "retailer", None)
        facts.venue = _collapse(getattr(retailer, "name", "") or "")
        facts.when = _when_label(getattr(recap.event, "date", None))
        facts.ba = _collapse(recap.external_ba_name or "") or _person_name(
            getattr(recap.ambassador, "user", None) if recap.ambassador_id else None
        )
        _add_metric(facts, "Engagements", recap.total_engagements)
        facts.photo_count = _image_count(recap.custom_recap_files.all(), "url")
        from recaps.models import CustomFieldValue, CustomRecapProductSample

        for name, value in CustomFieldValue.objects.filter(
            custom_recap=recap
        ).values_list("custom_field__name", "value"):
            text = _collapse(value)
            if not text or text.lower().startswith("http") or len(text) > 500:
                continue
            facts.field_lines.append(f"{_collapse(name) or 'Field'}: {text[:MAX_NOTE_CHARS]}")
        for sample in CustomRecapProductSample.objects.filter(
            custom_recap=recap
        ).select_related("product"):
            product = getattr(sample.product, "name", "") or "Product"
            facts.field_lines.append(f"Sampled {product}: {sample.quantity}")
        flags = _collapse(getattr(recap, "data_quality_flags", "") or "")
        if flags:
            facts.extra_flags.append(
                {
                    "code": "data_quality",
                    "label": flags[:240],
                    "severity": "high",
                }
            )
    else:
        recap = (
            Recap.objects.filter(id=recap_id)
            .select_related("event", "event__retailer", "retailer", "ambassador__user")
            .first()
        )
        if recap is None:
            return facts
        facts.exists = True
        facts.approved = bool(recap.approved)
        facts.archived = recap.archived_at is not None
        facts.name = _collapse(recap.name)
        retailer = recap.retailer or getattr(recap.event, "retailer", None)
        facts.venue = _collapse(getattr(retailer, "name", "") or "")
        facts.when = _when_label(getattr(recap.event, "date", None))
        facts.ba = _collapse(recap.external_ba_name or "") or _person_name(
            getattr(recap.ambassador, "user", None) if recap.ambassador_id else None
        )
        _add_metric(facts, "Engagements", recap.total_engagements)
        _add_metric(facts, "Products sold", recap.products_sold)
        _add_metric(facts, "Cans sold", recap.total_cans_sold)
        _add_metric(facts, "Packs sold", recap.total_packs_sold)
        if recap.account_spend_amount is not None:
            _add_metric(facts, "Account spend", recap.account_spend_amount)
        facts.photo_count = _image_count(recap.recap_files.all(), "file")
        from recaps.models import ConsumerEngagements, ProductSamples

        for row in ConsumerEngagements.objects.filter(recap=recap):
            _add_metric(facts, "Consumers reached", row.total_consumer)
            _add_metric(facts, "Willing to purchase", row.willing_to_purchase_consumers)
        for sample in ProductSamples.objects.filter(recap=recap).select_related("product"):
            product = getattr(sample.product, "name", "") or "Product"
            facts.field_lines.append(f"Sampled {product}: {sample.quantity}")
        if not _is_blank(recap.traffic_description):
            facts.field_lines.append(f"Traffic: {_collapse(recap.traffic_description)}")
        if not _is_blank(recap.competitive_presence):
            facts.field_lines.append(
                f"Competitive presence: {_collapse(recap.competitive_presence)}"
            )

    snippets = _feedback_snippets(recap_id, is_custom)
    preview = _collapse(" ".join(snippets))
    if preview:
        facts.notes_preview = preview[:MAX_NOTE_CHARS]
    return facts


def _fact_sheet(facts: _ReviewFacts, flags: list[dict]) -> str:
    lines = [f"Title: {facts.name or 'Recap'}"]
    if facts.venue:
        lines.append(f"Venue: {facts.venue}")
    if facts.when:
        lines.append(f"Date: {facts.when}")
    if facts.ba:
        lines.append(f"BA: {facts.ba}")
    for label, value in facts.metrics:
        lines.append(f"{label}: {value}")
    lines.append(f"Photos on file: {facts.photo_count}")
    for line in facts.field_lines[:24]:
        lines.append(line)
    if facts.notes_preview:
        lines.append(f"BA notes verbatim: {facts.notes_preview}")
    if flags:
        lines.append("Rule checks:")
        for flag in flags:
            lines.append(f"- {flag.get('label') or ''}")
    else:
        lines.append("Rule checks: none")
    return "\n".join(lines)


def _rules_what_happened(facts: _ReviewFacts) -> str:
    bits = []
    where = " · ".join(part for part in (facts.venue, facts.when) if part)
    title = facts.name or "This recap"
    bits.append(f"{title} — {where}." if where else f"{title}.")
    if facts.ba:
        bits.append(f"Filed by {facts.ba}.")
    for label, value in facts.metrics:
        bits.append(f"{label}: {value}.")
    bits.append(f"Photos on file: {facts.photo_count}.")
    if facts.notes_preview:
        bits.append(f"BA notes (verbatim): {facts.notes_preview}")
    return " ".join(bits)[:MAX_SUMMARY_CHARS]


def _rules_question(flags: list[dict]) -> str:
    if flags:
        label = _collapse(flags[0].get("label") or "")
        if label:
            return f"Can you clarify this for the recap: {label}"[:MAX_QUESTION_CHARS]
    return "Anything else from this stop the client should know?"


def _live_flags(recap_id: int, is_custom: bool, facts: _ReviewFacts) -> list[dict]:
    flags = []
    for flag in deterministic_quality_flags(recap_id, is_custom=is_custom):
        if not isinstance(flag, dict):
            continue
        label = _collapse(flag.get("label") or "")
        if not label:
            continue
        severity = flag.get("severity")
        if severity not in ("high", "medium", "low"):
            severity = "low"
        flags.append(
            {
                "code": str(flag.get("code") or "rule"),
                "label": label,
                "severity": severity,
            }
        )
    flags.extend(facts.extra_flags)
    rank = {"high": 0, "medium": 1, "low": 2}
    flags.sort(key=lambda item: rank.get(item["severity"], 9))
    return flags


def _load_extract(recap_id: int, is_custom: bool) -> dict | None:
    from recaps.models import RecapAiReview

    row = RecapAiReview.objects.filter(recap_id=recap_id, is_custom=is_custom).first()
    if row is None or not isinstance(row.feedback_extract, dict):
        return None
    return row.feedback_extract


def _load_summary_row(recap_id: int, is_custom: bool):
    from recaps.models import RecapAiReview

    return RecapAiReview.objects.filter(recap_id=recap_id, is_custom=is_custom).first()


def _save_summary(recap_id: int, is_custom: bool, payload: dict) -> None:
    from recaps.models import RecapAiReview

    row, _created = RecapAiReview.objects.get_or_create(
        recap_id=recap_id,
        is_custom=is_custom,
    )
    row.summary = payload
    row.summarized_at = timezone.now()
    row.save(update_fields=["summary", "summarized_at"])


def _compose_summary(recap_id: int, is_custom: bool, *, generate: bool) -> dict | None:
    """Build the reviewer payload. ``generate`` may call the model once."""
    facts = _load_review_facts(recap_id, is_custom)
    if not facts.exists:
        return None
    flags = _live_flags(recap_id, is_custom, facts)
    extract = _load_extract(recap_id, is_custom)
    row = _load_summary_row(recap_id, is_custom)
    cached = row.summary if row is not None and isinstance(row.summary, dict) else None
    rules_q = _rules_question(flags)
    # Any saved summary — model prose or the rules fallback written after a
    # bad model call — is final. Reopening and Summarize both read it.
    if cached and _collapse(str(cached.get("what_happened") or "")):
        return _payload(
            what_happened=str(cached.get("what_happened") or ""),
            follow_up=str(cached.get("follow_up_question") or "") or rules_q,
            flags=flags,
            extract=extract,
            cached=True,
            source=str(cached.get("source") or "rules"),
        )

    rules_what = _rules_what_happened(facts)
    if not generate:
        return _payload(
            what_happened=rules_what,
            follow_up=rules_q,
            flags=flags,
            extract=extract,
            cached=False,
            source="rules",
        )

    if not _api_key_configured():
        return _payload(
            what_happened=rules_what,
            follow_up=rules_q,
            flags=flags,
            extract=extract,
            cached=False,
            source="rules",
        )

    sheet = _fact_sheet(facts, flags)
    allowed = _numbers_in(sheet)
    what = rules_what
    question = rules_q
    source = "rules"
    try:
        raw = generate_json(
            _SUMMARY_SYSTEM,
            "FILED FACTS:\n" + sheet,
            schema=_SUMMARY_SCHEMA,
            max_tokens=500,
        )
    except AiUnavailable:
        raw = None
    except Exception:
        logger.warning("recap reviewer summary model failed recap=%s", recap_id)
        raw = None
    if isinstance(raw, dict):
        candidate = _collapse(raw.get("what_happened") if isinstance(raw.get("what_happened"), str) else "")
        follow = _collapse(
            raw.get("follow_up_question")
            if isinstance(raw.get("follow_up_question"), str)
            else ""
        )
        if candidate and _numbers_allowed(candidate, allowed):
            what = candidate[:MAX_SUMMARY_CHARS]
            source = "model"
        if follow and _numbers_allowed(follow, allowed):
            question = follow[:MAX_QUESTION_CHARS]
            source = "model" if source == "model" else source
        if source == "model":
            try:
                _save_summary(
                    recap_id,
                    is_custom,
                    {
                        "what_happened": what,
                        "follow_up_question": question,
                        "source": "model",
                    },
                )
            except Exception:
                logger.warning("recap reviewer summary save failed recap=%s", recap_id)
            return _payload(
                what_happened=what,
                follow_up=question,
                flags=flags,
                extract=extract,
                cached=True,
                source="model",
            )
    # Model failed or invented numbers. Remember the safe rules text so the
    # next open does not call the model again.
    try:
        _save_summary(
            recap_id,
            is_custom,
            {
                "what_happened": rules_what,
                "follow_up_question": rules_q,
                "source": "rules",
            },
        )
    except Exception:
        logger.warning("recap reviewer summary rules save failed recap=%s", recap_id)
        return _payload(
            what_happened=rules_what,
            follow_up=rules_q,
            flags=flags,
            extract=extract,
            cached=False,
            source="rules",
        )
    return _payload(
        what_happened=rules_what,
        follow_up=rules_q,
        flags=flags,
        extract=extract,
        cached=True,
        source="rules",
    )


def _payload(
    *,
    what_happened: str,
    follow_up: str,
    flags: list[dict],
    extract: dict | None,
    cached: bool,
    source: str,
) -> dict:
    return {
        "what_happened": what_happened,
        "looks_off": flags,
        "follow_up_question": follow_up,
        "cached": cached,
        "source": source,
        "feedback": extract,
    }


def reviewer_summary(recap_id: int, is_custom: bool, *, generate: bool) -> dict | None:
    """Ops summary for one recap. Never raises. Never writes approval state."""
    try:
        return _compose_summary(int(recap_id), bool(is_custom), generate=generate)
    except Exception:
        logger.warning("recap reviewer summary failed recap=%s", recap_id)
        return None


def _needs_review(recap_id: int, is_custom: bool) -> bool:
    from recaps.models import CustomRecap, Recap

    model = CustomRecap if is_custom else Recap
    row = model.objects.filter(id=recap_id).values_list("approved", "archived_at").first()
    if row is None:
        return False
    approved, archived_at = row
    return not approved and archived_at is None


# ---------------------------------------------------------------------------
# GraphQL — admin recap detail only. Not on the recap list.
# ---------------------------------------------------------------------------


@strawberry.type(name="RecapFeedbackExtract")
class RecapFeedbackExtractType:
    themes: list[str]
    competitor_products: list[str]
    objections: list[str]
    purchase_intent: str | None
    quotes: list[str]


@strawberry.type(name="RecapReviewFlag")
class RecapReviewFlagType:
    code: str
    label: str
    severity: str


@strawberry.type(name="RecapReviewerSummary")
class RecapReviewerSummaryType:
    what_happened: str
    looks_off: list[RecapReviewFlagType]
    follow_up_question: str
    cached: bool
    source: str
    feedback: RecapFeedbackExtractType | None


def _extract_type(payload: dict | None) -> RecapFeedbackExtractType | None:
    if not isinstance(payload, dict):
        return None
    intent = payload.get("purchase_intent")
    if intent not in ("likely", "unlikely", "mixed"):
        intent = None
    return RecapFeedbackExtractType(
        themes=[str(item) for item in payload.get("themes") or [] if item],
        competitor_products=[
            str(item) for item in payload.get("competitor_products") or [] if item
        ],
        objections=[str(item) for item in payload.get("objections") or [] if item],
        purchase_intent=intent,
        quotes=[str(item) for item in payload.get("quotes") or [] if item],
    )


def _summary_type(payload: dict) -> RecapReviewerSummaryType:
    flags = []
    for item in payload.get("looks_off") or []:
        if not isinstance(item, dict):
            continue
        flags.append(
            RecapReviewFlagType(
                code=str(item.get("code") or "rule"),
                label=str(item.get("label") or ""),
                severity=str(item.get("severity") or "low"),
            )
        )
    return RecapReviewerSummaryType(
        what_happened=str(payload.get("what_happened") or ""),
        looks_off=flags,
        follow_up_question=str(payload.get("follow_up_question") or ""),
        cached=bool(payload.get("cached")),
        source=str(payload.get("source") or "rules"),
        feedback=_extract_type(payload.get("feedback")),
    )


async def _ops_caller(info: strawberry.Info) -> bool:
    from utils.graphql.mixins import SparkGraphQLMixin

    user = await SparkGraphQLMixin().get_user(info)
    role_slug, is_staff, is_super, email = await resolve_request_user_access(user)
    return _is_admin_access(role_slug, is_staff, is_super, email)


def _parse_recap_id(recap_id) -> int | None:
    try:
        return resolve_id_to_int(str(recap_id).strip())
    except Exception:
        return None


@strawberry.type
class RecapAiQueries:
    @strawberry.field(permission_classes=[StrictIsAuthenticated])
    async def recap_reviewer_summary(
        self,
        info: strawberry.Info,
        recap_id: strawberry.ID,
        is_custom: bool = False,
        generate_if_missing: bool = False,
    ) -> RecapReviewerSummaryType | None:
        """Ops reviewer note for ONE recap detail page.

        Clients receive null. ``generateIfMissing`` calls the model only when
        the recap is Needs review (approved false, not archived) and no
        summary is cached. The recap list must not query this field.
        """
        if not await _ops_caller(info):
            return None
        rid = _parse_recap_id(recap_id)
        if rid is None:
            return None

        def _build():
            generate = bool(generate_if_missing) and _needs_review(rid, is_custom)
            return reviewer_summary(rid, is_custom, generate=generate)

        try:
            payload = await sync_to_async(_build, thread_sensitive=True)()
        except Exception:
            return None
        if payload is None:
            return None
        return _summary_type(payload)


@strawberry.type
class RecapAiMutations:
    @strawberry.mutation(permission_classes=[StrictIsAuthenticated])
    async def summarize_recap(
        self,
        info: strawberry.Info,
        recap_id: strawberry.ID,
        is_custom: bool = False,
    ) -> RecapReviewerSummaryType | None:
        """Explicit Summarize. Returns the cache when one already exists.

        Does not approve, reject, archive, or edit the recap.
        """
        if not await _ops_caller(info):
            return None
        rid = _parse_recap_id(recap_id)
        if rid is None:
            return None

        def _build():
            return reviewer_summary(rid, is_custom, generate=True)

        try:
            payload = await sync_to_async(_build, thread_sensitive=True)()
        except Exception:
            return None
        if payload is None:
            return None
        return _summary_type(payload)
