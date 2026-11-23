"""Two readings of the same approved consumer comments:

* other brands people brought up, and whether they preferred the tenant's
  product or the other one (:func:`get_competitor_mentions`);
* which of the tenant's own products does best in which state, from the
  recaps' per-product numbers and from what people said about each product
  (:func:`get_product_regions`).

Both sit on :mod:`recaps.feedback_themes`: the comments are the ones
:func:`recaps.feedback_themes.gather_comments` returns (approved recaps,
not archived, under a live request, windowed on the event date, ambassador
names, email addresses and phone numbers already taken out, plain notes
fields never read), and the division of work is the same. The model reads
numbered comments and tags them; every number a reader sees is counted
here from those tags, and every quote is the stored text of a comment.

A tag is kept only when the name it carries is written in the comment: a
brand the comment does not spell out, or a product whose name is not in the
comment, is dropped. The per-product numbers never touch the model.

What the model returned is kept in
:class:`tenants.models.TenantCommentInsight`, keyed by a digest of the
comments (and of the brand and product names they were read against), so a
page load is a read. Nothing is stored when the model is unavailable.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import unicodedata
from collections import Counter
from datetime import date

import strawberry
from asgiref.sync import sync_to_async

from recaps.feedback_themes import (
    MIN_COMMENTS,
    STATUS_READY,
    STATUS_TOO_FEW,
    STATUS_UNAVAILABLE,
    Comment,
    _client_visible,
    _parse_date,
    _short_quote,
    compose_prompt,
    gather_comments,
)
from recaps.models import (
    CustomFieldValue,
    CustomRecap,
    CustomRecapProductSample,
    ProductSamples,
    Recap,
)
from recaps.tenant_overview import _inclusive_dates_to_window
from recaps.types import _SOLD_SKU_LINE_RE, _sold_unit_lines
from utils.ai_text import AiUnavailable, generate_json
from utils.graphql.permissions import StrictIsAuthenticated

logger = logging.getLogger(__name__)

KIND_COMPETITORS = "competitors"
KIND_PRODUCT_FEELING = "product_feeling"

# Bump when a prompt or a rule here changes, so stored rows are rebuilt.
_VERSION = "3"

STANCE_OURS = "prefers_ours"
STANCE_THEIRS = "prefers_theirs"
STANCE_NEUTRAL = "neutral"
STANCES = (STANCE_OURS, STANCE_THEIRS, STANCE_NEUTRAL)

FEELING_POSITIVE = "positive"
FEELING_NEGATIVE = "negative"
FEELING_NEUTRAL = "neutral"
FEELINGS = (FEELING_POSITIVE, FEELING_NEGATIVE, FEELING_NEUTRAL)

# Other brands: under this many comments naming any other brand, the section
# is one sentence and no chart.
MIN_MENTION_COMMENTS = 3
MAX_BRANDS = 12
MAX_BRAND_CHARS = 40

# What sells where. A state names a leader only on at least this many
# approved recaps from at least this many separate events, so one busy
# Saturday cannot crown a product.
MIN_STATE_RECAPS = 3
MIN_STATE_EVENTS = 2
# "Spoken best of" needs this many recaps in the state with a comment about
# a product, and the leader this many recaps speaking well of it.
MIN_STATE_COMMENT_RECAPS = 3
MIN_POSITIVE_RECAPS = 2
MAX_STATES = 12

LEAD_READY = "ready"
LEAD_TOO_FEW = "too_few"
LEAD_NO_LEADER = "no_leader"

COMMENTS_READY = "ready"
COMMENTS_NONE = "none"
COMMENTS_UNAVAILABLE = "unavailable"

_SOLD_SKU_IREGEX = r"units sold\s*:"
assert _SOLD_SKU_LINE_RE.search("Units sold: x")

# Words in a product name that do not tell one product from another.
_GENERIC_WORDS = frozenset(
    """
    the and with for original classic flavor flavour flavored flavoured pack
    packs can cans bottle bottles case cases box bag bar bars size count mini
    large small regular single variety sample samples drink drinks water tea
    """.split()
)


# ─── names ──────────────────────────────────────────────────────────


def normalise(text) -> str:
    """A name or a comment reduced to lower-case words: accents, case,
    punctuation and a possessive "'s" do not make two spellings differ."""
    text = unicodedata.normalize("NFKD", str(text or ""))
    text = "".join(ch for ch in text if not unicodedata.combining(ch)).casefold()
    text = re.sub(r"['’`]s\b", "", text)
    text = re.sub(r"['’`.]", "", text)
    return " ".join(re.sub(r"[^a-z0-9]+", " ", text).split())


def _has_phrase(haystack: str, phrase: str) -> bool:
    """Whether normalised ``phrase`` is in normalised ``haystack`` as whole words."""
    return bool(phrase) and f" {phrase} " in f" {haystack} "


def written_in(comment_norm: str, name_norm: str) -> bool:
    """Whether a name is literally in a comment. "White Claw" is in
    "white claw" and in "WhiteClaw"; it is not in a comment that only
    describes it."""
    if not name_norm:
        return False
    if _has_phrase(comment_norm, name_norm):
        return True
    return name_norm.replace(" ", "") in comment_norm.split()


_STYLE_SUFFIXES = frozenset(
    {"style", "like", "type", "flavored", "flavoured", "flavor", "flavour"}
)
_STYLE_TAIL_RE = re.compile(
    r"[-–]\s*(?:style|like|type|flavou?red|flavou?r)\s*$",
    re.IGNORECASE,
)
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+|\n+")


def strip_brand_suffix(label: str) -> str:
    """"Dr Pepper-style" is Dr Pepper. A flavour word on the end is not a
    second brand."""
    cleaned = _clean_brand(label)
    cut = _STYLE_TAIL_RE.sub("", cleaned).strip(" -–")
    words = cut.split()
    if len(words) >= 2 and normalise(words[-1]) in _STYLE_SUFFIXES:
        cut = " ".join(words[:-1])
    return cut.strip()


def spelling_sentence(text: str, name_norm: str) -> str:
    """The shortest whole sentence in ``text`` that spells ``name_norm``.

    A pasted comment that also mentions stickers or another product keeps
    only the sentence that names this brand. Empty when the name is not
    written in any sentence.
    """
    raw = " ".join(str(text or "").split())
    if not raw or not name_norm:
        return ""
    parts = [part.strip() for part in _SENTENCE_SPLIT_RE.split(str(text or "")) if part.strip()]
    hits = [part for part in parts if written_in(normalise(part), name_norm)]
    if not hits:
        return raw if written_in(normalise(raw), name_norm) else ""
    return min(hits, key=len)


def tenant_names(tenant_id: int) -> tuple[str, list[tuple[int, str]]]:
    """The tenant's brand name and its catalog as ``(product id, name)``."""
    from events.models import Product
    from tenants.models import Tenant

    brand = (
        Tenant.objects.filter(id=tenant_id).values_list("name", flat=True).first() or ""
    )
    products = [
        (pid, " ".join((name or "").split()))
        for pid, name in Product.objects.filter(tenant_id=tenant_id)
        .order_by("name", "id")
        .values_list("id", "name")
        if (name or "").strip()
    ]
    return brand.strip(), products


