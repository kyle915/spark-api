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
    "point, leave it out."
)

_TEXT_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["summary", "takeaways"],
    "properties": {
        "summary": {"type": "string"},
        "takeaways": {"type": "array", "items": {"type": "string"}},
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


def approved_comments(tenant_id: int, start: date, end: date) -> list[str]:
    """What consumers said, from approved recaps in the window, newest event
    first. The feedback-themes gatherer is used, so the deck and the themes
    page read the same comments: approved and not archived, with ambassador
    names, emails and phone numbers taken out."""
    from recaps.feedback_themes import gather_comments

    comments, _cut = gather_comments(tenant_id, start, end)
    return [c.text for c in comments][:MAX_COMMENTS]


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


def build_facts(tenant, start: date, end: date) -> dict:
    """Everything the draft states as fact, from the dashboard's roll-ups."""
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
        return [
            {
                "label": r["label"],
                "consumers_sampled": int(r["consumers"]),
                "recaps": int(r["recaps"]),
            }
            for r in rows
            if r.get("kind") == "named" and r.get("label")
        ][:limit]

    return {
        "brand": tenant.name or "",
        "period": period_label(start, end),
        "numbers": numbers,
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
        "store_count": int(breakdown.get("store_count") or 0),
        "state_count": int(breakdown.get("state_count") or 0),
        "comments": approved_comments(tenant.id, start, end),
        "themes": feedback_themes(tenant.id, start, end),
        "labels": (
            "Consumers sampled, samples handed out and engagements are "
            "separate counts and must not be swapped."
        ),
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
    names = names or []
    fb_summary, fb_takeaways = fallback_text(facts)
    if _number(facts, "recaps") == 0:
        return fb_summary, fb_takeaways, ClientRecapDraft.SOURCE_FALLBACK
    generate = generate or generate_json
    # The weekly series stays out of the prompt: its dates are numbers the
    # model could mistake for figures. It is still drawn in the PDF.
    prompt_facts = {
        k: v
        for k, v in facts.items()
        if k not in ("comments", "themes", "consumers_sampled_by_week")
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
        return fb_summary, fb_takeaways, ClientRecapDraft.SOURCE_FALLBACK

    summary = _clean_line(raw.get("summary"), MAX_SUMMARY_CHARS, names)
    if not summary or not text_is_grounded(summary, facts):
        return fb_summary, fb_takeaways, ClientRecapDraft.SOURCE_FALLBACK
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
    return summary, takeaways, ClientRecapDraft.SOURCE_MODEL


# ─── Photos ─────────────────────────────────────────────────────────


def _eligible_photos(tenant_id: int):
    """Images a client may be shown: on approved, live recaps, not paperwork."""
    from recaps.photo_gallery import _base_queryset
    from recaps.types import _PAPERWORK_CATEGORY_RE

    return _base_queryset(tenant_id, None, None).exclude(
        file_recap_category__name__iregex=_PAPERWORK_CATEGORY_RE.pattern
    )


def default_photo_ids(tenant_id: int, start: date, end: date, limit: int = DEFAULT_PHOTOS) -> list[int]:
    """Photos for a new draft: staff pins first, then the scorer's picks by
    score, at most two per recap. Topped up with one field photo per recap
    when few have been picked."""
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


def clean_photo_ids(tenant_id: int, raw_ids) -> list[int]:
    """The ids that are this brand's client-visible photos, in the order
    given, without repeats, capped at :data:`MAX_PHOTOS`."""
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
    allowed = set(
        _eligible_photos(tenant_id).filter(id__in=wanted).values_list("id", flat=True)
    )
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
    A photo that is no longer client-visible is left out."""
    from recaps.photo_gallery import _image_url
    from recaps.photo_picks import _related, is_effective_pick

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
        if f is None:
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


def create_draft(tenant, start: date, end: date, user) -> ClientRecapDraft:
    """Assemble and store a new draft. One model call at most."""
    if end < start:
        raise ClientRecapDraftError("The end date is before the start date.")
    if (end - start).days > MAX_RANGE_DAYS:
        raise ClientRecapDraftError("Pick a range of 13 months or less.")
    facts = build_facts(tenant, start, end)
    names = _window_ba_names(tenant.id, _inclusive_dates_to_window(start, end))
    stored = stored_period_summary(tenant.id, start, end)
    if stored:
        # These dates already have a written client summary: use it rather
        # than paying for, and reviewing, a second one. No model call.
        summary = _hide_names(stored, names)[: MAX_SUMMARY_CHARS * 2]
        takeaways = fallback_text(facts)[1]
        source = ClientRecapDraft.SOURCE_SUMMARY
    else:
        summary, takeaways, source = compose_text(facts, names)
    return ClientRecapDraft.objects.create(
        tenant=tenant,
        start_date=start,
        end_date=end,
        title=f"{tenant.name or 'Client'} recap"[:MAX_TITLE_CHARS],
        summary=summary,
        takeaways=takeaways,
        text_source=source,
        facts=facts,
        photo_ids=default_photo_ids(tenant.id, start, end),
        created_by=user,
        updated_by=user,
    )


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
        draft.photo_ids = clean_photo_ids(draft.tenant_id, photo_ids)
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


def _bars(rows: list[tuple[str, int]], unit: str) -> str:
    """Horizontal bars, drawn with plain boxes. One accent, no gradient."""
    peak = max((value for _label, value in rows), default=0)
    if peak <= 0:
        return ""
    out = []
    for label, value in rows:
        width = max(1.5, round(100 * value / peak, 1))
        out.append(
            "<div class='bar-row'>"
            f"<div class='bar-label'>{_esc(label)}</div>"
            f"<div class='bar-track'><div class='bar-fill' style='width:{width}%'></div></div>"
            f"<div class='bar-value'>{value:,}</div>"
            "</div>"
        )
    return f"<div class='bars' aria-label='{_esc(unit)}'>{''.join(out)}</div>"


def _photo_figure(photo: dict, data_uri: str) -> str:
    lines = []
    if photo.get("caption"):
        lines.append(f"<span class='cap-main'>{_esc(photo['caption'])}</span>")
    where = " · ".join(
        part
        for part in (photo.get("store"), photo.get("state"), _short_date(photo.get("event_date")))
        if part
    )
    if where:
        lines.append(f"<span class='cap-meta'>{_esc(where)}</span>")
    caption = f"<figcaption>{''.join(lines)}</figcaption>" if lines else ""
    return f"<figure><div class='frame'><img src='{data_uri}' /></div>{caption}</figure>"


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
    """The deck as one HTML document. Each section starts a new page."""
    from recaps.pdf import _spark_mark_src

    facts = draft.facts or {}
    brand = facts.get("brand") or getattr(draft.tenant, "name", "") or ""
    period = facts.get("period") or period_label(draft.start_date, draft.end_date)
    mark = _spark_mark_src()

    def _header(title: str) -> str:
        return (
            "<header class='page-head'>"
            f"<p class='eyebrow'>{_esc(brand)} · {_esc(period)}</p>"
            f"<h2>{_esc(title)}</h2><div class='accent'></div></header>"
        )

    pages: list[str] = []

    # 1. Cover.
    hero = ""
    if embedded:
        hero = f"<div class='hero'><img src='{embedded[0][1]}' /></div>"
    pages.append(
        "<section class='page cover'>"
        f"<img class='spark-mark' src='{mark}' alt='Spark' />"
        f"<p class='eyebrow'>{_esc(brand)} · Client recap</p>"
        f"<h1>{_esc(draft.title or brand)}</h1>"
        f"<p class='period'>{_esc(period)}</p><div class='accent'></div>"
        f"{hero}"
        "<p class='prepared'>Prepared by Ignite Productions</p>"
        "</section>"
    )

    # 2. At a glance: the numbers, then the words.
    tiles = "".join(
        f"<div class='kpi'><strong>{int(n['value']):,}</strong><span>{_esc(n['label'])}</span></div>"
        for n in (facts.get("numbers") or [])[:8]
    )
    takeaways = "".join(f"<li>{_esc(t)}</li>" for t in (draft.takeaways or []) if str(t).strip())
    summary = "".join(
        f"<p>{_esc(par)}</p>" for par in str(draft.summary or "").split("\n") if par.strip()
    )
    pages.append(
        "<section class='page'>"
        + _header("At a glance")
        + (f"<div class='kpi-grid'>{tiles}</div>" if tiles else "")
        + (f"<div class='summary'>{summary}</div>" if summary else "")
        + (f"<h3>Takeaways</h3><ol class='takeaways'>{takeaways}</ol>" if takeaways else "")
        + "</section>"
    )

    # 3. When and where.
    weeks = [
        (f"Week of {_short_date(w['week_of'])}", int(w["consumers_sampled"]))
        for w in facts.get("consumers_sampled_by_week") or []
    ][-12:]
    stores = [(s["label"], int(s["consumers_sampled"])) for s in facts.get("top_stores") or []]
    states = [(s["label"], int(s["consumers_sampled"])) for s in facts.get("states") or []]
    blocks = []
    if len(weeks) > 1 and _bars(weeks, "by week"):
        blocks.append("<h3>Consumers sampled by week</h3>" + _bars(weeks, "by week"))
    if stores and _bars(stores, "by store"):
        blocks.append("<h3>Consumers sampled by store</h3>" + _bars(stores, "by store"))
    if states and _bars(states, "by state"):
        blocks.append("<h3>Consumers sampled by state</h3>" + _bars(states, "by state"))
    if blocks:
        pages.append("<section class='page'>" + _header("When and where") + "".join(blocks) + "</section>")

    # 4. What consumers said: counted themes when that feature has them,
    # otherwise plain quotes.
    themes = facts.get("themes") or []
    comments = facts.get("comments") or []
    if themes:
        items = "".join(
            "<div class='theme'>"
            f"<p class='theme-label'>{_esc(t['label'])}"
            + (
                f"<span> · noted on {int(t['recap_count']):,} recap{'s' if int(t['recap_count']) != 1 else ''}</span>"
                if t.get("recap_count")
                else ""
            )
            + "</p>"
            + (f"<blockquote>{_esc(t['quote'])}</blockquote>" if t.get("quote") else "")
            + "</div>"
            for t in themes[:6]
        )
        pages.append("<section class='page'>" + _header("What consumers said") + items + "</section>")
    elif comments:
        quotes = "".join(
            f"<blockquote>{_esc(c if len(c) <= 200 else c[:199].rstrip() + '…')}</blockquote>"
            for c in comments[:MAX_QUOTES_IN_PDF]
        )
        pages.append(
            "<section class='page'>"
            + _header("What consumers said")
            + f"<div class='quotes'>{quotes}</div>"
            + "<p class='note'>Comments noted by the field team on approved recaps.</p>"
            + "</section>"
        )

    # 5+. Photos, six to a page. The first photo is the cover, so it is not
    # repeated here unless it is the only one.
    gallery = embedded[1:] if len(embedded) > 1 else embedded
    for start_index in range(0, len(gallery), 6):
        group = gallery[start_index : start_index + 6]
        figures = "".join(_photo_figure(photo, uri) for photo, uri in group)
        pages.append(
            "<section class='page'>"
            + _header("From the field")
            + f"<div class='photo-grid'>{figures}</div></section>"
        )

    return (
        "<!doctype html><html><head><meta charset='utf-8' />"
        f"<title>{_esc(draft.title or brand)}</title></head><body>"
        + "".join(pages)
        + "</body></html>"
    )


# White page, the Spark mark, one accent (the lime the other report PDFs
# use), no gradients or shadows.
_DRAFT_CSS = """
        @page {
            size: Letter;
            margin: 0.75in;
            @bottom-left {
                content: "Ignite Productions";
                font-family: "Helvetica Neue", Helvetica, Arial, sans-serif;
                font-size: 8px; color: #7a7f70;
            }
            @bottom-right {
                content: counter(page);
                font-family: "Helvetica Neue", Helvetica, Arial, sans-serif;
                font-size: 8px; color: #7a7f70;
            }
        }
        @page :first { @bottom-left { content: ""; } @bottom-right { content: ""; } }
        body { background: #ffffff; color: #14160f; font-size: 11px; line-height: 1.45; }
        .page { break-before: page; }
        .page:first-child { break-before: auto; }
        .eyebrow {
            margin: 0 0 8px 0; font-size: 9px; color: #5c6154;
            text-transform: uppercase; letter-spacing: 0.16em;
        }
        h1 { font-size: 38px; line-height: 1.1; letter-spacing: -0.02em; margin: 0 0 8px 0; }
        h2 { font-size: 26px; letter-spacing: -0.01em; margin: 0 0 12px 0; }
        h3 {
            font-size: 10px; text-transform: uppercase; letter-spacing: 0.12em;
            color: #5c6154; margin: 30px 0 12px 0; break-after: avoid;
        }
        .accent { width: 44px; height: 3px; background: #c5f546; }
        .page-head { margin-bottom: 26px; }
        .cover .spark-mark { height: 24px; margin: 0 0 90px 0; }
        .cover .period { font-size: 14px; color: #3d4236; margin: 0 0 14px 0; }
        .cover .hero { margin-top: 36px; }
        .cover .hero img {
            width: 100%; height: 5.1in; object-fit: cover; display: block;
            border: 1px solid #e2e4d8;
        }
        .cover .prepared { margin-top: 18px; font-size: 9px; color: #7a7f70; }
        .kpi-grid { display: grid; grid-template-columns: repeat(4, 1fr); gap: 0; }
        .kpi {
            padding: 14px 12px 14px 0; border-top: 1px solid #14160f;
            margin: 0 14px 18px 0; break-inside: avoid;
        }
        .kpi strong {
            display: block; font-size: 30px; letter-spacing: -0.02em;
            font-variant-numeric: tabular-nums;
        }
        .kpi span { display: block; margin-top: 4px; font-size: 10px; color: #5c6154; }
        .summary p { font-size: 15px; line-height: 1.55; margin: 16px 0 0 0; max-width: 6.4in; }
        .takeaways { margin: 0; padding-left: 20px; max-width: 6.4in; }
        .takeaways li { font-size: 13.5px; line-height: 1.5; margin: 0 0 12px 0; padding-left: 6px; break-inside: avoid; }
        .bars { margin: 0; }
        .bar-row { display: flex; align-items: center; margin: 0 0 11px 0; break-inside: avoid; }
        .bar-label { width: 2.1in; font-size: 11.5px; padding-right: 10px; }
        .bar-track { flex: 1; height: 16px; background: #f1f2ec; }
        .bar-fill { height: 16px; background: #c5f546; }
        .bar-value {
            width: 0.7in; text-align: right; font-size: 11.5px;
            font-variant-numeric: tabular-nums;
        }
        blockquote {
            margin: 0 0 24px 0; padding: 2px 0 2px 16px; border-left: 3px solid #c5f546;
            font-size: 16px; line-height: 1.5; break-inside: avoid; max-width: 6.2in;
        }
        .theme { margin: 0 0 26px 0; break-inside: avoid; }
        .theme blockquote { margin-bottom: 0; font-size: 14px; }
        .theme-label { font-size: 14px; font-weight: 700; margin: 0 0 8px 0; }
        .theme-label span { font-weight: 400; color: #5c6154; }
        .note { font-size: 9px; color: #7a7f70; margin-top: 18px; }
        .photo-grid { display: grid; grid-template-columns: repeat(2, 1fr); gap: 14px 16px; }
        figure { margin: 0; break-inside: avoid; }
        figure .frame { border: 1px solid #e2e4d8; }
        figure img { width: 100%; height: 2.3in; object-fit: cover; display: block; }
        figcaption { margin-top: 6px; }
        .cap-main { display: block; font-size: 10.5px; }
        .cap-meta { display: block; font-size: 9px; color: #5c6154; }
"""


def render_draft_pdf(draft: ClientRecapDraft) -> bytes:
    """The draft as PDF bytes. Raises :class:`ClientRecapDraftError` when the
    renderer is not available, like the other report PDFs."""
    from recaps.pdf import _PDF_BASE_CSS

    embedded = _embed_photos(draft_photos(draft))
    document = build_draft_html(draft, embedded)
    try:
        from weasyprint import CSS, HTML
    except Exception as exc:  # native deps missing / import-time failure
        raise ClientRecapDraftError("PDF renderer is unavailable on this server.") from exc
    try:
        css = CSS(string=_PDF_BASE_CSS + _DRAFT_CSS)
        return HTML(string=document).write_pdf(stylesheets=[css])
    except Exception as exc:  # noqa: BLE001
        raise ClientRecapDraftError("The PDF could not be made.") from exc


def pdf_filename(draft: ClientRecapDraft) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", (draft.tenant.name or "client").lower()).strip("-") or "client"
    return f"{slug}-recap-{draft.start_date.isoformat()}-to-{draft.end_date.isoformat()}.pdf"
