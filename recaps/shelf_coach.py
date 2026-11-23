"""Real-time aisle coaching for a walk-up shelf photo.

Called when a BA adds a shelf-bucket photo (sibling of ``photo-check``), never
on page load. Returns advisory nudges from the same vision schema
:mod:`recaps.shelf_check` uses. Never rejects an upload and never blocks
submit.

When the photo is already filed as a :class:`recaps.models.CustomRecapFile`,
the coach optionally upserts a :class:`recaps.models.ShelfReading` so the
background cron and staff review share one truth.
"""

from __future__ import annotations

import logging

from utils.ai_text import AiUnavailable

from recaps.shelf_check import (
    NO,
    UNCLEAR,
    YES,
    interpret_shelf,
)

logger = logging.getLogger(__name__)

SHELF_COACH_UNAVAILABLE = "Shelf coach unavailable"

# Short request budget for field LTE; cron still does the deep pass.
COACH_TIMEOUT = 12.0
COACH_MAX_TOKENS = 220


def _unavailable(reason: str = SHELF_COACH_UNAVAILABLE) -> dict:
    return {
        "available": False,
        "reason": reason,
        "status": None,
        "answers": None,
        "nudges": [],
        "message": reason,
        "persisted": False,
    }


def coach_nudges(answer: dict) -> list[dict]:
    """Turn an interpreted shelf answer into BA-facing advisory chips.

    Pure. Codes are stable for the front-end; messages are human copy.
    """
    status = answer.get("status")
    nudges: list[dict] = []
    if status == "failed":
        return nudges
    if status == "unreadable":
        nudges.append(
            {
                "code": "not_shelf",
                "severity": "warn",
                "message": (
                    "This doesn't look like a shelf or cooler photo. "
                    "Retake a clear shot of the product on the shelf, or keep it."
                ),
            }
        )
        return nudges

    on_shelf = answer.get("product_on_shelf") or UNCLEAR
    tag = answer.get("price_tag_visible") or UNCLEAR
    gap = answer.get("out_of_stock_gap") or UNCLEAR

    if on_shelf == NO:
        nudges.append(
            {
                "code": "product_missing",
                "severity": "warn",
                "message": (
                    "Product doesn't appear on the shelf. "
                    "Ask store staff to restock if you can, then retake — or keep this photo."
                ),
            }
        )
    elif on_shelf == UNCLEAR:
        nudges.append(
            {
                "code": "product_unclear",
                "severity": "info",
                "message": (
                    "Hard to tell if the product is on the shelf. "
                    "A closer, brighter shot helps — retake or keep."
                ),
            }
        )

    if tag == NO:
        nudges.append(
            {
                "code": "no_price_tag",
                "severity": "warn",
                "message": (
                    "No price tag detected. Ask store staff for a shelf tag "
                    "before clocking out, then retake — or keep this photo."
                ),
            }
        )
    elif tag == UNCLEAR and on_shelf == YES:
        nudges.append(
            {
                "code": "price_unclear",
                "severity": "info",
                "message": (
                    "Price tag isn't clear. Include the price strip in the frame "
                    "if you can — retake or keep."
                ),
            }
        )

    if gap == YES:
        nudges.append(
            {
                "code": "oos_gap",
                "severity": "warn",
                "message": (
                    "Looks like an empty gap where the product belongs. "
                    "Note it for the recap and pull stock from the back if allowed."
                ),
            }
        )

    return nudges


def _primary_message(nudges: list[dict], answer: dict) -> str:
    if nudges:
        return nudges[0]["message"]
    if answer.get("status") == "read" and answer.get("product_on_shelf") == YES:
        tag = answer.get("price_tag_visible")
        if tag == YES:
            return "Shelf looks good — product and price tag visible."
        return "Product looks on the shelf."
    return "Shelf photo checked."