def is_own_name(name_norm: str, brand: str, product_names: list[str]) -> bool:
    """Whether a name the model returned is the tenant itself or one of its
    products, which are never "another brand"."""
    brand_norm = normalise(brand)
    if brand_norm and (
        _has_phrase(brand_norm, name_norm) or _has_phrase(name_norm, brand_norm)
    ):
        return True
    return any(_has_phrase(normalise(product), name_norm) for product in product_names)


# ─── storage ────────────────────────────────────────────────────────


def _digest(kind: str, comments: list[Comment], extra) -> str:
    blob = json.dumps(
        [_VERSION, kind, extra, [(c.recap, c.kind, c.text) for c in comments]],
        ensure_ascii=False,
    )
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _stored(tenant_id: int, kind: str, digest: str):
    from tenants.models import TenantCommentInsight

    return TenantCommentInsight.objects.filter(
        tenant_id=tenant_id, kind=kind, source_hash=digest
    ).first()


def _store(tenant_id: int, kind: str, digest: str, start, end, payload: dict):
    from tenants.models import TenantCommentInsight

    row, _created = TenantCommentInsight.objects.update_or_create(
        tenant_id=tenant_id,
        kind=kind,
        source_hash=digest,
        defaults={"start_date": start, "end_date": end, "payload": payload},
    )
    return row


def _ask(system: str, user: str, schema: dict) -> dict | None:
    """One tagging call on the small model. None when it is not available
    or returned nothing usable."""
    try:
        raw = generate_json(system, user, schema=schema, small=True)
    except AiUnavailable:
        return None
    except Exception:  # noqa: BLE001
        logger.exception("comment insights: model call failed")
        return None
    return raw if isinstance(raw, dict) else None


def _number(value) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def _settle(tags: set[str], yes: str, no: str, neither: str) -> str:
    """One recap's view from all its comments: ``yes`` or ``no`` only when
    its comments do not pull both ways."""
    if yes in tags and no not in tags:
        return yes
    if no in tags and yes not in tags:
        return no
    return neither


