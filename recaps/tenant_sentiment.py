"""AI-summarized "What people are saying" consumer sentiment for one tenant.

The dashboard surfaces a compact read on how CONSUMERS reacted to a client's
activations — an overall sentiment, a one-line summary, the recurring themes,
and a few verbatim quotes — distilled from the free-text feedback captured on
that tenant's recaps. This module owns gathering that text and turning it into
a small structured payload.

This is the AI-backed sibling of :mod:`recaps.tenant_insights` (which is now
fully deterministic). AI use here is sanctioned but COST-BOUNDED: the result is
cached in a :class:`tenants.models.TenantSentimentSnapshot` and refreshed at
most daily by a cron, so steady-state spend is ~1 OpenAI call per tenant per
day regardless of dashboard traffic.

* :func:`gather_consumer_feedback` — collect the tenant's free-text consumer
  feedback snippets, most-recent first, cleaned/deduped, and HARD-BOUNDED by
  both a snippet count and a cumulative character budget so the prompt can
  never blow up no matter how large the tenant is.
* :func:`build_tenant_sentiment` — gather the snippets and, when there are
  enough, ask OpenAI (via :func:`utils.ai_text.generate_json` with a strict
  schema) for the structured read. The result is defensively cleaned/clamped
  and quotes are verified to come VERBATIM from the gathered snippets. Returns
  ``None`` on too-little-data or ANY AI failure (the degrade posture
  :mod:`recaps.tenant_insights` uses).
* :func:`get_or_refresh_tenant_sentiment` — the snapshot front door, mirroring
  :func:`recaps.tenant_insights.get_or_refresh_tenant_insights`: serve a fresh
  snapshot, else regenerate + persist, else fall back to the last good
  snapshot; it NEVER raises.

Design rules (mirroring the rest of the report surface):

* **Reuse the tenant→events→recaps traversal.** Snippets are scoped exactly
  like :mod:`recaps.tenant_overview`: legacy :class:`recaps.models.Recap` free
  text through the event FK (``recap__event__tenant_id``) and custom-template
  free text through :class:`recaps.models.CustomFieldValue` on the custom
  recap's direct tenant FK. Year filtering reuses the same half-open
  :func:`recaps.tenant_overview._filter_year` window.
* **Never invent.** The system prompt pins the model to the provided text and
  requires quotes to be selected verbatim; the cleaner additionally DROPS any
  returned quote whose text isn't present in the gathered snippets, so a
  paraphrased/fabricated quote can never reach the client.
* **Bounded everywhere.** The gather is double-capped (count + chars) and every
  list in the AI result is capped (themes ≤ 5, quotes ≤ 3).

Everything here is synchronous Django ORM + one HTTP call; the GraphQL resolver
wraps the snapshot front door in ``sync_to_async``.
"""

from __future__ import annotations

import re
from datetime import datetime

from recaps.models import ConsumerFeedback, CustomFieldValue
from recaps.tenant_overview import _filter_year

# Hard upper bounds on the gathered prompt material. BOTH are enforced so the
# prompt stays small whether the tenant has 3 snippets or 300,000: we stop as
# soon as EITHER the snippet count or the cumulative character budget is hit.
DEFAULT_MAX_SNIPPETS = 120
DEFAULT_MAX_CHARS = 12_000

# Trim any single snippet so one rambling note can't dominate the budget (and
# so a returned quote stays a sane length). Mirrors
# ``tenant_overview.MAX_QUOTE_CHARS``.
MAX_SNIPPET_CHARS = 240

# Below this many distinct snippets there isn't enough signal to summarize, so
# :func:`build_tenant_sentiment` returns None rather than asking the model to
# extrapolate from one or two notes.
MIN_SNIPPETS_FOR_SUMMARY = 3

