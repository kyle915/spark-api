"""Fraud and quality flags on custom recaps. STAFF ONLY.

Every rule here is a fixed, explainable check over facts Spark already
stores: receipt readings (``recaps.receipt_reader``), photo hashes, clock
punches, and who filed the recap and when. Nothing calls a model, and
nothing here approves, rejects or edits a recap. The result is a
:class:`recaps.models.RecapFlag` row with a plain-language explanation that
names the other recap or store, for a staff member to look at and resolve.

Rules (kinds):

* ``duplicate_receipt`` — a receipt on this recap matches a receipt on
  another recap, any tenant: same store + ticket + date + total, same store +
  ticket + date (a receipt photographed in halves), or same ticket + date +
  total under a different store header (an edited header). The later upload
  is ``high``; the earlier one ``medium``.
* ``store_mismatch`` — the receipt's store number (or city) is not the
  event's. Also raised on the later copy of a duplicated receipt whose header
  was changed: the earlier copy shows where the ticket was really printed.
* ``short_clock`` — clocked in and out within N minutes
  (``RECEIPT_FLAG_SHORT_CLOCK_MINUTES``, default 15) on a recap that reports
  consumers, samples or sales.
* ``late_staff_filing`` — filed N or more days after the event
  (``RECEIPT_FLAG_LATE_FILING_DAYS``, default 7) by staff on the ambassador's
  behalf.
* ``duplicate_photo`` — the same or a nearly identical photo (SHA-256, or a
  64-bit difference hash within a few bits) is on another recap; repeats
  inside one recap are ``low``.
* ``receipt_math`` — a receipt's items, fees and tax do not add up to its
  printed total.
* ``multiple_receipts`` — one photo shows several paper receipts; only one of
  them was read, so the others are missing from spend and duplicate checks.

Co-activation slices share photos and receipts on purpose and are never
compared with each other.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from datetime import date, datetime, timedelta
from decimal import Decimal

from django.conf import settings
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

logger = logging.getLogger(__name__)

DEFAULT_SHORT_CLOCK_MINUTES = 15
DEFAULT_LATE_FILING_DAYS = 7
# Bits two difference hashes may differ by and still be "the same photo".
NEAR_PHOTO_BITS = 4
# How far apart (event dates) two recaps can be for a near-identical photo
# to be compared. Exact copies are compared without a window.
NEAR_PHOTO_WINDOW_DAYS = 60

KIND_LABELS = {
    "duplicate_receipt": "Duplicate receipt",
    "store_mismatch": "Receipt from another store",
    "short_clock": "Very short clock-in",
    "late_staff_filing": "Filed late by staff",
    "duplicate_photo": "Duplicate photo",
    "receipt_math": "Receipt doesn't add up",
    "multiple_receipts": "Several receipts in one photo",
}

_ACTIVITY_FIELD_WORDS = (
    "consum", "sampl", "sold", "sale", "unit", "engag", "trial", "interact",
    "purchas", "bought",
)


def flags_enabled() -> bool:
    return bool(getattr(settings, "RECEIPT_FLAGS_ENABLED", False))


def _setting_int(name: str, default: int) -> int:
    try:
        return max(1, int(getattr(settings, name, default)))
    except (TypeError, ValueError):
        return default


# ─── Small formatting helpers ───────────────────────────────────────


def _money(value) -> str:
    try:
        return f"${Decimal(str(value)):,.2f}"
    except Exception:  # noqa: BLE001
        return ""


def _day_label(day: date | None) -> str:
    if not day:
        return ""
    return f"{day.strftime('%b')} {day.day}, {day.year}"


def _time_label(hhmm: str) -> str:
    try:
        hour, minute = (int(x) for x in hhmm.split(":"))
    except (ValueError, AttributeError):
        return ""
    suffix = "AM" if hour < 12 else "PM"
    return f"{(hour % 12) or 12}:{minute:02d} {suffix}"


def _duration_label(seconds: float) -> str:
    seconds = max(0, int(round(seconds)))
    if seconds < 60:
        return f"{seconds} second{'s' if seconds != 1 else ''}"
    minutes = seconds // 60
    return f"{minutes} minute{'s' if minutes != 1 else ''}"


def _event_day(event) -> date | None:
    for attr in ("date", "start_time"):
        value = getattr(event, attr, None) if event is not None else None
        if isinstance(value, datetime):
            return value.date()
    return None


def _store_label(merchant: str, store_number: str, city: str = "") -> str:
    """"Total Wine #903 (Palm Beach Gardens)"; parts that are unknown drop out."""
    name = (merchant or "").strip() or "Store"
    if store_number:
        name = f"{name} #{store_number}"
    if city:
        name = f"{name} ({city})"
    return name


def _recap_ref(recap) -> dict:
    event = getattr(recap, "event", None)
    day = _event_day(event)
    return {
        "recapId": recap.id,
        "recapUuid": str(recap.uuid),
        "tenantId": recap.tenant_id,
        "eventName": (getattr(event, "name", "") or "")[:80],
        "eventDate": day.isoformat() if day else "",
    }


def _recap_phrase(recap) -> str:
    """How an explanation names another recap: its event and date (never a
    person)."""
    ref = _recap_ref(recap)
    name = ref["eventName"] or f"recap {recap.id}"
    day = _day_label(_event_day(recap.event))
    return f"the {name} recap" + (f" from {day}" if day else "")


# ─── What we know about one recap ───────────────────────────────────


class EventStore:
    """The store the event was at, as far as Spark knows."""

    def __init__(self, chain: str = "", store_number: str = "", city: str = "",
                 state_code: str = "", address: str = ""):
        self.chain = chain
        self.store_number = store_number
        self.city = city
        self.state_code = state_code
        self.address = address

    def label(self) -> str:
        return _store_label(self.chain, self.store_number, self.city)


def event_store_of(recap) -> EventStore:
    from recaps.recap_location import chain_in_text, current_values

    try:
        cur = current_values(recap)
    except Exception:  # noqa: BLE001
        cur = None
    event = recap.event
    retailer = getattr(cur, "retailer", "") or ""
    chain, number = chain_in_text(retailer) if retailer else ("", "")
    if not chain or not number:
        e_chain, e_number = chain_in_text(getattr(event, "name", "") or "", builtin_only=True)
        chain = chain or e_chain
        number = number or e_number
    store_number = (getattr(cur, "store_number", "") or "").strip().lstrip("0") or number
    return EventStore(
        chain=chain or retailer,
        store_number=store_number,
        city=getattr(cur, "city", "") or "",
        state_code=getattr(cur, "state_code", "") or "",
        address=getattr(event, "address", "") or "",
    )


def _load_recap(recap_id: int):
    from recaps.models import CustomRecap

    return (
        CustomRecap.objects.select_related(
            "event", "event__retailer", "event__location", "event__state",
            "event__request", "event__request__retailer", "retailer", "location",
            "state", "ambassador", "created_by", "created_by__role", "tenant",
        )
        .filter(id=recap_id)
        .first()
    )


def _readings_for(recap_ids) -> list:
    from recaps.models import ReceiptReading

    return list(
        ReceiptReading.objects.filter(custom_recap_file__custom_recap_id__in=list(recap_ids))
        .select_related("custom_recap_file")
        .order_by("custom_recap_file__created_at", "custom_recap_file_id")
    )


# ─── Rules ──────────────────────────────────────────────────────────


def _flag(kind: str, severity: str, explanation: str, dedupe_key: str = "", **details) -> dict:
    return {
        "kind": kind,
        "severity": severity,
        "explanation": explanation,
        "dedupe_key": dedupe_key[:120],
        "details": details,
    }


def _upload_order(reading) -> tuple:
    f = reading.custom_recap_file
    return (f.created_at or timezone.now(), f.id)


def duplicate_receipt_flags(recap, readings, family: set[int]) -> tuple[list[dict], set[int]]:
    """``(flags, other recap ids)``. Matches every key this recap's receipts
    carry against every other recap's receipts, any tenant."""
    from recaps.models import ReceiptReading

    flags: list[dict] = []
    others: set[int] = set()
    own_store = None
    by_other: dict[int, list[tuple]] = defaultdict(list)
    for r in readings:
        keys = Q()
        has = False
        for field in ("fingerprint", "ticket_key", "loose_key"):
            value = getattr(r, field, "")
            if value:
                keys |= Q(**{field: value})
                has = True
        if not has:
            continue
        twins = (
            ReceiptReading.objects.filter(keys)
            .exclude(custom_recap_file__custom_recap_id__in=family)
            .exclude(custom_recap_file__custom_recap__isnull=True)
            .select_related(
                "custom_recap_file", "custom_recap_file__custom_recap",
                "custom_recap_file__custom_recap__event",
            )
        )
        for t in twins:
            by_other[t.custom_recap_file.custom_recap_id].append((r, t))
    for other_id, pairs in sorted(by_other.items()):
        r, t = pairs[0]
        other = t.custom_recap_file.custom_recap
        others.add(other_id)
        later = _upload_order(r) > _upload_order(t)
        same_fp = bool(r.fingerprint) and r.fingerprint == t.fingerprint
        header_conflict = bool(
            r.store_number and t.store_number and r.store_number != t.store_number
        )
        ticket = r.ticket_number or t.ticket_number
        when = " ".join(
            x for x in (_day_label(_parse_day(r.receipt_date or t.receipt_date)),
                        _time_label(r.purchase_time or t.purchase_time)) if x
        )
        total = r.total if r.total is not None else t.total
        facts = ", ".join(
            x for x in (f"ticket {ticket}" if ticket else "", when,
                        _money(total) if total is not None else "") if x
        )
        their_store = _store_label(t.merchant, t.store_number, t.city)
        if header_conflict:
            what = (
                f"Same ticket as a receipt on {_recap_phrase(other)} ({facts}), "
                f"but the store header differs: this photo says "
                f"#{r.store_number}, that one says #{t.store_number}. One of the "
                "two headers was likely edited."
            )
        elif same_fp:
            what = f"Same receipt as one on {_recap_phrase(other)} ({their_store}, {facts})."
        else:
            what = f"Same ticket as a receipt on {_recap_phrase(other)} ({their_store}, {facts})."
        what += (
            " That copy was uploaded first." if later
            else " This copy was uploaded first; the other recap reused it."
        )
        flags.append(
            _flag(
                "duplicate_receipt",
                "high" if later else "medium",
                what,
                f"recap:{other_id}",
                otherRecap=_recap_ref(other),
                ticket=ticket,
                fileId=r.custom_recap_file_id,
                otherFileId=t.custom_recap_file_id,
                storeNumber=r.store_number,
                otherStoreNumber=t.store_number,
                headerConflict=header_conflict,
                uploadedLater=later,
                matchedOn="fingerprint" if same_fp else (
                    "ticket" if r.ticket_key and r.ticket_key == t.ticket_key else "ticket_date_total"
                ),
            )
        )
        # The later copy of a ticket whose header was changed: the earlier
        # copy shows where it was printed. If that is not this event's store,
        # the purchase did not happen here.
        if header_conflict and later:
            if own_store is None:
                own_store = event_store_of(recap)
            if not own_store.store_number or own_store.store_number != t.store_number:
                flags.append(
                    _flag(
                        "store_mismatch",
                        "high",
                        (
                            f"Ticket {ticket} was printed at {their_store}, per the "
                            f"earlier copy on {_recap_phrase(other)}. This event was "
                            f"at {own_store.label()}."
                        ),
                        f"copy:{other_id}",
                        otherRecap=_recap_ref(other),
                        fileId=r.custom_recap_file_id,
                        receiptStoreNumber=t.store_number,
                        eventStoreNumber=own_store.store_number,
                    )
                )
    return flags, others


