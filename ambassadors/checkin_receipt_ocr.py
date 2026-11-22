"""Pre-submit receipt OCR, brand-spend proposal, and mismatch advisories for walk-up check-in.

When an ambassador drops a receipt photo into an expense/receipt bucket on the
public check-in recap form, ``POST /api/public/checkin/<code>/receipt-read``
calls :func:`read_checkin_receipt`:

1. **Itemized brand spend proposal** — reuses :mod:`recaps.receipt_reader` +
   :func:`recaps.receipt_spend.summarize_receipt` (with automatic fallback to
   Google Cloud Vision :func:`receipts.ocr.run_ocr` when ``OPENAI_API_KEY`` is
   unset or unavailable) to read the printed total and line items, excluding
   non-brand items when both brand and non-brand lines are detected.
2. **Non-blocking pre-submit advisories** — checks:
   * ``store_mismatch``: receipt merchant / store # vs shift venue;
   * ``date_mismatch``: printed receipt date vs shift calendar date;
   * ``duplicate_receipt``: image SHA-256 or OCR duplicate hashes
     (``fingerprint``, ``ticket_key``, ``loose_key``) against prior submissions.

Advisory only: nothing is written to storage or the database, ambassador PII is
never sent to any model or echoed in public messages, and any missing
credential, timeout, or unreadable image degrades cleanly without blocking
recap submission.
"""

from __future__ import annotations

import concurrent.futures
import hashlib
import logging
import re
from datetime import date, datetime
from datetime import timezone as dt_timezone
from decimal import Decimal, InvalidOperation
from types import SimpleNamespace

from django.conf import settings
from django.core.cache import cache
from django.db.models import Q

from ambassadors import checkin_web
from events.models import Retailer
from recaps.models import (
    CustomRecap,
    CustomRecapFile,
    Recap,
    RecapFile,
    ReceiptReading,
)
from recaps.pdf import downscale_image_bytes
from recaps.photo_reuse import _family_ids, normalize_content_sha256
from recaps.recap_location import (
    ChainIndex,
    chain_in_text,
    same_street_address,
    title_store_text,
)
from recaps.receipt_reader import (
    READ_MAX_DIM,
    RECEIPT_SYSTEM_V3,
    RECEIPT_USER_V3,
    _RECEIPT_SCHEMA_V3,
    brand_hint_for_tenant,
    interpret_detail,
    interpret_reading,
    is_brand_line,
    normalize_ticket,
    parse_signed_money,
    parse_store_number,
    parse_total,
    receipt_keys,
    scrub_card_numbers,
)
from recaps.receipt_spend import summarize_receipt
from receipts.models import ConsumerReceipt
from receipts.ocr import OcrResult, run_ocr
from utils.ai_text import AiUnavailable, generate_vision_json

logger = logging.getLogger(__name__)

RECEIPT_UNREADABLE = "Could not read that receipt. Enter the spend amount by hand."

# Bounded well inside the mobile page's 12-second fetch timeout.
MODEL_DEADLINE_SECONDS = 7.5
_MODEL_REQUEST_TIMEOUT = 6.5
_MODEL_MAX_TOKENS = 1200

MAX_IMAGE_BYTES = 4_000_000
RESULT_CACHE_SECONDS = 15 * 60

_CENT = Decimal("0.01")
_ZERO = Decimal("0.00")

_EXECUTOR = concurrent.futures.ThreadPoolExecutor(
    max_workers=4, thread_name_prefix="checkin-receipt-ocr"
)

_TICKET_RE = re.compile(
    r"(?:ticket|trans(?:action)?|rcpt|receipt|check|order|ref)\s*(?:no\.?|num(?:ber)?|id|#|:)?\s*#?\s*([0-9A-Za-z-]{3,24})",
    re.IGNORECASE,
)
_OCR_LINE_AMOUNT_RE = re.compile(
    r"^(?P<label>.*?S?.*?)\s+\$?(?P<amount>-?\d{1,4}[.,]\d{2})\s*[A-Za-z]?\s*$"
)
_OCR_SUMMARY_LINE_RE = re.compile(
    r"\b(sub\s*total|subtotal|grand\s*total|total|tax|sales\s*tax|balance|amount\s*due|"
    r"amount\s*paid|change|cash|credit|debit|visa|mastercard|amex|discover|tender|"
    r"approved|auth|card|tip|gratuity|fee|bag\s*fee|deposit|crv|bottle\s*dep|"
    r"savings|you\s*saved)\b",
    re.IGNORECASE,
)
_GENERIC_VENUE_WORDS = frozenset(
    """
    the and for at in of on by to store stores market markets mart shop shops
    supermarket grocery groceries food foods fresh farmers wine wines spirits
    liquor liquors beer beers beverage beverages company co inc llc ltd corp
    center plaza square village north south east west
    """.split()
)
_NON_RETAIL_PROGRAM_RE = re.compile(
    r"\b(event|activation|seeding|guerilla|festival|sampling\s+tour)\b",
    re.IGNORECASE,
)


