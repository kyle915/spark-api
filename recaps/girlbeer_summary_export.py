"""Girl Beer "Summary" dashboard — Spark-computed values (no in-sheet formulas).

The Girl Beer sheet has a "Summary" tab whose KPI cards + per-ambassador / date /
retailer / flavor / age breakdowns were hand-built as spreadsheet formulas that
read the raw "Demo Recaps" tab. Those formulas broke (#REF!) as the data grew —
the spilling per-ambassador / per-date QUERY tables collided with the fixed
sections below them — and parts went stale.

This module rebuilds the whole Summary tab from the source of truth (the
tenant's CustomRecaps) as plain VALUES, laid out in two non-overlapping columns
so nothing can ever collide or #REF! again. It is recomputed on every recap
export (daily cron, and on-submit if the tenant opts in), so it stays current.

Numbers are verified against the previously-correct KPI/flavor/age values:
samples, customers engaged, variety packs, six-packs, buyers, account spend and
the flavor + age matrices all reconcile to the in-sheet totals. The reorder
count matches free-text answers that start with "yes". The store/location
breakdown reconciles to the full demo count (the old retailer formula silently
dropped ~1/3 of demos whose store wasn't in a hard-coded list).

Reuses the ADC Sheets client (utils.sheets_mirror) and the recap field
extractors (recaps.recap_sheet_export). The Cloud Run runtime service account
must have Editor access on the sheet. Failures are returned, never raised.
"""
from __future__ import annotations

import logging
import re
from collections import OrderedDict
from dataclasses import dataclass, field

from googleapiclient.errors import HttpError

from recaps.recap_sheet_export import (
    SERVICE_ACCOUNT_EMAIL,
    _normalize,
    _recap_meta,
    _tab_titles,
    _tenant_recaps,
    _values_by_field_name,
)
from utils.sheets_mirror import _service, extract_sheet_id

logger = logging.getLogger(__name__)

DEFAULT_SUMMARY_TAB = "Summary"

# Girl Beer's six SKUs, in the brand's canonical order.
FLAVORS = [
    ("Blueberry Lavender", "Blueberry Lavender 6-packs Sold"),
    ("Pineapple Yuzu", "Pineapple Yuzu 6-packs Sold"),
    ("Grapefruit Guava", "Grapefruit Guava 6-packs Sold"),
    ("Peach", "Peach 6-packs Sold"),
    ("Tangerine", "Tangerine 6-packs Sold"),
]

# Recognised retailer chains — collapse "H-E-B FM 685 / Pflugerville" → "H-E-B".
# Stores with no recognised chain group by their (cleaned) location label, so
# every demo is represented (the old formula dropped unmatched stores).
_CHAINS = [
    ("h-e-b", "H-E-B"),
    ("heb", "H-E-B"),
    ("albertsons", "Albertsons"),
    ("vons", "Vons"),
    ("pavilions", "Pavilions"),
    ("whole foods", "Whole Foods"),
    ("fry's", "Fry's"),
    ("frys", "Fry's"),
    ("sprouts", "Sprouts"),
    ("ralphs", "Ralphs"),
    ("gelson", "Gelson's"),
    ("safeway", "Safeway"),
    ("bristol farms", "Bristol Farms"),
    ("stater bros", "Stater Bros"),
    ("smart & final", "Smart & Final"),
    ("tom thumb", "Tom Thumb"),
    ("randalls", "Randalls"),
    ("kroger", "Kroger"),
    ("trader joe", "Trader Joe's"),
]

_AGE_BANDS = ["21-29", "30-39", "40+"]


def _num(v) -> float:
    if v is None:
        return 0.0
    s = str(v).replace("$", "").replace(",", "").replace("%", "").strip()
    if not s:
        return 0.0
    try:
        return float(s)
    except ValueError:
        return 0.0


def _i(v) -> int:
    return int(round(_num(v)))


def _is_yes(v) -> bool:
    """The reorder field is free text — count answers that START with 'yes'
    (Yes, yes, Yes!, 'Yes and adjusted in shelf'); excludes 'I think yes'."""
    return str(v or "").strip().lower().startswith("yes")