def _parse_day(text: str) -> date | None:
    try:
        return date.fromisoformat(text) if text else None
    except ValueError:
        return None


def _norm_city(city: str) -> str:
    return " ".join((city or "").lower().replace(".", " ").split())


def store_mismatch_flags(recap, readings, store: EventStore | None = None) -> list[dict]:
    from recaps.recap_location import chain_in_text, same_street_address

    store = store or event_store_of(recap)
    flags = []
    for r in readings:
        if r.status not in ("read", "unreadable"):
            continue
        receipt_label = _store_label(r.merchant, r.store_number, r.city)
        r_chain = chain_in_text(r.merchant or "")[0] if r.merchant else ""
        e_chain = chain_in_text(store.chain or "")[0] if store.chain else ""
        other_chain = bool(r_chain and e_chain and r_chain != e_chain)
        key = f"file:{r.custom_recap_file_id}"
        detail = {
            "fileId": r.custom_recap_file_id,
            "receiptStoreNumber": r.store_number,
            "eventStoreNumber": store.store_number,
            "receiptCity": r.city,
            "eventCity": store.city,
        }
        if r.address and store.address and same_street_address(r.address, store.address):
            continue
        if r.store_number and store.store_number and r.store_number != store.store_number:
            flags.append(
                _flag(
                    "store_mismatch",
                    "medium" if other_chain else "high",
                    (
                        f"Receipt is from {receipt_label}; this event was at "
                        f"{store.label()}."
                        + (" A different chain: check it was a supply run." if other_chain else "")
                    ),
                    key,
                    **detail,
                )
            )
            continue
        if (
            r.city and store.city and _norm_city(r.city) != _norm_city(store.city)
            and not (r.store_number and r.store_number == store.store_number)
        ):
            flags.append(
                _flag(
                    "store_mismatch",
                    "medium",
                    f"Receipt is from {receipt_label}, but this event was in {store.city}.",
                    key,
                    **detail,
                )
            )
    return flags


