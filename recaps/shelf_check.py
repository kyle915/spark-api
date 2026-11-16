"""Read on-shelf photos from retail recaps: is the brand's product on the
shelf, roughly how many facings, is there a price tag, is there a gap.

A recap template has photo buckets (``FileRecapCategory``; see
``ambassadors.checkin_web.serialize_photo_buckets``). Photos an ambassador
filed under an on-shelf style bucket (the same words
:mod:`recaps.photo_check` uses) on a retail recap are read here, once each,
and the answer is stored as a :class:`recaps.models.ShelfReading`.

* Reading is a background job (:func:`read_pending_shelf_photos`, fired by
  the ``read-shelf-photos`` cron) or an explicit staff action for one recap
  (:func:`read_recap_shelf_photos`). Never on a page load.
* One reading per photo. ``unclear`` is a full answer; the model is told not
  to guess, and the code turns anything short of a confident answer into
  ``unclear`` or a blank. A call that failed outright is retried at most
  :data:`MAX_ATTEMPTS` times in total.
* The model is asked about the shelf and the product only. It is told to
  ignore people and it is sent the brand and product names, nothing else.
* A staff correction (:class:`recaps.models.ShelfReadingCorrection`) is stored
  separately and always wins (:func:`effective_answers`).
* Everything a brand sees (:func:`shelf_summary`) is counted here, in code,
  from stored answers on APPROVED recaps only.
"""

from __future__ import annotations

import logging
import re
import time
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation

from django.db.models import Q
from django.utils import timezone

from recaps.photo_check import _KIND_WORDS
from utils.ai_text import AiUnavailable, generate_vision_json

logger = logging.getLogger(__name__)

YES, NO, UNCLEAR = "yes", "no", "unclear"
ANSWERS = (YES, NO, UNCLEAR)

# Bucket names that hold shelf photos: one vocabulary with the photo check.
SHELF_CATEGORY_IREGEX = "|".join(re.escape(word) for word in _KIND_WORDS["shelf"])

_IMAGE_SUFFIXES = (".jpg", ".jpeg", ".png", ".webp", ".gif", ".heic", ".heif")

# A call that failed (not an answer) is tried again, up to this many in all.
MAX_ATTEMPTS = 2
# The cron: at most this many photos and this long per run, and stop early
# when the model is clearly down.
DEFAULT_RUN_LIMIT = 40
MAX_RUN_LIMIT = 200
DEFAULT_RUN_SECONDS = 240
MAX_RUN_SECONDS = 480
MAX_CONSECUTIVE_FAILURES = 3
# One staff click on one recap.
RECAP_RUN_LIMIT = 6
RECAP_RUN_SECONDS = 25
# Per HTTP attempt.
_MODEL_REQUEST_TIMEOUT = 20.0
_MODEL_MAX_TOKENS = 300
SHELF_MAX_DIM = 1400

MAX_FACINGS = 60
MAX_PLAUSIBLE_PRICE = Decimal("500")
_MAX_PRODUCT_NAMES = 40
# Stores listed under "not found" in the brand summary.
NOT_FOUND_LIST_MAX = 8

SHELF_SYSTEM = (
    "You look at one photo of a retail store shelf, cooler or display taken "
    "during a product sampling visit, and answer questions about one brand's "
    "product. Answer only from what is clearly visible. When you are not "
    "sure, answer unclear; do not guess. Do not identify, describe or "
    "mention any person; if people are in the frame, ignore them. "
    "productOnShelf: yes when at least one package of the named brand's "
    "product is on the shelf, no when the shelf is clearly visible and the "
    "product is not there, unclear otherwise. facings: the number of "
    "front-facing packages of that product in the front row, and "
    "facingsCountClear true only when they can be counted. priceTagVisible: "
    "yes when a shelf price tag for that product is visible. price: the "
    "price printed on that tag as a plain decimal, and priceLegible true "
    "only when every digit can be read. outOfStockGap: yes when there is an "
    "empty space on the shelf where that product belongs."
)

