"""
One-way Spark → Google Sheet mirror for the per-tenant master tracker.

Each tenant stores their Sheet URL on `Tenant.linked_sheet_url`. When
a Request is created or updated, we push a single row keyed by the
request UUID into the linked Sheet's first worksheet, upserting on
match (looking up the UUID in column A) and appending otherwise.

This is intentionally simple — no two-way sync, no batched diff. The
admin sees a near-real-time mirror of the master tracker in whatever
Sheet their client already lives in.

Auth: reuses GOOGLE_CALENDAR_CREDENTIALS / GS_CREDENTIALS (service
account). The service account email needs Edit access to the linked
Sheet — surfaced as an onboarding step.

All callers should treat failures as non-fatal: a Sheets API miss
must never roll back a Request save. Wrap with try/except.
"""
from __future__ import annotations

import logging
import re
from typing import Iterable

from django.conf import settings
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

logger = logging.getLogger(__name__)

# Column A holds the request UUID — we look it up here to decide
# whether to update an existing row or append a new one. Keep stable
# across deploys; rearranging columns is a manual migration.
HEADER = [
    "Request UUID",
    "Status",
    "Date",
    "Brand",
    "Activation",
    "Retailer",
    "Distributor",
    "Address",
    "State",
    "Start Time",
    "End Time",
    "Assigned RMM",
    "Created At",
    "Updated At",
    "Spark Link",
]

_SHEET_ID_RE = re.compile(r"/d/([a-zA-Z0-9-_]+)")


def extract_sheet_id(url: str) -> str | None:
    if not url:
        return None
    m = _SHEET_ID_RE.search(url)
    return m.group(1) if m else None


SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]


def _credentials():
    """Resolve Sheets API credentials.

    Preference order:
      1. An explicit service-account JSON in GOOGLE_CALENDAR_CREDENTIALS
         / GS_CREDENTIALS (used in dev / local where ADC isn't the app SA).
      2. Application Default Credentials — i.e. the Cloud Run runtime
         service account (spark-api-new-sa@…). This matches how GCS auth
         works in prod (no JSON key to manage). The runtime SA still needs
         Editor access to each target Sheet, and the Sheets API must be
         enabled on the project.

    Returns None only if neither path yields usable creds, in which case
    callers no-op (a Sheets miss must never break a Request save).
    """
    info = getattr(settings, "GOOGLE_CALENDAR_CREDENTIALS", None) or getattr(
        settings, "GS_CREDENTIALS", None
    )
    if info:
        try:
            return service_account.Credentials.from_service_account_info(
                info, scopes=SCOPES
            )
        except Exception as e:
            logger.warning(
                "sheets_mirror: explicit SA creds invalid, trying ADC: %s", e
            )

    # Fall back to ADC / runtime service account.
    try:
        import google.auth

        creds, _project = google.auth.default(scopes=SCOPES)
        return creds
    except Exception as e:
        logger.warning("sheets_mirror: ADC credentials unavailable: %s", e)
        return None


def _service():
    creds = _credentials()
    if not creds:
        return None
    try:
        return build("sheets", "v4", credentials=creds, cache_discovery=False)
    except Exception as e:
        logger.warning("sheets_mirror: failed to build service: %s", e)
        return None


def _qualify(tab: str | None, a1: str) -> str:
    """Prefix an A1 range with a worksheet name when targeting a specific tab.

    When ``tab`` is None we return the bare range, which the Sheets API
    resolves against the spreadsheet's FIRST worksheet — the long-standing
    default for every tenant whose linked Sheet has the Master Tracker as its
    first tab (e.g. Girl Beer). When a tenant sets ``master_tracker_tab_name``
    (e.g. Liquid Death, whose first tab is a backup), we target that tab by
    name so the mirror never writes to the wrong sheet.
    """
    return f"'{tab}'!{a1}" if tab else a1


def _ensure_header(svc, sheet_id: str, tab: str | None = None) -> None:
    """Read row 1; if its first columns don't match HEADER, overwrite just
    those columns. Bootstraps a brand-new sheet on first sync, while NEVER
    touching manual columns past the 15 mirror columns — a tenant's Master
    Tracker may keep hand-maintained columns after "Spark Link" (Liquid Death
    has BA Notes, Contract Sent, …), and those must survive every sync."""
    try:
        resp = (
            svc.spreadsheets()
            .values()
            .get(spreadsheetId=sheet_id, range=_qualify(tab, "A1:Z1"))
            .execute()
        )
        existing = (resp.get("values") or [[]])[0]
        # Compare/write only the first 15 (mirror) columns. If they already
        # match, do nothing (manual columns P+ are left alone).
        if existing[: len(HEADER)] == HEADER:
            return
        svc.spreadsheets().values().update(
            spreadsheetId=sheet_id,
            range=_qualify(tab, f"A1:{_col_letter(len(HEADER))}1"),
            valueInputOption="RAW",
            body={"values": [HEADER]},
        ).execute()
    except HttpError as e:
        logger.warning("sheets_mirror: header write failed: %s", e)


def _col_letter(n: int) -> str:
    s = ""
    while n > 0:
        n, r = divmod(n - 1, 26)
        s = chr(65 + r) + s
    return s


