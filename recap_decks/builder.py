"""Build, finish, edit and share a client recap deck.

The deck is a :class:`recaps.models.ClientRecapDraft` (numbers, words,
photos; see ``recaps/client_recap_draft.py``) plus its
:class:`recap_decks.models.ClientRecapDeck` row (status, next steps, staff
choices).

**Big programs.** A draft is put together inside the staff member's
request when the window has at most :data:`INLINE_MAX_RECAPS` approved
recaps. Above that, the request stores the numbers, the photos and the
no-product visits (all SQL, bounded) with words built from the numbers, and
marks the deck ``building``. The slow part (the counted themes and the one
model call for the summary, takeaways and next steps) then runs:

* on a Cloud Task (``/api/tasks/recap-deck-build``) when Cloud Tasks is
  configured, and
* always on the ``build-recap-decks`` cron, which picks up any deck still
  ``building`` (a task that never ran, or a worker that died).

A build is claimed with one conditional UPDATE, so the task and the cron
never both write a deck. After :data:`MAX_BUILD_ATTEMPTS` the deck is
handed to staff with the words built from the numbers.

**Sharing.** Staff mark a deck ``ready``; only then can the brand's users
list and download it. Spark sends nothing when that happens. A ready deck is
read-only until staff move it back to draft.
"""

from __future__ import annotations

import logging
import time
from datetime import timedelta

from django.db.models import F, Q
from django.utils import timezone

from recap_decks.models import ClientRecapDeck
from recap_decks.no_product import excluded_custom_ids, excluded_keys

logger = logging.getLogger(__name__)

# Approved recaps above which the words are written outside the request.
INLINE_MAX_RECAPS = 400
# A build that started this long ago and never finished may be taken over.
STALE_AFTER = timedelta(minutes=8)
MAX_BUILD_ATTEMPTS = 3
TASK_PATH = "/api/tasks/recap-deck-build"
MAX_QUOTES = 6

BUILD_GAVE_UP = (
    "The summary could not be written, so it was built from the numbers. "
    "Edit it as you like."
)


class DeckError(RuntimeError):
    """A deck change that is not allowed right now. Safe to show staff."""


# ─── Reading ────────────────────────────────────────────────────────


def deck_for(draft) -> ClientRecapDeck:
    """The draft's deck row. Drafts made before decks existed get one, as a
    staff draft."""
    try:
        return draft.deck
    except ClientRecapDeck.DoesNotExist:
        deck, _created = ClientRecapDeck.objects.get_or_create(draft=draft)
        return deck


def deck_skip_recaps(draft) -> set[int]:
    """Custom recap ids of the no-product visits this deck leaves out."""
    deck = deck_for(draft)
    return excluded_custom_ids((draft.facts or {}).get("visits"), deck.kept_visits)


def left_out_visits(draft) -> list[dict]:
    """Every listed no-product visit, each with ``kept`` (staff counted it
    back in)."""
    kept = {str(k) for k in deck_for(draft).kept_visits or []}
    return [
        {**v, "kept": str(v.get("key")) in kept}
        for v in ((draft.facts or {}).get("visits") or {}).get("excluded") or []
    ]


def visits_to_check(draft) -> list[dict]:
    return list(((draft.facts or {}).get("visits") or {}).get("check") or [])


def program(draft) -> dict:
    """The deck's counts after the visits staff counted back in."""
    from recaps.client_recap_draft import program_figures

    facts = draft.facts or {}
    base = facts.get("program") or {}
    visits = facts.get("visits") or {}
    listed = {str(v.get("key")) for v in visits.get("excluded") or []}
    kept = len(listed & {str(k) for k in deck_for(draft).kept_visits or []})
    by_key = {n.get("key"): int(n.get("value") or 0) for n in facts.get("numbers") or []}
    if base:
        recaps = int(base.get("demos") or 0) + int(base.get("no_product_visits") or 0)
    else:  # a draft made before decks: every approved recap is a demo
        recaps = by_key.get("recaps", 0)
    consumers = by_key.get("consumers_reached", 0)
    return program_figures(
        recaps=recaps,
        consumers=consumers,
        no_product=max(0, int(base.get("no_product_visits") or 0) - kept),
        store_count=int(base.get("store_count") or facts.get("store_count") or 0),
        state_count=int(base.get("state_count") or facts.get("state_count") or 0),
    )


