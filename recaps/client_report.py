"""Branded PDF for the scheduled MONTHLY client performance report.

``build_client_monthly_report_pdf(tenant_id, year, month) -> bytes`` builds
ONE self-contained PDF summarizing a single tenant's program for one calendar
month — the deliverable the ``send_scheduled_client_reports`` cron emails to a
brand's client contacts each month.

This is the tenant-monthly sibling of :mod:`recaps.report_pdf` (which rolls up
ONE :class:`events.models.Request` campaign) and it deliberately REUSES the
same infrastructure so the output looks consistent with the rest of the report
surface:

* the WeasyPrint renderer + base styling — :data:`recaps.pdf._PDF_BASE_CSS`,
  imported lazily so a worker missing WeasyPrint's native deps fails cleanly
  (raises :class:`ClientMonthlyReportError`) rather than crashing at import;
* the hand-built inline KPI bar chart — :func:`recaps.report_charts.kpi_bar_svg`
  (the ``TenantKpiTotals`` it is fed exposes the SAME attribute names the chart
  reads, so no adapter is needed);
* the period numbers — the calendar-month window cores in
  :mod:`recaps.tenant_overview` (:func:`_tenant_kpi_totals_window` +
  :func:`tenant_monthly_trend`), so the PDF reconciles exactly with the
  dashboard's ``tenantKpis`` for the same span;
* the narrative — the deterministic insight buckets
  (:func:`recaps.tenant_insights.build_insight_buckets`) and, when available,
  the cached consumer-sentiment read
  (:func:`recaps.tenant_sentiment.get_or_refresh_tenant_sentiment`).

Layout (intentionally a clean, single-purpose summary — see the "trimmed"
note below):

  - Branded header: tenant name + "{Month YYYY} Performance".
  - KPI tiles: the period's eleven headline numbers (events / recaps + the
    nine summable KPIs).
  - One chart: the period's KPI bar chart.
  - Monthly trend: a compact trailing-12-month table (recaps / engagements /
    samples per month) so the client sees the shape over time.
  - Highlights: the deterministic insight bullets.
  - Sentiment: a one-line consumer-sentiment summary, only when a cached read
    is available (the cron passes ``include_sentiment=False`` when it must not
    trigger a fresh AI call).

TRIMMED from the MVP (noted in the PR): per-recap photo thumbnails. Embedding
photos means a GCS download + base64 blow-up per image, which is the heaviest
and most failure-prone part of the campaign PDF and adds little to a numbers-
first monthly summary. The KPI/trend/insight/sentiment content above is the
core deliverable; photos can be layered on later (the campaign PDF's
``_photo_grid`` is the reuse target).
"""

from __future__ import annotations

import html as _html

from recaps.pdf import _PDF_BASE_CSS
from recaps.report_charts import kpi_bar_svg
from recaps.tenant_insights import build_insight_buckets
from recaps.tenant_overview import (
    TenantKpiTotals,
    _add_months,
    _month_start,
    _tenant_event_recap_counts_window,
    _tenant_kpi_totals_window,
    tenant_monthly_trend,
)
from tenants.models import Tenant

# Month names indexed 1..12 (index 0 unused) for the "May 2026" header label.
# Hard-coded (not strftime) so the label is locale-independent and stable
# across environments — same approach tenant_overview._MONTH_ABBR uses.
_MONTH_NAMES = (
    "",
    "January",
    "February",
    "March",
    "April",
    "May",
    "June",
    "July",
    "August",
    "September",
    "October",
    "November",
    "December",
)

# How many trailing months of the monthly trend to show in the table. The
# trend helper already bounds itself to MONTHLY_TREND_MONTHS (12); we render
# the same bounded series.
_TREND_TABLE_MONTHS = 12

# Map the insight-bucket sentiment vocabulary onto a small set of CSS classes
# so a bucket's tone is reflected by its accent color in the PDF.
_SENTIMENT_CLASS = {
    "positive": "pos",
    "attention": "warn",
    "neutral": "neu",
}