def _d(value: object) -> Decimal:
    if value is None or value == "":
        return _ZERO
    try:
        return Decimal(str(value)).quantize(_CENT)
    except (InvalidOperation, ValueError):
        return _ZERO


def _money_str(value: Decimal | None) -> str | None:
    if value is None:
        return None
    return f"{value.quantize(_CENT):.2f}"


def _day_label(day: date | None) -> str:
    if day is None:
        return ""
    return f"{day.strftime('%b')} {day.day}, {day.year}"


def _parse_iso_day(text: str | None) -> date | None:
    raw = (text or "").strip()
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", raw):
        return None
    try:
        return date.fromisoformat(raw)
    except ValueError:
        return None


def _venue_tokens(text: str | None) -> set[str]:
    out: set[str] = set()
    for word in re.findall(r"[a-z]+", (text or "").lower()):
        if len(word) >= 3 and word not in _GENERIC_VENUE_WORDS:
            out.add(word)
    return out


def _unreadable_payload(
    *, advisories: list[dict] | None = None, sha256: str = ""
) -> dict:
    return {
        "available": False,
        "proposedSpend": None,
        "receiptTotal": None,
        "brandProductSubtotal": None,
        "excludedNonBrandTotal": None,
        "excludedNonBrandCount": 0,
        "merchant": "",
        "storeNumber": "",
        "receiptDate": "",
        "dedupeKey": sha256,
        "advisories": advisories or [],
        "message": RECEIPT_UNREADABLE,
    }


# ─── OCR / Vision extraction ────────────────────────────────────────


def _parse_ocr_items(text: str, tokens: set[str]) -> list[dict]:
    """Best-effort itemization over plain Vision OCR text lines."""
    items: list[dict] = []
    for raw_line in (text or "").splitlines():
        line = " ".join(raw_line.strip().split())
        if not line or _OCR_SUMMARY_LINE_RE.search(line):
            continue
        m = _OCR_LINE_AMOUNT_RE.match(line)
        if not m:
            continue
        name = m.group("label").strip(" .:-#*")
        if len(name) < 2 or not any(ch.isalpha() for ch in name):
            continue
        amt = parse_signed_money(m.group("amount").replace(",", "."))
        if amt is None or amt == _ZERO:
            continue
        items.append(
            {
                "name": name[:120],
                "qty": None,
                "unitPrice": None,
                "amount": str(amt),
                "isBrand": is_brand_line(name, tokens, False),
            }
        )
    return items


def _reading_from_ocr_fallback(ocr: OcrResult, tokens: set[str]) -> dict | None:
    """Convert :class:`receipts.ocr.OcrResult` into the same dict shape as
    :mod:`recaps.receipt_reader`."""
    if not ocr.ok or not ocr.text.strip():
        return None
    total = parse_total(str(ocr.amount)) if ocr.amount is not None else None
    if total is None:
        return None
    merchant = (ocr.store or "").strip()[:120]
    chain_name, chain_num = chain_in_text(merchant)
    if not chain_name:
        for line in ocr.text.splitlines()[:6]:
            c_name, c_num = chain_in_text(line)
            if c_name:
                merchant = merchant or c_name
                chain_num = chain_num or c_num
                break
    store_number = chain_num or ""
    if not store_number:
        store_match = re.search(
            r"(?:\bstore\s*(?:no\.?|num(?:ber)?|#)?|#)\s*0*(\d{1,5})\b",
            ocr.text,
            re.IGNORECASE,
        )
        if store_match:
            store_number = parse_store_number(store_match.group(1))
    ticket = ""
    ticket_match = _TICKET_RE.search(ocr.text)
    if ticket_match:
        ticket = normalize_ticket(ticket_match.group(1))
    receipt_date = ocr.purchase_date.isoformat() if ocr.purchase_date else ""
    line_items = _parse_ocr_items(ocr.text, tokens)
    keys = receipt_keys(
        store_number=store_number,
        merchant=merchant,
        ticket=ticket,
        receipt_date=receipt_date,
        total=total,
    )
    return {
        "status": "read",
        "total": total,
        "merchant": merchant,
        "store_number": store_number,
        "receipt_date": receipt_date,
        "address": "",
        "city": "",
        "state_code": "",
        "ticket_number": ticket,
        "purchase_time": "",
        "line_items": line_items,
        "subtotal": None,
        "fees": None,
        "tax": None,
        **keys,
    }