# Legacy ``ConsumerFeedback`` free-text columns that capture CONSUMER voice,
# most-quotable first. Deliberately EXCLUDES ``demographics`` (an audience
# description, not feedback). ``AccountFeedback`` is excluded entirely — it is
# the RETAILER/account's feedback, not the consumer's.
_LEGACY_FEEDBACK_FIELDS = (
    "quotes",
    "positive_stories",
    "feedback",
    "reasons_to_decline",
)

# Custom-template fields store feedback as free-text values keyed by the field
# NAME. We pull only value rows whose field name reads like consumer feedback.
# Superset of ``report_service``'s highlight regex (quote/story/highlight/
# feedback/testimonial) plus the comment/note/reaction/sentiment vocabulary the
# spec calls out and "what … said"-style prompts.
#
# This is the Python-side (``\b`` word-boundary) copy, used with ``re.search()``
# — by :mod:`recaps.recap_quality`, which duplicates this pattern locally, and
# available here for any future in-process matching. It is NOT used for the
# ``__iregex`` DB filter below; see :data:`_CUSTOM_FEEDBACK_NAME_IREGEX`.
_CUSTOM_FEEDBACK_NAME_RE = re.compile(
    r"quote|story|stories|highlight|feedback|testimonial|comment|"
    r"reaction|sentiment|verbatim|\bnotes?\b|consumer.*sa(?:id|y)|"
    r"what.*sa(?:id|y)|said",
    re.IGNORECASE,
)

# Postgres-side sibling of :data:`_CUSTOM_FEEDBACK_NAME_RE`, used ONLY for the
# ``__iregex`` filter in :func:`_custom_feedback_rows` below. A deliberately
# PLAIN STRING (not ``re.compile()``'d), with ``\y`` instead of ``\b`` for the
# word boundary around "notes?": Postgres's regex engine does NOT treat ``\b``
# as a word boundary the way Python's ``re`` does (confirmed live —
# ``'Field Notes' ~* '\bnotes?\b'`` is FALSE, which was silently excluding any
# field literally named "Notes"/"Field Notes" from every ``__iregex`` query
# built on ``_CUSTOM_FEEDBACK_NAME_RE.pattern``; ``\ynotes?\y`` is Postgres's
# correct spelling — confirmed live to match "Field Notes" but not
# "Annotestation"). Can't be ``re.compile()``'d — Python's ``re`` rejects ``\y``
# outright (``bad escape \y``). ``_CUSTOM_FEEDBACK_NAME_RE`` itself stays on
# ``\b`` because :mod:`recaps.recap_quality` reuses that pattern text with
# Python's ``re.search()``, where ``\y`` means nothing. Keep both in sync by
# hand if the vocabulary changes. Mirrored in
# :data:`recaps.field_sampling_report._FEEDBACK_NAME_RE_IREGEX`.
_CUSTOM_FEEDBACK_NAME_IREGEX = (
    r"quote|story|stories|highlight|feedback|testimonial|comment|"
    r"reaction|sentiment|verbatim|\ynotes?\y|consumer.*sa(?:id|y)|"
    r"what.*sa(?:id|y)|said"
)

# Valid enums the frontend renders. Anything else is normalised/dropped.
_VALID_SENTIMENTS = ("positive", "mixed", "negative")
_VALID_TONES = ("positive", "neutral", "negative")

# Caps on the structured result's lists (also enforced in the schema).
MAX_THEMES = 5
MAX_QUOTES = 3