def _clean_store(s: str) -> str:
    """Strip Spark's disambiguating ' · 2026-05-22' suffix and trailing date
    tokens so the same store groups together."""
    s = (s or "").strip()
    s = re.sub(r"\s*·\s*\d{4}-\d{2}-\d{2}\s*$", "", s)
    s = re.sub(r"\s+\d{1,2}/\d{1,2}/\d{2,4}\s*$", "", s)
    return s.strip(" ·-")


def _retailer_label(store: str) -> str:
    cleaned = _clean_store(store)
    low = cleaned.lower()
    for token, label in _CHAINS:
        if token in low:
            return label
    return cleaned or "Unspecified"


def _name_key(name: str) -> str:
    return re.sub(r"\s+", " ", (name or "").strip()).lower()


def _name_display(name: str) -> str:
    name = re.sub(r"\s+", " ", (name or "").strip())
    # Keep all-caps entries readable ("CORTNEY AYERS" → "Cortney Ayers") but
    # leave mixed-case names as the BA typed them.
    return name.title() if name.isupper() else name


def _parse_mdy(s: str):
    """MM/DD/YYYY → (year, month, day) tuple for sorting; None if unparseable."""
    m = re.match(r"\s*(\d{1,2})/(\d{1,2})/(\d{2,4})", str(s or ""))
    if not m:
        return None
    mo, da, yr = int(m.group(1)), int(m.group(2)), int(m.group(3))
    if yr < 100:
        yr += 2000
    return (yr, mo, da)


def _g(vals: dict, *cands) -> str:
    """Look up a recap field value by any of the candidate names (normalised),
    then by normalised startswith (handles long parenthetical labels)."""
    for c in cands:
        nc = _normalize(c)
        if nc in vals:
            return vals[nc]
    for c in cands:
        nc = _normalize(c)
        for k, v in vals.items():
            if k.startswith(nc):
                return v
    return ""


@dataclass
class GBSummary:
    demos: int = 0
    samples: int = 0
    engaged: int = 0
    variety_packs: int = 0
    six_packs: int = 0
    total_buyers: int = 0
    reorders: int = 0
    account_spend: float = 0.0
    flavor: "OrderedDict[str, int]" = field(default_factory=OrderedDict)
    # age matrix: row label -> [21-29, 30-39, 40+, total]
    age: "OrderedDict[str, list]" = field(default_factory=OrderedDict)
    by_ambassador: list = field(default_factory=list)  # dicts
    by_location: list = field(default_factory=list)
    by_date: list = field(default_factory=list)


