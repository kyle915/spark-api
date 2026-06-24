"""Daily Spark → Google Sheet export of a tenant's recap data ("demo info").

Writes one row per CustomRecap into a tenant's `recap_export_sheet_url`: a
fixed block of event/BA metadata, then one column per custom-template field
(every section — including the demographic breakdowns), with multiselect
answers rendered as comma lists. It's a FULL REFRESH each run — the target
worksheet is cleared and rewritten — so the sheet always mirrors current data
(edits and deletions included), which is what a "kept current daily" export
wants.

Reuses the ADC-based Sheets client + helpers from ``utils.sheets_mirror`` and
the recap field extractors from ``recaps.pdf``. The Cloud Run runtime service
account — ``spark-api-new-sa@spark-479222.iam.gserviceaccount.com`` — must have
Editor access on the target sheet (same account the Master-Tracker mirror
uses).

Failures are reported in the returned result dict but never raise to the
caller — a Sheets miss must not break anything upstream.
"""
from __future__ import annotations

import logging

from googleapiclient.errors import HttpError

from recaps.models import CustomField, CustomRecap
from recaps.pdf import (
    _event_date,
    _event_retailer,
    _event_state,
    _format_field_value,
)
from utils.sheets_mirror import _col_letter, _service, extract_sheet_id

logger = logging.getLogger(__name__)

# Worksheet (tab) the export writes into. Kept separate from any other tabs
# the client may keep in the same spreadsheet — we only clear/rewrite this one.
DEFAULT_TAB = "Recap Data"

# The runtime service account that needs Editor access on the target sheet —
# surfaced in the 403 error detail so a failed run tells you exactly how to fix
# it without digging through GCP.
SERVICE_ACCOUNT_EMAIL = "spark-api-new-sa@spark-479222.iam.gserviceaccount.com"

# Fixed leading (recap-level) columns, in order.
META_HEADER = [
    "Recap UUID",
    "Status",
    "Submitted At",
    "Event Date",
    "BA",
    "Retailer",
    "State",
    "Event",
    "Total Engagements",
]

# Field-type name tokens we skip as data columns — image/photo fields hold a
# blob path, not demo data.
_IMAGE_TYPE_TOKENS = ("image", "photo", "img")


def _is_image_field(custom_field) -> bool:
    ft = getattr(custom_field, "custom_field_type", None)
    name = (getattr(ft, "name", None) or "").lower()
    return any(tok in name for tok in _IMAGE_TYPE_TOKENS)


def _fmt_date(value) -> str:
    if not value:
        return ""
    try:
        return value.strftime("%Y-%m-%d")
    except Exception:
        return str(value)


def _fmt_dt(value) -> str:
    if not value:
        return ""
    try:
        return value.strftime("%Y-%m-%d %H:%M")
    except Exception:
        return str(value)


def _name_of(obj) -> str:
    """Render a Retailer/State (or a plain free-text name, or None) as text."""
    if obj is None:
        return ""
    name = getattr(obj, "name", None)
    if name:
        return str(name)
    return str(obj)


def _ba_name(recap) -> str:
    """Best display name for the BA who filed the recap.

    Prefers the linked Ambassador's user name/email, then the Ambassador's own
    name, then the free-text external_ba_name (sub-contractors with no account).
    """
    amb = getattr(recap, "ambassador", None)
    if amb is not None:
        user = getattr(amb, "user", None)
        if user is not None:
            full = " ".join(
                p
                for p in [
                    getattr(user, "first_name", "") or "",
                    getattr(user, "last_name", "") or "",
                ]
                if p
            ).strip()
            if full:
                return full
            email = getattr(user, "email", None)
            if email:
                return str(email)
        name = getattr(amb, "name", None)
        if name:
            return str(name)
    return (getattr(recap, "external_ba_name", None) or "").strip()


def _event_name(recap) -> str:
    event = getattr(recap, "event", None)
    return str(getattr(event, "name", "") or "") if event else ""


def _ordered_fields(tenant) -> list:
    """Every custom field across the tenant's recap templates, in display
    order (template → section.order → field.order, ties by id), excluding
    image/photo fields. Columns are keyed by field id, so recaps from any of
    the tenant's templates align to the same column set.
    """
    qs = (
        CustomField.objects.filter(custom_recap_template__tenant=tenant)
        .select_related("recap_section", "custom_field_type", "custom_recap_template")
        .order_by(
            "custom_recap_template__id",
            "recap_section__order",
            "recap_section__id",
            "order",
            "id",
        )
    )
    return [f for f in qs if not _is_image_field(f)]


