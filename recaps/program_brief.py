"""A short brief for the program report, drafted for one date range.

The model writes prose only. Every number in the stored paragraph, bullets
and quote has to appear in a fact packet built from approved recaps
(the same roll-ups the report page uses, plus themes and competitor
mentions that were already stored — this module does not ask the model to
group comments or read photos). A draft that invents a figure is thrown
away. A percent sign is kept only when that exact percent is in the facts,
so a count cannot be reprinted as a rate.

Reading a brief never calls the model. Staff ask for a new draft. A preset
of the last 30 or 90 days is published once the numbers check out, the same
way a weekly summary is. A 6-month, year-to-date or custom range stays a
draft until Ignite staff mark it reviewed, and a brand user is never given
a draft or a brief whose approved-recap count has since changed. Nothing
here sends email.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import date, timedelta

import strawberry
from asgiref.sync import sync_to_async
from django.utils import timezone

from recaps.consumer_breakdown import (
    KIND_NAMED,
    KIND_VENUE,
    LABELED_KINDS,
    tenant_consumer_breakdown,
)
from recaps.feedback_themes import MIN_COMMENTS, gather_comments, source_hash
from recaps.tenant_overview import (
    _inclusive_dates_to_window,
    _tenant_event_recap_counts_window,
    _tenant_kpi_totals_window,
    tenant_conversion_kpis,
)
from recaps.weekly_narration import numbers_are_grounded
from utils.ai_text import AiUnavailable, generate_json
from utils.graphql.mixins import resolve_id_to_int
from utils.graphql.permissions import StrictIsAuthenticated

logger = logging.getLogger(__name__)

MAX_SPAN_DAYS = 1825
MAX_STORES = 5
MAX_COMMENTS = 20
MAX_THEMES = 6
MAX_COMPETITORS = 3
MAX_PARAGRAPH = 900
MAX_BULLET = 280
MAX_QUOTE = 220
MAX_BULLETS = 3

REASON_UNAVAILABLE = "The brief could not be written right now."
REASON_NO_RECAPS = "No approved recaps in these dates."
REASON_UNGROUNDED = (
    "The draft used a number that is not in the approved recaps, so it was not kept."
)
REASON_NOT_FOUND = "Brief not found."
REASON_BAD_DATES = "Pick a start and end date."
REASON_LONG = "Pick a shorter range."
REASON_EDIT = "That edit uses a number that is not in the approved recaps."
REASON_QUOTE = "The quote has to be a comment from these recaps, copied as written."

PRESET_30 = "30"
PRESET_90 = "90"
PRESETS = frozenset({PRESET_30, PRESET_90, "180", "ytd", "custom"})
_AUTO_DAYS = {PRESET_30: 30, PRESET_90: 90}

_PERCENT = re.compile(r"\d[\d,]*(?:\.\d+)?%")
_REACH = re.compile(r"\breach\b", re.IGNORECASE)

_SECTIONS = {
    "liked": "what people liked",
    "passed": "why people passed",
    "asked": "questions people asked",
}

_SYSTEM = (
    "You draft a short program brief for a brand about its field program. "
    "Ignite staff check longer ranges before a brand sees them. Write to the "
    "brand in plain, short sentences. Use ONLY what is in FACTS. Copy every "
    "number exactly as given, with the label it already has. Never add, "
    "subtract, average, round or estimate, and never write a number or a "
    "percent that is not in FACTS. consumers_sampled is people sampled. Do "
    "not call it reach or samples handed out. Mention samples handed out "
    "only when FACTS has samples_handed_out. Mention conversion only when "
    "FACTS has conversion, and keep the note that it is retail and "
    "on-premise only. Put each number immediately beside the words for that "
    "figure. Do not use a year or a calendar date as a count. "
    "paragraph: one short paragraph on what ran and what worked. "
    "bullets: two or three short bullets. Use a feedback theme when FACTS "
    "has feedback_themes, and a watch-out when FACTS has a reason people "
    "passed, a question, a competitor, or a shelf miss. Leave a bullet out "
    "when FACTS cannot support it. "
    "quote: copy one consumer comment from FACTS exactly, or null. Do not "
    "paraphrase and do not name a person. "
    "No greeting, no sign-off, no headings, no markdown, no exclamation marks."
)

BRIEF_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["paragraph", "bullets", "quote"],
    "properties": {
        "paragraph": {"type": "string"},
        "bullets": {"type": "array", "items": {"type": "string"}},
        "quote": {"type": ["string", "null"]},
    },
}

_ID_PREFIX = "ProgramBrief:"


def parse_window(start_raw: str | None, end_raw: str | None) -> tuple[date, date] | None:
    """Inclusive dates, or None when they are missing, reversed or too long."""
    if not start_raw or not end_raw:
        return None
    try:
        start = date.fromisoformat(start_raw)
        end = date.fromisoformat(end_raw)
    except ValueError:
        return None
    if end < start or (end - start).days > MAX_SPAN_DAYS:
        return None
    return start, end


def window_reason(start_raw: str | None, end_raw: str | None) -> str:
    """Why ``parse_window`` rejected a pair. ``REASON_LONG`` when it is only
    too long, otherwise ``REASON_BAD_DATES``."""
    if start_raw and end_raw:
        try:
            start = date.fromisoformat(start_raw)
            end = date.fromisoformat(end_raw)
        except ValueError:
            return REASON_BAD_DATES
        if end >= start and (end - start).days > MAX_SPAN_DAYS:
            return REASON_LONG
    return REASON_BAD_DATES


def normalize_preset(raw: str | None) -> str:
    text = (raw or "").strip().lower()
    return text if text in PRESETS else "custom"


def auto_review(start: date, end: date, preset: str) -> bool:
    """True only for a last-30 or last-90 preset whose span is that many days.

    Six months, year to date, custom, and a preset that does not match the
    dates stay a draft until a person marks them reviewed.
    """
    expected = _AUTO_DAYS.get(preset)
    if expected is None:
        return False
    return (end - start).days == expected


def _plain(raw) -> str:
    text = " ".join(str(raw or "").replace("**", "").replace("__", "").replace("!", "").split())
    return text.lstrip("#-*• ").strip()


def _str_list(raw) -> list[str]:
    if not isinstance(raw, list):
        return []
    out = []
    for item in raw:
        line = _plain(item)
        if line:
            out.append(line)
    return out


def _joined(paragraph: str, bullets: list[str], quote: str) -> str:
    parts = [paragraph, *bullets]
    if quote:
        parts.append(quote)
    return "\n".join(part for part in parts if part)


def _norm_comment(text: str) -> str:
    return " ".join(str(text or "").split()).casefold()


def _comment_haystack(facts: dict) -> list[str]:
    hay: list[str] = []
    for comment in facts.get("consumer_comments") or []:
        if isinstance(comment, dict):
            hay.append(str(comment.get("text") or ""))
    for theme in facts.get("feedback_themes") or []:
        if isinstance(theme, dict):
            hay.append(str(theme.get("quote") or ""))
    for brand in facts.get("competitor_mentions") or []:
        if isinstance(brand, dict):
            hay.append(str(brand.get("quote") or ""))
    return hay


def quote_in_facts(quote: str, facts: dict) -> bool:
    """True when ``quote`` is one whole comment or stored quote, not a fragment."""
    needle = _norm_comment(quote)
    if len(needle) < 8:
        return False
    return any(_norm_comment(hay) == needle for hay in _comment_haystack(facts) if hay)


_ISO_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")
_WORD = re.compile(r"\d[\d,]*(?:\.\d+)?%?|[A-Za-z]+(?:'[A-Za-z]+)?")
# A count sits just before its label ("40 consumers were sampled") or just
# after a short lead-in ("sampled 40 consumers"). Two words before and four
# after is wide enough for that label and narrow enough that "40 events" does
# not borrow "consumers sampled" from the next clause.
_WORDS_BEFORE = 2
_WORDS_AFTER = 4


def _word_matches(word: str, keyword: str) -> bool:
    return word == keyword or word == f"{keyword}s" or word == f"{keyword}es"


def _window_has(words: list[str], index: int, group: tuple[str, ...]) -> bool:
    nearby = words[max(0, index - _WORDS_BEFORE) : index]
    nearby += words[index + 1 : index + 1 + _WORDS_AFTER]
    return all(any(_word_matches(word, keyword) for word in nearby) for keyword in group)


def _add_figure(
    figures: list[tuple[str, bool, tuple[tuple[str, ...], ...]]],
    value,
    *groups: tuple[str, ...],
    percent: bool = False,
) -> None:
    if value is None or value is False or not groups:
        return
    raw = str(value).strip().replace(",", "")
    if not raw or raw in {"0", "0.0", "0%"}:
        return
    is_percent = percent or raw.endswith("%")
    token = raw[:-1] if raw.endswith("%") else raw
    if token.endswith(".0"):
        token = token[:-2]
    if not token:
        return
    figures.append((token, is_percent, groups))


def _name_groups(name: str, *extra: str) -> tuple[tuple[str, ...], ...]:
    words = [word.lower() for word in re.findall(r"[A-Za-z0-9]+", name or "") if word]
    if not words:
        return ()
    return tuple((*extra, word) for word in words[:3])


def _metric_figures(facts: dict) -> list[tuple[str, bool, tuple[tuple[str, ...], ...]]]:
    """Each allowed number, and the words that have to sit next to it."""
    figures: list[tuple[str, bool, tuple[tuple[str, ...], ...]]] = []
    _add_figure(figures, facts.get("events_run"), ("event",))
    _add_figure(figures, facts.get("approved_recaps"), ("approved", "recap"))
    _add_figure(
        figures,
        facts.get("consumers_sampled"),
        ("consumer", "sampled"),
        ("people", "sampled"),
    )
    _add_figure(
        figures,
        facts.get("samples_handed_out"),
        ("sample", "handed"),
        ("handed", "out"),
    )
    _add_figure(figures, facts.get("units_sold"), ("unit", "sold"), ("sold",))
    _add_figure(figures, facts.get("first_time_consumers"), ("first",))
    _add_figure(figures, facts.get("brand_aware_consumers"), ("aware",), ("awareness",))
    _add_figure(
        figures,
        facts.get("consumers_willing_to_purchase"),
        ("willing",),
        ("purchase",),
        ("buy",),
    )
    _add_figure(figures, facts.get("store_count"), ("store",))
    conversion = facts.get("conversion") if isinstance(facts.get("conversion"), dict) else {}
    _add_figure(figures, conversion.get("conversion_percent"), ("conversion",), percent=True)
    _add_figure(figures, conversion.get("units_sold"), ("unit", "sold"), ("sold",))
    _add_figure(
        figures,
        conversion.get("sampled_at_those_events"),
        ("sampled", "those"),
        ("sampled", "conversion"),
    )
    for store in facts.get("stores") or []:
        if isinstance(store, dict):
            _add_figure(
                figures,
                store.get("consumers_sampled"),
                *_name_groups(str(store.get("store") or ""), "sampled"),
            )
    for state in facts.get("states") or []:
        if isinstance(state, dict):
            _add_figure(
                figures,
                state.get("consumers_sampled"),
                *_name_groups(str(state.get("state") or ""), "sampled"),
            )
    for theme in facts.get("feedback_themes") or []:
        if isinstance(theme, dict):
            _add_figure(figures, theme.get("recaps_mentioning_it"), ("recap",))
    for brand in facts.get("competitor_mentions") or []:
        if isinstance(brand, dict):
            _add_figure(figures, brand.get("recaps_mentioning_it"), ("recap",), ("mention",))
            _add_figure(figures, brand.get("recaps_preferring_ours"), ("prefer",), ("ours",))
            _add_figure(figures, brand.get("recaps_preferring_theirs"), ("prefer",), ("theirs",))
    shelf = facts.get("shelf") if isinstance(facts.get("shelf"), dict) else {}
    _add_figure(figures, shelf.get("retail_visits_with_a_shelf_answer"), ("visit",))
    _add_figure(figures, shelf.get("product_on_shelf"), ("shelf",))
    _add_figure(figures, shelf.get("product_not_on_shelf"), ("shelf",))
    _add_figure(figures, shelf.get("price_tag_visible"), ("price",), ("tag",))
    _add_figure(figures, shelf.get("shelf_photo_unclear"), ("unclear",), ("shelf",))
    previous = facts.get("previous_period") if isinstance(facts.get("previous_period"), dict) else {}
    _add_figure(
        figures,
        previous.get("events_run"),
        ("previous", "event"),
        ("prior", "event"),
        ("earlier", "event"),
    )
    _add_figure(
        figures,
        previous.get("consumers_sampled"),
        ("previous", "sampled"),
        ("prior", "sampled"),
        ("earlier", "sampled"),
    )
    return figures


def _without_copied_dates(text: str, facts: dict) -> str:
    blob = json.dumps(facts, ensure_ascii=False)
    cleaned = text or ""
    for stamp in set(_ISO_DATE.findall(cleaned)):
        if stamp in blob:
            cleaned = cleaned.replace(stamp, " ")
    return cleaned


def _without_exact_quotes(text: str, facts: dict) -> str:
    """A copied comment may contain digits. Those are the shopper's words."""
    cleaned = text or ""
    for hay in _comment_haystack(facts):
        plain = " ".join(str(hay or "").split())
        if len(plain) < 8:
            continue
        cleaned = re.sub(re.escape(plain), " ", cleaned, flags=re.IGNORECASE)
    return cleaned