def compute_girlbeer_summary(tenant) -> GBSummary:
    s = GBSummary()
    s.flavor = OrderedDict((label, 0) for label, _ in FLAVORS)
    age_rows = OrderedDict(
        (k, [0, 0, 0, 0])
        for k in [
            "Men who bought",
            "Women who bought",
            "Men who sampled",
            "Women who sampled",
        ]
    )
    amb: "OrderedDict[str, dict]" = OrderedDict()
    loc: "OrderedDict[str, dict]" = OrderedDict()
    dat: "OrderedDict[str, dict]" = OrderedDict()

    def bucket(store, key, display):
        d = store.get(key)
        if d is None:
            d = {
                "label": display,
                "demos": 0,
                "samples": 0,
                "engaged": 0,
                "buyers": 0,
                "six_packs": 0,
                "spend": 0.0,
            }
            store[key] = d
        return d

    for recap in _tenant_recaps(tenant):
        meta = _recap_meta(recap)
        vals = _values_by_field_name(recap)

        samples = _i(_g(vals, "Total Samples Given Out"))
        engaged = _i(_g(vals, "Number of Customers Engaged (talked to or sampled product)",
                        "Number of Customers Engaged"))
        variety = _i(_g(vals, "# of PURPLE Variety Packs sold")) + _i(
            _g(vals, "# of RED Variety Packs sold")
        )
        six = sum(_i(_g(vals, col)) for _, col in FLAVORS)
        men_bought = _i(_g(vals, "Men who bought (Total)"))
        women_bought = _i(_g(vals, "Women who bought (Total)"))
        buyers = men_bought + women_bought
        spend = _num(_g(vals, "Account Spend Amount"))
        reorder = _is_yes(_g(vals, "Did the demo influence the store to place a reorder?",
                             "Did the demo influence the store to place a reorder"))

        s.demos += 1
        s.samples += samples
        s.engaged += engaged
        s.variety_packs += variety
        s.six_packs += six
        s.total_buyers += buyers
        s.account_spend += spend
        if reorder:
            s.reorders += 1

        for label, col in FLAVORS:
            s.flavor[label] += _i(_g(vals, col))

        # Age matrix
        for band_i, band in enumerate(_AGE_BANDS):
            age_rows["Men who bought"][band_i] += _i(_g(vals, f"Men who bought ({band})"))
            age_rows["Women who bought"][band_i] += _i(_g(vals, f"Women who bought ({band})"))
            age_rows["Men who sampled"][band_i] += _i(_g(vals, f"Men who sampled ({band})"))
            age_rows["Women who sampled"][band_i] += _i(_g(vals, f"Women who sampled ({band})"))

        # By ambassador
        ba_name = meta.get("ba") or "Unknown"
        d = bucket(amb, _name_key(ba_name), _name_display(ba_name))
        d["demos"] += 1
        d["samples"] += samples
        d["engaged"] += engaged
        d["buyers"] += buyers
        d["six_packs"] += six
        d["spend"] += spend

        # By store / location
        label = _retailer_label(meta.get("store") or "")
        d = bucket(loc, label.lower(), label)
        d["demos"] += 1
        d["samples"] += samples
        d["engaged"] += engaged
        d["buyers"] += buyers
        d["spend"] += spend

        # By date
        date_str = meta.get("date") or "—"
        d = bucket(dat, date_str, date_str)
        d["demos"] += 1
        d["samples"] += samples
        d["engaged"] += engaged
        d["buyers"] += buyers
        d["spend"] += spend
        d["_sort"] = _parse_mdy(date_str) or (0, 0, 0)

    # Age totals column
    for k, row in age_rows.items():
        row[3] = row[0] + row[1] + row[2]
    all_sampled = [
        age_rows["Men who sampled"][i] + age_rows["Women who sampled"][i] for i in range(4)
    ]
    age_rows["All who sampled"] = all_sampled
    s.age = age_rows

    s.by_ambassador = sorted(amb.values(), key=lambda d: (-d["demos"], -d["samples"], d["label"]))
    s.by_location = sorted(loc.values(), key=lambda d: (-d["demos"], -d["samples"], d["label"]))
    s.by_date = sorted(dat.values(), key=lambda d: d.get("_sort", (0, 0, 0)), reverse=True)
    return s


# ─────────────────────────── grid + formatting ───────────────────────────

from recaps.ld_summary_export import _resolve_tab  # noqa: E402

TITLE_TEXT = "GIRL BEER  ·  RETAIL DEMO RECAP SUMMARY"
KPI_LABELS = [
    "DEMOS RUN", "SAMPLES GIVEN", "CUSTOMERS ENGAGED", "VARIETY PACKS SOLD",
    "6-PACKS SOLD", "TOTAL BUYERS", "REORDERS INFLUENCED", "ACCOUNT SPEND",
]
_LEFT0 = 0          # left-column start col (A)
_RIGHT0 = 8         # right-column start col (I) — disjoint from the left block
_BODY = 6          # first body row (0-based → sheet row 7)
_MAXC = 13         # last col used (N)

_DARK = {"red": 0.094, "green": 0.129, "blue": 0.192}
_WHITE = {"red": 1.0, "green": 1.0, "blue": 1.0}
_SECTIONBG = {"red": 0.929, "green": 0.937, "blue": 0.957}
_HEADBG = {"red": 0.965, "green": 0.969, "blue": 0.976}
_MUTED = {"red": 0.40, "green": 0.44, "blue": 0.52}
_BORDER = {"red": 0.82, "green": 0.84, "blue": 0.88}


def _emit_table(cells, fmt, start_row, col0, title, headers, data_rows,
                currency_cols=(), pct_cols=(), dec_cols=()):
    ncol = len(headers)
    cells[(start_row, col0)] = title
    fmt.append(("section", start_row, start_row + 1, col0, col0 + ncol))
    hr = start_row + 1
    for i, h in enumerate(headers):
        cells[(hr, col0 + i)] = h
    fmt.append(("header", hr, hr + 1, col0, col0 + ncol))
    dr0 = hr + 1
    if not data_rows:
        cells[(dr0, col0)] = "—"
        return dr0
    for ri, row in enumerate(data_rows):
        for ci, v in enumerate(row):
            cells[(dr0 + ri, col0 + ci)] = v
    dr_end = dr0 + len(data_rows)
    for c in currency_cols:
        fmt.append(("currency", dr0, dr_end, col0 + c, col0 + c + 1))
    for c in pct_cols:
        fmt.append(("percent", dr0, dr_end, col0 + c, col0 + c + 1))
    for c in dec_cols:
        fmt.append(("decimal", dr0, dr_end, col0 + c, col0 + c + 1))
    return dr_end - 1