def _ask_vision_or_ocr(
    image_bytes: bytes,
    *,
    content_type: str,
    brand_hint: str,
    tokens: set[str],
) -> dict | None:
    """Try structured vision reader first; fall back to ``receipts.ocr.run_ocr``."""
    user_prompt = RECEIPT_USER_V3
    if brand_hint:
        user_prompt += "\nThe brand and its products: " + brand_hint[:600]
    parsed = None
    try:
        parsed = generate_vision_json(
            RECEIPT_SYSTEM_V3,
            user_prompt,
            image_bytes=image_bytes,
            content_type=content_type,
            schema=_RECEIPT_SCHEMA_V3,
            max_tokens=_MODEL_MAX_TOKENS,
            small=bool(getattr(settings, "RECEIPT_READER_USE_SMALL_MODEL", False)),
            timeout=_MODEL_REQUEST_TIMEOUT,
        )
    except AiUnavailable:
        parsed = None
    except Exception:  # noqa: BLE001
        logger.info(
            "checkin receipt vision call failed; trying OCR fallback", exc_info=True
        )
        parsed = None

    if isinstance(parsed, dict):
        scrubbed = scrub_card_numbers(parsed)
        if scrubbed.get("isReceipt") is not True:
            return {"status": "not_a_receipt"}
        base = interpret_reading(scrubbed)
        detail = interpret_detail(scrubbed, tokens)
        merged = {**base, **detail}
        # Even if the total was cut off, merchant/date/ticket can still power advisories.
        if (
            merged.get("status") == "read"
            or merged.get("merchant")
            or merged.get("receipt_date")
            or merged.get("ticket_number")
        ):
            return merged

    ocr = run_ocr(image_bytes)
    return _reading_from_ocr_fallback(ocr, tokens)


def _reading_cache_key(
    image_sha256: str, tenant_id: int | None, tokens: set[str]
) -> str:
    tok_part = ",".join(sorted(tokens))
    digest = hashlib.sha256(
        f"{image_sha256}|{tenant_id or 0}|{tok_part}".encode("utf-8")
    ).hexdigest()
    return f"checkin-receipt-read:{digest}"


def _extract_reading(
    image_bytes: bytes,
    *,
    image_sha256: str,
    content_type: str,
    tenant_id: int | None,
) -> dict | None:
    hint, tokens = ("", set())
    if tenant_id:
        try:
            hint, tokens = brand_hint_for_tenant(tenant_id)
        except Exception:  # noqa: BLE001
            hint, tokens = ("", set())

    key = _reading_cache_key(image_sha256, tenant_id, tokens)
    try:
        cached = cache.get(key)
    except Exception:  # noqa: BLE001
        cached = None
    if isinstance(cached, dict):
        return cached

    prepared = downscale_image_bytes(image_bytes, max_dim=READ_MAX_DIM) or image_bytes
    future = _EXECUTOR.submit(
        _ask_vision_or_ocr,
        prepared,
        content_type="image/jpeg" if prepared is not image_bytes else content_type,
        brand_hint=hint,
        tokens=tokens,
    )
    try:
        reading = future.result(timeout=MODEL_DEADLINE_SECONDS)
    except Exception:  # noqa: BLE001 — deadline or runtime error degrades cleanly
        future.cancel()
        return None
    if isinstance(reading, dict):
        try:
            cache.set(key, reading, timeout=RESULT_CACHE_SECONDS)
        except Exception:  # noqa: BLE001
            pass
    return reading