# Strict JSON Schema for the structured outputs call. Per OpenAI strict mode:
# every object sets ``additionalProperties: false`` and lists ALL of its
# properties in ``required``; "optional" is expressed via a nullable union
# type, never by omission (none here are nullable — all fields are required and
# non-null, the model must always produce them).
SENTIMENT_JSON_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["overall_sentiment", "positive_pct", "summary", "themes", "quotes"],
    "properties": {
        "overall_sentiment": {
            "type": "string",
            "enum": list(_VALID_SENTIMENTS),
        },
        "positive_pct": {"type": "integer", "minimum": 0, "maximum": 100},
        "summary": {"type": "string"},
        "themes": {
            "type": "array",
            "maxItems": MAX_THEMES,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["label", "tone"],
                "properties": {
                    "label": {"type": "string"},
                    "tone": {"type": "string", "enum": list(_VALID_TONES)},
                },
            },
        },
        "quotes": {
            "type": "array",
            "maxItems": MAX_QUOTES,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["text", "tone"],
                "properties": {
                    "text": {"type": "string"},
                    "tone": {"type": "string", "enum": list(_VALID_TONES)},
                },
            },
        },
    },
}

# System prompt: pins the persona + the hard "real text only / verbatim quotes"
# constraints so the model can't fabricate consumer reactions or quotes.
_SENTIMENT_SYSTEM_PROMPT = (
    "You analyze REAL consumer feedback collected at a brand's field-marketing "
    "events and summarize how consumers reacted. Base EVERYTHING strictly on "
    "the feedback snippets provided in the user message — never invent, infer "
    "beyond, or embellish. Any quote you return MUST be copied VERBATIM "
    "(word-for-word) from one of the provided snippets; never paraphrase, "
    "edit, translate, or fabricate a quote. Choose representative quotes that "
    "reflect the overall sentiment. 'positive_pct' is your best estimate of "
    "the share of feedback that is positive (0-100). Keep 'summary' to 1-2 "
    "factual sentences. Return at most five themes and at most three quotes."
)


def _clean_snippet(text: str | None) -> str | None:
    """Collapse whitespace + length-trim a single snippet (None if empty).

    Mirrors :func:`recaps.tenant_overview._clean_quote`: normalises all
    internal whitespace runs to single spaces and trims to
    :data:`MAX_SNIPPET_CHARS` (with an ellipsis) so one long note can't
    dominate the prompt budget.
    """
    if not text:
        return None
    cleaned = " ".join(str(text).split()).strip()
    if not cleaned:
        return None
    if len(cleaned) > MAX_SNIPPET_CHARS:
        cleaned = cleaned[: MAX_SNIPPET_CHARS - 1].rstrip() + "…"
    return cleaned


def _legacy_feedback_rows(tenant_id: int, year: int | None):
    """Most-recent-first legacy ``ConsumerFeedback`` free-text value tuples.

    Scoped through the event (``recap__event__tenant_id`` — ``Recap`` has no
    direct tenant FK), exactly like :func:`recaps.tenant_overview` and the
    recap lists. Year-filtered on the feedback row's own ``created_at`` with the
    shared half-open window. Only the feedback text columns are selected — no
    Recap/feedback object is materialized — and ordering is newest-first so the
    snippet cap keeps the freshest feedback.
    """
    return (
        _filter_year(
            ConsumerFeedback.objects.filter(recap__event__tenant_id=tenant_id),
            "created_at",
            year,
        )
        .order_by("-created_at", "-id")
        .values_list(*_LEGACY_FEEDBACK_FIELDS)
        .iterator()
    )


def _custom_feedback_rows(tenant_id: int, year: int | None):
    """Most-recent-first custom feedback ``CustomFieldValue`` values.

    Pulls ONLY the feedback-like value rows (``custom_field__name`` matched by
    :data:`_CUSTOM_FEEDBACK_NAME_IREGEX` in SQL) on the custom recap's direct
    tenant FK, year-filtered on the value row's own ``created_at`` — the same
    bounded-slice approach :func:`recaps.tenant_overview._custom_kpis` uses for
    KPI values. Newest-first so the snippet cap keeps the freshest feedback.
    """
    return (
        _filter_year(
            CustomFieldValue.objects.filter(
                custom_recap__tenant_id=tenant_id,
                custom_field__name__iregex=_CUSTOM_FEEDBACK_NAME_IREGEX,
            ),
            "created_at",
            year,
        )
        .order_by("-created_at", "-id")
        .values_list("value", flat=True)
        .iterator()
    )


