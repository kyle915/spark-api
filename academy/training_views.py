"""Public (no-JWT) endpoint behind the shareable BA training link.

    GET /api/public/training/<code>  → hub + ordered resources

Possession of the code in the URL is the only credential, same as the web
check-in link and the tokenized client-live page. The response is read-only
and contains nothing but the training material the brand publishes to every
BA, so there is no per-BA data to leak and no session to mint.

Kept deliberately thin: parse → look up → serialise. The only real logic is
:func:`embed_url`, which normalises a video link into something an ``<iframe>``
can load, because the URL a person copies out of the address bar is a watch
page, not an embed.
"""

from __future__ import annotations

import re

from django.http import HttpRequest, JsonResponse
from django.views.decorators.http import require_http_methods

from academy.models import TrainingHub

# youtu.be/<id>, youtube.com/watch?v=<id>, /embed/<id>, /shorts/<id>
_YOUTUBE_RE = re.compile(
    r"(?:youtube\.com/(?:watch\?(?:.*&)?v=|embed/|shorts/|live/)|youtu\.be/)"
    r"([A-Za-z0-9_-]{6,})"
)
# vimeo.com/<digits>, player.vimeo.com/video/<digits>
_VIMEO_RE = re.compile(r"vimeo\.com/(?:video/)?(\d+)")
# Google Drive share links: /file/d/<id>/view
_DRIVE_RE = re.compile(r"drive\.google\.com/file/d/([A-Za-z0-9_-]+)")


def embed_url(url: str) -> str | None:
    """Return an iframe-safe embed URL for ``url``, or ``None``.

    ``None`` means "this isn't a recognised embeddable host" — the caller
    renders a plain link instead of an iframe, which is the correct fallback
    for a direct MP4 or an unknown provider. Never raises.
    """
    if not url:
        return None

    m = _YOUTUBE_RE.search(url)
    if m:
        # nocookie host: the training page is sent to contractors, and there
        # is no reason for it to drop advertising cookies on their phones.
        return f"https://www.youtube-nocookie.com/embed/{m.group(1)}?rel=0"

    m = _VIMEO_RE.search(url)
    if m:
        return f"https://player.vimeo.com/video/{m.group(1)}"

    m = _DRIVE_RE.search(url)
    if m:
        return f"https://drive.google.com/file/d/{m.group(1)}/preview"

    return None


def _resource_payload(res) -> dict:
    return {
        "id": res.id,
        "kind": res.kind,
        "title": res.title,
        "description": res.description,
        "url": res.url,
        "embedUrl": embed_url(res.url) if res.kind == "video" else None,
        "metaLabel": res.meta_label,
    }


@require_http_methods(["GET"])
def public_training_hub(request: HttpRequest, code: str) -> JsonResponse:
    """Everything the public training page needs, in one call."""
    normalized = (code or "").strip().upper()

    hub = (
        TrainingHub.objects.filter(code=normalized, is_active=True)
        .select_related("tenant")
        .first()
    )
    if hub is None:
        # Same message for "wrong code" and "retired campaign" — a public
        # endpoint shouldn't confirm which codes exist.
        return JsonResponse(
            {"error": "not_found", "message": "Training link not found."},
            status=404,
        )

    resources = [
        _resource_payload(r)
        for r in hub.resources.filter(published=True).order_by("order", "id")
    ]

    return JsonResponse(
        {
            "code": hub.code,
            "title": hub.title,
            "subtitle": hub.subtitle,
            "intro": hub.intro,
            "brandName": getattr(hub.tenant, "name", "") or "",
            "resources": resources,
        }
    )