# ─── other brands people mentioned ──────────────────────────────────

MENTIONS_JSON_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["mentions"],
    "properties": {
        "mentions": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["comment", "brand", "name", "stance"],
                "properties": {
                    "comment": {"type": "integer"},
                    "brand": {"type": "string"},
                    "name": {"type": "string"},
                    "stance": {"type": "string", "enum": list(STANCES)},
                },
            },
        }
    },
}


def competitor_system_prompt(brand: str, product_names: list[str]) -> str:
    own = brand or "the brand"
    products = "; ".join(product_names[:60])
    return (
        "You read comments that shoppers made at in-store product sampling "
        f"events run for {own}. Use only the numbered comments in the user "
        "message. Find every comment that names ANOTHER brand or another "
        f"company's product. {own} itself"
        + (f" and its own products ({products})" if products else "")
        + " are not other brands: never list them. For each one give the "
        "comment's number, the other brand's name with the spelling used in "
        "the comment (fix capitals only) as 'brand', that brand's proper name "
        "as the company writes it as 'name' (a comment that says 'Red Bulls' "
        "or 'redbull' gets name 'Red Bull'), and a stance: 'prefers_ours' when "
        f"the shopper prefers {own} to that brand, 'prefers_theirs' when they "
        "prefer the other brand, 'neutral' when they only mention it. A "
        "comment that names two other brands gets two entries. A flavour, an "
        "ingredient, a store or a kind of product is not a brand. A name "
        "with a suffix such as style or flavor is that brand without the "
        "suffix. Do not guess a brand the comment does not spell. Do not "
        "count anything: only tag comments."
    )


def brand_key(name_norm: str) -> str:
    """One key for the ways people write a brand: "red bull", "red bulls",
    "redbull" and "red bull's" are the same brand. The plural is folded only
    on the last word, and not for names that end in s themselves (Celsius,
    Bliss)."""
    words = name_norm.split()
    if words:
        last = words[-1]
        if len(last) > 3 and last.endswith("s") and not last.endswith(("ss", "us", "is")):
            words[-1] = last[:-1]
    return "".join(words)


def _clean_brand(raw) -> str:
    label = " ".join(str(raw or "").split()).strip(" .,:;-\"'")
    return label[:MAX_BRAND_CHARS].rstrip()


def count_mentions(
    raw: dict | None, comments: list[Comment], brand: str, product_names: list[str]
) -> dict:
    """Turn the model's tags into counted brands.

    A tag is dropped when its comment number is not a comment, when the
    brand is not written in that comment, or when the "brand" is the tenant
    or one of its products. ``recap_count`` is the number of distinct recaps
    whose comments name the brand; the three stance counts split those same
    recaps and add up to it. The quote is a stored comment.
    """
    tags = (raw or {}).get("mentions")
    if not isinstance(tags, list):
        tags = []
    norms = [normalise(c.text) for c in comments]
    brands: dict[str, dict] = {}
    seen: set[tuple[int, str]] = set()
    for tag in tags:
        if not isinstance(tag, dict):
            continue
        number = _number(tag.get("comment"))
        label = strip_brand_suffix(tag.get("brand"))
        stance = tag.get("stance")
        name = normalise(label)
        if number is None or not 1 <= number <= len(comments):
            continue
        if stance not in STANCES or len(name) < 2 or not re.search(r"[a-z]", name):
            continue
        index = number - 1
        if not written_in(norms[index], name):
            continue
        if is_own_name(name, brand, product_names):
            continue
        key = brand_key(name)
        if (index, key) in seen:
            continue
        seen.add((index, key))
        entry = brands.setdefault(
            key, {"spellings": Counter(), "proper": Counter(), "tags": []}
        )
        entry["spellings"][label] += 1
        # The model's proper name is used only when it is this same brand
        # written properly, never a different name it came up with.
        proper = strip_brand_suffix(tag.get("name"))
        if proper and brand_key(normalise(proper)) == key:
            entry["proper"][proper] += 1
        entry["tags"].append((index, stance))

    out: list[dict] = []
    for entry in brands.values():
        by_recap: dict[str, set[str]] = {}
        for index, stance in entry["tags"]:
            by_recap.setdefault(comments[index].recap, set()).add(stance)
        settled = Counter(
            _settle(stances, STANCE_OURS, STANCE_THEIRS, STANCE_NEUTRAL)
            for stances in by_recap.values()
        )
        leading = max(STANCES, key=lambda s: (settled[s], -STANCES.index(s)))
        quote_index = next(
            (i for i, stance in entry["tags"] if stance == leading), entry["tags"][0][0]
        )
        # The brand's proper name when the model gave one for this same
        # brand; otherwise the shortest spelling people used (the singular),
        # the most common one on a tie.
        shown = (
            entry["proper"].most_common(1)[0][0]
            if entry["proper"]
            else min(
                entry["spellings"],
                key=lambda sp: (len(sp), -entry["spellings"][sp]),
            )
        )
        out.append(
            {
                "brand": shown,
                "recap_count": len(by_recap),
                "prefer_ours": settled[STANCE_OURS],
                "prefer_theirs": settled[STANCE_THEIRS],
                "neutral": settled[STANCE_NEUTRAL],
                # The sentence that spells the brand, not the rest of a
                # pasted comment.
                "quote": _short_quote(
                    spelling_sentence(comments[quote_index].text, normalise(shown))
                ),
            }
        )
    out.sort(key=lambda b: (-b["recap_count"], b["brand"].casefold()))
    return {
        "brands": out[:MAX_BRANDS],
        "mention_comment_count": len({index for index, _key in seen}),
    }


