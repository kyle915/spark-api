"""A brand's theme list: the fixed set every recap is tagged against.

Months can only be compared when they are counted on the same terms, so the
list is stable. It is made once per brand from:

1. the brand's own "What consumers said" themes
   (:class:`tenants.models.TenantFeedbackThemes`), which were grouped from its
   real comments: "liked" themes are praise, "passed" are complaints, "asked"
   are requests;
2. then a short default list of things shoppers say at any sampling table,
   skipping any that say the same as a theme already on the list.

After that it only grows. The tagger may file a comment under ``other`` with
its own short label; once the same label turns up on
:data:`PROMOTE_MIN_RECAPS` recaps it is added as a theme. Old tags carry
that label, so they count toward the new theme without being read again.
A key is never renamed or removed.
"""

from __future__ import annotations

from collections import Counter, defaultdict

from recaps.comment_insights import normalise

POLARITY_PRAISE = "praise"
POLARITY_COMPLAINT = "complaint"
POLARITY_REQUEST = "request"
POLARITIES = (POLARITY_PRAISE, POLARITY_COMPLAINT, POLARITY_REQUEST)

OTHER = "other"

SOURCE_DEFAULT = "default"
SOURCE_FEEDBACK = "feedback"
SOURCE_PROMOTED = "promoted"

MAX_THEMES = 30
MAX_SEEDED_FROM_FEEDBACK = 14
MAX_LABEL_CHARS = 40
PROMOTE_MIN_RECAPS = 3

_SECTION_POLARITY = {
    "liked": POLARITY_PRAISE,
    "passed": POLARITY_COMPLAINT,
    "asked": POLARITY_REQUEST,
}

# Things shoppers say at almost any sampling table. Kept short and plain so a
# brand's own themes lead and these only fill gaps.
DEFAULT_THEMES: tuple[tuple[str, str, str], ...] = (
    ("great_taste", "Great taste", POLARITY_PRAISE),
    ("taste_not_for_them", "Didn't like the taste", POLARITY_COMPLAINT),
    ("too_sweet", "Too sweet", POLARITY_COMPLAINT),
    ("price_too_high", "Price too high", POLARITY_COMPLAINT),
    ("good_value", "Good value", POLARITY_PRAISE),
    ("likes_ingredients", "Likes the ingredients", POLARITY_PRAISE),
    ("ingredient_concerns", "Ingredient concerns", POLARITY_COMPLAINT),
    ("likes_packaging", "Likes the packaging", POLARITY_PRAISE),
    ("already_a_fan", "Already buys it", POLARITY_PRAISE),
    ("where_to_buy", "Asked where to buy", POLARITY_REQUEST),
    ("wants_other_flavors", "Wants more flavors", POLARITY_REQUEST),
)

# Words that do not tell one theme from another.
_FILLER = frozenset(
    """
    a an the and or of to for it its is was were be with in on at by from
    they them their people shopper shoppers consumer consumers customer
    customers product products really very so too much more than this that
    want wants wanted wish like likes liked love loved asked asks
    """.split()
) - {"too", "more"}


def theme_key(label: str) -> str:
    """A stable key for a label: lower-case words joined by ``_``."""
    return "_".join(normalise(label).split())[:48] or OTHER


def clean_label(raw) -> str:
    """A label in sentence case, short, with no trailing punctuation."""
    label = " ".join(str(raw or "").split()).strip(" .,:;-\"'")
    if len(label) > MAX_LABEL_CHARS:
        label = label[:MAX_LABEL_CHARS].rsplit(" ", 1)[0].rstrip(" ,;-")
    return label[:1].upper() + label[1:] if label else ""


def _words(label: str) -> set[str]:
    return {w for w in normalise(label).split() if w not in _FILLER and len(w) > 2}


def _same_theme(a: str, b: str) -> bool:
    """Whether two labels say the same thing: one's words inside the
    other's, or most of their words shared."""
    if normalise(a) == normalise(b):
        return True
    wa, wb = _words(a), _words(b)
    if not wa or not wb:
        return False
    if wa <= wb or wb <= wa:
        return True
    return len(wa & wb) / len(wa | wb) >= 0.5


def _entry(key: str, label: str, polarity: str, source: str) -> dict:
    return {"key": key, "label": label, "polarity": polarity, "source": source}


