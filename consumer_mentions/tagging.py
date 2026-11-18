"""Tag each approved recap's consumer comments once: themes and other brands.

What is read, for every brand alike (the rules of
:mod:`recaps.feedback_themes`):

* approved recaps only, not archived, under a live request, events kept on
  the dashboard;
* the consumer-comment fields (legacy feedback columns, custom fields named
  like feedback, a reason to decline or a question), never a plain notes
  field or a BA-name field;
* custom fields that ask which competitor products people drink, read for
  brand names only (never a theme, never a quote);
* known ambassador names, email addresses and phone numbers taken out
  before anything is sent or stored.

The model reads numbered comments, several recaps per call, and for each
comment says which themes of the brand's list it touches (or ``other`` with
a short label) and which other brands it names. Everything is checked here:
a theme key that is not on the list is dropped, a brand that is not written
in the comment is dropped, the brand itself and its products are never
another brand. Counting happens later, from these tags, in
:mod:`consumer_mentions.trends`.

A recap is read again only when its comments change (``source_hash``).
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import time
from dataclasses import dataclass, field

from recaps.comment_insights import (
    _clean_brand,
    brand_key,
    is_own_name,
    normalise,
    tenant_names,
    written_in,
)
from recaps.feedback_themes import (
    KIND_DECLINE,
    KIND_GENERAL,
    Comment,
    _client_visible,
    _custom_rows,
    _field_kind,
    _full_name,
    _legacy_rows,
    clean_comment,
)
from recaps.models import CustomFieldValue
from recaps.scoped_ai import _BA_NAME_FIELD_RE
from recaps.tenant_overview import _event_date_expr
from utils.ai_text import AiUnavailable, generate_json

from consumer_mentions.vocabulary import (
    OTHER,
    POLARITIES,
    clean_label,
    find_theme,
)

logger = logging.getLogger(__name__)

# Bump when the prompt or the checks change, so every recap is read again.
PROMPT_VERSION = "1"

KIND_COMPETITOR = "competitor"

# Fields that ask which other brands people drink or use. Matched by name;
# ``\y`` is Postgres's word boundary.
_COMPETITOR_FIELD_PG = (
    r"competit|other\s+brands?|\ywhat\s+(?:else\s+)?(?:do\s+)?(?:they|you|consumers?)\s+"
    r"(?:usually\s+|currently\s+)?(?:drink|buy|use)"
)

# A "proper name" that only adds one of these is a company, not the brand.
_CORPORATE_WORDS = frozenset(
    "inc llc ltd co corp corporation company holdings brands group beverage beverages".split()
)

# One call reads at most this much. A batch never splits a recap.
BATCH_MAX_COMMENTS = 40
BATCH_MAX_CHARS = 6_000
MAX_THEMES_PER_COMMENT = 3
MAX_OTHER_PER_COMMENT = 2
MAX_BRANDS_PER_COMMENT = 6

# Per-call ceiling the run's cost cap counts against.
_MODEL_TIMEOUT_SECONDS = 45.0

TAGS_JSON_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["comments"],
    "properties": {
        "comments": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["comment", "themes", "other", "brands"],
                "properties": {
                    "comment": {"type": "integer"},
                    "themes": {"type": "array", "items": {"type": "string"}},
                    "other": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "additionalProperties": False,
                            "required": ["label", "polarity"],
                            "properties": {
                                "label": {"type": "string"},
                                "polarity": {"type": "string", "enum": list(POLARITIES)},
                            },
                        },
                    },
                    "brands": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "additionalProperties": False,
                            "required": ["as_written", "name"],
                            "properties": {
                                "as_written": {"type": "string"},
                                "name": {"type": "string"},
                            },
                        },
                    },
                },
            },
        }
    },
}


# ─── what is read ───────────────────────────────────────────────────


def _competitor_rows(tenant_id: int):
    recap = "custom_recap__"
    return (
        _client_visible(
            CustomFieldValue.objects.filter(
                custom_recap__tenant_id=tenant_id,
                custom_field__name__iregex=_COMPETITOR_FIELD_PG,
            ),
            "custom_recap__event__",
            recap,
            None,
        )
        .annotate(_day=_event_date_expr("custom_recap__event__"))
        .order_by("-_day", "-id")
        .values_list(
            "custom_recap_id",
            "custom_field__name",
            "value",
            "custom_recap__ambassador__user__first_name",
            "custom_recap__ambassador__user__last_name",
            "custom_recap__external_ba_name",
        )
    )


def recap_comments(tenant_id: int) -> dict[str, list[Comment]]:
    """Every approved recap's consumer comments, newest event first, keyed
    ``L<id>`` / ``C<id>``. No size cap: each recap is tagged on its own
    comments. Recaps with nothing to read are left out."""
    raw: list[tuple[str, str, object]] = []
    names: set[str] = set()

    def _note(first, last, external) -> None:
        for name in (_full_name(first, last), (external or "").strip()):
            if name:
                names.add(name)

    for recap_id, quotes, stories, feedback, decline, first, last, external in (
        _legacy_rows(tenant_id, None).iterator()
    ):
        _note(first, last, external)
        key = f"L{recap_id}"
        for value in (quotes, stories, feedback):
            raw.append((key, KIND_GENERAL, value))
        raw.append((key, KIND_DECLINE, decline))

    for recap_id, name, value, first, last, external in _competitor_rows(tenant_id).iterator():
        _note(first, last, external)
        if _BA_NAME_FIELD_RE.search(name or ""):
            continue
        raw.append((f"C{recap_id}", KIND_COMPETITOR, value))

    for recap_id, name, value, first, last, external in _custom_rows(tenant_id, None).iterator():
        _note(first, last, external)
        if _BA_NAME_FIELD_RE.search(name or ""):
            continue
        raw.append((f"C{recap_id}", _field_kind(name), value))

    ordered = sorted(names, key=len, reverse=True)
    out: dict[str, list[Comment]] = {}
    seen: set[tuple[str, str]] = set()
    for recap, kind, value in raw:
        text = clean_comment(value, ordered)
        if not text or (recap, text.casefold()) in seen:
            continue
        seen.add((recap, text.casefold()))
        out.setdefault(recap, []).append(Comment(recap=recap, kind=kind, text=text))
    return out


def source_hash(comments: list[Comment]) -> str:
    blob = json.dumps(
        [PROMPT_VERSION, [(c.kind, c.text) for c in comments]], ensure_ascii=False
    )
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def pending(tenant_id: int, comments: dict[str, list[Comment]]) -> list[str]:
    """Recaps never tagged, or whose comments changed since. Newest first."""
    from consumer_mentions.models import RecapMentionTag

    stored = dict(
        RecapMentionTag.objects.filter(tenant_id=tenant_id).values_list(
            "recap_key", "source_hash"
        )
    )
    return [key for key, rows in comments.items() if stored.get(key) != source_hash(rows)]


def batches(keys: list[str], comments: dict[str, list[Comment]]) -> list[list[str]]:
    """Recaps grouped into calls. A recap bigger than one batch goes alone,
    its comments cut to the batch size."""
    out: list[list[str]] = []
    current: list[str] = []
    n = chars = 0
    for key in keys:
        rows = comments[key]
        size, length = len(rows), sum(len(c.text) for c in rows)
        if current and (n + size > BATCH_MAX_COMMENTS or chars + length > BATCH_MAX_CHARS):
            out.append(current)
            current, n, chars = [], 0, 0
        current.append(key)
        n, chars = n + size, chars + length
    if current:
        out.append(current)
    return out


# ─── the call ───────────────────────────────────────────────────────


def system_prompt(brand: str, product_names: list[str], themes: list[dict]) -> str:
    own = brand or "the brand"
    products = "; ".join(product_names[:60])
    listing = "\n".join(f"- {t['key']}: {t['label']} ({t['polarity']})" for t in themes)
    return (
        "You tag comments that shoppers made at in-store product sampling events "
        f"run for {own}. Use only the numbered comments in the user message.\n"
        "THEMES. The brand's themes are:\n"
        f"{listing}\n"
        "For each comment list the keys of the themes it is clearly about, at "
        f"most {MAX_THEMES_PER_COMMENT}. Use only keys from this list. When a "
        "comment is clearly about something none of them covers, put it in "
        "'other' with a plain label of two to five words in sentence case "
        "(for example 'Wants more caffeine') and its polarity: 'praise', "
        "'complaint' or 'request'. A label never holds a person's name. "
        "Comments marked [competitor] get no themes and no 'other'.\n"
        f"OTHER BRANDS. List every other brand or another company's product the "
        f"comment names. {own}"
        + (f" and its own products ({products})" if products else "")
        + " are never other brands. Give 'as_written' copied from the comment "
        "and 'name', the brand's proper name as the company writes it ('red "
        "bulls' is 'Red Bull'). A flavour, an ingredient, a store or a kind of "
        "product is not a brand. Do not guess a brand the comment does not name.\n"
        "Return one entry per comment number, empty lists when nothing applies. "
        "Do not count anything."
    )


def user_prompt(numbered: list[Comment]) -> str:
    lines = ["Comments from approved event recaps:"]
    for number, comment in enumerate(numbered, start=1):
        mark = "" if comment.kind == KIND_GENERAL else f" [{comment.kind}]"
        lines.append(f"{number}.{mark} {comment.text}")
    return "\n".join(lines)


def _ask(system: str, user: str) -> dict | None:
    try:
        raw = generate_json(
            system,
            user,
            schema=TAGS_JSON_SCHEMA,
            small=True,
            max_tokens=4000,
            timeout=_MODEL_TIMEOUT_SECONDS,
        )
    except AiUnavailable:
        raise
    except Exception:  # noqa: BLE001
        logger.exception("consumer mentions: model call failed")
        return None
    return raw if isinstance(raw, dict) else None


def _int(value) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def read_tags(
    raw: dict | None,
    numbered: list[Comment],
    themes: list[dict],
    brand: str,
    product_names: list[str],
) -> dict[str, dict]:
    """The model's answer, checked, as ``{recap: {"themes": [...], "brands":
    [...]}}`` for every recap in ``numbered`` (empty lists when nothing was
    found, so the recap is not read again)."""
    keys = {t["key"] for t in themes}
    norms = [normalise(c.text) for c in numbered]
    out: dict[str, dict] = {c.recap: {"themes": [], "brands": []} for c in numbered}
    seen_theme: set[tuple[str, str]] = set()
    seen_brand: set[tuple[str, str]] = set()
    entries = (raw or {}).get("comments")
    for entry in entries if isinstance(entries, list) else []:
        if not isinstance(entry, dict):
            continue
        number = _int(entry.get("comment"))
        if number is None or not 1 <= number <= len(numbered):
            continue
        comment = numbered[number - 1]
        slot = out[comment.recap]

        if comment.kind != KIND_COMPETITOR:
            picked = [k for k in entry.get("themes") or [] if isinstance(k, str) and k in keys]
            for key in picked[:MAX_THEMES_PER_COMMENT]:
                if (comment.recap, key) not in seen_theme:
                    seen_theme.add((comment.recap, key))
                    slot["themes"].append({"theme": key, "quote": comment.text})
            for other in (entry.get("other") or [])[:MAX_OTHER_PER_COMMENT]:
                if not isinstance(other, dict):
                    continue
                label = clean_label(other.get("label"))
                polarity = other.get("polarity")
                words = label.split()
                if polarity not in POLARITIES or not 2 <= len(words) <= 6:
                    continue
                known = find_theme(themes, label)
                ident = known["key"] if known else f"{OTHER}:{normalise(label)}"
                if (comment.recap, ident) in seen_theme:
                    continue
                seen_theme.add((comment.recap, ident))
                slot["themes"].append(
                    {"theme": known["key"], "quote": comment.text}
                    if known
                    else {
                        "theme": OTHER,
                        "label": label,
                        "polarity": polarity,
                        "quote": comment.text,
                    }
                )

        for item in (entry.get("brands") or [])[:MAX_BRANDS_PER_COMMENT]:
            if not isinstance(item, dict):
                continue
            written = _clean_brand(item.get("as_written"))
            name_norm = normalise(written)
            if len(name_norm) < 2 or not re.search(r"[a-z]", name_norm):
                continue
            if not written_in(norms[number - 1], name_norm):
                continue
            if is_own_name(name_norm, brand, product_names):
                continue
            key = brand_key(name_norm)
            proper = _clean_brand(item.get("name"))
            proper_norm = normalise(proper)
            # The proper name only when it is this same brand written well
            # ("redbull" → "Red Bull"), or the full name of what was written
            # ("monster" → "Monster Energy"); never a different brand.
            if proper and brand_key(proper_norm) == key:
                pass
            elif (
                proper
                and proper_norm.startswith(f"{name_norm} ")
                and len(proper_norm.split()) == len(name_norm.split()) + 1
                and proper_norm.split()[-1] not in _CORPORATE_WORDS
                and not is_own_name(proper_norm, brand, product_names)
            ):
                key = brand_key(proper_norm)
            else:
                proper = written
            if (comment.recap, key) in seen_brand:
                continue
            seen_brand.add((comment.recap, key))
            slot["brands"].append({"key": key, "name": proper})
    return out


# ─── a run ──────────────────────────────────────────────────────────


@dataclass
class RunResult:
    tenant_id: int
    recaps_with_comments: int = 0
    pending: int = 0
    tagged: int = 0
    calls: int = 0
    failed_calls: int = 0
    prompt_chars: int = 0
    stopped: str = ""
    promoted: list[str] = field(default_factory=list)

    def line(self, dry_run: bool) -> str:
        verb = "would tag" if dry_run else "tagged"
        count = self.pending if dry_run else self.tagged
        extra = f", stopped: {self.stopped}" if self.stopped else ""
        promoted = f", new themes: {', '.join(self.promoted)}" if self.promoted else ""
        return (
            f"tenant {self.tenant_id}: {self.recaps_with_comments} recaps with comments, "
            f"{self.pending} to read, {verb} {count}, calls {self.calls}"
            f" (failed {self.failed_calls}), ~{self.prompt_chars // 4} input tokens"
            f"{promoted}{extra}"
        )


def tag_tenant(
    tenant_id: int,
    *,
    dry_run: bool = True,
    max_calls: int = 20,
    deadline: float | None = None,
) -> RunResult:
    """Tag a brand's recaps whose comments are new or changed.

    ``dry_run`` reads and plans only: no model call, nothing stored (not
    even a new theme list). ``max_calls`` caps the model calls; the recaps
    left over are newest-first next time. ``deadline`` is a
    ``time.monotonic()`` value past which no new call starts.
    Raises :class:`AiUnavailable` when the model is not configured.
    """
    from consumer_mentions.models import RecapMentionTag
    from consumer_mentions.vocabulary import ensure_themes, promote

    result = RunResult(tenant_id=tenant_id)
    comments = recap_comments(tenant_id)
    result.recaps_with_comments = len(comments)
    todo = pending(tenant_id, comments)
    result.pending = len(todo)
    if not todo:
        return result

    themes = ensure_themes(tenant_id, save=not dry_run)
    brand, products = tenant_names(tenant_id)
    product_names = [name for _pid, name in products]
    system = system_prompt(brand, product_names, themes)

    for group in batches(todo, comments):
        if result.calls >= max_calls:
            result.stopped = f"reached {max_calls} calls"
            break
        if deadline is not None and time.monotonic() > deadline:
            result.stopped = "time limit"
            break
        numbered = [c for key in group for c in comments[key]][: BATCH_MAX_COMMENTS * 2]
        user = user_prompt(numbered)
        result.prompt_chars += len(system) + len(user)
        result.calls += 1
        if dry_run:
            continue
        raw = _ask(system, user)
        if raw is None or not isinstance(raw.get("comments"), list):
            result.failed_calls += 1
            continue
        tags = read_tags(raw, numbered, themes, brand, product_names)
        for key in group:
            found = tags.get(key, {"themes": [], "brands": []})
            RecapMentionTag.objects.update_or_create(
                tenant_id=tenant_id,
                recap_key=key,
                defaults={
                    "source_hash": source_hash(comments[key]),
                    "themes": found["themes"],
                    "brands": found["brands"],
                },
            )
            result.tagged += 1

    if not dry_run and result.tagged:
        result.promoted = [t["label"] for t in promote(tenant_id)]
    return result
