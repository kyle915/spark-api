"""Recap quality-check: flag incomplete / low-quality recaps for review.

A field-marketing recap is the BA's report of what happened at an event —
photos, the consumer/account free-text feedback, and the numeric KPIs
(engagements, consumers reached, samples handed out, units sold). When a recap
comes in thin (no photos, blank feedback, all-zero or self-contradictory
numbers) an admin wants it surfaced for review before it rolls into a client
report. This module turns one recap into a small, deterministic quality read:

    recap_quality_flags(recap_id, is_custom=False) -> {
        "score": int,            # 0-100, 100 = clean
        "flags": [               # the problems found, worst-first within tier
            {"code": str, "label": str, "severity": "high"|"medium"|"low"},
            ...
        ],
    }

It handles BOTH recap shapes (they DIFFER — see :mod:`recaps.models`):

* **Legacy** :class:`recaps.models.Recap` — typed KPI columns
  (``total_engagements`` / ``products_sold`` / ``total_cans_sold`` /
  ``total_packs_sold``), ``ConsumerEngagements`` for consumers-reached,
  ``ProductSamples`` for samples-distributed, free text on
  ``ConsumerFeedback`` + ``AccountFeedback``, photos on ``recap_files``
  (the ``file`` blob). Tenant is reached THROUGH the event
  (``recap.event.tenant_id`` — Recap has no direct tenant FK), exactly like
  :mod:`recaps.tenant_overview`.
* **Custom** :class:`recaps.models.CustomRecap` — only ``total_engagements`` is
  a typed column; the other KPIs live as free-text ``CustomFieldValue`` rows
  keyed by the field NAME and are parsed the same way
  :mod:`recaps.tenant_overview._custom_kpis_window` /
  :mod:`recaps.report_service` parse them, structured samples on
  ``CustomRecapProductSample``, feedback in ``CustomFieldValue`` rows whose
  field name reads like feedback (the :mod:`recaps.tenant_sentiment` regex),
  photos on ``custom_recap_files`` (the ``url`` blob). Tenant is a DIRECT FK
  (``custom_recap.tenant_id``).

Two layers, mirroring the rest of the report surface
(:mod:`recaps.tenant_insights` deterministic + :mod:`recaps.tenant_sentiment`
AI):

1. **Deterministic checks (the must-have, no AI).** Missing/low photos vs the
   expected minimum (:data:`MIN_EXPECTED_PHOTOS`, the required-photo
   expectation per issue #195), empty/blank key feedback, numeric
   inconsistencies (sold > sampled, everything zero, implausible values), and
   missing required fields. Each problem is a flag with a stable ``code``, a
   human ``label``, and a ``severity``; the score is 100 minus a per-severity
   penalty, floored at 0.
2. **Optional light AI pass (only if free-text feedback exists).** Asks
   :func:`utils.ai_text.generate_json` (a small bounded-prompt strict schema)
   whether the feedback itself is thin / vague / low-effort and, if so, adds a
   flag + a small score nudge. The call is CACHED in a
   :class:`recaps.models.RecapQualitySnapshot` keyed by the recap (mirroring
   :class:`tenants.models.TenantSentimentSnapshot`) so steady-state spend is
   ~one OpenAI call per recap. A missing key / AI failure degrades to
   deterministic-only — the AI pass can never raise out of here, and never
   lowers the must-have signal.

Everything is synchronous Django ORM (+ at most one cached HTTP call); the
GraphQL resolver in :mod:`recaps.report_types` wraps the entry point in
``sync_to_async`` and is itself never-raise.
"""

from __future__ import annotations

import re

# ---------------------------------------------------------------------------
# Tunables — penalties, thresholds, and the required-photo expectation.
# ---------------------------------------------------------------------------

# The starting (perfect) score and the per-severity penalty applied for each
# flag raised. Tuned so a single high-severity miss (e.g. zero photos) lands a
# recap comfortably below a "needs review" line while a lone low-severity nit
# barely dents the score. The score is floored at 0 (never negative).
MAX_SCORE = 100
_SEVERITY_PENALTY = {"high": 30, "medium": 15, "low": 7}

