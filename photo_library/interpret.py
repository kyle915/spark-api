"""Turn a typed search ("best crowd photos at Kroger in Texas") into photo
library filters.

The model only fills a fixed form (strict JSON schema): which tag keys,
which of the brand's products, a store word, state codes, dates, a minimum
quality, a sort. Every value is checked again here against the same closed
lists the search accepts (:func:`photo_library.search.clean_filters`), so a
reply can never add a filter the search does not know. The model never
writes SQL and never sees a photo, a file name or a person's name: it gets
the typed words, today's date and the brand's product names.

Without the model (no key, or a bad reply) the typed words are searched as
free text.
"""

from __future__ import annotations

import logging
from datetime import date

from photo_library.vocabulary import (
    CROWD_LABELS,
    CROWD_SIZES,
    SEARCHABLE_SHOWS,
    SETTING_LABELS,
    SETTINGS,
    SHOW_LABELS,
    SORTS,
    US_STATES,
)
from utils.ai_text import AiUnavailable, generate_json

logger = logging.getLogger(__name__)

MAX_QUERY = 200

SYSTEM = (
    "You turn a person's search for field-marketing photos into filters for "
    "a photo library. Fill only the form. Use a tag key only when the words "
    "ask for it. shows: product_on_table (product set out on a sampling "
    "table), shelf_display (product on a store shelf or cooler), "
    "consumers_engaging (shoppers at the table trying or looking at "
    "product; use it for crowd, busy, engagement, people sampling), "
    "ba_at_table (someone staffing the table), signage (banners, posters, "
    "branded signs), store_exterior (store front). crowdSizes: few is 1 to "
    "5 people, group is 6 to 15, crowd is more than 15; for 'crowd' or "
    "'busy' use group and crowd. settings: indoor_store, outdoor_event, "
    "bar_restaurant. products: only names from the brand's list. store: a "
    "retailer, chain or store name the person typed (for example Kroger or "
    "H-E-B #614), else empty. states: two-letter US state codes the person "
    "named. startDate and endDate: YYYY-MM-DD when the person named a time, "
    "using today's date to resolve 'last month' or 'this summer', else "
    "null. minQuality: 70 when the person asks for best, great or top "
    "photos, else null. hideIssues: true when the person asks for good, "
    "clear or usable photos. peoplePresent: true or false only when the "
    "person asks for photos with or without people, else null. sort: "
    "quality for best or top photos, newest for recent or latest, else "
    "relevance. text: the few words left that none of the fields cover "
    "(for example 'cooler' or 'tent'), else empty; never repeat a store, "
    "state, product or date there."
)


def _schema(product_labels: list[str]) -> dict:
    products = {"type": "string", "enum": product_labels or [""]}
    return {
        "type": "object",
        "additionalProperties": False,
        "required": [
            "text",
            "shows",
            "crowdSizes",
            "settings",
            "products",
            "store",
            "states",
            "startDate",
            "endDate",
            "minQuality",
            "hideIssues",
            "peoplePresent",
            "sort",
        ],
        "properties": {
            "text": {"type": "string"},
            "shows": {"type": "array", "items": {"type": "string", "enum": list(SEARCHABLE_SHOWS)}},
            "crowdSizes": {"type": "array", "items": {"type": "string", "enum": list(CROWD_SIZES)}},
            "settings": {"type": "array", "items": {"type": "string", "enum": list(SETTINGS)}},
            "products": {"type": "array", "items": products},
            "store": {"type": "string"},
            "states": {"type": "array", "items": {"type": "string", "enum": sorted(US_STATES)}},
            "startDate": {"type": ["string", "null"]},
            "endDate": {"type": ["string", "null"]},
            "minQuality": {"type": ["integer", "null"]},
            "hideIssues": {"type": "boolean"},
            "peoplePresent": {"type": ["boolean", "null"]},
            "sort": {"type": "string", "enum": list(SORTS)},
        },
    }


def reply_to_filters(parsed: dict, catalog: list[tuple[int, str]]) -> dict:
    """A model reply → the raw filter dict the search cleans. Products are
    matched to the brand's catalog by label; anything else is dropped."""
    by_label = {label.lower(): pid for pid, label in catalog}
    product_ids = []
    for label in parsed.get("products") or []:
        pid = by_label.get(str(label or "").strip().lower())
        if pid is not None and pid not in product_ids:
            product_ids.append(pid)
    min_quality = parsed.get("minQuality")
    return {
        "text": parsed.get("text") or "",
        "shows": parsed.get("shows") or [],
        "crowd_sizes": parsed.get("crowdSizes") or [],
        "settings": parsed.get("settings") or [],
        "product_ids": product_ids,
        "store": parsed.get("store") or "",
        "states": parsed.get("states") or [],
        "start_date": parsed.get("startDate"),
        "end_date": parsed.get("endDate"),
        "min_quality": min_quality if isinstance(min_quality, int) else None,
        "hide_issues": parsed.get("hideIssues") is True,
        "people_present": parsed.get("peoplePresent")
        if isinstance(parsed.get("peoplePresent"), bool)
        else None,
        "sort": parsed.get("sort") or "",
    }


def interpret(text: str, tenant_id: int, *, today: date | None = None) -> dict:
    """``{"ok", "reason", "raw"}``: ``raw`` is a filter dict for
    :func:`photo_library.search.clean_filters`. ``ok`` is False only when
    the words were searched as typed because the model could not help."""
    from photo_library.vision import catalog_labels

    words = " ".join(str(text or "").split())[:MAX_QUERY]
    if not words:
        return {"ok": True, "reason": "", "raw": {}}
    catalog = catalog_labels(tenant_id)
    if today is None:
        # Pacific "today", not the server's UTC date.
        from events.live_board import _ops_today

        today = _ops_today()
    user = (
        f"Today is {today.isoformat()}.\n"
        f"The brand's products: {', '.join(label for _p, label in catalog) or 'none listed'}.\n"
        f"Search: {words}"
    )
    try:
        parsed = generate_json(
            SYSTEM,
            user,
            schema=_schema([label for _p, label in catalog]),
            max_tokens=400,
            small=True,
            timeout=15,
        )
    except AiUnavailable:
        parsed = None
    if not isinstance(parsed, dict):
        return {
            "ok": False,
            "reason": "Searched your words as typed.",
            "raw": {"text": words},
        }
    return {"ok": True, "reason": "", "raw": reply_to_filters(parsed, catalog)}


def filter_chips(filters, product_names: dict[int, str]) -> list[str]:
    """Plain words for the filters in use, for the chips under the box."""
    chips: list[str] = []
    chips += [SHOW_LABELS[k] for k in filters.shows]
    chips += [CROWD_LABELS[k] for k in filters.crowd_sizes]
    chips += [SETTING_LABELS[k] for k in filters.settings]
    chips += [product_names.get(p, "") for p in filters.product_ids]
    if filters.store:
        chips.append(filters.store)
    chips += filters.states
    if filters.start or filters.end:
        chips.append(
            " to ".join(d.isoformat() for d in (filters.start, filters.end) if d)
        )
    if filters.min_quality is not None:
        chips.append(f"Quality {filters.min_quality}+")
    if filters.hide_issues:
        chips.append("No blurry or dark photos")
    if filters.people_present is True:
        chips.append("People in frame")
    elif filters.people_present is False:
        chips.append("No people in frame")
    if filters.text:
        chips.append(f"“{filters.text}”")
    return [c for c in chips if c]