def build_grid(summary: GBSummary, refreshed: str = ""):
    cells: dict = {}
    fmt: list = []

    cells[(0, 0)] = TITLE_TEXT
    sub = "Live summary — auto-updated from Spark recaps"
    if refreshed:
        sub += f"   ·   Last updated {refreshed}"
    cells[(1, 0)] = sub
    fmt.append(("title", 0, 1, 0, _MAXC + 1))
    fmt.append(("subtitle", 1, 2, 0, _MAXC + 1))

    # KPI band
    kpi_vals = [
        summary.demos, summary.samples, summary.engaged, summary.variety_packs,
        summary.six_packs, summary.total_buyers, summary.reorders,
        round(summary.account_spend, 2),
    ]
    for c, lbl in enumerate(KPI_LABELS):
        cells[(3, c)] = lbl
    for c, v in enumerate(kpi_vals):
        cells[(4, c)] = v
    fmt.append(("kpi_label", 3, 4, 0, 8))
    fmt.append(("kpi_value", 4, 5, 0, 8))
    fmt.append(("int", 4, 5, 0, 7))
    fmt.append(("currency0", 4, 5, 7, 8))

    # LEFT column
    r = _BODY
    r = _emit_table(
        cells, fmt, r, _LEFT0, "PERFORMANCE BY AMBASSADOR",
        ["Ambassador", "Demos", "Samples", "Engaged", "Buyers", "6-Packs", "Spend"],
        [[d["label"], d["demos"], d["samples"], d["engaged"], d["buyers"],
          d["six_packs"], round(d["spend"], 2)] for d in summary.by_ambassador],
        currency_cols=[6],
    )
    r += 2
    loc_rows = []
    for d in summary.by_location:
        avg = (d["samples"] / d["demos"]) if d["demos"] else 0
        rate = (d["buyers"] / d["engaged"]) if d["engaged"] else 0
        loc_rows.append([d["label"], d["demos"], d["samples"], round(avg, 1),
                         d["engaged"], d["buyers"], rate, round(d["spend"], 2)])
    _emit_table(
        cells, fmt, r, _LEFT0, "PERFORMANCE BY STORE / LOCATION",
        ["Store / Location", "Demos", "Samples", "Avg / Demo", "Engaged",
         "Buyers", "Purchase Rate", "Spend"],
        loc_rows, currency_cols=[7], pct_cols=[6], dec_cols=[3],
    )

    # RIGHT column
    r = _BODY
    _emit_table(
        cells, fmt, r, _RIGHT0, "6-PACK SALES BY FLAVOR",
        ["Flavor", "6-Packs Sold"],
        [[k, v] for k, v in summary.flavor.items()],
    )
    r += len(summary.flavor) + 4

    # Age matrix (special: sub-header band + 5 rows)
    cells[(r, _RIGHT0)] = "BUYERS & SAMPLERS BY AGE"
    fmt.append(("section", r, r + 1, _RIGHT0, _RIGHT0 + 5))
    hr = r + 1
    for i, h in enumerate(["", "21-29", "30-39", "40+", "Total"]):
        cells[(hr, _RIGHT0 + i)] = h
    fmt.append(("header", hr, hr + 1, _RIGHT0, _RIGHT0 + 5))
    ar = hr + 1
    for label, vals in summary.age.items():
        cells[(ar, _RIGHT0)] = label
        for i, v in enumerate(vals):
            cells[(ar, _RIGHT0 + 1 + i)] = v
        ar += 1
    r = ar + 2

    # By date (right column, most recent first)
    _emit_table(
        cells, fmt, r, _RIGHT0, "PERFORMANCE BY DATE",
        ["Date", "Demos", "Samples", "Engaged", "Buyers", "Spend"],
        [[d["label"], d["demos"], d["samples"], d["engaged"], d["buyers"],
          round(d["spend"], 2)] for d in summary.by_date],
        currency_cols=[5],
    )

    # Materialize the grid
    max_r = max(rc[0] for rc in cells) if cells else 0
    grid = [["" for _ in range(_MAXC + 1)] for _ in range(max_r + 1)]
    for (rr, cc), v in cells.items():
        if cc <= _MAXC:
            grid[rr][cc] = v
    return grid, fmt


