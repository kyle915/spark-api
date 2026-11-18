"""First draft of a client recap deck for one brand and date range.

Staff ask for a draft; Spark assembles it; staff edit the words, swap the
photos and download a PDF. Nothing here emails, shares or publishes
anything: the draft is a row only Ignite staff can read
(:class:`recaps.models.ClientRecapDraft`).

What goes in:

* **Numbers** from the same roll-ups the dashboard reads
  (:func:`recaps.tenant_overview._tenant_kpi_totals_window`, the ``tenantKpis``
  source, and :func:`recaps.consumer_breakdown.tenant_consumer_breakdown`).
  They are counted in SQL / Python and handed to the model as labelled
  figures. The model is never asked to compute one.
* **What consumers said**: the comments and counted themes of
  ``recaps/feedback_themes.py``.
* **Photos**: the best photos from ``recaps/photo_picks.py``, staff pins
  first. When none have been picked yet, one field photo per recap chosen by
  its bucket, so a draft never depends on the scorer having run.
* **Words**: when the dates are a week or a month that already has a
  written client summary (``recaps/weekly_summary.py``), that text is the
  summary and no model is called. Otherwise a short summary and three to five takeaways written by the
  model from approved recaps only, with ambassador names taken out before
  the prompt is built. Every number in the reply must appear in the facts it
  was given (:func:`text_is_grounded`, on the checker in
  ``recaps/explain_text.py``); a reply
  that fails, or no reply at all, is replaced by sentences built from the
  facts.

The PDF reuses the WeasyPrint set-up, base styles and Spark mark of the
other report PDFs (:mod:`recaps.pdf`, :mod:`recaps.client_report`).
"""

from __future__ import annotations

import html as _html
import json
import logging
import re
from dataclasses import asdict
from datetime import date

from recaps.consumer_breakdown import tenant_consumer_breakdown
from recaps.models import (
    ClientRecapDraft,
    CustomRecap,
    Recap,
)
from recaps.scoped_ai import _ba_names, _hide_names
from recaps.tenant_overview import (
    _filter_event_window,
    _inclusive_dates_to_window,
    _tenant_event_recap_counts_window,
    _tenant_kpi_totals_window,
)
from recaps.weekly_narration import numbers_are_grounded
from utils.ai_text import AiUnavailable, generate_json

logger = logging.getLogger(__name__)

MAX_RANGE_DAYS = 400
# One for the cover and six for a page of photos; staff can add a second page.
DEFAULT_PHOTOS = 7
MAX_PHOTOS = 13
MIN_PHOTOS_BEFORE_FILL = 4
MAX_PHOTOS_PER_RECAP = 2
MAX_COMMENTS = 40
MAX_QUOTES_IN_PDF = 6
MAX_QUOTE_CHARS = 220
MAX_NEXT_STEPS = 3
MAX_NEXT_STEP_CHARS = 240
MAX_TAKEAWAYS = 5
MIN_TAKEAWAYS = 3
MAX_SUMMARY_CHARS = 1200
MAX_TAKEAWAY_CHARS = 240
MAX_TITLE_CHARS = 160
MAX_STORES = 6
MAX_STATES = 8
# Longest side of a photo embedded in the PDF.
PDF_PHOTO_MAX_DIM = 1200

# The nine summable KPIs, in the order and words the report prints them.
# ``consumers_reached`` is the count the dashboards call consumers sampled.
KPI_LABELS = (
    ("consumers_reached", "Consumers sampled"),
    ("samples_distributed", "Samples handed out"),
    ("total_engagements", "Engagements"),
    ("products_sold", "Products sold"),
    ("cans_sold", "Cans sold"),
    ("packs_sold", "Packs sold"),
    ("first_time_consumers", "First-time consumers"),
    ("brand_aware_consumers", "Already knew the brand"),
    ("willing_to_purchase", "Said they would buy"),
)

_SYSTEM = (
    "You write the first draft of a client recap for a field-marketing "
    "agency. Agency staff will edit it before a client sees it. "
    "Use ONLY numbers that appear in FACTS, each with the label already "
    "attached to it. Never add, subtract, divide, average, round or compare "
    "numbers yourself, and never write a percentage that is not in FACTS. "
    "Consumers sampled, samples handed out and engagements are different "
    "counts: do not swap them. Refer to the dates only by the 'period' text "
    "in FACTS, and mention no other date. Do not name or describe any person. COMMENTS are what "
    "consumers said, noted on approved recaps; you may refer to what they "
    "said but do not invent a quote. Plain business English: no hype, no "
    "exclamation marks, no adjectives the facts do not support. "
    "summary: 3 to 4 sentences. takeaways: 3 to 5 short sentences, each "
    "specific and supported by FACTS or COMMENTS. If FACTS cannot support a "
    "point, leave it out. 'program.demos' already leaves out no-product "
    "visits (stores with no product on hand); call them demos. "
    "next_steps: 0 to 3 short, practical suggestions for the brand's next "
    "program, each tied to something in FACTS or COMMENTS (a store or state "
    "that did well, something consumers asked for). No promises about "
    "results. Return an empty list when FACTS do not support one."
)