# The tiles on the "At a glance" page, in order. The dashboard's totals,
# with the demo count and the per-demo figure the deck adds.
_TILE_KPIS = (
    ("consumers_reached", "Consumers sampled"),
    ("products_sold", "Products sold"),
    ("cans_sold", "Cans sold"),
    ("packs_sold", "Packs sold"),
    ("samples_distributed", "Samples handed out"),
    ("willing_to_purchase", "Said they would buy"),
    ("first_time_consumers", "First-time consumers"),
    ("brand_aware_consumers", "Already knew the brand"),
    ("total_engagements", "Engagements"),
)
MAX_TILES = 8


def deck_numbers(draft) -> list[dict]:
    """``[{key, label, value}]`` for the deck's headline tiles. Zeros are
    left out: they are nearly always a field the brand's form does not
    collect."""
    facts = draft.facts or {}
    figures = program(draft)
    by_key = {n.get("key"): int(n.get("value") or 0) for n in facts.get("numbers") or []}
    tiles: list[dict] = []
    if figures["demos"]:
        tiles.append({"key": "demos", "label": "Demos", "value": figures["demos"]})
    if figures["store_count"]:
        tiles.append({"key": "stores", "label": "Stores", "value": figures["store_count"]})
    if figures["state_count"] > 1:
        tiles.append({"key": "states", "label": "States", "value": figures["state_count"]})
    # Like the dashboard: cans / packs when the form counts them, otherwise
    # one "products sold". Samples handed out is not a headline (it is not
    # consumers: for some brands it is loaves or cases opened).
    skip = {"samples_distributed"}
    if by_key.get("cans_sold", 0) + by_key.get("packs_sold", 0) > 0:
        skip.add("products_sold")
    for key, label in _TILE_KPIS:
        if key in skip:
            continue
        if by_key.get(key, 0) > 0:
            tiles.append({"key": key, "label": label, "value": by_key[key]})
            if key == "consumers_reached" and figures["consumers_per_demo"]:
                tiles.append(
                    {
                        "key": "consumers_per_demo",
                        "label": "Consumers per demo",
                        "value": figures["consumers_per_demo"],
                    }
                )
    return tiles[:MAX_TILES]


def deck_quotes(draft, *, include_hidden: bool = False) -> list[dict]:
    """``[{text, hidden}]``: what consumers said, lightly trimmed. Theme
    quotes when the themes page has them, else the comments themselves."""
    from recaps.client_recap_draft import trim_quote

    facts = draft.facts or {}
    themes = facts.get("themes") or []
    raw = [t.get("quote") for t in themes if t.get("quote")] or list(facts.get("comments") or [])
    hidden = {str(q) for q in deck_for(draft).hidden_quotes or []}
    out: list[dict] = []
    seen: set[str] = set()
    for text in raw:
        quote = trim_quote(text)
        if not quote or quote.casefold() in seen:
            continue
        seen.add(quote.casefold())
        is_hidden = quote in hidden
        if is_hidden and not include_hidden:
            continue
        out.append({"text": quote, "hidden": is_hidden})
    return out if include_hidden else out[:MAX_QUOTES]


def no_product_note(draft) -> str:
    """The sentence the deck prints under its numbers, or ""."""
    figures = program(draft)
    left_out = figures["no_product_visits"]
    if not left_out:
        return ""
    return (
        f"{left_out:,} no-product visit{'s' if left_out != 1 else ''} "
        f"({'stores' if left_out != 1 else 'a store'} with no product on hand) "
        f"{'are' if left_out != 1 else 'is'} left out of the demo count and the per-demo "
        "figure. Totals are unchanged: those visits recorded nothing."
    )


# ─── Building ───────────────────────────────────────────────────────