def _fmt_requests(gid: int, fmt: list) -> list[dict]:
    """Translate the layout fmt-ops into Sheets batchUpdate requests."""
    req: list[dict] = []

    def rng(o):
        return {"sheetId": gid, "startRowIndex": o[1], "endRowIndex": o[2],
                "startColumnIndex": o[3], "endColumnIndex": o[4]}

    def cellfmt(o, cell, fields):
        req.append({"repeatCell": {"range": rng(o), "cell": cell, "fields": fields}})

    def numfmt(o, pattern):
        cellfmt(o, {"userEnteredFormat": {"numberFormat": {"type": "NUMBER", "pattern": pattern}}},
                "userEnteredFormat.numberFormat")

    for o in fmt:
        kind = o[0]
        if kind == "title":
            req.append({"mergeCells": {"range": rng(o), "mergeType": "MERGE_ALL"}})
            cellfmt(o, {"userEnteredFormat": {
                "backgroundColor": _DARK,
                "horizontalAlignment": "CENTER", "verticalAlignment": "MIDDLE",
                "textFormat": {"foregroundColor": _WHITE, "bold": True, "fontSize": 14}}},
                "userEnteredFormat(backgroundColor,horizontalAlignment,verticalAlignment,textFormat)")
        elif kind == "subtitle":
            req.append({"mergeCells": {"range": rng(o), "mergeType": "MERGE_ALL"}})
            cellfmt(o, {"userEnteredFormat": {
                "horizontalAlignment": "CENTER",
                "textFormat": {"foregroundColor": _MUTED, "italic": True, "fontSize": 9}}},
                "userEnteredFormat(horizontalAlignment,textFormat)")
        elif kind == "kpi_label":
            cellfmt(o, {"userEnteredFormat": {
                "backgroundColor": _SECTIONBG, "horizontalAlignment": "CENTER",
                "textFormat": {"foregroundColor": _MUTED, "bold": True, "fontSize": 8}}},
                "userEnteredFormat(backgroundColor,horizontalAlignment,textFormat)")
        elif kind == "kpi_value":
            cellfmt(o, {"userEnteredFormat": {
                "horizontalAlignment": "CENTER",
                "textFormat": {"bold": True, "fontSize": 16}}},
                "userEnteredFormat(horizontalAlignment,textFormat)")
        elif kind == "section":
            cellfmt(o, {"userEnteredFormat": {
                "backgroundColor": _DARK,
                "textFormat": {"foregroundColor": _WHITE, "bold": True, "fontSize": 10}}},
                "userEnteredFormat(backgroundColor,textFormat)")
        elif kind == "header":
            cellfmt(o, {"userEnteredFormat": {
                "backgroundColor": _HEADBG,
                "textFormat": {"bold": True, "fontSize": 9},
                "borders": {"bottom": {"style": "SOLID", "color": _BORDER}}}},
                "userEnteredFormat(backgroundColor,textFormat)")
            req.append({"updateBorders": {"range": rng(o),
                       "bottom": {"style": "SOLID", "color": _BORDER}}})
        elif kind == "currency":
            numfmt(o, "$#,##0.00")
        elif kind == "currency0":
            numfmt(o, "$#,##0")
        elif kind == "percent":
            numfmt(o, "0.0%")
        elif kind == "decimal":
            numfmt(o, "0.0")
        elif kind == "int":
            numfmt(o, "#,##0")

    # Column widths + clean font on the whole sheet.
    req.append({"repeatCell": {
        "range": {"sheetId": gid},
        "cell": {"userEnteredFormat": {"textFormat": {"fontFamily": "Arial"}}},
        "fields": "userEnteredFormat.textFormat.fontFamily"}})
    widths = [(0, 1, 190), (1, 8, 78), (8, 9, 150), (9, 14, 78)]
    for c0, c1, px in widths:
        req.append({"updateDimensionProperties": {
            "range": {"sheetId": gid, "dimension": "COLUMNS",
                      "startIndex": c0, "endIndex": c1},
            "properties": {"pixelSize": px}, "fields": "pixelSize"}})
    return req


