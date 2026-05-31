"""Branded PDF renderer for a client invoice.

``generate_invoice_pdf(invoice_id) -> bytes`` builds ONE self-contained HTML
document for an invoice and renders it through WeasyPrint exactly once — the
same engine + base styling the recap / campaign-report PDFs use
(``recaps.pdf._PDF_BASE_CSS``).

Layout:
  - Branded header: "Ignite" mark, the billed client (tenant) name, the
    invoice number, issue / due dates, and a status pill.
  - The line-item table (description / qty / unit price / amount).
  - A totals block (subtotal / tax / total).
  - Notes (when present).

WeasyPrint is imported lazily inside the render call (its native deps can be
missing at import time on some workers); a render failure raises
:class:`InvoicePdfError` so the public view turns it into a clean 500 instead
of crashing the worker — exactly the posture of
``recaps.report_pdf.generate_campaign_report_pdf``.
"""

from __future__ import annotations

import html as _html
from decimal import Decimal

from recaps.pdf import _PDF_BASE_CSS

# The brand the invoice is issued BY (the agency). Ignite is veteran-owned
# field marketing; the header reads "<Ignite>  ·  Invoice for <client>".
_AGENCY_NAME = "Ignite Productions"


class InvoicePdfError(RuntimeError):
    """Raised when the invoice PDF can't be rendered (missing WeasyPrint
    native deps, or a render-time failure). The public view maps this to a
    500 with a clear message rather than letting the worker crash."""


def _esc(value) -> str:
    """HTML-escape a value, rendering None/empty as an em dash."""
    if value in (None, ""):
        return "—"
    return _html.escape(str(value))


def _money(value, currency: str = "USD") -> str:
    """Format a Decimal-ish value as a currency amount string."""
    try:
        amount = Decimal(str(value or 0))
    except Exception:
        amount = Decimal("0")
    symbol = "$" if (currency or "USD").upper() == "USD" else ""
    return f"{symbol}{amount:,.2f}"


def _format_date(value) -> str:
    if not value:
        return "—"
    try:
        return value.strftime("%b %d, %Y")
    except Exception:
        return _esc(value)


def _line_rows(line_items, currency: str) -> str:
    if not line_items:
        return (
            "<tr><td colspan='4' class='empty'>No line items.</td></tr>"
        )
    rows = []
    for item in line_items:
        rows.append(
            "<tr>"
            f"<td>{_esc(item.description)}</td>"
            f"<td class='num'>{Decimal(str(item.quantity or 0)):,.2f}</td>"
            f"<td class='num'>{_money(item.unit_price, currency)}</td>"
            f"<td class='num'>{_money(item.amount, currency)}</td>"
            "</tr>"
        )
    return "".join(rows)


def _build_invoice_html(invoice, line_items) -> str:
    """Render the invoice model + its line items to a standalone HTML doc."""
    tenant = getattr(invoice, "tenant", None)
    client_name = getattr(tenant, "name", "") or ""
    currency = invoice.currency or "USD"
    status = (invoice.status or "draft").upper()
    notes_block = (
        f"<section class='card notes'><h2>Notes</h2><p>{_esc(invoice.notes)}</p></section>"
        if (invoice.notes or "").strip()
        else ""
    )

    return f"""
<!doctype html>
<html>
  <head>
    <meta charset="utf-8" />
    <title>Invoice {_esc(invoice.number)}</title>
  </head>
  <body>
    <header class="inv-header">
      <div class="brand">
        <p class="agency">{_esc(_AGENCY_NAME)}</p>
        <p class="eyebrow">Invoice</p>
      </div>
      <div class="inv-meta">
        <p class="number">{_esc(invoice.number)}</p>
        <span class="pill pill-{_esc(invoice.status)}">{_esc(status)}</span>
      </div>
    </header>

    <section class="parties">
      <div>
        <p class="label">Billed To</p>
        <p class="client">{_esc(client_name)}</p>
      </div>
      <div class="dates">
        <p><span class="label">Issued</span> {_format_date(invoice.issue_date)}</p>
        <p><span class="label">Due</span> {_format_date(invoice.due_date)}</p>
      </div>
    </section>

    <section class="card">
      <table class="lines">
        <thead>
          <tr>
            <th>Description</th>
            <th class="num">Qty</th>
            <th class="num">Unit Price</th>
            <th class="num">Amount</th>
          </tr>
        </thead>
        <tbody>{_line_rows(line_items, currency)}</tbody>
      </table>

      <div class="totals">
        <div class="row"><span>Subtotal</span><strong>{_money(invoice.subtotal, currency)}</strong></div>
        <div class="row"><span>Tax ({Decimal(str(invoice.tax_rate or 0)):,.2f}%)</span><strong>{_money(invoice.tax_amount, currency)}</strong></div>
        <div class="row total"><span>Total</span><strong>{_money(invoice.total, currency)}</strong></div>
      </div>
    </section>

    {notes_block}
  </body>
</html>
"""


