"""Branded PDF renderer for the Client Campaign Report.

``generate_campaign_report_pdf(request_id) -> bytes`` builds ONE
self-contained HTML document for a request's aggregate report and renders
it through WeasyPrint exactly once — the same engine + base styling the
per-recap / multi-recap PDFs use (``recaps.pdf._PDF_BASE_CSS`` +
``bytes_to_data_uri``).

Layout:
  - Branded header: brand (tenant) name, request title, date range.
  - KPI tiles (the eleven headline numbers).
  - Event table (name / date / location / recap count).
  - Photo grid (recap images embedded as data URIs).
  - BA roster.
  - A few consumer quotes.

WeasyPrint is imported lazily inside the render call (its native deps can
be missing at import time on some workers); a render failure raises
:class:`CampaignReportPdfError` so the public view turns it into a clean
500 instead of crashing the worker.
"""

from __future__ import annotations

import html as _html

from recaps import report_service
from recaps.pdf import _PDF_BASE_CSS, bytes_to_data_uri, safe
from recaps.report_charts import kpi_bar_svg
from utils.gcs import download_blob_bytes, extract_blob_name_from_url

# Embedding every photo as a base64 data URI is what makes the PDF
# portable, but each one is a GCS download + a base64 blow-up. Cap the
# embedded count well under the report's gallery cap so a huge campaign
# doesn't produce a 100MB PDF or a multi-minute render.
MAX_PDF_PHOTOS = 24


class CampaignReportPdfError(RuntimeError):
    """Raised when the report PDF can't be rendered (missing WeasyPrint
    native deps, or a render-time failure). The public view maps this to
    a 500 with a clear message rather than letting the worker crash."""


def _esc(value) -> str:
    """HTML-escape a value, rendering None/empty as an em dash."""
    if value in (None, ""):
        return "—"
    return _html.escape(str(value))


def _kpi_tiles(kpis: report_service.CampaignReportKpis) -> str:
    tiles = [
        ("Events", kpis.events),
        ("Recaps", kpis.recaps),
        ("Consumers Reached", kpis.consumers_reached),
        ("Samples Distributed", kpis.samples_distributed),
        ("Products Sold", kpis.products_sold),
        ("Cans Sold", kpis.cans_sold),
        ("Packs Sold", kpis.packs_sold),
        ("Total Engagements", kpis.total_engagements),
        ("First-Time", kpis.first_time_consumers),
        ("Brand Aware", kpis.brand_aware_consumers),
        ("Willing To Buy", kpis.willing_to_purchase),
    ]
    cells = "".join(
        f"<div class='kpi'><span>{_esc(label)}</span>"
        f"<strong>{int(value):,}</strong></div>"
        for label, value in tiles
    )
    return f"<div class='kpi-grid'>{cells}</div>"


def _format_event_date(iso_date: str | None) -> str:
    if not iso_date:
        return "—"
    from datetime import datetime

    try:
        return datetime.fromisoformat(iso_date).strftime("%b %-d, %Y")
    except Exception:
        return _esc(iso_date)


def _event_table(rows: list[report_service.CampaignReportEventRow]) -> str:
    if not rows:
        return "<p class='empty'>No events.</p>"
    body = "".join(
        f"<tr><td>{_esc(r.name)}</td>"
        f"<td>{_format_event_date(r.date)}</td>"
        f"<td>{_esc(r.city)}</td>"
        f"<td>{_esc(r.state)}</td>"
        f"<td class='num'>{int(r.recap_count)}</td></tr>"
        for r in rows
    )
    return (
        "<table class='evt'>"
        "<thead><tr><th>Event</th><th>Date</th><th>City</th>"
        "<th>State</th><th class='num'>Recaps</th></tr></thead>"
        f"<tbody>{body}</tbody></table>"
    )


def _photo_grid(photos: list[report_service.CampaignReportPhoto]) -> str:
    """Embed photo blobs as data URIs. Downloads each blob from GCS and
    base64-embeds it (same approach as the recap PDF) so the PDF is fully
    self-contained — no remote <img> fetches at view time."""
    figures: list[str] = []
    for photo in photos[:MAX_PDF_PHOTOS]:
        blob_name = extract_blob_name_from_url(photo.url)
        try:
            data = download_blob_bytes(blob_name)
        except Exception:
            data = None
        if not data:
            continue
        data_uri = bytes_to_data_uri(data)
        if not data_uri:
            continue
        caption = (
            f"<figcaption>{_esc(photo.caption)}</figcaption>"
            if photo.caption
            else ""
        )
        figures.append(f"<figure><img src='{data_uri}' />{caption}</figure>")
    if not figures:
        return "<p class='empty'>No photos.</p>"
    return f"<div class='gallery'>{''.join(figures)}</div>"


def _ba_roster(rows: list[report_service.CampaignReportBa]) -> str:
    if not rows:
        return "<p class='empty'>No ambassadors recorded.</p>"
    items = "".join(
        f"<li><strong>{_esc(ba.name)}</strong>"
        f"{' <em>(external)</em>' if ba.is_external else ''}"
        f" — {int(ba.event_count)} event{'s' if ba.event_count != 1 else ''}</li>"
        for ba in rows
    )
    return f"<ul class='roster'>{items}</ul>"


def _highlights(rows: list[report_service.CampaignReportQuote]) -> str:
    if not rows:
        return "<p class='empty'>No highlights captured.</p>"
    blocks = "".join(
        f"<blockquote>“{_esc(q.text)}”"
        f"{f'<cite>— {_esc(q.source)}</cite>' if q.source else ''}</blockquote>"
        for q in rows
    )
    return f"<div class='quotes'>{blocks}</div>"