# ─── Spend proposal from recaps.receipt_spend ───────────────────────


def propose_brand_spend(reading: dict) -> dict:
    """Use :func:`recaps.receipt_spend.summarize_receipt` to split brand spend
    from non-brand items and compute the proposed dollar figure."""
    line_items = reading.get("line_items") or []
    row = SimpleNamespace(
        id=0,
        custom_recap_file_id=0,
        custom_recap_file=SimpleNamespace(custom_recap_id=0, created_at=None),
        status=reading.get("status") or "unreadable",
        total=reading.get("total"),
        line_items=line_items,
        fees=reading.get("fees"),
        tax=reading.get("tax"),
        ticket_number=reading.get("ticket_number") or "",
        receipt_date=reading.get("receipt_date") or "",
        purchase_time=reading.get("purchase_time") or "",
        merchant=reading.get("merchant") or "",
        store_number=reading.get("store_number") or "",
        city=reading.get("city") or "",
    )
    summary = summarize_receipt([row])
    total: Decimal | None = summary.get("total")
    if not summary.get("counted") or total is None or total <= _ZERO:
        return {
            "proposed_spend": None,
            "receipt_total": total if total is not None and total > _ZERO else None,
            "brand_product": _ZERO,
            "excluded_non_brand_total": _ZERO,
            "excluded_non_brand_count": 0,
        }

    receipt_total = total.quantize(_CENT)
    brand_product: Decimal = summary["product"]
    non_brand_lines = [
        item
        for item in line_items
        if isinstance(item, dict)
        and not item.get("isBrand")
        and _d(item.get("amount")) > _ZERO
    ]
    positive_other = sum(
        (_d(item.get("amount")) for item in non_brand_lines), _ZERO
    ).quantize(_CENT)

    # Exclude non-brand items when the receipt itemizes BOTH brand product(s)
    # and non-brand item(s), so supply-only receipts (ice/cups) or unitemized
    # store codes still propose the receipt total rather than $0.00.
    if brand_product > _ZERO and _ZERO < positive_other < receipt_total:
        proposed = (receipt_total - positive_other).quantize(_CENT)
        return {
            "proposed_spend": proposed if proposed > _ZERO else None,
            "receipt_total": receipt_total,
            "brand_product": brand_product,
            "excluded_non_brand_total": positive_other,
            "excluded_non_brand_count": len(non_brand_lines),
        }

    return {
        "proposed_spend": receipt_total,
        "receipt_total": receipt_total,
        "brand_product": brand_product,
        "excluded_non_brand_total": _ZERO,
        "excluded_non_brand_count": 0,
    }


# ─── Pre-submit advisory checks ─────────────────────────────────────


def _shift_date_for(event) -> date | None:
    cal = checkin_web.event_calendar_date(event)
    if cal is not None:
        return cal
    start = getattr(event, "start_time", None)
    if isinstance(start, datetime):
        if start.tzinfo is not None:
            return start.astimezone(dt_timezone.utc).date()
        return start.date()
    return None


def _store_display(name: str, store_number: str = "") -> str:
    clean = (name or "").strip() or "Store"
    num = (store_number or "").strip().lstrip("#").lstrip("0")
    if num and not re.search(rf"#\s*0*{re.escape(num)}\b", clean):
        return f"{clean} #{num}"
    return clean


