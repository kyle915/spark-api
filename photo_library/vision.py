"""One look at one recap photo, shared by the best-photo scorer and the
photo library.

The best-photo scorer (``recaps/photo_picks.py``) already sends each photo it
opens to the vision model once. This module widens that one call so the same
reply also carries the library tags (what the photo shows, which of the
brand's products are visible, the crowd, the setting, the quality issues),
and both features store from it. A photo is never sent twice: whichever pass
reaches it first makes the call, and the other reuses the stored reply.

The model is told not to identify or describe anyone. It may say only
whether people are in the frame and roughly how many. The caption is cleaned
with the scorer's own rule (no person words, no ambassador names).
"""

from __future__ import annotations

from photo_library.vocabulary import CROWD_SIZES, ISSUES, SETTINGS, SHOWS

# The scorer's kinds, unchanged (recaps.photo_picks.KINDS).
KINDS = (
    "table_setup",
    "product_display",
    "shelf",
    "sampling",
    "signage",
    "store_exterior",
    "receipt",
    "screenshot",
    "other",
)

# Longest catalog list put in the prompt. Above this, products are matched
# from the caption words only (a very long enum costs tokens on every call).
MAX_CATALOG_IN_PROMPT = 80
MAX_PRODUCT_LABEL = 80

SYSTEM = (
    "You judge one field-marketing photo for use in a client report and tag "
    "it for a searchable photo library. "
    "Look only at the setup, the product, the branding and the setting. "
    "Do not identify, recognize or describe any person. You may say only "
    "whether people are in the frame (peoplePresent), roughly how many "
    "(crowdSize: none, few is 1 to 5, group is 6 to 15, crowd is more than "
    "15), whether shoppers are at the table or display trying or looking at "
    "the product (shows 'consumers_engaging') and whether someone is "
    "staffing the table (shows 'ba_at_table'). Never mention anyone's "
    "appearance, clothing, age, expression or name, anywhere. "
    "kind is what the photo mainly shows. A photo of a paper or on-screen "
    "receipt is 'receipt'. A phone or computer screen capture is "
    "'screenshot'. shows lists everything the photo shows, most important "
    "first. sharp is false when the photo is blurry or shaken. "
    "wellLit is false when it is too dark or blown out to read. issues lists "
    "what is wrong with the picture itself. setting is where it was taken. "
    "products lists only products from the brand's list that you can see "
    "clearly; leave it empty when unsure. "
    "usefulness is 0 to 100: how well this photo would show a client the "
    "activation (a clear, well framed table, product, shelf or branded "
    "setup scores high; a blurry, dark, cropped or irrelevant shot scores "
    "low). caption is at most 10 words, sentence case, no full stop, about "
    "the setup, product and setting only, never about people, for example "
    "'Sampling table at the store entrance' or 'Product on the cooler "
    "shelf'."
)
USER = "Judge and tag this photo. Do not describe any person in it."


def catalog_labels(tenant_id: int) -> list[tuple[int, str]]:
    """``[(product id, label)]`` for the brand's catalog, at most
    :data:`MAX_CATALOG_IN_PROMPT`, labels unique."""
    from events.models import Product

    rows = (
        Product.objects.filter(tenant_id=tenant_id)
        .select_related("product_type")
        .order_by("product_type__name", "name", "id")
    )
    out: list[tuple[int, str]] = []
    seen: set[str] = set()
    for product in rows:
        name = " ".join((product.name or "").split())
        if not name:
            continue
        kind = " ".join((getattr(product.product_type, "name", "") or "").split())
        label = f"{kind} {name}" if kind and kind.lower() not in name.lower() else name
        label = label[:MAX_PRODUCT_LABEL]
        if label.lower() in seen:
            continue
        seen.add(label.lower())
        out.append((product.id, label))
        if len(out) >= MAX_CATALOG_IN_PROMPT:
            break
    return out