def figures_match_labels(text: str, facts: dict) -> bool:
    """Every number sits next to the metric it actually is.

    A year inside ``2026-10-24`` is not a count of consumers, and ``40`` from
    consumers sampled cannot be written as ``40 events``.
    """
    figures = _metric_figures(facts)
    words = _WORD.findall(_without_copied_dates(_without_exact_quotes(text, facts), facts))
    lowered = [word.lower() for word in words]
    for index, word in enumerate(lowered):
        if not _NUM_TOKEN.match(word):
            continue
        is_percent = word.endswith("%")
        token = word[:-1] if is_percent else word
        token = token.replace(",", "")
        if token.endswith(".0"):
            token = token[:-2]
        candidates = [
            groups
            for figure, percent, groups in figures
            if figure == token and percent == is_percent
        ]
        if not candidates:
            return False
        if not any(_window_has(lowered, index, group) for groups in candidates for group in groups):
            return False
    return True


_NUM_TOKEN = re.compile(r"^\d[\d,]*(?:\.\d+)?%?$")


def brief_is_grounded(text: str, facts: dict) -> bool:
    """Every figure is in the facts, beside the label it belongs to.

    ``numbers_are_grounded`` also refuses "conversion" and "samples handed
    out" unless the facts already use those words. "reach" is refused here
    unless the facts say it, which they do not. A percent sign is kept only
    when that exact percent is in the facts.
    """
    if not numbers_are_grounded(text, facts):
        return False
    blob = json.dumps(facts, ensure_ascii=False)
    for token in _PERCENT.findall(text or ""):
        compact = token.replace(",", "")
        if token not in blob and compact not in blob:
            return False
    if _REACH.search(text or "") and not _REACH.search(blob):
        return False
    return figures_match_labels(text, facts)


