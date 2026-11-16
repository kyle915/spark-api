"""Consumer comments grouped into themes: what people liked, why they
passed, what they asked.

The model does one job here: it reads numbered comments and says which
comments belong together and what to call the group. Everything a reader
sees as a number is counted in Python from that assignment, and every quote
is the stored text of one of the numbered comments, never model prose.

What reaches the model, for staff and brand users alike:

* comments on approved recaps only, not archived, whose request was not
  soft-deleted, windowed on the EVENT date (the dashboard's basis,
  :func:`recaps.tenant_overview._filter_event_window`);
* no ambassador name: a known ambassador's full name written into a comment
  is replaced, and "BA name"-style form fields are never read;
* no email address or phone number.

The result is kept in :class:`tenants.models.TenantFeedbackThemes`, keyed
by a digest of the comments, so a page load is a read. The model is called
again only when the approved comments in the window have changed, or when
staff ask for a refresh.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from dataclasses import dataclass
from datetime import date

import strawberry
from asgiref.sync import sync_to_async

from recaps.models import ConsumerFeedback, CustomFieldValue
from recaps.scoped_ai import _BA_NAME_FIELD_RE, _hide_names
from recaps.tenant_overview import (
    _approved_only,
    _event_date_expr,
    _filter_event_window,
    _inclusive_dates_to_window,
)
from recaps.tenant_sentiment import _CUSTOM_FEEDBACK_NAME_IREGEX
from utils.ai_text import AiUnavailable, generate_json
from utils.graphql.permissions import StrictIsAuthenticated

logger = logging.getLogger(__name__)

SECTION_LIKED = "liked"
SECTION_PASSED = "passed"
SECTION_ASKED = "asked"
SECTIONS = (SECTION_LIKED, SECTION_PASSED, SECTION_ASKED)

KIND_GENERAL = "general"
KIND_DECLINE = "decline"
KIND_QUESTION = "question"

# Where a comment may be filed. A reason to decline is never something
# people liked, and a question is never a reason to decline.
_ALLOWED_SECTIONS = {
    KIND_GENERAL: frozenset(SECTIONS),
    KIND_DECLINE: frozenset({SECTION_PASSED}),
    KIND_QUESTION: frozenset({SECTION_ASKED}),
}

STATUS_READY = "ready"
STATUS_TOO_FEW = "too_few"
STATUS_UNAVAILABLE = "unavailable"

MIN_COMMENTS = 3
MIN_THEMES = 3
MAX_THEMES = 6
MAX_COMMENTS = 150
MAX_PROMPT_CHARS = 14_000
MAX_COMMENT_CHARS = 240
MAX_QUOTE_CHARS = 160
MAX_LABEL_CHARS = 60

# Bump when the prompt or the cleaning changes, so stored themes are rebuilt.
_VERSION = "1"

# How long one attempt may take. The page shows "not available right now"
# after this rather than leaving the section loading.
_MODEL_TIMEOUT_SECONDS = 15.0

# Custom-recap fields are matched by NAME. The decline and question patterns
# come in two spellings, like the feedback pattern they sit beside: ``\b`` for
# Python and ``\y`` for Postgres (see recaps.tenant_sentiment).
_DECLINE = (
    r"declin|objection|hesitat|barrier|"
    r"did\s*n.?t\s+(?:\w+\s+)?(?:buy|purchas)|"
    r"not\s+(?:\w+\s+)?(?:buy|purchas)|no\s+purchase|why\s+not"
)
_QUESTION_PY = r"question|\bfaqs?\b|\basked\b"
_QUESTION_PG = r"question|\yfaqs?\y|\yasked\y"
_DECLINE_RE = re.compile(_DECLINE, re.IGNORECASE)
_QUESTION_RE = re.compile(_QUESTION_PY, re.IGNORECASE)
# A field named "Notes" / "Additional notes" is left out: it usually holds
# the ambassador's own notes about the shift, which must not reach a brand
# as something a consumer said. A notes field that also names feedback
# ("Notes on consumer feedback") still matches on that word.
_NOTES_PG = r"\ynotes?\y|"
assert _NOTES_PG in _CUSTOM_FEEDBACK_NAME_IREGEX
_COMMENT_FIELD_IREGEX = (
    f"{_CUSTOM_FEEDBACK_NAME_IREGEX.replace(_NOTES_PG, '')}|{_DECLINE}|{_QUESTION_PG}"
)
_COMMENT_FIELD_RE = re.compile(_COMMENT_FIELD_IREGEX.replace(r"\y", r"\b"), re.IGNORECASE)


def is_comment_field(name: str | None) -> bool:
    """Whether a custom-recap field holds consumer comments, by its name."""
    return bool(_COMMENT_FIELD_RE.search(name or ""))

_EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
# Ten to fifteen digits with the usual separators between them.
_PHONE_RE = re.compile(r"(?<!\w)\+?\(?\d(?:[\s().-]{0,2}\d){9,14}(?!\w)")
_REMOVED = "[removed]"
_HAS_LETTERS_RE = re.compile(r"[A-Za-z]{2}")
_EMPTY_ANSWERS = frozenset(
    {"n/a", "na", "none", "no", "nope", "nothing", "no comment", "no comments", "tbd"}
)

THEMES_JSON_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["themes"],
    "properties": {
        "themes": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["section", "label", "comments", "quote"],
                "properties": {
                    "section": {"type": "string", "enum": list(SECTIONS)},
                    "label": {"type": "string"},
                    "comments": {"type": "array", "items": {"type": "integer"}},
                    "quote": {"type": "integer"},
                },
            },
        }
    },
}

_SYSTEM_PROMPT = (
    "You sort comments that shoppers made at in-store product sampling events "
    "into themes for the brand. Use only the numbered comments in the user "
    f"message. Make between {MIN_THEMES} and {MAX_THEMES} themes in total, "
    "fewer only when the comments do not support that many. Each theme has a "
    "section: 'liked' for what people liked, 'passed' for why people did not "
    "buy, 'asked' for questions people asked. A comment marked [decline] may "
    "only go in a 'passed' theme and a comment marked [question] only in an "
    "'asked' theme. Give each theme a plain name of two to five words in "
    "sentence case, with no brand ambassador's or shopper's name. In "
    "'comments' list the number of every comment that belongs to the theme. "
    "A comment belongs to at most one theme; leave out a comment that fits "
    "none. In 'quote' give the number of one short comment from that theme "
    "that represents it and holds no person's name or other personal detail. "
    "Do not count anything and do not write any totals: only assign comments."
)


@dataclass(frozen=True)
class Comment:
    """One consumer comment from one approved recap."""

    recap: str
    kind: str
    text: str


def _parse_date(raw: str | None) -> date | None:
    try:
        return date.fromisoformat(raw) if raw else None
    except ValueError:
        return None


def _field_kind(name: str | None) -> str:
    text = name or ""
    if _DECLINE_RE.search(text):
        return KIND_DECLINE
    if _QUESTION_RE.search(text):
        return KIND_QUESTION
    return KIND_GENERAL


def _full_name(first: str | None, last: str | None) -> str:
    return " ".join(part for part in ((first or "").strip(), (last or "").strip()) if part)


def _flatten(raw) -> str:
    """A stored value as one line. A multi-select value is a JSON list."""
    text = str(raw or "").strip()
    if text.startswith("["):
        try:
            parsed = json.loads(text)
        except ValueError:
            parsed = None
        if isinstance(parsed, list):
            text = "; ".join(str(item).strip() for item in parsed if str(item).strip())
    return " ".join(text.split())


def clean_comment(raw, names: list[str]) -> str | None:
    """Text fit to show a brand, or None when there is nothing to say.

    Ambassador names, email addresses and phone numbers are taken out, in
    that order, before the text is clipped.
    """
    text = _flatten(raw)
    if not text or not _HAS_LETTERS_RE.search(text):
        return None
    if text.casefold().strip(" .!-") in _EMPTY_ANSWERS:
        return None
    text = _hide_names(text, names)
    text = _EMAIL_RE.sub(_REMOVED, text)
    text = _PHONE_RE.sub(_REMOVED, text)
    if len(text) > MAX_COMMENT_CHARS:
        text = text[: MAX_COMMENT_CHARS - 1].rstrip() + "…"
    return text


def _client_visible(queryset, event_prefix: str, recap_prefix: str, window):
    """Rows of recaps a brand may be shown, in the window by event date.

    ``_filter_event_window`` drops soft-deleted requests, events kept off
    the dashboard and filings kept out of aggregates; this adds approved
    and not archived.
    """
    rows = _approved_only(_filter_event_window(queryset, event_prefix, window), recap_prefix)
    return rows.filter(**{f"{recap_prefix}archived_at__isnull": True})


def _legacy_rows(tenant_id: int, window):
    recap = "recap__"
    return (
        _client_visible(
            ConsumerFeedback.objects.filter(recap__event__tenant_id=tenant_id),
            "recap__event__",
            recap,
            window,
        )
        .annotate(_day=_event_date_expr("recap__event__"))
        .order_by("-_day", "-id")
        .values_list(
            "recap_id",
            "quotes",
            "positive_stories",
            "feedback",
            "reasons_to_decline",
            "recap__ambassador__user__first_name",
            "recap__ambassador__user__last_name",
            "recap__external_ba_name",
        )
    )


def _custom_rows(tenant_id: int, window):
    recap = "custom_recap__"
    return (
        _client_visible(
            CustomFieldValue.objects.filter(
                custom_recap__tenant_id=tenant_id,
                custom_field__name__iregex=_COMMENT_FIELD_IREGEX,
            ),
            "custom_recap__event__",
            recap,
            window,
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


def gather_comments(
    tenant_id: int,
    start: date | None,
    end: date | None,
    *,
    legacy_ids=None,
    custom_ids=None,
) -> tuple[list[Comment], bool]:
    """Consumer comments on approved recaps in the window, newest event first.

    Returns the comments and whether the list was cut at the size cap. Both
    recap shapes are read: the legacy feedback columns, and custom-recap
    fields whose name reads like feedback, a reason to decline or a question.
    Plain notes fields are not read.

    ``legacy_ids`` / ``custom_ids`` narrow the read to those recaps (one
    store's, for the store profile). Give both or neither.
    """
    window = _inclusive_dates_to_window(start, end)
    legacy_rows = _legacy_rows(tenant_id, window)
    custom_rows = _custom_rows(tenant_id, window)
    if legacy_ids is not None or custom_ids is not None:
        legacy_rows = legacy_rows.filter(recap_id__in=list(legacy_ids or ()))
        custom_rows = custom_rows.filter(custom_recap_id__in=list(custom_ids or ()))
    raw: list[tuple[str, str, object]] = []
    names: set[str] = set()

    def _note_names(first, last, external) -> None:
        for name in (_full_name(first, last), (external or "").strip()):
            if name:
                names.add(name)

    for (
        recap_id,
        quotes,
        stories,
        feedback,
        decline,
        first,
        last,
        external,
    ) in legacy_rows.iterator():
        _note_names(first, last, external)
        key = f"L{recap_id}"
        for value in (quotes, stories, feedback):
            raw.append((key, KIND_GENERAL, value))
        raw.append((key, KIND_DECLINE, decline))

    for recap_id, field_name, value, first, last, external in custom_rows.iterator():
        _note_names(first, last, external)
        if _BA_NAME_FIELD_RE.search(field_name or ""):
            continue
        raw.append((f"C{recap_id}", _field_kind(field_name), value))

    # Longest first, so "Jordan Rivera Jr" is replaced before "Jordan Rivera".
    ordered_names = sorted(names, key=len, reverse=True)
    comments: list[Comment] = []
    seen: set[tuple[str, str]] = set()
    chars = 0
    for recap, kind, value in raw:
        text = clean_comment(value, ordered_names)
        if not text:
            continue
        dedupe = (recap, text.casefold())
        if dedupe in seen:
            continue
        if len(comments) >= MAX_COMMENTS or chars + len(text) > MAX_PROMPT_CHARS:
            return comments, True
        seen.add(dedupe)
        comments.append(Comment(recap=recap, kind=kind, text=text))
        chars += len(text)
    return comments, False


def source_hash(comments: list[Comment]) -> str:
    blob = json.dumps(
        [_VERSION, [(c.recap, c.kind, c.text) for c in comments]], ensure_ascii=False
    )
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def compose_prompt(comments: list[Comment]) -> str:
    lines = ["Comments from approved event recaps:"]
    for number, comment in enumerate(comments, start=1):
        mark = "" if comment.kind == KIND_GENERAL else f" [{comment.kind}]"
        lines.append(f"{number}.{mark} {comment.text}")
    return "\n".join(lines)


def _clean_label(raw) -> str:
    label = " ".join(str(raw or "").split()).strip(" .:-")
    if len(label) > MAX_LABEL_CHARS:
        label = label[: MAX_LABEL_CHARS - 1].rstrip() + "…"
    return label


def _short_quote(text: str) -> str:
    if len(text) <= MAX_QUOTE_CHARS:
        return text
    return text[: MAX_QUOTE_CHARS - 1].rstrip() + "…"


def count_themes(raw: dict | None, comments: list[Comment]) -> list[dict]:
    """Turn the model's assignment into counted themes.

    A number that is not a comment, a comment already used by an earlier
    theme, and a comment filed under a section its kind does not allow are
    all ignored. ``recap_count`` is the number of distinct recaps among the
    comments that are left; a theme left with none is dropped. The quote is
    the stored text of the comment the model pointed at when that comment is
    in the theme, otherwise the theme's first comment.
    """
    themes_raw = (raw or {}).get("themes")
    if not isinstance(themes_raw, list):
        return []
    used: set[int] = set()
    themes: list[dict] = []
    for item in themes_raw:
        if not isinstance(item, dict):
            continue
        section = item.get("section")
        label = _clean_label(item.get("label"))
        if section not in SECTIONS or not label:
            continue
        kept: list[int] = []
        for number in item.get("comments") or []:
            if isinstance(number, bool) or not isinstance(number, int):
                continue
            index = number - 1
            if index < 0 or index >= len(comments) or index in used or index in kept:
                continue
            if section not in _ALLOWED_SECTIONS[comments[index].kind]:
                continue
            kept.append(index)
        if not kept:
            continue
        used.update(kept)
        quote_number = item.get("quote")
        quote_index = (
            quote_number - 1
            if isinstance(quote_number, int) and not isinstance(quote_number, bool)
            else -1
        )
        if quote_index not in kept:
            quote_index = kept[0]
        themes.append(
            {
                "section": section,
                "label": label,
                "recap_count": len({comments[i].recap for i in kept}),
                "comment_count": len(kept),
                "quote": _short_quote(comments[quote_index].text),
            }
        )
    themes.sort(key=lambda t: (-t["recap_count"], -t["comment_count"], t["label"].casefold()))
    return themes[:MAX_THEMES]


def build_themes(comments: list[Comment]) -> list[dict] | None:
    """Ask the model to group ``comments``. None when it is not available."""
    try:
        # Sorting comments into groups is a short classification job: the
        # small model does it in a fraction of the time the writing model
        # takes, and this call runs while someone is waiting on the page.
        raw = generate_json(
            _SYSTEM_PROMPT,
            compose_prompt(comments),
            schema=THEMES_JSON_SCHEMA,
            small=True,
            timeout=_MODEL_TIMEOUT_SECONDS,
        )
    except AiUnavailable:
        return None
    except Exception:  # noqa: BLE001
        logger.exception("feedback themes: model call failed")
        return None
    themes = count_themes(raw, comments)
    return themes or None


def _result(status: str, comments: list[Comment], *, row=None, truncated=False) -> dict:
    return {
        "status": status,
        "themes": list((row.payload or {}).get("themes") or []) if row else [],
        "recap_count": len({c.recap for c in comments}),
        "comment_count": len(comments),
        "truncated": truncated,
        "generated_at": row.generated_at.isoformat() if row else None,
    }


def get_feedback_themes(
    tenant_id: int,
    start: date | None,
    end: date | None,
    *,
    refresh: bool = False,
) -> dict:
    """Themes for a tenant and window: stored when the comments have not
    changed, otherwise built once and stored.

    ``status`` is ``ready``, ``too_few`` (under :data:`MIN_COMMENTS`
    comments, the model is not called) or ``unavailable`` (the model could
    not be reached or returned nothing usable, and nothing is stored for
    these comments). ``refresh`` builds again even when a stored row exists;
    if that fails the stored row is still served.
    """
    from tenants.models import TenantFeedbackThemes

    comments, truncated = gather_comments(tenant_id, start, end)
    if len(comments) < MIN_COMMENTS:
        return _result(STATUS_TOO_FEW, comments, truncated=truncated)

    digest = source_hash(comments)
    row = TenantFeedbackThemes.objects.filter(
        tenant_id=tenant_id, source_hash=digest
    ).first()
    if row is not None and not refresh:
        return _result(STATUS_READY, comments, row=row, truncated=truncated)

    themes = build_themes(comments)
    if themes is None:
        if row is not None:
            return _result(STATUS_READY, comments, row=row, truncated=truncated)
        return _result(STATUS_UNAVAILABLE, comments, truncated=truncated)

    row, _created = TenantFeedbackThemes.objects.update_or_create(
        tenant_id=tenant_id,
        source_hash=digest,
        defaults={
            "start_date": start,
            "end_date": end,
            "payload": {"themes": themes},
            "recap_count": len({c.recap for c in comments}),
            "comment_count": len(comments),
        },
    )
    return _result(STATUS_READY, comments, row=row, truncated=truncated)


# ─── GraphQL ────────────────────────────────────────────────────────


@strawberry.type
class FeedbackTheme:
    """One named group of consumer comments."""

    section: str
    label: str
    recap_count: int
    quote: str


@strawberry.type
class TenantFeedbackThemes:
    """What consumers said, grouped. ``status`` is ``ready``, ``too_few``
    or ``unavailable``; ``themes`` is empty unless it is ``ready``."""

    status: str
    themes: list[FeedbackTheme]
    recap_count: int
    comment_count: int
    truncated: bool
    generated_at: str | None
    start_date: str | None
    end_date: str | None


def _to_type(data: dict, start: date | None, end: date | None) -> TenantFeedbackThemes:
    return TenantFeedbackThemes(
        status=data["status"],
        themes=[
            FeedbackTheme(
                section=str(t.get("section") or ""),
                label=str(t.get("label") or ""),
                recap_count=int(t.get("recap_count") or 0),
                quote=str(t.get("quote") or ""),
            )
            for t in data["themes"]
            if isinstance(t, dict)
        ],
        recap_count=data["recap_count"],
        comment_count=data["comment_count"],
        truncated=data["truncated"],
        generated_at=data["generated_at"],
        start_date=start.isoformat() if start else None,
        end_date=end.isoformat() if end else None,
    )


def _unavailable(start: date | None, end: date | None) -> TenantFeedbackThemes:
    return TenantFeedbackThemes(
        status=STATUS_UNAVAILABLE,
        themes=[],
        recap_count=0,
        comment_count=0,
        truncated=False,
        generated_at=None,
        start_date=start.isoformat() if start else None,
        end_date=end.isoformat() if end else None,
    )


async def _load(
    target_tenant_id: int, start: date | None, end: date | None, *, refresh: bool
) -> TenantFeedbackThemes:
    def _build():
        from tenants.models import Tenant

        if not Tenant.objects.filter(id=target_tenant_id).exists():
            return None
        return get_feedback_themes(target_tenant_id, start, end, refresh=refresh)

    try:
        data = await sync_to_async(_build, thread_sensitive=True)()
    except Exception:  # noqa: BLE001
        logger.exception("tenantFeedbackThemes failed tenant=%s", target_tenant_id)
        return _unavailable(start, end)
    if data is None:
        return _unavailable(start, end)
    return _to_type(data, start, end)


@strawberry.type
class FeedbackThemeQueries:
    @strawberry.field(permission_classes=[StrictIsAuthenticated])
    async def tenant_feedback_themes(
        self,
        info: strawberry.Info,
        tenant_id: strawberry.ID,
        start_date: str | None = None,
        end_date: str | None = None,
    ) -> TenantFeedbackThemes:
        """Consumer comments from approved recaps, grouped into themes.

        A brand user reads their own tenant whatever id is passed; Ignite
        staff may pass any tenant. Dates are inclusive and compared with the
        event date. Never raises: anything that goes wrong is
        ``status: "unavailable"``.
        """
        from recaps.report_types import _CampaignReportService

        start, end = _parse_date(start_date), _parse_date(end_date)
        try:
            target = await _CampaignReportService().resolve_target_tenant_id(
                info, tenant_id
            )
        except Exception:  # noqa: BLE001
            return _unavailable(start, end)
        if target is None:
            return _unavailable(start, end)
        return await _load(target, start, end, refresh=False)


@strawberry.type
class FeedbackThemeMutations:
    @strawberry.mutation(permission_classes=[StrictIsAuthenticated])
    async def refresh_tenant_feedback_themes(
        self,
        info: strawberry.Info,
        tenant_id: strawberry.ID,
        start_date: str | None = None,
        end_date: str | None = None,
    ) -> TenantFeedbackThemes | None:
        """Build the themes again for a window. Ignite staff only; anyone
        else gets null and nothing is built."""
        from recaps.report_types import _CampaignReportService

        service = _CampaignReportService()
        if not await service.is_staff_caller(info):
            return None
        start, end = _parse_date(start_date), _parse_date(end_date)
        target = await service.resolve_target_tenant_id(info, tenant_id)
        if target is None:
            return _unavailable(start, end)
        return await _load(target, start, end, refresh=True)