def write_girlbeer_summary(tenant, *, tab: str = DEFAULT_SUMMARY_TAB,
                           sheet_url: str | None = None, dry_run: bool = False) -> dict:
    """Recompute + rebuild the Girl Beer Summary tab as values. Never raises."""
    from django.utils import timezone

    summary = compute_girlbeer_summary(tenant)
    try:
        refreshed = timezone.localtime(timezone.now()).strftime("%b %-d, %Y")
    except Exception:
        refreshed = ""
    grid, fmt = build_grid(summary, refreshed=refreshed)

    stats = {
        "demos": summary.demos, "samples": summary.samples,
        "engaged": summary.engaged, "variety_packs": summary.variety_packs,
        "six_packs": summary.six_packs, "buyers": summary.total_buyers,
        "reorders": summary.reorders, "spend": round(summary.account_spend, 2),
        "ambassadors": len(summary.by_ambassador),
        "locations": len(summary.by_location), "dates": len(summary.by_date),
    }
    if dry_run:
        return {"ok": True, "dry_run": True, "rows": len(grid), **stats}

    url = sheet_url or getattr(tenant, "recap_export_sheet_url", None)
    if not url:
        return {"ok": False, "error": "no-sheet-url", "tenant": getattr(tenant, "slug", None)}
    sheet_id = extract_sheet_id(url)
    if not sheet_id:
        return {"ok": False, "error": "bad-sheet-url", "url": url}
    svc = _service()
    if svc is None:
        return {"ok": False, "error": "no-credentials"}

    try:
        titles = _tab_titles(svc, sheet_id)
        actual = _resolve_tab(titles, tab.strip())
        if actual is None:
            svc.spreadsheets().batchUpdate(
                spreadsheetId=sheet_id,
                body={"requests": [{"addSheet": {"properties": {"title": tab.strip()}}}]},
            ).execute()
            actual = tab.strip()

        meta = (
            svc.spreadsheets()
            .get(spreadsheetId=sheet_id, fields="sheets.properties(title,sheetId)")
            .execute()
        )
        gid = next(
            (s["properties"]["sheetId"] for s in meta.get("sheets", [])
             if s.get("properties", {}).get("title") == actual),
            None,
        )

        # Clear values, wipe stale formatting, write fresh values, re-format.
        svc.spreadsheets().values().clear(
            spreadsheetId=sheet_id, range=f"'{actual}'!A:ZZ"
        ).execute()
        if gid is not None:
            svc.spreadsheets().batchUpdate(
                spreadsheetId=sheet_id,
                body={"requests": [
                    {"repeatCell": {"range": {"sheetId": gid}, "cell": {},
                                    "fields": "userEnteredFormat"}}
                ]},
            ).execute()
        svc.spreadsheets().values().update(
            spreadsheetId=sheet_id, range=f"'{actual}'!A1",
            valueInputOption="USER_ENTERED", body={"values": grid},
        ).execute()

        formatted = False
        try:
            if gid is not None:
                svc.spreadsheets().batchUpdate(
                    spreadsheetId=sheet_id, body={"requests": _fmt_requests(gid, fmt)}
                ).execute()
                formatted = True
        except Exception as fe:  # pragma: no cover - best effort
            logger.warning("girlbeer_summary_export: formatting failed (values written): %s", fe)

        return {"ok": True, "tab": actual, "rows": len(grid), "formatted": formatted,
                "sheet_id": sheet_id, **stats}
    except HttpError as e:
        status = getattr(getattr(e, "resp", None), "status", None)
        if str(status) == "403":
            detail = (f"Sheets API 403 — share the sheet with {SERVICE_ACCOUNT_EMAIL} "
                      f"(Editor).")
        else:
            detail = " ".join(str(e).split())[:400]
        logger.warning("girlbeer_summary_export: write failed (status=%s): %s", status, e)
        return {"ok": False, "error": "sheets-api", "status": status, "detail": detail}
    except Exception as e:  # pragma: no cover - defensive
        logger.exception("girlbeer_summary_export: unexpected failure")
        return {"ok": False, "error": "unexpected", "detail": " ".join(str(e).split())[:400]}