_TEXT_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["summary", "takeaways", "next_steps"],
    "properties": {
        "summary": {"type": "string"},
        "takeaways": {"type": "array", "items": {"type": "string"}},
        "next_steps": {"type": "array", "items": {"type": "string"}},
    },
}

class ClientRecapDraftError(RuntimeError):
    """A draft could not be made, saved or rendered. The message is safe to
    show staff."""


# ─── Facts ──────────────────────────────────────────────────────────


def period_label(start: date, end: date) -> str:
    """"Nov 1 – Nov 15, 2026" (or with both years when they differ)."""

    def _day(d: date, with_year: bool) -> str:
        text = f"{d.strftime('%b')} {d.day}"
        return f"{text}, {d.year}" if with_year else text

    if start == end:
        return _day(start, True)
    return f"{_day(start, start.year != end.year)} – {_day(end, True)}"


def _window_ba_names(tenant_id: int, window) -> list[str]:
    """Every name an ambassador goes by on this brand's recaps in the window,
    approved or not: a name can turn up in another recap's notes."""
    legacy = _filter_event_window(
        Recap.objects.filter(event__tenant_id=tenant_id), "event__", window
    ).select_related("ambassador__user")
    custom = _filter_event_window(
        CustomRecap.objects.filter(tenant_id=tenant_id), "event__", window
    ).select_related("ambassador__user")
    return _ba_names(list(legacy[:500]) + list(custom[:500]))


def approved_comments(
    tenant_id: int, start: date, end: date, *, skip_recaps: set[str] | None = None
) -> list[str]:
    """What consumers said, from approved recaps in the window, newest event
    first. The feedback-themes gatherer is used, so the deck and the themes
    page read the same comments: approved and not archived, with ambassador
    names, emails and phone numbers taken out.

    ``skip_recaps`` ("C123" / "L45") leaves out comments filed on those
    recaps: the no-product visits a deck does not count. What consumers said
    comes before the reasons they gave for not buying."""
    from recaps.feedback_themes import KIND_DECLINE, gather_comments

    comments, _cut = gather_comments(tenant_id, start, end)
    skip = skip_recaps or set()
    kept = [c for c in comments if c.recap not in skip]
    kept.sort(key=lambda c: 1 if c.kind == KIND_DECLINE else 0)
    return [c.text for c in kept][:MAX_COMMENTS]


def trim_quote(text: str, limit: int = MAX_QUOTE_CHARS) -> str:
    """A comment as a deck prints it: one line of spacing, no wrapping
    quote marks, and cut at a sentence (or else a word) when it runs long.
    Nothing is reworded."""
    cleaned = " ".join(str(text or "").split()).strip()
    cleaned = cleaned.strip("\"'“”‘’ ").strip()
    if len(cleaned) <= limit:
        return cleaned
    head = cleaned[:limit]
    sentence_end = max(head.rfind(". "), head.rfind("! "), head.rfind("? "))
    if sentence_end >= limit // 2:
        return head[: sentence_end + 1]
    return head[: head.rfind(" ")].rstrip(",;:- ") + "…" if " " in head else head + "…"


def feedback_themes(tenant_id: int, start: date, end: date) -> list[dict]:
    """Counted themes for the window from ``recaps/feedback_themes.py``:
    stored ones when the comments have not changed, otherwise built once.
    ``[]`` when there are too few comments or the model is unavailable; the
    deck then prints plain quotes."""
    from recaps.feedback_themes import get_feedback_themes

    try:
        data = get_feedback_themes(tenant_id, start, end)
    except Exception:  # noqa: BLE001
        logger.exception("client recap draft: feedback themes failed")
        return []
    if not isinstance(data, dict) or data.get("status") != "ready":
        return []
    themes = []
    for theme in data.get("themes") or []:
        if not isinstance(theme, dict) or not theme.get("label"):
            continue
        themes.append(
            {
                "section": str(theme.get("section") or ""),
                "label": str(theme["label"]),
                "recap_count": int(theme.get("recap_count") or 0),
                "quote": str(theme.get("quote") or ""),
            }
        )
    return themes