def receipt_math_flags(readings) -> list[dict]:
    flags = []
    for r in readings:
        if r.arithmetic_ok is False and r.status == "read":
            label = f"ticket {r.ticket_number}" if r.ticket_number else "a receipt"
            flags.append(
                _flag(
                    "receipt_math",
                    "low",
                    f"The total on {label} doesn't add up: {r.arithmetic_note or 'check the lines'}.",
                    f"file:{r.custom_recap_file_id}",
                    fileId=r.custom_recap_file_id,
                    note=r.arithmetic_note,
                )
            )
    return flags


def multiple_receipt_flags(readings) -> list[dict]:
    from recaps.receipt_reader import parse_receipt_count

    flags = []
    for r in readings:
        count = parse_receipt_count(r.raw if isinstance(r.raw, dict) else None)
        if count < 2:
            continue
        flags.append(
            _flag(
                "multiple_receipts",
                "low",
                f"One photo shows {count} receipts, but only one was read. "
                "Crop or re-upload the others so their spend is counted.",
                f"file:{r.custom_recap_file_id}",
                fileId=r.custom_recap_file_id,
                receiptCount=count,
            )
        )
    return flags


def recap_activity(recap) -> list[str]:
    """What the recap reports doing: "120 engagements", "Consumers sampled
    40". Empty when it reports nothing."""
    from recaps.models import (
        CustomFieldValue,
        CustomRecapProductSample,
        CustomRecapSalePerformance,
    )
    from recaps.numbers import parse_count

    out: list[str] = []
    if (recap.total_engagements or 0) > 0:
        out.append(f"{recap.total_engagements} engagements")
    values = CustomFieldValue.objects.filter(custom_recap_id=recap.id).select_related("custom_field")
    for row in values:
        name = (getattr(row.custom_field, "name", "") or "").strip()
        lowered = name.lower()
        if not any(w in lowered for w in _ACTIVITY_FIELD_WORDS):
            continue
        if "$" in (row.value or "") or "spend" in lowered or "price" in lowered:
            continue
        count = parse_count(row.value)
        if count and count > 0:
            out.append(f"{name[:40]} {count}")
        if len(out) >= 3:
            break
    if not out:
        samples = sum(
            CustomRecapProductSample.objects.filter(custom_recap_id=recap.id).values_list(
                "quantity", flat=True
            )
        )
        if samples > 0:
            out.append(f"{samples} samples")
        elif CustomRecapSalePerformance.objects.filter(custom_recap_id=recap.id).exists():
            out.append("sales")
    return out