def _ask_model(system: str, user: str):
    return generate_json(system, user, schema=BRIEF_SCHEMA)


def _from_model(raw, facts: dict) -> dict | None:
    if not isinstance(raw, dict):
        return None
    paragraph = _plain(raw.get("paragraph"))
    raw_bullets = raw.get("bullets")
    bullets: list[str] = []
    for item in raw_bullets if isinstance(raw_bullets, list) else []:
        line = _plain(item)
        if not line:
            continue
        bullets.append(line)
        if len(bullets) == MAX_BULLETS:
            break
    quote = _plain(raw.get("quote") or "")
    if quote and not quote_in_facts(quote, facts):
        quote = ""
    if not paragraph:
        return None
    if len(paragraph) > MAX_PARAGRAPH or any(len(line) > MAX_BULLET for line in bullets):
        return None
    if len(quote) > MAX_QUOTE:
        return None
    if not brief_is_grounded(_joined(paragraph, bullets, quote), facts):
        return None
    return {"paragraph": paragraph, "bullets": bullets, "quote": quote}


def compose_brief(facts: dict, *, generate=_ask_model) -> tuple[dict | None, str | None]:
    """``(draft, None)`` or ``(None, reason)``. Does not store anything."""
    if not int(facts.get("approved_recaps") or 0):
        return None, REASON_NO_RECAPS
    user = "FACTS:\n" + json.dumps(facts, indent=2, ensure_ascii=False)
    saw_paragraph = False
    for _attempt in range(2):
        try:
            raw = generate(_SYSTEM, user)
        except AiUnavailable:
            return None, REASON_UNAVAILABLE
        except Exception:  # noqa: BLE001
            logger.exception("program brief: model call failed")
            return None, REASON_UNAVAILABLE
        if isinstance(raw, dict) and _plain(raw.get("paragraph")):
            saw_paragraph = True
        drafted = _from_model(raw, facts)
        if drafted is not None:
            return drafted, None
        if raw is None:
            return None, REASON_UNAVAILABLE
    return None, REASON_UNGROUNDED if saw_paragraph else REASON_UNAVAILABLE


