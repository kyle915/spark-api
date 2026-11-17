"""On-demand image thumbnails for recap photos.

List surfaces in the admin web app were loading FULL-RESOLUTION photos
straight from GCS (multi-MB each on photo-heavy weeks). This endpoint
materializes a resized copy on first request and serves it from GCS
afterwards, so:

  * no upload-path changes (uploads go browser → GCS via signed URLs and
    never touch Django), and
  * no backfill — thumbs appear lazily for any existing photo the first
    time a list renders it.

`GET /api/public/img/thumb?path=<blob-or-public-url>&w=<width>`

  1. Resolve the blob name (accepts a raw blob path or the public
     storage.googleapis.com URL the API already hands out).
  2. If `thumbs/w{w}/{blob}.jpg` exists → 302 to its public URL.
  3. Non-image blobs (recap PDFs, guessed non-image content-type, `%PDF`
     magic) → 404. Recap PDFs are documents, not gallery photos; Pillow
     cannot identify them and used to fire a backend-error alert.
  4. Else download the original, Pillow-resize to `w` wide, store the
     JPEG thumb back in GCS, then 302.
  5. Anything unexpected (missing blob, oversized original, Pillow
     failure on a real photo) → 302 to the ORIGINAL public URL, so the
     UI degrades to exactly today's behavior rather than a broken image.

Unauthenticated by design while the bucket is public-read: a thumb of a
public object exposes nothing new. With ``PRIVATE_MEDIA_ENABLED`` on, the
``path`` must be an app media URL carrying a valid token (or a public
catalog key) and the redirect goes to a short-lived signed URL; a bare
private key is refused (403). The same resolution backs
``/api/public/media/o/<token>?w=`` (utils/media_views.py). Width is
whitelisted and the path is pinned to this bucket, so the endpoint can't be
used as a generic proxy.
"""

from __future__ import annotations

import io
import logging
import mimetypes

from django.http import HttpRequest, HttpResponse, HttpResponseRedirect
from django.views.decorators.http import require_GET
from PIL import UnidentifiedImageError

from utils import private_media
from utils.gcs import (
    blob_exists,
    download_blob_bytes,
    extract_blob_name_from_url,
    public_url,
    upload_bytes,
)
from utils.private_media import serve_url
from recaps.heic_conversion import is_heic_blob, jpg_blob_name_for

logger = logging.getLogger(__name__)

# Whitelisted widths — one for list/grid cells, one for larger previews.
ALLOWED_WIDTHS = (400, 800)
# Don't try to resize originals beyond this size (pathological uploads).
_MAX_ORIGINAL_BYTES = 40 * 1024 * 1024
_JPEG_QUALITY = 78
# Browsers may cache the redirect itself; the thumb object is immutable.
_REDIRECT_CACHE = "public, max-age=86400"
_PDF_MAGIC = b"%PDF"
# Not enough signal to skip Pillow — some uploads have no useful type.
_UNKNOWN_CONTENT_TYPES = frozenset({"", "application/octet-stream"})


def _thumb_blob_name(blob: str, width: int) -> str:
    # Thumbs always store as JPEG regardless of source extension; keeping
    # the original path inside the key makes the mapping auditable.
    return f"thumbs/w{width}/{blob}.jpg"


def _redirect(url: str) -> HttpResponse:
    resp = HttpResponseRedirect(url)
    resp["Cache-Control"] = _REDIRECT_CACHE
    return resp


def _not_an_image() -> HttpResponse:
    """Quiet skip for documents. Do not logger.exception — that alerts."""
    resp = HttpResponse(status=404)
    resp["Cache-Control"] = _REDIRECT_CACHE
    return resp


def _guessed_content_type(blob: str) -> str:
    guessed, _ = mimetypes.guess_type(blob)
    return (guessed or "").lower()


def _is_non_image_blob(blob: str, data: bytes | None = None) -> bool:
    """True for PDFs and other documents that must never reach Pillow."""
    name = blob.split("?", 1)[0].lower()
    if name.startswith("recaps/pdfs/") or name.endswith(".pdf"):
        return True
    content_type = _guessed_content_type(name)
    if (
        content_type
        and content_type not in _UNKNOWN_CONTENT_TYPES
        and not content_type.startswith("image/")
    ):
        return True
    if data is not None and data.startswith(_PDF_MAGIC):
        return True
    return False


class _NotImage:
    pass


NOT_IMAGE = _NotImage()