# Required-photo expectation (issue #195). A recap with NO image-typed file is
# a high-severity miss (an event with no photo is the canonical "incomplete"
# recap); below this count but non-zero is a softer low-severity nudge. There
# is no per-tenant required-photo column in the schema today, so this is the
# single shared expectation — kept as a module constant so a future
# per-tenant/per-template override is a one-line change.
MIN_EXPECTED_PHOTOS = 2

# Browser-renderable image extensions — the same ``isImage`` rule the recap
# hero-image picker and the campaign-report gallery use
# (:data:`recaps.types._IMAGE_URL_RE`), so "a photo" means a blob an ``<img>``
# can actually render, not a PDF/receipt/HEIC attached to the recap.
_IMAGE_EXT_RE = re.compile(r"\.(jpe?g|png|webp|gif|heic|heif)(\?|$)", re.IGNORECASE)

# Implausible-value ceilings. These are sanity bounds, not business rules: a
# single recap reporting numbers past these is almost certainly a typo / unit
# error (e.g. cans entered in the engagements box) and is worth a look. Generous
# enough that a genuinely huge activation won't trip them.
_IMPLAUSIBLE_ENGAGEMENTS = 100_000
_IMPLAUSIBLE_CONSUMERS = 100_000
_IMPLAUSIBLE_SAMPLES = 1_000_000
_IMPLAUSIBLE_SOLD = 1_000_000

# Custom-template field-name regexes. The KPI regex mirrors
# :data:`recaps.tenant_overview._CUSTOM_KPI_NAME_RE`; the feedback regex mirrors
# :data:`recaps.tenant_sentiment._CUSTOM_FEEDBACK_NAME_RE`. Reused here so the
# quality read "sees" the same free-text the dashboards aggregate / summarize.
_CUSTOM_KPI_NAME_RE = re.compile(
    r"consumers sampled|first time|knew about|willing to purchase|cans?|packs?",
    re.IGNORECASE,
)
_CUSTOM_FEEDBACK_NAME_RE = re.compile(
    r"quote|story|stories|highlight|feedback|testimonial|comment|"
    r"reaction|sentiment|verbatim|\bnotes?\b|consumer.*sa(?:id|y)|"
    r"what.*sa(?:id|y)|said",
    re.IGNORECASE,
)

# Leading-integer parse for free-text numeric fields (mirrors
# ``tenant_overview._leading_int`` / ``types._parse_recap_int``): pulls the
# first integer out of strings like "120 consumers", "~30", "12 cans". Returns
# None when there's no number.
_LEADING_INT_RE = re.compile(r"-?\d+")


def _leading_int(value) -> int | None:
    """First integer embedded in ``value`` (e.g. ``"120 cans"`` -> 120), or None."""
    if value is None:
        return None
    match = _LEADING_INT_RE.search(str(value))
    if not match:
        return None
    try:
        return int(match.group())
    except (TypeError, ValueError):
        return None


def _is_blank(text) -> bool:
    """True when ``text`` is None / not a string / only whitespace."""
    return not (isinstance(text, str) and text.strip())


# ---------------------------------------------------------------------------
# Normalized recap facts. Both shapes are reduced to the SAME small dataclass
# so the deterministic checks (and the AI pass) are written once, shape-blind.
# ---------------------------------------------------------------------------


class _RecapFacts:
    """The shape-agnostic facts the quality checks read.

    ``photo_count`` counts only image-typed files (``isImage``).
    ``feedback_texts`` is the list of non-blank consumer/account free-text
    snippets. The numeric KPIs are the same nine the dashboards roll up, each
    ``None`` when the recap doesn't carry that field at all (so "missing" and
    "explicitly zero" stay distinguishable). ``exists`` is False when the
    recap id didn't resolve (the resolver turns that into a safe default).
    """

    __slots__ = (
        "exists",
        "tenant_id",
        "photo_count",
        "feedback_texts",
        "total_engagements",
        "consumers_reached",
        "samples_distributed",
        "products_sold",
        "cans_sold",
        "packs_sold",
    )

    def __init__(self):
        self.exists = False
        self.tenant_id: int | None = None
        self.photo_count = 0
        self.feedback_texts: list[str] = []
        self.total_engagements: int | None = None
        self.consumers_reached: int | None = None
        self.samples_distributed: int | None = None
        self.products_sold: int | None = None
        self.cans_sold: int | None = None
        self.packs_sold: int | None = None

    def numeric_values(self) -> list[int]:
        """The present (non-None) numeric KPI values, for the all-zero check."""
        return [
            v
            for v in (
                self.total_engagements,
                self.consumers_reached,
                self.samples_distributed,
                self.products_sold,
                self.cans_sold,
                self.packs_sold,
            )
            if v is not None
        ]


