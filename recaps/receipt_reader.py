"""Read the total off receipt photos so it can be checked against the spend
an ambassador typed into the recap.

The existing review check only notices spend with NO receipt photo. It
cannot tell a $300 entry apart from $138 of receipts. This module reads each
receipt image once, stores what it found (``ReceiptReading``), and the
review check compares the sum to the entered spend.

Design notes:

* Reading is a background job (``read_recap_receipts``), never done inline
  in a page load — each image is a network call to the vision model.
* One reading per file, forever: a receipt photo does not change. A file
  that could not be read is stored as ``unreadable`` so it is not retried
  on every run (and so the check can say "couldn't read 1 of 3").
* The model is asked ONLY for the printed total, merchant and date. Card
  numbers and people are explicitly out of scope and nothing else about the
  image is stored.
* Fails closed on doubt: when the model is not confident of the total it is
  recorded as unreadable rather than guessed. A wrong "read" total would
  raise a false flag against an ambassador.

Same vision path as ``recaps.photo_check`` (``utils.ai_text``); without an
API key every file is simply left unread.

Reader v3 (Nov 2026) reads the whole ticket in the same single call: store
number and address, ticket / register / time, every line item, subtotal,
fees, tax, and the card's last four digits when printed (never more). It
checks the arithmetic (items + fees + tax == total within 2 cents) and
stores three duplicate keys so ``recaps.receipt_flags`` can spot the same
ticket on two recaps, even when the store header was edited. A daily cap
(``RECEIPT_READER_DAILY_MAX_READS``) bounds the cost.
"""

from __future__ import annotations

import hashlib
import logging
import re
from datetime import date, datetime, time, timedelta
from datetime import timezone as dt_timezone
from decimal import Decimal, InvalidOperation

from django.conf import settings
from django.utils import timezone

from utils.ai_text import AiUnavailable, generate_vision_json

logger = logging.getLogger(__name__)

RECEIPT_CATEGORY_RE = re.compile(r"receipt|product\s*spend|expense", re.IGNORECASE)
_IMAGE_RE = re.compile(r"\.(jpe?g|png|webp|gif|heic|heif)$", re.IGNORECASE)

# Bumped when the reader starts capturing more. Rows written by an older
# reader can be re-read once (``reread_older``) to fill the new fields.
READER_VERSION = 3

# A photo whose download or model call failed is tried this many times.
MAX_ATTEMPTS = 3
# Model calls per UTC day, across all tenants, unless settings say otherwise.
DEFAULT_DAILY_MAX_READS = 300
# Receipts are long and the print is small: read them larger than the PDF
# downscale default.
READ_MAX_DIM = 1800
# Lines + fees + tax may differ from the printed total by this much.
ARITHMETIC_TOLERANCE = Decimal("0.02")

RECEIPT_SYSTEM = (
    "You read retail purchase receipts and card-activity screenshots (a "
    "banking or card app listing charges). Report only what is printed in the "
    "image. If it shows more than one paper receipt, read the one most fully "
    "in frame. Never report card or account numbers, or anything about people. "
    "If you cannot clearly read the amounts, say so."
)
RECEIPT_USER = (
    "Read this image. Give the final total paid (after tax), the store name, "
    "the store number if one is printed (for example 614 from 'H-E-B #614' or "
    "'STORE 614'; never a register, transaction or phone number), and the "
    "purchase date if printed. List every money line separately in `lines`: "
    "a paper receipt is usually one charge; a refund or return receipt is a "
    "refund; a card-activity screenshot lists each charge or refund shown. "
    "If this is neither a receipt nor card activity, or the amounts are not "
    "legible, set isReceipt or totalLegible to false."
)
_RECEIPT_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "isReceipt", "totalLegible", "total", "merchant", "storeNumber", "date", "lines",
    ],
    "properties": {
        # True for a receipt OR a card-activity screenshot.
        "isReceipt": {"type": "boolean"},
        "totalLegible": {"type": "boolean"},
        # Final amount paid as a plain decimal string, e.g. "38.11".
        "total": {"type": "string"},
        "merchant": {"type": "string"},
        # Digits only, "" when no store number is printed.
        "storeNumber": {"type": "string"},
        # ISO date if printed, else "".
        "date": {"type": "string"},
        "lines": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["amount", "kind"],
                "properties": {
                    "amount": {"type": "string"},
                    "kind": {"type": "string", "enum": ["charge", "refund"]},
                },
            },
        },
    },
}

