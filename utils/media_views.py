"""Inline playback URLs for recap videos.

Walk-up clips land in the public bucket as ``.mov`` with
``Content-Type: video/quicktime``. Opening that URL in a new tab makes
Chrome download the file. An in-page ``<video>`` still fails when the
response type is QuickTime or ``Content-Disposition`` is ``attachment``.

``GET /api/public/media/play?path=<blob-or-public-url>`` 302s to a short
lived signed URL that forces:

  * ``Content-Type: video/mp4`` for ``.mov`` / ``.mp4`` / ``.m4v``
    (H.264-in-QuickTime plays in Chrome when labeled mp4; HEVC still will not)
  * ``Content-Disposition: inline``

The bucket is already ``allUsers:objectViewer``, so the signed override
does not expose anything the public URL didn't. Paths are pinned to this
bucket and to video extensions — this is not a generic file proxy.
"""

from __future__ import annotations

import logging

from django.http import HttpRequest, HttpResponse, HttpResponseRedirect
from django.views.decorators.http import require_GET

from utils.gcs import extract_blob_name_from_url, generate_download_url, public_url

logger = logging.getLogger(__name__)

# Chrome will attempt these. .mov is served as video/mp4 on purpose:
# many iPhone clips are H.264 in a QuickTime container and play once the
# response type is mp4. HEVC still fails in Chrome; the player shows that.
_PLAYBACK_TYPES = {
    ".mp4": "video/mp4",
    ".m4v": "video/mp4",
    ".mov": "video/mp4",
    ".webm": "video/webm",
}


def playback_content_type(blob: str) -> str | None:
    name = blob.split("?", 1)[0].split("#", 1)[0].lower()
    dot = name.rfind(".")
    if dot < 0:
        return None
    return _PLAYBACK_TYPES.get(name[dot:])


def _safe_blob(raw: str) -> str | None:
    blob = (extract_blob_name_from_url(raw) or "").lstrip("/")
    if not blob or ".." in blob.split("/") or blob.startswith(("/", "\\")):
        return None
    if "\\" in blob or "\x00" in blob:
        return None
    return blob


@require_GET
def play_video(request: HttpRequest) -> HttpResponse:
    blob = _safe_blob((request.GET.get("path") or "").strip())
    if blob is None:
        return HttpResponse(status=400)

    content_type = playback_content_type(blob)
    if not content_type:
        return HttpResponse(status=404)

    url = ""
    try:
        url = generate_download_url(
            blob,
            response_type=content_type,
            response_disposition="inline",
        )
    except Exception:  # noqa: BLE001 — public URL still lets <video> try
        logger.exception("inline video URL failed blob=%r", blob)
        url = public_url(blob) or ""

    if not url:
        return HttpResponse(status=404)

    resp = HttpResponseRedirect(url)
    # Signed URLs expire; don't let a shared cache hold one.
    resp["Cache-Control"] = "private, max-age=60"
    return resp