_ANSWER_PROP = {"type": "string", "enum": list(ANSWERS)}
SHELF_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "isShelfPhoto",
        "productOnShelf",
        "facings",
        "facingsCountClear",
        "priceTagVisible",
        "price",
        "priceLegible",
        "outOfStockGap",
    ],
    "properties": {
        "isShelfPhoto": {"type": "boolean"},
        "productOnShelf": _ANSWER_PROP,
        "facings": {"type": "integer"},
        "facingsCountClear": {"type": "boolean"},
        "priceTagVisible": _ANSWER_PROP,
        # e.g. "3.49"; "" when there is no legible price.
        "price": {"type": "string"},
        "priceLegible": {"type": "boolean"},
        "outOfStockGap": _ANSWER_PROP,
    },
}


# ─── Interpreting one answer ────────────────────────────────────────


def _answer(raw: object) -> str:
    return raw if raw in ANSWERS else UNCLEAR


def parse_price(raw: object) -> Decimal | None:
    text = str(raw if raw is not None else "").replace(",", "").replace("$", "").strip()
    if not re.fullmatch(r"\d{1,3}(\.\d{1,2})?", text):
        return None
    try:
        value = Decimal(text).quantize(Decimal("0.01"))
    except InvalidOperation:
        return None
    if value <= 0 or value > MAX_PLAUSIBLE_PRICE:
        return None
    return value


def parse_facings(raw: object) -> int | None:
    if isinstance(raw, bool) or not isinstance(raw, int):
        return None
    return raw if 1 <= raw <= MAX_FACINGS else None


def interpret_shelf(parsed: dict | None) -> dict:
    """Model output → the fields of a ``ShelfReading``. Pure.

    ``None`` (the call failed) is ``failed``. A photo that is not a shelf is
    ``unreadable``. Otherwise ``read``, with every doubtful part ``unclear``
    or blank: a facing count only with the product on the shelf and a clear
    count, a price only with a visible tag and a legible, plausible amount.
    """
    blank = {
        "product_on_shelf": UNCLEAR,
        "facings": None,
        "price_tag_visible": UNCLEAR,
        "price": None,
        "out_of_stock_gap": UNCLEAR,
    }
    if not isinstance(parsed, dict):
        return {"status": "failed", **blank}
    if parsed.get("isShelfPhoto") is not True:
        return {"status": "unreadable", **blank}
    on_shelf = _answer(parsed.get("productOnShelf"))
    tag = _answer(parsed.get("priceTagVisible"))
    facings = None
    if on_shelf == YES and parsed.get("facingsCountClear") is True:
        facings = parse_facings(parsed.get("facings"))
    price = None
    if tag == YES and parsed.get("priceLegible") is True:
        price = parse_price(parsed.get("price"))
    return {
        "status": "read",
        "product_on_shelf": on_shelf,
        "facings": facings,
        "price_tag_visible": tag,
        "price": price,
        "out_of_stock_gap": _answer(parsed.get("outOfStockGap")),
    }


def compose_prompt(brand: str, product_names: list[str]) -> str:
    lines = [f"Brand: {' '.join((brand or '').split())[:80] or 'not given'}"]
    names = [" ".join(n.split())[:80] for n in product_names if n and n.strip()]
    if names:
        lines.append("Its products:")
        lines.extend(f"- {name}" for name in names[:_MAX_PRODUCT_NAMES])
    lines.append("Answer for this brand's product. Ignore any people in the photo.")
    return "\n".join(lines)


def read_shelf_image(image_bytes: bytes, *, brand: str, product_names: list[str]) -> dict:
    """One shelf photo → interpreted answer. Raises only ``AiUnavailable``
    (no key), like the receipt reader."""
    if not image_bytes:
        return interpret_shelf(None)
    try:
        parsed = generate_vision_json(
            SHELF_SYSTEM,
            compose_prompt(brand, product_names),
            image_bytes=image_bytes,
            content_type="image/jpeg",
            schema=SHELF_SCHEMA,
            max_tokens=_MODEL_MAX_TOKENS,
            timeout=_MODEL_REQUEST_TIMEOUT,
        )
    except AiUnavailable:
        raise
    except Exception:  # noqa: BLE001
        parsed = None
    return interpret_shelf(parsed)


# ─── Which photos ───────────────────────────────────────────────────


def retail_recap_q(prefix: str = "") -> Q:
    """Custom recaps that are a retail visit: the event type, the request
    type or the recap template says "retail". ``prefix`` is the ORM path to
    the ``CustomRecap`` (``""`` on it, ``"custom_recap__"`` on its files)."""
    return (
        Q(**{f"{prefix}event__event_type__name__icontains": "retail"})
        | Q(**{f"{prefix}event__request__request_type__name__icontains": "retail"})
        | Q(**{f"{prefix}custom_recap_template__name__icontains": "retail"})
    )