def short_clock_flags(recap) -> list[dict]:
    from ambassadors.models import Attendance

    if not recap.ambassador_id or not recap.event_id:
        return []
    rows = list(
        Attendance.objects.filter(ambassador_id=recap.ambassador_id, event_id=recap.event_id)
        .select_related("source")
        .order_by("clock_time")
    )
    ins = [a.clock_time for a in rows if getattr(a.source, "name", "") == "clock_in" and a.clock_time]
    outs = [a.clock_time for a in rows if getattr(a.source, "name", "") == "clock_out" and a.clock_time]
    if not ins or not outs or outs[-1] < ins[0]:
        return []
    seconds = (outs[-1] - ins[0]).total_seconds()
    minutes = _setting_int("RECEIPT_FLAG_SHORT_CLOCK_MINUTES", DEFAULT_SHORT_CLOCK_MINUTES)
    if seconds >= minutes * 60:
        return []
    activity = recap_activity(recap)
    if not activity:
        return []
    return [
        _flag(
            "short_clock",
            "high",
            (
                f"On the clock for {_duration_label(seconds)} (first clock-in to "
                f"last clock-out), but the recap reports {', '.join(activity)}."
            ),
            "",
            seconds=int(seconds),
            thresholdMinutes=minutes,
            activity=activity,
        )
    ]


