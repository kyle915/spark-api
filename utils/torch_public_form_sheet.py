"""Append public Torch spark-form requests to the retail-schedule Sheet.

ONLY ``createRequestByUrl`` for ``/spark-form/keee-torch-thc`` writes here.
Admin tracker creates and the Binny/TWM bulk importer must not flood this
Sheet — they use other paths and never call this module.

The live workbook already has Binny's / Total Wine columns (State, Day of
Week, Date, Store Name, Start/End, Address, SKUs, Rate, …). We fill those
in the client's format and ADD Spark mapping columns on row 1 without
touching existing data or inventing BA rates.

Auth: same ADC / service-account path as ``utils.sheets_mirror``. Failures
are logged and swallowed — a Sheets miss must never 500 the public form.
"""
from __future__ import annotations

import logging
import re
from typing import Any

from django.conf import settings
from googleapiclient.errors import HttpError

from events.routing import extract_state_code
from events.torch_portal import is_torch_public_form_slug, is_torch_tenant
from utils.sheets_mirror import (
    _col_letter,
    _fmt_dt,
    _fmt_time_ld,
    _local,
    _qualify,
    _service,
    _skus_for_request,
    _tz_for_request,
    _weekday_ld,
)

logger = logging.getLogger(__name__)

TORCH_PUBLIC_FORM_SHEET_ID = "1kAvZhy2B9HoeSS-qjKXve8JWUV1oBxDqnhs1-7dQUYw"
TORCH_PUBLIC_FORM_GID = 0
SERVICE_ACCOUNT_EMAIL = "spark-api-new-sa@spark-479222.iam.gserviceaccount.com"

# Extra columns appended after the client's existing retail-schedule header.
# Never rename Rate / BA Name / Recap — those stay ops-owned and blank.
SPARK_EXTRA_HEADERS = [
    "Spark Request UUID",
    "Spark Link",
    "Request ID",
    "Request Type",
    "BA Count",
    "Non-Active",
    "Cases to Ship",
    "Requestor Name",
    "Requestor Email",
    "Schedule With",
    "City",
    "Submitted At",
]

UUID_HEADER = SPARK_EXTRA_HEADERS[0]

_BA_COUNT_RE = re.compile(r"BA count:\s*(\d+)", re.I)
_COUNTRY_SUFFIX_RE = re.compile(
    r"[\s,]*(?:united states of america|united states|u\.?\s*s\.?\s*a\.?|"
    r"u\.?\s*s\.?)\s*$",
    re.IGNORECASE,
)
_TRAILING_STATE_ZIP_RE = re.compile(
    r",?\s*[A-Za-z]{2}\s+\d{2,5}(?:-\d{4})?\s*$"
)

_SCHEDULE_LABELS = {
    "already_scheduled": "Schedule with account",
    "needs_scheduling": "Schedule for me",
}


def should_append_torch_public_form(
    request_url_name: str | None, tenant: Any
) -> bool:
    """True only for the unauthenticated Torch public spark-form path."""
    return is_torch_public_form_slug(request_url_name) and is_torch_tenant(tenant)


def _s(value: Any) -> str:
    return "" if value is None else str(value)


def _yes_no(value: Any) -> str:
    if value is True:
        return "Yes"
    if value is False:
        return "No"
    return ""


def _ba_count(request) -> str:
    notes = getattr(request, "notes", None) or ""
    match = _BA_COUNT_RE.search(notes)
    return match.group(1) if match else ""


def _store_name(request) -> str:
    retailer = getattr(request, "retailer", None)
    from_fk = getattr(retailer, "name", "") if retailer is not None else ""
    return (
        (from_fk or "").strip()
        or (getattr(request, "retailer_name", None) or "").strip()
        or (getattr(request, "name", None) or "").strip()
    )


def _state_code(request) -> str:
    try:
        code = getattr(getattr(request, "state", None), "code", "") or ""
        if code:
            return str(code).upper()
    except Exception:
        pass
    return extract_state_code(getattr(request, "address", None)) or ""