def build_facts(
    tenant,
    start: date,
    end: date,
    *,
    with_themes: bool = True,
    visits: dict | None = None,
    names: list[str] | None = None,
) -> dict:
    """Everything the draft states as fact, from the dashboard's roll-ups.

    ``with_themes=False`` skips the counted themes (a model call) so a big
    program can be put together inside one request and finished later
    (:mod:`recap_decks.builder`). ``visits`` is the no-product scan
    (:func:`recap_decks.no_product.no_product_visits`); it is run here when
    not given."""
    from recap_decks.no_product import excluded_keys, no_product_visits

    window = _inclusive_dates_to_window(start, end)
    events, recaps = _tenant_event_recap_counts_window(tenant.id, window)
    kpis = asdict(_tenant_kpi_totals_window(tenant.id, window))
    breakdown = tenant_consumer_breakdown(tenant.id, start, end)

    numbers = [
        {"key": "events", "label": "Events", "value": int(events)},
        {"key": "recaps", "label": "Approved recaps", "value": int(recaps)},
    ]
    for key, label in KPI_LABELS:
        value = int(kpis.get(key) or 0)
        # A zero here is far more often a field the brand's form does not
        # collect than a real zero, so it is left out rather than printed.
        if value > 0:
            numbers.append({"key": key, "label": label, "value": value})

    def _named(rows, limit):
        # "venue" rows are festivals / activations named by the walk-in
        # title: real places, flagged so the draft doesn't call them stores.
        return [
            {
                "label": r["label"],
                "consumers_sampled": int(r["consumers"]),
                "recaps": int(r["recaps"]),
                **({"event_venue": True} if r.get("kind") == "venue" else {}),
            }
            for r in rows
            if r.get("kind") in ("named", "venue") and r.get("label")
        ][:limit]

    if visits is None:
        visits = no_product_visits(tenant.id, start, end, names or [])
    store_count = int(breakdown.get("store_count") or 0)
    state_count = int(breakdown.get("state_count") or 0)
    return {
        "brand": tenant.name or "",
        "period": period_label(start, end),
        "numbers": numbers,
        "program": program_figures(
            recaps=int(recaps),
            consumers=int(kpis.get("consumers_reached") or 0),
            no_product=int(visits.get("excluded_count") or 0),
            store_count=store_count,
            state_count=state_count,
        ),
        "visits": visits,
        "consumers_sampled_by_week": [
            {
                "week_of": w["week_start"],
                "consumers_sampled": int(w["consumers"]),
                "recaps": int(w["recaps"]),
            }
            for w in breakdown.get("by_week") or []
        ],
        "top_stores": _named(breakdown.get("by_store") or [], MAX_STORES),
        "states": _named(breakdown.get("by_state") or [], MAX_STATES),
        "store_count": store_count,
        "state_count": state_count,
        "comments": approved_comments(
            tenant.id, start, end, skip_recaps=excluded_keys(visits)
        ),
        "themes": feedback_themes(tenant.id, start, end) if with_themes else [],
        "labels": (
            "Consumers sampled, samples handed out and engagements are "
            "separate counts and must not be swapped."
        ),
    }


def program_figures(
    *, recaps: int, consumers: int, no_product: int, store_count: int, state_count: int
) -> dict:
    """The deck's own counts, worked out here and never by the model.

    ``demos`` is the approved recaps less the no-product visits; a left-out
    visit recorded nothing, so no total changes. ``consumers_per_demo`` is
    consumers sampled over demos, rounded to a whole number."""
    demos = max(0, int(recaps) - int(no_product))
    per_demo = round(consumers / demos) if demos and consumers else 0
    return {
        "demos": demos,
        "no_product_visits": max(0, int(no_product)),
        "consumers_per_demo": int(per_demo),
        "store_count": int(store_count),
        "state_count": int(state_count),
    }


def _number(facts: dict, key: str) -> int:
    for item in facts.get("numbers") or []:
        if item.get("key") == key:
            return int(item.get("value") or 0)
    return 0


# ─── Words ──────────────────────────────────────────────────────────


def fallback_text(facts: dict) -> tuple[str, list[str]]:
    """Summary and takeaways built only from the facts. Used when the model
    is unavailable or its reply does not hold up."""
    brand = facts.get("brand") or "The brand"
    events = _number(facts, "events")
    recaps = _number(facts, "recaps")
    sentences = [
        f"{brand} ran {events:,} event{'s' if events != 1 else ''} in {facts.get('period')}, "
        f"with {recaps:,} approved recap{'s' if recaps != 1 else ''}."
    ]
    for item in (facts.get("numbers") or [])[2:5]:
        sentences.append(f"{item['label']}: {int(item['value']):,}.")
    program = facts.get("program") or {}
    left_out = int(program.get("no_product_visits") or 0)
    if left_out:
        sentences.append(
            f"{left_out:,} no-product visit{'s are' if left_out != 1 else ' is'} left out of "
            f"the demo count, which is {int(program.get('demos') or 0):,}."
        )
    takeaways: list[str] = []
    stores = facts.get("top_stores") or []
    if stores:
        top = stores[0]
        takeaways.append(
            f"{top['label']} had the most consumers sampled: {top['consumers_sampled']:,} "
            f"across {top['recaps']:,} recap{'s' if top['recaps'] != 1 else ''}."
        )
    states = facts.get("states") or []
    if states:
        top = states[0]
        takeaways.append(
            f"{top['label']} led by state with {top['consumers_sampled']:,} consumers sampled."
        )
    weeks = [w for w in facts.get("consumers_sampled_by_week") or [] if w["consumers_sampled"] > 0]
    if weeks:
        best = max(weeks, key=lambda w: w["consumers_sampled"])
        takeaways.append(
            f"The busiest week began {best['week_of']}: "
            f"{best['consumers_sampled']:,} consumers sampled."
        )
    for item in (facts.get("numbers") or [])[5:]:
        if len(takeaways) >= MAX_TAKEAWAYS:
            break
        takeaways.append(f"{item['label']}: {int(item['value']):,}.")
    if facts.get("comments") and len(takeaways) < MAX_TAKEAWAYS:
        count = len(facts["comments"])
        takeaways.append(
            f"{count:,} consumer comment{'s' if count != 1 else ''} noted on approved recaps; "
            "a selection is on the comments page."
        )
    return " ".join(sentences), takeaways[:MAX_TAKEAWAYS]