def schema_for(catalog: list[tuple[int, str]] | None) -> dict:
    """The strict JSON schema for one photo: the scorer's fields plus the
    library tags. ``products`` is an enum of the brand's catalog labels, or
    always empty when the brand has no catalog."""
    labels = [label for _id, label in catalog or []]
    product_items: dict = (
        {"type": "string", "enum": labels} if labels else {"type": "string", "enum": [""]}
    )
    return {
        "type": "object",
        "additionalProperties": False,
        "required": [
            "kind",
            "sharp",
            "wellLit",
            "productVisible",
            "brandingVisible",
            "usefulness",
            "caption",
            "shows",
            "products",
            "peoplePresent",
            "crowdSize",
            "setting",
            "issues",
        ],
        "properties": {
            "kind": {"type": "string", "enum": list(KINDS)},
            "sharp": {"type": "boolean"},
            "wellLit": {"type": "boolean"},
            "productVisible": {"type": "boolean"},
            "brandingVisible": {"type": "boolean"},
            "usefulness": {"type": "integer"},
            "caption": {"type": "string"},
            "shows": {"type": "array", "items": {"type": "string", "enum": list(SHOWS)}},
            "products": {"type": "array", "items": product_items},
            "peoplePresent": {"type": "boolean"},
            "crowdSize": {"type": "string", "enum": list(CROWD_SIZES)},
            "setting": {"type": "string", "enum": list(SETTINGS)},
            "issues": {"type": "array", "items": {"type": "string", "enum": list(ISSUES)}},
        },
    }


def has_library_tags(parsed) -> bool:
    """True when a reply carries the library fields (an older or partial
    reply only has the scorer's)."""
    return isinstance(parsed, dict) and all(
        key in parsed for key in ("shows", "peoplePresent", "crowdSize", "setting")
    )


def library_tags(
    parsed: dict,
    catalog: list[tuple[int, str]] | None,
    *,
    names: list[str] | None = None,
    width: int | None = None,
    height: int | None = None,
) -> dict | None:
    """A model reply → the values stored on :class:`RecapPhotoTag`.

    None when the reply has no library fields. Unknown keys are dropped, a
    product not on the brand's list is dropped, and the caption goes through
    the scorer's person filter. The quality score is the scorer's
    usefulness with its ceilings, so a blurry photo never ranks high.
    """
    from recaps.photo_picks import MIN_SIDE_PX, clean_caption, interpret_score

    if not has_library_tags(parsed):
        return None
    shows = _unique(s for s in parsed.get("shows") or [] if s in SHOWS)
    kind = parsed.get("kind")
    if kind == "receipt" and "receipt" not in shows:
        shows.insert(0, "receipt")
    if not shows:
        shows = ["other"]
    by_label = {label.lower(): pid for pid, label in catalog or []}
    product_ids: list[int] = []
    product_names: list[str] = []
    for raw in parsed.get("products") or []:
        pid = by_label.get(str(raw or "").strip().lower())
        if pid is not None and pid not in product_ids:
            product_ids.append(pid)
            product_names.append(next(lb for p, lb in catalog if p == pid))
    issues = _unique(i for i in parsed.get("issues") or [] if i in ISSUES)
    if parsed.get("sharp") is False and "blurry" not in issues:
        issues.append("blurry")
    if width and height and min(width, height) < MIN_SIDE_PX and "low_resolution" not in issues:
        issues.append("low_resolution")
    crowd = parsed.get("crowdSize") if parsed.get("crowdSize") in CROWD_SIZES else "none"
    people = bool(parsed.get("peoplePresent"))
    if crowd != "none":
        people = True
    setting = parsed.get("setting") if parsed.get("setting") in SETTINGS else "other"
    scored = interpret_score(parsed, names)
    quality = None
    if scored is not None and scored.get("status") == "scored":
        quality = int(scored["score"])
    caption_kind = kind if kind in KINDS else "other"
    is_receipt = "receipt" in shows or kind == "receipt"
    return {
        "shows": shows,
        "product_ids": product_ids,
        "product_names": product_names,
        "people_present": people,
        "crowd_size": crowd,
        "setting": setting,
        "issues": issues,
        "quality_score": None if is_receipt else quality,
        "caption": "" if is_receipt else clean_caption(parsed.get("caption"), caption_kind, names),
        "excluded": bool(is_receipt or kind == "screenshot"),
        "excluded_reason": "receipt" if is_receipt else ("screenshot" if kind == "screenshot" else ""),
    }


def stored_reply(parsed: dict) -> dict:
    """What is kept of a reply so the other pass can reuse it: the raw
    fields only, never a caption that failed the person filter."""
    keep = (
        "kind",
        "sharp",
        "wellLit",
        "productVisible",
        "brandingVisible",
        "usefulness",
        "shows",
        "products",
        "peoplePresent",
        "crowdSize",
        "setting",
        "issues",
    )
    return {k: parsed[k] for k in keep if k in parsed}


def _unique(items) -> list:
    out: list = []
    for item in items:
        if item not in out:
            out.append(item)
    return out
