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

        _ensure_header(svc, sheet_id)

        # Build the row. Use empty strings (not None) so the Sheets API
        # doesn't choke on type variance. Strings are forgiving across
        # the API surface.
        def s(v):
            if v is None:
                return ""
            return str(v)

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

        row = [
            s(request.uuid),
            status_name,
            s(getattr(request, "date", "")),
            s(getattr(tenant, "name", "")),
            request_type_name,
            retailer_name,
            distributor_name,
            s(getattr(request, "address", "")),
            state_code,
            s(getattr(request, "start_time", "")),
            s(getattr(request, "end_time", "")),
            rmm_label,
            s(getattr(request, "created_at", "")),
            s(getattr(request, "updated_at", "")),
            spark_link,
        ]

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


def upsert_many(requests: Iterable) -> int:
    """Bulk variant for the backfill / cron path. Returns count synced."""
    n = 0
    for r in requests:
        if upsert_request_row(r):
            n += 1
    return n