def _count_image_files(files, blob_attr: str) -> int:
    """Count files whose blob path looks like a renderable image.

    ``files`` is an iterable of RecapFile / CustomRecapFile rows; ``blob_attr``
    is the FileField name (``"file"`` for legacy, ``"url"`` for custom). Reads
    the stored blob NAME only — no GCS I/O, no signing — and applies the shared
    ``isImage`` extension rule.
    """
    count = 0
    for f in files:
        field_file = getattr(f, blob_attr, None)
        if not field_file:
            continue
        try:
            blob = field_file.name
        except Exception:
            blob = str(field_file)
        if blob and _IMAGE_EXT_RE.search(blob):
            count += 1
    return count


def _gather_legacy_facts(recap) -> _RecapFacts:
    """Reduce a legacy :class:`recaps.models.Recap` to :class:`_RecapFacts`.

    Tenant via the event FK (``recap.event.tenant_id`` — no direct tenant FK).
    Photos from ``recap_files`` (the ``file`` blob). Feedback from the
    ``ConsumerFeedback`` columns the sentiment gather uses (quotes /
    positive_stories / feedback / reasons_to_decline; ``demographics`` is an
    audience note, not feedback, so it's excluded) plus ``AccountFeedback``
    free text. KPIs: typed columns off the recap, consumers-reached summed off
    ``ConsumerEngagements``, samples summed off ``ProductSamples`` — the same
    sources as :func:`recaps.tenant_overview._legacy_kpis_window`.
    """
    from django.db.models import Sum

    from recaps.models import ConsumerEngagements, ProductSamples

    facts = _RecapFacts()
    facts.exists = True
    facts.tenant_id = getattr(recap.event, "tenant_id", None)

    facts.photo_count = _count_image_files(
        recap.recap_files.all(), "file"
    )

    texts: list[str] = []
    for cf in recap.consumer_feedback.all():
        for value in (
            cf.quotes,
            cf.positive_stories,
            cf.feedback,
            cf.reasons_to_decline,
        ):
            if not _is_blank(value):
                texts.append(value.strip())
    for af in recap.account_feedback.all():
        for value in (af.feedback, af.do_differently_feedback):
            if not _is_blank(value):
                texts.append(value.strip())
    facts.feedback_texts = texts

    facts.total_engagements = recap.total_engagements
    facts.products_sold = recap.products_sold
    facts.cans_sold = recap.total_cans_sold
    facts.packs_sold = recap.total_packs_sold

    # consumers_reached: sum of the engagement rows' total_consumer (a recap can
    # have several ConsumerEngagements rows). None when there are no rows at all
    # so "no engagement data" reads as missing rather than a real zero.
    consumers = ConsumerEngagements.objects.filter(recap=recap).aggregate(
        total=Sum("total_consumer")
    )["total"]
    facts.consumers_reached = consumers

    samples = ProductSamples.objects.filter(recap=recap).aggregate(
        total=Sum("quantity")
    )["total"]
    facts.samples_distributed = samples

    return facts


