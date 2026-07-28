"""Add an "Others" flavor row to the LD RMM KPI workbook's scorecard tabs.

The sample-side SKU columns run J..AJ, where **AJ = "Others"** (the catch-all
for cans that don't match a listed SKU). The four flavor KPI rows only span
J..AI:

    Mountain           J:K
    Iced Tea           Z:AE
    Sparkling Flavors  L:Y
    Energy             AF:AI
    -- nothing covers  AJ  --

So "Others" counts toward ``Total Cans Sampled`` (which sums J:AJ) but appears
in NO flavor row — the breakdown can never reconcile to the total. Workbook-wide
that hid 11,386 cans (West 5,130 / Central 4,200 / Pat 1,250 / Northeast 734 /
Poli 72), which is why folding it into Energy was rejected: it would have
overstated Energy on tabs the client reports from.

This inserts a 5th flavor row directly under Energy on every scorecard tab and
fills it with Energy's own formula shapes retargeted from AF:AI to AJ:AJ, so it
inherits the exact same anchoring, month keying and open/closed range style as
its siblings rather than a hand-written guess.

IMPORTANT — the insert shifts everything below it down one row: the event-log
header, the labeled FORMULA ROW that protects the SAMPLES/SALES spill anchors,
and all data. Google re-points the KPI ranges and MASTER's cross-tab
references automatically, and MASTER stays consistent because the insert is
applied to ALL tabs in one batch. Afterwards run ``fix_ld_kpi_totals`` (which
locates the FORMULA ROW by its label, not a fixed row number) to confirm
everything still lines up.

Safety: dry-run by default; refuses to run unless every tab agrees on the
Energy row index; no-ops when an "Others" row already exists.

Usage:
    python manage.py add_ld_others_row            # dry-run
    python manage.py add_ld_others_row --apply
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

SOURCE_LABEL = "Energy"      # row whose formula shapes we clone
NEW_LABEL = "Others"         # label for the inserted row
# Energy spans AF:AI; the new row targets the single Others column AJ.
SRC_FIRST, SRC_LAST, DST_COL = "AF", "AI", "AJ"
KPI_FORMULA_COLS = ["C"] + list("EFGHIJKLMNOPQR")

# AF..AI -> AJ..AJ, preserving each side's $ anchors and any end row.
_RETARGET = re.compile(rf"(\$?){SRC_FIRST}(\$?)(\d+)\s*:\s*(\$?){SRC_LAST}")


def _retarget(formula: str) -> tuple[str, int]:
    out, n = _RETARGET.subn(rf"\g<1>{DST_COL}\g<2>\g<3>:\g<4>{DST_COL}", formula)
    return out, n


class Command(BaseCommand):
    help = (
        'Insert an "Others" flavor row under Energy on the LD KPI scorecard '
        "tabs, cloning Energy's formula shapes onto column AJ. Dry-run default."
    )

    def add_arguments(self, parser):
        parser.add_argument("--sheet-url", type=str, default=WORKBOOK_URL)
        parser.add_argument("--tabs", type=str, default=DEFAULT_TABS)
        parser.add_argument(
            "--apply", action="store_true",
            help="Actually insert + write. Without this, report only.",
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
                "DRY RUN — pass --apply to insert the row and write formulas.\n"
            ))

        meta = (
            svc.spreadsheets()
            .get(spreadsheetId=sheet_id, fields="sheets.properties(title,sheetId)")
            .execute()
        )
        gids = {
            s["properties"]["title"]: s["properties"]["sheetId"]
            for s in meta.get("sheets", [])
        }

        # ---- Pass 1: locate the Energy row on each tab + sanity-check ------
        energy_rows: dict[str, int] = {}
        for tab in tabs:
            if tab not in gids:
                raise CommandError(f"Tab {tab!r} not found in the workbook.")
            resp = (
                svc.spreadsheets().values()
                .get(spreadsheetId=sheet_id, range=f"'{tab}'!A1:R20",
                     valueRenderOption="FORMULA")
                .execute()
            )
            rows = resp.get("values") or []
            labels = {
                str(r[0]).strip(): i
                for i, r in enumerate(rows, start=1) if r and r[0]
            }
            if NEW_LABEL in labels:
                self.stdout.write(self.style.SUCCESS(
                    f"[{tab}] '{NEW_LABEL}' row already present at "
                    f"{labels[NEW_LABEL]} — nothing to do."
                ))
                return
            if SOURCE_LABEL not in labels:
                raise CommandError(f"[{tab}] no {SOURCE_LABEL!r} row found.")
            energy_rows[tab] = labels[SOURCE_LABEL]

        distinct = sorted(set(energy_rows.values()))
        if len(distinct) != 1:
            raise CommandError(
                f"Tabs disagree on the {SOURCE_LABEL!r} row: {energy_rows}. "
                "Aborting rather than inserting at different offsets."
            )
        src_row = distinct[0]
        new_row = src_row + 1
        self.stdout.write(
            f"{SOURCE_LABEL} row = {src_row} on all {len(tabs)} tab(s) → "
            f"insert '{NEW_LABEL}' at row {new_row}\n"
        )

        if not apply:
            # Preview the retarget on the CURRENT (unshifted) formulas. On
            # apply the row numbers all shift +1, which Sheets does for us.
            tab = tabs[0]
            resp = (
                svc.spreadsheets().values()
                .get(spreadsheetId=sheet_id,
                     range=f"'{tab}'!A{src_row}:R{src_row}",
                     valueRenderOption="FORMULA")
                .execute()
            )
            row = (resp.get("values") or [[]])[0]
            shown = 0
            self.stdout.write(f"[{tab}] formulas cloned from {SOURCE_LABEL}:")
            for col in KPI_FORMULA_COLS:
                ci = ord(col) - ord("A")
                cur = str(row[ci]).strip() if len(row) > ci else ""
                if not cur.startswith("="):
                    continue
                new, n = _retarget(cur)
                if not n:
                    self.stdout.write(self.style.ERROR(
                        f"  ! {col}{src_row}: no {SRC_FIRST}:{SRC_LAST} range "
                        f"in {cur!r} — would be SKIPPED"
                    ))
                    continue
                if shown < 4:
                    self.stdout.write(f"  {col}: {cur}\n     → {new}")
                    shown += 1
            self.stdout.write(
                f"\nWould insert 1 row on {len(tabs)} tab(s) and write the "
                f"'{NEW_LABEL}' label + formulas.\n"
                "NOTE: the insert shifts the event log (header, FORMULA ROW, "
                "data) down one row on every tab."
            )
            return

        # ---- Pass 2: insert the row on every tab in ONE batch -------------
        svc.spreadsheets().batchUpdate(
            spreadsheetId=sheet_id,
            body={"requests": [
                {
                    "insertDimension": {
                        "range": {
                            "sheetId": gids[tab],
                            "dimension": "ROWS",
                            "startIndex": new_row - 1,
                            "endIndex": new_row,
                        },
                        # inherit Energy's formatting so it looks native
                        "inheritFromBefore": True,
                    }
                }
                for tab in tabs
            ]},
        ).execute()
        self.stdout.write(f"Inserted row {new_row} on {len(tabs)} tab(s).")

        # ---- Pass 3: re-read the (now shifted) Energy row + write Others ---
        writes: list[dict] = []
        for tab in tabs:
            resp = (
                svc.spreadsheets().values()
                .get(spreadsheetId=sheet_id,
                     range=f"'{tab}'!A{src_row}:R{src_row}",
                     valueRenderOption="FORMULA")
                .execute()
            )
            row = (resp.get("values") or [[]])[0]
            if not row or str(row[0]).strip() != SOURCE_LABEL:
                raise CommandError(
                    f"[{tab}] row {src_row} is {row[:1]!r}, expected "
                    f"{SOURCE_LABEL!r} — aborting before writing formulas."
                )
            writes.append({
                "range": f"'{tab}'!A{new_row}",
                "values": [[NEW_LABEL]],
            })
            n_cells = 0
            for col in KPI_FORMULA_COLS:
                ci = ord(col) - ord("A")
                cur = str(row[ci]).strip() if len(row) > ci else ""
                if not cur.startswith("="):
                    continue
                new, n = _retarget(cur)
                if not n:
                    self.stdout.write(self.style.ERROR(
                        f"  ! [{tab}] {col}{src_row}: no "
                        f"{SRC_FIRST}:{SRC_LAST} range — skipped"
                    ))
                    continue
                writes.append({
                    "range": f"'{tab}'!{col}{new_row}",
                    "values": [[new]],
                })
                n_cells += 1
            self.stdout.write(f"  [{tab}] {NEW_LABEL} row {new_row}: {n_cells} formula(s)")

        svc.spreadsheets().values().batchUpdate(
            spreadsheetId=sheet_id,
            body={"valueInputOption": "USER_ENTERED", "data": writes},
        ).execute()
        self.stdout.write(self.style.SUCCESS(
            f"\nWrote {len(writes)} cell(s). Next: run fix_ld_kpi_totals "
            "(dry-run) to confirm the FORMULA ROW + KPI ranges still align."
        ))
