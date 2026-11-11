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

RECEIPT_SYSTEM = (
    "You read retail purchase receipts. Report only what is printed on the "
    "receipt in the image. If the image shows more than one receipt, read the "
    "one that is most fully in frame. Never report card numbers or anything "
    "about people. If you cannot clearly read the final amount paid, say so."
)
RECEIPT_USER = (
    "Read this receipt. Give the final total paid (after tax), the store "
    "name, and the purchase date if printed. If this is not a receipt or the "
    "total is not legible, set isReceipt or totalLegible to false."
)
_RECEIPT_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["isReceipt", "totalLegible", "total", "merchant", "date"],
    "properties": {
        "isReceipt": {"type": "boolean"},
        "totalLegible": {"type": "boolean"},
        # Final amount paid as a plain decimal string, e.g. "38.11".
        "total": {"type": "string"},
        "merchant": {"type": "string"},
        # ISO date if printed, else "".
        "date": {"type": "string"},
    },
}

# A single field-marketing supply run above this is far more likely a
# misread (a barcode, a phone number) than a real purchase.
MAX_PLAUSIBLE_TOTAL = Decimal("5000")


def parse_total(raw) -> Decimal | None:
    """A printed total → Decimal, or None when it is not a plausible amount."""
    text = str(raw if raw is not None else "").replace(",", "").replace("$", "").strip()
    if not re.fullmatch(r"\d{1,5}(\.\d{1,2})?", text):
        return None
    try:
        value = Decimal(text).quantize(Decimal("0.01"))
    except InvalidOperation:
        return None
    if value <= 0 or value > MAX_PLAUSIBLE_TOTAL:
        return None
    return value


def interpret_reading(parsed: dict | None) -> dict:
    """Model output → ``{"status", "total", "merchant", "receipt_date"}``.
    Anything short of a confident, plausible total is ``unreadable``."""
    unreadable = {"status": "unreadable", "total": None, "merchant": "", "receipt_date": ""}
    if not isinstance(parsed, dict):
        return unreadable
    if parsed.get("isReceipt") is not True or parsed.get("totalLegible") is not True:
        return unreadable
    total = parse_total(parsed.get("total"))
    if total is None:
        return unreadable
    date_text = str(parsed.get("date") or "").strip()
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", date_text):
        date_text = ""
    return {
        "status": "read",
        "total": total,
        "merchant": str(parsed.get("merchant") or "").strip()[:120],
        "receipt_date": date_text,
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
            max_tokens=200,
        )
    except AiUnavailable:
        raise
    except Exception:  # noqa: BLE001
        parsed = None
    return interpret_reading(parsed)


def pending_receipt_files(*, tenant_id: int | None = None, since_days: int = 45):
    """Receipt-bucket image files on custom recaps that have no reading yet,
    newest first."""
    from recaps.models import CustomRecapFile

    qs = CustomRecapFile.objects.filter(
        file_recap_category__name__iregex=RECEIPT_CATEGORY_RE.pattern,
        receipt_reading__isnull=True,
        custom_recap__isnull=False,
        custom_recap__created_at__gte=timezone.now() - timedelta(days=since_days),
    ).select_related("custom_recap")
    if tenant_id:
        qs = qs.filter(custom_recap__tenant_id=tenant_id)
    # Images only, in the query: a PDF in the receipts bucket can never be
    # read, and leaving it "pending" would use up every run's limit.
    from django.db.models import Q

    images = Q()
    for suffix in (".jpg", ".jpeg", ".png", ".webp", ".gif", ".heic", ".heif"):
        images |= Q(url__iendswith=suffix)
    return qs.filter(images).order_by("-id")


def read_pending_receipts(
    *, tenant_id: int | None = None, since_days: int = 45, limit: int = 40
) -> dict:
    """Read up to ``limit`` unread receipt images and store the results."""
    from recaps.heic_conversion import display_blob_name, is_heic_blob
    from recaps.models import ReceiptReading
    from recaps.pdf import downscale_image_bytes
    from utils.gcs import download_blob_bytes, extract_blob_name_from_url

    counts = {"read": 0, "unreadable": 0, "skipped": 0, "errors": 0}
    for recap_file in pending_receipt_files(tenant_id=tenant_id, since_days=since_days)[:limit]:
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