def _counts(tenant_id: int, start: date, end: date) -> tuple[int, int]:
    window = _inclusive_dates_to_window(start, end)
    events, recaps = _tenant_event_recap_counts_window(tenant_id, window)
    return int(events), int(recaps)


def approved_recap_count(tenant_id: int, start: date, end: date) -> int:
    """Approved recaps in the window. The same count the report headline uses."""
    _events, recaps = _counts(tenant_id, start, end)
    return recaps


def _stored_themes(tenant_id: int, comments: list) -> list[dict]:
    """Themes already stored for these comments. Does not call the model."""
    if len(comments) < MIN_COMMENTS:
        return []
    from tenants.models import TenantFeedbackThemes

    row = TenantFeedbackThemes.objects.filter(
        tenant_id=tenant_id, source_hash=source_hash(comments)
    ).first()
    if row is None:
        return []
    out = []
    for theme in (row.payload or {}).get("themes") or []:
        if not isinstance(theme, dict):
            continue
        label = str(theme.get("label") or "").strip()
        count = int(theme.get("recap_count") or 0)
        if not label or count <= 0:
            continue
        item = {
            "section": _SECTIONS.get(str(theme.get("section") or ""), str(theme.get("section") or "")),
            "theme": label,
            "recaps_mentioning_it": count,
        }
        quote = str(theme.get("quote") or "").strip()
        if quote:
            item["quote"] = quote
        out.append(item)
        if len(out) >= MAX_THEMES:
            break
    return out


