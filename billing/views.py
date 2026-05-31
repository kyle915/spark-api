"""Public (no-JWT) HTTP endpoints for a shared invoice.

The signed share token IS the authorization — these views take no JWT and no
cookie, exactly like the events approval flow (``events/views.py``), the
receipts upload flow, and the campaign-report flow
(``recaps/report_views.py``). Two read-only operations, both keyed off the
``billing.invoice.v1`` token:

* GET /api/public/invoice/<token>        → the invoice as camelCase JSON.
* GET /api/public/invoice/<token>/pdf    → the branded invoice PDF.

Bad / expired tokens 4xx in the SAME shape as the other public flows:
``400`` (``{"error": "invalid", ...}``) for a tampered/malformed token and
``410`` (``{"error": "expired", ...}``) for one past its lifetime.
"""

from __future__ import annotations

import logging
from decimal import Decimal
from typing import Any

from django.http import HttpRequest, HttpResponse, JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods

from billing import models
from billing.pdf import InvoicePdfError, generate_invoice_pdf
from billing.tokens import BadSignature, SignatureExpired, verify_invoice_token

logger = logging.getLogger(__name__)


def _verify_or_4xx(token: str) -> int | HttpResponse:
    """Return the invoice id, or a 4xx JsonResponse matching the other public
    flows' error shape."""
    try:
        return verify_invoice_token(token)
    except SignatureExpired:
        return JsonResponse(
            {
                "error": "expired",
                "message": "This invoice link has expired. Please ask for a fresh link.",
            },
            status=410,
        )
    except (BadSignature, ValueError):
        return JsonResponse(
            {
                "error": "invalid",
                "message": "This invoice link is invalid or has been tampered with.",
            },
            status=400,
        )


def _load_invoice_or_404(invoice_id: int):
    """Return a non-deleted invoice (tenant + line_items prefetched), or a
    404 JsonResponse when it's missing / soft-deleted."""
    invoice = (
        models.Invoice.objects.filter(deleted_at__isnull=True)
        .select_related("tenant")
        .prefetch_related("line_items")
        .filter(id=invoice_id)
        .first()
    )
    if invoice is None:
        return JsonResponse(
            {"error": "not_found", "message": "Invoice not found."}, status=404
        )
    return invoice


def _money_str(value) -> str:
    """Serialize a Decimal-ish money value as a 2dp string (lossless)."""
    if value is None:
        return "0.00"
    try:
        return f"{Decimal(str(value)):.2f}"
    except Exception:
        return str(value)


def _iso_or_none(value) -> str | None:
    if value is None:
        return None
    try:
        return value.isoformat()
    except Exception:
        return str(value)


def _line_item_payload(item: models.InvoiceLineItem) -> dict[str, Any]:
    return {
        "id": str(item.id),
        "description": item.description or "",
        "quantity": _money_str(item.quantity),
        "unitPrice": _money_str(item.unit_price),
        "amount": _money_str(item.amount),
        "eventId": str(item.event_id) if getattr(item, "event_id", None) else None,
        "sortOrder": int(getattr(item, "sort_order", 0) or 0),
    }


def _invoice_payload(invoice: models.Invoice) -> dict[str, Any]:
    """The invoice as camelCase JSON — the same fields as the ``Invoice``
    GraphQL type MINUS ``shareToken`` (the caller already holds the token
    they arrived with), including the ordered ``lineItems``."""
    tenant = getattr(invoice, "tenant", None)
    client_name = getattr(tenant, "name", "") or ""
    line_items = sorted(
        invoice.line_items.all(),
        key=lambda i: (getattr(i, "sort_order", 0) or 0, i.id),
    )
    return {
        "uuid": str(invoice.uuid),
        "number": invoice.number or "",
        "status": invoice.status or "",
        "clientName": client_name,
        "issueDate": _iso_or_none(invoice.issue_date),
        "dueDate": _iso_or_none(invoice.due_date),
        "currency": invoice.currency or "USD",
        "notes": invoice.notes or "",
        "subtotal": _money_str(invoice.subtotal),
        "taxRate": _money_str(invoice.tax_rate),
        "taxAmount": _money_str(invoice.tax_amount),
        "total": _money_str(invoice.total),
        "lineItems": [_line_item_payload(item) for item in line_items],
        "sentAt": _iso_or_none(invoice.sent_at),
        "paidAt": _iso_or_none(invoice.paid_at),
        "createdAt": _iso_or_none(invoice.created_at),
        "updatedAt": _iso_or_none(invoice.updated_at),
    }


@csrf_exempt
@require_http_methods(["GET"])
def public_invoice_view(request: HttpRequest, token: str) -> HttpResponse:
    """Return the invoice JSON for a valid share token."""
    verified = _verify_or_4xx(token)
    if isinstance(verified, HttpResponse):
        return verified
    invoice_id = verified

    loaded = _load_invoice_or_404(invoice_id)
    if isinstance(loaded, HttpResponse):
        return loaded
    invoice: models.Invoice = loaded

    return JsonResponse({"invoice": _invoice_payload(invoice)})


@csrf_exempt
@require_http_methods(["GET"])
def public_invoice_pdf_view(request: HttpRequest, token: str) -> HttpResponse:
    """Stream the branded invoice PDF for a valid share token.

    A render failure (missing WeasyPrint native deps / a render error)
    returns a clean 500 — :func:`generate_invoice_pdf` raises
    :class:`InvoicePdfError` rather than letting the worker crash.
    """
    verified = _verify_or_4xx(token)
    if isinstance(verified, HttpResponse):
        return verified
    invoice_id = verified

    # Cheap existence check before paying for a render, so a stale link to a
    # deleted invoice 404s instead of 500-ing inside the PDF builder.
    loaded = _load_invoice_or_404(invoice_id)
    if isinstance(loaded, HttpResponse):
        return loaded
    invoice: models.Invoice = loaded

    try:
        pdf_bytes = generate_invoice_pdf(invoice_id)
    except InvoicePdfError as exc:
        logger.exception(
            "invoice_pdf: render failed for invoice_id=%s", invoice_id
        )
        return JsonResponse(
            {"error": "pdf_failed", "message": str(exc)}, status=500
        )

    response = HttpResponse(pdf_bytes, content_type="application/pdf")
    response["Content-Disposition"] = (
        f'inline; filename="{invoice.number or f"invoice-{invoice_id}"}.pdf"'
    )
    return response


__all__ = ["public_invoice_view", "public_invoice_pdf_view"]