def _mentions_result(status, comments, *, row=None, truncated=False) -> dict:
    payload = (row.payload or {}) if row else {}
    mentioned = int(payload.get("mention_comment_count") or 0)
    if status == STATUS_READY and mentioned < MIN_MENTION_COMMENTS:
        status = STATUS_TOO_FEW
    return {
        "status": status,
        "brands": list(payload.get("brands") or []) if status == STATUS_READY else [],
        "mention_comment_count": mentioned,
        "recap_count": len({c.recap for c in comments}),
        "comment_count": len(comments),
        "truncated": truncated,
        "generated_at": row.generated_at.isoformat() if row else None,
    }


def get_competitor_mentions(tenant_id: int, start: date | None, end: date | None) -> dict:
    """Other brands named in a tenant's approved comments for a window.

    ``status`` is ``ready``; ``too_few`` (under :data:`MIN_COMMENTS`
    comments, when the model is not asked, or under
    :data:`MIN_MENTION_COMMENTS` comments that name another brand); or
    ``unavailable`` (the model could not be reached: nothing is stored).
    """
    comments, truncated = gather_comments(tenant_id, start, end)
    if len(comments) < MIN_COMMENTS:
        return _mentions_result(STATUS_TOO_FEW, comments, truncated=truncated)

    brand, products = tenant_names(tenant_id)
    product_names = [name for _pid, name in products]
    digest = _digest(KIND_COMPETITORS, comments, [brand, product_names])
    row = _stored(tenant_id, KIND_COMPETITORS, digest)
    if row is None:
        raw = _ask(
            competitor_system_prompt(brand, product_names),
            compose_prompt(comments),
            MENTIONS_JSON_SCHEMA,
        )
        if raw is None or not isinstance(raw.get("mentions"), list):
            return _mentions_result(STATUS_UNAVAILABLE, comments, truncated=truncated)
        row = _store(
            tenant_id,
            KIND_COMPETITORS,
            digest,
            start,
            end,
            count_mentions(raw, comments, brand, product_names),
        )
    return _mentions_result(STATUS_READY, comments, row=row, truncated=truncated)


# ─── what sells where ───────────────────────────────────────────────

FEELINGS_JSON_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["mentions"],
    "properties": {
        "mentions": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["comment", "product", "feeling"],
                "properties": {
                    "comment": {"type": "integer"},
                    "product": {"type": "integer"},
                    "feeling": {"type": "string", "enum": list(FEELINGS)},
                },
            },
        }
    },
}


def product_words(name: str, brand: str) -> set[str]:
    """The words of a product name that tell it from the others: not the
    brand's own name, a size ("12oz", "10mg") or a word like "pack"."""
    brand_words = set(normalise(brand).split())
    return {
        word
        for word in normalise(name).split()
        if len(word) >= 3
        and word not in brand_words
        and word not in _GENERIC_WORDS
        and not any(ch.isdigit() for ch in word)
    }


def names_product(comment_norm: str, name: str, brand: str) -> bool:
    """Whether a comment spells a product: its whole name, or every word
    that sets it apart ("blue razz" for "Blue Razz 10mg", not "blue" alone)."""
    if written_in(comment_norm, normalise(name)):
        return True
    words = product_words(name, brand)
    if not words:
        return False
    return words <= set(comment_norm.split())