def _is_staff_user(user) -> bool:
    if user is None:
        return False
    from utils.graphql.permissions import _is_admin_access

    role = getattr(user, "role", None)
    try:
        return _is_admin_access(
            getattr(role, "slug", None), bool(user.is_staff), bool(user.is_superuser),
            getattr(user, "email", ""),
        )
    except Exception:  # noqa: BLE001
        return False


def late_staff_filing_flags(recap) -> list[dict]:
    day = _event_day(recap.event)
    if day is None or recap.created_at is None:
        return []
    days_late = (recap.created_at.date() - day).days
    threshold = _setting_int("RECEIPT_FLAG_LATE_FILING_DAYS", DEFAULT_LATE_FILING_DAYS)
    if days_late < threshold:
        return []
    ba_user_id = getattr(recap.ambassador, "user_id", None) if recap.ambassador_id else None
    by_staff = bool(recap.filling_for_ambassador) or (
        _is_staff_user(recap.created_by) and recap.created_by_id != ba_user_id
    )
    if not by_staff:
        return []
    return [
        _flag(
            "late_staff_filing",
            "medium",
            (
                f"Filed {days_late} days after the {_day_label(day)} event, by a "
                "staff account on the ambassador's behalf."
            ),
            "",
            daysLate=days_late,
            thresholdDays=threshold,
            filledForAmbassador=bool(recap.filling_for_ambassador),
            filedByUserId=recap.created_by_id,
        )
    ]


def _file_dhash(f) -> str:
    for rel in ("photo_score", "library_tag", "receipt_reading"):
        try:
            obj = getattr(f, rel)
        except Exception:  # noqa: BLE001
            continue
        value = getattr(obj, "image_dhash" if rel == "receipt_reading" else "dhash", "") or ""
        if value:
            return value
    return ""


def _related_names() -> list[str]:
    from recaps.models import CustomRecapFile

    names = []
    for rel in ("photo_score", "library_tag", "receipt_reading"):
        try:
            CustomRecapFile._meta.get_field(rel)
            names.append(rel)
        except Exception:  # noqa: BLE001
            pass
    return names


