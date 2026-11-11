"""Public (no-JWT) endpoint for a brand's shared program page.

GET /api/public/program/<token> → program totals, markets and field photos.

The signed token names the brand; the brand's ``program_share_enabled``
switch decides whether the link works. A switched-off link answers 404 in
the same shape as an unknown brand, so it does not reveal that it once
existed.
"""

from __future__ import annotations

import logging

from django.http import HttpRequest, JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods

from recaps.program_share import build_program_share
from recaps.program_tokens import (
    BadSignature,
    SignatureExpired,
    verify_program_token,
)

logger = logging.getLogger(__name__)

_NOT_AVAILABLE = {
    "error": "not_found",
    "message": "This program link is not available.",
}


@csrf_exempt
@require_http_methods(["GET"])
def public_program_view(request: HttpRequest, token: str) -> JsonResponse:
    try:
        tenant_id = verify_program_token(token)
    except SignatureExpired:
        return JsonResponse(
            {
                "error": "expired",
                "message": "This program link has expired. Please ask for a fresh link.",
            },
            status=410,
        )
    except (BadSignature, ValueError):
        return JsonResponse(
            {
                "error": "invalid",
                "message": "This program link is invalid or has been tampered with.",
            },
            status=400,
        )

    from tenants.models import Tenant

    tenant = Tenant.objects.filter(id=tenant_id, program_share_enabled=True).first()
    if tenant is None:
        return JsonResponse(_NOT_AVAILABLE, status=404)
    try:
        payload = build_program_share(tenant)
    except Exception as exc:  # noqa: BLE001
        logger.exception("public program page failed for tenant=%s: %s", tenant_id, exc)
        return JsonResponse(
            {"error": "unavailable", "message": "This page is temporarily unavailable."},
            status=503,
        )
    response = JsonResponse(payload)
    response["Cache-Control"] = "private, max-age=300"
    response["X-Robots-Tag"] = "noindex, nofollow"
    return response