def feeling_system_prompt(brand: str, products: list[tuple[int, str]]) -> str:
    listing = "\n".join(
        f"P{number}: {name}" for number, (_pid, name) in enumerate(products, start=1)
    )
    return (
        "You read comments that shoppers made at in-store product sampling "
        f"events for {brand or 'a brand'}. Use only the numbered comments in "
        "the user message. The brand's products are:\n"
        f"{listing}\n"
        "For each comment that is about one of these products, give the "
        "comment's number, the product's number (the digits after P) and how "
        "the shopper felt about THAT product: 'positive', 'negative' or "
        "'neutral'. A comment about two products gets two entries. Tag a "
        "product only when the comment spells that product's name. Leave "
        "out a comment when you cannot tell which product it is about. Do "
        "not count anything and do not rank the products: only tag comments."
    )


def keep_feelings(
    raw: dict | None, comments: list[Comment], products: list[tuple[int, str]], brand: str
) -> list[list]:
    """The model's tags as ``[recap, product name, feeling]``, one per
    comment and product, dropping any whose product is not named in the
    comment or whose numbers point at nothing."""
    tags = (raw or {}).get("mentions")
    if not isinstance(tags, list):
        return []
    norms = [normalise(c.text) for c in comments]
    kept: list[list] = []
    seen: set[tuple[int, int]] = set()
    for tag in tags:
        if not isinstance(tag, dict):
            continue
        number, product = _number(tag.get("comment")), _number(tag.get("product"))
        feeling = tag.get("feeling")
        if number is None or product is None or feeling not in FEELINGS:
            continue
        if not 1 <= number <= len(comments) or not 1 <= product <= len(products):
            continue
        index, name = number - 1, products[product - 1][1]
        if (index, product) in seen or not names_product(norms[index], name, brand):
            continue
        seen.add((index, product))
        kept.append([comments[index].recap, name, feeling])
    return kept


def recap_places(tenant_id: int, start: date | None, end: date | None) -> dict[str, dict]:
    """State and event of every recap a brand may be shown in the window,
    keyed like :class:`recaps.feedback_themes.Comment` (``L<id>`` legacy,
    ``C<id>`` custom). The state is the event's, else read off its address."""
    from events.routing import extract_state_code

    window = _inclusive_dates_to_window(start, end)
    places: dict[str, dict] = {}
    for prefix, queryset in (
        ("L", Recap.objects.filter(event__tenant_id=tenant_id)),
        ("C", CustomRecap.objects.filter(tenant_id=tenant_id)),
    ):
        rows = _client_visible(queryset, "event__", "", window).values_list(
            "id", "event_id", "event__state__code", "event__address"
        )
        for recap_id, event_id, state, address in rows.iterator():
            places[f"{prefix}{recap_id}"] = {
                "event": event_id,
                "state": (state or extract_state_code(address) or "").upper(),
            }
    return places


def product_lines(tenant_id: int, start: date | None, end: date | None) -> list[dict]:
    """Per-product numbers on approved recaps, with no model involved.

    Each line: ``recap`` key, ``product`` name, ``measure`` (``sold`` or
    ``sampled``) and ``units``. Sold comes only from a recap's per-product
    sold lines ("Units sold: <product>", the same lines
    :func:`recaps.types._sold_unit_lines` sums); a template that records one
    sold total has no per-product sold. Sampled is the recap's per-product
    sample rows: what was recorded as handed out.
    """
    window = _inclusive_dates_to_window(start, end)
    _brand, products = tenant_names(tenant_id)
    catalog = {normalise(name): name for _pid, name in products}
    lines: list[dict] = []

    def _add(recap: str, name: str, measure: str, units) -> None:
        name = " ".join((name or "").split())
        units = int(units or 0)
        if name and units > 0:
            lines.append(
                {
                    "recap": recap,
                    "product": catalog.get(normalise(name), name),
                    "measure": measure,
                    "units": units,
                }
            )

    samples = (
        (
            "C",
            CustomRecapProductSample.objects.filter(custom_recap__tenant_id=tenant_id),
            "custom_recap__",
        ),
        ("L", ProductSamples.objects.filter(recap__event__tenant_id=tenant_id), "recap__"),
    )
    for prefix, queryset, recap in samples:
        rows = _client_visible(queryset, f"{recap}event__", recap, window).values_list(
            f"{recap}id", "product__name", "quantity"
        )
        for recap_id, name, quantity in rows.iterator():
            _add(f"{prefix}{recap_id}", name, "sampled", quantity)

    fields: dict[int, list[tuple[str | None, str | None]]] = {}
    sold = _client_visible(
        CustomFieldValue.objects.filter(
            custom_recap__tenant_id=tenant_id,
            custom_field__name__iregex=_SOLD_SKU_IREGEX,
        ),
        "custom_recap__event__",
        "custom_recap__",
        window,
    ).values_list("custom_recap_id", "custom_field__name", "value")
    for recap_id, name, value in sold.iterator():
        fields.setdefault(recap_id, []).append((name, value))
    for recap_id, pairs in fields.items():
        for name, units in _sold_unit_lines(pairs):
            if _SOLD_SKU_LINE_RE.search(name):
                _add(f"C{recap_id}", name.split(":", 1)[1], "sold", units)
    return lines


