"""Mirror clock sessions into the link's Google Sheet.

One row per session, keyed by the Session ID column. Clock-in appends the
row; clock-out rewrites that same row. The DB stays the source of truth: a
failed write leaves ``sheet_dirty`` set and the ``clock-sheet-sync`` cron
retries it, so a Sheets outage never blocks or loses a clock action.

Auth and HTTP come from ``utils.sheets_mirror`` (same service account as the
master-tracker and Torch sheets). The account needs Editor on pasted sheets;
sheets it creates itself are shared out to staff as Editor.
"""

from __future__ import annotations

import functools
import logging
import re
from datetime import datetime
from zoneinfo import ZoneInfo

import google.auth.transport.requests
import httplib2
from django.conf import settings
from django.db import connection, transaction
from django.utils import timezone as dj_tz
from google_auth_httplib2 import AuthorizedHttp
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from timeclock.models import ClockLink, ClockSession
from utils.sheets_mirror import (
    _col_letter,
    _credentials,
    _execute_sheets,
    _service,
    extract_sheet_id,
)
from utils.torch_public_form_sheet import SERVICE_ACCOUNT_EMAIL as FALLBACK_SA_EMAIL

logger = logging.getLogger(__name__)

TAB = "Clock log"

HEADER = [
    "Link / Client",
    "First Name",
    "Last Name",
    "Email",
    "Market",
    "Address",
    "Clock In",
    "Timezone",
    "Clock In Lat",
    "Clock In Lng",
    "Clock In Accuracy (m)",
    "Clock In Map",
    "Clock Out",
    "Clock Out Lat",
    "Clock Out Lng",
    "Clock Out Accuracy (m)",
    "Clock Out Map",
    "Duration (h:mm)",
    "Hours",
    "Status",
    "Session ID",
]
SESSION_COL = _col_letter(len(HEADER))
LAST_COL = SESSION_COL

DRIVE_SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive.file",
]

_GPS_LABELS = {
    ClockSession.GPS_DENIED: "GPS denied",
    ClockSession.GPS_UNAVAILABLE: "GPS unavailable",
    ClockSession.GPS_TIMEOUT: "GPS timed out",
    ClockSession.GPS_UNSUPPORTED: "GPS not supported",
}
_UPDATED_ROW_RE = re.compile(r"![A-Z]+(\d+)")
# First key of the two-int advisory lock, so session ids can't collide with
# any other advisory lock in the database.
_LOCK_NAMESPACE = 7_411_001


class SheetSetupError(Exception):
    pass


@functools.lru_cache(maxsize=1)
def service_account_email() -> str:
    """The account staff must share a pasted Sheet with (Editor)."""
    info = getattr(settings, "GOOGLE_CALENDAR_CREDENTIALS", None) or getattr(
        settings, "GS_CREDENTIALS", None
    )
    if isinstance(info, dict) and info.get("client_email"):
        return info["client_email"]
    creds = _credentials()
    email = getattr(creds, "service_account_email", None)
    if creds is not None and (not email or email == "default"):
        try:
            creds.refresh(google.auth.transport.requests.Request())
            email = getattr(creds, "service_account_email", None)
        except Exception:  # noqa: BLE001 — fall back to the known runtime account
            email = None
    if email and email != "default":
        return email
    return FALLBACK_SA_EMAIL


# --------------------------------------------------------------------------
# Row building
# --------------------------------------------------------------------------
def _text(value) -> str:
    """Literal text under USER_ENTERED, so typed input can never be a formula."""
    value = "" if value is None else str(value)
    return f"'{value}" if value else ""


def _local(dt: datetime | None, tz_name: str) -> str:
    if dt is None:
        return ""
    loc = dt.astimezone(ZoneInfo(tz_name))
    return loc.strftime("%m/%d/%Y %I:%M:%S %p").lstrip("0")


def _tz_label(session: ClockSession) -> str:
    abbr = dj_tz.localtime(session.clock_in_at, ZoneInfo(session.timezone)).strftime("%Z")
    return _text(f"{abbr} ({session.timezone})")