class ClientMonthlyReportError(RuntimeError):
    """Raised when the monthly report PDF can't be rendered.

    Mirrors :class:`recaps.report_pdf.CampaignReportPdfError`: a missing
    tenant, or WeasyPrint's native deps / a render-time failure, surfaces as
    this so the cron logs a clean per-tenant error and moves on instead of
    crashing the whole run.
    """


def _esc(value) -> str:
    """HTML-escape a value, rendering None/empty as an em dash."""
    if value in (None, ""):
        return "—"
    return _html.escape(str(value))


def _month_label(year: int, month: int) -> str:
    """Human "Month YYYY" label for the header (e.g. "May 2026")."""
    name = _MONTH_NAMES[month] if 1 <= month <= 12 else str(month)
    return f"{name} {year}"


def _month_window(year: int, month: int) -> tuple:
    """Half-open ``[first-of-month, first-of-next-month)`` for ``year``-``month``.

    Built from :func:`recaps.tenant_overview._month_start` /
    :func:`_add_months` so it is identical in shape (and tzinfo) to the windows
    the comparison/year code uses — which is what lets the PDF's totals
    reconcile with ``tenant_kpi_totals`` for the same span.
    """
    start = _month_start(year, month)
    end = _month_start(*_add_months(year, month, 1))
    return start, end


def _kpi_tiles(events: int, recaps: int, kpis: TenantKpiTotals) -> str:
    """The eleven headline numbers as a tile grid (counts + the nine KPIs)."""
    tiles = [
        ("Events", events),
        ("Recaps", recaps),
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
        f"<strong>{int(value or 0):,}</strong></div>"
        for label, value in tiles
    )
    return f"<div class='kpi-grid'>{cells}</div>"


def _short_month(month_key: str) -> str:
    """Render a ``"YYYY-MM"`` trend key as ``"Mon YYYY"`` (verbatim on parse fail)."""
    try:
        year_s, mm = month_key.split("-")
        idx = int(mm)
    except (ValueError, AttributeError):
        return month_key
    if 1 <= idx <= 12:
        # Reuse the 3-letter abbreviation from the full month-name table.
        return f"{_MONTH_NAMES[idx][:3]} {year_s}"
    return month_key


def _trend_table(trend: list) -> str:
    """A compact trailing-months table: month, recaps, engagements, samples.

    ``trend`` is the :func:`recaps.tenant_overview.tenant_monthly_trend` series
    (already bounded + zero-filled). Rendered oldest→newest so the client reads
    the program's shape over time alongside the highlighted month.
    """
    rows = list(trend)[-_TREND_TABLE_MONTHS:]
    if not rows:
        return "<p class='empty'>No monthly activity yet.</p>"
    body = "".join(
        f"<tr><td>{_esc(_short_month(m.month))}</td>"
        f"<td class='num'>{int(m.recaps or 0):,}</td>"
        f"<td class='num'>{int(m.engagements or 0):,}</td>"
        f"<td class='num'>{int(m.samples or 0):,}</td></tr>"
        for m in rows
    )
    return (
        "<table class='trend'>"
        "<thead><tr><th>Month</th><th class='num'>Recaps</th>"
        "<th class='num'>Engagements</th><th class='num'>Samples</th></tr></thead>"
        f"<tbody>{body}</tbody></table>"
    )


def _highlights(buckets: list[dict]) -> str:
    """The deterministic insight buckets as accent-colored highlight rows."""
    if not buckets:
        return "<p class='empty'>No highlights for this period yet.</p>"
    rows: list[str] = []
    for bucket in buckets:
        sentiment = bucket.get("sentiment") or "neutral"
        cls = _SENTIMENT_CLASS.get(sentiment, "neu")
        title = _esc(bucket.get("title"))
        metric = _esc(bucket.get("metric"))
        detail = _esc(bucket.get("detail"))
        rows.append(
            f"<div class='insight {cls}'>"
            f"<div class='insight-head'><span class='insight-title'>{title}</span>"
            f"<span class='insight-metric'>{metric}</span></div>"
            f"<p class='insight-detail'>{detail}</p></div>"
        )
    return f"<div class='insights'>{''.join(rows)}</div>"


