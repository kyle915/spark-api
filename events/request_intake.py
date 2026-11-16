"""Read a pasted email or sheet row into DRAFT request fields (staff only).

Clients send requests by email, or type rows into a Google Sheet tracker
(State, weekday, date, store, start "10a", end "1p", address, notes, SKUs).
Staff used to retype them into Create Request. ``parseRequestDetails`` reads
the pasted text and returns one draft per visit it finds; the web form is
pre-filled with it for staff to check and submit through the ordinary
``createRequest`` mutation and its validation.

What this module never does
---------------------------
* It never creates or changes a request, an event, a retailer or a product.
  It has no write at all.
* It never stores or logs the pasted text (only its length is logged).
* It never sets a time zone. Request times must be localised to the venue's
  own zone (``events.mutations._localize_model_times_to_own_tz``) and the
  ``Timezone`` table holds a standard and a daylight row per zone; the form
  derives the zone from the address the way it always has.

"Does not guess"
----------------
The model is asked to copy what is written and to return null for anything
that is not there. The code then does not take its word for it:

* store name, street address, city, state, ZIP and notes are kept only when
  those words are actually in the pasted text;
* the date is rebuilt here from the month / day / year the model read. With
  no year written it is the next time that day comes round, and the draft
  says so;
* times are parsed here from the text as typed ("10a" -> 10:00). A time with
  no am / pm is only filled in when the other end of the shift settles it,
  and the draft says so; otherwise it is left blank;
* products are matched against the tenant's own product list, and the store
  against its retailers and accepted store-name aliases, in code. A product
  that matches nothing is shown as unmatched, never added.

Anything left blank is listed in ``could_not_read``.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import date

import strawberry
from asgiref.sync import sync_to_async
from graphql import GraphQLError

from events.retailer_aliases import (
    accepted_alias_map,
    alias_key,
    chain_key,
    resolve_store_name,
    split_store_number,
    _words,
)
from events.staffing_board import _accessible_tenants
from utils.graphql.mixins import resolve_id_to_int
from utils.graphql.permissions import IsClientOrSparkAdmin

logger = logging.getLogger(__name__)

MAX_TEXT_CHARS = 12000
MAX_DRAFTS = 10

UNAVAILABLE_MESSAGE = (
    "Pasted details could not be read right now. Fill in the form by hand."
)
NOTHING_FOUND_MESSAGE = (
    "No visit could be read from that text. Fill in the form by hand."
)

_PHONE_RE = re.compile(r"(?<!\d)(?:\+?1[\s.\-]?)?\(?\d{3}\)?[\s.\-]\d{3}[\s.\-]\d{4}(?!\d)")
_EMAIL_RE = re.compile(r"[\w.+\-]+@[\w\-]+(?:\.[\w\-]+)+")
_SIGN_OFF_RE = re.compile(
    r"^\s*(--\s*$|thanks\b|thank you\b|many thanks\b|best\b|best regards\b|kind regards\b|"
    r"regards\b|cheers\b|sincerely\b|warmly\b|sent from my\b|get outlook for\b)",
    re.I,
)
# A line that still carries visit details: a date, or a clock time.
_DETAIL_RE = re.compile(
    r"\b\d{1,2}/\d{1,2}\b|\b\d{1,2}(:\d{2})?\s*(a|p|am|pm)\b|\b\d{5}\b", re.I
)
_MAX_SIGNATURE_LINES = 12


def clean_pasted_text(text: str | None) -> tuple[str, bool]:
    """``(text to send, truncated)``: the signature block, phone numbers and
    email addresses are taken out before anything leaves Spark.

    A sign-off ("Thanks,", "Best,", "-- ") ends the text only when it is a
    short line of its own and what follows is short and carries no date, time
    or ZIP, so a row pasted under "Thanks!" (or a store called Best Buy) is
    not thrown away.
    """
    raw = (text or "").replace("\r\n", "\n").replace("\r", "\n")
    lines = raw.split("\n")
    content_seen = 0
    for i, line in enumerate(lines):
        if not line.strip():
            continue
        if (
            content_seen >= 1
            and len(line.strip()) <= 40
            and _SIGN_OFF_RE.match(line)
            and not _DETAIL_RE.search(line)
        ):
            rest = [ln for ln in lines[i + 1 :] if ln.strip()]
            if len(rest) <= _MAX_SIGNATURE_LINES and not any(
                _DETAIL_RE.search(ln) for ln in rest
            ):
                lines = lines[:i]
                break
        content_seen += 1
    cleaned = "\n".join(lines)
    cleaned = _PHONE_RE.sub("[phone]", cleaned)
    cleaned = _EMAIL_RE.sub("[email]", cleaned)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned).strip()
    truncated = len(cleaned) > MAX_TEXT_CHARS
    return cleaned[:MAX_TEXT_CHARS], truncated


# ─── Times and dates, parsed here rather than by the model ──────────

_TIME_RE = re.compile(
    r"^\s*(\d{1,2})(?:[:.]?(\d{2}))?\s*(a\.?m?\.?|p\.?m?\.?)?\s*$", re.I
)


def _read_time(text: str | None) -> tuple[int, int, str] | None:
    """``(hour, minute, "a" | "p" | "")`` exactly as typed, or None."""
    cleaned = (text or "").strip().casefold()
    if not cleaned:
        return None
    if cleaned in ("noon", "12 noon", "12noon"):
        return 12, 0, "p"
    if cleaned == "midnight":
        return 12, 0, "a"
    m = _TIME_RE.match(cleaned)
    if not m:
        return None
    hour, minute = int(m.group(1)), int(m.group(2) or 0)
    meridiem = (m.group(3) or "")[:1]
    if minute > 59 or hour > 23 or (meridiem and not 1 <= hour <= 12):
        return None
    return hour, minute, meridiem


def _to_minutes(hour: int, minute: int, meridiem: str) -> int:
    if meridiem == "a":
        hour = 0 if hour == 12 else hour
    elif meridiem == "p":
        hour = 12 if hour == 12 else hour + 12
    return hour * 60 + minute


def _fmt(minutes: int) -> str:
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def resolve_times(start_text: str | None, end_text: str | None) -> tuple[str, str, bool]:
    """``(start "HH:MM" or "", end "HH:MM" or "", assumed)``.

    A time written with am / pm, or on the 24-hour clock (13:00), is taken
    as written. A bare "10" is only filled in when the other end of the
    shift settles which half of the day it is ("10-1p" is 10 AM); then
    ``assumed`` is True. Two bare times ("10-2") are left blank.
    """
    start, end = _read_time(start_text), _read_time(end_text)

    def certain(t):
        if t is None:
            return None
        hour, minute, meridiem = t
        if meridiem:
            return _to_minutes(hour, minute, meridiem)
        if hour >= 13 or hour == 0:
            return hour * 60 + minute
        return None

    s, e = certain(start), certain(end)
    assumed = False

    def options(t):
        hour, minute, _ = t
        return [_to_minutes(hour, minute, "a"), _to_minutes(hour, minute, "p")]

    if start is not None and s is None and e is not None:
        fits = [o for o in options(start) if o < e]
        if fits:
            s, assumed = max(fits), True  # the later one: the shorter shift
    elif end is not None and e is None and s is not None:
        fits = [o for o in options(end) if o > s]
        if fits:
            e, assumed = min(fits), True
    return (_fmt(s) if s is not None else ""), (_fmt(e) if e is not None else ""), assumed


_WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]


def resolve_date(month, day, year, *, today: date) -> tuple[date | None, bool]:
    """``(date, year_assumed)``. With no year written, the next time that
    month and day come round (today counts)."""
    try:
        month, day = int(month), int(day)
    except (TypeError, ValueError):
        return None, False
    try:
        written = int(year) if year not in (None, "") else None
    except (TypeError, ValueError):
        written = None
    if written is not None:
        if written < 100:
            written += 2000
        if not 2000 <= written <= today.year + 5:
            return None, False
        try:
            return date(written, month, day), False
        except ValueError:
            return None, False
    for candidate_year in (today.year, today.year + 1, today.year + 2, today.year + 3, today.year + 4):
        try:
            candidate = date(candidate_year, month, day)
        except ValueError:
            continue  # 29 February in a year that has none
        if candidate >= today:
            return candidate, True
    return None, False


def _weekday_warning(weekday_text: str | None, when: date | None) -> str:
    typed = (weekday_text or "").strip().casefold()
    if not typed or when is None:
        return ""
    for index, name in enumerate(_WEEKDAYS):
        if typed.startswith(name[:3]):
            if index != when.weekday():
                return (
                    f"The text says {name.title()}, but {when.strftime('%B')} {when.day}, "
                    f"{when.year} is a {_WEEKDAYS[when.weekday()].title()}."
                )
            return ""
    return ""


# ─── Matching against what Spark already holds ──────────────────────


def _appears(value: str | None, haystack_words: str) -> bool:
    """True when ``value``'s words are in the pasted text, in order."""
    needle = _words(value)
    return bool(needle) and f" {needle} " in f" {haystack_words} "


