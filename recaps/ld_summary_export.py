"""Rebuild Liquid Death's "Summary" tab daily from Spark recap data.

LD's sheet ("[External] Liquid Death (Retail) Market Schedules") has a Summary
dashboard whose KPIs (Consumers Sampled / Single Cans / MultiPacks / Conversion
%) and breakdowns (by state, month, BA, and RMM) should reflect Spark. This
module recomputes those aggregates in Python from the tenant's CustomRecaps and
writes the Summary tab as values (USER_ENTERED), refreshed daily — no fragile
in-sheet formulas, and the math always matches the in-app dashboard because it
reuses the SAME matchers (recaps.report_service._accumulate_custom).

RMM activity: each recap is attributed to an RMM by its US state, reusing
events.routing.LIQUID_DEATH_TERRITORY (the same state→RMM map the public-form
router uses, which matches the sheet's existing RMM rows exactly).

Only the Summary tab (found by name) is cleared + rewritten — every other tab
(Master Tracker, market schedules, inventory, backup) is left untouched. The
runtime service account spark-api-new-sa@spark-479222.iam.gserviceaccount.com
must have Editor access on the sheet.
"""
from __future__ import annotations

import logging
import re
from collections import defaultdict
from dataclasses import dataclass, field

from googleapiclient.errors import HttpError

from events.routing import LIQUID_DEATH_TERRITORY
from recaps.models import CustomRecap
from recaps.pdf import _event_date, _event_state
from recaps.recap_sheet_export import SERVICE_ACCOUNT_EMAIL, _tab_titles
from recaps.report_service import (
    CampaignReportKpis,
    _accumulate_custom,
    _ba_display_name,
)
from utils.sheets_mirror import _col_letter, _service, extract_sheet_id

logger = logging.getLogger(__name__)

DEFAULT_SUMMARY_TAB = "Spark Summary"

# RMM email (from the territory map) → the first name shown in the sheet.
RMM_EMAIL_TO_NAME = {
    "l.giaccio@liquiddeath.com": "Lauren",
    "k.williams@liquiddeath.com": "Kristyn",
    "m.cristancho@liquiddeath.com": "Manuela",
    "ross@liquiddeath.com": "Ross",
    "pat@liquiddeath.com": "Pat",
    "t.reed@liquiddeath.com": "Timothy",
}
# Fixed display order for the RMM table (matches the sheet); Unassigned last.
RMM_ORDER = ["Lauren", "Kristyn", "Manuela", "Ross", "Pat", "Timothy"]
UNASSIGNED = "Unassigned"


def _build_state_to_rmm() -> dict[str, str]:
    """Invert LIQUID_DEATH_TERRITORY → {STATE: rmm_name}, first-wins so each
    demo is counted under exactly one RMM (so % of total sums to 100). DE is
    listed under both Manuela and Pat in the sheet; first-wins by RMM_ORDER
    assigns it to Manuela for counting (it has 0 demos today either way)."""
    state_to_rmm: dict[str, str] = {}
    # Iterate in RMM_ORDER so ties resolve deterministically.
    name_to_email = {v: k for k, v in RMM_EMAIL_TO_NAME.items()}
    for name in RMM_ORDER:
        email = name_to_email.get(name)
        for code in LIQUID_DEATH_TERRITORY.get(email, []):
            state_to_rmm.setdefault(code.upper(), name)
    return state_to_rmm


STATE_TO_RMM = _build_state_to_rmm()
# Display string of each RMM's mapped states (full territory, so DE shows under
# both, matching the sheet). Counting still uses STATE_TO_RMM (once).
RMM_MAPPED_STATES = {
    name: ", ".join(LIQUID_DEATH_TERRITORY.get(email, []))
    for email, name in RMM_EMAIL_TO_NAME.items()
}


@dataclass
class _Bucket:
    demos: int = 0
    consumers: int = 0
    cans: int = 0
    packs: int = 0
    willing: int = 0
    states: set = field(default_factory=set)