def _gather_custom_facts(custom_recap) -> _RecapFacts:
    """Reduce a custom :class:`recaps.models.CustomRecap` to :class:`_RecapFacts`.

    Tenant is the recap's DIRECT FK. Photos from ``custom_recap_files`` (the
    ``url`` blob). The only typed KPI is ``total_engagements``; the rest are
    parsed out of the recap's free-text ``CustomFieldValue`` rows keyed by
    field NAME, applying the same label rules
    :func:`recaps.tenant_overview._custom_kpis_window` uses (consumers
    sampled -> consumers_reached, cans/packs by name). Structured samples come
    from ``CustomRecapProductSample`` and, when absent, fall back to the summed
    "consumers sampled" headline (mirroring ``report_service``). Feedback is
    the value of any field whose NAME reads like feedback
    (:data:`_CUSTOM_FEEDBACK_NAME_RE`), matching the sentiment gather.
    """
    from django.db.models import Sum

    from recaps.models import CustomFieldValue, CustomRecapProductSample

    facts = _RecapFacts()
    facts.exists = True
    facts.tenant_id = custom_recap.tenant_id

    facts.photo_count = _count_image_files(
        custom_recap.custom_recap_files.all(), "url"
    )

    facts.total_engagements = custom_recap.total_engagements

    # Pull the recap's field (name, value) pairs once; classify both the
    # feedback snippets and the KPI numbers from the same in-memory list.
    pairs = list(
        CustomFieldValue.objects.filter(custom_recap=custom_recap).values_list(
            "custom_field__name", "value"
        )
    )

    texts: list[str] = []
    consumers_reached = 0
    products_sold = 0
    cans_sold = 0
    packs_sold = 0
    saw_consumers = False
    saw_sold = False
    for name, value in pairs:
        label = (name or "")
        # Feedback snippet: field NAME reads like feedback and value is text.
        if _CUSTOM_FEEDBACK_NAME_RE.search(label) and not _is_blank(value):
            texts.append(value.strip())
        # KPI parse: only the KPI-named fields carry numbers we sum.
        if not _CUSTOM_KPI_NAME_RE.search(label):
            continue
        parsed = _leading_int(value)
        if parsed is None:
            continue
        low = label.lower()
        if "consumers sampled" in low:
            consumers_reached += parsed
            saw_consumers = True
            saw_sold = True if "sold" in low else saw_sold
        if re.search(r"\bcans?\b", low):
            cans_sold += parsed
            saw_sold = True
        elif re.search(r"\bpacks?\b", low):
            packs_sold += parsed
            saw_sold = True
    facts.feedback_texts = texts

    facts.consumers_reached = consumers_reached if saw_consumers else None
    # products_sold for custom recaps is approximated by the cans+packs the
    # report rolls into products_sold (there is no separate sold-units field in
    # the minimal custom shape); None when no sold-ish field was present.
    if cans_sold or packs_sold:
        products_sold = cans_sold + packs_sold
        saw_sold = True
    facts.products_sold = products_sold if saw_sold else None
    facts.cans_sold = cans_sold if saw_sold else None
    facts.packs_sold = packs_sold if saw_sold else None

    structured_samples = CustomRecapProductSample.objects.filter(
        custom_recap=custom_recap
    ).aggregate(total=Sum("quantity"))["total"]
    if structured_samples:
        facts.samples_distributed = int(structured_samples)
    elif saw_consumers:
        # Fallback mirrors report_service: with no structured samples, the
        # "consumers sampled" headline doubles as samples-distributed.
        facts.samples_distributed = consumers_reached
    else:
        facts.samples_distributed = None

    return facts


# ---------------------------------------------------------------------------
# Deterministic checks. Each returns a flag dict or None; the runner collects
# the non-None ones. Flags use stable codes (a contract for the frontend).
# ---------------------------------------------------------------------------


def _flag(code: str, label: str, severity: str) -> dict:
    return {"code": code, "label": label, "severity": severity}


def _check_photos(facts: _RecapFacts) -> dict | None:
    """No image -> high; some but < expected -> low; enough -> clean."""
    if facts.photo_count <= 0:
        return _flag(
            "no_photos",
            "No photos attached to this recap.",
            "high",
        )
    if facts.photo_count < MIN_EXPECTED_PHOTOS:
        return _flag(
            "few_photos",
            f"Only {facts.photo_count} photo(s); at least "
            f"{MIN_EXPECTED_PHOTOS} are expected.",
            "low",
        )
    return None


def _check_feedback(facts: _RecapFacts) -> dict | None:
    """No non-blank consumer/account feedback text at all -> medium."""
    if not facts.feedback_texts:
        return _flag(
            "no_feedback",
            "No consumer or account feedback was recorded.",
            "medium",
        )
    return None


