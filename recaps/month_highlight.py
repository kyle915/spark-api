"""Highlight of the month: a best store, a quote and a photo, picked by rule.

``monthHighlight(tenantId, month)`` for one brand and one calendar month,
by EVENT date, from approved, non-archived recaps under live requests
(:func:`recaps.explain_facts.recap_facts`). Everything is chosen here, in
code. The model chooses nothing and writes neither the quote nor a number:
it may only reword the one caption sentence, and its wording is kept only
when every number in it is one it was given (:mod:`recaps.explain_text`).

THE RULES

* Floor: fewer than :data:`MIN_MONTH_RECAPS` approved recaps in the month
  and there is no highlight. Nor is there one when neither a best store nor
  a quote can be picked.
* Best store: stores are the Account Map's (:mod:`recaps.store_places`). A
  store qualifies with at least :data:`MIN_STORE_CONSUMERS` consumers
  sampled in the month and a conversion base. The highest conversion rate
  wins; ties go to more units sold, then more consumers sampled, then the
  store key. A recap that cannot be tied to one store counts for no store.
* Quote: a consumer comment from the month
  (:func:`recaps.feedback_themes.gather_comments`: comment fields only,
  never an ambassador's notes; names, emails and phone numbers removed), at
  least :data:`MIN_QUOTE_CHARS` characters. When the month's feedback
  themes are already stored for exactly these comments, the candidates are
  the quotes of its "liked" themes. Otherwise a comment is a candidate when
  it is a general comment (not a reason to decline, not a question), holds
  a word of praise and holds no word of complaint. The longest candidate
  wins; ties go to the newer event. The text is the stored comment.
* Photo: the first approved photo, oldest event first, in a category that
  reads as sampling or table set-up, from the best store's recaps when it
  has one, otherwise from any recap of the month. Never a receipt or other
  paperwork. Photo picks and scores are not on this branch's base; when
  they land, they belong ahead of this rule.

No ambassador name and no cost figure is read here. Same result for staff
and for the brand.

READ-ONLY.
"""

from __future__ import annotations

import calendar
import logging
import re
from datetime import date

import strawberry
from asgiref.sync import sync_to_async

from utils.graphql.permissions import StrictIsAuthenticated

logger = logging.getLogger(__name__)

KIND = "month_highlight"
MIN_MONTH_RECAPS = 5
MIN_STORE_CONSUMERS = 30
MIN_QUOTE_CHARS = 40

_PRAISE_RE = re.compile(
    r"\b(lov(?:e|ed|es|ing)|great|delicious|favou?rite|amazing|awesome|"
    r"enjoy(?:ed|s|ing)?|tasty|refreshing|excellent|perfect|fantastic|"
    r"wonderful|impressed|obsessed|best|liked|likes|smooth|yum(?:my)?|"
    r"will (?:buy|purchase)|bought|buying)\b",
    re.IGNORECASE,
)
_COMPLAINT_RE = re.compile(
    r"\b(not|no|never|n't|dont|didnt|but|too|bad|hate[ds]?|dislike[ds]?|"
    r"expensive|pricey|bitter|bland|gross|awful|terrible|weird|strange|"
    r"confus\w+|disappoint\w+|unfortunately|however|complain\w*|worse|worst)\b"
    r"|n't\b",
    re.IGNORECASE,
)
_SAMPLING_PHOTO_RE = re.compile(r"sampl|table|set[\s-]?up|display|booth|footprint", re.I)


@strawberry.type
class HighlightStore:
    store_key: str
    name: str
    market: str
    conversion: float
    consumers: int
    units_sold: int
    recaps: int
    # The store's recap with the most units sold that month, to open.
    recap_uuid: str | None
    recap_kind: str | None


@strawberry.type
class HighlightQuote:
    text: str
    recap_uuid: str | None
    recap_kind: str | None


@strawberry.type
class HighlightPhoto:
    url: str
    category: str
    recap_uuid: str


@strawberry.type
class MonthHighlight:
    # "2026-10"
    month: str
    # "October 2026"
    label: str
    caption: str
    # "model" or "template"
    caption_source: str
    recaps: int
    store: HighlightStore | None
    quote: HighlightQuote | None
    photo: HighlightPhoto | None


def parse_month(raw) -> tuple[date, date] | None:
    """First and last day of "YYYY-MM", or None."""
    match = re.fullmatch(r"(\d{4})-(\d{2})", str(raw or "").strip())
    if not match:
        return None
    year, month = int(match.group(1)), int(match.group(2))
    if not (2000 <= year <= 2100 and 1 <= month <= 12):
        return None
    return date(year, month, 1), date(year, month, calendar.monthrange(year, month)[1])