def _stored_competitors(tenant_id: int, comments: list) -> list[dict]:
    """Competitor tags already stored for these comments. Does not call the model."""
    if len(comments) < MIN_COMMENTS:
        return []
    from recaps.comment_insights import KIND_COMPETITORS, _digest, _stored, tenant_names

    brand, products = tenant_names(tenant_id)
    product_names = [name for _pid, name in products]
    row = _stored(
        tenant_id,
        KIND_COMPETITORS,
        _digest(KIND_COMPETITORS, comments, [brand, product_names]),
    )
    if row is None:
        return []
    out = []
    for item in (row.payload or {}).get("brands") or []:
        if not isinstance(item, dict):
            continue
        name = str(item.get("brand") or "").strip()
        count = int(item.get("recap_count") or 0)
        if not name or count <= 0:
            continue
        packed = {"brand": name, "recaps_mentioning_it": count}
        for key, label in (
            ("prefer_ours", "recaps_preferring_ours"),
            ("prefer_theirs", "recaps_preferring_theirs"),
        ):
            value = int(item.get(key) or 0)
            if value > 0:
                packed[label] = value
        quote = str(item.get("quote") or "").strip()
        if quote:
            packed["quote"] = quote
        out.append(packed)
        if len(out) >= MAX_COMPETITORS:
            break
    return out


def _shelf_facts(tenant_id: int, start: date, end: date) -> dict | None:
    from recaps.shelf_check import shelf_summary

    try:
        summary = shelf_summary(tenant_id, start, end)
    except Exception:  # noqa: BLE001
        logger.exception("program brief: shelf summary failed tenant=%s", tenant_id)
        return None
    visits = int(summary.get("visits") or 0)
    if visits <= 0:
        return None
    facts: dict = {
        "retail_visits_with_a_shelf_answer": visits,
        "note": "Counted from shelf photos on approved retail recaps.",
    }
    for key, label in (
        ("on_shelf", "product_on_shelf"),
        ("not_found", "product_not_on_shelf"),
        ("price_tag", "price_tag_visible"),
        ("unclear", "shelf_photo_unclear"),
    ):
        value = int(summary.get(key) or 0)
        if value > 0:
            facts[label] = value
    return facts


def brief_facts(tenant_id: int, start: date, end: date) -> dict:
    """Everything the draft may say. Themes and competitors are read from
    rows already stored for these comments; neither call reaches the model."""
    from tenants.models import Tenant

    events, recaps = _counts(tenant_id, start, end)
    window = _inclusive_dates_to_window(start, end)
    kpis = _tenant_kpi_totals_window(tenant_id, window)
    breakdown = tenant_consumer_breakdown(tenant_id, start, end)
    sampled = int(breakdown["total"])
    facts: dict = {
        "brand": Tenant.objects.filter(id=tenant_id).values_list("name", flat=True).first() or "",
        "period": f"{start.isoformat()} to {end.isoformat()}",
        "events_run": events,
        "approved_recaps": recaps,
        "consumers_sampled": sampled,
    }
    samples = int(kpis.samples_distributed or 0)
    if samples and samples != sampled:
        facts["samples_handed_out"] = samples
    for label, value in (
        ("units_sold", kpis.products_sold),
        ("first_time_consumers", kpis.first_time_consumers),
        ("brand_aware_consumers", kpis.brand_aware_consumers),
        ("consumers_willing_to_purchase", kpis.willing_to_purchase),
    ):
        if value:
            facts[label] = int(value)

    conversion = tenant_conversion_kpis(tenant_id, start, end)
    pct = conversion.get("pct")
    sold = int(conversion.get("sold") or 0)
    if pct is not None and sold:
        facts["conversion"] = {
            "conversion_percent": f"{pct:g}%",
            "units_sold": sold,
            "sampled_at_those_events": int(conversion.get("engagements") or 0),
            "note": (
                "Conversion counts retail and on-premise events only. "
                "Event and Event Activation are not included."
            ),
        }

    stores = [row for row in breakdown["by_store"] if row["kind"] in LABELED_KINDS][:MAX_STORES]
    if stores:
        facts["stores"] = [
            {
                "store": row["label"],
                "consumers_sampled": row["consumers"],
                **({"event_venue": True} if row["kind"] == KIND_VENUE else {}),
            }
            for row in stores
        ]
        facts["store_count"] = breakdown["store_count"]
    states = [row for row in breakdown["by_state"] if row["kind"] == KIND_NAMED]
    if states:
        facts["states"] = [
            {"state": row["label"], "consumers_sampled": row["consumers"]} for row in states[:8]
        ]

    span = (end - start).days + 1
    prev_end = start - timedelta(days=1)
    prev_start = prev_end - timedelta(days=span - 1)
    prev_events, _prev_recaps = _counts(tenant_id, prev_start, prev_end)
    prev_sampled = int(tenant_consumer_breakdown(tenant_id, prev_start, prev_end)["total"])
    if prev_events or prev_sampled:
        facts["previous_period"] = {
            "dates": f"{prev_start.isoformat()} to {prev_end.isoformat()}",
            "events_run": prev_events,
            "consumers_sampled": prev_sampled,
        }

    comments, _truncated = gather_comments(tenant_id, start, end)
    if comments:
        facts["consumer_comments"] = [
            {"type": comment.kind, "text": comment.text} for comment in comments[:MAX_COMMENTS]
        ]
    themes = _stored_themes(tenant_id, comments)
    if themes:
        facts["feedback_themes"] = themes
    competitors = _stored_competitors(tenant_id, comments)
    if competitors:
        facts["competitor_mentions"] = competitors
    shelf = _shelf_facts(tenant_id, start, end)
    if shelf:
        facts["shelf"] = shelf
    notes = [
        "consumers_sampled counts people who were sampled.",
        "brand_aware_consumers and consumers_willing_to_purchase are counts of people who answered, not rates.",
    ]
    if "samples_handed_out" in facts:
        notes.append(
            "samples handed out is units given out, separate from people sampled."
        )
    if "conversion" in facts:
        notes.append(
            "conversion is Retail and On-Premise only, not Event or Event Activation."
        )
    facts["labels"] = " ".join(notes)
    return facts


