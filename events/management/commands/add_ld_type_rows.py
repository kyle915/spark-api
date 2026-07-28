"""Add the missing activity-type rows to the LD RMM KPI workbook scorecards.

The KPI block breaks Total Cans Sampled down by activity type, but only has
rows for three of them:

    Direct Sampling      row 4
    Indirect Sampling    row 5
    Seeding              row 6
    -- nothing covers    "Event"  or  "Sales (Venues / Events / Accounts)" --

Each row filters the log on an exact string (``$D$20:$D = "Seeding"``), while
Total Cans Sampled sums the SKU columns unconditionally. So any log row carrying
one of the two unlisted types counts toward the total but appears in NO type
row, and the breakdown can never reconcile. Audited 2026-07-28, that hid 51,592
cans — West 42,828 (28% of the tab, incl. Festival of Books at 24,384), Central
5,688, South 2,688, Northeast 388.

This inserts two rows directly under Seeding on every scorecard tab and fills
them with Seeding's own formula shapes, swapping only the activity-type literal.
They therefore inherit the exact anchoring, month keying and open/closed range
style of their siblings rather than a hand-written guess. Nothing in the event
log is rewritten — retyping the RMMs' own entries would mean guessing what
"Event" means to the client.

Known straggler this does NOT fix: West row 49 ("LAFC Watch Party Sampling",
408 cans) is typed ``Sampling``, almost certainly a typo for one of the two
sampling types. Which one is a judgement call for the region owner, so it stays
uncounted and visible rather than being silently folded in.

IMPORTANT — the insert shifts everything below down TWO rows: the rest of the
KPI block, the event-log header, the labeled FORMULA ROW that protects the
SAMPLES/SALES spill anchors, and all data. Google re-points the KPI ranges and
MASTER's cross-tab references automatically, and MASTER stays consistent because
the insert is applied to ALL tabs in one batch. Afterwards run
``fix_ld_kpi_totals`` (which finds the FORMULA ROW by its label, not a fixed row
number) to confirm everything still lines up.

Safety: dry-run by default; refuses to run unless every tab agrees on the
Seeding row index; no-ops when the first new row already exists.

Usage:
    python manage.py add_ld_type_rows            # dry-run
    python manage.py add_ld_type_rows --apply
"""
from __future__ import annotations

from django.core.management.base import BaseCommand, CommandError

from utils.sheets_mirror import _service, extract_sheet_id

WORKBOOK_URL = (
    "https://docs.google.com/spreadsheets/d/"
    "1W4F7X_vdW7d0SmthUvdxujBH2CahG0DaB53xBVr5q04/edit"
)
DEFAULT_TABS = "Northeast,Florida,South,Central,West,Poli,Pat"

# Row whose formula shapes we clone, and the activity-type literal inside them.
SOURCE_LABEL = "Seeding"
SOURCE_MATCH = "Seeding"

# (row label, activity-type string as it appears in the log's column D).
# Order is the insert order, so these land directly under Seeding.
NEW_TYPES = [
    ("Event", "Event"),
    ("Sales (Venues / Events / Accounts)", "Sales (Venues / Events / Accounts)"),
]

KPI_FORMULA_COLS = ["C"] + list("EFGHIJKLMNOPQR")


def _retype(formula: str, activity: str) -> tuple[str, int]:
    """Swap the activity-type literal, leaving every range reference alone.

    The match is on the quoted literal so a bare word elsewhere in the formula
    can't be hit. Callers must check the count: 0 means the shape wasn't what we
    expected and the cell should be skipped rather than half-written.
    """
    needle = f'"{SOURCE_MATCH}"'
    n = formula.count(needle)
    if n != 1:
        return formula, 0
    return formula.replace(needle, f'"{activity}"'), 1