def _find_row_for_uuid(svc, sheet_id: str, uuid: str, tab: str | None = None) -> int | None:
    """Scan column A for the UUID. Returns 1-based row index, or None."""
    try:
        resp = (
            svc.spreadsheets()
            .values()
            .get(spreadsheetId=sheet_id, range=_qualify(tab, "A2:A10000"))
            .execute()
        )
        rows = resp.get("values") or []
        for i, row in enumerate(rows, start=2):
            if row and str(row[0]).strip() == str(uuid).strip():
                return i
        return None
    except HttpError as e:
        logger.warning("sheets_mirror: uuid lookup failed: %s", e)
        return None


def _chunks(seq, n):
    for i in range(0, len(seq), n):
        yield seq[i : i + n]


def _tz_offset_minutes(request) -> int:
    """The request's timezone offset in minutes (e.g. PDT = -420). Used to
    render date/time columns in the event's LOCAL time instead of raw UTC."""
    tz = getattr(request, "timezone", None)
    off = getattr(tz, "offset", None)
    return int(off) if isinstance(off, (int, float)) else 0


def _local(dt, offset_min: int):
    if dt is None:
        return None
    from datetime import timedelta

    try:
        return dt + timedelta(minutes=offset_min)
    except Exception:
        return dt


def _fmt_date(dt, offset_min: int) -> str:
    """Local calendar date, e.g. '5/30/2026'."""
    loc = _local(dt, offset_min)
    return loc.strftime("%-m/%-d/%Y") if loc else ""


def _fmt_time(dt, offset_min: int) -> str:
    """Local clock time, e.g. '4:00 PM'."""
    loc = _local(dt, offset_min)
    return loc.strftime("%-I:%M %p") if loc else ""


def _fmt_dt(dt, offset_min: int) -> str:
    """Local date + time, e.g. '5/26/2026 8:18 PM'."""
    loc = _local(dt, offset_min)
    return loc.strftime("%-m/%-d/%Y %-I:%M %p") if loc else ""


def _row_for_request(request) -> list | None:
    """Build the 15-column sheet row for a Request. Returns None when the
    request has no tenant (nothing to key the Brand column on). Shared by
    the live per-row upsert and the batched backfill."""
    tenant = getattr(request, "tenant", None)
    if tenant is None:
        return None

    def s(v):
        return "" if v is None else str(v)

    off = _tz_offset_minutes(request)

    status_name = ""
    try:
        status_name = (request.status.name or "").strip() if request.status_id else ""
    except Exception:
        pass

    rmm_label = ""
    try:
        rmm = getattr(request, "rmm_asigned", None)
        if rmm:
            rmm_label = (
                " ".join(
                    filter(
                        None,
                        [
                            getattr(rmm, "first_name", "") or "",
                            getattr(rmm, "last_name", "") or "",
                        ],
                    )
                ).strip()
                or getattr(rmm, "email", None)
                or ""
            )
    except Exception:
        pass

    retailer_name = ""
    try:
        retailer_name = getattr(getattr(request, "retailer", None), "name", "") or ""
    except Exception:
        pass

    distributor_name = ""
    try:
        distributor_name = (
            getattr(getattr(request, "distributor", None), "name", "") or ""
        )
    except Exception:
        pass

    state_code = ""
    try:
        state_code = getattr(getattr(request, "state", None), "code", "") or ""
    except Exception:
        pass

    request_type_name = ""
    try:
        request_type_name = (
            getattr(getattr(request, "request_type", None), "name", "") or ""
        )
    except Exception:
        pass

    admin_base = (
        getattr(settings, "ADMIN_FRONTEND_URL", "")
        or "https://admin.igniteproductions.co"
    )
    spark_link = f"{admin_base}/request/view/{request.uuid}"

    return [
        s(request.uuid),
        status_name,
        _fmt_date(getattr(request, "date", None), off),
        s(getattr(tenant, "name", "")),
        request_type_name,
        retailer_name,
        distributor_name,
        s(getattr(request, "address", "")),
        state_code,
        _fmt_time(getattr(request, "start_time", None), off),
        _fmt_time(getattr(request, "end_time", None), off),
        rmm_label,
        _fmt_dt(getattr(request, "created_at", None), off),
        _fmt_dt(getattr(request, "updated_at", None), off),
        spark_link,
    ]


