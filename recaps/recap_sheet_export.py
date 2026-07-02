"""Daily Spark → Google Sheet export of a tenant's recap data ("demo info").

Designed to feed an EXISTING client sheet without disturbing its layout. The
Girl Beer sheet has a formatted "Summary" dashboard (KPIs + per-ambassador /
date / retailer / flavor / age breakdowns) whose formulas read from a raw
"Demo Recaps" tab. So the export:

  * targets the raw-data tab (default "Demo Recaps"),
  * reads that tab's LIVE header row and maps each recap into those exact
    columns by name (Brand Ambassador / Date / Store/Location / Retailer +
    every custom-template field) — so the column order, the Summary tab, and
    all its formulas are preserved,
  * clears only the DATA rows (row 2 down) and rewrites them each run (full
    refresh — edits/deletions stay accurate), leaving row 1 and other tabs
    untouched,
  * writes with USER_ENTERED so numbers/dates land as real numbers/dates and
    the Summary's SUM/QUERY math keeps working.

If the target tab doesn't exist yet (a fresh sheet for some other tenant), it
falls back to building its own header + grid (build_export_grid).

Reuses the ADC Sheets client from utils.sheets_mirror and recap field
extractors from recaps.pdf. The Cloud Run runtime service account
(spark-api-new-sa@spark-479222.iam.gserviceaccount.com) must have Editor
access on the target sheet. Failures are returned in the result dict, never
raised.
"""
from __future__ import annotations

import logging
import re

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

# The raw-data tab the client's Summary dashboard reads from. We refresh this
# tab's data rows in place; we never touch the Summary tab.
DEFAULT_TAB = "Demo Recaps"

SERVICE_ACCOUNT_EMAIL = "spark-api-new-sa@spark-479222.iam.gserviceaccount.com"

# Header columns that map to recap-level metadata rather than a custom field.
# Keyed by NORMALIZED header text → meta key.
_META_ALIASES = {
    "brand ambassador": "ba",
    "ambassador": "ba",
    "ba": "ba",
    "date": "date",
    "event date": "date",
    "store/location": "store",
    "store location": "store",
    "location": "store",
    "store": "store",
    "retailer": "retailer",
    "status": "status",
}

# ── Fallback (fresh-sheet) header, used only when the target tab has no
#    header row of its own. Kyle's sheet has its own header, so this isn't
#    used for Girl Beer.
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

_IMAGE_TYPE_TOKENS = ("image", "photo", "img")


def _normalize(s) -> str:
    """Loose header/field-name match key: lowercase, collapse whitespace,
    drop spaces around a slash, strip a trailing '?'/':'."""
    s = (s or "").strip().lower()
    s = s.replace(" / ", "/")
    s = re.sub(r"\s+", " ", s).strip()
    return s.rstrip("?:").strip()


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


def _fmt_mdy(value) -> str:
    """MM/DD/YYYY — matches the date format already in the Demo Recaps tab so
    the Summary's by-date grouping stays consistent."""
    if not value:
        return ""
    try:
        return value.strftime("%m/%d/%Y")
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
    if obj is None:
        return ""
    name = getattr(obj, "name", None)
    if name:
        return str(name)
    return str(obj)


def _ba_name(recap) -> str:
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


def _store_location(recap) -> str:
    """A "Store/Location" string for the row: the event location's address or
    name, falling back to the event name."""
    event = getattr(recap, "event", None)
    if event is None:
        return ""
    loc = getattr(event, "location", None)
    if loc is not None:
        addr = getattr(loc, "address", None) or getattr(loc, "name", None)
        if addr:
            return str(addr)
    return str(getattr(event, "name", "") or "")


def _retailer_inferrer(tenant):
    """Fallback Retailer matcher for rows whose recap/event/request retailer
    FKs are all unset — common for Girl Beer, where events created off the
    schedule sheet carry the store in the event NAME (e.g. "Whole Foods Los
    Angeles · 2026-06-21") but no Retailer link, leaving the export's
    Retailer column blank. Matches the tenant's known Retailer names against
    the row's store/event text — longest name first, on word boundaries — and
    returns the canonical Retailer.name. Purely presentational: nothing is
    written back to the recap or event, and an explicit FK always wins
    (see _retailer_for_row)."""
    from events.models import Retailer

    names = Retailer.objects.filter(tenant=tenant).values_list("name", flat=True)
    patterns = [
        (
            re.compile(
                rf"(?<![a-z0-9]){re.escape(_normalize(n))}(?![a-z0-9])"
            ),
            n,
        )
        for n in sorted(
            {n.strip() for n in names if n and n.strip()}, key=len, reverse=True
        )
    ]

    def infer(text: str) -> str:
        t = _normalize(text)
        if not t:
            return ""
        for pat, name in patterns:
            if pat.search(t):
                return name
        return ""

    return infer