def check_store_mismatch(
    reading: dict,
    *,
    event,
    store_name_override: str = "",
    store_number_override: str = "",
) -> dict | None:
    """Compare receipt merchant / store # against the shift's venue."""
    merchant = (reading.get("merchant") or "").strip()
    r_num = parse_store_number(reading.get("store_number"))
    if not merchant and not r_num:
        return None

    tenant = getattr(event, "tenant", None)
    tenant_id = getattr(event, "tenant_id", None)
    if (
        tenant is not None
        and checkin_web.tenant_location_mode(tenant) == "market"
        and not (store_name_override or "").strip()
    ):
        return None

    r_address = (reading.get("address") or "").strip()
    req = getattr(event, "request", None)
    s_address = (
        getattr(event, "address", None)
        or getattr(req, "address", None)
        or getattr(req, "retailer_address", None)
        or ""
    ).strip()
    if r_address and s_address and same_street_address(r_address, s_address):
        return None

    retailers: list[tuple[int, str]] = []
    if tenant_id:
        retailers = list(
            Retailer.objects.filter(tenant_id=tenant_id)
            .exclude(name="")
            .order_by("id")
            .values_list("id", "name")[:500]
        )
    index = ChainIndex.for_tenant(tenant_id, retailers)

    r_chain, r_num_in_merchant = chain_in_text(merchant, index)
    if not r_chain and merchant:
        r_chain = index.canonical(merchant)
    r_num = r_num or r_num_in_merchant

    prefill = (
        checkin_web.store_identity_prefill(event)
        if event is not None
        else {"name": "", "number": ""}
    )
    event_retailer = getattr(getattr(event, "retailer", None), "name", "") or ""
    req_retailer = (
        getattr(getattr(req, "retailer", None), "name", "")
        or getattr(req, "retailer_name", "")
        or ""
    )
    event_name = (getattr(event, "name", "") or "").strip()
    typed_override = (store_name_override or "").strip()
    shift_store_name = (
        typed_override
        or event_retailer.strip()
        or req_retailer.strip()
        or (prefill.get("name") or "").strip()
        or title_store_text(event_name)
    )
    s_chain, s_num_in_name = chain_in_text(shift_store_name, index)
    if not s_chain and shift_store_name:
        s_chain = index.canonical(shift_store_name)
    if (not s_chain or not s_num_in_name) and event_name:
        e_chain, e_num = chain_in_text(event_name, index, builtin_only=True)
        if not s_chain:
            s_chain = e_chain
        if not s_num_in_name and (not s_chain or e_chain == s_chain):
            s_num_in_name = e_num

    s_num = (
        parse_store_number(store_number_override)
        or parse_store_number(prefill.get("number"))
        or s_num_in_name
    )

    etype_name = (getattr(getattr(event, "event_type", None), "name", "") or "").strip()
    non_retail_program = bool(_NON_RETAIL_PROGRAM_RE.search(etype_name))

    receipt_label = _store_display(r_chain or merchant, r_num)
    shift_label = _store_display(s_chain or shift_store_name or event_name, s_num)

    # 1. Both sides name known chains and they differ (e.g. Target vs Sprouts).
    if r_chain and s_chain and r_chain.casefold() != s_chain.casefold():
        return {
            "kind": "store_mismatch",
            "message": (
                f"This receipt says {receipt_label} — make sure it's from your "
                f"{shift_label} shift."
            ),
            "receiptStore": receipt_label,
            "shiftStore": shift_label,
        }

    # 2. Both sides carry a store # (without conflicting chains) and the # differs.
    if r_num and s_num and r_num != s_num:
        return {
            "kind": "store_mismatch",
            "message": (
                f"This receipt says {receipt_label} — make sure it's from your "
                f"{shift_label} shift."
            ),
            "receiptStore": receipt_label,
            "shiftStore": shift_label,
        }

    # 3. One side is a known retail chain (or BA explicitly typed the store name)
    # and the other side is a legible store name with zero shared venue words.
    # Skip non-retail event activations when no chain/typed store is on the shift
    # (buying ice at Target for a festival is normal).
    if non_retail_program and not s_chain and not typed_override:
        return None
    if not shift_store_name or not merchant:
        return None
    if r_chain or s_chain or typed_override:
        r_tokens = _venue_tokens(r_chain or merchant)
        s_tokens = _venue_tokens(s_chain or shift_store_name)
        if r_tokens and s_tokens and not (r_tokens & s_tokens):
            r_compact = re.sub(r"[^a-z0-9]", "", (r_chain or merchant).lower())
            s_compact = re.sub(r"[^a-z0-9]", "", (s_chain or shift_store_name).lower())
            if r_compact not in s_compact and s_compact not in r_compact:
                return {
                    "kind": "store_mismatch",
                    "message": (
                        f"This receipt says {receipt_label} — make sure it's from your "
                        f"{shift_label} shift."
                    ),
                    "receiptStore": receipt_label,
                    "shiftStore": shift_label,
                }
    return None