def _check_all_numbers_missing(facts: _RecapFacts) -> dict | None:
    """Recap carries NO numeric KPI at all -> high (an empty report)."""
    if not facts.numeric_values():
        return _flag(
            "no_kpis",
            "No KPI numbers were recorded (engagements, consumers, "
            "samples, or sales).",
            "high",
        )
    return None


def _check_all_zero(facts: _RecapFacts) -> dict | None:
    """Every PRESENT numeric KPI is zero -> medium (a worked event with all
    zeros is suspicious; skipped when no numbers are present — that's the
    ``no_kpis`` case instead)."""
    present = facts.numeric_values()
    if present and all(v == 0 for v in present):
        return _flag(
            "all_zero_kpis",
            "Every recorded KPI is zero.",
            "medium",
        )
    return None


def _check_sold_exceeds_sampled(facts: _RecapFacts) -> dict | None:
    """Units sold > samples distributed -> medium (you can't sell more than you
    handed out at a sampling event; a strong data-entry smell). Only checked
    when both numbers are present and samples is meaningful."""
    sold = facts.products_sold
    sampled = facts.samples_distributed
    if (
        sold is not None
        and sampled is not None
        and sampled > 0
        and sold > sampled
    ):
        return _flag(
            "sold_exceeds_sampled",
            f"Units sold ({sold}) exceed samples distributed ({sampled}).",
            "medium",
        )
    return None


def _check_implausible(facts: _RecapFacts) -> dict | None:
    """Any present KPI past its sanity ceiling -> medium (likely a typo/unit
    error). One flag covers all of them so a single fat-fingered field doesn't
    stack four penalties."""
    checks = (
        (facts.total_engagements, _IMPLAUSIBLE_ENGAGEMENTS),
        (facts.consumers_reached, _IMPLAUSIBLE_CONSUMERS),
        (facts.samples_distributed, _IMPLAUSIBLE_SAMPLES),
        (facts.products_sold, _IMPLAUSIBLE_SOLD),
        (facts.cans_sold, _IMPLAUSIBLE_SOLD),
        (facts.packs_sold, _IMPLAUSIBLE_SOLD),
    )
    for value, ceiling in checks:
        if value is not None and value > ceiling:
            return _flag(
                "implausible_value",
                "A KPI value is implausibly large and may be a data-entry "
                "error.",
                "medium",
            )
    return None


def _check_negative(facts: _RecapFacts) -> dict | None:
    """Any present KPI is negative -> medium (impossible count)."""
    if any(v < 0 for v in facts.numeric_values()):
        return _flag(
            "negative_value",
            "A KPI value is negative, which is not possible.",
            "medium",
        )
    return None


# The deterministic check pipeline, run in order. Each is a pure
# ``_RecapFacts -> dict | None``.
_DETERMINISTIC_CHECKS = (
    _check_photos,
    _check_feedback,
    _check_all_numbers_missing,
    _check_all_zero,
    _check_sold_exceeds_sampled,
    _check_implausible,
    _check_negative,
)

# Order flags are presented in: worst severity first, then stable insertion
# order within a tier (so the payload is deterministic for a given recap).
_SEVERITY_RANK = {"high": 0, "medium": 1, "low": 2}


def _score_from_flags(flags: list[dict]) -> int:
    """100 minus the summed per-severity penalties, floored at 0."""
    penalty = sum(_SEVERITY_PENALTY.get(f["severity"], 0) for f in flags)
    return max(0, MAX_SCORE - penalty)


def _run_deterministic(facts: _RecapFacts) -> list[dict]:
    """All deterministic flags for ``facts`` (unsorted; runner sorts/scores)."""
    out: list[dict] = []
    for check in _DETERMINISTIC_CHECKS:
        flag = check(facts)
        if flag is not None:
            out.append(flag)
    return out


# ---------------------------------------------------------------------------
# Optional AI pass — thin/vague/low-effort feedback. Cached per recap.
# ---------------------------------------------------------------------------

# How many feedback chars to show the model, and the most snippets. Tiny on
# purpose: this is a yes/no quality nudge, not a summary.
_AI_MAX_SNIPPETS = 12
_AI_MAX_CHARS = 2_500

# The score nudge applied when the AI judges the feedback low-quality. Small —
# the deterministic signal is the must-have; this just sharpens it.
_AI_LOW_QUALITY_PENALTY = 10