def gather_consumer_feedback(
    tenant_id: int,
    year: int | None = None,
    max_snippets: int = DEFAULT_MAX_SNIPPETS,
    max_chars: int = DEFAULT_MAX_CHARS,
) -> list[str]:
    """Collect a tenant's free-text consumer-feedback snippets, hard-bounded.

    Walks BOTH recap shapes — legacy :class:`recaps.models.ConsumerFeedback`
    free-text columns (through the event FK) and custom-template
    :class:`recaps.models.CustomFieldValue` rows whose field name reads like
    feedback — most-recent first, cleaning each snippet (whitespace-collapsed,
    length-trimmed) and DEDUPING on the cleaned text (case-insensitive).

    The result is bounded by BOTH ``max_snippets`` (a count cap) and
    ``max_chars`` (a cumulative character budget): collection stops as soon as
    EITHER limit is reached, so the downstream prompt is small regardless of
    tenant size. ``year=None`` gathers all-time; ``year=Y`` restricts to
    feedback whose ``created_at`` falls in calendar year ``Y`` (the same
    half-open window the tenant aggregates use).

    Returns the cleaned snippet strings (no surrounding quotes), newest first.
    Synchronous Django ORM; the only rows materialized are the bounded tail.
    """
    if max_snippets <= 0 or max_chars <= 0:
        return []

    snippets: list[str] = []
    seen: set[str] = set()
    total_chars = 0

    def _consume(raw_values) -> bool:
        """Add cleaned/deduped snippets from ``raw_values``.

        Returns True when a cap was hit (caller should stop), False otherwise.
        """
        nonlocal total_chars
        for raw in raw_values:
            cleaned = _clean_snippet(raw)
            if not cleaned:
                continue
            key = cleaned.lower()
            if key in seen:
                continue
            # Enforce the cumulative-char budget BEFORE appending so we never
            # overshoot it; if this snippet would breach the budget we stop
            # (rather than skip-and-continue) to keep "most recent first" intact.
            if total_chars + len(cleaned) > max_chars:
                return True
            seen.add(key)
            snippets.append(cleaned)
            total_chars += len(cleaned)
            if len(snippets) >= max_snippets:
                return True
        return False

    # Legacy rows yield one tuple per feedback row (several columns); flatten
    # to individual texts, preserving the newest-first row order.
    def _legacy_texts():
        for row in _legacy_feedback_rows(tenant_id, year):
            for value in row:
                yield value

    if _consume(_legacy_texts()):
        return snippets
    _consume(_custom_feedback_rows(tenant_id, year))
    return snippets


def _compose_user_prompt(snippets: list[str]) -> str:
    """Render the gathered snippets as the single source of truth for the model.

    A simple newline-delimited bulleted list. The snippets are already cleaned
    and bounded, so the whole prompt stays small.
    """
    lines = [
        "Consumer feedback snippets (each line is one piece of feedback):",
        "",
    ]
    lines.extend(f"- {s}" for s in snippets)
    return "\n".join(lines)


def _clamp_pct(value) -> int:
    """Coerce ``value`` to an int and clamp into ``[0, 100]`` (0 on garbage)."""
    try:
        pct = int(value)
    except (TypeError, ValueError):
        return 0
    return max(0, min(100, pct))


def _guard_tone(value) -> str:
    """Return ``value`` if a known tone, else ``"neutral"``."""
    if isinstance(value, str) and value in _VALID_TONES:
        return value
    return "neutral"


