"""One plain line for the reviewer: why this recap was flagged, and what to
look at first.

A recap under review already shows its checks — quality flags
(:mod:`recaps.recap_quality`), rule checks (:mod:`recaps.recap_review_checks`)
and photo reuse (:mod:`recaps.photo_reuse`). With several raised at once the
reviewer has to read them all to see what matters. This module turns the
raised flags into one or two sentences.

* **One flag** — a fixed sentence built from the flag's own numbers. No model.
* **Several flags** — the model is asked to join them into one readable line.
  It is handed the flag CODES and their NUMBERS only: no ambassador, store or
  brand name, no file name, no answer a person typed. Its sentence is thrown
  away if it contains a number it was not given. The "look at this first"
  sentence is always chosen in code and appended, never written by the model.
* **No model** (not configured, failed, or the recap is not waiting on
  review) — no line. The page shows its list of checks, as it always has.

The line is cached on :class:`recaps.models.RecapAiReview` next to the hash of
the codes and numbers it was written from, so opening the page does not call
the model again; it is rewritten only when the flags change. A failed attempt
is remembered for a while too, for the same reason.

Staff only. Nothing here approves, rejects, edits or sends anything.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from datetime import datetime, timedelta

import strawberry
from asgiref.sync import sync_to_async
from django.utils import timezone

from utils.graphql.mixins import resolve_id_to_int
from utils.graphql.permissions import StrictIsAuthenticated

logger = logging.getLogger(__name__)

MAX_SUMMARY_CHARS = 280
# How long a failed model attempt is remembered before the page asks again.
RETRY_AFTER = timedelta(hours=6)

# Most telling first. The first raised code in this order decides the
# "look at this first" sentence.
_PRIORITY: tuple[str, ...] = (
    "sold_exceeds_sampled",
    "photo_reuse",
    "engagements_rate",
    "implausible_value",
    "negative_value",
    "receipt_total_mismatch",
    "spend_without_receipt",
    "gps_distance",
    "clock_out_distance",
    "shift_overrun",
    "no_kpis",
    "all_zero_kpis",
    "no_photos",
    "photo_buckets",
    "duplicate_photos",
    "few_photos",
    "no_feedback",
    "low_feedback_quality",
)
_RANK = {code: i for i, code in enumerate(_PRIORITY)}

_LOOK_FIRST: dict[str, str] = {
    "sold_exceeds_sampled": "Check the sold count first.",
    "photo_reuse": "Compare the photos first.",
    "engagements_rate": "Check the counts for a typo first.",
    "implausible_value": "Check the counts for a typo first.",
    "negative_value": "Check the counts for a typo first.",
    "receipt_total_mismatch": "Check the spend against the receipts first.",
    "spend_without_receipt": "Ask for the receipt first.",
    "gps_distance": "Check where the shift was worked first.",
    "clock_out_distance": "Check where the shift ended first.",
    "shift_overrun": "Check the clock-out time first.",
    "no_kpis": "Check whether the counts were filled in first.",
    "all_zero_kpis": "Check whether the counts were filled in first.",
    "no_photos": "Ask for photos first.",
    "photo_buckets": "Check the photos first.",
    "duplicate_photos": "Check the photos first.",
    "few_photos": "Check the photos first.",
    "no_feedback": "Read the feedback first.",
    "low_feedback_quality": "Read the feedback first.",
}


def _plural(n, one: str, many: str) -> str:
    return one if n == 1 else many


def _fragment(code: str, n: dict) -> str:
    """The flag as a lower-case clause, from its code and numbers alone."""
    try:
        if code == "sold_exceeds_sampled" and n:
            return f"sold {n['sold']} against {n['sampled']} samples handed out"
        if code == "photo_reuse" and n:
            photos = n["photos"]
            where = (
                f"the {n['date']} recap"
                if n.get("other_recaps") == 1 and n.get("date")
                else f"{n['other_recaps']} other recaps"
                if n.get("other_recaps", 0) > 1
                else "another recap"
            )
            return (
                f"{photos} {_plural(photos, 'photo', 'photos')} also "
                f"{_plural(photos, 'appears', 'appear')} on {where}"
            )
        if code == "engagements_rate" and n:
            return (
                f"{n['count']} {n['kind']} were logged in a {n['hours']}-hour shift, "
                f"about {n['per_hour']} an hour"
            )
        if code == "receipt_total_mismatch" and n:
            return f"${n['entered']} was entered but the receipts total ${n['receipts']}"
        if code == "spend_without_receipt" and n:
            return f"${n['spend']} of spend has no receipt photo"
        if code == "gps_distance" and n:
            return f"the clock-in was {n['miles']} miles from the venue"
        if code == "clock_out_distance" and n:
            return f"the clock-out was {n['miles']} miles from the venue"
        if code == "shift_overrun" and n:
            return (
                f"{n['worked_hours']} hours were on the clock for a "
                f"{n['scheduled_hours']}-hour shift"
            )
        if code == "photo_buckets" and n:
            missing = n["missing"]
            return (
                f"{missing} of {n['required']} required photo groups "
                f"{_plural(missing, 'is', 'are')} empty"
            )
        if code == "duplicate_photos" and n:
            extra = n["extra"]
            return f"the same photo is filed {extra} extra {_plural(extra, 'time', 'times')}"
        if code == "few_photos" and n:
            photos = n["photos"]
            return f"there {_plural(photos, 'is', 'are')} only {photos} {_plural(photos, 'photo', 'photos')}"
    except (KeyError, TypeError):
        pass
    return _BARE.get(code, "a check was flagged")


_BARE: dict[str, str] = {
    "sold_exceeds_sampled": "more was sold than was handed out",
    "photo_reuse": "a photo also appears on another recap",
    "engagements_rate": "the counts are too high for the shift length",
    "implausible_value": "a count is too large to be right",
    "negative_value": "a count is below zero",
    "receipt_total_mismatch": "the spend entered does not match the receipts",
    "spend_without_receipt": "spend was entered with no receipt photo",
    "gps_distance": "the clock-in was far from the venue",
    "clock_out_distance": "the clock-out was far from the venue",
    "shift_overrun": "the time on the clock runs well past the shift",
    "no_kpis": "no counts were recorded",
    "all_zero_kpis": "every count is zero",
    "no_photos": "there are no photos",
    "photo_buckets": "a required photo group is empty",
    "duplicate_photos": "the same photo is filed more than once",
    "few_photos": "there are too few photos",
    "no_feedback": "no feedback was written",
    "low_feedback_quality": "the feedback is thin",
}


# ---------------------------------------------------------------------------
# Numbers. Read from structured facts where there are any; otherwise from the
# check's own code-generated wording (fixed formats in recap_review_checks).
# ---------------------------------------------------------------------------

_RATE_RE = re.compile(
    r"^([\d,]+) (engagements|samples) in a ([\d.]+)h shift \(~([\d,]+)/hr\)"
)
_MILES_RE = re.compile(r"([\d.]+) mi\b")
_OVERRUN_RE = re.compile(r"On the clock ([\d.]+)h for a ([\d.]+)h shift")
_BUCKETS_RE = re.compile(r"(\d+)/(\d+) required buckets")
_DUPES_RE = re.compile(r"(\d+) duplicate filing")
_MONEY_RE = re.compile(r"\$([\d,]+(?:\.\d\d)?)")


def _check_numbers(code: str, label: str, detail: str) -> dict:
    if code == "engagements_rate":
        m = _RATE_RE.search(label)
        if m:
            return {
                "count": m.group(1),
                "kind": m.group(2),
                "hours": m.group(3),
                "per_hour": m.group(4),
            }
    elif code in ("gps_distance", "clock_out_distance"):
        m = _MILES_RE.search(label)
        if m:
            return {"miles": m.group(1)}
    elif code == "shift_overrun":
        m = _OVERRUN_RE.search(label)
        if m:
            return {"worked_hours": m.group(1), "scheduled_hours": m.group(2)}
    elif code == "photo_buckets":
        m = _BUCKETS_RE.search(detail)
        if m:
            have, required = int(m.group(1)), int(m.group(2))
            return {"missing": max(0, required - have), "required": required}
    elif code == "duplicate_photos":
        m = _DUPES_RE.search(detail)
        if m:
            return {"extra": int(m.group(1))}
    elif code == "spend_without_receipt":
        m = _MONEY_RE.search(detail)
        if m:
            return {"spend": m.group(1)}
    elif code == "receipt_total_mismatch":
        amounts = _MONEY_RE.findall(label)
        if len(amounts) >= 2:
            if label.startswith("Receipts total"):
                return {"receipts": amounts[0], "entered": amounts[1]}
            return {"entered": amounts[0], "receipts": amounts[1]}
    return {}


def _short_date(iso: str) -> str:
    try:
        day = datetime.strptime(str(iso)[:10], "%Y-%m-%d")
    except ValueError:
        return ""
    return f"{day.strftime('%b')} {day.day}"


def gather_signals(recap_id: int, is_custom: bool) -> list[dict]:
    """The flags raised on a recap as ``{"code", "numbers"}``, most telling
    first. Codes and numbers only: nothing a person typed, and no name."""
    from recaps.models import RecapQualitySnapshot
    from recaps.photo_reuse import photo_reuse_checks
    from recaps.recap_quality import _load_facts, _run_deterministic
    from recaps.recap_review_checks import recap_review_checks

    signals: dict[str, dict] = {}

    facts = _load_facts(recap_id, is_custom)
    if not facts.exists:
        return []
    for flag in _run_deterministic(facts):
        code = str(flag.get("code") or "")
        if not code:
            continue
        numbers: dict = {}
        if code == "sold_exceeds_sampled":
            numbers = {"sold": facts.products_sold, "sampled": facts.samples_distributed}
        elif code == "few_photos":
            numbers = {"photos": facts.photo_count}
        signals[code] = numbers

    # The cached thin-feedback verdict, if one was already made. Read only:
    # this never asks the model about the feedback text.
    try:
        if RecapQualitySnapshot.objects.filter(
            recap_id=recap_id, is_custom=is_custom, low_quality=True
        ).exists():
            signals["low_feedback_quality"] = {}
    except Exception:
        pass

    for check in recap_review_checks(recap_id, is_custom=is_custom):
        if not isinstance(check, dict) or check.get("status") != "flag":
            continue
        code = str(check.get("code") or "")
        if code:
            signals[code] = _check_numbers(
                code, str(check.get("label") or ""), str(check.get("detail") or "")
            )

    try:
        reused = [
            item
            for item in (photo_reuse_checks(recap_id, is_custom=is_custom) or {}).get("items")
            or []
            if isinstance(item, dict) and item.get("status") == "reused"
        ]
    except Exception:
        reused = []
    if reused:
        others: set[str] = set()
        dates: list[str] = []
        for item in reused:
            for match in item.get("matches") or []:
                if not isinstance(match, dict):
                    continue
                rid = str(match.get("recapId") or "")
                if rid and rid not in others:
                    others.add(rid)
                    dates.append(str(match.get("eventDate") or ""))
        numbers = {"photos": len(reused), "other_recaps": len(others)}
        if len(others) == 1 and dates and _short_date(dates[0]):
            numbers["date"] = _short_date(dates[0])
        signals["photo_reuse"] = numbers

    ordered = sorted(signals, key=lambda code: (_RANK.get(code, len(_RANK)), code))
    return [{"code": code, "numbers": signals[code]} for code in ordered]


def signals_hash(signals: list[dict]) -> str:
    raw = json.dumps(signals, sort_keys=True, default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _sentence(text: str) -> str:
    text = text.strip().rstrip(".")
    return (text[:1].upper() + text[1:] + ".") if text else ""


def look_first(signals: list[dict]) -> str:
    return _LOOK_FIRST.get(signals[0]["code"], "Check that first.") if signals else ""


def single_flag_line(signals: list[dict]) -> str:
    """The fixed sentence for a recap with exactly one flag."""
    signal = signals[0]
    return f"{_sentence(_fragment(signal['code'], signal['numbers']))} {look_first(signals)}"


# ---------------------------------------------------------------------------
# Several flags: one model call, checked, cached.
# ---------------------------------------------------------------------------

_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["why"],
    "properties": {"why": {"type": "string"}},
}

_SYSTEM = (
    "You help a reviewer at a field-marketing agency. A report from an event "
    "was flagged by several automatic checks. You are given each check as a "
    "code, its numbers, and a plain clause. Write ONE sentence, or two short "
    "ones, that says why the report was flagged, joining the checks in the "
    "order given (the first matters most). Plain words, sentence case, no "
    "lists, no advice, no opinion on who is at fault. Use only the numbers "
    "you are given, exactly as written; do not add, round or compute any. Do "
    "not mention names, stores or brands — you are not given any. Do not say "
    "what to look at first; that is added afterwards. At most 45 words."
)


def _prompt(signals: list[dict]) -> str:
    lines = ["Checks, most important first:"]
    for signal in signals:
        numbers = json.dumps(signal["numbers"], sort_keys=True, default=str)
        lines.append(
            f"- code: {signal['code']} | numbers: {numbers} | "
            f"plain: {_fragment(signal['code'], signal['numbers'])}"
        )
    return "\n".join(lines)


_NUMBER_RE = re.compile(r"\d+(?:[.,]\d+)*")
# Small counts a sentence may spell out. "One" is left out: it is also an
# ordinary word ("one of the checks").
_NUMBER_WORDS = {
    "two": "2", "three": "3", "four": "4", "five": "5", "six": "6",
    "seven": "7", "eight": "8", "nine": "9", "ten": "10", "eleven": "11",
    "twelve": "12",
}


def _allowed_numbers(signals: list[dict]) -> set[str]:
    allowed: set[str] = set()
    for signal in signals:
        text = _fragment(signal["code"], signal["numbers"])
        for value in signal["numbers"].values():
            text += f" {value}"
        for found in _NUMBER_RE.findall(text):
            allowed.add(found)
            allowed.add(found.replace(",", ""))
    return allowed


def _clean_model_line(raw, signals: list[dict]) -> str | None:
    """The model's sentence if it is safe to show, else None."""
    if not isinstance(raw, dict) or not isinstance(raw.get("why"), str):
        return None
    text = " ".join(raw["why"].split())
    if not text or len(text) > MAX_SUMMARY_CHARS:
        return None
    allowed = _allowed_numbers(signals)
    for found in _NUMBER_RE.findall(text):
        if found not in allowed and found.replace(",", "") not in allowed:
            return None
    # A small count may be spelled out ("two photos"); it must still be one
    # of the numbers given.
    low = text.lower()
    for word, digits in _NUMBER_WORDS.items():
        if re.search(rf"\b{word}\b", low) and digits not in allowed:
            return None
    return _sentence(text)