_INVOICE_CSS = """
        .inv-header {
            display: flex;
            justify-content: space-between;
            align-items: flex-start;
            margin-bottom: 22px;
        }
        .agency { margin: 0; font-size: 18px; font-weight: 700; color: #111827; }
        .eyebrow {
            margin: 4px 0 0 0;
            font-size: 11px;
            color: #6b7280;
            text-transform: uppercase;
            letter-spacing: 0.16em;
        }
        .inv-meta { text-align: right; }
        .number { margin: 0 0 6px 0; font-size: 20px; font-weight: 700; color: #111827; }
        .pill {
            display: inline-block;
            padding: 3px 10px;
            border-radius: 999px;
            font-size: 9px;
            font-weight: 700;
            text-transform: uppercase;
            letter-spacing: 0.08em;
            background: #e5e7eb;
            color: #374151;
        }
        .pill-sent { background: #dbeafe; color: #1e40af; }
        .pill-paid { background: #dcfce7; color: #166534; }
        .pill-void { background: #fee2e2; color: #991b1b; }
        .parties {
            display: flex;
            justify-content: space-between;
            margin-bottom: 18px;
        }
        .label {
            margin: 0 0 4px 0;
            font-size: 9px;
            color: #6b7280;
            text-transform: uppercase;
            letter-spacing: 0.08em;
        }
        .client { margin: 0; font-size: 14px; font-weight: 600; color: #111827; }
        .dates { text-align: right; }
        .dates p { margin: 0 0 4px 0; font-size: 11px; color: #1f2933; }
        .dates .label { display: inline-block; margin-right: 6px; }
        .card {
            background: #ffffff;
            border-radius: 12px;
            padding: 16px 18px;
            box-shadow: 0 6px 16px rgba(15, 23, 42, 0.08);
            margin-bottom: 16px;
        }
        h2 {
            font-size: 13px;
            margin: 0 0 10px 0;
            text-transform: uppercase;
            letter-spacing: 0.06em;
            color: #374151;
        }
        table.lines { width: 100%; border-collapse: collapse; font-size: 11px; }
        table.lines th {
            text-align: left;
            text-transform: uppercase;
            letter-spacing: 0.06em;
            font-size: 8px;
            color: #6b7280;
            border-bottom: 1px solid #e5e7eb;
            padding: 7px 8px;
        }
        table.lines td {
            padding: 8px;
            border-bottom: 1px solid #f3f4f6;
            color: #111827;
            vertical-align: top;
        }
        table.lines .num { text-align: right; white-space: nowrap; }
        table.lines .empty { color: #9ca3af; font-style: italic; text-align: center; }
        .totals {
            margin-top: 14px;
            margin-left: auto;
            width: 46%;
        }
        .totals .row {
            display: flex;
            justify-content: space-between;
            padding: 5px 0;
            font-size: 11px;
            color: #1f2933;
        }
        .totals .row strong { font-weight: 600; }
        .totals .total {
            border-top: 2px solid #111827;
            margin-top: 4px;
            padding-top: 8px;
            font-size: 14px;
        }
        .totals .total strong { font-weight: 700; }
        .notes p { margin: 0; font-size: 11px; color: #1f2933; white-space: pre-wrap; }
        .empty { color: #9ca3af; font-style: italic; }
"""


def generate_invoice_pdf(invoice_id: int) -> bytes:
    """Render an invoice to branded PDF bytes.

    Raises :class:`InvoicePdfError` when the invoice can't be found, is
    soft-deleted, or WeasyPrint can't render (missing native deps / render
    failure) so the public PDF view returns a clear 500 and the worker
    survives.
    """
    from billing import models

    invoice = (
        models.Invoice.objects.filter(deleted_at__isnull=True)
        .select_related("tenant")
        .prefetch_related("line_items")
        .filter(id=int(invoice_id))
        .first()
    )
    if invoice is None:
        raise InvoicePdfError(f"Invoice {invoice_id} not found.")

    line_items = list(invoice.line_items.all())
    document_html = _build_invoice_html(invoice, line_items)

    try:
        from weasyprint import CSS, HTML
    except Exception as exc:  # native deps missing / import-time failure
        raise InvoicePdfError(
            "PDF renderer is unavailable on this server."
        ) from exc

    try:
        css = CSS(string=_PDF_BASE_CSS + _INVOICE_CSS)
        return HTML(string=document_html).write_pdf(stylesheets=[css])
    except Exception as exc:
        raise InvoicePdfError("Failed to render invoice PDF.") from exc