RECEIPT_SYSTEM_V3 = (
    "You read retail purchase receipts and card-activity screenshots (a "
    "banking or card app listing charges) for a field-marketing agency that "
    "checks its own expense receipts. Report only what is printed in the "
    "image; use an empty string for anything not printed or not legible. If "
    "the image shows more than one paper receipt, read the one most fully in "
    "frame, and count every separate paper receipt you can see in "
    "receiptCount. Never report a full card or account number: only the last four "
    "digits of a card when they are printed. Never report cashier names or "
    "anything else about people. If you cannot clearly read the amounts, say so."
)
RECEIPT_USER_V3 = (
    "Read this image. Give the final total paid (after tax): the balance "
    "or amount charged (for example BALANCE, AMOUNT DUE, or the card / cash "
    "tender line), never a savings, coupon or rewards figure such as TOTAL "
    "SAVINGS, TOTAL COUPONS or Annual Card Savings. Give the store name, "
    "the store number if one is printed (for example 614 from 'H-E-B #614', "
    "'Store: 903' or '903-Total Wine'; never a register, ticket or phone "
    "number), the store's street address, city and 2-letter state if "
    "printed, the ticket / transaction / receipt number, the register "
    "number, the purchase date (YYYY-MM-DD; a two-digit year such as "
    "11/24/26 means 2026) and time (24-hour HH:MM).\n"
    "List every purchased item in `items` with its name as printed, "
    "quantity, unit price and line amount (a discount or coupon is a "
    "negative amount, including store savings and loyalty-card lines such as "
    "'KROGER SAVINGS 0.29' printed without a minus sign). Put bag fees, bottle deposits, CRV and similar fees "
    "in `fees` (their sum), not in items. Give the printed subtotal and "
    "sales tax. Set isBrandProduct on an item when it is one of the brand's "
    "products listed below. Give cardLast4 only if exactly four card digits "
    "are printed. List every money line separately in `lines`: a paper "
    "receipt is usually one charge; a refund or return receipt is a refund; "
    "a card-activity screenshot lists each charge or refund shown. Set "
    "totalLegible false when the total is cut off or not in this photo (for "
    "example the top half of a long receipt). Give your confidence from 0 to "
    "1 that the total, store number and ticket number are read correctly. "
    "If this is neither a receipt nor card activity, set isReceipt false. "
    "Set receiptCount to how many separate paper receipts appear in the "
    "photo, even partly (1 for a single receipt or a card-activity "
    "screenshot)."
)
_STR = {"type": "string"}
_RECEIPT_SCHEMA_V3 = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "isReceipt", "totalLegible", "total", "merchant", "storeNumber", "date",
        "lines", "address", "city", "state", "ticketNumber", "register", "time",
        "items", "subtotal", "fees", "tax", "cardLast4", "confidence",
        "receiptCount",
    ],
    "properties": {
        **_RECEIPT_SCHEMA["properties"],
        "address": _STR,
        "city": _STR,
        "state": _STR,
        "ticketNumber": _STR,
        "register": _STR,
        "time": _STR,
        "items": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["name", "qty", "unitPrice", "amount", "isBrandProduct"],
                "properties": {
                    "name": _STR,
                    "qty": _STR,
                    "unitPrice": _STR,
                    "amount": _STR,
                    "isBrandProduct": {"type": "boolean"},
                },
            },
        },
        "subtotal": _STR,
        "fees": _STR,
        "tax": _STR,
        "cardLast4": _STR,
        "confidence": {"type": "number"},
        "receiptCount": {"type": "integer"},
    },
}

# A single field-marketing supply run above this is far more likely a
# misread (a barcode, a phone number) than a real purchase.
MAX_PLAUSIBLE_TOTAL = Decimal("5000")
_CENT = Decimal("0.01")


def parse_total(raw) -> Decimal | None:
    """A printed total → Decimal, or None when it is not a plausible amount."""
    text = str(raw if raw is not None else "").replace(",", "").replace("$", "").strip()
    if text.startswith("-"):
        text = text[1:].strip()
    if not re.fullmatch(r"\d{1,5}(\.\d{1,2})?", text):
        return None
    try:
        value = Decimal(text).quantize(_CENT)
    except InvalidOperation:
        return None
    if value <= 0 or value > MAX_PLAUSIBLE_TOTAL:
        return None
    return value


def parse_store_number(raw) -> str:
    """'#614' / '0614' / 'Store 614' → '614'; anything else → ''."""
    digits = re.sub(r"\D", "", str(raw or ""))
    if not digits or len(digits) > 5:
        return ""
    return digits.lstrip("0") or ""


def parse_lines(raw) -> list[Decimal] | None:
    """Model ``lines`` → signed amounts (refunds negative). ``None`` when any
    line is not a plausible amount, so a half-read screenshot is not trusted."""
    if not isinstance(raw, list):
        return None
    out: list[Decimal] = []
    for line in raw:
        if not isinstance(line, dict):
            return None
        amount = parse_total(line.get("amount"))
        if amount is None:
            return None
        out.append(-amount if line.get("kind") == "refund" else amount)
    return out


