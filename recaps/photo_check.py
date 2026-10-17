"""Advisory photo classification for a walk-up recap capture.

Called when a BA adds a photo, never when a page loads. The result is a
suggestion: which of the tenant's buckets the shot looks like, and whether a
shelf shot appears to show product. It never rejects an upload, never approves
or files a recap, and never asks a model to identify a person.

Two ways to see the photo, in order:

1. OpenAI vision via :func:`utils.ai_text.generate_vision_json` when
   ``OPENAI_API_KEY`` is set (the same key the rest of the AI text helpers use).
2. Google Cloud Vision ``LABEL_DETECTION`` with Application Default Credentials,
   the same REST pattern as :mod:`receipts.ocr`. Face detection is not requested.

If neither can run, the result is ``available=False`` and the reason
``Photo check unavailable``. The photo still saves.
"""

from __future__ import annotations

import base64
import logging
from typing import Literal

from django.conf import settings

from utils.ai_text import AiUnavailable, generate_vision_json

logger = logging.getLogger(__name__)

PHOTO_CHECK_UNAVAILABLE = "Photo check unavailable"

BucketKind = Literal["shelf", "table_setup", "sampling", "receipt", "other"]

_KINDS: tuple[BucketKind, ...] = (
    "shelf",
    "table_setup",
    "sampling",
    "receipt",
    "other",
)

# LABEL_DETECTION only. Do not add FACE_DETECTION or anything that returns
# a person identity.
VISION_FEATURES = [{"type": "LABEL_DETECTION", "maxResults": 20}]

PHOTO_SYSTEM = (
    "You sort one field-marketing photo into a bucket. "
    "Look only at shelves, table setups, products, and receipts. "
    "Do not identify, describe, or recognize any person or face. "
    "If people are in the frame, ignore them completely. "
    "Never output a name, age, or description of a person. "
    "productVisible is true only when a product package is visible."
)

_PHOTO_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["bucket", "productVisible"],
    "properties": {
        "bucket": {"type": "string", "enum": list(_KINDS)},
        "productVisible": {"type": "boolean"},
    },
}

_KIND_WORDS: dict[BucketKind, tuple[str, ...]] = {
    "shelf": ("shelf", "stock", "on-shelf", "cooler"),
    "table_setup": ("table", "set up", "setup", "footprint"),
    "sampling": ("sampling", "sample", "consumer"),
    "receipt": ("receipt", "spend", "expense"),
    "other": (),
}

_PRODUCT_LABELS = (
    "bottle",
    "can",
    "beverage",
    "drink",
    "product",
    "packaging",
    "wine",
    "beer",
    "soda",
    "carton",
)
_SHELF_LABELS = ("shelf", "shelving", "display case", "cooler", "refrigerator")
_TABLE_LABELS = ("table", "tablecloth", "furniture", "desk")
_RECEIPT_LABELS = ("receipt", "invoice")
_SAMPLING_LABELS = ("tasting", "sample", "cup")

_VISION_ENDPOINT = "https://vision.googleapis.com/v1/images:annotate"
_SCOPES = ["https://www.googleapis.com/auth/cloud-platform"]


def _unavailable() -> dict:
    return {
        "available": False,
        "reason": PHOTO_CHECK_UNAVAILABLE,
        "bucket": None,
        "suggestedBucketName": None,
        "productVisible": None,
        "emptyShelf": False,
        "message": PHOTO_CHECK_UNAVAILABLE,
    }


def parse_bucket_kind(raw: object) -> BucketKind | None:
    if raw in _KINDS:
        return raw  # type: ignore[return-value]
    return None


def suggest_bucket_name(kind: BucketKind, bucket_names: list[str]) -> str | None:
    """The tenant bucket whose name matches ``kind``, if the brand has one."""
    words = _KIND_WORDS[kind]
    if not words:
        return None
    for name in bucket_names:
        lowered = (name or "").strip().lower()
        if not lowered:
            continue
        if any(word in lowered for word in words):
            return name
    return None


def is_shelf_bucket_name(name: str | None) -> bool:
    lowered = (name or "").strip().lower()
    return any(word in lowered for word in _KIND_WORDS["shelf"])