def maps_url(lat: float | None, lng: float | None) -> str:
    if lat is None or lng is None:
        return ""
    return f"https://www.google.com/maps?q={lat},{lng}"


def _num(value) -> str:
    return "" if value is None else str(value)


def _map_cell(lat, lng, gps_status: str) -> str:
    url = maps_url(lat, lng)
    if url:
        return url
    return _text(_GPS_LABELS.get(gps_status, ""))


def format_duration(minutes: int | None) -> str:
    if minutes is None:
        return ""
    return f"{minutes // 60}:{minutes % 60:02d}"


def status_label(session: ClockSession) -> str:
    return "Clocked out" if session.clock_out_at else "Clocked in"


def build_row(session: ClockSession) -> list[str]:
    link = session.link
    who = link.label
    if link.tenant_id:
        who = f"{link.tenant.name} · {link.label}"
    minutes = session.duration_minutes
    out = session.clock_out_at is not None
    return [
        _text(who),
        _text(session.first_name),
        _text(session.last_name),
        _text(session.email),
        _text(session.market),
        _text(session.address),
        _local(session.clock_in_at, session.timezone),
        _tz_label(session),
        _num(session.clock_in_lat),
        _num(session.clock_in_lng),
        _num(session.clock_in_accuracy_m),
        _map_cell(session.clock_in_lat, session.clock_in_lng, session.clock_in_gps),
        _local(session.clock_out_at, session.timezone),
        _num(session.clock_out_lat),
        _num(session.clock_out_lng),
        _num(session.clock_out_accuracy_m),
        _map_cell(session.clock_out_lat, session.clock_out_lng, session.clock_out_gps) if out else "",
        _text(format_duration(minutes)),
        f"{minutes / 60:.2f}" if minutes is not None else "",
        status_label(session),
        str(session.uuid),
    ]


# --------------------------------------------------------------------------
# Sheet plumbing
# --------------------------------------------------------------------------
def _range(a1: str) -> str:
    return f"'{TAB}'!{a1}"


def _tab_gid(svc, sheet_id: str) -> int | None:
    meta = _execute_sheets(
        svc.spreadsheets().get(spreadsheetId=sheet_id, fields="sheets.properties"),
        op="clock tab lookup",
    )
    for sheet in meta.get("sheets") or []:
        props = sheet.get("properties") or {}
        if props.get("title") == TAB:
            return props.get("sheetId")
    return None


def ensure_tab(svc, sheet_id: str) -> int:
    """Make sure the ``Clock log`` tab exists with our header. Returns its gid.

    Pasted sheets keep whatever else they hold; we only ever touch our tab.
    """
    gid = _tab_gid(svc, sheet_id)
    if gid is None:
        resp = _execute_sheets(
            svc.spreadsheets().batchUpdate(
                spreadsheetId=sheet_id,
                body={
                    "requests": [
                        {
                            "addSheet": {
                                "properties": {
                                    "title": TAB,
                                    "gridProperties": {"frozenRowCount": 1},
                                }
                            }
                        }
                    ]
                },
            ),
            op="clock add tab",
        )
        gid = resp["replies"][0]["addSheet"]["properties"]["sheetId"]
    current = _execute_sheets(
        svc.spreadsheets().values().get(spreadsheetId=sheet_id, range=_range(f"A1:{LAST_COL}1")),
        op="clock header read",
    )
    if (current.get("values") or [[]])[0] != HEADER:
        _execute_sheets(
            svc.spreadsheets().values().update(
                spreadsheetId=sheet_id,
                range=_range(f"A1:{LAST_COL}1"),
                valueInputOption="RAW",
                body={"values": [HEADER]},
            ),
            op="clock header write",
        )
    return gid