def interpret_reading(parsed: dict | None) -> dict:
    """Model output → ``{"status", "total", "merchant", "receipt_date",
    "store_number", "charges"}``.

    ``charges`` are signed decimal strings (refunds negative) whose sum is
    ``total``. When the listed lines do not add up to the printed total the
    lines are dropped and the total stands as one charge — the total is the
    figure printed most prominently, so it is the one trusted. Anything
    short of a confident, plausible total is ``unreadable``."""
    unreadable = {
        "status": "unreadable", "total": None, "merchant": "", "receipt_date": "",
        "store_number": "", "charges": [],
    }
    if not isinstance(parsed, dict):
        return unreadable
    if parsed.get("isReceipt") is not True or parsed.get("totalLegible") is not True:
        return unreadable
    lines = parse_lines(parsed.get("lines")) or []
    printed = parse_total(parsed.get("total"))
    net = sum(lines, Decimal("0")).quantize(_CENT) if lines else None
    if printed is not None and net is not None and abs(net) != printed:
        lines, net = [], None  # lines disagree with the total: trust the total
    if printed is None and net is None:
        return unreadable
    if net is not None:
        total = net  # signed: a refund-only receipt nets negative
    else:
        raw_total = str(parsed.get("total") or "").strip()
        total = -printed if raw_total.startswith("-") else printed
        lines = [total]
    if total == 0 and not lines:
        return unreadable
    date_text = str(parsed.get("date") or "").strip()
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", date_text):
        date_text = ""
    return {
        "status": "read",
        "total": total,
        "merchant": str(parsed.get("merchant") or "").strip()[:120],
        "receipt_date": date_text,
        "store_number": parse_store_number(parsed.get("storeNumber")),
        "charges": [str(x) for x in lines],
    }


# ─── Reader v3: the whole ticket ─────────────────────────────────────

_SIGNED_MONEY_RE = re.compile(r"^\(?-?\$?\s*-?\d{1,5}(?:\.\d{1,2})?\)?-?$")
_CARD_RUN_RE = re.compile(r"(?<!\d)(?:\d[ -]?){12,18}\d(?!\d)")
_TIME_RE = re.compile(r"^(\d{1,2}):(\d{2})(?::\d{2})?\s*([ap]\.?m\.?)?$", re.IGNORECASE)
_STATE_RE = re.compile(r"^[A-Za-z]{2}$")

# Words in a brand's name that say nothing about the brand on a receipt line.
_GENERIC_BRAND_WORDS = frozenset(
    """
    the and of co company inc llc ltd corp brand brands drink drinks beverage
    beverages bev energy water waters soda sodas seltzer seltzers sparkling
    coffee tea teas juice juices beer beers brewing brewery wine wines winery
    spirits vodka whiskey tequila rum gin cider kombucha foods food snack
    snacks bar bars farms farm kitchen bakery bread organic natural original
    premium craft hard
    """.split()
)


def parse_signed_money(raw) -> Decimal | None:
    """"-1.50" / "(1.50)" / "1.50-" / "$3.49" → Decimal (sign kept); "" → None."""
    text = str(raw if raw is not None else "").replace(",", "").strip()
    if not text or not _SIGNED_MONEY_RE.fullmatch(text):
        return None
    negative = text.startswith("(") or "-" in text
    digits = re.sub(r"[^\d.]", "", text)
    try:
        value = Decimal(digits).quantize(_CENT)
    except InvalidOperation:
        return None
    if value > MAX_PLAUSIBLE_TOTAL:
        return None
    return -value if negative else value


def _luhn_ok(digits: str) -> bool:
    total, alt = 0, False
    for ch in reversed(digits):
        d = int(ch)
        if alt:
            d *= 2
            if d > 9:
                d -= 9
        total += d
        alt = not alt
    return total % 10 == 0


def scrub_card_numbers(value):
    """``value`` with any card-number-like digit run (13–19 digits passing
    the Luhn check) replaced by "[card]". Recurses through dicts and lists.
    A product barcode (12 digits) is left alone."""
    if isinstance(value, dict):
        return {k: scrub_card_numbers(v) for k, v in value.items()}
    if isinstance(value, list):
        return [scrub_card_numbers(v) for v in value]
    if isinstance(value, str):
        def _sub(match):
            digits = re.sub(r"\D", "", match.group(0))
            if 13 <= len(digits) <= 19 and _luhn_ok(digits):
                return "[card]"
            return match.group(0)

        return _CARD_RUN_RE.sub(_sub, value)
    return value


def parse_card_last4(raw) -> str:
    """Exactly four digits, else "" — a longer number is never kept."""
    text = re.sub(r"[\s*xX•.#-]", "", str(raw or ""))
    return text if re.fullmatch(r"\d{4}", text) else ""


def parse_time(raw) -> str:
    """"2:07 PM" / "14:07:39" → "14:07"; anything else → ""."""
    m = _TIME_RE.match(str(raw or "").strip())
    if not m:
        return ""
    hour, minute, ampm = int(m.group(1)), int(m.group(2)), (m.group(3) or "").lower()
    if ampm.startswith("p") and hour < 12:
        hour += 12
    elif ampm.startswith("a") and hour == 12:
        hour = 0
    if hour > 23 or minute > 59:
        return ""
    return f"{hour:02d}:{minute:02d}"


def normalize_ticket(raw) -> str:
    """Ticket / transaction number → upper-case letters and digits, leading
    zeros dropped. "" when it is too short or too long to be one."""
    text = re.sub(r"[^0-9A-Za-z]", "", str(raw or "")).upper().lstrip("0")
    return text if 3 <= len(text) <= 32 else ""


def _merchant_key(merchant: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (merchant or "").lower())[:24]


def _sha(*parts: str) -> str:
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()