def approved_recap_count(tenant_id: int, start, end) -> int:
    from recaps.tenant_overview import (
        _inclusive_dates_to_window,
        _tenant_event_recap_counts_window,
    )

    _events, recaps = _tenant_event_recap_counts_window(
        tenant_id, _inclusive_dates_to_window(start, end)
    )
    return int(recaps)


def start_deck(tenant, start, end, user):
    """Make a draft and its deck. Small programs are finished here; a big
    one is left ``building`` and handed to a Cloud Task when there is one
    (the cron finishes it either way)."""
    from recaps.client_recap_draft import create_draft
    from utils.cloud_tasks import enqueue

    inline = approved_recap_count(tenant.id, start, end) <= INLINE_MAX_RECAPS
    draft = create_draft(tenant, start, end, user, finish_words=inline)
    deck = deck_for(draft)
    if deck.status == ClientRecapDeck.STATUS_BUILDING:
        try:
            enqueue(TASK_PATH, {"deck": deck.id})
        except Exception:  # noqa: BLE001 — the cron is the safety net
            logger.exception("recap deck: enqueue failed for %s", deck.id)
    return draft


def _claim(deck_id: int) -> bool:
    now = timezone.now()
    return (
        ClientRecapDeck.objects.filter(id=deck_id, status=ClientRecapDeck.STATUS_BUILDING)
        .filter(Q(build_started_at__isnull=True) | Q(build_started_at__lt=now - STALE_AFTER))
        .update(build_started_at=now, build_attempts=F("build_attempts") + 1)
        == 1
    )


def finish_build(deck_id: int, *, generate=None) -> str:
    """Write the words for a deck left ``building``. Returns ``done``,
    ``gave_up``, or ``skipped`` (not building, or another worker has it)."""
    from recaps.client_recap_draft import (
        _inclusive_dates_to_window,
        _window_ba_names,
        compose_deck_text,
        feedback_themes,
    )

    if not _claim(deck_id):
        return "skipped"
    deck = ClientRecapDeck.objects.select_related("draft__tenant").get(id=deck_id)
    draft = deck.draft
    if deck.build_attempts > MAX_BUILD_ATTEMPTS:
        deck.status = ClientRecapDeck.STATUS_DRAFT
        deck.build_note = BUILD_GAVE_UP
        deck.save(update_fields=["status", "build_note", "updated_at"])
        return "gave_up"
    facts = dict(draft.facts or {})
    facts["themes"] = feedback_themes(draft.tenant_id, draft.start_date, draft.end_date)
    names = _window_ba_names(
        draft.tenant_id, _inclusive_dates_to_window(draft.start_date, draft.end_date)
    )
    words = compose_deck_text(facts, names, generate=generate)
    draft.facts = facts
    draft.summary = words["summary"]
    draft.takeaways = words["takeaways"]
    draft.text_source = words["source"]
    draft.save(update_fields=["facts", "summary", "takeaways", "text_source", "updated_at"])
    deck.next_steps = words["next_steps"]
    deck.status = ClientRecapDeck.STATUS_DRAFT
    deck.build_note = "" if words["source"] != "fallback" else BUILD_GAVE_UP
    deck.save(update_fields=["next_steps", "status", "build_note", "updated_at"])
    return "done"


def finish_pending_builds(*, max_decks: int = 5, max_seconds: int = 240) -> dict:
    """The cron: finish decks still ``building``, oldest first, within the
    caps. Sends nothing."""
    started = time.monotonic()
    counts = {"done": 0, "gave_up": 0, "skipped": 0, "failed": 0}
    stale = timezone.now() - STALE_AFTER
    ids = list(
        ClientRecapDeck.objects.filter(status=ClientRecapDeck.STATUS_BUILDING)
        .filter(Q(build_started_at__isnull=True) | Q(build_started_at__lt=stale))
        .order_by("id")
        .values_list("id", flat=True)[: max(1, int(max_decks))]
    )
    for deck_id in ids:
        if time.monotonic() - started > max_seconds:
            break
        try:
            counts[finish_build(deck_id)] += 1
        except Exception:  # noqa: BLE001 — the next run tries again
            logger.exception("recap deck: build failed for %s", deck_id)
            counts["failed"] += 1
    return counts


# ─── Editing and sharing ────────────────────────────────────────────