@dataclass
class LdSummary:
    total_demos: int = 0
    consumers: int = 0
    cans: int = 0
    packs: int = 0
    willing: int = 0
    by_rmm: dict = field(default_factory=lambda: defaultdict(_Bucket))
    by_state: dict = field(default_factory=lambda: defaultdict(int))
    by_month: dict = field(default_factory=lambda: defaultdict(_Bucket))
    by_ba: dict = field(default_factory=lambda: defaultdict(int))

    @property
    def conversion_pct(self) -> float:
        # Units sold (single cans + multipacks) per consumer sampled — matches
        # the LD sheet's "Conversion %" KPI, which sits alongside the cans +
        # packs columns. (Not willing/sampled, which can exceed 100%.)
        units = self.cans + self.packs
        return (units / self.consumers * 100) if self.consumers else 0.0


def _recap_state_code(recap) -> str | None:
    st = _event_state(recap)
    code = getattr(st, "code", None)
    if code:
        code = str(code).strip().upper()
        if len(code) >= 2:
            return code[:2]
    # Fall back to parsing the event name/address text.
    try:
        from events.routing import extract_state_code

        event = getattr(recap, "event", None)
        for src in (getattr(event, "name", None), getattr(event, "address", None)):
            if src:
                c = extract_state_code(str(src))
                if c:
                    return c
    except Exception:
        pass
    return None


def _recap_ba(recap) -> str:
    amb = getattr(recap, "ambassador", None)
    if amb is not None:
        name = _ba_display_name(amb)
        if name:
            return name
    return (getattr(recap, "external_ba_name", None) or "").strip() or "Unknown"


def _recap_month(recap) -> str | None:
    d = _event_date(recap)
    if not d:
        return None
    try:
        return d.strftime("%Y-%m")
    except Exception:
        return None


def compute_ld_summary(tenant) -> LdSummary:
    """Aggregate the tenant's CustomRecaps into the Summary model. Reuses
    report_service._accumulate_custom per recap so the KPI math matches the
    in-app dashboard and the campaign report."""
    summary = LdSummary()
    recaps = (
        CustomRecap.objects.filter(tenant=tenant)
        .select_related("event", "ambassador", "ambassador__user", "state")
        .prefetch_related(
            "custom_field_value__custom_field", "custom_recap_product_sample"
        )
    )
    for recap in recaps:
        k = CampaignReportKpis()
        _accumulate_custom(recap, k)
        consumers, cans, packs, willing = (
            k.consumers_reached,
            k.cans_sold,
            k.packs_sold,
            k.willing_to_purchase,
        )

        summary.total_demos += 1
        summary.consumers += consumers
        summary.cans += cans
        summary.packs += packs
        summary.willing += willing

        state = _recap_state_code(recap)
        rmm = STATE_TO_RMM.get(state, UNASSIGNED) if state else UNASSIGNED
        rb = summary.by_rmm[rmm]
        rb.demos += 1
        rb.consumers += consumers
        rb.cans += cans
        rb.packs += packs
        rb.willing += willing
        if state:
            rb.states.add(state)
            summary.by_state[state] += 1

        month = _recap_month(recap)
        if month:
            mb = summary.by_month[month]
            mb.demos += 1
            mb.consumers += consumers
            mb.cans += cans
            mb.packs += packs

        summary.by_ba[_recap_ba(recap)] += 1

    return summary


# ── OnBrand "RECAPS" tab — the sheet's comprehensive demo dataset spanning
#    2025–2026 (1.7k+ rows). Column indices confirmed via describe_sheet_tabs
#    --peek-tab RECAPS. We read it for the all-years headline + by-year split
#    (Spark only has 2026, so 2025 must come from here).
ONBRAND_RECAPS_TAB = "RECAPS"
_RC_DATE = 2
_RC_CONSUMERS = 10
_RC_WILLING = 13
_RC_CANS = 15
_RC_PACKS = 16


def _num(cell) -> int:
    """Parse a recap numeric cell → int; blanks / '-' / 'N/A' / junk → 0."""
    if cell is None:
        return 0
    s = str(cell).strip().replace(",", "")
    if not s or s.lower() in ("-", "—", "n/a", "na", "none"):
        return 0
    m = re.search(r"-?\d+", s)
    return int(m.group(0)) if m else 0