def text_is_grounded(text: str, facts: dict) -> bool:
    """Every figure in ``text`` is one of the facts.

    Two checks. :func:`recaps.weekly_narration.numbers_are_grounded` keeps
    the label rules (no "conversion" or "samples handed out" unless the
    facts have them). :func:`recaps.explain_text.unsupported_numbers` then
    checks digits, number words and ratio words against the counted values,
    the period label and the consumer comments only. The weekly dates are
    left out of that list on purpose: "12%" must not pass because a week
    began on the 12th.
    """
    from recaps.explain_text import unsupported_numbers

    if not numbers_are_grounded(text, facts):
        return False
    values: list[str] = [str(int(item.get("value") or 0)) for item in facts.get("numbers") or []]
    for row in (facts.get("top_stores") or []) + (facts.get("states") or []):
        values.append(str(int(row.get("consumers_sampled") or 0)))
        values.append(str(int(row.get("recaps") or 0)))
    values.append(str(int(facts.get("store_count") or 0)))
    values.append(str(int(facts.get("state_count") or 0)))
    for value in (facts.get("program") or {}).values():
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            values.append(str(int(value)))
    supplied = " ; ".join(
        [str(facts.get("period") or ""), *values, *(facts.get("comments") or [])]
    )
    return not unsupported_numbers(text, supplied)


def _clean_line(raw, limit: int, names: list[str]) -> str:
    text = " ".join(str(raw or "").split()).strip()
    text = _hide_names(text, names)
    if len(text) > limit:
        text = text[: limit - 1].rstrip() + "…"
    return text


def stored_period_summary(tenant_id: int, start: date, end: date) -> str:
    """The weekly or monthly client summary already written for exactly
    these dates (``recaps/weekly_summary.py``), or "". Staff's edited text
    wins over the generated one, and a reviewed row over a draft."""
    from tenants.models import TenantWeeklySummary

    rows = list(
        TenantWeeklySummary.objects.filter(tenant_id=tenant_id, week_start=start, week_end=end)
    )
    rows.sort(key=lambda r: 0 if r.status == TenantWeeklySummary.STATUS_REVIEWED else 1)
    for row in rows:
        text = (row.edited_body or row.body or "").strip()
        if text:
            return text
    return ""


def compose_text(
    facts: dict, names: list[str] | None = None, *, generate=None
) -> tuple[str, list[str], str]:
    """``(summary, takeaways, source)``. ``source`` is ``model`` when the
    model's reply was kept, otherwise ``fallback``."""
    words = compose_deck_text(facts, names, generate=generate)
    return words["summary"], words["takeaways"], words["source"]


