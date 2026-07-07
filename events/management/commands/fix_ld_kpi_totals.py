"""
Formula repairs for the LD RMM KPI workbook's scorecard tabs + MASTER.

Each scorecard tab (Northeast / Florida / South / Central / West / Poli /
Pat) logs events with per-SKU can counts in columns J..AJ (samples, AJ =
"Others") and AL..BL (sales, BL = "Others"). Repairs, each idempotent
(already-fixed cells are skipped, unknown formulas reported + left alone):

1. Annual totals one column short of Others (original 2026-07-01 fix):
       Total Cans Sampled   =SUM(J19:AI1008)   → should end at AJ
       Total Sales          =SUM(AL19:BK1008)  → should end at BL

2. MONTHLY Total Sales SUMPRODUCTs end at $BK — they miss BL ("Others"
   sales), so the monthly breakdown disagrees with the (fixed) annual
   total whenever Others sales are logged. Cols E..R on the Total Sales
   row, every scorecard tab.

3. Missing SAMPLES/SALES row-total anchors: each tab's I19 / AK19 holds a
   BYROW spill formula that auto-sums the SKU columns per row, parked in
   a dedicated dummy row labeled "FORMULA ROW" so data-row cleanups can't
   delete it. Poli's tab lost the anchors twice — first to the
   build-poli-tab data clear (A19:BM1011), then to a row deletion that
   removed the whole formula row. Restores the anchors, re-inserting a
   labeled FORMULA ROW at 19 when the current row 19 is real data, and
   clearing any literal values below that would block the spill
   (reported first).

4. MASTER YTD cells E16:I16 (Hearse / CRM / Total Sales / Events
   Supported / Seedings) sum only 11 monthly-total cells — the December
   term (J171..J175) is missing. C16/D16 already have all 12.

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

# Monthly Total Sales cells (cols E..R on the "Total Sales" row) sum
# $AL$19:$BK — one column short of BL ("Others" sales). The live sheet
# uses OPEN-ENDED ranges (no end row: "$AL$19:$BK"), but a bounded end
# row is tolerated too. Only this exact range is rewritten; anything
# else in the formula is preserved.
MONTHLY_SALES_COLS = "EFGHIJKLMNOPQR"
MONTHLY_SALES_BROKEN = re.compile(r"(\$AL\$\d+:\$?)BK(?![A-Z])(\d*)")

# Row-total spill anchors on each scorecard tab's first data row. The
# formulas are copied verbatim from the intact tabs (Pat/Northeast/...).
FORMULA_ROW = 19
ANCHORS = [
    ("I", "SAMPLES", "=BYROW(J19:AJ, LAMBDA(row, IF(COUNTA(row)=0, 0, SUM(row))))"),
    ("AK", "SALES", "=BYROW(AL19:BL, LAMBDA(row, IF(COUNTA(row)=0, 0, SUM(row))))"),
]

# MASTER YTD cells whose =SUM(J..+J..) chain stops at November: the
# December monthly-total term to append. C16/D16 already include all 12.
MASTER_YTD_MISSING = {
    "E16": "J171",  # Hearse Appearances
    "F16": "J172",  # CRM Contacts Collected
    "G16": "J173",  # Total Sales
    "H16": "J174",  # Events Supported
    "I16": "J175",  # Seedings
}


class Command(BaseCommand):
    help = (
        "Repair LD KPI workbook formulas: annual + monthly totals missing "
        "the Others column, missing SAMPLES/SALES BYROW anchors, and MASTER "
        "YTD cells missing December. Dry-run by default."
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
            "--master-tab",
            type=str,
            default="MASTER",
            help="MASTER rollup tab for the YTD-December repair ('' skips it).",
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
        clears: list[str] = []
        row_inserts: list[str] = []  # tabs needing a fresh FORMULA ROW at 19
        for tab in tabs:
            self.stdout.write(self.style.MIGRATE_HEADING(f"[{tab}]"))
            # One read: labels + annual (C) and monthly (E..R) formulas for
            # rows 1-20.
            try:
                resp = (
                    svc.spreadsheets().values()
                    .get(spreadsheetId=sheet_id, range=f"'{tab}'!A1:R20",
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

            # ---- Repair 2: monthly Total Sales $AL:$BK → $AL:$BL --------
            sales_idx = next(
                (i for i, r in enumerate(rows, start=1)
                 if r and str(r[0]).strip() == "Total Sales"),
                None,
            )
            if sales_idx is not None:
                row = rows[sales_idx - 1]
                n_fixed = 0
                for col in MONTHLY_SALES_COLS:
                    ci = ord(col) - ord("A")  # E..R are single letters
                    current = str(row[ci]).strip() if len(row) > ci else ""
                    if not current.startswith("="):
                        continue
                    fixed, n = MONTHLY_SALES_BROKEN.subn(r"\g<1>BL\g<2>", current)
                    if n:
                        writes.append({
                            "range": f"'{tab}'!{col}{sales_idx}",
                            "values": [[fixed]],
                        })
                        n_fixed += 1
                if n_fixed:
                    self.stdout.write(
                        f"  + Total Sales monthly ({sales_idx}): widened "
                        f"{n_fixed} cell(s) $BK → $BL (Others sales)"
                    )
                else:
                    self.stdout.write(
                        "  - Total Sales monthly: no $AL:$BK ranges found "
                        "(already $BL or unknown shape — skip)"
                    )

            # ---- Repair 3: restore SAMPLES/SALES BYROW anchors ----------
            # On intact tabs the anchors live in a dedicated dummy row 19
            # labeled "FORMULA ROW" (column C) so that deleting data rows
            # can't take the formulas with it. If the anchors are gone AND
            # row 19 is a real data row (the FORMULA ROW itself was
            # deleted), a fresh row is inserted at 19 first — otherwise the
            # next data-row cleanup wipes the formulas again.
            try:
                aresp = (
                    svc.spreadsheets().values()
                    .batchGet(
                        spreadsheetId=sheet_id,
                        ranges=[f"'{tab}'!A{FORMULA_ROW}:C{FORMULA_ROW}"]
                        + [
                            f"'{tab}'!{col}{FORMULA_ROW}:{col}"
                            for col, _, _ in ANCHORS
                        ],
                        valueRenderOption="FORMULA",
                    )
                    .execute()
                )
                aranges = aresp.get("valueRanges") or []
            except Exception as e:
                self.stdout.write(self.style.ERROR(f"  anchor read failed: {e}"))
                aranges = []
            marker_row = (aranges[0].get("values") or [[]])[0] if aranges else []
            has_marker = (
                len(marker_row) > 2
                and str(marker_row[2]).strip().upper() == "FORMULA ROW"
            )
            needs_insert = False
            for (col, label, anchor), vr in zip(ANCHORS, aranges[1:]):
                col_rows = vr.get("values") or []
                head = str(col_rows[0][0]).strip() if col_rows and col_rows[0] else ""
                if head.upper().startswith("=BYROW"):
                    self.stdout.write(f"  - {label} anchor ({col}{FORMULA_ROW}): present (skip)")
                    continue
                if not has_marker:
                    needs_insert = True
                # Literal values below the anchor block the spill — clear
                # them (values only; the SKU columns are the source of truth).
                blockers = [
                    (FORMULA_ROW + i, r[0])
                    for i, r in enumerate(col_rows[1:], start=1)
                    if r and str(r[0]).strip() != ""
                ]
                for rownum, val in blockers[:20]:
                    self.stdout.write(
                        f"      clearing literal {col}{rownum} = {val!r} "
                        "(will be recomputed from SKU columns)"
                    )
                if blockers:
                    clears.append(f"'{tab}'!{col}{FORMULA_ROW + 1}:{col}")
                self.stdout.write(
                    f"  + {label} anchor ({col}{FORMULA_ROW}): missing "
                    f"(was {head!r}) → restore BYROW"
                )
                writes.append({
                    "range": f"'{tab}'!{col}{FORMULA_ROW}",
                    "values": [[anchor]],
                })
            if needs_insert:
                self.stdout.write(
                    f"  + row {FORMULA_ROW} is a DATA row (the FORMULA ROW "
                    "was deleted) → insert a fresh labeled row above it"
                )
                row_inserts.append(tab)
                writes.append({
                    "range": f"'{tab}'!B{FORMULA_ROW}:C{FORMULA_ROW}",
                    "values": [[False, "FORMULA ROW"]],
                })

        # ---- Repair 4: MASTER YTD cells missing the December term -------
        master_tab = (opts.get("master_tab") or "").strip()
        if master_tab:
            self.stdout.write(self.style.MIGRATE_HEADING(f"[{master_tab}]"))
            try:
                mresp = (
                    svc.spreadsheets().values()
                    .get(spreadsheetId=sheet_id,
                         range=f"'{master_tab}'!E16:I16",
                         valueRenderOption="FORMULA")
                    .execute()
                )
                mrow = (mresp.get("values") or [[]])[0]
            except Exception as e:
                self.stdout.write(self.style.ERROR(f"  read failed: {e}"))
                mrow = []
            for i, (cell, term) in enumerate(sorted(MASTER_YTD_MISSING.items())):
                current = str(mrow[i]).strip() if len(mrow) > i else ""
                if not (current.upper().startswith("=SUM(") and current.endswith(")")):
                    self.stdout.write(
                        f"  - {cell}: formula is {current!r} — not the known "
                        "pattern (skip)"
                    )
                    continue
                if re.search(rf"\b{term}\b", current):
                    self.stdout.write(f"  - {cell}: already includes {term} (skip)")
                    continue
                fixed = current[:-1] + f"+{term})"
                self.stdout.write(f"  + {cell}: {current}  →  {fixed}")
                writes.append({
                    "range": f"'{master_tab}'!{cell}",
                    "values": [[fixed]],
                })

        if not writes and not clears and not row_inserts:
            self.stdout.write("\nNothing to fix.")
            return
        if not apply:
            self.stdout.write(
                f"\nWould update {len(writes)} cell(s)"
                + (f", clear {len(clears)} range(s)" if clears else "")
                + (f", insert {len(row_inserts)} FORMULA ROW(s)" if row_inserts else "")
                + "."
            )
            return
        # Order matters: insert the fresh FORMULA ROW first (shifting data
        # down), THEN clear spill-blocking literals, THEN write formulas —
        # a BYROW written before its column is clear lands as a #REF!.
        if row_inserts:
            meta = (
                svc.spreadsheets()
                .get(spreadsheetId=sheet_id,
                     fields="sheets.properties(title,sheetId)")
                .execute()
            )
            gids = {
                s["properties"]["title"]: s["properties"]["sheetId"]
                for s in meta.get("sheets", [])
            }
            requests = []
            for tab in row_inserts:
                if tab not in gids:
                    raise CommandError(f"Tab {tab!r} vanished mid-run — aborting.")
                requests.append({
                    "insertDimension": {
                        "range": {
                            "sheetId": gids[tab],
                            "dimension": "ROWS",
                            "startIndex": FORMULA_ROW - 1,
                            "endIndex": FORMULA_ROW,
                        },
                        "inheritFromBefore": False,
                    }
                })
            svc.spreadsheets().batchUpdate(
                spreadsheetId=sheet_id, body={"requests": requests}
            ).execute()
        if clears:
            svc.spreadsheets().values().batchClear(
                spreadsheetId=sheet_id, body={"ranges": clears}
            ).execute()
        if writes:
            svc.spreadsheets().values().batchUpdate(
                spreadsheetId=sheet_id,
                body={"valueInputOption": "USER_ENTERED", "data": writes},
            ).execute()
        self.stdout.write(self.style.SUCCESS(
            f"\nUpdated {len(writes)} cell(s)"
            + (f", cleared {len(clears)} range(s)" if clears else "")
            + (f", inserted {len(row_inserts)} FORMULA ROW(s)" if row_inserts else "")
            + "."
        ))