def duplicate_photo_flags(recap, family: set[int], skip_other_receipts: set[int]) -> list[dict]:
    from recaps.models import CustomRecapFile
    from recaps.photo_picks import hash_distance
    from recaps.photo_reuse import normalize_content_sha256

    rels = _related_names()
    files = list(
        CustomRecapFile.objects.filter(custom_recap_id=recap.id)
        .select_related("file_recap_category", *rels)
        .order_by("created_at", "id")
    )
    if not files:
        return []
    flags: list[dict] = []
    from recaps.receipt_reader import RECEIPT_CATEGORY_RE

    def is_receipt(f) -> bool:
        cat = getattr(f.file_recap_category, "name", "") if f.file_recap_category_id else ""
        return bool(RECEIPT_CATEGORY_RE.search(cat or ""))

    info = []
    for f in files:
        info.append(
            {
                "file": f,
                "sha": normalize_content_sha256(f.content_sha256),
                "path": str(getattr(f.url, "name", f.url) or ""),
                "dhash": _file_dhash(f),
                "receipt": is_receipt(f),
            }
        )

    # Within this recap: the same photo attached more than once.
    groups: list[list[dict]] = []
    for item in info:
        for group in groups:
            head = group[0]
            if (
                (item["sha"] and item["sha"] == head["sha"])
                or (item["path"] and item["path"] == head["path"])
                or (item["dhash"] and head["dhash"]
                    and hash_distance(item["dhash"], head["dhash"]) <= NEAR_PHOTO_BITS)
            ):
                group.append(item)
                break
        else:
            groups.append([item])
    for group in groups:
        if len(group) < 2:
            continue
        flags.append(
            _flag(
                "duplicate_photo",
                "low",
                f"The same photo is attached {len(group)} times in this recap.",
                f"within:{group[0]['file'].id}",
                fileIds=[g["file"].id for g in group],
                within=True,
            )
        )

    # Another recap: exact copies (any tenant), then near-identical ones
    # (same tenant, events within the window).
    matches: dict[int, dict] = {}

    def note(other_recap, own_file, other_file, how: str) -> None:
        slot = matches.setdefault(
            other_recap.id, {"recap": other_recap, "pairs": [], "how": set()}
        )
        slot["pairs"].append((own_file, other_file))
        slot["how"].add(how)

    for item in info:
        if not item["sha"]:
            continue
        for other in (
            CustomRecapFile.objects.filter(content_sha256=item["sha"])
            .exclude(custom_recap_id__in=family)
            .exclude(custom_recap__isnull=True)
            .select_related("custom_recap", "custom_recap__event")[:20]
        ):
            if item["receipt"] and other.custom_recap_id in skip_other_receipts:
                continue
            note(other.custom_recap, item["file"], other, "exact")

    # Near-identical matching skips receipts: two different receipts photographed
    # the same way can hash alike, and a reused receipt is caught far more
    # surely by its ticket number (duplicate_receipt).
    with_hash = [i for i in info if i["dhash"] and not i["receipt"]]
    if with_hash:
        day = _event_day(recap.event)
        window = Q(custom_recap__tenant_id=recap.tenant_id)
        if day:
            lo, hi = day - timedelta(days=NEAR_PHOTO_WINDOW_DAYS), day + timedelta(days=NEAR_PHOTO_WINDOW_DAYS)
            window &= (
                Q(custom_recap__event__date__date__range=(lo, hi))
                | Q(custom_recap__event__date__isnull=True)
            )
        # Scan light rows (id + hashes), then load only the matches.
        near_rels = [r for r in rels if r != "receipt_reading"]
        hashed = Q()
        hash_fields = []
        for rel in near_rels:
            hashed |= Q(**{f"{rel}__dhash__gt": ""})
            hash_fields.append(f"{rel}__dhash")
        matched: dict[int, list[dict]] = defaultdict(list)
        if hash_fields:
            rows = (
                CustomRecapFile.objects.filter(window, hashed)
                .exclude(custom_recap_id__in=family)
                .exclude(custom_recap__isnull=True)
                .exclude(file_recap_category__name__iregex=RECEIPT_CATEGORY_RE.pattern)
                .values_list("id", *hash_fields)[:20000]
            )
            for row in rows:
                hashes = [h for h in row[1:] if h]
                for item in with_hash:
                    if any(hash_distance(item["dhash"], h) <= NEAR_PHOTO_BITS for h in hashes):
                        matched[row[0]].append(item)
        if matched:
            for other in CustomRecapFile.objects.filter(id__in=list(matched)).select_related(
                "custom_recap", "custom_recap__event"
            ):
                for item in matched[other.id]:
                    note(other.custom_recap, item["file"], other, "near")

    for other_id, slot in sorted(matches.items()):
        other = slot["recap"]
        pairs = slot["pairs"]
        own_ids = sorted({p[0].id for p in pairs})
        later = any(
            (p[0].created_at, p[0].id) > (p[1].created_at, p[1].id) for p in pairs
        )
        exact = "exact" in slot["how"]
        count = len(own_ids)
        noun = "photo" if count == 1 else f"{count} photos"
        verb = "is the same file as" if exact else "is nearly identical to"
        if count > 1:
            verb = "are the same files as" if exact else "are nearly identical to"
        flags.append(
            _flag(
                "duplicate_photo",
                "medium" if later else "low",
                (
                    f"{noun[0].upper() + noun[1:]} on this recap {verb} one on "
                    f"{_recap_phrase(other)}."
                    + (" That one was uploaded first." if later else " This recap's copy came first.")
                ),
                f"recap:{other_id}",
                otherRecap=_recap_ref(other),
                fileIds=own_ids,
                otherFileIds=sorted({p[1].id for p in pairs}),
                exact=exact,
                uploadedLater=later,
            )
        )
    return flags