def pick_store(facts: list[dict], store_of: dict[int, str]) -> dict | None:
    """The best store among per-recap ``facts``; ``store_of`` maps an event
    to its store key."""
    stores: dict[str, dict] = {}
    for fact in facts:
        key = store_of.get(fact["event_id"])
        if not key:
            continue
        held = stores.setdefault(
            key,
            {"key": key, "consumers": 0, "units": 0, "base": 0, "recaps": 0, "top": None},
        )
        held["recaps"] += 1
        held["consumers"] += fact["consumers"] or 0
        if fact["base"]:
            held["base"] += fact["base"]
            held["units"] += fact["units"] or 0
            top = held["top"]
            if top is None or (fact["units"] or 0, -fact["id"]) > (top["units"] or 0, -top["id"]):
                held["top"] = fact
    qualified = [
        s for s in stores.values() if s["base"] > 0 and s["consumers"] >= MIN_STORE_CONSUMERS
    ]
    if not qualified:
        return None
    for store in qualified:
        store["conversion"] = round(store["units"] / store["base"] * 100, 1)

    def _number(key: str) -> int:
        return int(key[1:]) if key[1:].isdigit() else 0

    qualified.sort(
        key=lambda s: (-s["conversion"], -s["units"], -s["consumers"], _number(s["key"]))
    )
    return qualified[0]


def pick_quote(comments, liked_quotes: list[str] | None = None):
    """The standout comment, or None. ``comments`` come newest event first."""
    from recaps.feedback_themes import KIND_GENERAL

    def _matches_liked(text: str) -> bool:
        for quote in liked_quotes or ():
            stem = quote.rstrip("…").strip()
            if stem and text.startswith(stem):
                return True
        return False

    candidates = []
    for order, comment in enumerate(comments):
        text = comment.text
        if len(text) < MIN_QUOTE_CHARS:
            continue
        if liked_quotes:
            ok = _matches_liked(text)
        else:
            ok = (
                comment.kind == KIND_GENERAL
                and bool(_PRAISE_RE.search(text))
                and not _COMPLAINT_RE.search(text)
            )
        if ok:
            candidates.append((-len(text), order, comment))
    if not candidates:
        return None
    candidates.sort(key=lambda item: (item[0], item[1]))
    return candidates[0][2]


def _stored_liked_quotes(tenant_id: int, comments) -> list[str] | None:
    """Quotes of the "liked" themes, when themes are stored for exactly
    these comments. Reads a row; never builds themes."""
    from recaps.feedback_themes import SECTION_LIKED, source_hash
    from tenants.models import TenantFeedbackThemes

    row = TenantFeedbackThemes.objects.filter(
        tenant_id=tenant_id, source_hash=source_hash(comments)
    ).first()
    themes = (row.payload or {}).get("themes") if row is not None else None
    if not isinstance(themes, list):
        return None
    quotes = [
        t["quote"]
        for t in themes
        if isinstance(t, dict) and t.get("section") == SECTION_LIKED and t.get("quote")
    ]
    return quotes or None


def _pick_photo(tenant_id: int, start: date, end: date, preferred_ids: list[int]):
    from recaps.photo_gallery import _base_queryset, _image_url
    from recaps.types import _PAPERWORK_CATEGORY_RE

    base = (
        _base_queryset(tenant_id, start, end)
        .exclude(file_recap_category__name__iregex=_PAPERWORK_CATEGORY_RE.pattern)
        .filter(file_recap_category__name__iregex=_SAMPLING_PHOTO_RE.pattern)
        .select_related("file_recap_category", "custom_recap")
        .order_by("custom_recap__event__date", "custom_recap_id", "id")
    )
    pools = [base.filter(custom_recap_id__in=preferred_ids)] if preferred_ids else []
    pools.append(base)
    for pool in pools:
        for f in pool[:20]:
            url = _image_url(f)
            if url:
                return HighlightPhoto(
                    url=url,
                    category=getattr(f.file_recap_category, "name", None) or "",
                    recap_uuid=str(f.custom_recap.uuid),
                )
    return None


def _recap_uuids(keys: set[tuple[str, int]]) -> dict[tuple[str, int], str]:
    from recaps.models import CustomRecap, Recap

    out: dict[tuple[str, int], str] = {}
    for kind, model in (("custom", CustomRecap), ("legacy", Recap)):
        ids = [pk for k, pk in keys if k == kind]
        if ids:
            for pk, uuid in model.objects.filter(id__in=ids).values_list("id", "uuid"):
                out[(kind, pk)] = str(uuid)
    return out