def match_products(
    typed: list[str], catalog: list[tuple[str, str]]
) -> tuple[list[tuple[str, str]], list[str]]:
    """``(matched [(uuid, name)], unmatched typed names)``.

    A typed name matches a product when they are the same once case and
    punctuation are ignored, or when exactly one product contains it (or is
    contained in it). Two candidates is not a match.
    """
    by_key: dict[str, tuple[str, str]] = {}
    for uuid, name in catalog:
        by_key.setdefault(chain_key(name), (uuid, name))
    matched: list[tuple[str, str]] = []
    unmatched: list[str] = []
    for raw in typed:
        cleaned = " ".join(str(raw or "").split())
        if not cleaned:
            continue
        key = chain_key(cleaned)
        hit = by_key.get(key)
        if hit is None and len(key) >= 3:
            words = f" {_words(cleaned)} "
            near = [
                (uuid, name)
                for uuid, name in catalog
                if words in f" {_words(name)} " or f" {_words(name)} " in words
            ]
            if len(near) == 1:
                hit = near[0]
        if hit is None:
            if cleaned not in unmatched:
                unmatched.append(cleaned)
        elif hit not in matched:
            matched.append(hit)
    return matched, unmatched


def match_store(
    typed: str, retailer_names: list[str], aliases: dict[str, str]
) -> tuple[str, str]:
    """``(name Spark already uses for this store, store number)``.

    Tries the whole typed name, then the name without a trailing store
    number ("Heb 725" -> H-E-B, store 725). ``("", "")`` when nothing known
    matches: the typed name is then used as it is.
    """
    cleaned = " ".join((typed or "").split())
    if not cleaned:
        return "", ""
    known: dict[str, str] = {}
    for name in retailer_names:
        known.setdefault(chain_key(name), name)
    for canonical in aliases.values():
        known.setdefault(chain_key(canonical), canonical)

    def lookup(name: str) -> str:
        key = alias_key(name)
        if key in aliases:
            return resolve_store_name(name, aliases)
        held = known.get(chain_key(name))
        return resolve_store_name(held, aliases) if held else ""

    whole = lookup(cleaned)
    if whole:
        return whole, ""
    base, number, _marked = split_store_number(cleaned)
    if number:
        banner = lookup(base)
        if banner:
            return banner, number
    return "", ""


