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
"""

from __future__ import annotations

import logging
import re
from datetime import timedelta
from decimal import Decimal, InvalidOperation

from django.utils import timezone

from utils.ai_text import AiUnavailable, generate_vision_json

logger = logging.getLogger(__name__)

RECEIPT_CATEGORY_RE = re.compile(r"receipt|product\s*spend|expense", re.IGNORECASE)
_IMAGE_RE = re.compile(r"\.(jpe?g|png|webp|gif|heic|heif)$", re.IGNORECASE)

# Bumped when the reader starts capturing more. Rows written by an older
# reader can be re-read once (``reread_older``) to fill the new fields.
READER_VERSION = 2

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


def read_receipt_image(image_bytes: bytes, content_type: str = "image/jpeg") -> dict:
    """One receipt image → interpreted reading. Never raises."""
    if not image_bytes:
        return interpret_reading(None)
    try:
        parsed = generate_vision_json(
            RECEIPT_SYSTEM,
            RECEIPT_USER,
            image_bytes=image_bytes,
            content_type=content_type,
            schema=_RECEIPT_SCHEMA,
            max_tokens=400,
        )
    except AiUnavailable:
        raise
    except Exception:  # noqa: BLE001
        parsed = None
    return interpret_reading(parsed)


def pending_receipt_files(
    *, tenant_id: int | None = None, since_days: int = 45, reread_older: bool = False
):
    """Receipt-bucket image files on custom recaps that have no reading yet,
    newest first. ``reread_older`` also includes files read by an older
    reader (before store numbers and charge lines were captured)."""
    from django.db.models import Q

    from recaps.models import CustomRecapFile

    unread = Q(receipt_reading__isnull=True)
    if reread_older:
        unread |= Q(receipt_reading__reader_version__lt=READER_VERSION)
    qs = CustomRecapFile.objects.filter(
        unread,
        file_recap_category__name__iregex=RECEIPT_CATEGORY_RE.pattern,
        custom_recap__isnull=False,
        custom_recap__created_at__gte=timezone.now() - timedelta(days=since_days),
    ).select_related("custom_recap")
    if tenant_id:
        qs = qs.filter(custom_recap__tenant_id=tenant_id)
    # Images only, in the query: a PDF in the receipts bucket can never be
    # read, and leaving it "pending" would use up every run's limit.
    images = Q()
    for suffix in (".jpg", ".jpeg", ".png", ".webp", ".gif", ".heic", ".heif"):
        images |= Q(url__iendswith=suffix)
    return qs.filter(images).order_by("-id")


def read_pending_receipts(
    *,
    tenant_id: int | None = None,
    since_days: int = 45,
    limit: int = 40,
    reread_older: bool = False,
) -> dict:
    """Read up to ``limit`` unread receipt images and store the results."""
    from recaps.heic_conversion import display_blob_name, is_heic_blob
    from recaps.models import ReceiptReading
    from recaps.pdf import downscale_image_bytes
    from utils.gcs import download_blob_bytes, extract_blob_name_from_url

    counts = {"read": 0, "unreadable": 0, "skipped": 0, "errors": 0}
    pending = pending_receipt_files(
        tenant_id=tenant_id, since_days=since_days, reread_older=reread_older
    )
    for recap_file in pending[:limit]:
        blob = extract_blob_name_from_url(str(recap_file.url)) or ""
        if not blob or not _IMAGE_RE.search(blob):
            counts["skipped"] += 1
            continue
        try:
            source = display_blob_name(blob) if is_heic_blob(blob) else blob
            data = download_blob_bytes(source)
            if not data:
                counts["errors"] += 1
                continue
            data = downscale_image_bytes(data) or data
            reading = read_receipt_image(data, "image/jpeg")
        except AiUnavailable:
            # No API key: nothing can be read this run; leave everything unread.
            counts["skipped"] += 1
            return {**counts, "ai_unavailable": True}
        except Exception as exc:  # noqa: BLE001
            logger.warning("receipt read failed for file %s: %s", recap_file.id, exc)
            counts["errors"] += 1
            continue
        ReceiptReading.objects.update_or_create(
            custom_recap_file=recap_file,
            defaults={
                "status": reading["status"],
                "total": reading["total"],
                "merchant": reading["merchant"],
                "receipt_date": reading["receipt_date"],
                "store_number": reading.get("store_number", ""),
                "charges": reading.get("charges", []),
                "reader_version": READER_VERSION,
            },
        )
        counts[reading["status"]] += 1
    return {**counts, "ai_unavailable": False}


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