def caption_draft(label: str, store: dict | None, recaps: int):
    from recaps.explain_text import Draft, n, pct, plural

    if store is None:
        template = f"{label} had {plural(recaps, 'approved recap')}."
        return Draft("caption", template, f"Month: {label}.\nApproved recaps: {n(recaps)}.")
    place = store["name"] or "One store"
    if store["market"]:
        place += f" in {store['market']}"
    template = (
        f"{place} had the highest conversion rate in {label}: "
        f"{pct(store['conversion'])}% from {n(store['consumers'])} consumers sampled, "
        f"with {n(store['units'])} units sold."
    )
    facts = "\n".join(
        [
            f"Month: {label}.",
            f"Store: {place}.",
            f"Its conversion rate: {pct(store['conversion'])}%.",
            f"Consumers sampled there: {n(store['consumers'])}.",
            f"Units sold there: {n(store['units'])}.",
        ]
    )
    return Draft("caption", template, facts)


def build_month_highlight(tenant_id: int, raw_month) -> MonthHighlight | None:
    from recaps import explain_text
    from recaps.explain_facts import recap_facts
    from recaps.feedback_themes import gather_comments
    from recaps.store_places import store_header, tenant_places

    bounds = parse_month(raw_month)
    if bounds is None:
        return None
    start, end = bounds
    facts = recap_facts(tenant_id, start, end)
    if len(facts) < MIN_MONTH_RECAPS:
        return None

    places = tenant_places(tenant_id)
    store = pick_store(facts, places.for_event_ids(f["event_id"] for f in facts))
    comments, _cut = gather_comments(tenant_id, start, end)
    chosen = pick_quote(comments, _stored_liked_quotes(tenant_id, comments))
    if store is None and chosen is None:
        return None

    wanted: set[tuple[str, int]] = set()
    if store is not None and store["top"] is not None:
        wanted.add((store["top"]["kind"], store["top"]["id"]))
    if chosen is not None:
        quote_key = ("custom" if chosen.recap.startswith("C") else "legacy", int(chosen.recap[1:]))
        wanted.add(quote_key)
    uuids = _recap_uuids(wanted)

    preferred: list[int] = []
    store_type = None
    if store is not None:
        header = store_header(tenant_id, places, store["key"])
        store["name"] = header["name"]
        store["market"] = ", ".join(p for p in (header["city"], header["state"]) if p)
        store_events = {
            pk for pk, key in places.for_event_ids(f["event_id"] for f in facts).items()
            if key == store["key"]
        }
        preferred = [
            f["id"] for f in facts if f["kind"] == "custom" and f["event_id"] in store_events
        ]
        top = store["top"]
        store_type = HighlightStore(
            store_key=store["key"],
            name=store["name"],
            market=store["market"],
            conversion=store["conversion"],
            consumers=store["consumers"],
            units_sold=store["units"],
            recaps=store["recaps"],
            recap_uuid=uuids.get((top["kind"], top["id"])) if top else None,
            recap_kind=top["kind"] if top else None,
        )

    label = f"{calendar.month_name[start.month]} {start.year}"
    month = f"{start.year:04d}-{start.month:02d}"
    written = explain_text.write(
        tenant_id, KIND, month, [caption_draft(label, store, len(facts))]
    )[0]

    quote_type = None
    if chosen is not None:
        quote_type = HighlightQuote(
            text=chosen.text,
            recap_uuid=uuids.get(quote_key),
            recap_kind=quote_key[0] if uuids.get(quote_key) else None,
        )
    return MonthHighlight(
        month=month,
        label=label,
        caption=written.text,
        caption_source=written.source,
        recaps=len(facts),
        store=store_type,
        quote=quote_type,
        photo=_pick_photo(tenant_id, start, end, preferred),
    )


@strawberry.type
class MonthHighlightQueries:
    @strawberry.field(permission_classes=[StrictIsAuthenticated])
    async def month_highlight(
        self, info: strawberry.Info, tenant_id: strawberry.ID, month: str
    ) -> MonthHighlight | None:
        """The highlight for a brand and a "YYYY-MM" month. Null when the
        month has too little to show, so the screen shows nothing."""
        from recaps.explain_schema import _scope

        target, _staff = await _scope(info, tenant_id)
        if target is None:
            return None
        try:
            return await sync_to_async(
                lambda: build_month_highlight(target, month), thread_sensitive=True
            )()
        except Exception as exc:  # noqa: BLE001
            logger.exception("monthHighlight failed tenant=%s: %s", target, exc)
            return None