# ─── The model call ─────────────────────────────────────────────────

_NULLABLE_STRING = {"type": ["string", "null"]}
_NULLABLE_INT = {"type": ["integer", "null"]}

_AI_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["requests"],
    "properties": {
        "requests": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": [
                    "date_text", "weekday_text", "month", "day", "year",
                    "start_text", "end_text", "store_name", "street_address",
                    "city", "state", "zip", "notes", "products",
                ],
                "properties": {
                    "date_text": _NULLABLE_STRING,
                    "weekday_text": _NULLABLE_STRING,
                    "month": _NULLABLE_INT,
                    "day": _NULLABLE_INT,
                    "year": _NULLABLE_INT,
                    "start_text": _NULLABLE_STRING,
                    "end_text": _NULLABLE_STRING,
                    "store_name": _NULLABLE_STRING,
                    "street_address": _NULLABLE_STRING,
                    "city": _NULLABLE_STRING,
                    "state": _NULLABLE_STRING,
                    "zip": _NULLABLE_STRING,
                    "notes": _NULLABLE_STRING,
                    "products": {"type": "array", "items": {"type": "string"}},
                },
            },
        }
    },
}

_AI_SYSTEM = (
    "You read a pasted email or spreadsheet rows asking for in-store product sampling "
    "visits, and copy out the details of each visit. Return one object per visit "
    f"(at most {MAX_DRAFTS}); a spreadsheet row is one visit. Copy text exactly as it is "
    "written. If something is not written, return null for it: never fill a gap from "
    "what you know about a store, a city or a product, and never work out an address. "
    "date_text is the date as written; month and day are its numbers; year is null "
    "unless a year is written. weekday_text is the day of the week if one is written. "
    "start_text and end_text are the times exactly as written (for example \"10a\", "
    "\"1p\", \"2:30 PM\"); do not convert them. store_name is the store or retailer as "
    "written, with its store number if one is written. street_address is the street "
    "line only; city, state and zip are separate and each null unless written. notes "
    "is any instruction for the visit, copied word for word, or null. products lists "
    "each product or SKU named for sampling, as written. Do not include greetings, "
    "signatures, people's names or contact details."
)