# ─── Evaluate + store ───────────────────────────────────────────────


def family_ids(recap) -> set[int]:
    from recaps.photo_reuse import _family_ids

    try:
        return _family_ids(recap)
    except Exception:  # noqa: BLE001
        return {recap.id}


def compute_recap_flags(recap_id: int) -> tuple[list[dict], set[int]]:
    """``(flags, related recap ids)`` for one custom recap. Reads only."""
    recap = _load_recap(recap_id)
    if recap is None:
        return [], set()
    family = family_ids(recap)
    readings = [
        r for r in _readings_for({recap.id})
        if r.status in ("read", "unreadable")
    ]
    flags: list[dict] = []
    dup, others = duplicate_receipt_flags(recap, readings, family)
    flags.extend(dup)
    copy_flagged = {f["details"].get("fileId") for f in dup if f["kind"] == "store_mismatch"}
    store = event_store_of(recap)
    flags.extend(
        f for f in store_mismatch_flags(recap, readings, store)
        if f["details"].get("fileId") not in copy_flagged
    )
    flags.extend(receipt_math_flags(readings))
    flags.extend(multiple_receipt_flags(readings))
    flags.extend(short_clock_flags(recap))
    flags.extend(late_staff_filing_flags(recap))
    flags.extend(duplicate_photo_flags(recap, family, skip_other_receipts=others))
    return flags, others


def apply_recap_flags(recap_id: int, tenant_id: int, flags: list[dict]) -> dict:
    """Upsert ``flags`` for the recap and close the open ones that no longer
    apply. A flag a person resolved stays resolved."""
    from recaps.models import RecapFlag

    counts = {"created": 0, "updated": 0, "reopened": 0, "cleared": 0}
    now = timezone.now()
    with transaction.atomic():
        existing = {
            (row.kind, row.dedupe_key): row
            for row in RecapFlag.objects.select_for_update().filter(custom_recap_id=recap_id)
        }
        seen = set()
        for f in flags:
            key = (f["kind"], f["dedupe_key"])
            if key in seen:
                continue
            seen.add(key)
            row = existing.get(key)
            if row is None:
                RecapFlag.objects.create(
                    tenant_id=tenant_id, custom_recap_id=recap_id, kind=f["kind"],
                    severity=f["severity"], explanation=f["explanation"],
                    details=f["details"], dedupe_key=f["dedupe_key"],
                )
                counts["created"] += 1
                continue
            auto_closed = row.resolved_at is not None and row.resolved_by_id is None and (
                row.resolution_note == RecapFlag.AUTO_CLEARED_NOTE
            )
            changed = (
                row.severity != f["severity"] or row.explanation != f["explanation"]
                or row.details != f["details"]
            )
            if auto_closed:
                row.resolved_at = None
                row.resolution_note = ""
                counts["reopened"] += 1
            elif row.resolved_at is not None:
                continue  # a person resolved it: leave it alone
            elif changed:
                counts["updated"] += 1
            else:
                continue
            row.severity, row.explanation, row.details = f["severity"], f["explanation"], f["details"]
            row.save()
        for key, row in existing.items():
            if key in seen or row.resolved_at is not None:
                continue
            row.resolved_at = now
            row.resolved_by = None
            row.resolution_note = RecapFlag.AUTO_CLEARED_NOTE
            row.save(update_fields=["resolved_at", "resolved_by", "resolution_note", "updated_at"])
            counts["cleared"] += 1
    return counts