def compose_deck_text(facts: dict, names: list[str] | None = None, *, generate=None) -> dict:
    """``{summary, takeaways, next_steps, source}`` from one model call.

    The summary and every takeaway and next step must pass
    :func:`text_is_grounded`. A summary that fails, or no reply, gives the
    sentences built from the facts and no next steps (they are optional).
    """
    names = names or []
    fb_summary, fb_takeaways = fallback_text(facts)
    fallback = {
        "summary": fb_summary,
        "takeaways": fb_takeaways,
        "next_steps": [],
        "source": ClientRecapDraft.SOURCE_FALLBACK,
    }
    if _number(facts, "recaps") == 0:
        return fallback
    generate = generate or generate_json
    # The weekly series stays out of the prompt: its dates are numbers the
    # model could mistake for figures. It is still drawn in the PDF. The
    # visit list (stores, dates, notes) is for staff, not the model.
    prompt_facts = {
        k: v
        for k, v in facts.items()
        if k not in ("comments", "themes", "consumers_sampled_by_week", "visits")
    }
    user = (
        "FACTS:\n"
        + json.dumps(prompt_facts, indent=2)
        + "\n\nCOMMENTS (from approved recaps):\n"
        + ("\n".join(f"- {c}" for c in facts.get("comments") or []) or "- none recorded")
    )
    try:
        raw = generate(_SYSTEM, user, schema=_TEXT_SCHEMA, max_tokens=1200)
    except AiUnavailable:
        raw = None
    except Exception:  # noqa: BLE001
        logger.exception("client recap draft: model call failed")
        raw = None
    if not isinstance(raw, dict):
        return fallback

    summary = _clean_line(raw.get("summary"), MAX_SUMMARY_CHARS, names)
    if not summary or not text_is_grounded(summary, facts):
        return fallback
    next_steps: list[str] = []
    for item in raw.get("next_steps") if isinstance(raw.get("next_steps"), list) else []:
        line = _clean_line(item, MAX_NEXT_STEP_CHARS, names)
        if line and text_is_grounded(line, facts) and line not in next_steps:
            next_steps.append(line)
    takeaways: list[str] = []
    for item in raw.get("takeaways") if isinstance(raw.get("takeaways"), list) else []:
        line = _clean_line(item, MAX_TAKEAWAY_CHARS, names)
        # A takeaway with a number that is not in the facts is dropped.
        if line and text_is_grounded(line, facts) and line not in takeaways:
            takeaways.append(line)
    takeaways = takeaways[:MAX_TAKEAWAYS]
    for line in fb_takeaways:
        if len(takeaways) >= MIN_TAKEAWAYS:
            break
        if line not in takeaways:
            takeaways.append(line)
    return {
        "summary": summary,
        "takeaways": takeaways,
        "next_steps": next_steps[:MAX_NEXT_STEPS],
        "source": ClientRecapDraft.SOURCE_MODEL,
    }


# ─── Photos ─────────────────────────────────────────────────────────


def _eligible_photos(tenant_id: int):
    """Images a client may be shown: on approved, live recaps, not paperwork."""
    from recaps.photo_gallery import _base_queryset
    from recaps.types import _PAPERWORK_CATEGORY_RE

    return _base_queryset(tenant_id, None, None).exclude(
        file_recap_category__name__iregex=_PAPERWORK_CATEGORY_RE.pattern
    )


# Why the scorer set a photo aside that also rules it out of a deck: it
# is not a field photo, or it would not print.
_UNUSABLE_SKIPS = frozenset(
    {"receipt", "screenshot", "low_resolution", "not_an_image", "missing_file"}
)


def photo_is_usable(recap_file, skip_recaps: set[int] | None = None) -> bool:
    """A photo a deck may print: not a receipt or screenshot, not flagged
    unreadable or too small by the scorer, and not from a no-product visit
    (``skip_recaps`` are custom recap ids). Staff pins do not override this."""
    from recaps.photo_picks import _related, is_receipt

    if skip_recaps and recap_file.custom_recap_id in skip_recaps:
        return False
    if is_receipt(recap_file):
        return False
    score = _related(recap_file, "photo_score")
    if score is None:
        return True
    if score.status == "unreadable" or score.kind == "screenshot":
        return False
    return not (score.status == "skipped" and score.skip_reason in _UNUSABLE_SKIPS)


def default_photo_ids(
    tenant_id: int,
    start: date,
    end: date,
    limit: int = DEFAULT_PHOTOS,
    *,
    skip_recaps: set[int] | None = None,
) -> list[int]:
    """Photos for a new draft: staff pins first, then the scorer's picks by
    score, at most two per recap. Topped up with one field photo per recap
    when few have been picked. Never an unusable photo or one from a
    no-product visit (:func:`photo_is_usable`)."""
    from recaps.photo_gallery import _base_queryset
    from recaps.photo_picks import (
        _related,
        category_kind,
        effective_pick_q,
        is_receipt,
    )
    from recaps.types import _PAPERWORK_CATEGORY_RE

    base = (
        _base_queryset(tenant_id, start, end)
        .exclude(file_recap_category__name__iregex=_PAPERWORK_CATEGORY_RE.pattern)
        .select_related("file_recap_category", "photo_score", "staff_pick", "receipt_reading")
    )
    picks = list(base.filter(effective_pick_q()).order_by("-custom_recap__event__date", "id")[:200])

    def _rank(f):
        staff = _related(f, "staff_pick")
        score = _related(f, "photo_score")
        pinned = 0 if (staff is not None and staff.choice == "pinned") else 1
        return (pinned, -(score.score or 0) if score is not None else 0, -f.id)

    chosen: list[int] = []
    per_recap: dict[int, int] = {}
    for f in sorted(picks, key=_rank):
        if len(chosen) >= limit:
            break
        if not photo_is_usable(f, skip_recaps):
            continue
        if per_recap.get(f.custom_recap_id, 0) >= MAX_PHOTOS_PER_RECAP:
            continue
        per_recap[f.custom_recap_id] = per_recap.get(f.custom_recap_id, 0) + 1
        chosen.append(f.id)
    if len(chosen) >= MIN_PHOTOS_BEFORE_FILL:
        return chosen

    # Too few picks (the scorer has not run, or is unavailable): one field
    # photo per recap by its bucket, newest event first.
    order = {"sampling": 0, "table_setup": 1, "shelf": 2, "other": 3}
    best: dict[int, tuple] = {}
    for f in base.order_by("-custom_recap__event__date", "-custom_recap_id", "id")[:400]:
        if f.id in chosen or f.custom_recap_id in per_recap or is_receipt(f):
            continue
        if not photo_is_usable(f, skip_recaps):
            continue
        staff = _related(f, "staff_pick")
        score = _related(f, "photo_score")
        if staff is not None and staff.choice == "unpinned":
            continue
        if score is not None and score.status != "scored":
            continue
        rank = order.get(category_kind(getattr(f.file_recap_category, "name", "")), 3)
        current = best.get(f.custom_recap_id)
        if current is None or rank < current[0]:
            best[f.custom_recap_id] = (rank, f.id)
    for _rank_value, file_id in best.values():
        if len(chosen) >= limit:
            break
        chosen.append(file_id)
    return chosen


