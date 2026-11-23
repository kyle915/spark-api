"""Buyer Pitch Deck sell-story captions from facts Spark already has.

No new vision calls. Captions are drafted from:

* existing :class:`recaps.models.RecapPhotoScore` captions / kinds (vision
  already ran for photo picks),
* consumer mention themes on the photo's recap
  (:class:`consumer_mentions.models.RecapMentionTag`),
* shelf reading answers when the photo is a shelf shot
  (:class:`recaps.models.ShelfReading` / staff correction).

Staff pin notes always win and are never overwritten. Dollars, lift, and
visit counts for the deck-level sell-story line come only from the KPI
snapshot already computed for the deck — the model may reword, never invent.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from typing import Any

from django.core.cache import cache

logger = logging.getLogger(__name__)

MAX_CAPTION_CHARS = 140
MAX_SELL_STORY_CHARS = 360
MAX_PHOTOS_FOR_MODEL = 12
MAX_MENTION_THEMES = 4
CACHE_SECONDS = 30 * 24 * 3600

_NUMBER_RE = re.compile(r"\d+(?:[.,]\d+)*")
_PERSON_RE = re.compile(
    r"\b(person|people|man|woman|guy|girl|worker|ambassador|ba|rep|staff|"
    r"customer|shopper|he|she|they|his|her|their)\b",
    re.IGNORECASE,
)

_CAPTION_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["captions", "sell_story"],
    "properties": {
        "captions": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["id", "caption"],
                "properties": {
                    "id": {"type": "string"},
                    "caption": {"type": "string"},
                },
            },
        },
        "sell_story": {"type": "string"},
    },
}

_SYSTEM = (
    "You write short buyer-facing captions for a field-marketing pitch deck. "
    "Use ONLY the facts given for each photo and the KPI lines for the "
    "sell-story. Do not add, round, or invent any number. Do not name or "
    "describe any person. Plain business English, no hype, no exclamation "
    "marks. Each caption: one sentence, max 24 words, sentence case. "
    "sell_story: one or two short sentences that frame the program for a "
    "retail buyer, grounded only in the KPI lines and mention themes given."
)


def _sentence(text: str) -> str:
    text = " ".join((text or "").split()).strip(" .\"'“”")
    if not text:
        return ""
    if len(text) > MAX_CAPTION_CHARS:
        text = text[: MAX_CAPTION_CHARS - 1].rstrip() + "…"
    return text[:1].upper() + text[1:] + ("." if not text.endswith((".", "!", "?")) else "")


def _clean_caption(raw: Any) -> str:
    text = _sentence(str(raw or ""))
    if not text or _PERSON_RE.search(text):
        return ""
    return text.rstrip(".")


def _allowed_numbers(*parts: Any) -> set[str]:
    allowed: set[str] = set()
    for part in parts:
        for found in _NUMBER_RE.findall(json.dumps(part, default=str)):
            allowed.add(found)
            allowed.add(found.replace(",", ""))
    return allowed


def _numbers_ok(text: str, allowed: set[str]) -> bool:
    for found in _NUMBER_RE.findall(text or ""):
        if found not in allowed and found.replace(",", "") not in allowed:
            return False
    return True


def rules_photo_caption(facts: dict[str, Any]) -> str:
    """Deterministic caption from score / shelf / mentions / store."""
    pin_note = (facts.get("pin_note") or "").strip()
    if pin_note:
        return _clean_caption(pin_note) or pin_note[:MAX_CAPTION_CHARS]

    store = (facts.get("store") or facts.get("retailer") or "").strip()
    category = (facts.get("category") or "").strip()
    score_caption = (facts.get("score_caption") or "").strip()
    kind = (facts.get("kind") or "").strip()
    themes = [t for t in (facts.get("mention_themes") or []) if t]
    shelf = facts.get("shelf") or {}
    skus = [s for s in (facts.get("skus") or []) if s]

    if shelf.get("product_on_shelf") == "yes":
        bits = []
        if store:
            bits.append(f"On shelf at {store}")
        else:
            bits.append("On shelf")
        facings = shelf.get("facings")
        if facings is not None:
            bits.append(f"{facings} facing{'s' if int(facings) != 1 else ''}")
        price = shelf.get("price")
        if price is not None:
            bits.append(f"${price}")
        return _sentence(", ".join(bits))

    if shelf.get("product_on_shelf") == "no":
        where = f" at {store}" if store else ""
        return _sentence(f"Gap on shelf{where} — product not found in frame")

    if score_caption and not _PERSON_RE.search(score_caption):
        base = _clean_caption(score_caption) or score_caption[:MAX_CAPTION_CHARS]
        if themes and themes[0].lower() not in base.lower():
            return _sentence(f"{base.rstrip('.')} — shoppers mentioned {themes[0]}")
        return _sentence(base)

    if kind == "table_setup" or "set" in category.lower():
        where = f" at {store}" if store else ""
        sku_bit = f" featuring {skus[0]}" if skus else ""
        return _sentence(f"Demo table set-up{where}{sku_bit}")

    if themes:
        where = f" at {store}" if store else ""
        return _sentence(f"In-store sampling{where}: consumers mentioned {themes[0]}")

    if store and category:
        return _sentence(f"{category} at {store}")
    if store:
        return _sentence(f"In-store demo at {store}")
    if category:
        return _sentence(category)
    return ""


def rules_sell_story(
    *,
    brand_name: str,
    kpis: dict[str, Any] | None,
    mention_themes: list[str],
    period_start: str,
    period_end: str,
) -> str:
    """Deck-level leave-behind line from KPI snapshot + mention themes only."""
    kpis = kpis or {}
    brand = (brand_name or "The brand").strip() or "The brand"
    visits = int(kpis.get("totalVisits") or 0)
    stores = int(kpis.get("storesActivated") or 0)
    samples = int(kpis.get("totalSamplesDistributed") or 0)
    lift = kpis.get("samplingLiftPct")
    parts = [
        f"{brand} ran {visits} visit{'s' if visits != 1 else ''} "
        f"across {stores} store{'s' if stores != 1 else ''} "
        f"from {period_start} to {period_end}"
    ]
    if samples:
        parts[0] += f", handing out {samples} sample{'s' if samples != 1 else ''}"
    parts[0] += "."
    if lift is not None:
        parts.append(f"Matched POS sampling lift in scope: {lift}%.")
    if mention_themes:
        parts.append(f"Consumers talked about {', '.join(mention_themes[:3])}.")
    return " ".join(parts)[:MAX_SELL_STORY_CHARS]


def _photo_facts_for_ids(
    tenant_id: int, photo_ids: list[int]
) -> dict[int, dict[str, Any]]:
    """Load score / shelf / mention facts for deck photos. Tenant-scoped."""
    from consumer_mentions.models import RecapMentionTag
    from recaps.models import CustomRecapFile
    from recaps.photo_picks import _related
    from recaps.shelf_check import effective_answers

    if not photo_ids:
        return {}
    rows = (
        CustomRecapFile.objects.filter(
            id__in=photo_ids,
            custom_recap__tenant_id=tenant_id,
        )
        .select_related(
            "photo_score",
            "shelf_reading",
            "shelf_correction",
            "custom_recap",
            "file_recap_category",
        )
    )
    by_id: dict[int, dict[str, Any]] = {}
    recap_ids: set[int] = set()
    for rf in rows:
        recap = rf.custom_recap
        if recap is None:
            continue
        score = _related(rf, "photo_score")
        shelf = effective_answers(
            getattr(rf, "shelf_reading", None),
            getattr(rf, "shelf_correction", None),
        )
        by_id[rf.id] = {
            "file_id": rf.id,
            "recap_id": recap.id,
            "score_caption": (score.caption if score is not None else "") or "",
            "kind": (score.kind if score is not None else "") or "",
            "score": score.score if score is not None else None,
            "shelf": shelf or {},
            "mention_themes": [],
        }
        recap_ids.add(recap.id)

    if recap_ids:
        keys = [f"C{rid}" for rid in recap_ids]
        tags = RecapMentionTag.objects.filter(
            tenant_id=tenant_id, recap_key__in=keys
        )
        themes_by_recap: dict[int, list[str]] = {}
        for tag in tags:
            try:
                rid = int(str(tag.recap_key)[1:])
            except (TypeError, ValueError):
                continue
            labels: list[str] = []
            for theme in tag.themes or []:
                if not isinstance(theme, dict):
                    continue
                label = (theme.get("label") or theme.get("theme") or "").strip()
                if label and label.lower() != "other" and label not in labels:
                    labels.append(label)
                if len(labels) >= MAX_MENTION_THEMES:
                    break
            themes_by_recap[rid] = labels
        for facts in by_id.values():
            facts["mention_themes"] = themes_by_recap.get(facts["recap_id"], [])
    return by_id


def _cache_key(payload: dict[str, Any]) -> str:
    raw = json.dumps(payload, sort_keys=True, default=str)
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:40]
    return f"buyer_deck_captions:v1:{digest}"


def _model_polish(
    photo_prompts: list[dict[str, Any]],
    *,
    sell_story_facts: dict[str, Any],
) -> dict[str, Any] | None:
    from utils.ai_text import AiUnavailable, generate_json

    user = (
        "PHOTO FACTS (draft a caption per id; keep each id):\n"
        + json.dumps(photo_prompts, indent=2, default=str)
        + "\n\nSELL-STORY FACTS:\n"
        + json.dumps(sell_story_facts, indent=2, default=str)
    )
    try:
        raw = generate_json(
            _SYSTEM, user, schema=_CAPTION_SCHEMA, max_tokens=900, small=True
        )
    except AiUnavailable:
        return None
    except Exception:  # noqa: BLE001
        logger.exception("buyer deck captions: model call failed")
        return None
    return raw if isinstance(raw, dict) else None


def enrich_deck_photos(
    tenant_id: int,
    photos: list[dict[str, Any]],
    *,
    brand_name: str = "",
    kpis: dict[str, Any] | None = None,
    period_start: str = "",
    period_end: str = "",
    intro_note: str = "",
) -> tuple[list[dict[str, Any]], str, str]:
    """Enrich deck photo captions + optional sell-story.

    Returns ``(photos, sell_story, sell_story_source)``. Photo dicts gain
    ``captionSource`` (``pin`` / ``rules`` / ``model``). Existing non-empty
    pin notes stay. Never raises.
    """
    if not photos:
        sell = (intro_note or "").strip()
        if sell:
            return photos, sell[:MAX_SELL_STORY_CHARS], "staff"
        return photos, "", "rules"

    photo_ids: list[int] = []
    for p in photos:
        try:
            photo_ids.append(int(str(p.get("id"))))
        except (TypeError, ValueError):
            continue
    facts_by_id = _photo_facts_for_ids(tenant_id, photo_ids)

    enriched: list[dict[str, Any]] = []
    rules_captions: dict[str, str] = {}
    all_themes: list[str] = []
    for p in photos:
        row = dict(p)
        pid = None
        try:
            pid = int(str(p.get("id")))
        except (TypeError, ValueError):
            pid = None
        facts = dict(facts_by_id.get(pid) or {})
        facts.update(
            {
                "store": p.get("store") or "",
                "retailer": p.get("retailer") or "",
                "category": p.get("category") or "",
                "skus": list(p.get("skus") or []),
                "pin_note": (p.get("pin_note") or "").strip(),
                # Prefer explicit pin note; fall back to caption already on
                # the photo (pin note || scorer) when pin_note was not copied.
                "score_caption": facts.get("score_caption")
                or ((p.get("caption") or "").strip() if not (p.get("pin_note") or "").strip() else ""),
            }
        )
        # If the photo wall already put a pin note into caption, treat as pin.
        pin_note = (p.get("pin_note") or "").strip()
        if pin_note:
            caption = _clean_caption(pin_note) or pin_note[:MAX_CAPTION_CHARS]
            source = "pin"
        else:
            caption = rules_photo_caption(facts)
            source = "rules" if caption else "rules"
        row["caption"] = caption
        row["captionSource"] = source
        # Strip internal-only keys from snapshot photo records.
        row.pop("pin_note", None)
        row.pop("recap_uuid", None)
        row.pop("is_pinned", None)
        row.pop("pinned_at", None)
        enriched.append(row)
        if caption and source != "pin":
            rules_captions[str(p.get("id"))] = caption
        for theme in facts.get("mention_themes") or []:
            if theme not in all_themes:
                all_themes.append(theme)

    sell_rules = rules_sell_story(
        brand_name=brand_name,
        kpis=kpis,
        mention_themes=all_themes,
        period_start=period_start,
        period_end=period_end,
    )
    if (intro_note or "").strip():
        return enriched, intro_note.strip()[:MAX_SELL_STORY_CHARS], "staff"

    # Model polish: one call for captions that are not staff pin notes + sell story.
    polish_inputs = []
    for p, row in zip(photos, enriched):
        if row.get("captionSource") == "pin":
            continue
        pid = str(p.get("id"))
        facts = facts_by_id.get(int(pid)) if str(pid).isdigit() else None
        polish_inputs.append(
            {
                "id": pid,
                "store": p.get("store") or "",
                "category": p.get("category") or "",
                "skus": list(p.get("skus") or [])[:4],
                "kind": (facts or {}).get("kind") or "",
                "score_caption": (facts or {}).get("score_caption") or "",
                "mention_themes": (facts or {}).get("mention_themes") or [],
                "shelf": (facts or {}).get("shelf") or {},
                "rules_caption": row.get("caption") or "",
            }
        )
        if len(polish_inputs) >= MAX_PHOTOS_FOR_MODEL:
            break

    sell_facts = {
        "brand": brand_name,
        "period_start": period_start,
        "period_end": period_end,
        "kpis": {
            k: kpis.get(k)
            for k in (
                "totalVisits",
                "storesActivated",
                "totalSamplesDistributed",
                "repObservedUnits",
                "adjustedRegisterUnits",
                "samplingLiftPct",
                "posCoverageLabel",
            )
            if kpis and kpis.get(k) is not None
        },
        "mention_themes": all_themes[:MAX_MENTION_THEMES],
        "rules_sell_story": sell_rules,
    }
    cache_payload = {"photos": polish_inputs, "sell": sell_facts}
    key = _cache_key(cache_payload)
    cached = cache.get(key)
    raw = cached if isinstance(cached, dict) else None
    if raw is None and polish_inputs:
        raw = _model_polish(polish_inputs, sell_story_facts=sell_facts)
        if raw is not None:
            cache.set(key, raw, CACHE_SECONDS)

    sell_source = "rules"
    sell_story = sell_rules
    if isinstance(raw, dict):
        allowed_sell = _allowed_numbers(sell_facts)
        model_sell = _clean_caption(raw.get("sell_story"))
        if (
            model_sell
            and _numbers_ok(model_sell, allowed_sell)
            and len(model_sell) <= MAX_SELL_STORY_CHARS
        ):
            sell_story = model_sell if model_sell.endswith(".") else model_sell + "."
            sell_source = "model"

        by_model_id = {}
        for item in raw.get("captions") if isinstance(raw.get("captions"), list) else []:
            if not isinstance(item, dict):
                continue
            cid = str(item.get("id") or "").strip()
            if cid:
                by_model_id[cid] = item.get("caption")
        for row in enriched:
            if row.get("captionSource") == "pin":
                continue
            cid = str(row.get("id") or "")
            candidate = _clean_caption(by_model_id.get(cid))
            if not candidate:
                continue
            allowed = _allowed_numbers(
                rules_captions.get(cid, ""),
                next((p for p in polish_inputs if p["id"] == cid), {}),
            )
            if _numbers_ok(candidate, allowed):
                row["caption"] = candidate if candidate.endswith(".") else candidate + "."
                row["captionSource"] = "model"

    return enriched, sell_story, sell_source


def overlay_snapshot_captions(
    reminted_photos: list[dict[str, Any]],
    snapshot_photos: list[dict[str, Any]] | None,
) -> list[dict[str, Any]]:
    """Keep reminted URLs; restore sell-story captions saved at deck create."""
    by_id = {
        str(p.get("id")): p
        for p in (snapshot_photos or [])
        if isinstance(p, dict) and p.get("id") is not None
    }
    out: list[dict[str, Any]] = []
    for photo in reminted_photos:
        row = dict(photo)
        snap = by_id.get(str(photo.get("id")))
        if snap and (snap.get("caption") or "").strip():
            # Staff pin notes on the live wall win over a stale snapshot caption.
            live_pin = (photo.get("pin_note") or "").strip()
            if not live_pin:
                row["caption"] = snap["caption"]
                if snap.get("captionSource"):
                    row["captionSource"] = snap["captionSource"]
        row.pop("pin_note", None)
        out.append(row)
    return out