def _clean_themes(raw) -> list[dict]:
    """Keep only well-formed ``{label, tone}`` themes, tone-guarded, capped.

    Drops any non-dict entry or one missing a non-empty string ``label``;
    normalises ``tone`` to a known value; caps the list at :data:`MAX_THEMES`.
    """
    out: list[dict] = []
    if not isinstance(raw, list):
        return out
    for item in raw:
        if not isinstance(item, dict):
            continue
        label = item.get("label")
        if not isinstance(label, str) or not label.strip():
            continue
        out.append({"label": label.strip(), "tone": _guard_tone(item.get("tone"))})
        if len(out) >= MAX_THEMES:
            break
    return out


def _clean_quotes(raw, allowed_snippets: list[str]) -> list[dict]:
    """Keep only VERBATIM ``{text, tone}`` quotes present in the snippets, capped.

    The anti-fabrication guard: a returned quote is kept ONLY when its text
    matches one of the gathered ``allowed_snippets`` (case-insensitive,
    whitespace-collapsed substring match — a snippet may have been trimmed with
    an ellipsis, so the model's verbatim copy can be a prefix of, or equal to,
    a snippet, or the snippet can contain the quote). Anything the model
    paraphrased or invented is dropped. Tone is guarded; the list is capped at
    :data:`MAX_QUOTES`.
    """
    out: list[dict] = []
    if not isinstance(raw, list) or not allowed_snippets:
        return out

    # Normalised haystack of allowed text, and the ellipsis-stripped variants
    # so a model copy of a trimmed snippet (sans the trailing "…") still hits.
    normalized_allowed = []
    for snippet in allowed_snippets:
        norm = " ".join(snippet.split()).lower().rstrip("…").strip()
        if norm:
            normalized_allowed.append(norm)

    for item in raw:
        if not isinstance(item, dict):
            continue
        text = item.get("text")
        if not isinstance(text, str) or not text.strip():
            continue
        norm_text = " ".join(text.split()).lower().rstrip("…").strip()
        if not norm_text:
            continue
        # Verbatim check: the quote must be contained in (or contain) an
        # allowed snippet. Both directions cover snippet-trimming and the model
        # quoting a sentence out of a longer snippet.
        verbatim = any(
            norm_text in hay or hay in norm_text for hay in normalized_allowed
        )
        if not verbatim:
            continue
        out.append({"text": text.strip(), "tone": _guard_tone(item.get("tone"))})
        if len(out) >= MAX_QUOTES:
            break
    return out


def _clean_sentiment_payload(raw: dict, snippets: list[str]) -> dict | None:
    """Defensively clean/clamp the model's structured result (or None).

    Returns a payload with EXACTLY ``overall_sentiment`` (enum-guarded),
    ``positive_pct`` (clamped 0-100), ``summary`` (stripped string), ``themes``
    (well-formed + capped) and ``quotes`` (verbatim-verified + capped). Returns
    None only when the result isn't a dict or has no usable ``summary`` —
    everything else degrades field-by-field rather than failing the whole read.
    """
    if not isinstance(raw, dict):
        return None

    summary = raw.get("summary")
    if not isinstance(summary, str) or not summary.strip():
        return None

    overall = raw.get("overall_sentiment")
    if not isinstance(overall, str) or overall not in _VALID_SENTIMENTS:
        overall = "mixed"

    return {
        "overall_sentiment": overall,
        "positive_pct": _clamp_pct(raw.get("positive_pct")),
        "summary": summary.strip(),
        "themes": _clean_themes(raw.get("themes")),
        "quotes": _clean_quotes(raw.get("quotes"), snippets),
    }