def kind_from_labels(labels: list[str]) -> tuple[BucketKind, bool]:
    """Map Cloud Vision label strings onto a bucket. Product words set visibility."""
    lowered = [label.strip().lower() for label in labels if label and label.strip()]
    product = any(any(word in label for word in _PRODUCT_LABELS) for label in lowered)

    def hit(words: tuple[str, ...]) -> bool:
        return any(any(word in label for word in words) for label in lowered)

    if hit(_RECEIPT_LABELS):
        return "receipt", product
    if hit(_SHELF_LABELS):
        return "shelf", product
    if hit(_TABLE_LABELS):
        return "table_setup", product
    if hit(_SAMPLING_LABELS) or product:
        return "sampling", product
    return "other", product


def _vision_label_names(image_bytes: bytes) -> list[str] | None:
    """LABEL_DETECTION via the Vision REST API. None when it cannot run."""
    try:
        import google.auth
        from google.auth.transport.requests import AuthorizedSession

        creds, _ = google.auth.default(scopes=_SCOPES)
        session = AuthorizedSession(creds)
        payload = {
            "requests": [
                {
                    "image": {"content": base64.b64encode(image_bytes).decode("ascii")},
                    "features": VISION_FEATURES,
                }
            ]
        }
        resp = session.post(_VISION_ENDPOINT, json=payload, timeout=25)
        resp.raise_for_status()
        data = resp.json()
    except Exception as exc:  # noqa: BLE001 — degrade, never fail the upload
        logger.warning("photo check: Vision labels unavailable: %s", exc)
        return None
    responses = data.get("responses") or [{}]
    first = responses[0] if responses else {}
    if first.get("error"):
        return None
    names: list[str] = []
    for item in first.get("labelAnnotations") or []:
        if isinstance(item, dict) and item.get("description"):
            names.append(str(item["description"]))
    return names


def _message(
    *,
    kind: BucketKind,
    suggested: str | None,
    chosen: str,
    empty_shelf: bool,
) -> str:
    if empty_shelf:
        return (
            "This shelf photo doesn't appear to show product. "
            "Retake it or keep it — the photo still saves either way."
        )
    if suggested and chosen and suggested.strip().lower() != chosen.strip().lower():
        return f"This looks like a {suggested} photo. Retake it or keep it in {chosen}."
    label = suggested or kind.replace("_", " ")
    return f"Looks like {label}."


def _pack(
    kind: BucketKind,
    product_visible: bool | None,
    bucket_names: list[str],
    chosen_bucket_name: str,
) -> dict:
    suggested = suggest_bucket_name(kind, bucket_names)
    shelfish = kind == "shelf" or is_shelf_bucket_name(chosen_bucket_name)
    empty = bool(shelfish and product_visible is False)
    return {
        "available": True,
        "reason": "",
        "bucket": kind,
        "suggestedBucketName": suggested,
        "productVisible": product_visible,
        "emptyShelf": empty,
        "message": _message(
            kind=kind,
            suggested=suggested,
            chosen=chosen_bucket_name,
            empty_shelf=empty,
        ),
    }


def classify_photo(
    image_bytes: bytes,
    *,
    content_type: str,
    bucket_names: list[str],
    chosen_bucket_name: str = "",
) -> dict:
    """Classify one photo. Never raises. Never rejects the upload."""
    if not image_bytes or not (content_type or "").startswith("image/"):
        return _unavailable()

    kind: BucketKind | None = None
    product: bool | None = None
    key = (getattr(settings, "OPENAI_API_KEY", "") or "").strip()
    if key:
        try:
            parsed = generate_vision_json(
                PHOTO_SYSTEM,
                "Classify this photo. Ignore any people in it.",
                image_bytes=image_bytes,
                content_type=content_type,
                schema=_PHOTO_SCHEMA,
            )
        except AiUnavailable:
            parsed = None
        if isinstance(parsed, dict):
            kind = parse_bucket_kind(parsed.get("bucket"))
            visible = parsed.get("productVisible")
            product = visible if isinstance(visible, bool) else None

    if kind is None:
        labels = _vision_label_names(image_bytes)
        if not labels:
            return _unavailable()
        kind, seen = kind_from_labels(labels)
        product = seen

    if kind is None:
        return _unavailable()
    return _pack(kind, product, bucket_names, chosen_bucket_name)