class Command(BaseCommand):
    help = (
        'Insert "Event" and "Sales (Venues / Events / Accounts)" activity-type '
        "rows under Seeding on the LD KPI scorecard tabs, cloning Seeding's "
        "formula shapes. Dry-run default."
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
        n_new = len(NEW_TYPES)
        if not apply:
            self.stdout.write(self.style.WARNING(
                "DRY RUN — pass --apply to insert the rows and write formulas.\n"
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

        # ---- Pass 1: locate the Seeding row on each tab + sanity-check -----
        src_rows: dict[str, int] = {}
        for tab in tabs:
            if tab not in gids:
                raise CommandError(f"Tab {tab!r} not found in the workbook.")
            resp = (
                svc.spreadsheets().values()
                .get(spreadsheetId=sheet_id, range=f"'{tab}'!A1:R22",
                     valueRenderOption="FORMULA")
                .execute()
            )
            rows = resp.get("values") or []
            labels = {
                str(r[0]).strip(): i
                for i, r in enumerate(rows, start=1) if r and r[0]
            }
            first_label = NEW_TYPES[0][0]
            if first_label in labels:
                self.stdout.write(self.style.SUCCESS(
                    f"[{tab}] {first_label!r} row already present at "
                    f"{labels[first_label]} — nothing to do."
                ))
                return
            if SOURCE_LABEL not in labels:
                raise CommandError(f"[{tab}] no {SOURCE_LABEL!r} row found.")
            src_rows[tab] = labels[SOURCE_LABEL]

        distinct = sorted(set(src_rows.values()))
        if len(distinct) != 1:
            raise CommandError(
                f"Tabs disagree on the {SOURCE_LABEL!r} row: {src_rows}. "
                "Aborting rather than inserting at different offsets."
            )
        src_row = distinct[0]
        first_new = src_row + 1
        self.stdout.write(
            f"{SOURCE_LABEL} row = {src_row} on all {len(tabs)} tab(s) → insert "
            f"{n_new} row(s) at {first_new}–{first_new + n_new - 1}: "
            + ", ".join(lbl for lbl, _ in NEW_TYPES) + "\n"
        )

        if not apply:
            # Preview the retype on the CURRENT (unshifted) formulas. On apply
            # every row number shifts +2, which Sheets does for us.
            tab = tabs[0]
            resp = (
                svc.spreadsheets().values()
                .get(spreadsheetId=sheet_id,
                     range=f"'{tab}'!A{src_row}:R{src_row}",
                     valueRenderOption="FORMULA")
                .execute()
            )
            row = (resp.get("values") or [[]])[0]
            for label, activity in NEW_TYPES:
                self.stdout.write(f"[{tab}] '{label}' cloned from {SOURCE_LABEL}:")
                shown = n_ok = 0
                for col in KPI_FORMULA_COLS:
                    ci = ord(col) - ord("A")
                    cur = str(row[ci]).strip() if len(row) > ci else ""
                    if not cur.startswith("="):
                        continue
                    new, n = _retype(cur, activity)
                    if not n:
                        self.stdout.write(self.style.ERROR(
                            f"  ! {col}{src_row}: no single \"{SOURCE_MATCH}\" "
                            f"literal in {cur!r} — would be SKIPPED"
                        ))
                        continue
                    n_ok += 1
                    if shown < 2:
                        self.stdout.write(f"  {col}: {cur}\n     → {new}")
                        shown += 1
                self.stdout.write(f"  ({n_ok} formula(s) per tab)\n")
            self.stdout.write(
                f"Would insert {n_new} row(s) on {len(tabs)} tab(s) and write "
                "the labels + formulas.\nNOTE: this shifts the rest of the KPI "
                "block and the whole event log (header, FORMULA ROW, data) down "
                f"{n_new} rows on every tab."
            )
            return

        # ---- Pass 2: insert the rows on every tab in ONE batch -------------
        svc.spreadsheets().batchUpdate(
            spreadsheetId=sheet_id,
            body={"requests": [
                {
                    "insertDimension": {
                        "range": {
                            "sheetId": gids[tab],
                            "dimension": "ROWS",
                            "startIndex": first_new - 1,
                            "endIndex": first_new - 1 + n_new,
                        },
                        # inherit Seeding's formatting so they look native
                        "inheritFromBefore": True,
                    }
                }
                for tab in tabs
            ]},
        ).execute()
        self.stdout.write(
            f"Inserted rows {first_new}–{first_new + n_new - 1} on "
            f"{len(tabs)} tab(s)."
        )

        # ---- Pass 3: re-read the Seeding row + write the new type rows -----
        # Seeding itself did not move, but its ranges now point 2 rows lower
        # (Google rewrote them), so re-reading is what keeps the clones correct.
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
            for offset, (label, activity) in enumerate(NEW_TYPES):
                tgt = first_new + offset
                writes.append({"range": f"'{tab}'!A{tgt}", "values": [[label]]})
                # Mirror Seeding's monthly TARGET literal so the new rows read
                # like their siblings instead of leaving a blank in the column.
                d_cur = str(row[3]).strip() if len(row) > 3 else ""
                if d_cur and not d_cur.startswith("="):
                    writes.append({
                        "range": f"'{tab}'!D{tgt}", "values": [[d_cur]],
                    })
                n_cells = 0
                for col in KPI_FORMULA_COLS:
                    ci = ord(col) - ord("A")
                    cur = str(row[ci]).strip() if len(row) > ci else ""
                    if not cur.startswith("="):
                        continue
                    new, n = _retype(cur, activity)
                    if not n:
                        self.stdout.write(self.style.ERROR(
                            f"  ! [{tab}] {col}{src_row}: no single "
                            f"\"{SOURCE_MATCH}\" literal — skipped"
                        ))
                        continue
                    writes.append({
                        "range": f"'{tab}'!{col}{tgt}", "values": [[new]],
                    })
                    n_cells += 1
                self.stdout.write(
                    f"  [{tab}] {label} → row {tgt}: {n_cells} formula(s)"
                )

        svc.spreadsheets().values().batchUpdate(
            spreadsheetId=sheet_id,
            body={"valueInputOption": "USER_ENTERED", "data": writes},
        ).execute()
        self.stdout.write(self.style.SUCCESS(
            f"\nWrote {len(writes)} cell(s). Next: run fix_ld_kpi_totals "
            "(dry-run) to confirm the FORMULA ROW + KPI ranges still align."
        ))