def _ask_model(cleaned_text: str) -> list[dict] | None:
    """The model's reading of the text, or None when AI is off or fails."""
    from utils.ai_text import AiUnavailable, generate_json

    try:
        out = generate_json(
            _AI_SYSTEM,
            "Pasted text:\n\n" + cleaned_text,
            schema=_AI_SCHEMA,
            max_tokens=4000,
        )
    except AiUnavailable:
        return None
    except Exception:  # noqa: BLE001
        logger.info("request intake: AI call failed", exc_info=True)
        return None
    if not isinstance(out, dict) or not isinstance(out.get("requests"), list):
        return None
    return [r for r in out["requests"] if isinstance(r, dict)]


# ─── Drafts ─────────────────────────────────────────────────────────


@dataclass
class Draft:
    date: str = ""
    date_as_typed: str = ""
    year_assumed: bool = False
    date_warning: str = ""
    start_time: str = ""
    start_as_typed: str = ""
    end_time: str = ""
    end_as_typed: str = ""
    time_assumed: bool = False
    store_name: str = ""
    matched_store_name: str = ""
    store_number: str = ""
    street_address: str = ""
    city: str = ""
    state: str = ""
    zip: str = ""
    address: str = ""
    notes: str = ""
    products: list[tuple[str, str]] = field(default_factory=list)
    unmatched_products: list[str] = field(default_factory=list)
    could_not_read: list[str] = field(default_factory=list)


def _text(value) -> str:
    return " ".join(str(value).split()) if isinstance(value, (str, int)) else ""


def build_draft(
    raw: dict,
    *,
    text_words: str,
    today: date,
    catalog: list[tuple[str, str]],
    retailer_names: list[str],
    aliases: dict[str, str],
) -> Draft | None:
    """One model object -> one checked draft. None when it holds nothing."""
    draft = Draft()

    when, year_assumed = resolve_date(raw.get("month"), raw.get("day"), raw.get("year"), today=today)
    draft.date_as_typed = _text(raw.get("date_text"))[:60]
    if when is not None:
        draft.date = when.isoformat()
        draft.year_assumed = year_assumed
        draft.date_warning = _weekday_warning(raw.get("weekday_text"), when)

    draft.start_as_typed = _text(raw.get("start_text"))[:30]
    draft.end_as_typed = _text(raw.get("end_text"))[:30]
    draft.start_time, draft.end_time, draft.time_assumed = resolve_times(
        draft.start_as_typed, draft.end_as_typed
    )

    def grounded(key: str, limit: int) -> str:
        value = _text(raw.get(key))[:limit]
        return value if value and _appears(value, text_words) else ""

    draft.store_name = grounded("store_name", 80)
    draft.street_address = grounded("street_address", 200)
    draft.city = grounded("city", 80)
    draft.state = grounded("state", 30)
    draft.zip = grounded("zip", 10)
    if draft.zip and not re.fullmatch(r"\d{5}(-\d{4})?", draft.zip):
        draft.zip = ""
    draft.notes = grounded("notes", 1000)

    if draft.store_name:
        draft.matched_store_name, draft.store_number = match_store(
            draft.store_name, retailer_names, aliases
        )
        if not draft.store_number:
            _base, number, marked = split_store_number(draft.store_name)
            if number and marked:
                draft.store_number = number

    tail = " ".join(part for part in (draft.state, draft.zip) if part)
    draft.address = ", ".join(
        part for part in (draft.street_address, draft.city, tail) if part
    )

    typed_products = [
        _text(p)[:80]
        for p in (raw.get("products") or [])
        if _text(p) and _appears(_text(p), text_words)
    ]
    draft.products, draft.unmatched_products = match_products(typed_products, catalog)

    for label, value in (
        ("Date", draft.date),
        ("Start time", draft.start_time),
        ("End time", draft.end_time),
        ("Store", draft.store_name),
        ("Street address", draft.street_address),
        ("City", draft.city),
        ("State", draft.state),
        ("ZIP", draft.zip),
    ):
        if not value:
            draft.could_not_read.append(label)

    has_anything = any(
        (draft.date, draft.start_time, draft.end_time, draft.store_name,
         draft.street_address, draft.products, draft.unmatched_products)
    )
    return draft if has_anything else None