def _needs_review(recap_id: int, is_custom: bool) -> bool:
    from recaps.models import CustomRecap, Recap

    model = CustomRecap if is_custom else Recap
    row = model.objects.filter(id=recap_id).values_list("approved", "archived_at").first()
    return row is not None and not row[0] and row[1] is None


def _result(summary: str | None, source: str, count: int) -> dict:
    return {"summary": summary, "source": source, "flag_count": count}


def flag_explanation(recap_id, is_custom: bool = False) -> dict | None:
    """``{"summary", "source", "flag_count"}`` for one recap, or None when
    nothing is flagged. ``summary`` is None when there is no line to show
    (several flags and no model): the page then shows its list of checks.
    Never raises."""
    try:
        rid = int(recap_id)
        signals = gather_signals(rid, bool(is_custom))
    except Exception:
        logger.warning("flag explanation failed recap=%s", recap_id)
        return None
    if not signals:
        return None
    count = len(signals)
    if count == 1:
        return _result(single_flag_line(signals), "rules", count)
    try:
        return _result(*_combined_line(rid, bool(is_custom), signals), count)
    except Exception:
        logger.warning("flag explanation failed recap=%s", recap_id)
        return _result(None, "rules", count)


def _combined_line(recap_id: int, is_custom: bool, signals: list[dict]) -> tuple[str | None, str]:
    from recaps.models import RecapAiReview
    from utils.ai_text import AiUnavailable, generate_json

    digest = signals_hash(signals)
    row = RecapAiReview.objects.filter(recap_id=recap_id, is_custom=is_custom).first()
    cached = row.flag_explanation if row is not None else None
    if isinstance(cached, dict) and row.flag_explanation_hash == digest:
        text = cached.get("text")
        if isinstance(text, str) and text:
            return text, "model"
        try:
            tried = datetime.fromisoformat(str(cached.get("at")))
        except (TypeError, ValueError):
            tried = None
        if tried is not None and timezone.now() - tried < RETRY_AFTER:
            return None, "rules"

    # Only a recap still waiting on review is worth a model call.
    if not _needs_review(recap_id, is_custom):
        return None, "rules"

    try:
        raw = generate_json(_SYSTEM, _prompt(signals), schema=_SCHEMA, max_tokens=300)
    except AiUnavailable:
        # Not configured: no call was made, so there is nothing to remember.
        return None, "rules"
    why = _clean_model_line(raw, signals)
    text = f"{why} {look_first(signals)}" if why else ""

    row, _created = RecapAiReview.objects.get_or_create(
        recap_id=recap_id, is_custom=is_custom
    )
    row.flag_explanation = {"text": text, "at": timezone.now().isoformat()}
    row.flag_explanation_hash = digest
    row.save(update_fields=["flag_explanation", "flag_explanation_hash"])
    return (text, "model") if text else (None, "rules")


