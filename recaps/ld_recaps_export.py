"""Liquid Death raw-recap export → a branded "Spark Recaps" tab.

Kyle wants every Spark recap's RAW data mirrored into the LD sheet, in the same
restrained look/feel as the Spark Summary, refreshed automatically when recaps
are submitted/edited (not just nightly).

Unlike the Girl Beer export (recap_sheet_export.py), which maps into an existing
client tab by header name, LD has no such tab — so this BUILDS a dedicated,
branded tab: a black title bar, a bold header row (event/BA metadata + one
column per recap field), one row per recap, frozen header. It only ever
creates/clears the one "Spark Recaps" tab; every other tab is left untouched.

Reuses the field extractors from recap_sheet_export and the branding helpers +
palette from ld_summary_export. The runtime service account
(spark-api-new-sa@spark-479222.iam.gserviceaccount.com) needs Editor access.
Failures are returned in the result dict, never raised.
"""
from __future__ import annotations

import logging

from googleapiclient.errors import HttpError

from recaps.ld_summary_export import (
    SERVICE_ACCOUNT_EMAIL,
    _cell,
    _merge,
    _repeat,
    _resolve_tab,
)
from recaps.pdf import _event_date, _event_retailer, _event_state
from recaps.recap_sheet_export import (
    _ba_name,
    _event_name,
    _fmt_mdy,
    _is_image_field,
    _name_of,
    _normalize,
    _ordered_fields,
    _store_location,
    _tab_titles,
    _values_by_field_name,
)
from recaps.models import CustomRecap
from utils.sheets_mirror import _col_letter, _service, extract_sheet_id

logger = logging.getLogger(__name__)

DEFAULT_RECAPS_TAB = "Spark Recaps"

# Recap-level metadata columns, shown before the per-field columns.
META_COLUMNS = ["Date", "Brand Ambassador", "Retailer", "Store / Location", "State", "Status", "Event"]


def _deduped_field_names(tenant) -> list[str]:
    """Distinct (non-image) recap field display names in template order.

    LD has two template versions with same-meaning fields under different names
    ("Total number of consumers sampled" vs "How many TOTAL consumers did you
    sample?"). We keep one column per DISTINCT name, first occurrence wins, so a
    recap fills whichever column its template uses."""
    seen: set[str] = set()
    names: list[str] = []
    for f in _ordered_fields(tenant):  # already excludes image fields
        name = (getattr(f, "name", "") or "").strip()
        key = _normalize(name)
        if not name or key in seen:
            continue
        seen.add(key)
        names.append(name)
    return names


def _recaps_for(tenant, year: int | None):
    qs = (
        CustomRecap.objects.filter(tenant=tenant)
        .select_related("event", "ambassador", "ambassador__user", "state")
        .prefetch_related("custom_field_value__custom_field")
        .order_by("-submitted_at", "-id")
    )
    recaps = list(qs)
    if year is not None:
        recaps = [r for r in recaps if (_event_date(r) and _event_date(r).year == year)]
    return recaps


def build_ld_recaps_grid(tenant, *, year: int | None = None) -> tuple[list[list], dict, int]:
    """Return (rows, layout, ncols) for the branded Spark Recaps tab."""
    field_names = _deduped_field_names(tenant)
    norm_fields = [_normalize(n) for n in field_names]
    header = list(META_COLUMNS) + field_names
    ncols = max(len(header), 1)

    rows: list[list] = []
    layout = {"title": 0, "subtitle": 1, "header": None, "ncols": ncols}

    def add(row=None) -> int:
        rows.append(list(row) if row else [])
        return len(rows) - 1

    yr = f" · {year}" if year else ""
    add(["LIQUID DEATH · SPARK RECAPS (RAW)" + yr])
    add(["Auto-updated from Spark — one row per recap, every field"])
    layout["header"] = add(header)

    for recap in _recaps_for(tenant, year):
        by_name = _values_by_field_name(recap)
        meta = [
            _fmt_mdy(_event_date(recap)),
            _ba_name(recap),
            _name_of(_event_retailer(recap)),
            _store_location(recap),
            _name_of(_event_state(recap)),
            "Approved" if getattr(recap, "approved", False) else "Submitted",
            _event_name(recap),
        ]
        rows.append(meta + [by_name.get(nf, "") for nf in norm_fields])

    return rows, layout, ncols