def build_tenant_sentiment(tenant_id: int, year: int | None = None) -> dict | None:
    """Gather feedback and produce the structured sentiment read (or None).

    Collects the tenant's consumer-feedback snippets via
    :func:`gather_consumer_feedback`; if there are fewer than
    :data:`MIN_SNIPPETS_FOR_SUMMARY` distinct snippets there isn't enough
    signal, so returns ``None`` WITHOUT calling OpenAI (no token cost on thin
    tenants). Otherwise calls :func:`utils.ai_text.generate_json` with the
    strict :data:`SENTIMENT_JSON_SCHEMA`, then defensively cleans/clamps the
    result and verifies every quote is verbatim from the snippets.

    ``year`` is threaded straight through to the gather (``None`` = all-time).

    Returns the cleaned payload dict ``{overall_sentiment, positive_pct,
    summary, themes, quotes}`` on success, or ``None`` when there's too little
    data OR the AI call fails / returns nothing usable — the same degrade
    posture :func:`recaps.tenant_insights.build_tenant_insights` uses, so a
    single tenant's hiccup never raises.
    """
    try:
        snippets = gather_consumer_feedback(tenant_id, year=year)
        if len(snippets) < MIN_SNIPPETS_FOR_SUMMARY:
            return None

        # Imported lazily so the gather/cleaner helpers stay unit-testable
        # without importing the HTTP client (and so a missing settings module
        # can't break import of this module).
        from utils.ai_text import generate_json

        result = generate_json(
            _SENTIMENT_SYSTEM_PROMPT,
            _compose_user_prompt(snippets),
            schema=SENTIMENT_JSON_SCHEMA,
        )
        if result is None:
            return None
        return _clean_sentiment_payload(result, snippets)
    except Exception:
        # Mirror tenant_insights: ANY failure (incl. AiUnavailable when the key
        # is unset, or a DB hiccup) degrades to None rather than raising.
        return None


def get_or_refresh_tenant_sentiment(
    tenant_id: int, year: int | None = None, max_age_hours: int = 24
) -> tuple[dict | None, datetime | None]:
    """Serve a fresh sentiment snapshot, else regenerate + persist; never raise.

    The snapshot front door, mirroring
    :func:`recaps.tenant_insights.get_or_refresh_tenant_insights` but with a
    REAL freshness gate (the sentiment read is an AI call, so we amortise it):

    1. If the newest snapshot for ``(tenant, year)`` is younger than
       ``max_age_hours``, serve it (no AI call).
    2. Otherwise regenerate via :func:`build_tenant_sentiment`; on success
       persist a new :class:`tenants.models.TenantSentimentSnapshot` and return
       it.
    3. If regeneration yields ``None`` (too little data / AI failure), fall
       back to the LAST GOOD snapshot for ``(tenant, year)`` if one exists, so
       a transient AI outage doesn't blank an already-populated card.
    4. If there's nothing to serve at all, return ``(None, None)``.

    ``year`` partitions the cache (an all-time snapshot has ``year=None``; a
    per-year snapshot stores its year). NEVER raises: any DB/compute/AI error
    degrades to ``(None, None)`` (or the last-good fallback).
    """
    # Imported lazily so this module stays importable without Django apps
    # loaded (e.g. unit-testing the pure gather/cleaner functions).
    from django.utils import timezone

    from tenants.models import TenantSentimentSnapshot

    try:
        latest = (
            TenantSentimentSnapshot.objects.filter(tenant_id=tenant_id, year=year)
            .order_by("-generated_at")
            .first()
        )

        # 1) Fresh enough -> serve as-is, no AI call.
        if latest is not None:
            age = timezone.now() - latest.generated_at
            if age.total_seconds() <= max_age_hours * 3600:
                return latest.payload, latest.generated_at

        # 2) Stale or missing -> regenerate.
        payload = build_tenant_sentiment(tenant_id, year=year)
        if payload is not None:
            sample_size = len(gather_consumer_feedback(tenant_id, year=year))
            snapshot = TenantSentimentSnapshot.objects.create(
                tenant_id=tenant_id,
                year=year,
                payload=payload,
                sample_size=sample_size,
            )
            return snapshot.payload, snapshot.generated_at

        # 3) Regeneration produced nothing -> fall back to last good, if any.
        if latest is not None:
            return latest.payload, latest.generated_at

        # 4) Nothing to serve.
        return None, None
    except Exception:
        return None, None