def _year_of(cell) -> str | None:
    m = re.search(r"(20\d\d)", str(cell or ""))
    return m.group(1) if m else None


def read_recaps_tab_by_year(svc, sheet_id: str, tab: str = ONBRAND_RECAPS_TAB) -> dict:
    """Aggregate the OnBrand RECAPS tab into per-year buckets (demos +
    consumers/cans/packs/willing). Returns {year: _Bucket}; empty dict if the
    tab is missing/unreadable (the summary then falls back to Spark-only)."""
    by_year: dict[str, _Bucket] = defaultdict(_Bucket)
    try:
        resp = (
            svc.spreadsheets()
            .values()
            .get(spreadsheetId=sheet_id, range=f"'{tab}'!A2:Q100000")
            .execute()
        )
    except HttpError as e:
        logger.warning("ld_summary_export: RECAPS tab read failed: %s", e)
        return {}
    for row in resp.get("values") or []:
        yr = _year_of(row[_RC_DATE] if len(row) > _RC_DATE else "")
        if not yr:
            continue
        b = by_year[yr]
        b.demos += 1
        b.consumers += _num(row[_RC_CONSUMERS]) if len(row) > _RC_CONSUMERS else 0
        b.willing += _num(row[_RC_WILLING]) if len(row) > _RC_WILLING else 0
        b.cans += _num(row[_RC_CANS]) if len(row) > _RC_CANS else 0
        b.packs += _num(row[_RC_PACKS]) if len(row) > _RC_PACKS else 0
    return dict(by_year)


def _conv(b: _Bucket) -> str:
    return f"{((b.cans + b.packs) / b.consumers * 100):.1f}%" if b.consumers else "0.0%"


def _pct(n: int, total: int) -> str:
    return f"{(n / total * 100):.1f}%" if total else "0.0%"


# Restrained LD palette (black/white, no rainbow — per Kyle's design pref).
_BLACK = {"red": 0.05, "green": 0.05, "blue": 0.05}
_DARK = {"red": 0.16, "green": 0.16, "blue": 0.16}
_LIGHT = {"red": 0.91, "green": 0.91, "blue": 0.91}
_WHITE = {"red": 1.0, "green": 1.0, "blue": 1.0}
_GRAYTEXT = {"red": 0.5, "green": 0.5, "blue": 0.5}
SUMMARY_WIDTH = 8  # widest section (the RMM table)