def receipt_keys(
    *, store_number: str, merchant: str, ticket: str, receipt_date: str, total
) -> dict:
    """The three duplicate keys for one reading (sha256 hex, "" when the
    reading lacks what the key needs).

    * ``fingerprint`` — store + ticket + date + total: the same receipt.
    * ``ticket_key`` — store + ticket + date: the same receipt photographed
      in two halves (one half may have no total).
    * ``loose_key`` — ticket + date + total, ignoring the store: catches a
      copy whose store header was edited.

    The store is the printed store number, or the merchant name when no
    number is printed.
    """
    ticket = normalize_ticket(ticket)
    store = parse_store_number(store_number) or (
        "m:" + _merchant_key(merchant) if _merchant_key(merchant) else ""
    )
    day = receipt_date if re.fullmatch(r"\d{4}-\d{2}-\d{2}", receipt_date or "") else ""
    amount = ""
    if total is not None:
        try:
            amount = str(abs(Decimal(str(total))).quantize(_CENT))
        except InvalidOperation:
            amount = ""
    keys = {"fingerprint": "", "ticket_key": "", "loose_key": ""}
    if not ticket or not day:
        return keys
    if store and amount:
        keys["fingerprint"] = _sha("fp", store, ticket, day, amount)
    if store:
        keys["ticket_key"] = _sha("tk", store, ticket, day)
    if amount:
        keys["loose_key"] = _sha("lk", ticket, day, amount)
    return keys


def brand_tokens(*names: str) -> set[str]:
    """Distinctive words of a brand's name ("Pivot Energy Drink" → {"pivot"}),
    for spotting the brand's own products on a receipt line."""
    out: set[str] = set()
    for name in names:
        for word in re.findall(r"[a-z0-9]+", (name or "").lower()):
            if len(word) >= 3 and word not in _GENERIC_BRAND_WORDS and not word.isdigit():
                out.add(word)
    return out


def is_brand_line(name: str, tokens: set[str], model_says: bool = False) -> bool:
    """A receipt line is the brand's product when it names a distinctive
    word of the brand, or the model matched it to the brand's product list."""
    words = set(re.findall(r"[a-z0-9]+", (name or "").lower()))
    return bool(words & tokens) or bool(model_says)


def check_arithmetic(
    items: list[Decimal], fees: Decimal | None, tax: Decimal | None,
    subtotal: Decimal | None, total: Decimal | None,
) -> tuple[bool | None, str]:
    """Do the printed numbers add up? ``(ok, note)``; ``ok`` is None when
    there is nothing to check, or when the photo plainly shows only part of
    the item lines (the printed subtotal is larger than the lines in view,
    and subtotal + tax still equals the total)."""
    if total is None or (not items and subtotal is None):
        return None, ""
    paid = abs(total)
    lines = sum(items, Decimal("0"))
    fee = fees or Decimal("0")
    tx = tax or Decimal("0")
    tol = ARITHMETIC_TOLERANCE
    if subtotal is not None:
        if min(abs(subtotal + fee + tx - paid), abs(subtotal + tx - paid)) > tol:
            return False, (
                f"Subtotal ${subtotal:,.2f} + fees ${fee:,.2f} + tax ${tx:,.2f} "
                f"does not equal the printed total ${paid:,.2f}"
            )
        if not items:
            return True, ""
        if min(abs(subtotal - lines), abs(subtotal - (lines + fee))) <= tol:
            return True, ""
        if lines < subtotal:
            return None, "Some item lines are not in this photo"
        return False, (
            f"Items add up to ${lines:,.2f}, more than the printed subtotal "
            f"${subtotal:,.2f}"
        )
    expected = (lines + fee + tx).quantize(_CENT)
    if abs(expected - paid) <= tol:
        return True, ""
    return False, (
        f"Items ${lines:,.2f} + fees ${fee:,.2f} + tax ${tx:,.2f} = "
        f"${expected:,.2f}, but the printed total is ${paid:,.2f} "
        f"(off by ${abs(paid - expected):,.2f})"
    )


def parse_receipt_count(parsed: dict | None) -> int:
    """How many separate paper receipts the model saw in the photo (≥ 1).

    Readings stored before the model was asked report nothing and count as 1."""
    if not isinstance(parsed, dict):
        return 1
    raw = parsed.get("receiptCount")
    if isinstance(raw, bool):
        return 1
    try:
        count = int(raw)
    except (TypeError, ValueError):
        return 1
    return max(1, min(count, 20))