def _row_holds(svc, sheet_id: str, row: int, session_uuid: str) -> bool:
    got = _execute_sheets(
        svc.spreadsheets().values().get(
            spreadsheetId=sheet_id, range=_range(f"{SESSION_COL}{row}")
        ),
        op="clock row check",
    )
    values = got.get("values") or []
    return bool(values and values[0] and str(values[0][0]).strip() == session_uuid)


def find_row(svc, sheet_id: str, session: ClockSession) -> int | None:
    session_uuid = str(session.uuid)
    if session.sheet_row and _row_holds(svc, sheet_id, session.sheet_row, session_uuid):
        return session.sheet_row
    got = _execute_sheets(
        svc.spreadsheets().values().get(
            spreadsheetId=sheet_id, range=_range(f"{SESSION_COL}2:{SESSION_COL}")
        ),
        op="clock row scan",
    )
    for i, row in enumerate(got.get("values") or [], start=2):
        if row and str(row[0]).strip() == session_uuid:
            return i
    return None


def _write(svc, sheet_id: str, session: ClockSession) -> int:
    ensure_tab(svc, sheet_id)
    row_values = build_row(session)
    row = find_row(svc, sheet_id, session)
    if row is not None:
        _execute_sheets(
            svc.spreadsheets().values().update(
                spreadsheetId=sheet_id,
                range=_range(f"A{row}:{LAST_COL}{row}"),
                valueInputOption="USER_ENTERED",
                body={"values": [row_values]},
            ),
            op="clock row update",
        )
        return row
    resp = _execute_sheets(
        svc.spreadsheets().values().append(
            spreadsheetId=sheet_id,
            range=_range("A:A"),
            valueInputOption="USER_ENTERED",
            insertDataOption="INSERT_ROWS",
            body={"values": [row_values]},
        ),
        op="clock row append",
    )
    updated = ((resp or {}).get("updates") or {}).get("updatedRange") or ""
    match = _UPDATED_ROW_RE.search(updated)
    return int(match.group(1)) if match else 0


def sync_session(session_id: int) -> bool:
    """Write one session's row. True when the sheet now matches the DB.

    Serialized per session with an advisory lock (not a row lock, which would
    make a clock-out wait on Google) so a clock-in sync and a quick clock-out
    sync can't both append; whichever runs second finds the row the first one
    wrote. ``sheet_dirty`` only clears if nothing changed mid-write.
    """
    with transaction.atomic():
        with connection.cursor() as cursor:
            cursor.execute("SELECT pg_advisory_xact_lock(%s, %s)", [_LOCK_NAMESPACE, session_id])
        session = (
            ClockSession.objects.select_related("link__tenant")
            .filter(pk=session_id)
            .first()
        )
        if session is None:
            return False
        sheet_id = session.link.sheet_id
        if not sheet_id:
            ClockSession.objects.filter(pk=session.pk).update(sheet_dirty=False)
            return False
        svc = _service()
        if svc is None:
            _record_error(session, "No Google credentials on the server.")
            return False
        try:
            row = _write(svc, sheet_id, session)
        except Exception as exc:  # noqa: BLE001 — recorded; the sweep retries
            logger.warning("clock sheet sync failed session=%s", session.pk, exc_info=True)
            _record_error(session, _describe(exc))
            return False
        ClockSession.objects.filter(pk=session.pk, updated_at=session.updated_at).update(
            sheet_dirty=False, sheet_synced_at=dj_tz.now(), sheet_error="", sheet_row=row or None
        )
        return True


def _record_error(session: ClockSession, message: str) -> None:
    ClockSession.objects.filter(pk=session.pk).update(sheet_error=message[:500])


def _describe(exc: Exception) -> str:
    if isinstance(exc, HttpError):
        status = getattr(getattr(exc, "resp", None), "status", None)
        if status in (403, 404):
            return (
                f"Google said {status}: share the Sheet with "
                f"{service_account_email()} as Editor."
            )
        return f"Google said {status}."
    return type(exc).__name__