def parse_details(tenant_id: int, text: str | None, *, today: date | None = None):
    """``(ok, message, truncated, drafts)``. Reads; writes nothing."""
    from events.live_board import _ops_today
    from events.models import Product, Retailer

    cleaned, truncated = clean_pasted_text(text)
    if len(cleaned) < 8:
        return False, "Paste the email or the sheet row first.", False, []
    raw = _ask_model(cleaned)
    if raw is None:
        return False, UNAVAILABLE_MESSAGE, truncated, []

    catalog = [
        (str(uuid), name)
        for uuid, name in Product.objects.filter(tenant_id=tenant_id)
        .order_by("id")
        .values_list("uuid", "name")
    ]
    retailer_names = list(
        Retailer.objects.filter(tenant_id=tenant_id)
        .exclude(name="")
        .order_by("id")
        .values_list("name", flat=True)
    )
    aliases = accepted_alias_map(tenant_id)
    text_words = _words(cleaned)
    today = today or _ops_today()

    drafts: list[Draft] = []
    for item in raw[:MAX_DRAFTS]:
        draft = build_draft(
            item,
            text_words=text_words,
            today=today,
            catalog=catalog,
            retailer_names=retailer_names,
            aliases=aliases,
        )
        if draft is not None:
            drafts.append(draft)
    if len(raw) > MAX_DRAFTS:
        truncated = True
    logger.info(
        "request intake: tenant=%s chars=%s drafts=%s", tenant_id, len(cleaned), len(drafts)
    )
    if not drafts:
        return False, NOTHING_FOUND_MESSAGE, truncated, []
    return True, "", truncated, drafts


# ─── GraphQL ────────────────────────────────────────────────────────


@strawberry.type
class RequestIntakeProduct:
    uuid: strawberry.ID
    name: str


@strawberry.type
class RequestIntakeDraft:
    # "YYYY-MM-DD", or "" when no date could be read.
    date: str
    date_as_typed: str
    # True when no year was written and the next occurrence was used.
    year_assumed: bool
    # Set when a written weekday does not match the date.
    date_warning: str
    # "HH:MM" on the 24-hour clock, or "".
    start_time: str
    start_as_typed: str
    end_time: str
    end_as_typed: str
    # True when am / pm was not written and was settled by the other time.
    time_assumed: bool
    store_name: str
    # The name Spark already uses for this store, "" when it is a new name.
    matched_store_name: str
    store_number: str
    street_address: str
    city: str
    state: str
    zip: str
    # The parts above on one line, for the form's address box.
    address: str
    notes: str
    products: list[RequestIntakeProduct]
    unmatched_products: list[str]
    could_not_read: list[str]


@strawberry.type
class RequestIntakeResult:
    ok: bool
    # Why nothing came back; "" on success.
    message: str
    # True when the text was cut, or held more visits than are returned.
    truncated: bool
    drafts: list[RequestIntakeDraft]


def _to_type(draft: Draft) -> RequestIntakeDraft:
    return RequestIntakeDraft(
        date=draft.date,
        date_as_typed=draft.date_as_typed,
        year_assumed=draft.year_assumed,
        date_warning=draft.date_warning,
        start_time=draft.start_time,
        start_as_typed=draft.start_as_typed,
        end_time=draft.end_time,
        end_as_typed=draft.end_as_typed,
        time_assumed=draft.time_assumed,
        store_name=draft.store_name,
        matched_store_name=draft.matched_store_name,
        store_number=draft.store_number,
        street_address=draft.street_address,
        city=draft.city,
        state=draft.state,
        zip=draft.zip,
        address=draft.address,
        notes=draft.notes,
        products=[RequestIntakeProduct(uuid=u, name=n) for u, n in draft.products],
        unmatched_products=draft.unmatched_products,
        could_not_read=draft.could_not_read,
    )


@strawberry.type
class RequestIntakeMutations:
    @strawberry.mutation(permission_classes=[IsClientOrSparkAdmin])
    async def parse_request_details(
        self, info: strawberry.Info, tenant_id: strawberry.ID, text: str
    ) -> RequestIntakeResult:
        """Read pasted text into draft request fields. Staff only. A mutation
        so the text travels in a request body; it creates nothing."""
        is_admin, _allowed = await _accessible_tenants(info.context.request.user)
        if not is_admin:
            raise GraphQLError("Staff only.")
        try:
            resolved = resolve_id_to_int(tenant_id)
        except Exception as exc:  # noqa: BLE001
            raise GraphQLError("Invalid tenant id.") from exc
        try:
            ok, message, truncated, drafts = await sync_to_async(parse_details)(
                resolved, text
            )
        except Exception:  # noqa: BLE001 — the form must stay usable by hand
            logger.exception("request intake failed tenant=%s", resolved)
            return RequestIntakeResult(
                ok=False, message=UNAVAILABLE_MESSAGE, truncated=False, drafts=[]
            )
        return RequestIntakeResult(
            ok=ok, message=message, truncated=truncated, drafts=[_to_type(d) for d in drafts]
        )
