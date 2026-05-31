"""Public (no-JWT) HTTP endpoints for the Client Campaign Report.

The signed share token IS the authorization — these views take no JWT and
no cookie, exactly like the events approval flow (`events/views.py`) and
the receipts upload flow. Two read-only operations, both keyed off the
``reports.campaign.v1`` token:

* GET /api/public/report/<token>        → the report as camelCase JSON.
* GET /api/public/report/<token>/pdf    → the branded report PDF.

Bad / expired tokens 4xx in the SAME shape as the approval flow:
``400`` (``{"error": "invalid", ...}``) for a tampered/malformed token and
``410`` (``{"error": "expired", ...}``) for one past its lifetime — so the
web client can reuse the approval-flow error handling.
"""

from __future__ import annotations

import logging

from django.http import HttpRequest, HttpResponse, JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods

from recaps import report_service
from recaps.report_pdf import CampaignReportPdfError, generate_campaign_report_pdf
from recaps.report_tokens import BadSignature, SignatureExpired, verify_report_token

logger = logging.getLogger(__name__)


def _verify_or_4xx(token: str) -> int | HttpResponse:
    """Return the request id, or a 4xx JsonResponse matching the approval
    flow's error shape."""
    try:
        return verify_report_token(token)
    except SignatureExpired:
        return JsonResponse(
            {
                "error": "expired",
                "message": "This report link has expired. Please ask for a fresh link.",
            },
            status=410,
        )
    except (BadSignature, ValueError):
        return JsonResponse(
            {
                "error": "invalid",
                "message": "This report link is invalid or has been tampered with.",
            },
            status=400,
        )


def _build_report_or_404(request_id: int):
    """Aggregate the report, or return a 404 JsonResponse when the request
    no longer exists (e.g. soft-deleted after the link was minted)."""
    from django.utils import timezone

    def _build():
        request_obj = report_service.get_report_request(request_id)
        if request_obj is None:
            return None
        return report_service.build_campaign_report(
            request_obj, generated_at=timezone.now().isoformat()
        )

    data = _build()
    if data is None:
        return JsonResponse(
            {"error": "not_found", "message": "Report not found."}, status=404
        )
    return data


@csrf_exempt
@require_http_methods(["GET"])
def public_report_view(request: HttpRequest, token: str) -> HttpResponse:
    """Return the campaign report JSON for a valid share token.

    The payload mirrors the ``CampaignReport`` GraphQL fields (camelCase,
    nested ``kpis`` / ``events`` / ``photos`` / ``ambassadors`` /
    ``highlights``) but WITHOUT ``shareToken`` — the caller already holds
    the token they arrived with.
    """
    verified = _verify_or_4xx(token)
    if isinstance(verified, HttpResponse):
        return verified
    request_id = verified

    built = _build_report_or_404(request_id)
    if isinstance(built, HttpResponse):
        return built

    return JsonResponse({"report": report_service.report_to_dict(built)})


@csrf_exempt
@require_http_methods(["GET"])
def public_report_pdf_view(request: HttpRequest, token: str) -> HttpResponse:
    """Stream the branded report PDF for a valid share token.

    A render failure (missing WeasyPrint native deps / a render error)
    returns a clean 500 — :func:`generate_campaign_report_pdf` raises
    :class:`CampaignReportPdfError` rather than letting the worker crash.
    """
    verified = _verify_or_4xx(token)
    if isinstance(verified, HttpResponse):
        return verified
    request_id = verified

    # Cheap existence check before paying for a render, so a stale link to
    # a deleted request 404s instead of 500-ing inside the PDF builder.
    request_obj = report_service.get_report_request(request_id)
    if request_obj is None:
        return JsonResponse(
            {"error": "not_found", "message": "Report not found."}, status=404
        )

    try:
        pdf_bytes = generate_campaign_report_pdf(request_id)
    except CampaignReportPdfError as exc:
        logger.exception(
            "campaign_report_pdf: render failed for request_id=%s", request_id
        )
        return JsonResponse(
            {"error": "pdf_failed", "message": str(exc)}, status=500
        )

    response = HttpResponse(pdf_bytes, content_type="application/pdf")
    response["Content-Disposition"] = (
        f'inline; filename="campaign-report-{request_id}.pdf"'
    )
    return response