def _leader(totals: dict[str, int]) -> str | None:
    """The product with the most, or None when the top two are level."""
    ranked = sorted(totals.items(), key=lambda item: (-item[1], item[0].casefold()))
    if not ranked or ranked[0][1] <= 0:
        return None
    if len(ranked) > 1 and ranked[1][1] == ranked[0][1]:
        return None
    return ranked[0][0]


def _volume_lead(lines: list[dict], places: dict[str, dict]) -> dict:
    """The leading product of one state for one measure, with its base."""
    recaps = {line["recap"] for line in lines}
    events = {places[key]["event"] for key in recaps}
    totals: dict[str, int] = {}
    for line in lines:
        totals[line["product"]] = totals.get(line["product"], 0) + line["units"]
    lead = {"status": LEAD_TOO_FEW, "product": None, "units": 0, "recaps": len(recaps)}
    if len(recaps) < MIN_STATE_RECAPS or len(events) < MIN_STATE_EVENTS:
        return lead
    product = _leader(totals)
    if product is None:
        return {**lead, "status": LEAD_NO_LEADER}
    return {**lead, "status": LEAD_READY, "product": product, "units": totals[product]}


def _spoken_lead(tags: list[list]) -> dict:
    """The product a state's recaps spoke best of, with its base."""
    by_product: dict[str, dict[str, set[str]]] = {}
    for recap, product, feeling in tags:
        by_product.setdefault(product, {}).setdefault(recap, set()).add(feeling)
    recaps = {recap for recap, _product, _feeling in tags}
    counted = {
        product: Counter(
            _settle(feelings, FEELING_POSITIVE, FEELING_NEGATIVE, FEELING_NEUTRAL)
            for feelings in per_recap.values()
        )
        for product, per_recap in by_product.items()
    }
    lead = {
        "status": LEAD_TOO_FEW,
        "product": None,
        "positive": 0,
        "negative": 0,
        "recaps": len(recaps),
    }
    if len(recaps) < MIN_STATE_COMMENT_RECAPS:
        return lead
    product = _leader({name: count[FEELING_POSITIVE] for name, count in counted.items()})
    count = counted.get(product) if product else None
    if (
        count is None
        or count[FEELING_POSITIVE] < MIN_POSITIVE_RECAPS
        or count[FEELING_POSITIVE] <= count[FEELING_NEGATIVE]
    ):
        return {**lead, "status": LEAD_NO_LEADER}
    return {
        **lead,
        "status": LEAD_READY,
        "product": product,
        "positive": count[FEELING_POSITIVE],
        "negative": count[FEELING_NEGATIVE],
    }


def summarize_regions(
    lines: list[dict], tags: list[list], places: dict[str, dict]
) -> list[dict]:
    """Pure roll-up by state. ``lines`` are :func:`product_lines`, ``tags``
    are :func:`keep_feelings`, ``places`` is :func:`recap_places`. A recap
    with no known state is left out. The three columns are worked out
    separately and never combined into one score."""
    states: dict[str, dict] = {}

    def _state(recap: str) -> dict | None:
        code = (places.get(recap) or {}).get("state")
        if not code:
            return None
        return states.setdefault(
            code, {"sold": [], "sampled": [], "tags": [], "recaps": set()}
        )

    for line in lines:
        bucket = _state(line["recap"])
        if bucket is not None:
            bucket[line["measure"]].append(line)
            bucket["recaps"].add(line["recap"])
    for tag in tags:
        bucket = _state(tag[0])
        if bucket is not None:
            bucket["tags"].append(tag)
            bucket["recaps"].add(tag[0])

    rows = [
        {
            "state": code,
            "recaps": len(bucket["recaps"]),
            "sold": _volume_lead(bucket["sold"], places),
            "sampled": _volume_lead(bucket["sampled"], places),
            "spoken": _spoken_lead(bucket["tags"]),
        }
        for code, bucket in states.items()
    ]
    rows.sort(key=lambda row: (-row["recaps"], row["state"]))
    return rows[:MAX_STATES]


