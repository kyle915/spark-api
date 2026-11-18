"""The client recap deck as one HTML document, for WeasyPrint.

16:9 slides (13.33 × 7.5 in), one per PDF page. Restrained on purpose:
white paper, ink type, the Spark mark, and the lime accent only on the
cover and the bars' value marks. No gradients, glows, shadows or accent
stripes; sentence case; nothing set under 12px.

Each slide has a fixed size and a text budget: long text steps down a type
size instead of running off the page, and lists are capped (the rest is
counted, never silently dropped).

Pages, in order (empty ones are skipped):

1. Cover — brand, title, dates, lead photo.
2. At a glance — the headline numbers and the no-product note.
3. Summary — the written summary and the takeaways.
4. Where it ran — states, top stores and the weekly trend.
5. What consumers said — themes or quotes.
6. From the field — photos, six to a page.
7. Next steps — when there are any.
8. How we counted — the basis, and the visits left out.
"""

from __future__ import annotations

import html as _html
from datetime import date

MAX_STATE_BARS = 8
MAX_STORE_ROWS = 8
MAX_WEEK_BARS = 12
MAX_LEFT_OUT_ROWS = 12
PHOTOS_PER_SLIDE = 6
MAX_LABEL_CHARS = 28
COLUMN_MAX_PX = 92

FONT_STACK = '"Helvetica Neue", "Liberation Sans", Helvetica, Arial, sans-serif'


def _esc(value) -> str:
    return _html.escape(str(value if value is not None else ""))


def _short_date(iso: str | None) -> str:
    try:
        d = date.fromisoformat(str(iso or "")[:10])
    except ValueError:
        return ""
    return f"{d.strftime('%b')} {d.day}"