def _retailer_for_row(recap, infer_retailer=None) -> str:
    retailer = _name_of(_event_retailer(recap))
    if not retailer and infer_retailer is not None:
        retailer = infer_retailer(f"{_store_location(recap)} {_event_name(recap)}")
    return retailer


def _recap_meta(recap, infer_retailer=None) -> dict:
    return {
        "ba": _ba_name(recap),
        "date": _fmt_mdy(_event_date(recap)),
        "store": _store_location(recap),
        "retailer": _retailer_for_row(recap, infer_retailer),
        "status": "Approved" if getattr(recap, "approved", False) else "Submitted",
    }


def _tenant_recaps(tenant):
    return (
        CustomRecap.objects.filter(tenant=tenant)
        .select_related("event", "ambassador")
        .prefetch_related("custom_field_value__custom_field")
        .order_by("submitted_at", "id")
    )


def _values_by_field_name(recap) -> dict:
    out = {}
    for cfv in recap.custom_field_value.all():
        cf = getattr(cfv, "custom_field", None)
        if cf is None:
            continue
        out[_normalize(getattr(cf, "name", ""))] = _format_field_value(cfv.value)
    return out


def rows_for_header(tenant, header: list[str]) -> list[list]:
    """Map each recap into the given header's columns (by name), so the data
    lands in the client's existing layout. Meta columns (Brand Ambassador /
    Date / Store/Location / Retailer / Status) resolve from recap metadata;
    every other column matches a custom field by name; anything unmatched
    (Note, receipt-image columns, …) is left blank.
    """
    norm_header = [_normalize(h) for h in header]

    def _date_key(recap):
        d = _event_date(recap)
        try:
            return (d.year, d.month, d.day)
        except Exception:
            return (0, 0, 0)

    # Newest recap first (by event date, then id) so the latest demos sit at
    # the top of the tab.
    recaps = sorted(
        _tenant_recaps(tenant),
        key=lambda r: (_date_key(r), getattr(r, "id", 0)),
        reverse=True,
    )
    infer_retailer = _retailer_inferrer(tenant)
    rows: list[list] = []
    for recap in recaps:
        meta = _recap_meta(recap, infer_retailer)
        by_name = _values_by_field_name(recap)
        row = []
        for nh in norm_header:
            if nh in _META_ALIASES:
                row.append(meta.get(_META_ALIASES[nh], ""))
            else:
                row.append(by_name.get(nh, ""))
        rows.append(row)
    return rows


def _ordered_fields(tenant) -> list:
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
    """Fallback builder for a fresh sheet with no header of its own: META_HEADER
    + one column per (non-image) custom field, one row per recap."""
    fields = _ordered_fields(tenant)
    field_ids = [f.id for f in fields]
    header = list(META_HEADER) + [f.name for f in fields]

    infer_retailer = _retailer_inferrer(tenant)
    rows: list[list] = []
    for recap in _tenant_recaps(tenant):
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
            _retailer_for_row(recap, infer_retailer),
            _name_of(_event_state(recap)),
            _event_name(recap),
            recap.total_engagements if recap.total_engagements is not None else "",
        ]
        rows.append(meta + [values_by_field.get(fid, "") for fid in field_ids])
    return header, rows


def _tab_titles(svc, sheet_id: str) -> list[str]:
    meta = (
        svc.spreadsheets()
        .get(spreadsheetId=sheet_id, fields="sheets.properties.title")
        .execute()
    )
    return [s.get("properties", {}).get("title") for s in meta.get("sheets", [])]


def _read_header(svc, sheet_id: str, tab: str) -> list[str]:
    resp = (
        svc.spreadsheets()
        .values()
        .get(spreadsheetId=sheet_id, range=f"'{tab}'!1:1")
        .execute()
    )
    values = resp.get("values") or [[]]
    return [str(c) for c in values[0]] if values else []


def refresh_recap_export(tenant) -> dict:
    """Dispatch a tenant's recap-data export.

    If the tenant has a `recap_export_tab_name` set, use the branded, self-built
    export into that tab (recaps.ld_recaps_export — Liquid Death). Otherwise use
    the legacy map-into-existing-tab export (the "Demo Recaps" tab whose Summary
    formulas Girl Beer relies on). Returns a result dict; never raises."""
    tab = (getattr(tenant, "recap_export_tab_name", "") or "").strip()
    if tab:
        from recaps.ld_recaps_export import write_ld_recaps

        result = write_ld_recaps(tenant, tab=tab)
    else:
        result = export_tenant_recaps_to_sheet(tenant)

    # Optionally rebuild a computed "Summary" dashboard (values, never #REF!)
    # for tenants that opt in (Girl Beer = "Summary"). Best-effort: a Summary
    # failure never affects the raw-data export result.
    summary_tab = (getattr(tenant, "recap_summary_tab_name", "") or "").strip()
    if summary_tab and result.get("ok"):
        try:
            from recaps.girlbeer_summary_export import write_girlbeer_summary

            result["summary"] = write_girlbeer_summary(tenant, tab=summary_tab)
        except Exception as e:  # pragma: no cover - defensive
            result["summary"] = {"ok": False, "error": "unexpected", "detail": str(e)[:300]}
    return result