def build_summary_grid(summary: LdSummary, years: dict | None = None) -> tuple[list[list], dict]:
    """Return (rows, layout). `layout` records which rows are the title /
    subtitle / KPI strip / section headers / table headers, so the caller can
    apply branded cell formatting.

    When `years` (a {year: _Bucket} map from the OnBrand RECAPS tab) is given,
    the headline + a PERFORMANCE BY YEAR table reflect ALL demos across every
    year (Kyle's "capture all 2025 & 2026 events"), and the Spark-recap
    breakdowns below are clearly labeled as Spark-tracked. Without it the
    headline is the Spark-recap totals (back-compat)."""
    rows: list[list] = []
    layout = {
        "title": 0,
        "subtitle": 1,
        "kpi_header": None,
        "kpi_value": None,
        "sections": [],
        "table_headers": [],
        "ncols": SUMMARY_WIDTH,
    }

    def add(row=None) -> int:
        rows.append(list(row) if row else [])
        return len(rows) - 1

    # Headline: all-demo totals from the RECAPS tab when available, else Spark.
    if years:
        head = _Bucket()
        for b in years.values():
            head.demos += b.demos
            head.consumers += b.consumers
            head.cans += b.cans
            head.packs += b.packs
        head_demos, head_consumers, head_cans, head_packs = (
            head.demos, head.consumers, head.cans, head.packs
        )
        head_conv = _conv(head)
        yrs_present = sorted(years.keys())
        span = f"{yrs_present[0]}–{yrs_present[-1]}" if len(yrs_present) > 1 else (yrs_present[0] if yrs_present else "")
        subtitle = f"Auto-updated daily · all retail sampling demos {span}".strip()
    else:
        head_demos = summary.total_demos
        head_consumers, head_cans, head_packs = summary.consumers, summary.cans, summary.packs
        head_conv = f"{summary.conversion_pct:.1f}%"
        subtitle = "Auto-updated daily from Spark — Retail Samplings"

    add(["LIQUID DEATH · RETAIL SAMPLING SUMMARY"])
    add([subtitle])
    add()

    layout["kpi_header"] = add(
        ["DEMOS DONE", "CONSUMERS SAMPLED", "SINGLE CANS SOLD", "MULTIPACKS SOLD", "CONVERSION %"]
    )
    layout["kpi_value"] = add(
        [head_demos, head_consumers, head_cans, head_packs, head_conv]
    )
    add()

    # Performance by year — the two-years-separate view Kyle asked for.
    if years:
        layout["sections"].append(add(["PERFORMANCE BY YEAR"]))
        layout["table_headers"].append(
            add(["Year", "Demos", "Consumers Sampled", "Single Cans", "MultiPacks", "Conversion %"])
        )
        for yr in sorted(years.keys(), reverse=True):
            b = years[yr]
            add([yr, b.demos, b.consumers, b.cans, b.packs, _conv(b)])
        add()
        # Everything below is Spark-app data (2026 only today).
        layout["sections"].append(add(["SPARK-TRACKED DETAIL · 2026 (live app submissions)"]))
        add([f"{summary.total_demos} recaps submitted in Spark · {summary.consumers} consumers · "
             f"{summary.cans} single cans · {summary.packs} multipacks · {summary.conversion_pct:.1f}% conversion"])
        add()

    total = summary.total_demos

    # Performance by RMM (Kyle's explicit ask)
    layout["sections"].append(add(["PERFORMANCE BY RMM"]))
    layout["table_headers"].append(
        add(
            [
                "RMM",
                "Mapped States",
                "Retail Samplings",
                "% of Total",
                "Consumers Sampled",
                "Single Cans",
                "MultiPacks",
                "Conversion %",
            ]
        )
    )
    rmm_names = [n for n in RMM_ORDER if n in summary.by_rmm]
    if summary.by_rmm.get(UNASSIGNED):
        rmm_names.append(UNASSIGNED)
    for name in rmm_names:
        b = summary.by_rmm[name]
        conv = (
            f"{((b.cans + b.packs) / b.consumers * 100):.1f}%" if b.consumers else "0.0%"
        )
        add(
            [
                name,
                RMM_MAPPED_STATES.get(name, ""),
                b.demos,
                _pct(b.demos, total),
                b.consumers,
                b.cans,
                b.packs,
                conv,
            ]
        )
    add()

    layout["sections"].append(add(["PERFORMANCE BY STATE"]))
    layout["table_headers"].append(add(["State", "Demos Done", "% of Total"]))
    for code, demos in sorted(summary.by_state.items(), key=lambda kv: (-kv[1], kv[0])):
        add([code, demos, _pct(demos, total)])
    add()

    layout["sections"].append(add(["PERFORMANCE BY MONTH"]))
    layout["table_headers"].append(
        add(["Month", "Demos", "Consumers Sampled", "Single Cans", "MultiPacks"])
    )
    for month in sorted(summary.by_month.keys()):
        b = summary.by_month[month]
        add([month, b.demos, b.consumers, b.cans, b.packs])
    add()

    layout["sections"].append(add(["PERFORMANCE BY BRAND AMBASSADOR"]))
    layout["table_headers"].append(add(["Brand Ambassador", "Demos Done"]))
    for ba, demos in sorted(summary.by_ba.items(), key=lambda kv: (-kv[1], kv[0])):
        add([ba, demos])

    return rows, layout


def _cell(bg=None, fg=None, bold=False, italic=False, size=None, align="LEFT") -> dict:
    return {
        "userEnteredFormat": {
            "backgroundColor": bg or _WHITE,
            "horizontalAlignment": align,
            "verticalAlignment": "MIDDLE",
            "textFormat": {
                "foregroundColor": fg or _BLACK,
                "bold": bold,
                "italic": italic,
                "fontSize": size or 10,
            },
        }
    }