def extract_city(address: str | None) -> str:
    """Best-effort city from a Google Places / comma address."""
    if not address:
        return ""
    norm = re.sub(r"[\t ]+", " ", address.strip())
    norm = _COUNTRY_SUFFIX_RE.sub("", norm).strip()
    norm = _TRAILING_STATE_ZIP_RE.sub("", norm).strip().rstrip(",")
    parts = [p.strip() for p in norm.split(",") if p.strip()]
    if len(parts) >= 2:
        return parts[-1]
    return ""


def _fmt_date_torch(dt, tz) -> str:
    """Match the existing retail-schedule dates: 'Sep 10, 2026'."""
    loc = _local(dt, tz)
    if not loc:
        return ""
    return loc.strftime("%b %-d, %Y")


def _schedule_with(request) -> str:
    raw = (getattr(request, "scheduling_status", None) or "").strip()
    if raw in _SCHEDULE_LABELS:
        return _SCHEDULE_LABELS[raw]
    return ""


def _spark_link(request) -> str:
    admin_base = (
        getattr(settings, "ADMIN_FRONTEND_URL", "")
        or "https://admin.igniteproductions.co"
    ).rstrip("/")
    uuid = getattr(request, "uuid", None)
    if not uuid:
        return ""
    return f"{admin_base}/request/view/{uuid}"


def build_torch_public_form_values(request) -> dict[str, str]:
    """Header-name → cell value for one public Torch request.

    Ops-owned columns (Rate, BA Name, Recap, Contract, …) are omitted so
    we never invent a rate or clobber staffing fields.
    """
    tz = _tz_for_request(request)
    address = getattr(request, "address", None) or ""
    req_id = getattr(request, "id", None)
    request_type = getattr(getattr(request, "request_type", None), "name", "") or ""
    return {
        "State": _state_code(request),
        "Day of Week": _weekday_ld(getattr(request, "date", None), tz),
        "Date": _fmt_date_torch(getattr(request, "date", None), tz),
        "Store Name": _store_name(request),
        "Start Time": _fmt_time_ld(getattr(request, "start_time", None), tz),
        "End Time": _fmt_time_ld(getattr(request, "end_time", None), tz),
        "Address": address,
        "Requested? ": "Y",
        "SKUs to sample": _skus_for_request(request),
        "Cell Phone #": _s(getattr(request, "store_manager_phone", None)).strip(),
        UUID_HEADER: _s(getattr(request, "uuid", None)),
        "Spark Link": _spark_link(request),
        "Request ID": f"REQ-{req_id}" if req_id else "",
        "Request Type": request_type,
        "BA Count": _ba_count(request),
        "Non-Active": _yes_no(
            getattr(request, "is_non_active_product_required", None)
        ),
        "Cases to Ship": _s(getattr(request, "cases_to_be_shipped", None)).strip(),
        "Requestor Name": (
            (getattr(request, "client_name", None) or "").strip()
        ),
        "Requestor Email": (
            (getattr(request, "requestor_email", None) or "").strip()
            or (getattr(request, "client_email", None) or "").strip()
        ),
        "Schedule With": _schedule_with(request),
        "City": extract_city(address),
        "Submitted At": _fmt_dt(getattr(request, "created_at", None), tz),
    }


def _tab_for_gid(svc, sheet_id: str, gid: int) -> str | None:
    try:
        meta = (
            svc.spreadsheets()
            .get(spreadsheetId=sheet_id, fields="sheets.properties")
            .execute()
        )
        for sheet in meta.get("sheets") or []:
            props = sheet.get("properties") or {}
            if props.get("sheetId") == gid:
                return props.get("title")
        sheets = meta.get("sheets") or []
        if sheets:
            return (sheets[0].get("properties") or {}).get("title")
    except HttpError as e:
        logger.warning("torch public-form sheet: tab lookup failed: %s", e)
    return "Retail Schedule"


def _read_header(svc, sheet_id: str, tab: str | None) -> list[str]:
    resp = (
        svc.spreadsheets()
        .values()
        .get(spreadsheetId=sheet_id, range=_qualify(tab, "A1:AZ1"))
        .execute()
    )
    return list((resp.get("values") or [[]])[0])