def _public_answers(answer: dict) -> dict | None:
    status = answer.get("status")
    if status not in ("read", "unreadable"):
        return None
    if status == "unreadable":
        return {
            "productOnShelf": UNCLEAR,
            "facings": None,
            "priceTagVisible": UNCLEAR,
            "price": None,
            "outOfStockGap": UNCLEAR,
        }
    price = answer.get("price")
    return {
        "productOnShelf": answer.get("product_on_shelf") or UNCLEAR,
        "facings": answer.get("facings"),
        "priceTagVisible": answer.get("price_tag_visible") or UNCLEAR,
        "price": str(price) if price is not None else None,
        "outOfStockGap": answer.get("out_of_stock_gap") or UNCLEAR,
    }


def coach_shelf_photo(
    image_bytes: bytes,
    *,
    brand: str,
    product_names: list[str],
    content_type: str = "image/jpeg",
) -> dict:
    """One shelf photo → advisory payload. Never raises; never rejects.

    Transient vision failures return ``available=False`` and do not include
    an ``_answer`` so callers never persist ``status=failed`` (which would
    burn cron ``MAX_ATTEMPTS``).
    """
    if not image_bytes:
        return _unavailable()
    mime = (content_type or "image/jpeg").strip() or "image/jpeg"
    if not mime.startswith("image/"):
        mime = "image/jpeg"
    try:
        from utils.ai_text import generate_vision_json
        from recaps.shelf_check import (
            SHELF_SCHEMA,
            SHELF_SYSTEM,
            compose_prompt,
        )

        try:
            parsed = generate_vision_json(
                SHELF_SYSTEM,
                compose_prompt(brand, product_names),
                image_bytes=image_bytes,
                content_type=mime,
                schema=SHELF_SCHEMA,
                max_tokens=COACH_MAX_TOKENS,
                timeout=COACH_TIMEOUT,
            )
        except AiUnavailable:
            return _unavailable()
        except Exception:  # noqa: BLE001
            parsed = None
        answer = interpret_shelf(parsed)
    except AiUnavailable:
        return _unavailable()
    except Exception as exc:  # noqa: BLE001
        logger.warning("shelf coach failed: %s", exc)
        return _unavailable()

    # Soft model/HTTP miss → unavailable (leave unread for cron). Never
    # report available=true with status=failed.
    if answer.get("status") == "failed":
        return _unavailable()

    nudges = coach_nudges(answer)
    return {
        "available": True,
        "reason": "",
        "status": answer.get("status"),
        "answers": _public_answers(answer),
        "nudges": nudges,
        "message": _primary_message(nudges, answer),
        "persisted": False,
        "_answer": answer,  # internal — stripped before JSON response
    }


def persist_shelf_reading(custom_recap_file, answer: dict) -> bool:
    """Upsert a ``ShelfReading`` for a filed photo. Returns True when saved.

    Only persists ``read`` / ``unreadable``. Never writes ``failed`` (leave
    for cron retry without burning attempts). Never overwrites a staff
    correction.
    """
    from recaps.models import ShelfReading, ShelfReadingCorrection

    if custom_recap_file is None:
        return False
    status = answer.get("status")
    if status not in (
        ShelfReading.STATUS_READ,
        ShelfReading.STATUS_UNREADABLE,
    ):
        return False
    if ShelfReadingCorrection.objects.filter(custom_recap_file=custom_recap_file).exists():
        return False

    fields = {
        "product_on_shelf": answer.get("product_on_shelf") or UNCLEAR,
        "facings": answer.get("facings"),
        "price_tag_visible": answer.get("price_tag_visible") or UNCLEAR,
        "price": answer.get("price"),
        "out_of_stock_gap": answer.get("out_of_stock_gap") or UNCLEAR,
    }
    existing = ShelfReading.objects.filter(custom_recap_file=custom_recap_file).first()
    attempts = (existing.attempts if existing else 0) + 1
    ShelfReading.objects.update_or_create(
        custom_recap_file=custom_recap_file,
        defaults={"status": status, "attempts": attempts, **fields},
    )
    return True


def pack_response(result: dict, *, persisted: bool = False) -> dict:
    """Public JSON shape (no internal ``_answer``)."""
    return {
        "available": bool(result.get("available")),
        "reason": result.get("reason") or "",
        "status": result.get("status"),
        "answers": result.get("answers"),
        "nudges": list(result.get("nudges") or []),
        "message": result.get("message") or "",
        "persisted": bool(persisted),
    }