# Strict JSON schema (OpenAI structured outputs): every object sets
# ``additionalProperties: false`` and lists ALL properties in ``required``.
_FEEDBACK_QUALITY_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["low_quality", "reason"],
    "properties": {
        # True when the feedback is thin / vague / boilerplate / low-effort.
        "low_quality": {"type": "boolean"},
        # A short (<= ~12 word) human reason; shown as the flag label suffix.
        "reason": {"type": "string"},
    },
}

_FEEDBACK_QUALITY_SYSTEM_PROMPT = (
    "You review free-text feedback a brand ambassador wrote about a "
    "field-marketing event, and judge ONLY whether it is too thin, vague, "
    "generic, or low-effort to be useful to the brand (e.g. 'good', 'n/a', "
    "'people liked it', filler, or copy-paste boilerplate). Base your "
    "judgment strictly on the text provided — never invent details. Set "
    "'low_quality' to true when the feedback is not substantive, false when "
    "it contains specific, usable observations. Keep 'reason' to a short "
    "phrase (a dozen words at most)."
)


def _ai_feedback_flag(feedback_texts: list[str]) -> dict | None:
    """Ask the model whether the feedback is low-quality; a flag dict or None.

    Bounded prompt (snippet + char capped). Returns a ``low_feedback_quality``
    flag (low severity) ONLY when the model is confident the feedback is thin;
    returns None on a "fine" verdict, on no/empty text, on a missing key, or on
    ANY AI/parse failure. NEVER raises — the caller treats None as "AI added
    nothing".
    """
    # Bound the material BEFORE composing the prompt (count + cumulative chars).
    snippets: list[str] = []
    total = 0
    for text in feedback_texts:
        cleaned = " ".join(str(text).split()).strip()
        if not cleaned:
            continue
        if total + len(cleaned) > _AI_MAX_CHARS:
            break
        snippets.append(cleaned)
        total += len(cleaned)
        if len(snippets) >= _AI_MAX_SNIPPETS:
            break
    if not snippets:
        return None

    user_prompt = "\n".join(
        ["Feedback snippets (one per line):", ""] + [f"- {s}" for s in snippets]
    )

    try:
        # Imported lazily so the deterministic path (and this module's import)
        # never depends on the HTTP client / settings being importable.
        from utils.ai_text import generate_json

        result = generate_json(
            _FEEDBACK_QUALITY_SYSTEM_PROMPT,
            user_prompt,
            schema=_FEEDBACK_QUALITY_SCHEMA,
        )
    except Exception:
        # AiUnavailable (no key) or anything else -> degrade silently.
        return None

    if not isinstance(result, dict) or result.get("low_quality") is not True:
        return None

    reason = result.get("reason")
    label = "Feedback looks thin or low-effort."
    if isinstance(reason, str) and reason.strip():
        # Trim a runaway reason so the label stays short.
        clipped = reason.strip()
        if len(clipped) > 120:
            clipped = clipped[:119].rstrip() + "…"
        label = f"Feedback looks thin or low-effort: {clipped}"
    return _flag("low_feedback_quality", label, "low")


def _maybe_ai_flag(recap_id: int, is_custom: bool, facts: _RecapFacts) -> dict | None:
    """Return the cached/computed AI low-quality flag, or None.

    Caches the AI verdict in a :class:`recaps.models.RecapQualitySnapshot` keyed
    by ``(recap_id, is_custom)`` so the OpenAI call fires ~once per recap. On a
    cache hit the stored flag is rebuilt without an AI call; on a miss the AI is
    asked and the verdict (flag-or-no-flag) is persisted. NEVER raises: any DB
    or AI failure degrades to None (deterministic-only).
    """
    # No feedback at all -> nothing for the AI to judge (the deterministic
    # ``no_feedback`` flag already covers that case). Skip the call entirely.
    if not facts.feedback_texts:
        return None

    try:
        from recaps.models import RecapQualitySnapshot
    except Exception:
        return None

    try:
        snapshot = RecapQualitySnapshot.objects.filter(
            recap_id=recap_id, is_custom=is_custom
        ).first()
        if snapshot is not None:
            # Cache hit: rebuild the flag from the stored verdict, no AI call.
            if snapshot.low_quality:
                label = snapshot.label or "Feedback looks thin or low-effort."
                return _flag("low_feedback_quality", label, "low")
            return None

        # Cache miss: ask the model, then persist whatever it decided so the
        # next read is free (we store the negative verdict too, to avoid
        # re-asking on every hit).
        flag = _ai_feedback_flag(facts.feedback_texts)
        RecapQualitySnapshot.objects.create(
            recap_id=recap_id,
            is_custom=is_custom,
            low_quality=flag is not None,
            label=(flag["label"] if flag is not None else ""),
        )
        return flag
    except Exception:
        # DB hiccup / race on the unique key / anything else -> just try the
        # AI directly (still never raises) so we degrade to deterministic-only
        # rather than dropping the signal.
        try:
            return _ai_feedback_flag(facts.feedback_texts)
        except Exception:
            return None