def export_tenant_recaps_to_sheet(tenant, *, tab: str = DEFAULT_TAB, sheet_url: str | None = None) -> dict:
    """Refresh a tenant's recap data into their export sheet, preserving the
    existing layout. Returns a result dict; never raises.
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
        titles = _tab_titles(svc, sheet_id)
        matched_existing = False

        if tab in titles:
            header = _read_header(svc, sheet_id, tab)
        else:
            header = []
            svc.spreadsheets().batchUpdate(
                spreadsheetId=sheet_id,
                body={"requests": [{"addSheet": {"properties": {"title": tab}}}]},
            ).execute()

        if header:
            # Existing client layout: map into its columns, leave row 1 alone.
            matched_existing = True
            rows = rows_for_header(tenant, header)
            ncols = len(header)
            wrote_header = False
        else:
            # Fresh tab: lay down our own header + grid.
            header, rows = build_export_grid(tenant)
            ncols = len(header)
            svc.spreadsheets().values().update(
                spreadsheetId=sheet_id,
                range=f"'{tab}'!A1",
                valueInputOption="USER_ENTERED",
                body={"values": [header]},
            ).execute()
            wrote_header = True

        # Clear existing data rows (row 2 down) without touching the header
        # row or any other tab, then write the fresh rows. USER_ENTERED so
        # numbers/dates/currency parse as values the Summary formulas can sum.
        end_col = _col_letter(max(ncols, 1))
        svc.spreadsheets().values().clear(
            spreadsheetId=sheet_id, range=f"'{tab}'!A2:{end_col}"
        ).execute()
        if rows:
            svc.spreadsheets().values().update(
                spreadsheetId=sheet_id,
                range=f"'{tab}'!A2",
                valueInputOption="USER_ENTERED",
                body={"values": rows},
            ).execute()

        # Make every data row share the first row's look. New recaps that grew
        # past the client's originally-formatted range would otherwise render
        # plain; copy row 2's per-column formatting down across all data rows so
        # they all match. Best-effort — values are already written.
        if matched_existing and len(rows) > 1:
            try:
                props = (
                    svc.spreadsheets()
                    .get(spreadsheetId=sheet_id, fields="sheets.properties(title,sheetId)")
                    .execute()
                )
                gid = next(
                    (s["properties"]["sheetId"] for s in props.get("sheets", [])
                     if s.get("properties", {}).get("title") == tab),
                    None,
                )
                if gid is not None:
                    svc.spreadsheets().batchUpdate(
                        spreadsheetId=sheet_id,
                        body={"requests": [{"copyPaste": {
                            "source": {"sheetId": gid, "startRowIndex": 1, "endRowIndex": 2,
                                       "startColumnIndex": 0, "endColumnIndex": ncols},
                            "destination": {"sheetId": gid, "startRowIndex": 1,
                                            "endRowIndex": 1 + len(rows),
                                            "startColumnIndex": 0, "endColumnIndex": ncols},
                            "pasteType": "PASTE_FORMAT",
                        }}]},
                    ).execute()
            except Exception:  # pragma: no cover - formatting is best-effort
                logger.warning("recap_sheet_export: row-format copy failed (non-fatal)", exc_info=True)

        return {
            "ok": True,
            "rows": len(rows),
            "columns": ncols,
            "sheet_id": sheet_id,
            "tab": tab,
            "matched_existing_layout": matched_existing,
            "wrote_header": wrote_header,
        }
    except HttpError as e:
        status = getattr(getattr(e, "resp", None), "status", None)
        if str(status) == "403":
            detail = (
                f"Sheets API 403 — share the sheet with {SERVICE_ACCOUNT_EMAIL} "
                f"(Editor) and confirm the Sheets API is enabled."
            )
        elif str(status) == "400":
            detail = (
                f"Sheets API 400 — could not find/parse the '{tab}' tab. "
                f"Confirm the data tab is named exactly '{tab}'. ({' '.join(str(e).split())[:200]})"
            )
        else:
            detail = " ".join(str(e).split())[:400]
        logger.warning("recap_sheet_export: write failed (status=%s): %s", status, e)
        return {"ok": False, "error": "sheets-api", "status": status, "detail": detail}
    except Exception as e:  # pragma: no cover - defensive
        logger.exception("recap_sheet_export: unexpected failure")
        return {"ok": False, "error": "unexpected", "detail": " ".join(str(e).split())[:400]}
