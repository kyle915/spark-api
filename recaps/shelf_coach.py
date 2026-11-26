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
MODE_SHELF = "shelf"
MODE_SETUP = "setup"

_SETUP_BUCKET_WORDS = (
    "setup",
    "set up",
    "set-up",
    "footprint",
    "table",
    "booth",
    "display",
    "stand",
    "banner",
    "tablecloth",
    "activation",
)
_SHELF_BUCKET_WORDS = (
    "shelf",
    "stock",
    "on-shelf",
    "cooler",
    "aisle",
    "endcap",
)
_NON_COACH_WORDS = ("receipt", "expense", "product spend", "tally")

SETUP_SYSTEM = (
    "You inspect one field sampling setup photo (demo table, display, booth, "
    "or event footprint) taken during a retail or event activation. Answer "
    "only from what is clearly visible; when unsure, answer unclear and never "
    "guess. Do not identify, describe, or mention any person or face; if "
    "people are in the frame, ignore them completely. "
    "isSetupPhoto: true when the photo shows a sampling table, booth, display, "
    "cooler/activation stand, or product setup; false if unrelated (such as a "
    "receipt, blank wall, floor, or close-up text sheet). "
    "photoClear: yes when focused and well-lit, no when noticeably blurry, "
    "very dark, or obstructed, unclear otherwise. "
    "productVisible: yes when packages, cans, bottles, or samples of the "
    "brand's product are visible on the setup, no when the table/setup is "
    "visible with no product on it, unclear otherwise. "
    "brandingVisible: yes when brand labels, signage, display box, banner, or "
    "branded tablecloth face the camera legibly, no when labels/signage are "
    "turned away or absent, unclear otherwise."
)

SETUP_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "isSetupPhoto",
        "photoClear",
        "productVisible",
        "brandingVisible",
    ],
    "properties": {
        "isSetupPhoto": {"type": "boolean"},
        "photoClear": {"type": "string", "enum": [YES, NO, UNCLEAR]},
        "productVisible": {"type": "string", "enum": [YES, NO, UNCLEAR]},
        "brandingVisible": {"type": "string", "enum": [YES, NO, UNCLEAR]},
    },
}

# Short request budget for field LTE; cron still does the deep pass.
COACH_TIMEOUT = 12.0
COACH_MAX_TOKENS = 220


def detect_coach_mode(mode: object = None, bucket_name: object = None) -> str:
    """Resolve ``"shelf"`` vs ``"setup"`` coaching mode. Defaults to ``"shelf"``."""
    explicit = str(mode or "").strip().lower()
    if explicit in (MODE_SHELF, MODE_SETUP):
        return explicit
    label = str(bucket_name or "").strip().lower()
    if not label or any(w in label for w in _NON_COACH_WORDS):
        return MODE_SHELF
    if any(w in label for w in _SHELF_BUCKET_WORDS):
        return MODE_SHELF
    if any(w in label for w in _SETUP_BUCKET_WORDS):
        return MODE_SETUP
    return MODE_SHELF


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


def interpret_setup(parsed: dict | None) -> dict:
    """Model output → interpreted setup check dict. Pure."""
    blank = {
        "photo_clear": UNCLEAR,
        "product_visible": UNCLEAR,
        "branding_visible": UNCLEAR,
    }
    if not isinstance(parsed, dict):
        return {"status": "failed", **blank}
    if parsed.get("isSetupPhoto") is not True:
        return {"status": "unreadable", **blank}
    valid = (YES, NO, UNCLEAR)
    clear = parsed.get("photoClear")
    prod = parsed.get("productVisible")
    brand_vis = parsed.get("brandingVisible")
    return {
        "status": "read",
        "photo_clear": clear if clear in valid else UNCLEAR,
        "product_visible": prod if prod in valid else UNCLEAR,
        "branding_visible": brand_vis if brand_vis in valid else UNCLEAR,
    }


