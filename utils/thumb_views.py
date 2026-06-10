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
  3. Else download the original, Pillow-resize to `w` wide, store the
     JPEG thumb back in GCS, then 302.
  4. Anything unexpected (non-image bytes, missing blob, oversized
     original, Pillow failure) → 302 to the ORIGINAL public URL, so the
     UI degrades to exactly today's behavior rather than a broken image.

Unauthenticated by design: the originals live in a bucket with
allUsers:objectViewer (see utils.gcs.public_url), so a thumb of a public
object exposes nothing new. Width is whitelisted and the path is pinned
to this bucket, so the endpoint can't be used as a generic proxy.
"""

from __future__ import annotations

import io
import logging

from django.http import HttpRequest, HttpResponse, HttpResponseRedirect
from django.views.decorators.http import require_GET

from utils.gcs import (
    blob_exists,
    download_blob_bytes,
    extract_blob_name_from_url,
    public_url,
    upload_bytes,
)

logger = logging.getLogger(__name__)

# Whitelisted widths — one for list/grid cells, one for larger previews.
ALLOWED_WIDTHS = (400, 800)
# Don't try to resize originals beyond this size (pathological uploads).
_MAX_ORIGINAL_BYTES = 40 * 1024 * 1024
_JPEG_QUALITY = 78
# Browsers may cache the redirect itself; the thumb object is immutable.
_REDIRECT_CACHE = "public, max-age=86400"


def _thumb_blob_name(blob: str, width: int) -> str:
    # Thumbs always store as JPEG regardless of source extension; keeping
    # the original path inside the key makes the mapping auditable.
    return f"thumbs/w{width}/{blob}.jpg"


def _redirect(url: str) -> HttpResponse:
    resp = HttpResponseRedirect(url)
    resp["Cache-Control"] = _REDIRECT_CACHE
    return resp


@require_GET
def thumb(request: HttpRequest) -> HttpResponse:
    raw_path = (request.GET.get("path") or "").strip()
    blob = (extract_blob_name_from_url(raw_path) or "").lstrip("/")
    if not blob or blob.startswith("thumbs/"):
        return HttpResponse(status=400)

    try:
        width = int(request.GET.get("w") or 400)
    except (TypeError, ValueError):
        width = 400
    if width not in ALLOWED_WIDTHS:
        width = 400

    original_url = public_url(blob) or ""
    thumb_blob = _thumb_blob_name(blob, width)

    try:
        if blob_exists(thumb_blob):
            return _redirect(public_url(thumb_blob) or original_url)

        data = download_blob_bytes(blob)
        if data is None or len(data) > _MAX_ORIGINAL_BYTES:
            return _redirect(original_url)

        from PIL import Image

        img = Image.open(io.BytesIO(data))
        img.load()
        if img.mode not in ("RGB", "L"):
            img = img.convert("RGB")
        # Never upscale — a photo narrower than the target is fine as-is.
        if img.width <= width:
            return _redirect(original_url)
        ratio = width / float(img.width)
        img = img.resize((width, max(1, int(img.height * ratio))))

        out = io.BytesIO()
        img.save(out, format="JPEG", quality=_JPEG_QUALITY, optimize=True)
        upload_bytes(thumb_blob, out.getvalue(), content_type="image/jpeg")
        return _redirect(public_url(thumb_blob) or original_url)
    except Exception:  # noqa: BLE001 — degrade to the original, never break
        logger.exception("Thumb generation failed for blob=%r", blob)
        return _redirect(original_url) if original_url else HttpResponse(
            status=404
        )