def interpret_detail(parsed: dict | None, tokens: set[str] | None = None) -> dict:
    """Model output → the v3 fields (address, ticket, items, fees, tax,
    card last four, confidence, arithmetic, duplicate keys). Pure."""
    tokens = tokens or set()
    empty = {
        "address": "", "city": "", "state_code": "", "ticket_number": "",
        "register": "", "purchase_time": "", "line_items": [], "subtotal": None,
        "fees": None, "tax": None, "card_last4": "", "confidence": None,
        "arithmetic_ok": None, "arithmetic_note": "",
        "fingerprint": "", "ticket_key": "", "loose_key": "", "receipt_count": 1,
    }
    if not isinstance(parsed, dict) or parsed.get("isReceipt") is not True:
        return empty
    items: list[dict] = []
    amounts: list[Decimal] = []
    items_ok = True
    for raw in parsed.get("items") or []:
        if not isinstance(raw, dict):
            continue
        amount = parse_signed_money(raw.get("amount"))
        name = str(raw.get("name") or "").strip()[:120]
        if amount is None:
            items_ok = False
            continue
        unit = parse_signed_money(raw.get("unitPrice"))
        qty_text = str(raw.get("qty") or "").strip()
        try:
            qty = float(qty_text) if qty_text else None
        except ValueError:
            qty = None
        amounts.append(amount)
        items.append(
            {
                "name": name,
                "qty": qty,
                "unitPrice": str(unit) if unit is not None else None,
                "amount": str(amount),
                "isBrand": is_brand_line(name, tokens, raw.get("isBrandProduct") is True),
            }
        )
    fees = parse_signed_money(parsed.get("fees"))
    tax = parse_signed_money(parsed.get("tax"))
    subtotal = parse_signed_money(parsed.get("subtotal"))
    total = parse_total(parsed.get("total")) if parsed.get("totalLegible") is True else None
    if items_ok:
        ok, note = check_arithmetic(amounts, fees, tax, subtotal, total)
    else:
        ok, note = False, "Some item amounts could not be read"
    try:
        confidence = float(parsed.get("confidence"))
        confidence = max(0.0, min(1.0, confidence))
    except (TypeError, ValueError):
        confidence = None
    if ok is False:
        confidence = min(confidence if confidence is not None else 1.0, 0.4)
    if total is None and parsed.get("totalLegible") is not True:
        note = note or "No total in this photo"
    state = str(parsed.get("state") or "").strip().upper()
    date_text = str(parsed.get("date") or "").strip()
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", date_text):
        date_text = ""
    store_number = parse_store_number(parsed.get("storeNumber"))
    merchant = str(parsed.get("merchant") or "").strip()[:120]
    ticket = normalize_ticket(parsed.get("ticketNumber"))
    keys = receipt_keys(
        store_number=store_number, merchant=merchant, ticket=ticket,
        receipt_date=date_text, total=total,
    )
    return {
        # Also for a photo with no legible total (the top half of a long
        # receipt), so the halves can be matched by store and ticket.
        "merchant": merchant,
        "store_number": store_number,
        "receipt_date": date_text,
        "address": str(parsed.get("address") or "").strip()[:200],
        "city": str(parsed.get("city") or "").strip()[:80],
        "state_code": state if _STATE_RE.fullmatch(state) else "",
        "ticket_number": ticket,
        "register": re.sub(r"[^0-9A-Za-z]", "", str(parsed.get("register") or ""))[:16],
        "purchase_time": parse_time(parsed.get("time")),
        "line_items": items,
        "subtotal": subtotal,
        "fees": fees,
        "tax": tax,
        "card_last4": parse_card_last4(parsed.get("cardLast4")),
        "confidence": confidence,
        "arithmetic_ok": ok,
        "arithmetic_note": note[:200],
        "receipt_count": parse_receipt_count(parsed),
        **keys,
    }


def image_dhash(image_bytes: bytes) -> str:
    """64-bit difference hash of an image, "" when it cannot be decoded."""
    if not image_bytes:
        return ""
    try:
        from io import BytesIO

        from PIL import Image

        from recaps.photo_picks import difference_hash

        with Image.open(BytesIO(image_bytes)) as im:
            return difference_hash(im)
    except Exception:  # noqa: BLE001
        return ""


def read_receipt_image(
    image_bytes: bytes,
    content_type: str = "image/jpeg",
    *,
    brand_hint: str = "",
    tokens: set[str] | None = None,
) -> dict:
    """One receipt image → interpreted reading (v2 keys + v3 detail + the
    model's raw answer and usage). Never raises except :class:`AiUnavailable`
    (no API key). A model call that came back with nothing is ``failed``."""
    if not image_bytes:
        return {**interpret_reading(None), **interpret_detail(None), "raw": {}}
    user = RECEIPT_USER_V3
    if brand_hint:
        user += "\nThe brand and its products: " + brand_hint[:600]
    meta: dict = {}
    try:
        parsed = generate_vision_json(
            RECEIPT_SYSTEM_V3,
            user,
            image_bytes=image_bytes,
            content_type=content_type,
            schema=_RECEIPT_SCHEMA_V3,
            max_tokens=1200,
            meta=meta,
            small=bool(getattr(settings, "RECEIPT_READER_USE_SMALL_MODEL", False)),
        )
    except AiUnavailable:
        raise
    except Exception:  # noqa: BLE001
        parsed = None
    usage = {
        "model": meta.get("model") or "",
        "input_tokens": meta.get("input_tokens"),
        "output_tokens": meta.get("output_tokens"),
    }
    if parsed is None:
        return {
            **interpret_reading(None), **interpret_detail(None), **usage,
            "status": "failed", "raw": {}, "error": "The model returned nothing usable",
        }
    reading = {**interpret_reading(parsed), **interpret_detail(parsed, tokens), **usage}
    if parsed.get("isReceipt") is not True:
        reading["status"] = "not_a_receipt"
    reading["raw"] = scrub_card_numbers(parsed)
    return reading