def _sentiment_line(sentiment: dict | None) -> str:
    """A one-line consumer-sentiment summary, or '' when there's nothing to show.

    ``sentiment`` is the cleaned payload from
    :func:`recaps.tenant_sentiment.get_or_refresh_tenant_sentiment`
    (``{overall_sentiment, positive_pct, summary, themes, quotes}``) or None.
    Renders the overall label + positive-% and the one-line summary; returns an
    empty string when no sentiment is available so the section is omitted
    entirely rather than showing an empty card.
    """
    if not sentiment:
        return ""
    summary = (sentiment.get("summary") or "").strip()
    if not summary:
        return ""
    overall = _esc(sentiment.get("overall_sentiment") or "mixed")
    try:
        pct = int(sentiment.get("positive_pct") or 0)
    except (TypeError, ValueError):
        pct = 0
    return (
        "<section class='card'>"
        "<h2>What consumers are saying</h2>"
        f"<p class='sentiment-meta'>Overall: <strong>{overall}</strong> · "
        f"{pct}% positive</p>"
        f"<p class='sentiment-summary'>{_esc(summary)}</p>"
        "</section>"
    )


def _build_report_html(
    *,
    tenant_name: str,
    period_label: str,
    events: int,
    recaps: int,
    kpis: TenantKpiTotals,
    trend: list,
    buckets: list[dict],
    sentiment: dict | None,
) -> str:
    return f"""
<!doctype html>
<html>
  <head>
    <meta charset="utf-8" />
    <title>{_esc(tenant_name)} — {_esc(period_label)} Performance</title>
  </head>
  <body>
    <header class="report-header">
      <p class="eyebrow">{_esc(tenant_name)} · Monthly Performance</p>
      <h1>{_esc(period_label)} Performance</h1>
      <div class="accent"></div>
    </header>

    <section class="card">
      <h2>Performance</h2>
      {_kpi_tiles(events, recaps, kpis)}
      <div class="kpi-chart-wrap">{kpi_bar_svg(kpis)}</div>
    </section>

    <section class="card">
      <h2>Monthly Trend</h2>
      {_trend_table(trend)}
    </section>

    <section class="card">
      <h2>Highlights</h2>
      {_highlights(buckets)}
    </section>

    {_sentiment_line(sentiment)}
  </body>
</html>
"""