def _is_stale(row, approved: int) -> bool:
    stored = (row.facts or {}).get("approved_recaps")
    if stored is None:
        return False
    return int(stored) != int(approved)


def write_brief(tenant_id: int, start: date, end: date, preset: str):
    """Write or replace the brief. ``(row, None)`` or ``(None, reason)``.

    A failure leaves any stored brief as it was. A matching 30- or 90-day
    preset is stored already reviewed, with no reviewer. Every other preset
    is a draft.
    """
    from tenants.models import TenantProgramBrief

    facts = brief_facts(tenant_id, start, end)
    drafted, reason = compose_brief(facts)
    if drafted is None:
        return None, reason
    publish = auto_review(start, end, preset)
    now = timezone.now()
    row, _created = TenantProgramBrief.objects.update_or_create(
        tenant_id=tenant_id,
        start_date=start,
        end_date=end,
        defaults={
            "preset": preset,
            "paragraph": drafted["paragraph"],
            "bullets": drafted["bullets"],
            "quote": drafted["quote"],
            "has_edit": False,
            "edited_paragraph": "",
            "edited_bullets": [],
            "edited_quote": "",
            "status": (
                TenantProgramBrief.STATUS_REVIEWED
                if publish
                else TenantProgramBrief.STATUS_DRAFT
            ),
            "facts": facts,
            "generated_at": now,
            "reviewed_by": None,
            "reviewed_at": now if publish else None,
        },
    )
    return row, None


def _shown(row) -> tuple[str, list[str], str]:
    if row.has_edit:
        return (
            _plain(row.edited_paragraph),
            _str_list(row.edited_bullets),
            _plain(row.edited_quote),
        )
    return (_plain(row.paragraph), _str_list(row.bullets), _plain(row.quote))


def _row_id(raw) -> int | None:
    text = str(raw or "").strip().removeprefix(_ID_PREFIX)
    return int(text) if text.isdigit() else None


def _person(user) -> str | None:
    if user is None:
        return None
    name = " ".join(
        part
        for part in (getattr(user, "first_name", ""), getattr(user, "last_name", ""))
        if part
    ).strip()
    return name or (getattr(user, "email", None) or None)


# ─── GraphQL ────────────────────────────────────────────────────────


@strawberry.type
class ProgramBrief:
    """The brief a page may show. ``is_draft`` is only true for staff."""

    id: strawberry.ID
    start_date: str
    end_date: str
    preset: str
    paragraph: str
    bullets: list[str]
    quote: str | None
    status: str
    is_draft: bool
    stale: bool
    auto_reviewed: bool
    generated_at: str
    reviewed_by_name: str | None
    reviewed_at: str | None


@strawberry.type
class ProgramBriefView:
    """Whether these dates have approved recaps, and the brief when the
    caller is allowed to see it."""

    enough: bool
    approved_recaps: int
    brief: ProgramBrief | None


@strawberry.type
class ProgramBriefResult:
    ok: bool
    reason: str | None
    brief: ProgramBrief | None