def _receipt_file_q():
    from django.db.models import Q

    images = Q()
    for suffix in (".jpg", ".jpeg", ".png", ".webp", ".gif", ".heic", ".heif"):
        images |= Q(url__iendswith=suffix)
    return Q(file_recap_category__name__iregex=RECEIPT_CATEGORY_RE.pattern) & images


def _event_window_q(prefix: str, date_from: date | None, date_to: date | None):
    """Recaps whose EVENT date (else filing date) is in the range."""
    from django.db.models import Q

    if not date_from and not date_to:
        return Q()
    on_event = Q(**{f"{prefix}event__date__isnull": False})
    on_created = Q(**{f"{prefix}event__date__isnull": True})
    if date_from:
        on_event &= Q(**{f"{prefix}event__date__date__gte": date_from})
        on_created &= Q(**{f"{prefix}created_at__date__gte": date_from})
    if date_to:
        on_event &= Q(**{f"{prefix}event__date__date__lte": date_to})
        on_created &= Q(**{f"{prefix}created_at__date__lte": date_to})
    return on_event | on_created


def pending_receipt_files(
    *,
    tenant_id: int | None = None,
    since_days: int = 45,
    reread_older: bool = False,
    date_from: date | None = None,
    date_to: date | None = None,
    file_ids: list[int] | None = None,
):
    """Receipt-bucket image files on custom recaps that still need a read,
    newest first: no reading yet, queued (``pending``), or ``failed`` with
    tries left. ``reread_older`` also includes files read by an older reader.
    A date range (event date) replaces ``since_days`` when given."""
    from django.db.models import Q

    from recaps.models import CustomRecapFile, ReceiptReading

    if file_ids:
        # Named photos are read again whatever their status or date: the
        # caller is correcting a reading by hand.
        qs = CustomRecapFile.objects.filter(
            _receipt_file_q(), id__in=list(file_ids), custom_recap__isnull=False
        ).select_related("custom_recap")
        if tenant_id:
            qs = qs.filter(custom_recap__tenant_id=tenant_id)
        return qs.order_by("-id")

    unread = (
        Q(receipt_reading__isnull=True)
        | Q(receipt_reading__status=ReceiptReading.STATUS_PENDING)
        | Q(
            receipt_reading__status=ReceiptReading.STATUS_FAILED,
            receipt_reading__attempts__lt=MAX_ATTEMPTS,
        )
    )
    if reread_older:
        unread |= Q(receipt_reading__reader_version__lt=READER_VERSION) & ~Q(
            receipt_reading__status=ReceiptReading.STATUS_FAILED,
            receipt_reading__attempts__gte=MAX_ATTEMPTS,
        )
    qs = CustomRecapFile.objects.filter(
        unread, _receipt_file_q(), custom_recap__isnull=False
    ).select_related("custom_recap")
    if date_from or date_to:
        qs = qs.filter(_event_window_q("custom_recap__", date_from, date_to))
    else:
        qs = qs.filter(
            custom_recap__created_at__gte=timezone.now() - timedelta(days=since_days)
        )
    if tenant_id:
        qs = qs.filter(custom_recap__tenant_id=tenant_id)
    return qs.order_by("-id")


def _file_summary(recap_file) -> dict:
    """One receipt photo and its current reading, for a dry run. No images,
    no people: ids, store, date and amounts only."""
    reading = getattr(recap_file, "receipt_reading", None)
    return {
        "fileId": recap_file.id,
        "recapId": recap_file.custom_recap_id,
        "tenantId": recap_file.custom_recap.tenant_id,
        "status": getattr(reading, "status", None),
        "merchant": getattr(reading, "merchant", ""),
        "receiptDate": getattr(reading, "receipt_date", ""),
        "total": str(reading.total) if reading is not None and reading.total is not None else None,
        "subtotal": str(reading.subtotal) if reading is not None and reading.subtotal is not None else None,
    }


def find_read_receipts(
    *,
    merchant: str,
    tenant_id: int | None = None,
    date_from: date | None = None,
    date_to: date | None = None,
    limit: int = 40,
) -> list[dict]:
    """Receipt photos already read whose store name contains ``merchant``,
    within an event-date window. Read-only: finds the file id to pass to
    ``file_ids`` when a reading needs correcting."""
    from recaps.models import CustomRecapFile

    qs = CustomRecapFile.objects.filter(
        _receipt_file_q(),
        custom_recap__isnull=False,
        receipt_reading__merchant__icontains=merchant,
    ).select_related("custom_recap", "receipt_reading")
    if date_from or date_to:
        qs = qs.filter(_event_window_q("custom_recap__", date_from, date_to))
    if tenant_id:
        qs = qs.filter(custom_recap__tenant_id=tenant_id)
    return [_file_summary(f) for f in qs.order_by("-id")[:limit]]


def daily_cap() -> int:
    try:
        return max(0, int(getattr(settings, "RECEIPT_READER_DAILY_MAX_READS", DEFAULT_DAILY_MAX_READS)))
    except (TypeError, ValueError):
        return DEFAULT_DAILY_MAX_READS


