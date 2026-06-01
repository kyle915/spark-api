"""Hand-built inline SVG charts for the Client Campaign Report PDF.

WeasyPrint renders SVG natively, so the report can carry a real chart
without a JS chart library or matplotlib (neither of which the PDF worker
has). :func:`kpi_bar_svg` returns a self-contained ``<svg>...</svg>``
string — a horizontal bar chart of the campaign's most meaningful KPIs —
that ``recaps.report_pdf`` injects straight into the report HTML as raw
markup (it is already valid, escaped XML, so it must NOT be HTML-escaped
again at the injection site).

The output is deterministic and bounded: a fixed set of (at most seven)
KPI rows, a fixed canvas width, and a height that grows only with the
number of non-omitted rows. Bars are scaled proportionally to the largest
value on the chart. The all-zero / no-data case renders a small "No
performance data yet." note instead of an empty axis.

Colors match the report's existing palette
(``recaps.report_pdf._REPORT_CSS``): the ``#c5f546`` brand accent for the
bars, ``#111827`` for value labels, ``#6b7280`` for axis/category labels,
and ``#e5e7eb`` for the bar track / baseline.
"""

from __future__ import annotations

import html as _html

# --- Palette (mirrors recaps.report_pdf._REPORT_CSS) -----------------------
_BAR_FILL = "#c5f546"          # brand accent green
_BAR_TRACK = "#eef0f3"         # faint full-width track behind each bar
_VALUE_COLOR = "#111827"       # near-black, matches .kpi strong
_LABEL_COLOR = "#6b7280"       # muted gray, matches .kpi span / axis labels
_AXIS_COLOR = "#e5e7eb"        # hairline baseline / gridline
_EMPTY_COLOR = "#9ca3af"       # matches .empty italic note

# --- Geometry (all fixed → deterministic + bounded output) -----------------
_WIDTH = 720                   # canvas width (px); scales to the card width
_PAD_LEFT = 168                # room for the category labels
_PAD_RIGHT = 64                # room for the value label past the bar end
_PAD_TOP = 14
_PAD_BOTTOM = 14
_ROW_HEIGHT = 30               # vertical pitch per KPI row
_BAR_HEIGHT = 16               # bar thickness within a row
_FONT = "Helvetica, Arial, sans-serif"


def _esc(value) -> str:
    """XML-escape a value for safe inlining into the SVG text nodes."""
    return _html.escape(str(value), quote=True)


def _kpi_rows(kpis) -> list[tuple[str, int]]:
    """The (label, value) pairs the chart visualizes, in display order.

    Reads attributes defensively (``getattr`` + ``int`` coercion) so a
    namedtuple, a dataclass, or any duck-typed stand-in works, and a None
    on any field degrades to 0 rather than raising.
    """
    spec = [
        ("Consumers Reached", "consumers_reached"),
        ("Samples Distributed", "samples_distributed"),
        ("Products Sold", "products_sold"),
        ("Total Engagements", "total_engagements"),
        ("First-Time", "first_time_consumers"),
        ("Brand-Aware", "brand_aware_consumers"),
        ("Willing To Purchase", "willing_to_purchase"),
    ]
    rows: list[tuple[str, int]] = []
    for label, attr in spec:
        try:
            value = int(getattr(kpis, attr, 0) or 0)
        except (TypeError, ValueError):
            value = 0
        rows.append((label, max(value, 0)))
    return rows


def _empty_svg(message: str = "No performance data yet.") -> str:
    """A tiny standalone SVG note for the all-zero / no-data case."""
    height = _PAD_TOP + _ROW_HEIGHT + _PAD_BOTTOM
    return (
        f'<svg class="kpi-chart" xmlns="http://www.w3.org/2000/svg" '
        f'viewBox="0 0 {_WIDTH} {height}" width="100%" '
        f'role="img" aria-label="{_esc(message)}">'
        f'<text x="{_WIDTH // 2}" y="{height // 2}" '
        f'text-anchor="middle" dominant-baseline="middle" '
        f'font-family="{_FONT}" font-size="12" font-style="italic" '
        f'fill="{_EMPTY_COLOR}">{_esc(message)}</text>'
        f"</svg>"
    )


def kpi_bar_svg(kpis) -> str:
    """Return an inline ``<svg>`` horizontal bar chart of campaign KPIs.

    ``kpis`` is a :class:`recaps.report_service.CampaignReportKpis` (or any
    object/namedtuple exposing the same attribute names). The bars are
    scaled to the largest value present; each row shows its category label
    (left) and exact value (right of the bar). Rows whose value is 0 are
    omitted so a sparse campaign doesn't show a wall of empty bars — unless
    *every* row is 0, in which case a "No performance data yet." note is
    rendered instead.

    The returned string is valid, already-escaped SVG/XML and is meant to
    be injected as RAW markup (do not HTML-escape it again).
    """
    rows = [(label, value) for label, value in _kpi_rows(kpis) if value > 0]
    if not rows:
        return _empty_svg()

    max_value = max(value for _, value in rows)
    # max_value > 0 is guaranteed (we filtered to value > 0).

    bar_area = _WIDTH - _PAD_LEFT - _PAD_RIGHT
    height = _PAD_TOP + _ROW_HEIGHT * len(rows) + _PAD_BOTTOM
    label_baseline_offset = _BAR_HEIGHT / 2 + 4  # ~vertical centering of text

    parts: list[str] = [
        f'<svg class="kpi-chart" xmlns="http://www.w3.org/2000/svg" '
        f'viewBox="0 0 {_WIDTH} {height}" width="100%" '
        f'role="img" aria-label="Campaign KPI bar chart">'
    ]

    # Baseline at the left edge of the bar area.
    parts.append(
        f'<line x1="{_PAD_LEFT}" y1="{_PAD_TOP}" '
        f'x2="{_PAD_LEFT}" y2="{height - _PAD_BOTTOM}" '
        f'stroke="{_AXIS_COLOR}" stroke-width="1" />'
    )

    for index, (label, value) in enumerate(rows):
        row_top = _PAD_TOP + index * _ROW_HEIGHT
        bar_y = row_top + (_ROW_HEIGHT - _BAR_HEIGHT) / 2
        text_y = bar_y + label_baseline_offset
        bar_w = (value / max_value) * bar_area
        # Keep a non-zero sliver so the smallest bar is still visible.
        bar_w = max(bar_w, 2.0)

        # Category label (right-aligned into the left gutter).
        parts.append(
            f'<text x="{_PAD_LEFT - 10}" y="{text_y:.1f}" '
            f'text-anchor="end" font-family="{_FONT}" font-size="11" '
            f'fill="{_LABEL_COLOR}">{_esc(label)}</text>'
        )
        # Faint full-width track behind the bar.
        parts.append(
            f'<rect x="{_PAD_LEFT}" y="{bar_y:.1f}" '
            f'width="{bar_area}" height="{_BAR_HEIGHT}" rx="3" '
            f'fill="{_BAR_TRACK}" />'
        )
        # The value bar.
        parts.append(
            f'<rect x="{_PAD_LEFT}" y="{bar_y:.1f}" '
            f'width="{bar_w:.1f}" height="{_BAR_HEIGHT}" rx="3" '
            f'fill="{_BAR_FILL}" />'
        )
        # Value label just past the end of the bar.
        parts.append(
            f'<text x="{_PAD_LEFT + bar_w + 8:.1f}" y="{text_y:.1f}" '
            f'text-anchor="start" font-family="{_FONT}" font-size="11" '
            f'font-weight="700" fill="{_VALUE_COLOR}">{value:,}</text>'
        )

    parts.append("</svg>")
    return "".join(parts)