def setup_coach_nudges(answer: dict) -> list[dict]:
    """Turn an interpreted setup photo answer into BA-facing advisory nudges."""
    status = answer.get("status")
    nudges: list[dict] = []
    if status == "failed":
        return nudges
    if status == "unreadable":
        nudges.append(
            {
                "code": "not_setup",
                "severity": "warn",
                "message": (
                    "This doesn't look like a table or display setup photo. "
                    "Retake a wide shot of your setup, or keep it."
                ),
            }
        )
        return nudges

    clear = answer.get("photo_clear") or UNCLEAR
    prod = answer.get("product_visible") or UNCLEAR
    branding = answer.get("branding_visible") or UNCLEAR

    if clear == NO:
        nudges.append(
            {
                "code": "blurry_or_dark",
                "severity": "warn",
                "message": (
                    "Photo looks blurry or dark. Step back into better light and "
                    "retake while you're set up — or keep this photo."
                ),
            }
        )
    if prod == NO:
        nudges.append(
            {
                "code": "product_missing_on_setup",
                "severity": "warn",
                "message": (
                    "Brand product isn't clearly visible on the table or display. "
                    "Include samples or packages in frame and retake — or keep this photo."
                ),
            }
        )
    elif branding == NO and prod == YES:
        nudges.append(
            {
                "code": "labels_turned_away",
                "severity": "info",
                "message": (
                    "Product labels or signage aren't facing the camera. "
                    "Turn cans or signage forward for a cleaner client photo — retake or keep."
                ),
            }
        )
    return nudges


def _setup_primary_message(nudges: list[dict], answer: dict) -> str:
    if nudges:
        return nudges[0]["message"]
    if answer.get("status") == "read" and answer.get("product_visible") == YES:
        return "Setup looks sharp — display and product clearly visible."
    return "Setup photo checked."


def _setup_public_answers(answer: dict) -> dict | None:
    status = answer.get("status")
    if status not in ("read", "unreadable"):
        return None
    return {
        "productOnShelf": answer.get("product_visible") or UNCLEAR,
        "facings": None,
        "priceTagVisible": answer.get("branding_visible") or UNCLEAR,
        "price": None,
        "outOfStockGap": NO if answer.get("photo_clear") == YES else UNCLEAR,
    }


def coach_shelf_photo(
    image_bytes: bytes,
    *,
    brand: str,
    product_names: list[str],
    content_type: str = "image/jpeg",
    mode: str = MODE_SHELF,
) -> dict:
    """One shelf or setup photo → advisory payload. Never raises; never rejects.

    Transient vision failures return ``available=False`` and do not include
    an ``_answer`` so callers never persist ``status=failed`` (which would
    burn cron ``MAX_ATTEMPTS``).
    """
    if not image_bytes:
        return _unavailable()
    mime = (content_type or "image/jpeg").strip() or "image/jpeg"
    if not mime.startswith("image/"):
        mime = "image/jpeg"
    resolved_mode = detect_coach_mode(mode)
    try:
        from utils.ai_text import generate_vision_json
        from recaps.shelf_check import (
            SHELF_SCHEMA,
            SHELF_SYSTEM,
            compose_prompt,
        )

        system = SETUP_SYSTEM if resolved_mode == MODE_SETUP else SHELF_SYSTEM
        schema = SETUP_SCHEMA if resolved_mode == MODE_SETUP else SHELF_SCHEMA
        try:
            parsed = generate_vision_json(
                system,
                compose_prompt(brand, product_names),
                image_bytes=image_bytes,
                content_type=mime,
                schema=schema,
                max_tokens=COACH_MAX_TOKENS,
                timeout=COACH_TIMEOUT,
            )
        except AiUnavailable:
            return _unavailable()
        except Exception:  # noqa: BLE001
            parsed = None
        answer = (
            interpret_setup(parsed)
            if resolved_mode == MODE_SETUP
            else interpret_shelf(parsed)
        )
    except AiUnavailable:
        return _unavailable()
    except Exception as exc:  # noqa: BLE001
        logger.warning("shelf coach failed: %s", exc)
        return _unavailable()

    # Soft model/HTTP miss → unavailable (leave unread for cron). Never
    # report available=true with status=failed.
    if answer.get("status") == "failed":
        return _unavailable()

    if resolved_mode == MODE_SETUP:
        nudges = setup_coach_nudges(answer)
        return {
            "available": True,
            "reason": "",
            "mode": MODE_SETUP,
            "status": answer.get("status"),
            "answers": _setup_public_answers(answer),
            "nudges": nudges,
            "message": _setup_primary_message(nudges, answer),
            "persisted": False,
        }

    nudges = coach_nudges(answer)
    return {
        "available": True,
        "reason": "",
        "mode": MODE_SHELF,
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