def model_calls_today(now: datetime | None = None) -> int:
    from recaps.models import ReceiptReading

    now = now or timezone.now()
    start = datetime.combine(now.astimezone(dt_timezone.utc).date(), time.min, tzinfo=dt_timezone.utc)
    return ReceiptReading.objects.filter(model_called_at__gte=start).count()


def brand_hint_for_tenant(tenant_id: int | None) -> tuple[str, set[str]]:
    """``(hint text for the prompt, distinctive brand words)`` for a tenant:
    its name and up to 40 catalog product names."""
    if not tenant_id:
        return "", set()
    from events.models import Product
    from tenants.models import Tenant

    name = Tenant.objects.filter(id=tenant_id).values_list("name", flat=True).first() or ""
    products = list(
        Product.objects.filter(tenant_id=tenant_id).order_by("name").values_list("name", flat=True)[:40]
    )
    hint = name
    if products:
        hint += " — products: " + "; ".join(p for p in products if p)
    return hint, brand_tokens(name)


def queue_recap_receipts(custom_recap_id: int) -> int:
    """Mark a recap's unread receipt photos ``pending`` so the background
    reader picks them up first. Cheap (no model call). Returns how many."""
    from recaps.models import CustomRecapFile, ReceiptReading

    files = CustomRecapFile.objects.filter(
        _receipt_file_q(), custom_recap_id=custom_recap_id, receipt_reading__isnull=True
    ).values_list("id", flat=True)
    made = 0
    for file_id in files:
        _row, created = ReceiptReading.objects.get_or_create(
            custom_recap_file_id=file_id,
            defaults={"status": ReceiptReading.STATUS_PENDING, "reader_version": READER_VERSION},
        )
        made += int(created)
    return made


_SAVED_FIELDS = (
    "address", "city", "state_code", "ticket_number", "register", "purchase_time",
    "line_items", "subtotal", "fees", "tax", "card_last4", "confidence",
    "arithmetic_ok", "arithmetic_note", "raw", "model", "input_tokens",
    "output_tokens", "fingerprint", "ticket_key", "loose_key",
)


def read_pending_receipts(
    *,
    tenant_id: int | None = None,
    since_days: int = 45,
    limit: int = 40,
    reread_older: bool = False,
    dry_run: bool = False,
    date_from: date | None = None,
    date_to: date | None = None,
    file_ids: list[int] | None = None,
) -> dict:
    """Read up to ``limit`` unread receipt images and store the results.

    ``file_ids`` reads exactly those receipt photos again, even ones already
    read (to correct a misread); the dry run then lists them with their
    current reading.

    Stops at the daily cap (``RECEIPT_READER_DAILY_MAX_READS`` model calls
    per UTC day). ``dry_run`` reads nothing and reports what would be read.
    ``recap_ids`` lists the recaps whose receipts changed, for the flag
    checker."""
    from recaps.heic_conversion import display_blob_name, is_heic_blob
    from recaps.models import ReceiptReading
    from recaps.pdf import downscale_image_bytes
    from utils.gcs import download_blob_bytes, extract_blob_name_from_url

    counts = {
        "read": 0, "unreadable": 0, "not_a_receipt": 0, "failed": 0,
        "skipped": 0, "errors": 0,
    }
    pending = pending_receipt_files(
        tenant_id=tenant_id, since_days=since_days, reread_older=reread_older,
        date_from=date_from, date_to=date_to, file_ids=file_ids,
    )
    cap = daily_cap()
    used = model_calls_today()
    remaining = max(0, cap - used)
    report_extra = {
        "daily_cap": cap, "model_calls_today": used, "capped": False,
        "dry_run": dry_run, "recap_ids": [],
    }
    if dry_run:
        waiting = pending.count()
        out = {
            **counts, **report_extra, "ai_unavailable": False,
            "would_read": min(waiting, limit, remaining), "waiting": waiting,
        }
        if file_ids:
            out["files"] = [_file_summary(f) for f in pending[:limit]]
        return out
    budget = min(limit, remaining)
    if budget <= 0:
        return {**counts, **report_extra, "capped": True, "ai_unavailable": False}

    hints: dict[int, tuple[str, set[str]]] = {}
    touched: set[int] = set()
    for recap_file in pending[:budget]:
        blob = extract_blob_name_from_url(str(recap_file.url)) or ""
        if not blob or not _IMAGE_RE.search(blob):
            counts["skipped"] += 1
            continue
        tenant = recap_file.custom_recap.tenant_id
        if tenant not in hints:
            try:
                hints[tenant] = brand_hint_for_tenant(tenant)
            except Exception:  # noqa: BLE001
                hints[tenant] = ("", set())
        hint, tokens = hints[tenant]
        prior = ReceiptReading.objects.filter(custom_recap_file=recap_file).first()
        attempts = (prior.attempts if prior else 0) + 1
        try:
            source = display_blob_name(blob) if is_heic_blob(blob) else blob
            data = download_blob_bytes(source)
            if not data:
                counts["errors"] += 1
                _save_failure(recap_file, attempts, "Photo could not be downloaded", called=False)
                continue
            data = downscale_image_bytes(data, max_dim=READ_MAX_DIM) or data
            dhash = image_dhash(data)
            reading = read_receipt_image(data, "image/jpeg", brand_hint=hint, tokens=tokens)
        except AiUnavailable:
            # No API key: nothing can be read this run; leave everything unread.
            counts["skipped"] += 1
            return {**counts, **report_extra, "ai_unavailable": True,
                    "recap_ids": sorted(touched)}
        except Exception as exc:  # noqa: BLE001
            logger.warning("receipt read failed for file %s: %s", recap_file.id, exc)
            counts["errors"] += 1
            _save_failure(recap_file, attempts, "Reading failed", called=True)
            continue
        status = reading.get("status") or "unreadable"
        defaults = {
            "status": status,
            "total": reading.get("total"),
            "merchant": reading.get("merchant", ""),
            "receipt_date": reading.get("receipt_date", ""),
            "store_number": reading.get("store_number", ""),
            "charges": reading.get("charges", []),
            "reader_version": READER_VERSION,
            "image_dhash": dhash,
            "attempts": attempts,
            "error": str(reading.get("error") or "")[:200],
            "model_called_at": timezone.now(),
        }
        for field in _SAVED_FIELDS:
            if field in reading:
                defaults[field] = reading[field]
        if defaults.get("raw") is None:
            defaults["raw"] = {}
        reading_row, _ = ReceiptReading.objects.update_or_create(
            custom_recap_file=recap_file, defaults=defaults
        )
        counts[status if status in counts else "unreadable"] += 1
        touched.add(recap_file.custom_recap_id)
        # Success → auto-match; reread fail / drift → withdraw auto-links only.
        try:
            from pilot.finance import sync_auto_match_after_ocr

            sync_auto_match_after_ocr(reading_row)
        except Exception:  # noqa: BLE001 — OCR cron must not fail on finance heal
            logger.exception(
                "auto-match sync for receipt reading %s failed", reading_row.id
            )
    report_extra["capped"] = budget < limit and pending.count() > 0
    return {**counts, **report_extra, "ai_unavailable": False, "recap_ids": sorted(touched)}