def _ensure_extra_headers(svc, sheet_id: str, tab: str | None) -> list[str]:
    """Append Spark columns onto row 1 if missing. Never rewrite A–AB."""
    existing = _read_header(svc, sheet_id, tab)
    have = {h.strip().lower() for h in existing}
    missing = [h for h in SPARK_EXTRA_HEADERS if h.strip().lower() not in have]
    if not missing:
        return existing
    start = len(existing) + 1
    end = start + len(missing) - 1
    svc.spreadsheets().values().update(
        spreadsheetId=sheet_id,
        range=_qualify(tab, f"{_col_letter(start)}1:{_col_letter(end)}1"),
        valueInputOption="RAW",
        body={"values": [missing]},
    ).execute()
    return existing + missing


def _row_from_values(header: list[str], values: dict[str, str]) -> list[str]:
    out: list[str] = []
    for name in header:
        key = name
        if key in values:
            out.append(values[key])
            continue
        # Client header has a trailing space on "Requested? " / "Shipped? ".
        stripped = name.strip()
        matched = ""
        for vk, vv in values.items():
            if vk.strip() == stripped:
                matched = vv
                break
        out.append(matched)
    return out


def _find_uuid_row(svc, sheet_id: str, tab: str | None, header: list[str], uuid: str) -> int | None:
    try:
        idx = next(
            i
            for i, name in enumerate(header)
            if (name or "").strip().lower() == UUID_HEADER.lower()
        )
    except StopIteration:
        return None
    col = _col_letter(idx + 1)
    try:
        resp = (
            svc.spreadsheets()
            .values()
            .get(spreadsheetId=sheet_id, range=_qualify(tab, f"{col}2:{col}100000"))
            .execute()
        )
        for i, row in enumerate(resp.get("values") or [], start=2):
            if row and str(row[0]).strip() == str(uuid).strip():
                return i
    except HttpError as e:
        logger.warning("torch public-form sheet: uuid lookup failed: %s", e)
    return None



def _parse_sheet_date(cell: str):
    """Parse a Date cell. None when it is not a date we recognise.

    The column is client-maintained free text, so this never guesses — an
    unrecognised cell simply doesn't participate in ordering.
    """
    import datetime as _dt

    text = (cell or "").strip()
    if not text:
        return None
    for fmt in ("%m/%d/%Y", "%m/%d/%y", "%Y-%m-%d", "%m-%d-%Y",
                "%b %d, %Y", "%B %d, %Y"):
        try:
            return _dt.datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def _date_column_values(svc, sheet_id: str, tab, header: list[str]):
    """[(row_number, date)] for every parseable Date cell, in sheet order."""
    try:
        di = header.index("Date")
    except ValueError:
        return None
    grid = (
        svc.spreadsheets()
        .values()
        .get(
            spreadsheetId=sheet_id,
            range=_qualify(tab, f"{_col_letter(di + 1)}2:{_col_letter(di + 1)}"),
        )
        .execute()
        .get("values", [])
    )
    out = []
    for n, row in enumerate(grid, start=2):
        d = _parse_sheet_date(row[0] if row else "")
        if d is not None:
            out.append((n, d))
    return out


def _insert_index_for_date(dated, target):
    """1-based row to insert BEFORE so `target` lands in date order.

    None means "no opinion — append instead". That is returned when the sheet
    is not already ascending, because there is no correct position in an
    unsorted list and quietly inventing one would scatter rows.
    """
    if not dated or target is None:
        return None
    for (_, a), (_, b) in zip(dated, dated[1:]):
        if b < a:
            return None  # not sorted; don't pretend to know where this goes
    for row_number, d in dated:
        if d > target:
            return row_number
    return None  # belongs at the end


def _insert_row_at(svc, sheet_id: str, tab, gid: int, index: int, row: list[str]):
    """Open a blank row at `index` and write `row` into it."""
    svc.spreadsheets().batchUpdate(
        spreadsheetId=sheet_id,
        body={
            "requests": [
                {
                    "insertDimension": {
                        "range": {
                            "sheetId": gid,
                            "dimension": "ROWS",
                            "startIndex": index - 1,
                            "endIndex": index,
                        },
                        "inheritFromBefore": True,
                    }
                }
            ]
        },
    ).execute()
    svc.spreadsheets().values().update(
        spreadsheetId=sheet_id,
        range=_qualify(tab, f"A{index}"),
        valueInputOption="USER_ENTERED",
        body={"values": [row]},
    ).execute()