# ---------------------------------------------------------------------------
# GraphQL. Staff only.
# ---------------------------------------------------------------------------


@strawberry.type(name="RecapFlagExplanation")
class RecapFlagExplanationType:
    """Why a recap was flagged, in a line. ``summary`` is null when there is
    no line to show; ``source`` is ``"rules"`` (a fixed sentence) or
    ``"model"`` (several flags joined)."""

    summary: str | None
    source: str
    flag_count: int


@strawberry.type
class RecapFlagExplanationQueries:
    @strawberry.field(permission_classes=[StrictIsAuthenticated])
    async def recap_flag_explanation(
        self,
        info: strawberry.Info,
        recap_id: strawberry.ID,
        is_custom: bool = False,
    ) -> RecapFlagExplanationType | None:
        """One line on why a recap was flagged, for the reviewer.

        Ignite staff only — a brand user reads null, for their own recaps
        too. Null as well when nothing is flagged. Never approves, rejects
        or edits the recap.
        """
        from events.staffing_board import _accessible_tenants

        try:
            is_admin, _allowed = await _accessible_tenants(info.context.request.user)
        except Exception:
            return None
        if not is_admin:
            return None
        try:
            rid = resolve_id_to_int(str(recap_id).strip())
        except Exception:
            return None
        payload = await sync_to_async(flag_explanation, thread_sensitive=True)(
            rid, is_custom
        )
        if payload is None:
            return None
        return RecapFlagExplanationType(
            summary=payload["summary"],
            source=payload["source"],
            flag_count=payload["flag_count"],
        )