def _brief_type(row, approved: int) -> ProgramBrief:
    paragraph, bullets, quote = _shown(row)
    reviewed = row.status == row.STATUS_REVIEWED
    return ProgramBrief(
        id=strawberry.ID(f"{_ID_PREFIX}{row.id}"),
        start_date=row.start_date.isoformat(),
        end_date=row.end_date.isoformat(),
        preset=row.preset,
        paragraph=paragraph,
        bullets=bullets,
        quote=quote or None,
        status=row.status,
        is_draft=not reviewed,
        stale=_is_stale(row, approved),
        auto_reviewed=reviewed and row.reviewed_by_id is None,
        generated_at=row.generated_at.isoformat(),
        reviewed_by_name=_person(row.reviewed_by),
        reviewed_at=row.reviewed_at.isoformat() if row.reviewed_at else None,
    )


def _load_row(tenant_id: int, start: date, end: date):
    from tenants.models import TenantProgramBrief

    return (
        TenantProgramBrief.objects.select_related("reviewed_by")
        .filter(tenant_id=tenant_id, start_date=start, end_date=end)
        .first()
    )


def load_view(tenant_id: int, start: date, end: date, *, staff: bool) -> ProgramBriefView:
    """Read a stored brief. Does not call the model or rebuild facts."""
    approved = approved_recap_count(tenant_id, start, end)
    row = _load_row(tenant_id, start, end)
    if row is not None and not staff:
        if row.status != row.STATUS_REVIEWED or _is_stale(row, approved):
            row = None
    return ProgramBriefView(
        enough=approved > 0,
        approved_recaps=approved,
        brief=_brief_type(row, approved) if row is not None else None,
    )


def _fail(reason: str) -> ProgramBriefResult:
    return ProgramBriefResult(ok=False, reason=reason, brief=None)


async def _staff_user(info: strawberry.Info):
    """The caller when they are Ignite staff, else None."""
    from events.staffing_board import _accessible_tenants
    from utils.graphql.mixins import SparkGraphQLMixin

    user = await SparkGraphQLMixin().get_user(info)
    is_admin, _allowed = await _accessible_tenants(user)
    return user if is_admin else None


async def _target_tenant(info: strawberry.Info, tenant_id: strawberry.ID) -> int | None:
    from recaps.report_types import _CampaignReportService

    try:
        return await _CampaignReportService().resolve_target_tenant_id(info, tenant_id)
    except Exception:  # noqa: BLE001
        return None


@strawberry.type
class ProgramBriefQueries:
    @strawberry.field(permission_classes=[StrictIsAuthenticated])
    async def program_brief(
        self,
        info: strawberry.Info,
        tenant_id: strawberry.ID,
        start_date: str,
        end_date: str,
    ) -> ProgramBriefView | None:
        """The stored brief for these dates, or null when the dates are not
        usable. A brand user gets their own tenant, and only a brief staff
        have marked reviewed (or that passed the 30/90 number check) whose
        approved-recap count still matches. Reads a stored row. Never calls
        the model.
        """
        window = parse_window(start_date, end_date)
        if window is None:
            return None
        target = await _target_tenant(info, tenant_id)
        if target is None:
            return None
        staff = await _staff_user(info) is not None
        start, end = window
        try:
            return await sync_to_async(load_view, thread_sensitive=True)(
                target, start, end, staff=staff
            )
        except Exception:  # noqa: BLE001
            logger.exception("programBrief failed tenant=%s", target)
            return None