def _image_q() -> Q:
    images = Q()
    for suffix in _IMAGE_SUFFIXES:
        images |= Q(url__iendswith=suffix)
    return images


def shelf_files(*, recap_id: int | None = None):
    """Image files in an on-shelf style bucket on retail custom recaps."""
    from recaps.models import CustomRecapFile

    qs = (
        CustomRecapFile.objects.filter(
            file_recap_category__name__iregex=SHELF_CATEGORY_IREGEX,
            custom_recap__isnull=False,
        )
        .filter(retail_recap_q("custom_recap__"))
        .filter(_image_q())
    )
    if recap_id is not None:
        qs = qs.filter(custom_recap_id=recap_id)
    return qs.distinct()


def pending_shelf_files(
    *, tenant_id: int | None = None, since_days: int = 45, recap_id: int | None = None
):
    """Shelf photos with no answer yet (never read, or a failed call with
    attempts left), newest first. Archived recaps are skipped."""
    from recaps.models import ShelfReading

    qs = shelf_files(recap_id=recap_id).filter(
        Q(shelf_reading__isnull=True)
        | Q(
            shelf_reading__status=ShelfReading.STATUS_FAILED,
            shelf_reading__attempts__lt=MAX_ATTEMPTS,
        ),
        custom_recap__archived_at__isnull=True,
    )
    if recap_id is None:
        qs = qs.filter(
            custom_recap__created_at__gte=timezone.now() - timedelta(days=since_days)
        )
    if tenant_id:
        qs = qs.filter(custom_recap__tenant_id=tenant_id)
    return qs.select_related("custom_recap__tenant").order_by("-id")


def _product_names(tenant_id: int, cache: dict) -> list[str]:
    if tenant_id not in cache:
        from events.models import Product

        cache[tenant_id] = list(
            Product.objects.filter(tenant_id=tenant_id)
            .order_by("name", "id")
            .values_list("name", flat=True)[:_MAX_PRODUCT_NAMES]
        )
    return cache[tenant_id]


def _read_files(files, *, limit: int, max_seconds: float) -> dict:
    """Read up to ``limit`` files within ``max_seconds`` and store each answer."""
    from recaps.heic_conversion import display_blob_name, is_heic_blob
    from recaps.models import ShelfReading
    from recaps.pdf import downscale_image_bytes
    from utils.gcs import download_blob_bytes, extract_blob_name_from_url

    counts = {"read": 0, "unreadable": 0, "failed": 0, "skipped": 0, "errors": 0}
    started = time.monotonic()
    consecutive_failures = 0
    products: dict[int, list[str]] = {}
    stopped = ""
    for recap_file in files[:limit]:
        if time.monotonic() - started > max_seconds:
            stopped = "time"
            break
        if consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
            stopped = "failures"
            break
        blob = extract_blob_name_from_url(str(recap_file.url)) or ""
        if not blob:
            counts["skipped"] += 1
            continue
        recap = recap_file.custom_recap
        try:
            source = display_blob_name(blob) if is_heic_blob(blob) else blob
            data = download_blob_bytes(source)
            if not data:
                counts["errors"] += 1
                continue
            data = downscale_image_bytes(data, max_dim=SHELF_MAX_DIM) or data
            answer = read_shelf_image(
                data,
                brand=getattr(recap.tenant, "name", "") or "",
                product_names=_product_names(recap.tenant_id, products),
            )
        except AiUnavailable:
            # No API key: nothing can be read this run; leave everything unread.
            return {**counts, "ai_unavailable": True, "stopped": "unavailable"}
        except Exception as exc:  # noqa: BLE001
            logger.warning("shelf read failed for file %s: %s", recap_file.id, exc)
            counts["errors"] += 1
            continue

        status = answer.pop("status")
        existing = ShelfReading.objects.filter(custom_recap_file=recap_file).first()
        attempts = (existing.attempts if existing else 0) + 1
        ShelfReading.objects.update_or_create(
            custom_recap_file=recap_file,
            defaults={"status": status, "attempts": attempts, **answer},
        )
        counts[status] += 1
        consecutive_failures = consecutive_failures + 1 if status == "failed" else 0
    return {**counts, "ai_unavailable": False, "stopped": stopped}