# ---------------------------------------------------------------------------
# Public entry point.
# ---------------------------------------------------------------------------

# A safe, neutral result for "recap not found / nothing to assess". A missing
# recap is not a quality problem, so it scores a clean 100 with no flags (the
# resolver also returns this on out-of-scope / error).
_EMPTY_RESULT: dict = {"score": MAX_SCORE, "flags": []}


def _finalize(flags: list[dict]) -> dict:
    """Sort flags worst-first + stable, compute the score, return the payload."""
    ordered = sorted(
        flags, key=lambda f: _SEVERITY_RANK.get(f["severity"], 99)
    )
    return {"score": _score_from_flags(ordered), "flags": ordered}


def recap_quality_flags(recap_id, is_custom: bool = False) -> dict:
    """Deterministic (+ optional cached-AI) quality read for one recap.

    Loads the recap (legacy :class:`recaps.models.Recap` when
    ``is_custom`` is False, else :class:`recaps.models.CustomRecap`), reduces it
    to shape-agnostic facts, runs the deterministic checks, then — only when the
    recap has free-text feedback — folds in the cached AI low-quality verdict.

    Args:
        recap_id: The recap's primary key (int or numeric string).
        is_custom: Select the custom-template shape over the legacy one.

    Returns:
        ``{"score": int 0-100, "flags": [{"code", "label", "severity"}, ...]}``.
        Flags are ordered worst-severity-first (stable within a tier) and the
        score is ``100`` minus the summed per-severity penalties, floored at
        ``0``. A missing recap (or any failure) returns the neutral
        ``{"score": 100, "flags": []}`` — this function NEVER raises.
    """
    try:
        rid = int(recap_id)
    except (TypeError, ValueError):
        return dict(_EMPTY_RESULT)

    try:
        facts = _load_facts(rid, is_custom)
    except Exception:
        return dict(_EMPTY_RESULT)

    if not facts.exists:
        return dict(_EMPTY_RESULT)

    flags = _run_deterministic(facts)

    # Optional AI pass — cached, never-raise, deterministic-only on failure.
    ai_flag = _maybe_ai_flag(rid, is_custom, facts)
    if ai_flag is not None:
        flags.append(ai_flag)

    return _finalize(flags)


def _load_facts(recap_id: int, is_custom: bool) -> _RecapFacts:
    """Fetch the recap (prefetching the children the checks read) and reduce it.

    Returns a not-``exists`` :class:`_RecapFacts` when the id doesn't resolve.
    Kept separate from :func:`recap_quality_flags` so the (synchronous) ORM work
    has one home the resolver can wrap in ``sync_to_async``.
    """
    if is_custom:
        from recaps.models import CustomRecap

        custom_recap = (
            CustomRecap.objects.filter(id=recap_id)
            .prefetch_related("custom_recap_files", "custom_field_value")
            .first()
        )
        if custom_recap is None:
            return _RecapFacts()
        return _gather_custom_facts(custom_recap)

    from recaps.models import Recap

    recap = (
        Recap.objects.filter(id=recap_id)
        .select_related("event")
        .prefetch_related("recap_files", "consumer_feedback", "account_feedback")
        .first()
    )
    if recap is None:
        return _RecapFacts()
    return _gather_legacy_facts(recap)
