"""Find (and optionally rescue) activity rows stranded ABOVE the log header.

The LD KPI scorecard tabs are laid out:

    rows 3..19   KPI block
    row  21      event-log HEADER  ("Date | ... | Event Name | Category | ...")
    row  22      labeled FORMULA ROW holding the I / AK BYROW spill anchors
    rows 23..    the activity log itself

Every KPI formula reads from the FORMULA ROW down (`$A$22:$A`, `D22:D1009`, …).
The sheet's own "Liquid Death Tools" Apps Script appends new activity at a
HARDCODED row, and that row is now above the header because the block grew:
the "Others" flavor row added one row and the "Event" / "Sales (Venues / Events
/ Accounts)" activity-type rows added two more. So a row the script writes lands
outside every formula range — it is on the sheet, visibly, but counts toward
nothing. That is why cans entered through the tool stop showing up while the
same numbers typed by hand into the log work fine.

This reports each stranded row with the cans it carries, and with --apply moves
it into the top of the real log (immediately under the FORMULA ROW) so the cans
finally count. Locating the header and formula row BY LABEL, never by number, is
the whole point — the layout has moved three times already.

Note this does NOT fix the Apps Script; only its owner can. The report prints
what the script needs to change.

Usage:
    python manage.py fix_ld_orphan_log_rows            # dry-run
    python manage.py fix_ld_orphan_log_rows --apply
"""
from __future__ import annotations

from django.core.management.base import BaseCommand, CommandError

from utils.sheets_mirror import _service, extract_sheet_id

WORKBOOK_URL = (
    "https://docs.google.com/spreadsheets/d/"
    "1W4F7X_vdW7d0SmthUvdxujBH2CahG0DaB53xBVr5q04/edit"
)
DEFAULT_TABS = "Northeast,Florida,South,Central,West,Poli,Pat"

HEADER_MARKERS = ("event name", "category")
FORMULA_ROW_LABEL = "formula row"
# Sample-side SKU columns J..AJ -> 0-based 9..35 inclusive.
SKU_FIRST, SKU_LAST = 9, 35
SCAN_TO = 40


def _num(v) -> float:
    try:
        return float(str(v).replace(",", "").replace("$", "").strip() or 0)
    except Exception:
        return 0.0


class Command(BaseCommand):
    help = (
        "Report activity rows stranded above the LD log header (invisible to "
        "every KPI formula); --apply moves them into the log. Dry-run default."
    )

    def add_arguments(self, parser):
        parser.add_argument("--sheet-url", type=str, default=WORKBOOK_URL)
        parser.add_argument("--tabs", type=str, default=DEFAULT_TABS)
        parser.add_argument("--apply", action="store_true")

    def handle(self, *args, **opts):
        sheet_id = extract_sheet_id(opts["sheet_url"])
        if not sheet_id:
            raise CommandError("Could not parse a sheet id from --sheet-url.")
        svc = _service()
        if svc is None:
            raise CommandError("No Sheets credentials (ADC).")
        tabs = [t.strip() for t in (opts["tabs"] or "").split(",") if t.strip()]
        apply = opts["apply"]
        if not apply:
            self.stdout.write(self.style.WARNING(
                "DRY RUN — pass --apply to move the stranded rows into the log.\n"
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

        total_rows = 0
        total_cans = 0.0
        for tab in tabs:
            if tab not in gids:
                self.stdout.write(self.style.ERROR(f"[{tab}] not found"))
                continue
            resp = (
                svc.spreadsheets().values()
                .get(spreadsheetId=sheet_id,
                     range=f"'{tab}'!A1:AJ{SCAN_TO}",
                     valueRenderOption="FORMATTED_VALUE")
                .execute()
            )
            rows = resp.get("values") or []

            header_row = formula_row = None
            for i, row in enumerate(rows, start=1):
                joined = " ".join(str(c) for c in row).lower()
                if header_row is None and all(m in joined for m in HEADER_MARKERS):
                    header_row = i
                if formula_row is None and FORMULA_ROW_LABEL in joined:
                    formula_row = i
            if header_row is None or formula_row is None:
                self.stdout.write(self.style.ERROR(
                    f"[{tab}] could not locate header/FORMULA ROW by label "
                    f"(header={header_row}, formula={formula_row}) — skipped"
                ))
                continue

            # A stranded row: above the header, but carrying an activity type or
            # an event name. The KPI block above it has labels in column A, the
            # log has a DATE in column A — so require A to be non-label-ish and
            # either C (event name) or D (category) to be filled.
            stranded = []
            for i in range(3, header_row):
                row = rows[i - 1] if len(rows) >= i else []
                if not row:
                    continue
                a = str(row[0]).strip() if len(row) > 0 else ""
                c = str(row[2]).strip() if len(row) > 2 else ""
                d = str(row[3]).strip() if len(row) > 3 else ""
                if not (c or d):
                    continue
                # KPI rows carry their label in column A ("Direct", "Mountain"…);
                # a log row's column A is a date / serial.
                if a and not a[0].isdigit():
                    continue
                cans = sum(
                    _num(row[j]) for j in range(SKU_FIRST, SKU_LAST + 1)
                    if len(row) > j
                )
                stranded.append((i, a, c, d, cans))

            self.stdout.write(
                f"[{tab}] header row {header_row}, FORMULA ROW {formula_row}, "
                f"log starts {formula_row + 1}"
            )
            if not stranded:
                self.stdout.write("  no stranded rows above the header\n")
                continue
            for i, a, c, d, cans in stranded:
                total_rows += 1
                total_cans += cans
                self.stdout.write(self.style.WARNING(
                    f"  ! row {i}: {a} | {c[:34]} | {d[:26]} | {cans:,.0f} cans "
                    "— ABOVE the header, counts toward NOTHING"
                ))
            if not apply:
                self.stdout.write(
                    f"  would move {len(stranded)} row(s) to row "
                    f"{formula_row + 1}\n"
                )
                continue

            # Move bottom-up so earlier indices stay valid. moveDimension
            # carries values AND formatting, and Sheets re-points references.
            gid = gids[tab]
            moved = 0
            for i, *_rest in sorted(stranded, key=lambda t: t[0], reverse=True):
                svc.spreadsheets().batchUpdate(
                    spreadsheetId=sheet_id,
                    body={"requests": [{
                        "moveDimension": {
                            "source": {
                                "sheetId": gid, "dimension": "ROWS",
                                "startIndex": i - 1, "endIndex": i,
                            },
                            # +1 because a forward move is applied after the
                            # source row is lifted out.
                            "destinationIndex": formula_row + 1,
                        }
                    }]},
                ).execute()
                moved += 1
            self.stdout.write(self.style.SUCCESS(
                f"  moved {moved} row(s) into the log\n"
            ))

        self.stdout.write(
            f"\n{total_rows} stranded row(s) carrying {total_cans:,.0f} cans "
            "that no KPI formula can see."
        )
        self.stdout.write(
            "\nThe Apps Script behind 'Liquid Death Tools' still appends at a "
            "FIXED row. It must find its insert point by LABEL instead, e.g.\n"
            "    const col = sh.getRange('C1:C40').getValues().flat();\n"
            "    const formulaRow = col.findIndex(v =>\n"
            "        String(v).trim().toUpperCase() === 'FORMULA ROW') + 1;\n"
            "    sh.insertRowAfter(formulaRow);   // then write into formulaRow+1\n"
            "Otherwise every future row/column added to the KPI block silently "
            "strands new entries again."
        )