def read_pending_shelf_photos(
    *,
    tenant_id: int | None = None,
    since_days: int = 45,
    limit: int = DEFAULT_RUN_LIMIT,
    max_seconds: float = DEFAULT_RUN_SECONDS,
) -> dict:
    """The cron: read unread shelf photos, within a count and time budget."""
    limit = max(1, min(int(limit or DEFAULT_RUN_LIMIT), MAX_RUN_LIMIT))
    max_seconds = max(5.0, min(float(max_seconds or DEFAULT_RUN_SECONDS), MAX_RUN_SECONDS))
    files = pending_shelf_files(tenant_id=tenant_id, since_days=since_days)
    report = _read_files(files, limit=limit, max_seconds=max_seconds)
    report["remaining"] = pending_shelf_files(
        tenant_id=tenant_id, since_days=since_days
    ).count()
    return report


def read_recap_shelf_photos(recap_id: int) -> dict:
    """Staff asked for one recap: read its unread shelf photos now."""
    files = pending_shelf_files(recap_id=recap_id)
    return _read_files(files, limit=RECAP_RUN_LIMIT, max_seconds=RECAP_RUN_SECONDS)


# ─── Answers, with the staff correction on top ──────────────────────


def _answers_of(row) -> dict:
    return {
        "product_on_shelf": _answer(row.product_on_shelf),
        "facings": row.facings,
        "price_tag_visible": _answer(row.price_tag_visible),
        "price": row.price,
        "out_of_stock_gap": _answer(row.out_of_stock_gap),
    }


def effective_answers(reading, correction) -> dict | None:
    """The answer to use for one photo: the staff correction when there is
    one, else what was read. ``None`` when there is neither (not read yet,
    not a shelf photo, or a failed call)."""
    if correction is not None:
        return _answers_of(correction)
    if reading is not None and reading.status == "read":
        return _answers_of(reading)
    return None


def save_correction(
    recap_file,
    *,
    product_on_shelf: str,
    facings: int | None,
    price_tag_visible: str,
    price,
    out_of_stock_gap: str,
    note: str = "",
    user=None,
):
    """Store a staff member's answers for one photo. The reading is untouched."""
    from recaps.models import ShelfReadingCorrection

    for value in (product_on_shelf, price_tag_visible, out_of_stock_gap):
        if value not in ANSWERS:
            raise ValueError("Answers must be yes, no or unclear.")
    if facings is not None and not 0 <= int(facings) <= 999:
        raise ValueError("Facings must be a small whole number.")
    amount = None
    if price not in (None, ""):
        amount = parse_price(price)
        if amount is None:
            raise ValueError("Price must be an amount like 3.49.")
    row, _ = ShelfReadingCorrection.objects.update_or_create(
        custom_recap_file=recap_file,
        defaults={
            "product_on_shelf": product_on_shelf,
            "facings": int(facings) if facings is not None and product_on_shelf == YES else None,
            "price_tag_visible": price_tag_visible,
            "price": amount if price_tag_visible == YES else None,
            "out_of_stock_gap": out_of_stock_gap,
            "note": (note or "").strip()[:2000],
            "corrected_by": user if getattr(user, "pk", None) else None,
        },
    )
    return row


def recap_shelf_photos(recap_id: int) -> list[dict]:
    """Every shelf photo on one recap with its reading and correction, for
    the staff review page."""
    from recaps.heic_conversion import display_blob_name
    from utils.gcs import extract_blob_name_from_url, public_url

    rows = []
    files = (
        shelf_files(recap_id=recap_id)
        .select_related(
            "file_recap_category",
            "shelf_reading",
            "shelf_correction",
            "shelf_correction__corrected_by",
        )
        .order_by("id")
    )
    for recap_file in files:
        reading = getattr(recap_file, "shelf_reading", None)
        correction = getattr(recap_file, "shelf_correction", None)
        blob = extract_blob_name_from_url(str(recap_file.url)) or ""
        try:
            url = public_url(display_blob_name(blob)) or ""
        except Exception:  # noqa: BLE001
            url = ""
        rows.append(
            {
                "file_id": recap_file.id,
                "file_url": url,
                "bucket_name": getattr(recap_file.file_recap_category, "name", "") or "",
                "reading": reading,
                "correction": correction,
            }
        )
    return rows


