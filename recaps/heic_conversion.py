"""
HEIC → JPG conversion utilities for recap files.

iPhone photos upload as .heic (Apple's image format). Browsers can't render
HEIC natively, so the front-end has a libheif WASM fallback to decode them
client-side. That fallback works but is slow (700KB WASM + CPU-bound) and
unreliable on mobile — most cards show blank "DECODING…" tiles forever.

Server-side fix: when a HEIC file lands, generate a JPG sibling and store it
as a separate RecapFile row referencing the same recap. The grid filter
already prefers browser-renderable extensions, so the JPG shows in the hero
slot and the HEIC stays available for archival/download.

Design notes
============
- Sibling row (not a thumbnail field): existing front-end "find first .jpg"
  logic just works, no schema change, no JSON-blob plumbing.
- Idempotent: convert_recap_file() is a no-op if a JPG variant already
  exists at the expected blob path. Safe to re-run on the same file.
- Skips conversion if pillow_heif isn't installed (pyproject.toml pins it,
  but stay defensive in case a deploy lands without the wheel).
- Conversion failures DO NOT raise into the caller — we'd rather ship the
  HEIC alone than 500 the whole upload. Errors get logged via Django's
  logger so we can audit a failure rate later.
"""

from __future__ import annotations

import io
import logging
from typing import Optional

from django.conf import settings  # noqa: F401  (kept for future quality knobs)

from utils.gcs import download_blob_bytes, upload_bytes, public_url, blob_exists

logger = logging.getLogger(__name__)

# Pillow + pillow_heif are pinned in pyproject.toml — but the import is
# wrapped because someone could deploy without the wheel and we shouldn't
# crash the whole upload path.
try:
    from PIL import Image  # type: ignore
    import pillow_heif  # type: ignore

    pillow_heif.register_heif_opener()
    _HEIC_SUPPORT = True
except Exception:  # pragma: no cover
    Image = None  # type: ignore
    _HEIC_SUPPORT = False


# Extensions we want to auto-convert. Apple ships .heic; some pipelines
# produce .heif (the underlying container format).
HEIC_EXTS = (".heic", ".heif")


def is_heic_blob(blob_name: str) -> bool:
    """True if the blob path looks like a HEIC file (case-insensitive)."""
    if not blob_name:
        return False
    # Strip any query string before the extension check.
    head = blob_name.split("?", 1)[0].lower()
    return head.endswith(HEIC_EXTS)


def jpg_blob_name_for(heic_blob: str) -> str:
    """Compute the JPG sibling path for a given HEIC blob.

    `recaps/123-foo.heic` → `recaps/123-foo.jpg`
    Preserves directory + base name so the converted file lives next to
    the original in GCS and is easy to correlate later.
    """
    head = heic_blob.split("?", 1)[0]
    if head.lower().endswith(".heic"):
        return head[:-len(".heic")] + ".jpg"
    if head.lower().endswith(".heif"):
        return head[:-len(".heif")] + ".jpg"
    return head + ".jpg"  # Defensive — shouldn't hit on the happy path


def convert_heic_bytes_to_jpg(
    heic_bytes: bytes,
    *,
    quality: int = 85,
) -> Optional[bytes]:
    """Decode HEIC bytes and return JPG-encoded bytes. None on any failure.

    quality=85 is a reasonable photo-grade tradeoff (Apple's default JPEG
    export sits at ~80-90 depending on capture mode).
    """
    if not _HEIC_SUPPORT or not heic_bytes:
        return None
    try:
        with Image.open(io.BytesIO(heic_bytes)) as im:
            # HEICs can include alpha; flatten to RGB before JPEG encode.
            if im.mode != "RGB":
                im = im.convert("RGB")
            out = io.BytesIO()
            im.save(out, format="JPEG", quality=quality, optimize=True)
            return out.getvalue()
    except Exception as exc:
        logger.warning("HEIC→JPG decode failed: %s", exc)
        return None