def _delete_row(svc, sheet_id: str, gid: int, index: int):
    """Remove one row. Callers must have confirmed it is a Spark-written row."""
    svc.spreadsheets().batchUpdate(
        spreadsheetId=sheet_id,
        body={
            "requests": [
                {
                    "deleteDimension": {
                        "range": {
                            "sheetId": gid,
                            "dimension": "ROWS",
                            "startIndex": index - 1,
                            "endIndex": index,
                        }
                    }
                }
            ]
        },
    ).execute()


def _append_row(request) -> bool:
    """Write one already-authorised request onto the sheet.

    Callers own the authorisation decision; this only writes. Split out so the
    public-form and signed-in paths cannot drift into two different row shapes
    or two different dedupe rules.

    Returns True when a row was written. Returns False on skip / soft fail, and
    never raises — a Sheets problem must not 500 a request submission. Use
    `diagnose_torch_sheet` to tell those two Falses apart.
    """
    try:
        uuid = getattr(request, "uuid", None)
        if not uuid:
            return False
        svc = _service()
        if not svc:
            logger.warning(
                "torch sheet: no Sheets credentials (share Editor with %s)",
                SERVICE_ACCOUNT_EMAIL,
            )
            return False
        tab = _tab_for_gid(svc, TORCH_PUBLIC_FORM_SHEET_ID, TORCH_PUBLIC_FORM_GID)
        header = _ensure_extra_headers(svc, TORCH_PUBLIC_FORM_SHEET_ID, tab)
        if _find_uuid_row(svc, TORCH_PUBLIC_FORM_SHEET_ID, tab, header, str(uuid)):
            return False
        values = build_torch_public_form_values(request)
        row = _row_from_values(header, values)

        # Land the row in DATE order rather than at the bottom. The client's
        # schedule is maintained chronologically and they read it that way, so
        # appending buried each new request below December.
        #
        # Falls back to a plain append whenever the position isn't knowable —
        # sheet not sorted, no Date column, unparseable date. Appending is
        # merely untidy; guessing a position in an unsorted sheet scatters rows
        # through the client's data, which is worse and harder to undo.
        target = _parse_sheet_date(values.get("Date", ""))
        dated = _date_column_values(svc, TORCH_PUBLIC_FORM_SHEET_ID, tab, header)
        index = _insert_index_for_date(dated, target) if dated is not None else None

        if index is not None:
            _insert_row_at(
                svc, TORCH_PUBLIC_FORM_SHEET_ID, tab,
                TORCH_PUBLIC_FORM_GID, index, row,
            )
        else:
            svc.spreadsheets().values().append(
                spreadsheetId=TORCH_PUBLIC_FORM_SHEET_ID,
                range=_qualify(tab, "A:A"),
                valueInputOption="USER_ENTERED",
                insertDataOption="INSERT_ROWS",
                body={"values": [row]},
            ).execute()
        return True
    except Exception:
        logger.warning(
            "torch sheet append failed for request=%s",
            getattr(request, "id", None),
            exc_info=True,
        )
        return False


def append_torch_public_form_row(
    request, request_url_name: str | None = None
) -> bool:
    """Append one Torch row submitted through the PUBLIC spark-form.

    Gated on the form slug as well as the tenant, so nothing else can reach
    this entry point by accident.
    """
    tenant = getattr(request, "tenant", None)
    if not should_append_torch_public_form(request_url_name, tenant):
        return False
    return _append_row(request)


def append_torch_request_row(request) -> bool:
    """Append one Torch row created by a SIGNED-IN user (the in-app form).

    Tenant-gated only — there is no form slug on this path.

    Deliberately NOT wired into the bulk importer
    (`events.batch_requests.import_requests_from_excel_bytes`). Torch's Binny /
    Total Wine loads come through that path in the hundreds, and dumping them
    into the client's workbook is exactly the flood the ids-only backfill was
    written to prevent. One person filling in one form is the thing this is
    for.
    """
    if not is_torch_tenant(getattr(request, "tenant", None)):
        return False
    return _append_row(request)