# ─── Brand summary ──────────────────────────────────────────────────


def visit_answer(photo_answers: list[dict]) -> dict:
    """Several photos from one visit → one answer for the visit. Pure.

    On shelf: yes when any photo shows the product; no only when every photo
    says no; otherwise unclear. Price tag: yes when any photo shows one.
    """
    on_shelf = [a["product_on_shelf"] for a in photo_answers]
    if YES in on_shelf:
        product = YES
    elif on_shelf and all(a == NO for a in on_shelf):
        product = NO
    else:
        product = UNCLEAR
    return {
        "product_on_shelf": product,
        "price_tag_visible": YES
        if any(a["price_tag_visible"] == YES for a in photo_answers)
        else UNCLEAR,
    }


def _store_name(event) -> str:
    retailer = getattr(event, "retailer", None)
    request = getattr(event, "request", None)
    for name in (
        getattr(retailer, "name", None),
        getattr(getattr(request, "retailer", None), "name", None) if request else None,
        getattr(request, "retailer_name", None) if request else None,
        getattr(event, "name", None),
    ):
        if name and str(name).strip():
            return str(name).strip()[:120]
    return "Store not named"


def _event_day(event) -> str:
    for value in (
        getattr(event, "date", None),
        getattr(event, "start_time", None),
        getattr(getattr(event, "request", None), "date", None),
    ):
        if value:
            try:
                return value.date().isoformat() if hasattr(value, "date") else value.isoformat()
            except Exception:  # noqa: BLE001
                continue
    return ""


def shelf_summary(tenant_id: int, start: date | None, end: date | None) -> dict:
    """On-shelf figures for one brand from APPROVED retail recaps in the
    window (by event date). Counted here from stored answers.

    The base is retail visits with at least one shelf photo that has an
    answer. ``unread_visits`` are visits whose shelf photos have none yet.
    No ambassador, no staff note.
    """
    from recaps.models import CustomRecap, CustomRecapFile
    from recaps.tenant_overview import (
        _approved_only,
        _filter_event_window,
        _inclusive_dates_to_window,
    )

    window = _inclusive_dates_to_window(start, end)
    recaps = _approved_only(
        _filter_event_window(
            CustomRecap.objects.filter(tenant_id=tenant_id).filter(retail_recap_q()),
            "event__",
            window,
        ),
        "",
    ).filter(archived_at__isnull=True)
    recap_ids = list(recaps.values_list("id", flat=True).distinct())

    by_recap: dict[int, list[dict | None]] = {}
    files = (
        CustomRecapFile.objects.filter(
            custom_recap_id__in=recap_ids,
            file_recap_category__name__iregex=SHELF_CATEGORY_IREGEX,
        )
        .filter(_image_q())
        .select_related("shelf_reading", "shelf_correction")
    )
    for recap_file in files:
        by_recap.setdefault(recap_file.custom_recap_id, []).append(
            effective_answers(
                getattr(recap_file, "shelf_reading", None),
                getattr(recap_file, "shelf_correction", None),
            )
        )

    counts = {"visits": 0, "on_shelf": 0, "not_found": 0, "unclear": 0, "price_tag": 0}
    unread_visits = 0
    not_found_ids: list[int] = []
    for recap_id, answers in by_recap.items():
        known = [a for a in answers if a is not None]
        if not known:
            unread_visits += 1
            continue
        visit = visit_answer(known)
        counts["visits"] += 1
        if visit["product_on_shelf"] == YES:
            counts["on_shelf"] += 1
        elif visit["product_on_shelf"] == NO:
            counts["not_found"] += 1
            not_found_ids.append(recap_id)
        else:
            counts["unclear"] += 1
        if visit["price_tag_visible"] == YES:
            counts["price_tag"] += 1

    stores = []
    if not_found_ids:
        rows = (
            CustomRecap.objects.filter(id__in=not_found_ids)
            .select_related("event__retailer", "event__request__retailer")
            .order_by("-event__date", "-id")
        )
        for recap in rows[:NOT_FOUND_LIST_MAX]:
            stores.append(
                {"store_name": _store_name(recap.event), "event_date": _event_day(recap.event)}
            )
    return {
        **counts,
        "unread_visits": unread_visits,
        "not_found_stores": stores,
        "not_found_more": max(0, len(not_found_ids) - len(stores)),
    }