def _product_feelings(tenant_id: int, start, end) -> tuple[str, list[list], object]:
    """Tags for the comments that name one of the tenant's products:
    ``(comments status, tags, stored row)``. Only those comments are sent."""
    brand, products = tenant_names(tenant_id)
    if not products:
        return COMMENTS_NONE, [], None
    comments, _truncated = gather_comments(tenant_id, start, end)
    naming = [
        comment
        for comment in comments
        if any(
            names_product(normalise(comment.text), name, brand) for _pid, name in products
        )
    ]
    if not naming:
        return COMMENTS_NONE, [], None
    digest = _digest(KIND_PRODUCT_FEELING, naming, [brand, [n for _p, n in products]])
    row = _stored(tenant_id, KIND_PRODUCT_FEELING, digest)
    if row is None:
        raw = _ask(
            feeling_system_prompt(brand, products),
            compose_prompt(naming),
            FEELINGS_JSON_SCHEMA,
        )
        if raw is None or not isinstance(raw.get("mentions"), list):
            return COMMENTS_UNAVAILABLE, [], None
        row = _store(
            tenant_id,
            KIND_PRODUCT_FEELING,
            digest,
            start,
            end,
            {"tags": keep_feelings(raw, naming, products, brand)},
        )
    tags = [
        tag
        for tag in (row.payload or {}).get("tags") or []
        if isinstance(tag, list) and len(tag) == 3
    ]
    return COMMENTS_READY, tags, row


def get_product_regions(tenant_id: int, start: date | None, end: date | None) -> dict:
    """Which product leads in which state, three ways side by side: units
    sold, samples handed out, and what people said.

    ``status`` is ``ready`` when at least one state has a leader in any
    column, otherwise ``too_few``. ``comments_status`` says whether the
    comment column could be worked out: ``ready``, ``none`` (no comment
    names a product; the model is not asked) or ``unavailable`` (the model
    could not be reached; the number columns are still returned).
    """
    places = recap_places(tenant_id, start, end)
    lines = [line for line in product_lines(tenant_id, start, end) if line["recap"] in places]
    comments_status, tags, row = _product_feelings(tenant_id, start, end)
    tags = [tag for tag in tags if tag[0] in places]
    states = summarize_regions(lines, tags, places)
    ready = any(
        state[column]["status"] == LEAD_READY
        for state in states
        for column in ("sold", "sampled", "spoken")
    )
    return {
        "status": STATUS_READY if ready else STATUS_TOO_FEW,
        "comments_status": comments_status,
        "has_sold": any(line["measure"] == "sold" for line in lines),
        "has_sampled": any(line["measure"] == "sampled" for line in lines),
        "states": states,
        "recap_count": len({line["recap"] for line in lines} | {tag[0] for tag in tags}),
        "generated_at": row.generated_at.isoformat() if row else None,
    }


# ─── GraphQL ────────────────────────────────────────────────────────


@strawberry.type
class CompetitorBrand:
    """Another brand named in consumer comments. The three stance counts
    are recaps and add up to ``recap_count``."""

    brand: str
    recap_count: int
    prefer_ours: int
    prefer_theirs: int
    neutral: int
    quote: str


@strawberry.type
class TenantCompetitorMentions:
    """Other brands people mentioned. ``status`` is ``ready``, ``too_few``
    or ``unavailable``; ``brands`` is empty unless it is ``ready``."""

    status: str
    brands: list[CompetitorBrand]
    mention_comment_count: int
    recap_count: int
    comment_count: int
    truncated: bool
    generated_at: str | None
    start_date: str | None
    end_date: str | None


@strawberry.type
class ProductRegionState:
    """One state's leading product, three ways. Each ``*_status`` is
    ``ready`` (the product is named), ``too_few`` (not enough recaps or
    events to say) or ``no_leader`` (enough data, nothing clearly ahead)."""

    state: str
    recaps: int
    sold_status: str
    sold_product: str | None
    sold_units: int
    sold_recaps: int
    sampled_status: str
    sampled_product: str | None
    sampled_units: int
    sampled_recaps: int
    spoken_status: str
    spoken_product: str | None
    spoken_positive: int
    spoken_negative: int
    spoken_recaps: int


@strawberry.type
class TenantProductRegions:
    """What sells where. ``status`` is ``ready``, ``too_few`` or
    ``unavailable``; ``comments_status`` is ``ready``, ``none`` or
    ``unavailable`` and covers only the comment column."""

    status: str
    comments_status: str
    has_sold: bool
    has_sampled: bool
    states: list[ProductRegionState]
    recap_count: int
    min_state_recaps: int
    generated_at: str | None
    start_date: str | None
    end_date: str | None


def _iso(day: date | None) -> str | None:
    return day.isoformat() if day else None