def sync_dirty(*, link: ClockLink | None = None, limit: int = 200) -> dict:
    qs = ClockSession.objects.filter(sheet_dirty=True).exclude(link__sheet_id="")
    if link is not None:
        qs = qs.filter(link=link)
    ids = list(qs.order_by("updated_at").values_list("id", flat=True)[:limit])
    synced = sum(1 for sid in ids if sync_session(sid))
    return {"attempted": len(ids), "synced": synced, "failed": len(ids) - synced}


def delete_row(session: ClockSession) -> bool:
    """Remove this session's row. False when there was no row to remove."""
    sheet_id = session.link.sheet_id
    if not sheet_id:
        return False
    svc = _service()
    if svc is None:
        raise SheetSetupError("No Google credentials on the server.")
    gid = _tab_gid(svc, sheet_id)
    if gid is None:
        return False
    row = find_row(svc, sheet_id, session)
    if row is None:
        return False
    _execute_sheets(
        svc.spreadsheets().batchUpdate(
            spreadsheetId=sheet_id,
            body={
                "requests": [
                    {
                        "deleteDimension": {
                            "range": {
                                "sheetId": gid,
                                "dimension": "ROWS",
                                "startIndex": row - 1,
                                "endIndex": row,
                            }
                        }
                    }
                ]
            },
        ),
        op="clock row delete",
    )
    return True


# --------------------------------------------------------------------------
# Linking a sheet to a link
# --------------------------------------------------------------------------
def _drive_service():
    creds = _credentials(scopes=DRIVE_SCOPES)
    if creds is None:
        raise SheetSetupError("No Google credentials on the server.")
    http = AuthorizedHttp(creds, http=httplib2.Http(timeout=60))
    return build("drive", "v3", http=http, cache_discovery=False)


def attach_existing_sheet(link: ClockLink, url: str) -> None:
    sheet_id = extract_sheet_id(url or "")
    if not sheet_id:
        raise SheetSetupError("That doesn't look like a Google Sheets link.")
    svc = _service()
    if svc is None:
        raise SheetSetupError("No Google credentials on the server.")
    try:
        ensure_tab(svc, sheet_id)
    except HttpError as exc:
        status = getattr(getattr(exc, "resp", None), "status", None)
        if status in (403, 404):
            raise SheetSetupError(
                f"Spark can't open that Sheet. Share it with {service_account_email()} "
                "as Editor, then try again."
            ) from exc
        raise SheetSetupError(f"Google said {status} when opening that Sheet.") from exc
    link.sheet_id = sheet_id
    link.save(update_fields=["sheet_id", "updated_at"])


def create_sheet(link: ClockLink, share_with: list[str]) -> str:
    """Create ``Spark Clock — <label>`` and share it (Editor) with ``share_with``."""
    svc = _service()
    if svc is None:
        raise SheetSetupError("No Google credentials on the server.")
    title = f"Spark Clock — {link.label}"
    try:
        created = _execute_sheets(
            svc.spreadsheets().create(
                body={
                    "properties": {"title": title},
                    "sheets": [
                        {"properties": {"title": TAB, "gridProperties": {"frozenRowCount": 1}}}
                    ],
                },
                fields="spreadsheetId",
            ),
            op="clock sheet create",
        )
    except HttpError as exc:
        status = getattr(getattr(exc, "resp", None), "status", None)
        raise SheetSetupError(
            f"Google wouldn't let Spark create a Sheet ({status}). Create a blank Sheet, "
            f"share it with {service_account_email()} as Editor, and paste its link instead."
        ) from exc
    sheet_id = created["spreadsheetId"]
    link.sheet_id = sheet_id
    link.save(update_fields=["sheet_id", "updated_at"])
    ensure_tab(svc, sheet_id)
    drive = _drive_service()
    for email in share_with:
        _execute_sheets(
            drive.permissions().create(
                fileId=sheet_id,
                body={"type": "user", "role": "writer", "emailAddress": email},
                sendNotificationEmail=True,
                emailMessage=f"Spark clock in/out log for “{link.label}”.",
            ),
            op="clock sheet share",
        )
    return sheet_id