def _save_failure(recap_file, attempts: int, error: str, *, called: bool) -> None:
    from recaps.models import ReceiptReading

    defaults = {
        "status": ReceiptReading.STATUS_FAILED,
        "attempts": attempts,
        "error": error[:200],
        "reader_version": READER_VERSION,
    }
    if called:
        defaults["model_called_at"] = timezone.now()
    ReceiptReading.objects.update_or_create(custom_recap_file=recap_file, defaults=defaults)


# ─── Review check ───────────────────────────────────────────────────

# Entered spend may differ from the receipts by rounding or a small tip.
TOLERANCE_ABS = Decimal("1.00")
TOLERANCE_PCT = Decimal("0.05")


def compare_spend_to_receipts(
    entered: float | Decimal | None, readings: list[dict], receipt_files: int
) -> dict:
    """Pure comparison → ``{"status", "label", "detail"}``.

    ``readings`` are the stored rows for this recap's receipt files
    (``status``, ``total``); ``receipt_files`` is how many receipt-bucket
    images the recap has. ``na`` whenever the comparison would be a guess:
    no spend entered, no receipt images, or not every receipt has been read.
    """
    label = "Receipts match spend"
    if entered is None or receipt_files <= 0:
        return {"status": "na", "label": label, "detail": ""}
    read = [r for r in readings if r.get("status") == "read" and r.get("total") is not None]
    unreadable = sum(1 for r in readings if r.get("status") == "unreadable")
    if len(readings) < receipt_files:
        return {"status": "na", "label": label, "detail": "Receipts not read yet"}
    if not read:
        return {
            "status": "na",
            "label": label,
            "detail": f"Couldn't read {unreadable} receipt photo{'s' if unreadable != 1 else ''}",
        }
    entered_d = Decimal(str(entered)).quantize(Decimal("0.01"))
    total = sum((Decimal(str(r["total"])) for r in read), Decimal("0")).quantize(Decimal("0.01"))
    gap = entered_d - total
    allowed = max(TOLERANCE_ABS, (entered_d * TOLERANCE_PCT).quantize(Decimal("0.01")))
    note = f" ({unreadable} couldn't be read)" if unreadable else ""
    count = f"{len(read)} receipt{'s' if len(read) != 1 else ''}"
    if unreadable:
        # Some receipts are unread, so the receipts can only UNDER-count.
        # Entered spend above what was read proves nothing; only flag when
        # the receipts already exceed what was entered.
        if total - entered_d > allowed:
            return {
                "status": "flag",
                "label": f"Receipts total ${total:,.2f}, more than the ${entered_d:,.2f} entered",
                "detail": f"{count} read{note}",
            }
        return {"status": "na", "label": label, "detail": f"{count} read{note}"}
    if abs(gap) > allowed:
        direction = "more" if gap > 0 else "less"
        return {
            "status": "flag",
            "label": f"${entered_d:,.2f} entered, receipts total ${total:,.2f}",
            "detail": f"Entered is ${abs(gap):,.2f} {direction} than {count} read",
        }
    return {
        "status": "pass",
        "label": label,
        "detail": f"${entered_d:,.2f} entered, {count} total ${total:,.2f}",
    }