def evaluate_recap(recap_id: int, *, dry_run: bool = False, follow: bool = True) -> dict:
    """Check one recap and store its flags. ``follow`` re-checks the other
    recaps a duplicate points at, so both sides carry the flag."""
    from recaps.models import CustomRecap

    tenant_id = CustomRecap.objects.filter(id=recap_id).values_list("tenant_id", flat=True).first()
    if tenant_id is None:
        return {"flags": 0}
    flags, others = compute_recap_flags(recap_id)
    out = {"flags": len(flags), "kinds": sorted({f["kind"] for f in flags})}
    if dry_run:
        out["preview"] = [
            {"kind": f["kind"], "severity": f["severity"], "explanation": f["explanation"]}
            for f in flags
        ]
        return out
    out.update(apply_recap_flags(recap_id, tenant_id, flags))
    if follow:
        for other_id in sorted(others):
            try:
                evaluate_recap(other_id, follow=False)
            except Exception:  # noqa: BLE001
                logger.exception("receipt flags: follow-up check failed recap=%s", other_id)
    return out


def check_recaps(
    *,
    tenant_id: int | None = None,
    recap_ids: list[int] | None = None,
    since_hours: int = 2,
    date_from: date | None = None,
    date_to: date | None = None,
    limit: int = 300,
    dry_run: bool = False,
) -> dict:
    """Check a batch of recaps: the given ids, plus recaps filed or updated
    in the last ``since_hours`` hours (or whose event falls in the date
    range, for a backfill)."""
    from recaps.models import CustomRecap
    from recaps.receipt_reader import _event_window_q

    ids: list[int] = list(dict.fromkeys(int(i) for i in (recap_ids or [])))
    qs = CustomRecap.objects.all()
    if tenant_id:
        qs = qs.filter(tenant_id=tenant_id)
    if date_from or date_to:
        qs = qs.filter(_event_window_q("", date_from, date_to))
    else:
        since = timezone.now() - timedelta(hours=max(1, since_hours))
        qs = qs.filter(Q(created_at__gte=since) | Q(updated_at__gte=since))
    for rid in qs.order_by("-id").values_list("id", flat=True)[: max(1, limit)]:
        if rid not in ids:
            ids.append(rid)
    totals = {"recaps": 0, "flags": 0, "created": 0, "updated": 0, "reopened": 0,
              "cleared": 0, "errors": 0, "dry_run": dry_run}
    kinds: dict[str, int] = defaultdict(int)
    preview = []
    for rid in ids[: max(1, limit)]:
        try:
            res = evaluate_recap(rid, dry_run=dry_run)
        except Exception:  # noqa: BLE001
            logger.exception("receipt flags: check failed recap=%s", rid)
            totals["errors"] += 1
            continue
        totals["recaps"] += 1
        totals["flags"] += res.get("flags", 0)
        for k in ("created", "updated", "reopened", "cleared"):
            totals[k] += res.get(k, 0)
        for kind in res.get("kinds", []):
            kinds[kind] += 1
        if dry_run and res.get("preview") and len(preview) < 50:
            preview.append({"recapId": rid, "flags": res["preview"]})
    totals["recaps_by_kind"] = dict(kinds)
    if dry_run:
        totals["preview"] = preview
    return totals


def check_recap_quietly(custom_recap_id: int) -> None:
    """For the approve path: queue the recap's receipt photos for reading
    and check the flags that need no reading. Never raises."""
    try:
        from recaps.receipt_reader import queue_recap_receipts

        queue_recap_receipts(custom_recap_id)
    except Exception:  # noqa: BLE001
        logger.exception("receipt queue failed recap=%s", custom_recap_id)
    if not flags_enabled():
        return
    try:
        evaluate_recap(custom_recap_id)
    except Exception:  # noqa: BLE001
        logger.exception("receipt flags failed recap=%s", custom_recap_id)