@strawberry.type
class ProgramBriefMutations:
    @strawberry.mutation(permission_classes=[StrictIsAuthenticated])
    async def generate_program_brief(
        self,
        info: strawberry.Info,
        tenant_id: strawberry.ID,
        start_date: str,
        end_date: str,
        preset: str = "custom",
    ) -> ProgramBriefResult:
        """Write the brief for one brand and range, replacing any stored one
        and its edit. Staff only. Sends nothing."""
        if await _staff_user(info) is None:
            return _fail("Not available.")
        window = parse_window(start_date, end_date)
        if window is None:
            return _fail(window_reason(start_date, end_date))
        try:
            tid = resolve_id_to_int(str(tenant_id).strip())
        except Exception:  # noqa: BLE001
            return _fail("Pick a brand.")
        start, end = window
        chosen = normalize_preset(preset)

        def _build():
            from tenants.models import Tenant

            if not Tenant.objects.filter(id=tid).exists():
                return None, "Pick a brand."
            row, reason = write_brief(tid, start, end, chosen)
            if row is None:
                return None, reason
            approved = approved_recap_count(tid, start, end)
            fresh = _load_row(tid, start, end)
            return (_brief_type(fresh, approved) if fresh is not None else None), reason

        try:
            brief, reason = await sync_to_async(_build, thread_sensitive=True)()
        except Exception:  # noqa: BLE001
            logger.exception("generateProgramBrief failed tenant=%s", tid)
            return _fail(REASON_UNAVAILABLE)
        if brief is None:
            return _fail(reason or REASON_UNAVAILABLE)
        return ProgramBriefResult(ok=True, reason=None, brief=brief)

    @strawberry.mutation(permission_classes=[StrictIsAuthenticated])
    async def save_program_brief(
        self,
        info: strawberry.Info,
        id: strawberry.ID,
        paragraph: str,
        bullets: list[str],
        quote: str = "",
    ) -> ProgramBriefResult:
        """Keep staff's wording when it still only uses numbers from the
        stored facts, and a quote that is still one of those comments.
        Staff only. Sends nothing."""
        user = await _staff_user(info)
        if user is None:
            return _fail("Not available.")
        rid = _row_id(id)
        if rid is None:
            return _fail(REASON_NOT_FOUND)
        cleaned_paragraph = _plain(paragraph)
        cleaned_bullets = _str_list(bullets)[:MAX_BULLETS]
        cleaned_quote = _plain(quote)
        if not cleaned_paragraph:
            return _fail("Write a paragraph.")
        if len(bullets or []) > MAX_BULLETS:
            return _fail("Keep it to three bullets.")
        if (
            len(cleaned_paragraph) > MAX_PARAGRAPH
            or any(len(line) > MAX_BULLET for line in cleaned_bullets)
            or len(cleaned_quote) > MAX_QUOTE
        ):
            return _fail("That edit is too long.")

        def _save():
            from tenants.models import TenantProgramBrief

            row = _load_by_id(rid)
            if row is None:
                return None, REASON_NOT_FOUND
            if not brief_is_grounded(
                _joined(cleaned_paragraph, cleaned_bullets, cleaned_quote), row.facts or {}
            ):
                return None, REASON_EDIT
            if cleaned_quote and not quote_in_facts(cleaned_quote, row.facts or {}):
                return None, REASON_QUOTE
            same = (
                cleaned_paragraph == _plain(row.paragraph)
                and cleaned_bullets == _str_list(row.bullets)
                and cleaned_quote == _plain(row.quote)
            )
            if same:
                row.has_edit = False
                row.edited_paragraph = ""
                row.edited_bullets = []
                row.edited_quote = ""
            else:
                row.has_edit = True
                row.edited_paragraph = cleaned_paragraph
                row.edited_bullets = cleaned_bullets
                row.edited_quote = cleaned_quote
            if row.status == TenantProgramBrief.STATUS_REVIEWED and row.reviewed_by_id is None:
                row.reviewed_by = user
                row.reviewed_at = timezone.now()
            row.save()
            fresh = _load_by_id(rid)
            approved = approved_recap_count(fresh.tenant_id, fresh.start_date, fresh.end_date)
            return _brief_type(fresh, approved), None

        try:
            brief, reason = await sync_to_async(_save, thread_sensitive=True)()
        except Exception:  # noqa: BLE001
            logger.exception("saveProgramBrief failed id=%s", rid)
            return _fail(REASON_UNAVAILABLE)
        if brief is None:
            return _fail(reason or REASON_NOT_FOUND)
        return ProgramBriefResult(ok=True, reason=None, brief=brief)

    @strawberry.mutation(permission_classes=[StrictIsAuthenticated])
    async def set_program_brief_reviewed(
        self, info: strawberry.Info, id: strawberry.ID, reviewed: bool
    ) -> ProgramBriefResult:
        """Mark a draft reviewed, or put it back to draft. Staff only.
        Marking it reviewed sends nothing. A stale count still hides it
        from the brand until the brief is written again."""
        user = await _staff_user(info)
        if user is None:
            return _fail("Not available.")
        rid = _row_id(id)
        if rid is None:
            return _fail(REASON_NOT_FOUND)

        def _save():
            from tenants.models import TenantProgramBrief

            row = _load_by_id(rid)
            if row is None:
                return None
            if reviewed:
                row.status = TenantProgramBrief.STATUS_REVIEWED
                row.reviewed_by = user
                row.reviewed_at = timezone.now()
            else:
                row.status = TenantProgramBrief.STATUS_DRAFT
                row.reviewed_by = None
                row.reviewed_at = None
            row.save(
                update_fields=["status", "reviewed_by", "reviewed_at", "updated_at"]
            )
            fresh = _load_by_id(rid)
            approved = approved_recap_count(fresh.tenant_id, fresh.start_date, fresh.end_date)
            return _brief_type(fresh, approved)

        try:
            brief = await sync_to_async(_save, thread_sensitive=True)()
        except Exception:  # noqa: BLE001
            logger.exception("setProgramBriefReviewed failed id=%s", rid)
            return _fail(REASON_UNAVAILABLE)
        if brief is None:
            return _fail(REASON_NOT_FOUND)
        return ProgramBriefResult(ok=True, reason=None, brief=brief)


def _load_by_id(rid: int):
    from tenants.models import TenantProgramBrief

    return (
        TenantProgramBrief.objects.select_related("reviewed_by").filter(id=rid).first()
    )