def _repeat(gid: int, r0: int, r1: int, c0: int, c1: int, cell: dict) -> dict:
    return {
        "repeatCell": {
            "range": {
                "sheetId": gid,
                "startRowIndex": r0,
                "endRowIndex": r1,
                "startColumnIndex": c0,
                "endColumnIndex": c1,
            },
            "cell": cell,
            "fields": "userEnteredFormat(backgroundColor,horizontalAlignment,verticalAlignment,textFormat)",
        }
    }


def _merge(gid: int, row: int, ncols: int) -> dict:
    return {
        "mergeCells": {
            "range": {
                "sheetId": gid,
                "startRowIndex": row,
                "endRowIndex": row + 1,
                "startColumnIndex": 0,
                "endColumnIndex": ncols,
            },
            "mergeType": "MERGE_ALL",
        }
    }


def summary_format_requests(gid: int, layout: dict) -> list[dict]:
    """Sheets batchUpdate requests to brand the Spark Summary tab."""
    n = layout["ncols"]
    reqs: list[dict] = [
        # Clear any prior merges (idempotent re-runs).
        {"unmergeCells": {"range": {"sheetId": gid}}},
        # Freeze the title + subtitle.
        {
            "updateSheetProperties": {
                "properties": {"sheetId": gid, "gridProperties": {"frozenRowCount": 2}},
                "fields": "gridProperties.frozenRowCount",
            }
        },
        # Column widths: wide first column (names / mapped states), rest medium.
        {
            "updateDimensionProperties": {
                "range": {"sheetId": gid, "dimension": "COLUMNS", "startIndex": 0, "endIndex": 1},
                "properties": {"pixelSize": 230},
                "fields": "pixelSize",
            }
        },
        {
            "updateDimensionProperties": {
                "range": {"sheetId": gid, "dimension": "COLUMNS", "startIndex": 1, "endIndex": n},
                "properties": {"pixelSize": 130},
                "fields": "pixelSize",
            }
        },
        # Title bar + subtitle.
        _merge(gid, layout["title"], n),
        _merge(gid, layout["subtitle"], n),
        _repeat(gid, layout["title"], layout["title"] + 1, 0, n,
                _cell(bg=_BLACK, fg=_WHITE, bold=True, size=16, align="CENTER")),
        _repeat(gid, layout["subtitle"], layout["subtitle"] + 1, 0, n,
                _cell(bg=_WHITE, fg=_GRAYTEXT, italic=True, size=10, align="CENTER")),
        # KPI strip.
        _repeat(gid, layout["kpi_header"], layout["kpi_header"] + 1, 0, 5,
                _cell(bg=_DARK, fg=_WHITE, bold=True, size=10, align="CENTER")),
        _repeat(gid, layout["kpi_value"], layout["kpi_value"] + 1, 0, 5,
                _cell(bg=_LIGHT, fg=_BLACK, bold=True, size=14, align="CENTER")),
    ]
    for sr in layout["sections"]:
        reqs.append(_merge(gid, sr, n))
        reqs.append(
            _repeat(gid, sr, sr + 1, 0, n, _cell(bg=_DARK, fg=_WHITE, bold=True, size=11, align="LEFT"))
        )
    for tr in layout["table_headers"]:
        reqs.append(
            _repeat(gid, tr, tr + 1, 0, n, _cell(bg=_LIGHT, fg=_BLACK, bold=True, size=10, align="LEFT"))
        )
    return reqs


def _resolve_tab(titles: list[str], wanted: str) -> str | None:
    for t in titles:
        if (t or "").strip().lower() == wanted.strip().lower():
            return t
    return None


