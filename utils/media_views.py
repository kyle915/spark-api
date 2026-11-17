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

While the bucket is ``allUsers:objectViewer`` the signed override does not
expose anything the public URL didn't. With ``PRIVATE_MEDIA_ENABLED`` on,
``path`` must be an app media URL with a valid token (a bare private key is
refused). Paths are pinned to this bucket and to video extensions — this is
not a generic file proxy.

``GET /api/public/media/o/<token>/<name>`` is the private-media reader: it
verifies the HMAC token minted by ``utils.private_media.media_url`` and 302s
to a short-lived signed URL. ``?w=400|800`` serves a thumbnail; video keys
get the same inline/mp4 overrides as ``play``; a HEIC key requested as
``<name>.jpg`` serves the converted JPG sibling.
"""

from __future__ import annotations

import logging

from django.http import HttpRequest, HttpResponse, HttpResponseRedirect
from django.views.decorators.http import require_GET

from utils import private_media
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
    raw = (request.GET.get("path") or "").strip()
    if private_media.enabled() or private_media.is_media_url(raw):
        return _play_private(raw)
    blob = _safe_blob(raw)
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


def _signed_redirect(blob: str, **overrides) -> HttpResponse:
    try:
        url, max_age = private_media.serve_url(blob, **overrides)
    except Exception:  # noqa: BLE001 — signing outage: fail closed, quietly
        logger.exception("media signing failed blob=%r", blob)
        return HttpResponse(status=503)
    if not url:
        return HttpResponse(status=404)
    resp = HttpResponseRedirect(url)
    if max_age is None:
        resp["Cache-Control"] = "public, max-age=86400"
    else:
        resp["Cache-Control"] = f"private, max-age={max_age}"
    # The signed URL carries a credential; keep it out of third-party Referers.
    resp["Referrer-Policy"] = "no-referrer"
    return resp


def _play_private(raw: str) -> HttpResponse:
    from utils.thumb_views import resolve_requested_blob

    blob, status = resolve_requested_blob(raw)
    if blob is None:
        return HttpResponse(status=status)
    content_type = playback_content_type(blob)
    if not content_type:
        return HttpResponse(status=404)
    return _signed_redirect(
        blob, response_type=content_type, response_disposition="inline"
    )


@require_GET
def media_object(request: HttpRequest, token: str, name: str = "") -> HttpResponse:
    """Private-media reader. See module docstring."""
    try:
        blob = private_media.verify_token(token)
    except private_media.TokenExpired:
        return HttpResponse(status=410)
    except private_media.TokenError:
        return HttpResponse(status=404)

    if request.GET.get("w"):
        from utils.thumb_views import (
            NOT_IMAGE,
            parse_width,
            redirect_to_blob,
            resolve_thumb_blob,
        )

        if blob.startswith("thumbs/"):
            return redirect_to_blob(blob)
        target = resolve_thumb_blob(blob, parse_width(request.GET.get("w")))
        if target is NOT_IMAGE:
            return HttpResponse(status=404)
        return redirect_to_blob(target)

    content_type = playback_content_type(blob)
    if content_type:
        return _signed_redirect(
            blob, response_type=content_type, response_disposition="inline"
        )

    # Clients rewrite ``.heic`` → ``.jpg`` on the URL to reach the converted
    # sibling (recapDisplayUrl on the web). The filename is not signed, so
    # honour only that one derived object of the signed key.
    from recaps.heic_conversion import is_heic_blob, jpg_blob_name_for

    if is_heic_blob(blob) and name.lower().endswith(".jpg"):
        blob = jpg_blob_name_for(blob)
    return _signed_redirect(blob)