def seed_themes(feedback_rows: list[dict]) -> list[dict]:
    """The first list for a brand. ``feedback_rows`` are stored feedback
    themes payloads, newest first: ``[{"themes": [{section, label, ...}]}]``.
    Pure; nothing is read or written."""
    themes: list[dict] = []

    def _add(label: str, polarity: str, source: str, key: str | None = None) -> bool:
        label = clean_label(label)
        if not label or polarity not in POLARITIES or len(themes) >= MAX_THEMES:
            return False
        key = key or theme_key(label)
        if key == OTHER or any(
            t["key"] == key or _same_theme(t["label"], label) for t in themes
        ):
            return False
        themes.append(_entry(key, label, polarity, source))
        return True

    seeded = 0
    for row in feedback_rows:
        for theme in (row or {}).get("themes") or []:
            if seeded >= MAX_SEEDED_FROM_FEEDBACK or not isinstance(theme, dict):
                continue
            polarity = _SECTION_POLARITY.get(theme.get("section"))
            if polarity and _add(theme.get("label"), polarity, SOURCE_FEEDBACK):
                seeded += 1
    for key, label, polarity in DEFAULT_THEMES:
        _add(label, polarity, SOURCE_DEFAULT, key)
    return themes


def find_theme(themes: list[dict], label: str) -> dict | None:
    """The theme a free label belongs to, by exact key or exact words."""
    key, norm = theme_key(label), normalise(label)
    for theme in themes:
        if theme["key"] == key or normalise(theme["label"]) == norm:
            return theme
    return None


def promotions(themes: list[dict], tags: list[list[dict]]) -> list[dict]:
    """``other`` labels written on enough recaps to become themes.

    ``tags`` is one list of theme items per recap. A label is counted once
    per recap. Pure: returns the entries to append, most recaps first.
    """
    recaps: Counter = Counter()
    spelling: dict[str, Counter] = defaultdict(Counter)
    polarity: dict[str, Counter] = defaultdict(Counter)
    for items in tags:
        seen: set[str] = set()
        for item in items or []:
            if not isinstance(item, dict) or item.get("theme") != OTHER:
                continue
            label = clean_label(item.get("label"))
            if not label or find_theme(themes, label):
                continue
            key = theme_key(label)
            if key in seen:
                continue
            seen.add(key)
            recaps[key] += 1
            spelling[key][label] += 1
            if item.get("polarity") in POLARITIES:
                polarity[key][item["polarity"]] += 1
    out: list[dict] = []
    room = MAX_THEMES - len(themes)
    for key, count in sorted(recaps.items(), key=lambda kv: (-kv[1], kv[0])):
        if count < PROMOTE_MIN_RECAPS or len(out) >= room:
            continue
        label = spelling[key].most_common(1)[0][0]
        if any(_same_theme(label, t["label"]) for t in [*themes, *out]):
            continue
        tone = polarity[key].most_common(1)[0][0] if polarity[key] else POLARITY_REQUEST
        out.append(_entry(key, label, tone, SOURCE_PROMOTED))
    return out


def resolve(item: dict, themes: list[dict]) -> dict | None:
    """The vocabulary theme a stored item counts toward, or None for an
    ``other`` label that has not been promoted."""
    if not isinstance(item, dict):
        return None
    key = item.get("theme")
    if key and key != OTHER:
        return next((t for t in themes if t["key"] == key), None)
    label = item.get("label")
    return find_theme(themes, label) if label else None


# ─── storage ────────────────────────────────────────────────────────


def load_themes(tenant_id: int) -> list[dict]:
    """The stored list, or an empty one when the brand has none yet."""
    from consumer_mentions.models import MentionThemeVocabulary

    row = MentionThemeVocabulary.objects.filter(tenant_id=tenant_id).first()
    return list(row.themes or []) if row else []


def ensure_themes(tenant_id: int, *, save: bool = True) -> list[dict]:
    """The brand's list, seeded on first use. With ``save=False`` (a dry
    run) a missing list is worked out but not stored."""
    from consumer_mentions.models import MentionThemeVocabulary
    from tenants.models import TenantFeedbackThemes

    row = MentionThemeVocabulary.objects.filter(tenant_id=tenant_id).first()
    if row is not None and row.themes:
        return list(row.themes)
    payloads = list(
        TenantFeedbackThemes.objects.filter(tenant_id=tenant_id)
        .order_by("-generated_at")
        .values_list("payload", flat=True)[:10]
    )
    themes = seed_themes(payloads)
    if save:
        MentionThemeVocabulary.objects.update_or_create(
            tenant_id=tenant_id, defaults={"themes": themes}
        )
    return themes


def promote(tenant_id: int, *, save: bool = True) -> list[dict]:
    """Add ``other`` labels that have reached :data:`PROMOTE_MIN_RECAPS`
    recaps. Returns what was (or, with ``save=False``, would be) added."""
    from consumer_mentions.models import MentionThemeVocabulary, RecapMentionTag

    themes = load_themes(tenant_id)
    if not themes:
        return []
    tags = list(
        RecapMentionTag.objects.filter(tenant_id=tenant_id).values_list("themes", flat=True)
    )
    added = promotions(themes, tags)
    if added and save:
        MentionThemeVocabulary.objects.filter(tenant_id=tenant_id).update(
            themes=[*themes, *added]
        )
    return added