def clean_photo_ids(tenant_id: int, raw_ids, *, skip_recaps: set[int] | None = None) -> list[int]:
    """The ids that are this brand's client-visible photos, in the order
    given, without repeats, capped at :data:`MAX_PHOTOS`. Unusable photos and
    photos from no-product visits are refused (:func:`photo_is_usable`)."""
    wanted: list[int] = []
    for raw in raw_ids or []:
        try:
            value = int(raw)
        except (TypeError, ValueError):
            continue
        if value not in wanted:
            wanted.append(value)
    if not wanted:
        return []
    allowed = {
        f.id
        for f in _eligible_photos(tenant_id)
        .filter(id__in=wanted)
        .select_related("file_recap_category", "photo_score", "receipt_reading")
        if photo_is_usable(f, skip_recaps)
    }
    return [i for i in wanted if i in allowed][:MAX_PHOTOS]


def _store_name(event) -> str:
    """The store, from the retailer or the request. Never the raw event
    title: those read like "Sampling-Frys Scottsdale-07/03/2026-121"."""
    request = getattr(event, "request", None)
    for name in (
        getattr(getattr(event, "retailer", None), "name", None),
        getattr(getattr(request, "retailer", None), "name", None),
        getattr(request, "retailer_name", None),
    ):
        cleaned = (name or "").strip()
        if cleaned:
            return cleaned
    return ""


def draft_photos(draft: ClientRecapDraft) -> list[dict]:
    """The draft's photos, in order, with what the screen and the PDF show.
    A photo that is no longer client-visible, or no longer usable in a deck,
    is left out."""
    from recap_decks.builder import deck_skip_recaps
    from recaps.photo_gallery import _image_url
    from recaps.photo_picks import _related, is_effective_pick

    skip = deck_skip_recaps(draft)

    ids = [int(i) for i in (draft.photo_ids or []) if str(i).isdigit()]
    if not ids:
        return []
    rows = {
        f.id: f
        for f in _eligible_photos(draft.tenant_id)
        .filter(id__in=ids)
        .select_related(
            "file_recap_category",
            "custom_recap__event__retailer",
            "custom_recap__event__request__retailer",
            "custom_recap__event__state",
            "photo_score",
            "staff_pick",
            "receipt_reading",
        )
    }
    photos: list[dict] = []
    for file_id in ids:
        f = rows.get(file_id)
        if f is None or not photo_is_usable(f, skip):
            continue
        url = _image_url(f)
        if not url:
            continue
        event = getattr(f.custom_recap, "event", None)
        when = getattr(event, "date", None) or getattr(event, "start_time", None)
        score = _related(f, "photo_score")
        photos.append(
            {
                "id": str(f.id),
                "url": url,
                "category": getattr(f.file_recap_category, "name", None) or "",
                "caption": (score.caption if score is not None else "") or "",
                "store": _store_name(event) if event is not None else "",
                "state": getattr(getattr(event, "state", None), "code", None) or "",
                "event_date": when.date().isoformat() if when else "",
                "is_pick": is_effective_pick(f),
                "_file": f,
            }
        )
    return photos


# ─── Draft rows ─────────────────────────────────────────────────────


