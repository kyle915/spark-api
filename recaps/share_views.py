"""Public (no-JWT) HTTP endpoints for a shared recap.

The signed share token IS the authorization — these views take no JWT and
no cookie, exactly like the campaign-report flow:

* GET /api/public/recap/<token>        → recap as camelCase JSON.
* GET /api/public/recap/<token>/pdf    → the branded recap PDF.

Bad / expired tokens 4xx in the SAME shape as the report flow:
``400`` invalid, ``410`` expired, ``404`` recap gone.
"""

from __future__ import annotations

import logging

from django.http import HttpRequest, HttpResponse, JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods

from recaps.recap_tokens import BadSignature, SignatureExpired, verify_recap_token
from recaps.share_render import (
    load_shared_recap,
    recap_to_public_dict,
    render_shared_recap_pdf,
)

logger = logging.getLogger(__name__)


def _verify_or_4xx(token: str):
    try:
        return verify_recap_token(token)
    except SignatureExpired:
        return JsonResponse(
            {
                "error": "expired",
                "message": "This recap link has expired. Please ask for a fresh link.",
            },
            status=410,
        )
    except (BadSignature, ValueError):
        return JsonResponse(
            {
                "error": "invalid",
                "message": "This recap link is invalid or has been tampered with.",
            },
            status=400,
        )


@csrf_exempt
@require_http_methods(["GET"])
def public_recap_view(request: HttpRequest, token: str) -> HttpResponse:
    verified = _verify_or_4xx(token)
    if isinstance(verified, HttpResponse):
        return verified
    kind, recap_id = verified

    recap = load_shared_recap(kind, recap_id)
    if recap is None:
        return JsonResponse(
            {"error": "not_found", "message": "Recap not found."}, status=404
        )
    return JsonResponse({"recap": recap_to_public_dict(kind, recap)})


@csrf_exempt
@require_http_methods(["GET"])
def public_recap_pdf_view(request: HttpRequest, token: str) -> HttpResponse:
    verified = _verify_or_4xx(token)
    if isinstance(verified, HttpResponse):
        return verified
    kind, recap_id = verified

    recap = load_shared_recap(kind, recap_id)
    if recap is None:
        return JsonResponse(
            {"error": "not_found", "message": "Recap not found."}, status=404
        )

    try:
        pdf_bytes = render_shared_recap_pdf(kind, recap)
    except Exception:
        logger.exception(
            "recap_share_pdf: render failed for kind=%s id=%s", kind, recap_id
        )
        return JsonResponse(
            {"error": "pdf_failed", "message": "Failed to render recap PDF."},
            status=500,
        )

    slug = (getattr(recap, "uuid", None) or recap_id)
    response = HttpResponse(pdf_bytes, content_type="application/pdf")
    response["Content-Disposition"] = f'inline; filename="recap-{slug}.pdf"'
    return response