def _parse_sheet_date(value):
    """Parse a Date cell ('M/D/YYYY' or 'YYYY-MM-DD') to a date; None if blank/bad."""
    if not value:
        return None
    from datetime import datetime

    s = str(value).strip()
    for fmt in ("%m/%d/%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(s, fmt).date()
        except (ValueError, TypeError):
            continue
    return None


def _tab_gid(svc, sheet_id: str, tab: str | None) -> int | None:
    """Resolve a worksheet's numeric sheetId (gid). None tab → first worksheet."""
    try:
        meta = (
            svc.spreadsheets()
            .get(spreadsheetId=sheet_id, fields="sheets.properties(title,sheetId,index)")
            .execute()
        )
        sheets = meta.get("sheets", [])
        if not sheets:
            return None
        if not tab:
            sheets = sorted(sheets, key=lambda s: s.get("properties", {}).get("index", 0))
            return sheets[0]["properties"]["sheetId"]
        for s in sheets:
            if s.get("properties", {}).get("title") == tab:
                return s["properties"]["sheetId"]
    except HttpError as e:
        logger.warning("sheets_mirror: gid lookup failed: %s", e)
    return None


def _row_date(cells: list) -> "object | None":
    """The date for a tracker row, scanning col B then col C.

    Mirror rows put the date in col C (B = Status). But LD's hand-curated rows
    are inconsistent: day-first rows ("Sunday | 7/12/2026 | Walmart…") carry the
    date in col B, while state-first rows ("NY | Tuesday | 7/7/2026 | …") carry
    it in col C. Trying B then C handles all three; non-date cells (Status, a
    weekday name, a retailer) parse to None and fall through."""
    col_b = cells[0] if len(cells) > 0 else ""
    col_c = cells[1] if len(cells) > 1 else ""
    return _parse_sheet_date(col_b) or _parse_sheet_date(col_c)


def _date_descending_insert_index(svc, sheet_id: str, tab: str | None, new_date) -> int | None:
    """1-based row to insert a new row *before* so the schedule stays DESCENDING
    (newest first). None → append at end. Rows with no parseable date (section
    dividers, blanks) are skipped, never the insertion point. The date is read
    from col B or col C per row (see _row_date)."""
    if new_date is None:
        return None
    try:
        resp = (
            svc.spreadsheets()
            .values()
            .get(spreadsheetId=sheet_id, range=_qualify(tab, "B2:C100000"))
            .execute()
        )
    except HttpError as e:
        logger.warning("sheets_mirror: date-column read failed: %s", e)
        return None
    for i, r in enumerate(resp.get("values") or [], start=2):
        d = _row_date(r)
        if d is not None and d < new_date:
            return i
    return None


def _insert_dated_row(svc, sheet_id, tab, gid, at_row, row, end_col) -> None:
    """Insert a blank row at `at_row` (1-based) and write `row` into it."""
    svc.spreadsheets().batchUpdate(
        spreadsheetId=sheet_id,
        body={
            "requests": [
                {
                    "insertDimension": {
                        "range": {
                            "sheetId": gid,
                            "dimension": "ROWS",
                            "startIndex": at_row - 1,
                            "endIndex": at_row,
                        },
                        "inheritFromBefore": False,
                    }
                }
            ]
        },
    ).execute()
    svc.spreadsheets().values().update(
        spreadsheetId=sheet_id,
        range=_qualify(tab, f"A{at_row}:{end_col}{at_row}"),
        valueInputOption="RAW",
        body={"values": [row]},
    ).execute()


def _append_or_insert_new(svc, sheet_id, tab, tenant, row, end_col) -> None:
    """Add a NEW request row: date-positioned insert when the tenant opts in
    (master_tracker_insert_by_date + a named tab), else plain append."""
    if getattr(tenant, "master_tracker_insert_by_date", False) and tab:
        at = _date_descending_insert_index(svc, sheet_id, tab, _parse_sheet_date(row[2]))
        if at is not None:
            gid = _tab_gid(svc, sheet_id, tab)
            if gid is not None:
                _insert_dated_row(svc, sheet_id, tab, gid, at, row, end_col)
                return
    svc.spreadsheets().values().append(
        spreadsheetId=sheet_id,
        range=_qualify(tab, f"A2:{end_col}"),
        valueInputOption="RAW",
        insertDataOption="INSERT_ROWS",
        body={"values": [row]},
    ).execute()


# ----------------------------------------------------------------------------
# Liquid Death "MASTER_Tracker" client layout — opt-in via
# Tenant.master_tracker_layout == "ld_retail".
#
# LD's tracker is hand-built with THEIR columns: A State · B Date (weekday) ·
# C Date (M/D/YYYY) · D Store Name · E Start Time ("10a") · F End Time ("1p") ·
# G Address · H Notes · I SKUs to sample — then ~18 manually-maintained columns
# (Recap Received, BA Name, Rate, Email …). The generic 15-column mirror would
# scramble all of that, so this layout writes ONLY columns A–I in LD's format
# and stashes the Spark request UUID in a far-right key column, so we find /
# update our OWN rows in place and NEVER read or touch row 1 (their header) or
# the client's manual columns.
# ----------------------------------------------------------------------------
LD_RETAIL_LAYOUT = "ld_retail"
# Spark UUID key column — far past LD's used grid (61 cols) so it can't collide
# with a client column. _col_letter(70) == "BR".
_LD_KEY_COL_INDEX = 70


def _tenant_layout(tenant) -> str:
    return (getattr(tenant, "master_tracker_layout", "") or "").strip()


def _fmt_time_ld(dt, offset_min: int) -> str:
    """LD clock style: '10a', '1p', '5:30p' — minutes omitted on the hour."""
    loc = _local(dt, offset_min)
    if not loc:
        return ""
    suffix = "a" if loc.hour < 12 else "p"
    hour12 = loc.strftime("%-I")
    return f"{hour12}{suffix}" if loc.minute == 0 else f"{hour12}:{loc.strftime('%M')}{suffix}"


def _weekday_ld(dt, offset_min: int) -> str:
    loc = _local(dt, offset_min)
    return loc.strftime("%A") if loc else ""


def _skus_for_request(request) -> str:
    """'SKUs to sample' — comma-joined product names on the request, de-duped
    in order. Empty on none / any error (a Sheets miss must never break a save)."""
    try:
        out: list[str] = []
        seen: set[str] = set()
        for rp in request.request_product.all():
            product = getattr(rp, "product", None)
            name = (getattr(product, "name", "") or "").strip() if product else ""
            key = name.lower()
            if name and key not in seen:
                seen.add(key)
                out.append(name)
        return ", ".join(out)
    except Exception:
        return ""


def _ld_retail_row(request) -> list | None:
    """LD MASTER_Tracker columns A–I for a Request, in the client's format.
    None when the request has no tenant."""
    tenant = getattr(request, "tenant", None)
    if tenant is None:
        return None
    off = _tz_offset_minutes(request)

    def _attr(getter):
        try:
            return getter() or ""
        except Exception:
            return ""

    state_code = _attr(lambda: getattr(getattr(request, "state", None), "code", ""))
    store = _attr(lambda: getattr(getattr(request, "retailer", None), "name", "")) or (
        getattr(request, "retailer_name", "") or ""
    )
    return [
        state_code,                                               # A State
        _weekday_ld(getattr(request, "date", None), off),         # B Date (weekday)
        _fmt_date(getattr(request, "date", None), off),           # C Date
        store,                                                    # D Store Name
        _fmt_time_ld(getattr(request, "start_time", None), off),  # E Start Time
        _fmt_time_ld(getattr(request, "end_time", None), off),    # F End Time
        getattr(request, "address", "") or "",                    # G Address
        getattr(request, "notes", "") or "",                      # H Notes
        _skus_for_request(request),                               # I SKUs to sample
    ]


def _ld_key_col() -> str:
    return _col_letter(_LD_KEY_COL_INDEX)


def _ld_existing_rows(svc, sheet_id, tab) -> dict:
    """Map {spark_uuid: 1-based row} from the far-right key column."""
    col = _ld_key_col()
    out: dict[str, int] = {}
    try:
        resp = (
            svc.spreadsheets()
            .values()
            .get(spreadsheetId=sheet_id, range=_qualify(tab, f"{col}2:{col}100000"))
            .execute()
        )
        for i, r in enumerate(resp.get("values") or [], start=2):
            if r and str(r[0]).strip():
                out[str(r[0]).strip()] = i
    except HttpError as e:
        logger.warning("sheets_mirror[ld]: key-column read failed: %s", e)
    return out


def _ld_next_row(svc, sheet_id, tab) -> int:
    """First free row to append at: 1 + the furthest non-empty row across the
    Store-Name column (every real client row has one) and the Spark key column.
    Spark rows always land BELOW the client's existing content — never inserted
    among their manual rows."""
    last = 1
    col = _ld_key_col()
    for rng in (_qualify(tab, "D1:D100000"), _qualify(tab, f"{col}1:{col}100000")):
        try:
            vals = (
                svc.spreadsheets()
                .values()
                .get(spreadsheetId=sheet_id, range=rng)
                .execute()
                .get("values")
                or []
            )
        except HttpError as e:
            logger.warning("sheets_mirror[ld]: extent read failed: %s", e)
            continue
        for i in range(len(vals), 0, -1):
            if vals[i - 1] and str(vals[i - 1][0]).strip():
                last = max(last, i)
                break
    return last + 1


def _ld_write(svc, sheet_id, tab, row_idx, data9, uuid) -> None:
    """Write A:I (data) + the key cell as two ranges, so columns J..key-1 (the
    client's manual columns) and row 1 (their header) are never touched."""
    col = _ld_key_col()
    svc.spreadsheets().values().batchUpdate(
        spreadsheetId=sheet_id,
        body={
            "valueInputOption": "USER_ENTERED",
            "data": [
                {"range": _qualify(tab, f"A{row_idx}:I{row_idx}"), "values": [data9]},
                {"range": _qualify(tab, f"{col}{row_idx}"), "values": [[uuid]]},
            ],
        },
    ).execute()


def _ld_grid_info(svc, sheet_id, tab) -> tuple[int | None, int | None]:
    """(gid, columnCount) for a tab (None tab = first worksheet)."""
    try:
        meta = (
            svc.spreadsheets()
            .get(
                spreadsheetId=sheet_id,
                fields="sheets.properties(title,sheetId,index,gridProperties(columnCount))",
            )
            .execute()
        )
    except HttpError as e:
        logger.warning("sheets_mirror[ld]: grid-info read failed: %s", e)
        return None, None
    sheets = meta.get("sheets", [])
    if not sheets:
        return None, None
    if not tab:
        sheets = sorted(sheets, key=lambda s: s.get("properties", {}).get("index", 0))
        p = sheets[0]["properties"]
        return p["sheetId"], p.get("gridProperties", {}).get("columnCount")
    for s in sheets:
        p = s.get("properties", {})
        if p.get("title") == tab:
            return p["sheetId"], p.get("gridProperties", {}).get("columnCount")
    return None, None


def _ld_ensure_grid(svc, sheet_id, tab) -> int | None:
    """Make sure the tab is wide enough for the far-right Spark key column
    (BR = col 70). Client-built sheets are usually narrower, and a too-narrow
    grid makes every key write fail with 'exceeds grid limits' — which once
    left an inserted-but-never-filled blank row behind (REQ-1208). Appending
    empty columns is purely additive: it never touches existing data or the
    client's manual columns. Returns the tab's gid (reusable by callers), or
    None when the tab wasn't found."""
    gid, cols = _ld_grid_info(svc, sheet_id, tab)
    if gid is None:
        return None
    need = _LD_KEY_COL_INDEX + 2  # key column + one spare
    if cols is not None and cols < need:
        try:
            svc.spreadsheets().batchUpdate(
                spreadsheetId=sheet_id,
                body={
                    "requests": [
                        {
                            "appendDimension": {
                                "sheetId": gid,
                                "dimension": "COLUMNS",
                                "length": need - cols,
                            }
                        }
                    ]
                },
            ).execute()
            logger.info(
                "sheets_mirror[ld]: widened tab %r from %s to %s columns",
                tab, cols, need,
            )
        except HttpError as e:
            logger.warning("sheets_mirror[ld]: column append failed: %s", e)
    return gid


def _ld_remove_blank_rows(svc, sheet_id, tab, gid, last_row: int = 40) -> int:
    """Delete fully-blank rows in the top section (rows 2..last_row) — debris
    from a past insert whose content write then failed on the too-narrow grid
    (see _ld_ensure_grid). A row only qualifies when BOTH its data block
    (A:O) and its Spark key cell are empty, and the scan stays in the top
    slice where Spark inserts land, so a client's intentional spacer rows
    deeper in the sheet are never touched."""
    col = _ld_key_col()
    try:
        resp = (
            svc.spreadsheets()
            .values()
            .batchGet(
                spreadsheetId=sheet_id,
                ranges=[
                    _qualify(tab, f"A2:O{last_row}"),
                    _qualify(tab, f"{col}2:{col}{last_row}"),
                ],
            )
            .execute()
        )
    except HttpError as e:
        logger.warning("sheets_mirror[ld]: blank-row scan failed: %s", e)
        return 0
    ranges = resp.get("valueRanges", [])
    data = (ranges[0].get("values") if len(ranges) > 0 else None) or []
    keys = (ranges[1].get("values") if len(ranges) > 1 else None) or []

    def _blank(cells) -> bool:
        # An inserted row inherits its neighbors' checkbox data-validation,
        # so an orphaned row reads as FALSE in those cells, not "" — treat
        # unchecked checkboxes as blank. A checked box (TRUE) is real input.
        return not any(
            str(c).strip() and str(c).strip().upper() != "FALSE" for c in cells
        )

    to_delete = []
    for i in range(last_row - 1):  # 0-based offset from row 2
        row_cells = data[i] if i < len(data) else []
        key_cells = keys[i] if i < len(keys) else []
        if _blank(row_cells) and _blank(key_cells):
            to_delete.append(i + 2)
    # Trailing not-yet-used rows also read as blank — only delete blanks that
    # sit ABOVE real content (true gaps), never the empty tail of the sheet.
    last_content = 0
    for i in range(last_row - 1):
        row_cells = data[i] if i < len(data) else []
        key_cells = keys[i] if i < len(keys) else []
        if not (_blank(row_cells) and _blank(key_cells)):
            last_content = i + 2
    to_delete = [r for r in to_delete if r < last_content]
    if not to_delete:
        return 0
    try:
        svc.spreadsheets().batchUpdate(
            spreadsheetId=sheet_id,
            body={
                "requests": [
                    {
                        "deleteDimension": {
                            "range": {
                                "sheetId": gid,
                                "dimension": "ROWS",
                                "startIndex": r - 1,
                                "endIndex": r,
                            }
                        }
                    }
                    # bottom-up so earlier deletes don't shift later indexes
                    for r in sorted(to_delete, reverse=True)
                ]
            },
        ).execute()
    except HttpError as e:
        logger.warning("sheets_mirror[ld]: blank-row delete failed: %s", e)
        return 0
    logger.info(
        "sheets_mirror[ld]: removed %s blank row(s) at %s", len(to_delete), to_delete
    )
    return len(to_delete)


def delete_ld_rows(tenant, rows: list[int]) -> tuple[int, list[str]]:
    """Delete specific 1-based rows from an LD-layout tracker — a guarded
    one-off for pruning a client's hand-entered duplicates after Spark's
    keyed rows for the same events landed. Refuses the header, anything
    outside the top section (rows 2..40), and — the hard guard — any row
    that carries a Spark key: mirror-managed rows can only be removed by
    deleting the request in Spark, never by this pruner. Returns
    (deleted_count, notes) where notes records, per row, either the A:I
    content that was deleted (audit trail) or why it was refused.
    """
    notes: list[str] = []
    if _tenant_layout(tenant) != LD_RETAIL_LAYOUT:
        return 0, ["tenant is not on the ld_retail layout — refusing"]
    sheet_id = extract_sheet_id(getattr(tenant, "linked_sheet_url", "") or "")
    if not sheet_id:
        return 0, ["tenant has no linked_sheet_url"]
    svc = _service()
    if not svc:
        return 0, ["Sheets API service unavailable"]
    tab = (getattr(tenant, "master_tracker_tab_name", "") or "").strip() or None

    gid = _ld_ensure_grid(svc, sheet_id, tab)
    if gid is None:
        return 0, ["tab not found"]
    col = _ld_key_col()
    targets = sorted({r for r in rows if 2 <= r <= 40})
    refused = sorted(set(rows) - set(targets))
    if refused:
        notes.append(f"refused (outside rows 2-40): {refused}")
    if not targets:
        return 0, notes
    lo, hi = targets[0], targets[-1]
    try:
        resp = (
            svc.spreadsheets()
            .values()
            .batchGet(
                spreadsheetId=sheet_id,
                ranges=[
                    _qualify(tab, f"A{lo}:I{hi}"),
                    _qualify(tab, f"{col}{lo}:{col}{hi}"),
                ],
            )
            .execute()
        )
    except HttpError as e:
        return 0, notes + [f"pre-delete read failed: {e}"]
    ranges = resp.get("valueRanges", [])
    data = (ranges[0].get("values") if len(ranges) > 0 else None) or []
    keys = (ranges[1].get("values") if len(ranges) > 1 else None) or []

    deletable: list[int] = []
    for r in targets:
        i = r - lo
        key_cells = keys[i] if i < len(keys) else []
        key = str(key_cells[0]).strip() if key_cells else ""
        if key:
            notes.append(f"refused row {r}: carries Spark key {key}")
            continue
        row_cells = data[i] if i < len(data) else []
        notes.append(
            f"deleting row {r}: " + " | ".join(str(c) for c in row_cells[:9])
        )
        deletable.append(r)
    if not deletable:
        return 0, notes
    try:
        svc.spreadsheets().batchUpdate(
            spreadsheetId=sheet_id,
            body={
                "requests": [
                    {
                        "deleteDimension": {
                            "range": {
                                "sheetId": gid,
                                "dimension": "ROWS",
                                "startIndex": r - 1,
                                "endIndex": r,
                            }
                        }
                    }
                    # bottom-up so earlier deletes don't shift later indexes
                    for r in sorted(deletable, reverse=True)
                ]
            },
        ).execute()
    except HttpError as e:
        return 0, notes + [f"delete failed: {e}"]
    logger.info(
        "sheets_mirror[ld]: pruned %s row(s) at %s", len(deletable), deletable
    )
    return len(deletable), notes


def _ld_insert_dated_row(svc, sheet_id, tab, gid, at_row, row9, uuid) -> None:
    """LD-layout counterpart to _insert_dated_row: insert a blank row at
    at_row (1-based, pushing everything from there down) and write the A:I
    data + Spark key column into it. _insert_dated_row only knows the
    generic contiguous layout, hence the separate function.

    If the content write fails after the row was inserted (e.g. a grid-limit
    error), the inserted row is deleted again so the sheet is never left with
    an orphaned blank row, then the error propagates."""
    svc.spreadsheets().batchUpdate(
        spreadsheetId=sheet_id,
        body={
            "requests": [
                {
                    "insertDimension": {
                        "range": {
                            "sheetId": gid,
                            "dimension": "ROWS",
                            "startIndex": at_row - 1,
                            "endIndex": at_row,
                        },
                        "inheritFromBefore": False,
                    }
                }
            ]
        },
    ).execute()
    try:
        _ld_write(svc, sheet_id, tab, at_row, row9, uuid)
    except HttpError:
        try:
            svc.spreadsheets().batchUpdate(
                spreadsheetId=sheet_id,
                body={
                    "requests": [
                        {
                            "deleteDimension": {
                                "range": {
                                    "sheetId": gid,
                                    "dimension": "ROWS",
                                    "startIndex": at_row - 1,
                                    "endIndex": at_row,
                                }
                            }
                        }
                    ]
                },
            ).execute()
        except HttpError as cleanup_err:  # pragma: no cover - best-effort
            logger.warning(
                "sheets_mirror[ld]: failed-insert cleanup also failed: %s",
                cleanup_err,
            )
        raise


def _ld_upsert_request_row(svc, sheet_id, tab, request) -> bool:
    data9 = _ld_retail_row(request)
    if data9 is None:
        return False
    # Widen the grid to the key column FIRST: on a too-narrow client sheet
    # even the key-column READ below 400s, so every request looked new and
    # every write failed. Also hands back the gid for the insert path.
    gid = _ld_ensure_grid(svc, sheet_id, tab)
    uuid = str(request.uuid)
    existing = _ld_existing_rows(svc, sheet_id, tab)
    row_idx = existing.get(uuid)
    if row_idx is not None:
        _ld_write(svc, sheet_id, tab, row_idx, data9, uuid)
        return True

    # Brand-new row: LD wants fresh submissions surfaced at the top of the
    # sheet, not appended below ~4,500 rows of history — the same
    # master_tracker_insert_by_date opt-in the generic (non-LD) layout
    # already honors for its own appends (see _append_or_insert_new).
    tenant = getattr(request, "tenant", None)
    if getattr(tenant, "master_tracker_insert_by_date", False) and tab:
        at = (
            _date_descending_insert_index(svc, sheet_id, tab, _parse_sheet_date(data9[2]))
            if gid is not None
            else None
        )
        if at is not None:
            _ld_insert_dated_row(svc, sheet_id, tab, gid, at, data9, uuid)
            return True

    row_idx = _ld_next_row(svc, sheet_id, tab)
    _ld_write(svc, sheet_id, tab, row_idx, data9, uuid)
    return True


def _ld_bulk_sync(svc, sheet_id, tab, requests) -> tuple[int, str | None]:
    """Returns (rows_written, error_message). error_message is None on a
    clean run; a partial failure still reports whatever DID land in
    rows_written instead of discarding it (see the per-chunk try below —
    this used to be one try/except around the whole loop, so a failure on
    the last chunk reported 0 written even if earlier chunks had already
    landed, and the actual API error was logged server-side only, never
    surfaced to whoever ran the backfill).

    Existing rows (already in the sheet, keyed by UUID) are batched — their
    row index never moves, so it's safe to write them all in one shot.
    Brand-new rows go one at a time when master_tracker_insert_by_date is
    set: an insertDimension shifts every row below it, so the next
    insertion point has to be resolved fresh each time, not pre-computed.
    """
    requests = list(requests)
    if not requests:
        return 0, None
    tenant = getattr(requests[0], "tenant", None)
    # Heal the grid before ANY read/write: widen to the key column (narrow
    # client sheets 400 even on key-column reads) and clear blank-row debris
    # left by past half-failed inserts — BEFORE reading row indexes, since
    # deletes shift everything below them.
    gid = _ld_ensure_grid(svc, sheet_id, tab)
    if gid is not None:
        _ld_remove_blank_rows(svc, sheet_id, tab, gid)
    existing = _ld_existing_rows(svc, sheet_id, tab)
    col = _ld_key_col()

    update_payload: list = []
    new_rows: list[tuple[str, list]] = []
    for req in requests:
        row9 = _ld_retail_row(req)
        if row9 is None:
            continue
        uuid = str(req.uuid)
        idx = existing.get(uuid)
        if idx is not None:
            update_payload.append({"range": _qualify(tab, f"A{idx}:I{idx}"), "values": [row9]})
            update_payload.append({"range": _qualify(tab, f"{col}{idx}"), "values": [[uuid]]})
        else:
            new_rows.append((uuid, row9))

    written = 0
    for chunk in _chunks(update_payload, 1000):
        try:
            svc.spreadsheets().values().batchUpdate(
                spreadsheetId=sheet_id,
                body={"valueInputOption": "USER_ENTERED", "data": chunk},
            ).execute()
        except HttpError as e:
            logger.warning(
                "sheets_mirror[ld]: bulk update failed after %s rows: %s", written // 2, e
            )
            return written // 2, str(e)
        written += len(chunk)
    written //= 2

    insert_by_date = bool(getattr(tenant, "master_tracker_insert_by_date", False)) and tab
    if not (insert_by_date and new_rows):
        gid = None
    for uuid, row9 in new_rows:
        at = (
            _date_descending_insert_index(svc, sheet_id, tab, _parse_sheet_date(row9[2]))
            if gid is not None
            else None
        )
        try:
            if at is not None:
                _ld_insert_dated_row(svc, sheet_id, tab, gid, at, row9, uuid)
            else:
                idx = _ld_next_row(svc, sheet_id, tab)
                _ld_write(svc, sheet_id, tab, idx, row9, uuid)
        except HttpError as e:
            logger.warning(
                "sheets_mirror[ld]: new-row write failed after %s rows: %s", written, e
            )
            return written, str(e)
        written += 1

    return written, None


def upsert_request_row(request) -> bool:
    """Sync one Request row into its tenant's linked Sheet.

    Returns True on success (row inserted or updated), False otherwise.
    Safe to call on every Request save — every failure path logs and
    returns False without raising.
    """
    try:
        tenant = getattr(request, "tenant", None)
        sheet_url = getattr(tenant, "linked_sheet_url", None) if tenant else None
        if not sheet_url:
            return False
        sheet_id = extract_sheet_id(sheet_url)
        if not sheet_id:
            return False
        svc = _service()
        if not svc:
            return False

        row = _row_for_request(request)
        if row is None:
            return False

        # Optional per-tenant Master Tracker tab (None = first worksheet,
        # the default for every tenant whose tracker is the first tab).
        tab = (getattr(tenant, "master_tracker_tab_name", "") or "").strip() or None

        # Client-specific column layout (Liquid Death): write only A–I in their
        # format, keyed by a far-right Spark-UUID column; never touch the header
        # or their manual columns. Returns before the generic 15-col path.
        if _tenant_layout(tenant) == LD_RETAIL_LAYOUT:
            return _ld_upsert_request_row(svc, sheet_id, tab, request)

        _ensure_header(svc, sheet_id, tab)

        existing_row = _find_row_for_uuid(svc, sheet_id, str(request.uuid), tab)
        end_col = _col_letter(len(row))

        if existing_row:
            svc.spreadsheets().values().update(
                spreadsheetId=sheet_id,
                range=_qualify(tab, f"A{existing_row}:{end_col}{existing_row}"),
                valueInputOption="RAW",
                body={"values": [row]},
            ).execute()
        else:
            _append_or_insert_new(svc, sheet_id, tab, tenant, row, end_col)
        return True
    except HttpError as e:
        logger.warning(
            "sheets_mirror: API error for request=%s: %s",
            getattr(request, "id", None),
            e,
        )
        return False
    except Exception as e:
        logger.warning(
            "sheets_mirror: unexpected error for request=%s: %s",
            getattr(request, "id", None),
            e,
        )
        return False


def bulk_sync_requests(requests) -> tuple[int, str | None]:
    """Batched backfill for many Requests that share ONE tenant/sheet.

    The per-row upsert makes ~3 Sheets API calls (header check + UUID
    lookup + write). At hundreds of rows that blows past the Sheets quota
    (60 read & 60 write requests / min / user) and 429s. This does the
    whole tenant in a small constant number of calls instead:

        1 header ensure + 1 column-A read
        + ⌈new/500⌉ append calls + ⌈existing/500⌉ batchUpdate calls.

    Assumes every request belongs to the same tenant (the management
    command groups by tenant). Returns (rows_written, error_message) —
    error_message is None on a clean run; on a failure it carries the
    actual Sheets API error instead of only logging it server-side, so
    whoever ran the backfill (a one-off cron dispatch, usually) can see
    why without needing Cloud Run log access.
    """
    requests = list(requests)
    if not requests:
        return 0, None

    tenant = getattr(requests[0], "tenant", None)
    sheet_url = getattr(tenant, "linked_sheet_url", None) if tenant else None
    sheet_id = extract_sheet_id(sheet_url or "")
    if not sheet_id:
        return 0, "tenant has no linked_sheet_url (or it isn't a valid Sheets URL)"
    svc = _service()
    if not svc:
        return 0, "Sheets API service unavailable (credentials not configured)"

    # Optional per-tenant Master Tracker tab (None = first worksheet).
    tab = (getattr(tenant, "master_tracker_tab_name", "") or "").strip() or None

    # Client-specific column layout (Liquid Death) — see _ld_bulk_sync.
    if _tenant_layout(tenant) == LD_RETAIL_LAYOUT:
        return _ld_bulk_sync(svc, sheet_id, tab, requests)

    _ensure_header(svc, sheet_id, tab)

    # One read of column A → {uuid: 1-based row index} so we can tell
    # appends from in-place updates without a per-row lookup.
    existing: dict[str, int] = {}
    try:
        resp = (
            svc.spreadsheets()
            .values()
            .get(spreadsheetId=sheet_id, range=_qualify(tab, "A2:A100000"))
            .execute()
        )
        for i, r in enumerate(resp.get("values") or [], start=2):
            if r and str(r[0]).strip():
                existing[str(r[0]).strip()] = i
    except HttpError as e:
        logger.warning("sheets_mirror: bulk column-A read failed: %s", e)
        return 0, str(e)

    end_col = _col_letter(len(HEADER))
    appends: list[list] = []
    updates: list[dict] = []
    for req in requests:
        row = _row_for_request(req)
        if row is None:
            continue
        idx = existing.get(str(req.uuid))
        if idx:
            updates.append(
                {"range": _qualify(tab, f"A{idx}:{end_col}{idx}"), "values": [row]}
            )
        else:
            appends.append(row)

    insert_by_date = bool(getattr(tenant, "master_tracker_insert_by_date", False)) and tab

    written = 0
    error = None
    try:
        # Updates first: they target absolute row indices from the column-A
        # read above, so they must run BEFORE any date-insert shifts rows.
        for chunk in _chunks(updates, 500):
            svc.spreadsheets().values().batchUpdate(
                spreadsheetId=sheet_id,
                body={"valueInputOption": "RAW", "data": chunk},
            ).execute()
            written += len(chunk)

        if insert_by_date and appends:
            # Place each new row at its date-sorted slot (descending). Each
            # call re-reads col C, so sequential inserts stay correct and the
            # final order is independent of insertion order.
            gid = _tab_gid(svc, sheet_id, tab)
            for row in appends:
                at = (
                    _date_descending_insert_index(svc, sheet_id, tab, _parse_sheet_date(row[2]))
                    if gid is not None
                    else None
                )
                if at is not None:
                    _insert_dated_row(svc, sheet_id, tab, gid, at, row, end_col)
                else:
                    svc.spreadsheets().values().append(
                        spreadsheetId=sheet_id,
                        range=_qualify(tab, f"A2:{end_col}"),
                        valueInputOption="RAW",
                        insertDataOption="INSERT_ROWS",
                        body={"values": [row]},
                    ).execute()
                written += 1
        else:
            for chunk in _chunks(appends, 500):
                svc.spreadsheets().values().append(
                    spreadsheetId=sheet_id,
                    range=_qualify(tab, f"A2:{end_col}"),
                    valueInputOption="RAW",
                    insertDataOption="INSERT_ROWS",
                    body={"values": chunk},
                ).execute()
                written += len(chunk)
    except HttpError as e:
        logger.warning(
            "sheets_mirror: bulk write failed after %s rows: %s", written, e
        )
        error = str(e)

    return written, error


def upsert_many(requests: Iterable) -> int:
    """Per-row variant, kept for compatibility. Prefer bulk_sync_requests
    for backfills — it batches to respect the Sheets API quota."""
    n = 0
    for r in requests:
        if upsert_request_row(r):
            n += 1
    return n