def create_draft(
    tenant, start: date, end: date, user, *, finish_words: bool = True
) -> ClientRecapDraft:
    """Assemble and store a new draft and its deck row. One model call at
    most, plus the counted themes.

    ``finish_words=False`` (a big program) stores the numbers, photos and
    no-product visits now, with words built from the numbers, and leaves the
    deck ``building``: :func:`recap_decks.builder.finish_build` writes the
    summary, takeaways and next steps later, outside this request."""
    from recap_decks.models import ClientRecapDeck
    from recap_decks.no_product import excluded_custom_ids, no_product_visits

    if end < start:
        raise ClientRecapDraftError("The end date is before the start date.")
    if (end - start).days > MAX_RANGE_DAYS:
        raise ClientRecapDraftError("Pick a range of 13 months or less.")
    names = _window_ba_names(tenant.id, _inclusive_dates_to_window(start, end))
    visits = no_product_visits(tenant.id, start, end, names)
    facts = build_facts(
        tenant, start, end, with_themes=finish_words, visits=visits, names=names
    )
    stored = stored_period_summary(tenant.id, start, end)
    next_steps: list[str] = []
    status = ClientRecapDeck.STATUS_DRAFT
    if stored:
        # These dates already have a written client summary: use it rather
        # than paying for, and reviewing, a second one. No model call.
        summary = _hide_names(stored, names)[: MAX_SUMMARY_CHARS * 2]
        takeaways = fallback_text(facts)[1]
        source = ClientRecapDraft.SOURCE_SUMMARY
    elif finish_words:
        words = compose_deck_text(facts, names)
        summary, takeaways, source = words["summary"], words["takeaways"], words["source"]
        next_steps = words["next_steps"]
    else:
        summary, takeaways = fallback_text(facts)
        source = ClientRecapDraft.SOURCE_FALLBACK
        status = ClientRecapDeck.STATUS_BUILDING
    draft = ClientRecapDraft.objects.create(
        tenant=tenant,
        start_date=start,
        end_date=end,
        title=f"{tenant.name or 'Client'} recap"[:MAX_TITLE_CHARS],
        summary=summary,
        takeaways=takeaways,
        text_source=source,
        facts=facts,
        photo_ids=default_photo_ids(
            tenant.id, start, end, skip_recaps=excluded_custom_ids(visits)
        ),
        created_by=user,
        updated_by=user,
    )
    ClientRecapDeck.objects.create(draft=draft, status=status, next_steps=next_steps)
    return draft


def update_draft(
    draft: ClientRecapDraft,
    user,
    *,
    title: str | None = None,
    summary: str | None = None,
    takeaways: list[str] | None = None,
    photo_ids: list | None = None,
) -> ClientRecapDraft:
    """Save staff edits. Only what is passed changes."""
    edited = False
    if title is not None:
        cleaned = " ".join(title.split())[:MAX_TITLE_CHARS]
        if not cleaned:
            raise ClientRecapDraftError("The title can't be blank.")
        draft.title = cleaned
    if summary is not None:
        cleaned = summary.strip()[:MAX_SUMMARY_CHARS * 2]
        edited = edited or cleaned != draft.summary
        draft.summary = cleaned
    if takeaways is not None:
        cleaned_list = [
            " ".join(str(t).split())[: MAX_TAKEAWAY_CHARS * 2]
            for t in takeaways
            if str(t or "").strip()
        ][:8]
        edited = edited or cleaned_list != list(draft.takeaways or [])
        draft.takeaways = cleaned_list
    if photo_ids is not None:
        from recap_decks.builder import deck_skip_recaps

        draft.photo_ids = clean_photo_ids(
            draft.tenant_id, photo_ids, skip_recaps=deck_skip_recaps(draft)
        )
    if edited:
        draft.text_source = ClientRecapDraft.SOURCE_EDITED
    draft.updated_by = user
    draft.save()
    return draft


# ─── PDF ────────────────────────────────────────────────────────────


def _esc(value) -> str:
    return _html.escape(str(value if value is not None else ""))


def _short_date(iso: str) -> str:
    try:
        d = date.fromisoformat(iso)
    except (TypeError, ValueError):
        return ""
    return f"{d.strftime('%b')} {d.day}"


def _embed_photos(photos: list[dict]) -> list[tuple[dict, str]]:
    """Download, downscale and inline each photo. One that cannot be fetched
    is left out rather than failing the PDF."""
    from recaps.heic_conversion import display_blob_name, is_heic_blob
    from recaps.pdf import bytes_to_data_uri, downscale_image_bytes
    from recaps.photo_picks import _stored_path
    from utils.gcs import download_blob_bytes, extract_blob_name_from_url

    out: list[tuple[dict, str]] = []
    for photo in photos[:MAX_PHOTOS]:
        blob = extract_blob_name_from_url(_stored_path(photo["_file"])) or ""
        if not blob:
            continue
        try:
            data = download_blob_bytes(display_blob_name(blob) if is_heic_blob(blob) else blob)
        except Exception:  # noqa: BLE001
            data = None
        if not data:
            continue
        uri = bytes_to_data_uri(downscale_image_bytes(data, max_dim=PDF_PHOTO_MAX_DIM, quality=78))
        if uri:
            out.append((photo, uri))
    return out