def build_export_grid(tenant) -> tuple[list[str], list[list]]:
    """Build (header_row, data_rows) for a tenant's recaps.

    Reads the DB but performs no Sheets I/O — this is the unit-testable core.
    One row per CustomRecap, newest first; metadata columns then one column per
    custom field. Multiselect values are rendered as comma lists.
    """
    fields = _ordered_fields(tenant)
    field_ids = [f.id for f in fields]
    header = list(META_HEADER) + [f.name for f in fields]

    recaps = (
        CustomRecap.objects.filter(tenant=tenant)
        .select_related("event", "ambassador")
        .prefetch_related("custom_field_value__custom_field")
        .order_by("-submitted_at", "-id")
    )

    rows: list[list] = []
    for recap in recaps:
        values_by_field: dict[int, str] = {}
        for cfv in recap.custom_field_value.all():
            cf = getattr(cfv, "custom_field", None)
            if cf is None:
                continue
            values_by_field[cf.id] = _format_field_value(cfv.value)

        meta = [
            str(recap.uuid),
            "Approved" if getattr(recap, "approved", False) else "Submitted",
            _fmt_dt(getattr(recap, "submitted_at", None)),
            _fmt_date(_event_date(recap)),
            _ba_name(recap),
            _name_of(_event_retailer(recap)),
            _name_of(_event_state(recap)),
            _event_name(recap),
            recap.total_engagements if recap.total_engagements is not None else "",
        ]
        rows.append(meta + [values_by_field.get(fid, "") for fid in field_ids])

    return header, rows


def _ensure_tab(svc, sheet_id: str, tab: str) -> None:
    """Make sure a worksheet titled `tab` exists; create it if missing."""
    meta = (
        svc.spreadsheets()
        .get(spreadsheetId=sheet_id, fields="sheets.properties.title")
        .execute()
    )
    titles = {
        s.get("properties", {}).get("title") for s in meta.get("sheets", [])
    }
    if tab in titles:
        return
    svc.spreadsheets().batchUpdate(
        spreadsheetId=sheet_id,
        body={"requests": [{"addSheet": {"properties": {"title": tab}}}]},
    ).execute()


def write_grid_to_sheet(svc, sheet_id: str, tab: str, header: list, rows: list) -> int:
    """Clear `tab` and write header + rows. Returns the data-row count."""
    _ensure_tab(svc, sheet_id, tab)
    svc.spreadsheets().values().clear(
        spreadsheetId=sheet_id, range=f"'{tab}'!A:ZZ"
    ).execute()
    grid = [header] + rows
    end_col = _col_letter(len(header))
    svc.spreadsheets().values().update(
        spreadsheetId=sheet_id,
        range=f"'{tab}'!A1:{end_col}{len(grid)}",
        valueInputOption="RAW",
        body={"values": grid},
    ).execute()
    return len(rows)


def export_tenant_recaps_to_sheet(tenant, *, tab: str = DEFAULT_TAB, sheet_url: str | None = None) -> dict:
    """Full-refresh a tenant's recap data into their export sheet.

    Returns a result dict; never raises. On a 403 the detail names the service
    account that needs Editor access on the sheet.
    """
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
        header, rows = build_export_grid(tenant)
        n = write_grid_to_sheet(svc, sheet_id, tab, header, rows)
        return {
            "ok": True,
            "rows": n,
            "columns": len(header),
            "sheet_id": sheet_id,
            "tab": tab,
        }
    except HttpError as e:
        status = getattr(getattr(e, "resp", None), "status", None)
        if str(status) == "403":
            detail = (
                f"Sheets API 403 — share the sheet with {SERVICE_ACCOUNT_EMAIL} "
                f"(Editor) and confirm the Sheets API is enabled."
            )
        else:
            detail = " ".join(str(e).split())[:400]
        logger.warning("recap_sheet_export: write failed (status=%s): %s", status, e)
        return {"ok": False, "error": "sheets-api", "status": status, "detail": detail}
    except Exception as e:  # pragma: no cover - defensive
        logger.exception("recap_sheet_export: unexpected failure")
        return {"ok": False, "error": "unexpected", "detail": " ".join(str(e).split())[:400]}
