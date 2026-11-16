"""Read the counts off a photo of a tally sheet, for the check-in recap form.

An ambassador photographs the sheet they kept during the shift (handwritten
or printed counts) and taps "Fill from photo". This module asks the vision
model for the number written beside each of the form's own NUMBER fields and
per-product counts, and returns them as *proposals*. Nothing is saved here:
the form shows each proposal beside its field and the ambassador accepts or
ignores it. The photo itself is not stored.

Rules, all enforced in code after the model answers:

* only number fields of this template (and its product list) can be filled;
  the model returns a reference from the list it was handed, and any other
  reference is dropped;
* a value is kept only when the model marks it clearly legible AND it parses
  as a plain, plausible number. Anything else is left blank, never guessed;
* the same reference answered twice with different numbers is dropped;
* the model is sent field labels and product names only: no ambassador, no
  store, no session, and it is told to ignore people, names and phone numbers.

Every failure (no key, slow, refused, odd output) ends in the same plain
"could not read" result. The whole model step is bounded by
:data:`MODEL_DEADLINE_SECONDS`.
"""

from __future__ import annotations

import concurrent.futures
import hashlib
import logging
import re
from decimal import Decimal, InvalidOperation

from django.core.cache import cache

logger = logging.getLogger(__name__)

TALLY_UNREADABLE = "Could not read that photo. Enter the numbers by hand."

# The whole model step may take this long, then the ambassador is told to
# type the numbers. The page gives up on the request at 8 seconds.
MODEL_DEADLINE_SECONDS = 6.5
# Per HTTP attempt; under the deadline so a slow attempt fails inside it.
_MODEL_REQUEST_TIMEOUT = 6.0
_MODEL_MAX_TOKENS = 600

# The longest side sent to the model. Handwriting needs more pixels than a
# shelf photo; this is still a small fraction of a phone photo.
TALLY_MAX_DIM = 1600
MAX_IMAGE_BYTES = 4_000_000

MAX_FIELD_TARGETS = 30
MAX_PRODUCT_TARGETS = 30
_MAX_LABEL_CHARS = 120
# A count above this on one sampling shift is a misread, not a tally.
MAX_PLAUSIBLE_COUNT = 100_000

# The same photo against the same form is answered from here, not read again.
RESULT_CACHE_SECONDS = 15 * 60

_EXECUTOR = concurrent.futures.ThreadPoolExecutor(
    max_workers=4, thread_name_prefix="tally-read"
)

_NUMERIC_TYPE_RE = re.compile(r"number|numeric|int|decimal|float", re.IGNORECASE)
# Number-typed fields that are not a count from the shift.
_NOT_A_TALLY_RE = re.compile(
    r"phone|\bzip\b|postal|\bstore\s*(number|no\b|#)|\bid\b|employee|badge|"
    r"mileage|\bmiles?\b|odometer|\bname\b|e-?mail|\bcontact\b|address",
    re.IGNORECASE,
)
# Fields that hold money may carry cents. Everything else is a whole number.
_MONEY_FIELD_RE = re.compile(r"spend|spent|price|cost|\$|amount|total\s+sales", re.IGNORECASE)
_WHOLE_RE = re.compile(r"\d{1,6}")
_MONEY_RE = re.compile(r"\d{1,6}(\.\d{1,2})?")

TALLY_SYSTEM = (
    "You read a photo of a tally sheet from an in-store product sampling "
    "shift: handwritten or printed counts. You are given the lines of a "
    "form, each with a reference. For each line whose number is written on "
    "the sheet, return its reference and the number exactly as written. "
    "Set legible to true only when you can read every digit with certainty; "
    "for tally marks, only when every mark is distinct. Do not add numbers "
    "together, estimate, or infer a number that is not written. Leave out "
    "any line that is not on the sheet. If the photo is not a tally sheet or "
    "list of counts, set isTally to false and return no readings. Do not "
    "identify or describe any person. Ignore names, phone numbers and "
    "signatures."
)


def _unreadable() -> dict:
    return {
        "available": False,
        "values": [],
        "productSamples": [],
        "message": TALLY_UNREADABLE,
    }


def _label(text: object) -> str:
    return " ".join(str(text or "").split())[:_MAX_LABEL_CHARS]


def tally_targets(template: dict | None, *, include_products: bool = True) -> list[dict]:
    """The lines the model may fill, from the serialized check-in template:
    ``[{ref, kind: "field"|"product", id, label, money}]``."""
    template = template or {}
    targets: list[dict] = []
    for section in template.get("sections") or []:
        for field in (section or {}).get("fields") or []:
            if not isinstance(field, dict):
                continue
            name = _label(field.get("name"))
            if not name or not _NUMERIC_TYPE_RE.search(str(field.get("type") or "")):
                continue
            if _NOT_A_TALLY_RE.search(name):
                continue
            if len(targets) >= MAX_FIELD_TARGETS:
                break
            targets.append(
                {
                    "ref": f"f{len(targets) + 1}",
                    "kind": "field",
                    "id": str(field.get("id") or ""),
                    "label": name,
                    "money": bool(_MONEY_FIELD_RE.search(name)),
                }
            )
    if include_products:
        count = 0
        for product in template.get("products") or []:
            if not isinstance(product, dict):
                continue
            name = _label(product.get("name"))
            if not name or count >= MAX_PRODUCT_TARGETS:
                continue
            count += 1
            targets.append(
                {
                    "ref": f"p{count}",
                    "kind": "product",
                    "id": str(product.get("id") or ""),
                    "label": name,
                    "money": False,
                }
            )
    return [t for t in targets if t["id"]]