def check_date_mismatch(reading: dict, *, event) -> dict | None:
    """Compare printed receipt date against the shift's calendar date."""
    receipt_day = _parse_iso_day(reading.get("receipt_date"))
    shift_day = _shift_date_for(event)
    if receipt_day is None or shift_day is None or receipt_day == shift_day:
        return None
    return {
        "kind": "date_mismatch",
        "message": (
            f"This receipt is dated {_day_label(receipt_day)} — make sure it's from "
            f"your {_day_label(shift_day)} shift."
        ),
        "receiptDate": receipt_day.isoformat(),
        "shiftDate": shift_day.isoformat(),
    }


def _own_shift_recap_ids(event, ambassador) -> tuple[set[int], set[int]]:
    """``(custom_recap_ids, legacy_recap_ids)`` already attached to this BA's
    current shift (plus co-activation slices), so re-uploads never self-flag."""
    event_id = getattr(event, "id", None)
    amb_id = getattr(ambassador, "id", None)
    if not event_id:
        return set(), set()

    custom_qs = CustomRecap.objects.filter(event_id=event_id)
    legacy_qs = Recap.objects.filter(event_id=event_id)
    if amb_id:
        custom_qs = custom_qs.filter(ambassador_id=amb_id)
        legacy_qs = legacy_qs.filter(ambassador_id=amb_id)

    custom_ids: set[int] = set()
    for recap in custom_qs[:20]:
        try:
            custom_ids |= _family_ids(recap)
        except Exception:  # noqa: BLE001
            custom_ids.add(recap.id)
    legacy_ids = set(legacy_qs.values_list("id", flat=True)[:20])
    return custom_ids, legacy_ids


def check_duplicate_receipt(
    reading: dict | None,
    *,
    digests: set[str],
    event,
    ambassador,
) -> dict | None:
    """Check SHA-256 file hashes and OCR receipt keys against prior submissions."""
    own_custom_ids, own_legacy_ids = _own_shift_recap_ids(event, ambassador)
    clean_digests = {d for d in (normalize_content_sha256(x) for x in digests) if d}

    # 1. Exact SHA-256 match against prior custom recaps, legacy recaps, or consumer receipts.
    if clean_digests:
        prior_custom = (
            CustomRecapFile.objects.filter(
                content_sha256__in=clean_digests,
                custom_recap__isnull=False,
            )
            .exclude(custom_recap_id__in=own_custom_ids)
            .select_related("custom_recap__event")
            .order_by("created_at", "id")
            .first()
        )
        if prior_custom is not None:
            prior_day = _shift_date_for(
                getattr(prior_custom.custom_recap, "event", None)
            )
            when = f" ({_day_label(prior_day)})" if prior_day else ""
            return {
                "kind": "duplicate_receipt",
                "message": (
                    f"This receipt photo matches one already submitted on another recap{when} "
                    "— make sure it wasn't uploaded twice."
                ),
                "matchedOn": "sha256",
            }

        prior_legacy = (
            RecapFile.objects.filter(
                content_sha256__in=clean_digests,
                recap__isnull=False,
            )
            .exclude(recap_id__in=own_legacy_ids)
            .select_related("recap__event")
            .order_by("created_at", "id")
            .first()
        )
        if prior_legacy is not None:
            prior_day = _shift_date_for(getattr(prior_legacy.recap, "event", None))
            when = f" ({_day_label(prior_day)})" if prior_day else ""
            return {
                "kind": "duplicate_receipt",
                "message": (
                    f"This receipt photo matches one already submitted on another recap{when} "
                    "— make sure it wasn't uploaded twice."
                ),
                "matchedOn": "sha256",
            }

        prior_consumer = (
            ConsumerReceipt.objects.filter(image_sha256__in=clean_digests)
            .order_by("submitted_at", "id")
            .first()
        )
        if prior_consumer is not None:
            return {
                "kind": "duplicate_receipt",
                "message": (
                    "This receipt photo matches an earlier submission "
                    "— make sure it wasn't uploaded twice."
                ),
                "matchedOn": "sha256",
            }

    # 2. OCR duplicate keys (fingerprint / ticket_key / loose_key) against ReceiptReading.
    if isinstance(reading, dict):
        keys_q = Q()
        has_key = False
        for field in ("fingerprint", "ticket_key", "loose_key"):
            val = (reading.get(field) or "").strip()
            if val:
                keys_q |= Q(**{field: val})
                has_key = True
        if has_key:
            twin = (
                ReceiptReading.objects.filter(
                    keys_q,
                    custom_recap_file__custom_recap__isnull=False,
                )
                .exclude(custom_recap_file__custom_recap_id__in=own_custom_ids)
                .select_related("custom_recap_file__custom_recap__event")
                .order_by("custom_recap_file__created_at", "custom_recap_file_id")
                .first()
            )
            if twin is not None:
                other_recap = twin.custom_recap_file.custom_recap
                prior_day = _shift_date_for(getattr(other_recap, "event", None))
                when = f" ({_day_label(prior_day)})" if prior_day else ""
                return {
                    "kind": "duplicate_receipt",
                    "message": (
                        f"This receipt matches a ticket already submitted on another recap{when} "
                        "— make sure it's from this shift."
                    ),
                    "matchedOn": "ocr_key",
                }
    return None