def _clip(text: str, limit: int = MAX_LABEL_CHARS) -> str:
    text = " ".join(str(text or "").split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _size_class(chars: int, steps: tuple[int, int]) -> str:
    """``""`` / ``"smaller"`` / ``"smallest"`` by how much text there is."""
    if chars <= steps[0]:
        return ""
    if chars <= steps[1]:
        return "smaller"
    return "smallest"


def _plural(count: int, one: str, many: str | None = None) -> str:
    return f"{count:,} {one if count == 1 else (many or one + 's')}"


class _Deck:
    def __init__(self, brand: str, period: str, mark: str):
        self.brand = brand
        self.period = period
        self.mark = mark
        self.slides: list[str] = []

    def slide(self, title: str, body: str, extra_class: str = "") -> None:
        number = len(self.slides) + 1
        self.slides.append(
            f"<section class='slide {extra_class}'>"
            "<header class='top'>"
            f"<span class='kicker'>{_esc(self.brand)} · {_esc(self.period)}</span>"
            f"<img class='mark' src='{self.mark}' alt='Spark by Ignite' />"
            "</header>"
            f"<h2>{_esc(title)}</h2>"
            f"<div class='body'>{body}</div>"
            "<footer class='foot'>"
            "<span>Prepared by Ignite Productions</span>"
            f"<span class='num'>{number}</span>"
            "</footer></section>"
        )


def _bars(rows: list[tuple[str, int]], css: str = "") -> str:
    peak = max((value for _label, value in rows), default=0)
    if peak <= 0:
        return ""
    out = []
    for label, value in rows:
        width = max(1.5, round(100 * value / peak, 1))
        out.append(
            "<div class='bar-row'>"
            f"<div class='bar-label'>{_esc(_clip(label))}</div>"
            f"<div class='bar-track'><div class='bar-fill' style='width:{width}%'></div></div>"
            f"<div class='bar-value'>{value:,}</div>"
            "</div>"
        )
    return f"<div class='bars {css}'>{''.join(out)}</div>"


def _columns(rows: list[tuple[str, int]]) -> str:
    """A column chart as a one-row table (robust in WeasyPrint): the value
    over each column, the label under it."""
    peak = max((value for _label, value in rows), default=0)
    if peak <= 0:
        return ""
    cells = "".join(
        "<td>"
        f"<span class='col-value'>{value:,}</span>"
        f"<div class='col-bar' style='height:{max(2, round(COLUMN_MAX_PX * value / peak))}px'></div>"
        "</td>"
        for _label, value in rows
    )
    labels = "".join(f"<td>{_esc(label)}</td>" for label, _value in rows)
    return (
        "<table class='columns'>"
        f"<tr class='col-bars'>{cells}</tr>"
        f"<tr class='col-labels'>{labels}</tr>"
        "</table>"
    )


def _photo_figure(photo: dict, data_uri: str) -> str:
    lines = []
    if photo.get("caption"):
        lines.append(f"<span class='cap-main'>{_esc(_clip(photo['caption'], 70))}</span>")
    where = " · ".join(
        part
        for part in (
            _clip(photo.get("store") or "", 34),
            photo.get("state"),
            _short_date(photo.get("event_date")),
        )
        if part
    )
    if where:
        lines.append(f"<span class='cap-meta'>{_esc(where)}</span>")
    caption = f"<figcaption>{''.join(lines)}</figcaption>" if lines else ""
    return f"<figure><div class='frame'><img src='{data_uri}' alt='' /></div>{caption}</figure>"


def build_deck_html(
    *,
    brand: str,
    title: str,
    period: str,
    tiles: list[dict],
    basis_note: str,
    summary: str,
    takeaways: list[str],
    weeks: list[tuple[str, int]],
    stores: list[tuple[str, int]],
    states: list[tuple[str, int]],
    where_line: str,
    themes: list[dict],
    quotes: list[str],
    photos: list[tuple[dict, str]],
    next_steps: list[str],
    method: list[str],
    left_out: list[dict],
    left_out_total: int,
    mark: str,
    mark_on_dark: str,
) -> str:
    """The deck as one HTML document. Every argument is already clean and
    client-safe; this function only lays it out."""
    deck = _Deck(brand, period, mark)

    # 1. Cover.
    hero = photos[0][1] if photos else ""
    cover = (
        "<section class='cover" + (" with-photo" if hero else "") + "'>"
        "<div class='cover-panel'>"
        f"<img class='mark' src='{mark_on_dark}' alt='Spark by Ignite' />"
        "<div class='cover-words'>"
        f"<p class='cover-brand'>{_esc(brand)}</p>"
        f"<h1>{_esc(title or brand)}</h1>"
        f"<p class='cover-period'>{_esc(period)}</p>"
        "</div>"
        "<p class='cover-foot'>Campaign recap · Prepared by Ignite Productions</p>"
        "</div>"
        + (f"<div class='cover-photo'><img src='{hero}' alt='' /></div>" if hero else "")
        + "</section>"
    )

    # 2. At a glance.
    if tiles:
        grid = "".join(
            f"<div class='kpi'><strong>{int(t['value']):,}</strong><span>{_esc(t['label'])}</span></div>"
            for t in tiles
        )
        rows = "two-rows" if len(tiles) > 4 else "one-row"
        note = f"<p class='basis'>{_esc(basis_note)}</p>" if basis_note else ""
        deck.slide("At a glance", f"<div class='kpi-grid {rows}'>{grid}</div>{note}")

    # 3. Summary and takeaways.
    summary_pars = [p.strip() for p in str(summary or "").split("\n") if p.strip()]
    takeaway_items = [t for t in takeaways if str(t).strip()]
    if summary_pars or takeaway_items:
        summary_size = _size_class(sum(len(p) for p in summary_pars), (650, 1000))
        take_size = _size_class(sum(len(t) for t in takeaway_items), (520, 900))
        left = (
            f"<div class='summary {summary_size}'>"
            + "".join(f"<p>{_esc(p)}</p>" for p in summary_pars)
            + "</div>"
            if summary_pars
            else ""
        )
        right = (
            f"<div class='takeaways {take_size}'><h3>Takeaways</h3><ol>"
            + "".join(
                f"<li><span class='n'>{i:02d}</span><span class='t'>{_esc(t)}</span></li>"
                for i, t in enumerate(takeaway_items, start=1)
            )
            + "</ol></div>"
            if takeaway_items
            else ""
        )
        layout = "two-col" if left and right else "one-col"
        deck.slide("Summary", f"<div class='{layout}'>{left}{right}</div>")

    # 4. Where it ran: states and top stores, then the weekly trend.
    panels = []
    if states:
        panels.append(
            "<div class='panel'><h3>Consumers sampled by state</h3>"
            + _bars(states[:MAX_STATE_BARS])
            + "</div>"
        )
    if stores:
        panels.append(
            "<div class='panel'><h3>Top stores by consumers sampled</h3>"
            + _bars(stores[:MAX_STORE_ROWS])
            + "</div>"
        )
    trend = weeks[-MAX_WEEK_BARS:]
    columns = _columns(trend) if len(trend) > 1 and any(v for _l, v in trend) else ""
    if panels or columns:
        line = f"<p class='lede'>{_esc(where_line)}</p>" if where_line else ""
        body = line
        if panels:
            body += f"<div class='panels n{len(panels)}'>{''.join(panels)}</div>"
        if columns:
            body += f"<div class='trend'><h3>Consumers sampled by week (week starting)</h3>{columns}</div>"
        deck.slide("Where it ran", body)

    # 5. What consumers said.
    if themes:
        items = "".join(
            "<div class='theme'>"
            f"<p class='theme-label'>{_esc(t['label'])}"
            + (
                f"<span> · noted on {_plural(int(t['recap_count']), 'recap')}</span>"
                if t.get("recap_count")
                else ""
            )
            + "</p>"
            + (f"<blockquote>{_esc(t['quote'])}</blockquote>" if t.get("quote") else "")
            + "</div>"
            for t in themes[:6]
        )
        deck.slide("What consumers said", f"<div class='quote-grid'>{items}</div>")
    elif quotes:
        items = "".join(
            f"<div class='quote'><blockquote>“{_esc(q)}”</blockquote></div>" for q in quotes[:6]
        )
        deck.slide(
            "What consumers said",
            f"<div class='quote-grid'>{items}</div>"
            "<p class='basis'>As noted by the field team on approved recaps, trimmed for length.</p>",
        )

    # 6. Photos. The first is the cover, so it is not repeated unless it is
    # the only one.
    gallery = photos[1:] if len(photos) > 1 else photos
    for start in range(0, len(gallery), PHOTOS_PER_SLIDE):
        group = gallery[start : start + PHOTOS_PER_SLIDE]
        figures = "".join(_photo_figure(photo, uri) for photo, uri in group)
        deck.slide("From the field", f"<div class='photo-grid'>{figures}</div>")

    # 7. Next steps.
    steps = [s for s in next_steps if str(s).strip()]
    if steps:
        size = _size_class(sum(len(s) for s in steps), (420, 800))
        deck.slide(
            "Next steps",
            f"<ol class='steps {size}'>"
            + "".join(
                f"<li><span class='n'>{i:02d}</span><span class='t'>{_esc(s)}</span></li>"
                for i, s in enumerate(steps, start=1)
            )
            + "</ol>",
        )

    # 8. How we counted.
    basis = "<ul class='method'>" + "".join(f"<li>{_esc(m)}</li>" for m in method) + "</ul>"
    table = ""
    if left_out:
        rows = "".join(
            "<tr>"
            f"<td>{_esc(_short_date(v.get('day')))}</td>"
            f"<td>{_esc(_clip(v.get('store') or 'Store not recorded', 34))}</td>"
            f"<td>{_esc(v.get('state') or '')}</td>"
            f"<td>{_esc(v.get('label') or '')}</td>"
            "</tr>"
            for v in left_out[:MAX_LEFT_OUT_ROWS]
        )
        more = left_out_total - min(len(left_out), MAX_LEFT_OUT_ROWS)
        table = (
            "<div class='left-out'><h3>No-product visits left out</h3>"
            "<table><thead><tr><th>Date</th><th>Store</th><th>State</th><th>Why</th></tr></thead>"
            f"<tbody>{rows}</tbody></table>"
            + (f"<p class='basis'>And {_plural(more, 'more visit')}.</p>" if more > 0 else "")
            + "</div>"
        )
    deck.slide(
        "How we counted",
        f"<div class='{'two-col' if table else 'one-col'}'><div>{basis}</div>{table}</div>",
    )

    return (
        "<!doctype html><html><head><meta charset='utf-8' />"
        f"<title>{_esc(title or brand)}</title></head><body>"
        + cover
        + "".join(deck.slides)
        + "</body></html>"
    )


# One accent (Spark lime), ink and paper. Sizes are CSS px (1/96 in); the
# smallest text is 12px.
DECK_CSS = """
@page { size: 13.333in 7.5in; margin: 0; }
html, body { margin: 0; padding: 0; }
body {
    background: #ffffff; color: #16191c; font-size: 16px; line-height: 1.45;
    font-family: %(font)s;
}
section { width: 13.333in; height: 7.49in; position: relative; overflow: hidden;
    box-sizing: border-box; break-after: page; }
section:last-child { break-after: auto; }

/* Cover */
.cover { background: #16191c; color: #ffffff; }
.cover-panel { position: absolute; top: 0; bottom: 0; left: 0; width: 100%%;
    box-sizing: border-box; padding: 64px 72px; }
.cover.with-photo .cover-panel { width: 47%%; }
.cover .mark { height: 40px; width: auto; display: block; }
.cover-words { position: absolute; left: 72px; right: 56px; top: 230px; }
.cover-brand { margin: 0 0 14px 0; font-size: 18px; color: #c9ccd0; }
.cover h1 { margin: 0; font-size: 46px; line-height: 1.08; font-weight: 600;
    letter-spacing: -0.015em; }
.cover-period { margin: 22px 0 0 0; font-size: 20px; color: #c4d82e; }
.cover-foot { position: absolute; left: 72px; bottom: 56px; margin: 0;
    font-size: 13px; color: #a7abb0; }
.cover-photo { position: absolute; top: 0; right: 0; bottom: 0; width: 53%%; }
.cover-photo img { width: 100%%; height: 100%%; object-fit: cover; display: block; }

/* Inner slides */
.slide { background: #ffffff; padding: 48px 72px 0 72px; }
.top { display: flex; justify-content: space-between; align-items: center; height: 28px; }
.kicker { font-size: 14px; color: #5b6066; }
.top .mark { height: 26px; width: auto; }
h2 { margin: 22px 0 26px 0; font-size: 34px; line-height: 1.1; font-weight: 600;
    letter-spacing: -0.01em; }
h3 { margin: 0 0 14px 0; font-size: 15px; font-weight: 600; color: #16191c; }
.body { position: absolute; left: 72px; right: 72px; top: 150px; bottom: 74px; }
.foot { position: absolute; left: 72px; right: 72px; bottom: 30px; display: flex;
    justify-content: space-between; font-size: 12px; color: #80858b;
    border-top: 1px solid #e3e1db; padding-top: 12px; }

/* At a glance */
.kpi-grid { display: flex; flex-wrap: wrap; }
.kpi { width: 25%%; box-sizing: border-box; padding: 18px 24px 0 0; border-top: 1px solid #16191c; }
.kpi-grid.two-rows .kpi { height: 150px; }
.kpi-grid.one-row .kpi { height: 190px; }
.kpi strong { display: block; font-size: 50px; line-height: 1.05; font-weight: 600;
    letter-spacing: -0.02em; font-variant-numeric: tabular-nums; }
.kpi-grid.one-row .kpi strong { font-size: 62px; }
.kpi span { display: block; margin-top: 10px; font-size: 16px; color: #5b6066; }
.basis { margin: 22px 0 0 0; font-size: 13px; line-height: 1.5; color: #5b6066; max-width: 900px; }

/* Summary */
.two-col { display: flex; justify-content: space-between; }
.two-col > div { width: 48%%; }
.one-col > div { width: 82%%; }
.summary p { margin: 0 0 16px 0; font-size: 20px; line-height: 1.55; }
.summary.smaller p { font-size: 16px; }
.summary.smallest p { font-size: 13px; line-height: 1.5; }
.takeaways ol, .steps { list-style: none; margin: 0; padding: 0; }
.takeaways li, .steps li { display: flex; align-items: baseline; margin: 0 0 16px 0; padding-top: 12px;
    border-top: 1px solid #e3e1db; }
.takeaways .n, .steps .n { width: 42px; flex: none; font-size: 15px; font-weight: 600;
    color: #80858b; font-variant-numeric: tabular-nums; }
.takeaways .t { font-size: 17px; line-height: 1.45; }
.takeaways.smaller .t { font-size: 15px; }
.takeaways.smallest .t { font-size: 13px; }
.takeaways.smallest li { margin-bottom: 10px; padding-top: 8px; }
.steps { width: 82%%; }
.steps li { margin-bottom: 22px; padding-top: 16px; }
.steps .t { font-size: 22px; line-height: 1.4; }
.steps.smaller .t { font-size: 18px; }
.steps.smallest .t { font-size: 14px; }

/* Where it ran */
.lede { margin: -10px 0 20px 0; font-size: 18px; color: #3a3f44; }
.panels { display: flex; justify-content: space-between; }
.panels .panel { box-sizing: border-box; }
.panels.n1 .panel { width: 62%%; }
.panels.n2 .panel { width: 47%%; }
.bar-row { display: flex; align-items: center; height: 26px; }
.bar-label { width: 42%%; font-size: 14px; padding-right: 12px; white-space: nowrap;
    overflow: hidden; box-sizing: border-box; }
.bar-track { flex: 1; height: 10px; background: #efeee9; }
.bar-fill { height: 10px; background: #16191c; }
.bar-value { width: 64px; text-align: right; font-size: 14px; font-variant-numeric: tabular-nums; }
.trend { position: absolute; left: 0; right: 0; bottom: 0; }
.columns { width: 100%%; border-collapse: collapse; table-layout: fixed; }
.columns td { text-align: center; padding: 0 6px; }
.col-bars td { vertical-align: bottom; height: 112px; }
.col-value { display: block; font-size: 12px; color: #3a3f44; margin-bottom: 4px;
    font-variant-numeric: tabular-nums; }
.col-bar { background: #16191c; width: 70%%; margin: 0 auto; }
.col-labels td { font-size: 12px; color: #5b6066; padding-top: 6px;
    border-top: 1px solid #16191c; white-space: nowrap; }

/* Consumers */
.quote-grid { display: flex; flex-wrap: wrap; justify-content: space-between; }
.quote, .theme { width: 48.5%%; box-sizing: border-box; height: 150px; overflow: hidden;
    border-top: 1px solid #e3e1db; padding-top: 14px; }
blockquote { margin: 0; font-size: 17px; line-height: 1.45; color: #16191c; }
.theme blockquote { font-size: 15px; color: #3a3f44; }
.theme-label { margin: 0 0 8px 0; font-size: 16px; font-weight: 600; }
.theme-label span { font-weight: 400; color: #5b6066; font-size: 14px; }

/* Photos */
.photo-grid { display: flex; flex-wrap: wrap; }
figure { margin: 0 2%% 16px 0; width: 32%%; height: 238px; overflow: hidden; }
figure:nth-child(3n) { margin-right: 0; }
figure .frame { height: 190px; background: #efeee9; }
figure img { width: 100%%; height: 190px; object-fit: cover; display: block; }
figcaption { margin-top: 6px; }
.cap-main { display: block; font-size: 13px; line-height: 1.3; white-space: nowrap; overflow: hidden; }
.cap-meta { display: block; font-size: 12px; line-height: 1.3; color: #5b6066; white-space: nowrap; overflow: hidden; }

/* How we counted */
.method { margin: 0; padding: 0 0 0 18px; }
.method li { margin: 0 0 12px 0; font-size: 15px; line-height: 1.5; }
.left-out table { width: 100%%; border-collapse: collapse; }
.left-out th { text-align: left; font-size: 12px; font-weight: 600; color: #5b6066;
    padding: 0 8px 6px 0; border-bottom: 1px solid #16191c; }
.left-out td { font-size: 12px; padding: 5px 8px 5px 0; border-bottom: 1px solid #e3e1db;
    white-space: nowrap; }
""" % {"font": FONT_STACK}