def resolve_thumb_blob(blob: str, width: int):
    """The blob to serve for a ``width`` thumbnail of ``blob``.

    Returns the thumb (materializing it on first request), a HEIC's JPG
    sibling or sibling thumb, or the original when no thumb can be made
    (missing/oversized/narrow/undecodable photo). Returns ``NOT_IMAGE`` for
    documents. Never raises. Callers decide HOW to serve the returned key
    (public URL vs signed URL), so this works for public and private objects.
    """
    thumb_blob = _thumb_blob_name(blob, width)

    if _is_non_image_blob(blob):
        return NOT_IMAGE

    # HEIC/HEIF: never hand the original to Pillow (and never 302 to a
    # multi-MB HEIC as the first choice). Prefer the JPG sibling's thumb,
    # else the sibling. Conversion is scheduled at upload.
    if is_heic_blob(blob):
        sibling = jpg_blob_name_for(blob)
        sibling_thumb = _thumb_blob_name(sibling, width)
        try:
            if blob_exists(sibling_thumb):
                return sibling_thumb
            if blob_exists(sibling):
                return sibling
        except Exception:  # noqa: BLE001 — degrade without Pillow on HEIC
            logger.exception(
                "HEIC sibling thumb lookup failed for blob=%r", blob
            )
        return blob

    try:
        if blob_exists(thumb_blob):
            return thumb_blob

        data = download_blob_bytes(blob)
        if data is None or len(data) > _MAX_ORIGINAL_BYTES:
            return blob
        if _is_non_image_blob(blob, data):
            return NOT_IMAGE

        from PIL import Image, ImageOps

        img = Image.open(io.BytesIO(data))
        img.load()
        # Bake in the photo's EXIF rotation before resizing. Phones store a
        # sideways shot as raw landscape pixels + an orientation tag; Pillow's
        # resize + save drops that tag, so without this the thumbnail shows the
        # un-rotated (sideways) pixels, while the full-res original still
        # carries the tag and the browser displays it correctly — the "sideways
        # in the grid, right when opened" bug. Mirrors the recap PDF path.
        img = ImageOps.exif_transpose(img) or img
        if img.mode not in ("RGB", "L"):
            img = img.convert("RGB")
        # Never upscale — a photo narrower than the target is fine as-is.
        if img.width <= width:
            return blob
        ratio = width / float(img.width)
        img = img.resize((width, max(1, int(img.height * ratio))))

        out = io.BytesIO()
        img.save(out, format="JPEG", quality=_JPEG_QUALITY, optimize=True)
        upload_bytes(thumb_blob, out.getvalue(), content_type="image/jpeg")
        return thumb_blob
    except UnidentifiedImageError:
        # Bytes that looked like they might be a photo but aren't. Quiet 404
        # so a stray CSV/PDF-without-extension does not fire an alert.
        return NOT_IMAGE
    except Exception:  # noqa: BLE001 — degrade to the original, never break
        logger.exception("Thumb generation failed for blob=%r", blob)
        return blob


def parse_width(raw) -> int:
    try:
        width = int(raw or 400)
    except (TypeError, ValueError):
        width = 400
    return width if width in ALLOWED_WIDTHS else 400


def redirect_to_blob(blob: str) -> HttpResponse:
    """302 to ``blob`` — public URL when that still works, else signed."""
    if not private_media.enabled():
        url = public_url(blob)
        return _redirect(url) if url else HttpResponse(status=404)
    try:
        url, max_age = serve_url(blob)
    except Exception:  # noqa: BLE001 — signing outage: fail closed, quietly
        logger.exception("media redirect signing failed blob=%r", blob)
        return HttpResponse(status=503)
    if not url:
        return HttpResponse(status=404)
    if max_age is None:
        return _redirect(url)
    resp = HttpResponseRedirect(url)
    resp["Cache-Control"] = f"private, max-age={max_age}"
    resp["Referrer-Policy"] = "no-referrer"
    return resp


def resolve_requested_blob(raw_path: str) -> tuple[str | None, int]:
    """Blob for a legacy ``?path=`` argument, or ``(None, status)``.

    Flag off: any key/public URL in this bucket (today's behaviour). Flag on:
    an app media URL with a valid, unexpired token, or a raw key under a
    public catalog prefix. A bare private key is refused — otherwise this
    endpoint would re-open the private bucket to anyone holding a key.
    """
    raw_path = (raw_path or "").strip()
    if private_media.is_media_url(raw_path):
        token = private_media.token_from_url(raw_path) or ""
        try:
            return private_media.verify_token(token), 200
        except private_media.TokenExpired:
            return None, 410
        except private_media.TokenError:
            return None, 404
    blob = (extract_blob_name_from_url(raw_path) or "").lstrip("/")
    if not blob:
        return None, 400
    if private_media.enabled() and not private_media.is_public_blob(blob):
        return None, 403
    return blob, 200


@require_GET
def thumb(request: HttpRequest) -> HttpResponse:
    blob, status = resolve_requested_blob(request.GET.get("path") or "")
    if blob is None:
        return HttpResponse(status=status)
    if blob.startswith("thumbs/"):
        return HttpResponse(status=400)

    width = parse_width(request.GET.get("w"))
    target = resolve_thumb_blob(blob, width)
    if target is NOT_IMAGE:
        return _not_an_image()
    return redirect_to_blob(target)