# ─── Public entry point ─────────────────────────────────────────────


def read_checkin_receipt(
    image_bytes: bytes,
    *,
    content_type: str = "image/jpeg",
    event,
    ambassador=None,
    client_sha256: str = "",
    store_name: str = "",
    store_number: str = "",
) -> dict:
    """Read one check-in receipt photo, propose brand spend, and build advisories.

    Never raises: any error returns a clean ``available: False`` payload so the
    check-in recap form keeps working offline or without AI/OCR credentials.
    """
    try:
        clean_client_sha = normalize_content_sha256(client_sha256)
        if not image_bytes or len(image_bytes) > MAX_IMAGE_BYTES:
            dup = (
                check_duplicate_receipt(
                    None, digests={clean_client_sha}, event=event, ambassador=ambassador
                )
                if clean_client_sha
                else None
            )
            return _unreadable_payload(
                advisories=[dup] if dup else [],
                sha256=clean_client_sha,
            )
        if content_type and not content_type.lower().startswith("image/"):
            return _unreadable_payload(sha256=clean_client_sha)

        image_sha = hashlib.sha256(image_bytes).hexdigest()
        digests = {h for h in (image_sha, clean_client_sha) if h}
        tenant_id = getattr(event, "tenant_id", None)

        reading = _extract_reading(
            image_bytes,
            image_sha256=image_sha,
            content_type=content_type,
            tenant_id=tenant_id,
        )

        advisories: list[dict] = []
        dup = check_duplicate_receipt(
            reading, digests=digests, event=event, ambassador=ambassador
        )
        if dup is not None:
            advisories.append(dup)

        if not isinstance(reading, dict) or reading.get("status") == "not_a_receipt":
            return _unreadable_payload(advisories=advisories, sha256=image_sha)

        store_adv = check_store_mismatch(
            reading,
            event=event,
            store_name_override=store_name,
            store_number_override=store_number,
        )
        if store_adv is not None:
            advisories.append(store_adv)

        date_adv = check_date_mismatch(reading, event=event)
        if date_adv is not None:
            advisories.append(date_adv)

        spend = propose_brand_spend(reading)
        dedupe_key = (
            reading.get("fingerprint")
            or reading.get("ticket_key")
            or reading.get("loose_key")
            or image_sha
        )
        proposed_str = _money_str(spend["proposed_spend"])
        if proposed_str is None:
            return {
                **_unreadable_payload(advisories=advisories, sha256=dedupe_key),
                "merchant": reading.get("merchant") or "",
                "storeNumber": reading.get("store_number") or "",
                "receiptDate": reading.get("receipt_date") or "",
            }

        excluded_str = (
            _money_str(spend["excluded_non_brand_total"])
            if spend["excluded_non_brand_count"] > 0
            else None
        )
        return {
            "available": True,
            "proposedSpend": proposed_str,
            "receiptTotal": _money_str(spend["receipt_total"]),
            "brandProductSubtotal": _money_str(spend["brand_product"]),
            "excludedNonBrandTotal": excluded_str,
            "excludedNonBrandCount": int(spend["excluded_non_brand_count"]),
            "merchant": reading.get("merchant") or "",
            "storeNumber": reading.get("store_number") or "",
            "receiptDate": reading.get("receipt_date") or "",
            "dedupeKey": dedupe_key,
            "advisories": advisories,
            "message": "",
        }
    except Exception:  # noqa: BLE001 — advisory; never block recap submission
        logger.exception("checkin receipt read failed")
        return _unreadable_payload()