def ld_recaps_format_requests(gid: int, layout: dict) -> list[dict]:
    """Branded formatting for the Spark Recaps tab (matches the Summary)."""
    from recaps.ld_summary_export import _BLACK, _DARK, _GRAYTEXT, _WHITE

    n = layout["ncols"]
    return [
        {"unmergeCells": {"range": {"sheetId": gid}}},
        {
            "updateSheetProperties": {
                "properties": {"sheetId": gid, "gridProperties": {"frozenRowCount": 3}},
                "fields": "gridProperties.frozenRowCount",
            }
        },
        # Date column narrow, everything else readable-wide.
        {
            "updateDimensionProperties": {
                "range": {"sheetId": gid, "dimension": "COLUMNS", "startIndex": 0, "endIndex": 1},
                "properties": {"pixelSize": 110},
                "fields": "pixelSize",
            }
        },
        {
            "updateDimensionProperties": {
                "range": {"sheetId": gid, "dimension": "COLUMNS", "startIndex": 1, "endIndex": n},
                "properties": {"pixelSize": 190},
                "fields": "pixelSize",
            }
        },
        _merge(gid, layout["title"], n),
        _merge(gid, layout["subtitle"], n),
        _repeat(gid, layout["title"], layout["title"] + 1, 0, n,
                _cell(bg=_BLACK, fg=_WHITE, bold=True, size=16, align="CENTER")),
        _repeat(gid, layout["subtitle"], layout["subtitle"] + 1, 0, n,
                _cell(bg=_WHITE, fg=_GRAYTEXT, italic=True, size=10, align="CENTER")),
        _repeat(gid, layout["header"], layout["header"] + 1, 0, n,
                _cell(bg=_DARK, fg=_WHITE, bold=True, size=10, align="LEFT")),
    ]


def write_ld_recaps(
    tenant,
    *,
    tab: str = DEFAULT_RECAPS_TAB,
    sheet_url: str | None = None,
    year: int | None = None,
    dry_run: bool = False,
) -> dict:
    """Build + write the branded Spark Recaps tab. Returns a result dict; never
    raises. Only the one tab is created/cleared/written."""
    grid, layout, ncols = build_ld_recaps_grid(tenant, year=year)
    data_rows = max(len(grid) - 3, 0)  # minus title/subtitle/header
    if dry_run:
        return {"ok": True, "dry_run": True, "rows": data_rows, "columns": ncols}

    url = (
        sheet_url
        or getattr(tenant, "recap_export_sheet_url", None)
        or getattr(tenant, "linked_sheet_url", None)
    )
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

        svc.spreadsheets().values().clear(
            spreadsheetId=sheet_id, range=f"'{actual}'!A:ZZ"
        ).execute()
        svc.spreadsheets().values().update(
            spreadsheetId=sheet_id,
            range=f"'{actual}'!A1",
            valueInputOption="USER_ENTERED",
            body={"values": grid},
        ).execute()

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
                    body={"requests": ld_recaps_format_requests(gid, layout)},
                ).execute()
                formatted = True
        except Exception as fe:  # pragma: no cover - formatting is best-effort
            logger.warning("ld_recaps_export: formatting failed (values written): %s", fe)

        return {
            "ok": True,
            "tab": actual,
            "rows": data_rows,
            "columns": ncols,
            "formatted": formatted,
            "sheet_id": sheet_id,
        }
    except HttpError as e:
        status = getattr(getattr(e, "resp", None), "status", None)
        if str(status) == "403":
            detail = f"Sheets API 403 — share the sheet with {SERVICE_ACCOUNT_EMAIL} (Editor)."
        else:
            detail = " ".join(str(e).split())[:400]
        logger.warning("ld_recaps_export: write failed (status=%s): %s", status, e)
        return {"ok": False, "error": "sheets-api", "status": status, "detail": detail}
    except Exception as e:  # pragma: no cover - defensive
        logger.exception("ld_recaps_export: unexpected failure")
        return {"ok": False, "error": "unexpected", "detail": " ".join(str(e).split())[:400]}