def _clean_lines(lines, limit: int, max_items: int) -> list[str]:
    return [
        " ".join(str(line).split())[:limit] for line in lines or [] if str(line or "").strip()
    ][:max_items]


def check_editable(draft) -> None:
    deck = deck_for(draft)
    if deck.status == ClientRecapDeck.STATUS_READY:
        raise DeckError(
            "This deck is shared with the brand. Move it back to draft to edit it."
        )
    if deck.status == ClientRecapDeck.STATUS_BUILDING:
        raise DeckError("The summary is still being written. Try again in a few minutes.")


def update_deck(
    draft,
    *,
    next_steps: list[str] | None = None,
    hidden_quotes: list[str] | None = None,
    kept_visits: list[str] | None = None,
) -> ClientRecapDeck:
    """Save the deck's own staff edits. Only what is passed changes."""
    from recaps.client_recap_draft import MAX_NEXT_STEP_CHARS, clean_photo_ids

    check_editable(draft)
    deck = deck_for(draft)
    fields: list[str] = []
    if next_steps is not None:
        deck.next_steps = _clean_lines(next_steps, MAX_NEXT_STEP_CHARS * 2, 5)
        fields.append("next_steps")
    if hidden_quotes is not None:
        known = {q["text"] for q in deck_quotes(draft, include_hidden=True)}
        deck.hidden_quotes = [q for q in dict.fromkeys(str(x) for x in hidden_quotes) if q in known]
        fields.append("hidden_quotes")
    if kept_visits is not None:
        listed = {
            str(v.get("key"))
            for v in ((draft.facts or {}).get("visits") or {}).get("excluded") or []
        }
        deck.kept_visits = [k for k in dict.fromkeys(str(x) for x in kept_visits) if k in listed]
        fields.append("kept_visits")
    if fields:
        deck.save(update_fields=[*fields, "updated_at"])
    if kept_visits is not None:
        # A visit left out again takes its photos with it.
        kept_photos = clean_photo_ids(
            draft.tenant_id,
            draft.photo_ids or [],
            skip_recaps=excluded_custom_ids((draft.facts or {}).get("visits"), deck.kept_visits),
        )
        if kept_photos != list(draft.photo_ids or []):
            draft.photo_ids = kept_photos
            draft.save(update_fields=["photo_ids", "updated_at"])
    return deck


def set_status(draft, status: str, user) -> ClientRecapDeck:
    """``ready`` shares the deck with the brand's users; ``draft`` takes it
    back. Nothing is sent either way."""
    deck = deck_for(draft)
    if status not in (ClientRecapDeck.STATUS_READY, ClientRecapDeck.STATUS_DRAFT):
        raise DeckError("Pick ready or draft.")
    if deck.status == ClientRecapDeck.STATUS_BUILDING:
        raise DeckError("The summary is still being written. Try again in a few minutes.")
    if status == ClientRecapDeck.STATUS_READY:
        if not (draft.title or "").strip():
            raise DeckError("Give the deck a title first.")
        deck.status = ClientRecapDeck.STATUS_READY
        deck.ready_at = timezone.now()
        deck.ready_by = user
    else:
        deck.status = ClientRecapDeck.STATUS_DRAFT
        deck.ready_at = None
        deck.ready_by = None
    deck.save(update_fields=["status", "ready_at", "ready_by", "updated_at"])
    return deck


def ready_decks(tenant_id: int, limit: int = 20):
    return (
        ClientRecapDeck.objects.filter(
            draft__tenant_id=tenant_id, status=ClientRecapDeck.STATUS_READY
        )
        .select_related("draft__tenant")
        .order_by("-ready_at", "-id")[: max(1, min(int(limit or 20), 50))]
    )


__all__ = [
    "DeckError",
    "deck_for",
    "deck_numbers",
    "deck_quotes",
    "deck_skip_recaps",
    "excluded_keys",
    "finish_build",
    "finish_pending_builds",
    "left_out_visits",
    "no_product_note",
    "program",
    "ready_decks",
    "set_status",
    "start_deck",
    "update_deck",
    "visits_to_check",
]