def write_ld_summary(
    tenant,
    *,
    tab: str = DEFAULT_SUMMARY_TAB,
    target_tab: str | None = None,
    sheet_url: str | None = None,
    dry_run: bool = False,
) -> dict:
    """Recompute + rebuild the LD Summary tab. Returns a result dict; never
    raises. `target_tab` (e.g. "Summary (staging)") writes to a scratch tab,
    creating it if missing, for eyeball before swapping to the live `tab`.
    """
    summary = compute_ld_summary(tenant)

    # Resolve the sheet + client up front so we can read the OnBrand RECAPS tab
    # (the all-years demo source) before building the grid.
    url = (
        sheet_url
        or getattr(tenant, "recap_export_sheet_url", None)
        or getattr(tenant, "linked_sheet_url", None)
    )
    sheet_id = extract_sheet_id(url) if url else None
    svc = _service()

    years = read_recaps_tab_by_year(svc, sheet_id) if (svc and sheet_id) else {}
    grid, layout = build_summary_grid(summary, years=years or None)
    stats = {
        "demos": summary.total_demos,
        "consumers": summary.consumers,
        "cans": summary.cans,
        "packs": summary.packs,
        "conversion_pct": round(summary.conversion_pct, 2),
        "rmms": {n: summary.by_rmm[n].demos for n in summary.by_rmm},
        "by_year": {
            yr: {
                "demos": b.demos,
                "consumers": b.consumers,
                "cans": b.cans,
                "packs": b.packs,
                "conversion_pct": round((b.cans + b.packs) / b.consumers * 100, 2) if b.consumers else 0.0,
            }
            for yr, b in sorted(years.items())
        },
    }
    if dry_run:
        return {"ok": True, "dry_run": True, "rows": len(grid), **stats}

    if not url:
        return {"ok": False, "error": "no-sheet-url", "tenant": getattr(tenant, "slug", None)}
    if not sheet_id:
        return {"ok": False, "error": "bad-sheet-url", "url": url}
    if svc is None:
        return {"ok": False, "error": "no-credentials"}

    try:
        titles = _tab_titles(svc, sheet_id)
        wanted = (target_tab or tab).strip()
        actual = _resolve_tab(titles, wanted)
        if actual is None:
            # "Spark Summary" is a dedicated, Spark-owned tab — create it if it
            # doesn't exist yet. Creating a new tab never touches existing tabs.
            svc.spreadsheets().batchUpdate(
                spreadsheetId=sheet_id,
                body={"requests": [{"addSheet": {"properties": {"title": wanted}}}]},
            ).execute()
            actual = wanted

        # Clear the whole tab then write the rebuilt block. Only this tab.
        svc.spreadsheets().values().clear(
            spreadsheetId=sheet_id, range=f"'{actual}'!A:ZZ"
        ).execute()
        svc.spreadsheets().values().update(
            spreadsheetId=sheet_id,
            range=f"'{actual}'!A1",
            valueInputOption="USER_ENTERED",
            body={"values": grid},
        ).execute()

        # Apply branded formatting (non-fatal — values are already written).
        formatted = False
        try:
            meta = (
                svc.spreadsheets()
                .get(spreadsheetId=sheet_id, fields="sheets.properties(title,sheetId)")
                .execute()
            )
            gid = next(
                (
                    s["properties"]["sheetId"]
                    for s in meta.get("sheets", [])
                    if s.get("properties", {}).get("title") == actual
                ),
                None,
            )
            if gid is not None:
                svc.spreadsheets().batchUpdate(
                    spreadsheetId=sheet_id,
                    body={"requests": summary_format_requests(gid, layout)},
                ).execute()
                formatted = True
        except Exception as fe:  # pragma: no cover - formatting is best-effort
            logger.warning("ld_summary_export: formatting failed (values written): %s", fe)

        return {
            "ok": True,
            "tab": actual,
            "rows": len(grid),
            "formatted": formatted,
            "sheet_id": sheet_id,
            **stats,
        }
    except HttpError as e:
        status = getattr(getattr(e, "resp", None), "status", None)
        if str(status) == "403":
            detail = (
                f"Sheets API 403 — share the sheet with {SERVICE_ACCOUNT_EMAIL} (Editor)."
            )
        else:
            detail = " ".join(str(e).split())[:400]
        logger.warning("ld_summary_export: write failed (status=%s): %s", status, e)
        return {"ok": False, "error": "sheets-api", "status": status, "detail": detail}
    except Exception as e:  # pragma: no cover - defensive
        logger.exception("ld_summary_export: unexpected failure")
        return {"ok": False, "error": "unexpected", "detail": " ".join(str(e).split())[:400]}