def ensure_jpg_sibling(
    *,
    heic_blob_name: str,
    recap_id: int,
    file_type,
    file_recap_category,
    created_by,
) -> Optional["object"]:
    """Convert a HEIC RecapFile into a JPG sibling on GCS + DB.

    Returns the freshly-created RecapFile, or None if nothing happened.
    Idempotent: returns None if a row with the same JPG blob path already
    exists for this recap.

    Caller passes the FileType, FileRecapCategory, and creator from the
    parent HEIC row so the sibling looks like an organic upload from the
    same user.
    """
    # Local import to avoid a circular dep at module load.
    from recaps import models

    if not _HEIC_SUPPORT:
        return None
    if not is_heic_blob(heic_blob_name):
        return None

    jpg_blob = jpg_blob_name_for(heic_blob_name)

    # Idempotency — bail if a sibling for this recap already points there.
    existing = models.RecapFile.objects.filter(
        recap_id=recap_id, file=jpg_blob
    ).first()
    if existing:
        return None

    heic_bytes = download_blob_bytes(heic_blob_name)
    if not heic_bytes:
        logger.warning(
            "HEIC sibling: source blob unreadable, skipping: %s", heic_blob_name
        )
        return None

    jpg_bytes = convert_heic_bytes_to_jpg(heic_bytes)
    if not jpg_bytes:
        return None

    try:
        upload_bytes(jpg_blob, jpg_bytes, content_type="image/jpeg")
    except Exception as exc:
        logger.warning("HEIC sibling upload failed (%s): %s", jpg_blob, exc)
        return None

    sibling = models.RecapFile.objects.create(
        recap_id=recap_id,
        name="Auto-generated JPG variant",
        file=jpg_blob,
        file_type=file_type,
        file_recap_category=file_recap_category,
        approved=True,
        created_by=created_by,
    )
    return sibling


def ensure_jpg_sibling_blob(heic_blob_name: str) -> Optional[str]:
    """Convert a HEIC blob into a JPG sibling *blob in GCS only* (no DB row).

    Returns the JPG blob path on success (or if it already exists), else
    None. Idempotent: if the sibling blob is already present we skip the
    download/decode/upload and just return its path.

    This is the model-agnostic counterpart to ``ensure_jpg_sibling``.
    The legacy ``RecapFile`` flow wants a *sibling DB row* (the front-end
    hero picker scans for a renderable `.jpg` file row). ``CustomRecapFile``
    has no such picker — its ``displayUrl`` resolver just rewrites a
    `.heic` blob path to the `.jpg` sibling and serves it — so all that
    path needs is the converted blob sitting next to the original. Best-
    effort: any failure logs + returns None, never raises into the caller.
    """
    if not _HEIC_SUPPORT:
        return None
    if not is_heic_blob(heic_blob_name):
        return None

    jpg_blob = jpg_blob_name_for(heic_blob_name)

    # Idempotency — sibling blob already uploaded (e.g. re-run backfill,
    # or an edit that re-attaches the same HEIC). Skip the round-trip.
    if blob_exists(jpg_blob):
        return jpg_blob

    heic_bytes = download_blob_bytes(heic_blob_name)
    if not heic_bytes:
        logger.warning(
            "HEIC sibling blob: source unreadable, skipping: %s", heic_blob_name
        )
        return None

    jpg_bytes = convert_heic_bytes_to_jpg(heic_bytes)
    if not jpg_bytes:
        return None

    try:
        upload_bytes(jpg_blob, jpg_bytes, content_type="image/jpeg")
    except Exception as exc:
        logger.warning("HEIC sibling blob upload failed (%s): %s", jpg_blob, exc)
        return None

    return jpg_blob


def jpg_sibling_url_for_heic(heic_blob_name: str) -> Optional[str]:
    """Convenience for templates / inline render — returns the public URL
    of a JPG sibling if it exists, else None. (Not currently used by the
    main upload path; kept for future inline rendering hooks.)"""
    if not is_heic_blob(heic_blob_name):
        return None
    return public_url(jpg_blob_name_for(heic_blob_name))


def display_blob_name(blob_name: Optional[str]) -> Optional[str]:
    """Resolve the *viewable* blob for a recap file.

    - Non-HEIC blob → returned unchanged (already browser-renderable).
    - HEIC blob with a converted `.jpg` sibling present in GCS → the
      sibling path (so the frontend gets a plain, CORS-free <img>).
    - HEIC blob with no sibling yet → the original `.heic` path, so the
      frontend can still fall back to in-browser decoding.

    Pure path logic + one cheap `blob_exists` HEAD check; safe to call
    from a (sync) resolver body wrapped in ``sync_to_async``.
    """
    if not blob_name:
        return blob_name
    if not is_heic_blob(blob_name):
        return blob_name
    jpg_blob = jpg_blob_name_for(blob_name)
    if blob_exists(jpg_blob):
        return jpg_blob
    return blob_name