def build_draft_html(draft: ClientRecapDraft, embedded: list[tuple[dict, str]]) -> str:
    """The deck as one HTML document (:mod:`recap_decks.deck_html`).

    Everything it prints is client-safe by the time it gets here: numbers
    from the stored facts (the dashboard roll-ups), store names from the
    retailer (never a raw event title), quotes with ambassador names taken
    out, and no evidence from the no-product visits beyond the reason."""
    from recap_decks import builder
    from recap_decks.deck_html import build_deck_html
    from recaps.pdf import _spark_mark_src

    facts = draft.facts or {}
    brand = facts.get("brand") or getattr(draft.tenant, "name", "") or ""
    period = facts.get("period") or period_label(draft.start_date, draft.end_date)
    figures = builder.program(draft)
    deck = builder.deck_for(draft)

    weeks = [
        (_short_date(w["week_of"]), int(w["consumers_sampled"]))
        for w in facts.get("consumers_sampled_by_week") or []
    ]
    stores = [(s["label"], int(s["consumers_sampled"])) for s in facts.get("top_stores") or []]
    states = [(s["label"], int(s["consumers_sampled"])) for s in facts.get("states") or []]
    where_bits = []
    if figures["demos"]:
        where_bits.append(f"{figures['demos']:,} demo{'s' if figures['demos'] != 1 else ''}")
    if figures["store_count"]:
        where_bits.append(
            f"at {figures['store_count']:,} store{'s' if figures['store_count'] != 1 else ''}"
        )
    if figures["state_count"]:
        where_bits.append(
            f"in {figures['state_count']:,} state{'s' if figures['state_count'] != 1 else ''}"
        )
    where_line = (" ".join(where_bits) + ".") if where_bits else ""
    if where_line:
        where_line = where_line[0].upper() + where_line[1:]

    basis = (
        f"Approved recaps for {period}, by the date of each event. "
        "Totals are the same as the Spark dashboard for these dates."
    )
    note = builder.no_product_note(draft)
    if note:
        basis = f"{basis} {note}"

    visits = facts.get("visits") or {}
    method = [
        f"Approved recaps only, placed by the date of each event ({period}).",
        "Totals match the Spark dashboard for the same dates.",
        (
            f"Demos leave out {figures['no_product_visits']:,} no-product "
            f"visit{'s' if figures['no_product_visits'] != 1 else ''}: stores with no product "
            "on hand. Those visits recorded nothing, so no total changes."
            if figures["no_product_visits"]
            else "Every approved recap in these dates is counted as a demo."
        ),
    ]
    if figures["consumers_per_demo"]:
        method.append("Consumers per demo is consumers sampled divided by demos, rounded.")
    method.append(
        "Consumer comments are as noted on approved recaps, trimmed for length. "
        "Ambassadors are not named."
    )
    if visits.get("capped"):
        method.append("The newest 5,000 recaps were checked for no-product visits.")
    left_out = [v for v in builder.left_out_visits(draft) if not v.get("kept")]

    themes = facts.get("themes") or []
    hidden = {str(q) for q in deck.hidden_quotes or []}
    shown_themes = [
        t for t in themes if trim_quote(t.get("quote") or "") not in hidden or not t.get("quote")
    ]
    return build_deck_html(
        brand=brand,
        title=draft.title or f"{brand} recap",
        period=period,
        tiles=builder.deck_numbers(draft),
        basis_note=basis,
        summary=draft.summary or "",
        takeaways=[str(t) for t in draft.takeaways or []],
        weeks=weeks,
        stores=stores,
        states=states,
        where_line=where_line,
        themes=[
            {**t, "quote": trim_quote(t.get("quote") or "")} for t in shown_themes
        ],
        quotes=[q["text"] for q in builder.deck_quotes(draft)],
        photos=embedded,
        next_steps=[str(s) for s in deck.next_steps or []],
        method=method,
        left_out=left_out,
        left_out_total=max(len(left_out), figures["no_product_visits"]),
        mark=_spark_mark_src(),
        mark_on_dark=_spark_mark_src(invert=True),
    )


def _deck_css() -> str:
    from recap_decks.deck_html import DECK_CSS

    return DECK_CSS


# Kept under its old name for importers and tests.
_DRAFT_CSS = _deck_css()


def render_draft_pdf(draft: ClientRecapDraft) -> bytes:
    """The deck as PDF bytes. Raises :class:`ClientRecapDraftError` when the
    renderer is not available, like the other report PDFs."""
    embedded = _embed_photos(draft_photos(draft))
    document = build_draft_html(draft, embedded)
    try:
        from weasyprint import CSS, HTML
    except Exception as exc:  # native deps missing / import-time failure
        raise ClientRecapDraftError("PDF renderer is unavailable on this server.") from exc
    try:
        css = CSS(string=_deck_css())
        return HTML(string=document).write_pdf(stylesheets=[css])
    except Exception as exc:  # noqa: BLE001
        raise ClientRecapDraftError("The PDF could not be made.") from exc


def pdf_filename(draft: ClientRecapDraft) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", (draft.tenant.name or "client").lower()).strip("-") or "client"
    return f"{slug}-recap-{draft.start_date.isoformat()}-to-{draft.end_date.isoformat()}.pdf"
