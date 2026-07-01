"""
One-off: widen the two ANUAL/TOTAL formulas on the LD RMM KPI workbook's
scorecard tabs so they include the "Others" column.

Each scorecard tab (Northeast / Florida / South / Central / West / Poli /
Pat) logs events with per-SKU can counts in columns J..AJ (samples, AJ =
"Others") and AL..BL (sales, BL = "Others"). The per-row totals (I / AK,
BYROW over J:AJ / AL:BL) and the monthly SUMPRODUCTs ($J$19:$AJ) already
include Others — but the two annual TOTAL cells were written before the
Others columns existed and stop one column short:

    Total Cans Sampled   =SUM(J19:AI1008)   → should end at AJ
    Total Sales          =SUM(AL19:BK1008)  → should end at BL

So each tab's annual total disagrees with its own monthly breakdown by
exactly the cans/sales logged under Others. This command rewrites the two
cells per tab (preserving each tab's own start/end rows), reporting the
delta each fix adds. Rows are located by their column-A label (not
hardcoded), and a cell whose formula doesn't match the expected broken
pattern is reported and left untouched — so re-running is a no-op.

Usage:
    python manage.py fix_ld_kpi_totals            # dry-run, per-tab report
    python manage.py fix_ld_kpi_totals --apply    # write the fixed formulas
"""
from __future__ import annotations

import re

from django.core.management.base import BaseCommand, CommandError

from utils.sheets_mirror import _service, extract_sheet_id

WORKBOOK_URL = (
    "https://docs.google.com/spreadsheets/d/"
    "1W4F7X_vdW7d0SmthUvdxujBH2CahG0DaB53xBVr5q04/edit"
)
DEFAULT_TABS = "Northeast,Florida,South,Central,West,Poli,Pat"

# (row label in column A, broken-range regex, fixed end column)
# The regex captures the start cell and end ROW so each tab keeps its own
# grid bounds; only the end COLUMN is corrected.
FIXES = [
    (
        "Total Cans Sampled",
        re.compile(r"^=SUM\((J\d+):AI(\d+)\)$", re.IGNORECASE),
        "AJ",
        "AJ",  # delta column: what the fix adds
    ),
    (
        "Total Sales",
        re.compile(r"^=SUM\((AL\d+):BK(\d+)\)$", re.IGNORECASE),
        "BL",
        "BL",
    ),
]


class Command(BaseCommand):
    help = (
        "Widen the LD KPI scorecard tabs' annual Total Cans Sampled / Total "
        "Sales formulas to include the Others column. Dry-run by default."
    )

    def add_arguments(self, parser):
        parser.add_argument("--sheet-url", type=str, default=WORKBOOK_URL)
        parser.add_argument(
            "--tabs",
            type=str,
            default=DEFAULT_TABS,
            help="Comma-separated scorecard tab names to fix.",
        )
        parser.add_argument(
            "--apply",
            action="store_true",
            help="Actually write. Without this, print what WOULD change.",
        )

    def handle(self, *args, **opts):
        sheet_id = extract_sheet_id(opts["sheet_url"])
        if not sheet_id:
            raise CommandError("Could not parse a sheet id from --sheet-url.")
        svc = _service()
        if svc is None:
            raise CommandError("No Sheets credentials (ADC).")
        tabs = [t.strip() for t in (opts["tabs"] or "").split(",") if t.strip()]
        if not tabs:
            raise CommandError("No tabs to process.")
        apply = opts["apply"]
        if not apply:
            self.stdout.write(self.style.WARNING(
                "DRY RUN — pass --apply to write the fixed formulas.\n"
            ))

        writes: list[dict] = []
        for tab in tabs:
            self.stdout.write(self.style.MIGRATE_HEADING(f"[{tab}]"))
            # One read: labels + current annual-total formulas for rows 1-20.
            try:
                resp = (
                    svc.spreadsheets().values()
                    .get(spreadsheetId=sheet_id, range=f"'{tab}'!A1:C20",
                         valueRenderOption="FORMULA")
                    .execute()
                )
            except Exception as e:
                self.stdout.write(self.style.ERROR(f"  read failed: {e}"))
                continue
            rows = resp.get("values") or []

            for label, broken_re, end_col, delta_col in FIXES:
                row_idx = next(
                    (i for i, r in enumerate(rows, start=1)
                     if r and str(r[0]).strip() == label),
                    None,
                )
                if row_idx is None:
                    self.stdout.write(f"  - {label}: row not found (skip)")
                    continue
                row = rows[row_idx - 1]
                current = str(row[2]).strip() if len(row) > 2 else ""
                m = broken_re.match(current)
                if not m:
                    self.stdout.write(
                        f"  - {label} (C{row_idx}): formula is {current!r} — "
                        "not the known broken pattern (skip)"
                    )
                    continue
                start_cell, end_row = m.group(1), m.group(2)
                fixed = f"=SUM({start_cell}:{end_col}{end_row})"

                # The delta this fix adds = the Others column's own sum over
                # the same row span. Reported so the change can be eyeballed
                # against the tab before/after.
                delta = None
                try:
                    start_row = re.sub(r"[A-Z]+", "", start_cell, flags=re.IGNORECASE)
                    dresp = (
                        svc.spreadsheets().values()
                        .get(spreadsheetId=sheet_id,
                             range=f"'{tab}'!{delta_col}{start_row}:{delta_col}{end_row}")
                        .execute()
                    )
                    delta = 0
                    for r in dresp.get("values") or []:
                        raw = (r[0] if r else "") or ""
                        raw = str(raw).replace(",", "").strip()
                        try:
                            delta += float(raw)
                        except ValueError:
                            pass
                except Exception:
                    pass

                self.stdout.write(
                    f"  + {label} (C{row_idx}): {current}  →  {fixed}"
                    + (f"   (adds {delta:,.0f} from {delta_col})" if delta is not None else "")
                )
                writes.append({
                    "range": f"'{tab}'!C{row_idx}",
                    "values": [[fixed]],
                })

        if not writes:
            self.stdout.write("\nNothing to fix.")
            return
        if not apply:
            self.stdout.write(f"\nWould update {len(writes)} cell(s).")
            return
        svc.spreadsheets().values().batchUpdate(
            spreadsheetId=sheet_id,
            body={"valueInputOption": "USER_ENTERED", "data": writes},
        ).execute()
        self.stdout.write(self.style.SUCCESS(f"\nUpdated {len(writes)} cell(s)."))
