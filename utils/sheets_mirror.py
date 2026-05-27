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


def _ensure_header(svc, sheet_id: str) -> None:
    """Read row 1; if it doesn't match HEADER, overwrite it. Lets a
    brand-new sheet bootstrap on first sync."""
    try:
        resp = (
            svc.spreadsheets()
            .values()
            .get(spreadsheetId=sheet_id, range="A1:Z1")
            .execute()
        )
        existing = (resp.get("values") or [[]])[0]
        if existing == HEADER:
            return
        svc.spreadsheets().values().update(
            spreadsheetId=sheet_id,
            range=f"A1:{_col_letter(len(HEADER))}1",
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


def _find_row_for_uuid(svc, sheet_id: str, uuid: str) -> int | None:
    """Scan column A for the UUID. Returns 1-based row index, or None."""
    try:
        resp = (
            svc.spreadsheets()
            .values()
            .get(spreadsheetId=sheet_id, range="A2:A10000")
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

        _ensure_header(svc, sheet_id)

        existing_row = _find_row_for_uuid(svc, sheet_id, str(request.uuid))
        end_col = _col_letter(len(row))

        if existing_row:
            svc.spreadsheets().values().update(
                spreadsheetId=sheet_id,
                range=f"A{existing_row}:{end_col}{existing_row}",
                valueInputOption="RAW",
                body={"values": [row]},
            ).execute()
        else:
            svc.spreadsheets().values().append(
                spreadsheetId=sheet_id,
                range=f"A2:{end_col}",
                valueInputOption="RAW",
                insertDataOption="INSERT_ROWS",
                body={"values": [row]},
            ).execute()
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


def bulk_sync_requests(requests) -> int:
    """Batched backfill for many Requests that share ONE tenant/sheet.

    The per-row upsert makes ~3 Sheets API calls (header check + UUID
    lookup + write). At hundreds of rows that blows past the Sheets quota
    (60 read & 60 write requests / min / user) and 429s. This does the
    whole tenant in a small constant number of calls instead:

        1 header ensure + 1 column-A read
        + ⌈new/500⌉ append calls + ⌈existing/500⌉ batchUpdate calls.

    Assumes every request belongs to the same tenant (the management
    command groups by tenant). Returns the number of rows written.
    """
    requests = list(requests)
    if not requests:
        return 0

    tenant = getattr(requests[0], "tenant", None)
    sheet_url = getattr(tenant, "linked_sheet_url", None) if tenant else None
    sheet_id = extract_sheet_id(sheet_url or "")
    if not sheet_id:
        return 0
    svc = _service()
    if not svc:
        return 0

    _ensure_header(svc, sheet_id)

    # One read of column A → {uuid: 1-based row index} so we can tell
    # appends from in-place updates without a per-row lookup.
    existing: dict[str, int] = {}
    try:
        resp = (
            svc.spreadsheets()
            .values()
            .get(spreadsheetId=sheet_id, range="A2:A100000")
            .execute()
        )
        for i, r in enumerate(resp.get("values") or [], start=2):
            if r and str(r[0]).strip():
                existing[str(r[0]).strip()] = i
    except HttpError as e:
        logger.warning("sheets_mirror: bulk column-A read failed: %s", e)
        return 0

    end_col = _col_letter(len(HEADER))
    appends: list[list] = []
    updates: list[dict] = []
    for req in requests:
        row = _row_for_request(req)
        if row is None:
            continue
        idx = existing.get(str(req.uuid))
        if idx:
            updates.append({"range": f"A{idx}:{end_col}{idx}", "values": [row]})
        else:
            appends.append(row)

    written = 0
    try:
        for chunk in _chunks(appends, 500):
            svc.spreadsheets().values().append(
                spreadsheetId=sheet_id,
                range=f"A2:{end_col}",
                valueInputOption="RAW",
                insertDataOption="INSERT_ROWS",
                body={"values": chunk},
            ).execute()
            written += len(chunk)
        for chunk in _chunks(updates, 500):
            svc.spreadsheets().values().batchUpdate(
                spreadsheetId=sheet_id,
                body={"valueInputOption": "RAW", "data": chunk},
            ).execute()
            written += len(chunk)
    except HttpError as e:
        logger.warning(
            "sheets_mirror: bulk write failed after %s rows: %s", written, e
        )

    return written


def upsert_many(requests: Iterable) -> int:
    """Per-row variant, kept for compatibility. Prefer bulk_sync_requests
    for backfills — it batches to respect the Sheets API quota."""
    n = 0
    for r in requests:
        if upsert_request_row(r):
            n += 1
    return n