def _mentions_type(data: dict | None, start, end) -> TenantCompetitorMentions:
    data = data or {}
    return TenantCompetitorMentions(
        status=data.get("status", STATUS_UNAVAILABLE),
        brands=[
            CompetitorBrand(
                brand=str(b.get("brand") or ""),
                recap_count=int(b.get("recap_count") or 0),
                prefer_ours=int(b.get("prefer_ours") or 0),
                prefer_theirs=int(b.get("prefer_theirs") or 0),
                neutral=int(b.get("neutral") or 0),
                quote=str(b.get("quote") or ""),
            )
            for b in data.get("brands") or []
            if isinstance(b, dict)
        ],
        mention_comment_count=data.get("mention_comment_count", 0),
        recap_count=data.get("recap_count", 0),
        comment_count=data.get("comment_count", 0),
        truncated=data.get("truncated", False),
        generated_at=data.get("generated_at"),
        start_date=_iso(start),
        end_date=_iso(end),
    )


def _regions_type(data: dict | None, start, end) -> TenantProductRegions:
    data = data or {}
    return TenantProductRegions(
        status=data.get("status", STATUS_UNAVAILABLE),
        comments_status=data.get("comments_status", COMMENTS_UNAVAILABLE),
        has_sold=data.get("has_sold", False),
        has_sampled=data.get("has_sampled", False),
        states=[
            ProductRegionState(
                state=s["state"],
                recaps=s["recaps"],
                sold_status=s["sold"]["status"],
                sold_product=s["sold"]["product"],
                sold_units=s["sold"]["units"],
                sold_recaps=s["sold"]["recaps"],
                sampled_status=s["sampled"]["status"],
                sampled_product=s["sampled"]["product"],
                sampled_units=s["sampled"]["units"],
                sampled_recaps=s["sampled"]["recaps"],
                spoken_status=s["spoken"]["status"],
                spoken_product=s["spoken"]["product"],
                spoken_positive=s["spoken"]["positive"],
                spoken_negative=s["spoken"]["negative"],
                spoken_recaps=s["spoken"]["recaps"],
            )
            for s in data.get("states") or []
        ],
        recap_count=data.get("recap_count", 0),
        min_state_recaps=MIN_STATE_RECAPS,
        generated_at=data.get("generated_at"),
        start_date=_iso(start),
        end_date=_iso(end),
    )


async def _resolve(info, tenant_id, start, end, build, to_type, name: str):
    """Scope the caller to a tenant, run ``build`` and shape the result.
    Never raises: anything that goes wrong is ``status: "unavailable"``."""
    from recaps.report_types import _CampaignReportService

    def _run(target: int):
        from tenants.models import Tenant

        if not Tenant.objects.filter(id=target).exists():
            return None
        return build(target, start, end)

    try:
        # A brand user is pinned to their own tenant whatever id they pass.
        target = await _CampaignReportService().resolve_target_tenant_id(info, tenant_id)
        if target is None:
            return to_type(None, start, end)
        data = await sync_to_async(_run, thread_sensitive=True)(target)
    except Exception:  # noqa: BLE001
        logger.exception("%s failed", name)
        return to_type(None, start, end)
    return to_type(data, start, end)


@strawberry.type
class CommentInsightQueries:
    @strawberry.field(permission_classes=[StrictIsAuthenticated])
    async def tenant_competitor_mentions(
        self,
        info: strawberry.Info,
        tenant_id: strawberry.ID,
        start_date: str | None = None,
        end_date: str | None = None,
    ) -> TenantCompetitorMentions:
        """Other brands named in comments on approved recaps, with how many
        recaps named each and which way people leaned. A brand user reads
        their own tenant whatever id is passed; Ignite staff may pass any
        tenant. Dates are inclusive and compared with the event date."""
        return await _resolve(
            info,
            tenant_id,
            _parse_date(start_date),
            _parse_date(end_date),
            get_competitor_mentions,
            _mentions_type,
            "tenantCompetitorMentions",
        )

    @strawberry.field(permission_classes=[StrictIsAuthenticated])
    async def tenant_product_regions(
        self,
        info: strawberry.Info,
        tenant_id: strawberry.ID,
        start_date: str | None = None,
        end_date: str | None = None,
    ) -> TenantProductRegions:
        """The tenant's leading product in each state: by units sold, by
        samples handed out and by what people said, from approved recaps.
        Scoped and windowed like ``tenantCompetitorMentions``."""
        return await _resolve(
            info,
            tenant_id,
            _parse_date(start_date),
            _parse_date(end_date),
            get_product_regions,
            _regions_type,
            "tenantProductRegions",
        )