def _schema(refs: list[str]) -> dict:
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["isTally", "readings"],
        "properties": {
            "isTally": {"type": "boolean"},
            "readings": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["ref", "value", "legible"],
                    "properties": {
                        "ref": {"type": "string", "enum": refs},
                        # The number as written, digits only, e.g. "42".
                        "value": {"type": "string"},
                        "legible": {"type": "boolean"},
                    },
                },
            },
        },
    }


def compose_prompt(targets: list[dict]) -> str:
    lines = ["Form lines:"]
    for target in targets:
        if target["kind"] == "product":
            lines.append(f"- [{target['ref']}] count for product: {target['label']}")
        else:
            lines.append(f"- [{target['ref']}] {target['label']}")
    lines.append("Return a reading only for lines whose number is written on the sheet.")
    return "\n".join(lines)


def parse_count(raw: object, *, money: bool = False) -> str | None:
    """A written number → the string to put in the field, or None."""
    text = str(raw if raw is not None else "").replace(",", "").replace("$", "").strip()
    if not (_MONEY_RE if money else _WHOLE_RE).fullmatch(text):
        return None
    try:
        value = Decimal(text)
    except InvalidOperation:
        return None
    if value < 0 or value > MAX_PLAUSIBLE_COUNT:
        return None
    if money and "." in text:
        return f"{value.quantize(Decimal('0.01'))}"
    return str(int(value))


def interpret_tally(parsed: dict | None, targets: list[dict]) -> dict:
    """Model output → the proposals. Pure. Anything short of a clearly
    legible, plausible number for a known line is left out."""
    if not isinstance(parsed, dict) or parsed.get("isTally") is not True:
        return _unreadable()
    readings = parsed.get("readings")
    if not isinstance(readings, list):
        return _unreadable()
    by_ref = {t["ref"]: t for t in targets}
    kept: dict[str, str] = {}
    dropped: set[str] = set()
    for reading in readings:
        if not isinstance(reading, dict):
            continue
        ref = str(reading.get("ref") or "")
        target = by_ref.get(ref)
        if target is None or ref in dropped:
            continue
        if reading.get("legible") is not True:
            continue
        value = parse_count(reading.get("value"), money=target["money"])
        if value is None:
            continue
        if ref in kept and kept[ref] != value:
            # Two different numbers for one line: neither is trustworthy.
            kept.pop(ref)
            dropped.add(ref)
            continue
        kept[ref] = value
    if not kept:
        return _unreadable()
    values = [
        {"fieldId": by_ref[ref]["id"], "value": value}
        for ref, value in kept.items()
        if by_ref[ref]["kind"] == "field"
    ]
    samples = [
        {"productId": by_ref[ref]["id"], "value": value}
        for ref, value in kept.items()
        if by_ref[ref]["kind"] == "product"
    ]
    return {"available": True, "values": values, "productSamples": samples, "message": ""}


def _ask_model(image_bytes: bytes, targets: list[dict]) -> dict | None:
    from utils.ai_text import generate_vision_json

    return generate_vision_json(
        TALLY_SYSTEM,
        compose_prompt(targets),
        image_bytes=image_bytes,
        content_type="image/jpeg",
        schema=_schema([t["ref"] for t in targets]),
        max_tokens=_MODEL_MAX_TOKENS,
        # Reading handwriting: the main model. A wrong digit is worse than
        # a blank.
        timeout=_MODEL_REQUEST_TIMEOUT,
    )


def _cache_key(image_bytes: bytes, targets: list[dict]) -> str:
    digest = hashlib.sha256(image_bytes)
    for target in targets:
        digest.update(f"|{target['kind']}:{target['id']}:{target['label']}".encode("utf-8"))
    return f"tally-read:{digest.hexdigest()}"


def read_tally_photo(
    image_bytes: bytes,
    *,
    content_type: str = "image/jpeg",
    template: dict | None,
    include_products: bool = True,
) -> dict:
    """One tally photo → proposals for the form's number fields. Never raises.

    ``{"available", "values": [{fieldId, value}], "productSamples":
    [{productId, value}], "message"}``. ``available`` is False, with the
    plain message, whenever nothing could be read.
    """
    try:
        if not image_bytes or len(image_bytes) > MAX_IMAGE_BYTES:
            return _unreadable()
        if content_type and not content_type.startswith("image/"):
            return _unreadable()
        targets = tally_targets(template, include_products=include_products)
        if not targets:
            return _unreadable()

        key = _cache_key(image_bytes, targets)
        try:
            cached = cache.get(key)
        except Exception:  # noqa: BLE001
            cached = None
        if isinstance(cached, dict):
            return cached

        from recaps.pdf import downscale_image_bytes

        small = downscale_image_bytes(image_bytes, max_dim=TALLY_MAX_DIM, quality=82) or image_bytes
        future = _EXECUTOR.submit(_ask_model, small, targets)
        try:
            parsed = future.result(timeout=MODEL_DEADLINE_SECONDS)
        except Exception:  # noqa: BLE001 — deadline, no key, anything else
            future.cancel()
            return _unreadable()
        if parsed is None:
            # A failed call, not an answer: do not remember it.
            return _unreadable()
        result = interpret_tally(parsed, targets)
        try:
            cache.set(key, result, timeout=RESULT_CACHE_SECONDS)
        except Exception:  # noqa: BLE001
            pass
        return result
    except Exception:  # noqa: BLE001 — advisory; the form works without it
        logger.exception("tally read failed")
        return _unreadable()