def _build_report_html(data: report_service.CampaignReportData) -> str:
    return f"""
<!doctype html>
<html>
  <head>
    <meta charset="utf-8" />
    <title>{_esc(data.title)} — Campaign Report</title>
  </head>
  <body>
    <header class="report-header">
      <p class="eyebrow">{_esc(data.brand_name)} · Campaign Report</p>
      <h1>{_esc(data.title)}</h1>
      <p class="date-range">{_esc(data.date_range)}</p>
      <div class="accent"></div>
    </header>

    <section class="card">
      <h2>Performance</h2>
      {_kpi_tiles(data.kpis)}
      <div class="kpi-chart-wrap">{kpi_bar_svg(data.kpis)}</div>
    </section>

    <section class="card">
      <h2>Events</h2>
      {_event_table(data.events)}
    </section>

    <section class="card">
      <h2>Ambassadors</h2>
      {_ba_roster(data.ambassadors)}
    </section>

    <section class="card">
      <h2>Highlights</h2>
      {_highlights(data.highlights)}
    </section>

    <section class="card">
      <h2>Photo Gallery</h2>
      {_photo_grid(data.photos)}
    </section>
  </body>
</html>
"""


_REPORT_CSS = """
        h1 { font-size: 30px; margin: 0 0 4px 0; }
        h2 {
            font-size: 16px;
            margin: 0 0 12px 0;
            text-transform: uppercase;
            letter-spacing: 0.06em;
            color: #374151;
        }
        .report-header { margin-bottom: 22px; }
        .eyebrow {
            margin: 0 0 6px 0;
            font-size: 11px;
            color: #6b7280;
            text-transform: uppercase;
            letter-spacing: 0.16em;
        }
        .date-range { margin: 0 0 12px 0; font-size: 13px; color: #52606d; }
        .accent {
            width: 48px; height: 4px; background: #c5f546;
            border-radius: 2px;
        }
        .card {
            background: #ffffff;
            border-radius: 12px;
            padding: 16px 18px;
            box-shadow: 0 6px 16px rgba(15, 23, 42, 0.08);
            margin-bottom: 16px;
        }
        .kpi-grid {
            display: grid;
            grid-template-columns: repeat(4, 1fr);
            gap: 12px;
        }
        .kpi {
            background: #f5f6f8;
            border-radius: 10px;
            border-top: 3px solid #c5f546;
            padding: 12px 10px;
        }
        .kpi span {
            display: block;
            font-size: 8px;
            color: #6b7280;
            text-transform: uppercase;
            letter-spacing: 0.08em;
            margin-bottom: 6px;
        }
        .kpi strong { font-size: 20px; font-weight: 700; color: #111827; }
        .kpi-chart-wrap { margin-top: 16px; }
        .kpi-chart { display: block; width: 100%; }
        table.evt {
            width: 100%;
            border-collapse: collapse;
            font-size: 10px;
        }
        table.evt th {
            text-align: left;
            text-transform: uppercase;
            letter-spacing: 0.06em;
            font-size: 8px;
            color: #6b7280;
            border-bottom: 1px solid #e5e7eb;
            padding: 6px 8px;
        }
        table.evt td {
            padding: 6px 8px;
            border-bottom: 1px solid #f3f4f6;
            color: #111827;
        }
        table.evt .num { text-align: right; }
        .roster { margin: 0; padding-left: 18px; font-size: 11px; }
        .roster li { margin-bottom: 5px; color: #111827; }
        .roster em { color: #6b7280; font-style: italic; }
        .quotes blockquote {
            margin: 0 0 12px 0;
            padding: 10px 14px;
            background: #f5f6f8;
            border-left: 3px solid #c5f546;
            border-radius: 0 8px 8px 0;
            font-size: 11px;
            color: #1f2933;
        }
        .quotes cite {
            display: block;
            margin-top: 6px;
            font-size: 9px;
            color: #6b7280;
            font-style: normal;
        }
        .gallery {
            display: grid;
            grid-template-columns: repeat(3, 1fr);
            gap: 12px;
        }
        .gallery figure {
            margin: 0;
            background: #f3f4f6;
            border-radius: 10px;
            padding: 8px;
            text-align: center;
        }
        .gallery img {
            max-width: 100%;
            max-height: 180px;
            object-fit: contain;
            border-radius: 6px;
        }
        .gallery figcaption {
            margin-top: 6px;
            font-size: 8px;
            color: #6b7280;
        }
        .empty { margin: 0; color: #9ca3af; font-style: italic; }
"""


def generate_campaign_report_pdf(request_id: int) -> bytes:
    """Render a request's campaign report to branded PDF bytes.

    Raises :class:`CampaignReportPdfError` when the request can't be found
    or WeasyPrint can't render (missing native deps / render failure) so
    the public PDF view returns a clear 500 and the worker survives.
    """
    from datetime import datetime, timezone as _tz

    request_obj = report_service.get_report_request(int(request_id))
    if request_obj is None:
        raise CampaignReportPdfError(f"Request {request_id} not found.")

    data = report_service.build_campaign_report(
        request_obj, generated_at=datetime.now(_tz.utc).isoformat()
    )
    document_html = _build_report_html(data)

    try:
        from weasyprint import CSS, HTML
    except Exception as exc:  # native deps missing / import-time failure
        raise CampaignReportPdfError(
            "PDF renderer is unavailable on this server."
        ) from exc

    try:
        css = CSS(string=_PDF_BASE_CSS + _REPORT_CSS)
        return HTML(string=document_html).write_pdf(stylesheets=[css])
    except Exception as exc:
        raise CampaignReportPdfError("Failed to render campaign report PDF.") from exc