# Report-specific styling layered on top of recaps.pdf._PDF_BASE_CSS. Mirrors
# recaps.report_pdf._REPORT_CSS (same palette, card, kpi-grid, chart, table
# rules) so the monthly report and the campaign report look like one family,
# plus the insight-row + sentiment styling unique to this report.
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
        table.trend {
            width: 100%;
            border-collapse: collapse;
            font-size: 10px;
        }
        table.trend th {
            text-align: left;
            text-transform: uppercase;
            letter-spacing: 0.06em;
            font-size: 8px;
            color: #6b7280;
            border-bottom: 1px solid #e5e7eb;
            padding: 6px 8px;
        }
        table.trend td {
            padding: 6px 8px;
            border-bottom: 1px solid #f3f4f6;
            color: #111827;
        }
        table.trend .num { text-align: right; }
        .insights { display: grid; grid-template-columns: repeat(1, 1fr); gap: 10px; }
        .insight {
            background: #f5f6f8;
            border-left: 3px solid #9ca3af;
            border-radius: 0 8px 8px 0;
            padding: 10px 14px;
        }
        .insight.pos { border-left-color: #c5f546; }
        .insight.warn { border-left-color: #f59e0b; }
        .insight.neu { border-left-color: #9ca3af; }
        .insight-head {
            display: flex;
            justify-content: space-between;
            align-items: baseline;
        }
        .insight-title {
            font-size: 11px;
            font-weight: 700;
            color: #111827;
            text-transform: uppercase;
            letter-spacing: 0.06em;
        }
        .insight-metric { font-size: 13px; font-weight: 700; color: #111827; }
        .insight-detail { margin: 4px 0 0 0; font-size: 11px; color: #1f2933; }
        .sentiment-meta { margin: 0 0 6px 0; font-size: 11px; color: #52606d; }
        .sentiment-summary { margin: 0; font-size: 12px; color: #111827; }
        .empty { margin: 0; color: #9ca3af; font-style: italic; }
"""


def build_client_monthly_report_pdf(
    tenant_id: int,
    year: int,
    month: int,
    *,
    include_sentiment: bool = True,
) -> bytes:
    """Render one tenant's monthly performance report to branded PDF bytes.

    Aggregates the calendar month ``year``-``month`` for ``tenant_id`` — the
    event/recap counts + nine summable KPIs over that month's half-open window
    (reconciling with ``tenant_kpi_totals`` for the span), the trailing monthly
    trend, the deterministic insight buckets, and (when ``include_sentiment``)
    the cached consumer-sentiment read — and renders the branded layout through
    WeasyPrint exactly once.

    ``include_sentiment=False`` skips the sentiment lookup entirely (the
    snapshot front door can trigger a fresh AI call on a cache miss; the cron
    leaves it on, but callers that must guarantee zero AI cost can turn it
    off). A None/empty sentiment simply omits that section.

    Returns the PDF bytes. Raises :class:`ClientMonthlyReportError` when the
    tenant doesn't exist or WeasyPrint can't render (missing native deps /
    render failure), so the cron turns it into a clean per-tenant skip instead
    of crashing the run. WeasyPrint is imported lazily inside the render call
    for the same reason :mod:`recaps.report_pdf` does.
    """
    try:
        tenant = Tenant.objects.get(id=tenant_id)
    except Tenant.DoesNotExist as exc:
        raise ClientMonthlyReportError(f"Tenant {tenant_id} not found.") from exc

    window = _month_window(year, month)
    events, recaps = _tenant_event_recap_counts_window(tenant_id, window)
    kpis = _tenant_kpi_totals_window(tenant_id, window)

    # Trend + insights are program-level (trailing window / all-time) context,
    # the same series the dashboard shows; they intentionally aren't scoped to
    # the single month so the client sees the month IN CONTEXT.
    trend = tenant_monthly_trend(tenant_id)
    buckets = build_insight_buckets(tenant_id)

    sentiment = None
    if include_sentiment:
        # Lazy import keeps this module importable without the AI client and
        # mirrors the degrade posture: any failure leaves sentiment None.
        try:
            from recaps.tenant_sentiment import get_or_refresh_tenant_sentiment

            sentiment, _generated_at = get_or_refresh_tenant_sentiment(tenant_id)
        except Exception:
            sentiment = None

    document_html = _build_report_html(
        tenant_name=tenant.name or "—",
        period_label=_month_label(year, month),
        events=events,
        recaps=recaps,
        kpis=kpis,
        trend=trend,
        buckets=buckets,
        sentiment=sentiment,
    )

    try:
        from weasyprint import CSS, HTML
    except Exception as exc:  # native deps missing / import-time failure
        raise ClientMonthlyReportError(
            "PDF renderer is unavailable on this server."
        ) from exc

    try:
        css = CSS(string=_PDF_BASE_CSS + _REPORT_CSS)
        return HTML(string=document_html).write_pdf(stylesheets=[css])
    except Exception as exc:
        raise ClientMonthlyReportError(
            "Failed to render client monthly report PDF."
        ) from exc
