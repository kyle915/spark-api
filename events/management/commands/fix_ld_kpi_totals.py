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

5. KPI range START row skewed off the FORMULA ROW. Every KPI formula in
   rows 3-16 must begin at row 19 (the labeled FORMULA ROW, which is
   inert: blank date, Is-Event FALSE, no can counts) so that the first
   real data row — 20 — is inside the range. Repair 3 above inserts a
   fresh row 19 when the formula row was deleted, and Sheets then
   auto-shifts every range start DOWN by one (19→20) while the data
   shifts down too — leaving the ranges starting one row BELOW the first
   data row, silently excluding it from every metric. Poli's tab took
   this twice (ranges ended up at 21 vs data at 20), so her first logged
   event counted toward nothing. Detects the tab's dominant start row and
   rewrites only START positions (a row number immediately followed by
   ":"), never end rows, and no-ops when the start already equals 19.

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

# Row-total spill anchors live in a dedicated dummy row labeled "FORMULA ROW"
# in column C. Its row number is NOT hardcoded: adding a KPI metric row (e.g.
# the "Others" flavor row) shifts the whole event log down, and a fixed 19
# would then silently target the wrong row — that is exactly how Poli's ranges
# ended up skewed. `_find_formula_row` locates it by label per tab and only
# falls back to this default when the label is missing.
FORMULA_ROW_DEFAULT = 19
FORMULA_ROW_LABEL = "FORMULA ROW"
# Anchor templates: {r} is substituted with the located formula row.
ANCHORS = [
    ("I", "SAMPLES", "=BYROW(J{r}:AJ, LAMBDA(row, IF(COUNTA(row)=0, 0, SUM(row))))"),
    ("AK", "SALES", "=BYROW(AL{r}:BL, LAMBDA(row, IF(COUNTA(row)=0, 0, SUM(row))))"),
]


def _find_formula_row(svc, sheet_id: str, tab: str) -> int:
    """Row number of the labeled FORMULA ROW on `tab` (falls back to 19).

    Scans a window around the usual position so a shifted layout still
    resolves. Column C carries the label on every scorecard tab.
    """
    try:
        resp = (
            svc.spreadsheets().values()
            .get(spreadsheetId=sheet_id, range=f"'{tab}'!C15:C30")
            .execute()
        )
    except Exception:
        return FORMULA_ROW_DEFAULT
    for i, r in enumerate(resp.get("values") or [], start=15):
        if r and str(r[0]).strip().upper() == FORMULA_ROW_LABEL:
            return i
    return FORMULA_ROW_DEFAULT

# Repair 5 — KPI block whose range starts must sit on the FORMULA ROW.
# Rows 3-16 are the KPI metrics; C = annual total, E = arrow-paged current
# month, F..R = Jan..Dec (R duplicates December). D is a static target, so
# it is never rewritten.
# 3-17 rather than 3-16: adding the "Others" flavor row extends the
# block by one. Non-formula cells are skipped, so over-scanning is safe.
KPI_ROWS = range(3, 18)
KPI_FORMULA_COLS = ["C"] + list("EFGHIJKLMNOPQR")
# A range START is a (optionally $-anchored) column + row immediately
# followed by ":". The lookahead is what keeps end rows untouched — an end
# row is followed by ")" / "," / whitespace, never ":".
_RANGE_START = re.compile(r"(?<![A-Z0-9$])(\$?[A-Z]{1,2}\$?)(\d+)(?=:)")


def _kpi_start_rows(rows: list[list], col_letters: list[str]) -> list[int]:
    """Every range-start row number found in the KPI block's formulas."""
    found: list[int] = []
    for r in KPI_ROWS:
        row = rows[r - 1] if len(rows) >= r else []
        for col in col_letters:
            ci = ord(col) - ord("A")
            cur = str(row[ci]).strip() if len(row) > ci else ""
            if not cur.startswith("="):
                continue
            found.extend(int(m.group(2)) for m in _RANGE_START.finditer(cur))
    return found


def _reanchor(formula: str, wrong_row: int, right_row: int) -> tuple[str, int]:
    """Rewrite range STARTS from wrong_row to right_row. Returns (new, n)."""
    def sub(m: re.Match) -> str:
        return f"{m.group(1)}{right_row}" if int(m.group(2)) == wrong_row else m.group(0)
    out = _RANGE_START.sub(sub, formula)
    return out, (0 if out == formula else 1)


# Repair 6 — a KPI cell whose formula was pasted over with a value.
# The month grid is E (arrow-paged "current month", keyed off D$1) then
# F..Q = Jan..Dec. Every cell in a metric row shares one shape and differs ONLY
# by its month-key column, so a lost formula is rebuildable exactly from any
# surviving sibling in the same row.
#
# Found in the wild: Florida!K7 (June / Mountain) held a literal SPACE, so the
# monthly breakdown under-reported 974 cans while the annual was correct. NOTE
# a whitespace-only literal survives a naive `if value` check — detect these by
# asserting the cell HAS a formula, never by hunting for non-empty literals.
#
# R is deliberately IN this list but gated by _month_cols_for() below: row 1 has
# no R header, so R's month key matches nothing and every R formula evaluates to
# 0. Only row 3 has one (a stray copy-drag); "restoring" R4..R11 would paste a
# column of zeros onto all 7 tabs. Gating on a real header keeps that from
# happening again if someone adds or removes a month column.
MONTH_COLS = list("EFGHIJKLMNOPQR")


def _month_key(col: str) -> str:
    """The month-name header cell a monthly formula compares against."""
    return "D" if col == "E" else col


def _month_cols_for(header_row: list) -> list:
    """Month columns this tab actually has, i.e. whose month key is populated.

    A monthly formula keys off its header cell (=TEXT(...)=K$1), so a column
    with a blank header can only ever total 0 — it is spare grid, not a month.
    """
    def head(letter: str) -> str:
        i = ord(letter) - ord("A")
        return str(header_row[i]).strip() if len(header_row) > i else ""

    return [c for c in MONTH_COLS if head(_month_key(c))]


def _rebuild_month_formula(sib_formula: str, sib_col: str, tgt_col: str):
    """Clone a sibling monthly formula, swapping only its month key."""
    sk, tk = _month_key(sib_col), _month_key(tgt_col)
    if sk == tk:
        return sib_formula, 0
    out, n = re.subn(
        rf"(?<![A-Z0-9$]){re.escape(sk)}\$1", f"{tk}$1", sib_formula
    )
    return out, n


# A tab's own grid end row, learned from any bounded formula on that tab, so a
# donated ANNUAL formula can be retargeted to the correct tab length.
# The end row may be followed by ")" (SUM) or "," (COUNTIF's criteria
# argument), so match on a lookahead rather than consuming the delimiter.
_END_ROW_RE = re.compile(r":\$?[A-Z]{1,2}(\d{3,5})(?=[),])")


def _retarget_end_row(formula: str, end_row: int) -> str:
    return _END_ROW_RE.sub(lambda m: m.group(0).replace(m.group(1), str(end_row)), formula)


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
            "--restore-annual",
            action="store_true",
            help=(
                "Also rebuild ANNUAL (col C) cells whose formula was pasted "
                "over, donating the same metric's formula from another tab. "
                "OFF by default: deliberate business hardcodes live in col C "
                "(e.g. Northeast Retail Samplings), and overwriting one "
                "destroys a number that exists nowhere else."
            ),
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
        row_inserts: list = []  # (tab, row) needing a fresh FORMULA ROW
        annual_gaps: list = []   # (tab, row, label, value, end_row)
        annual_donors: dict = {} # tab -> {label: annual formula}
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

            # The formula row is LOCATED per tab (see _find_formula_row):
            # inserting a KPI metric row shifts the whole event log down, so a
            # hardcoded 19 would target the wrong row.
            FORMULA_ROW = _find_formula_row(svc, sheet_id, tab)
            if FORMULA_ROW != FORMULA_ROW_DEFAULT:
                self.stdout.write(
                    f"  · FORMULA ROW located at {FORMULA_ROW} "
                    f"(not the default {FORMULA_ROW_DEFAULT})"
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
                    "values": [[anchor.format(r=FORMULA_ROW)]],
                })
            if needs_insert:
                self.stdout.write(
                    f"  + row {FORMULA_ROW} is a DATA row (the FORMULA ROW "
                    "was deleted) → insert a fresh labeled row above it"
                )
                row_inserts.append((tab, FORMULA_ROW))
                writes.append({
                    "range": f"'{tab}'!B{FORMULA_ROW}:C{FORMULA_ROW}",
                    "values": [[False, "FORMULA ROW"]],
                })

            # ---- Repair 5: KPI range starts skewed off the FORMULA ROW ---
            # Skipped when this run is going to insert a row here: the
            # insert itself shifts every range start, so re-anchoring in the
            # same pass would fight it. Re-run afterwards to settle.
            if needs_insert:
                self.stdout.write(
                    "  - KPI range starts: deferred (a FORMULA ROW insert is "
                    "queued this run — re-run to re-anchor)"
                )
            else:
                starts = _kpi_start_rows(rows, KPI_FORMULA_COLS)
                if not starts:
                    self.stdout.write(
                        "  - KPI range starts: no ranges found (skip)"
                    )
                else:
                    from collections import Counter
                    tally = Counter(starts)
                    dominant, n_dom = tally.most_common(1)[0]
                    if dominant == FORMULA_ROW:
                        self.stdout.write(
                            f"  - KPI range starts: already row {FORMULA_ROW} "
                            f"({n_dom} refs) (skip)"
                        )
                    else:
                        n_cells = 0
                        for r in KPI_ROWS:
                            row = rows[r - 1] if len(rows) >= r else []
                            for col in KPI_FORMULA_COLS:
                                ci = ord(col) - ord("A")
                                cur = str(row[ci]).strip() if len(row) > ci else ""
                                if not cur.startswith("="):
                                    continue
                                fixed, changed = _reanchor(
                                    cur, dominant, FORMULA_ROW
                                )
                                if changed:
                                    writes.append({
                                        "range": f"'{tab}'!{col}{r}",
                                        "values": [[fixed]],
                                    })
                                    n_cells += 1
                        other = {k: v for k, v in tally.items() if k != dominant}
                        self.stdout.write(self.style.WARNING(
                            f"  + KPI range starts: row {dominant} → "
                            f"{FORMULA_ROW} in {n_cells} cell(s) — the first "
                            f"data row was OUTSIDE every KPI range"
                        ))
                        if other:
                            self.stdout.write(
                                f"      note: other start rows present {other} "
                                "(left alone)"
                            )

            # ---- Repair 6: KPI cells whose formula was pasted over ------
            # Monthly cells are rebuilt from a sibling in the same row (exact,
            # differing only by month key). Annual (C) cells are only REPORTED
            # unless --restore-annual, since col C is where intentional
            # business hardcodes live.
            end_row = None
            for r in KPI_ROWS:
                row = rows[r - 1] if len(rows) >= r else []
                cur = str(row[2]).strip() if len(row) > 2 else ""
                m = _END_ROW_RE.search(cur)
                if m:
                    end_row = int(m.group(1)); break
            month_cols = _month_cols_for(rows[0] if rows else [])
            skipped_cols = [c for c in MONTH_COLS if c not in month_cols]
            if skipped_cols:
                self.stdout.write(
                    f"  · month columns: {''.join(month_cols)} "
                    f"(no row-1 header on {','.join(skipped_cols)} — spare grid, "
                    "not a month)"
                )
            n_month = 0
            for r in KPI_ROWS:
                row = rows[r - 1] if len(rows) >= r else []
                label = str(row[0]).strip() if row else ""
                have, missing = {}, []
                for col in month_cols:
                    ci = ord(col) - ord("A")
                    cur = str(row[ci]).strip() if len(row) > ci else ""
                    if cur.startswith("="):
                        have[col] = cur
                    else:
                        missing.append((col, cur))
                if len(have) < 3:
                    continue  # not a monthly formula family — leave alone
                for col, was in missing:
                    sib_col, sib = next(iter(have.items()))
                    fixed, n = _rebuild_month_formula(sib, sib_col, col)
                    if not n:
                        self.stdout.write(self.style.ERROR(
                            f"  ! {col}{r} ({label}): could not rebuild from "
                            f"{sib_col}{r} — skipped"
                        ))
                        continue
                    self.stdout.write(self.style.WARNING(
                        f"  + {col}{r} ({label}): formula was MISSING "
                        f"(held {was!r}) → rebuilt from {sib_col}{r}"
                    ))
                    writes.append({
                        "range": f"'{tab}'!{col}{r}",
                        "values": [[fixed]],
                    })
                    n_month += 1
                # annual
                cur_c = str(row[2]).strip() if len(row) > 2 else ""
                if label and cur_c and not cur_c.startswith("="):
                    annual_gaps.append((tab, r, label, cur_c, end_row))
            if not n_month:
                self.stdout.write("  - pasted-over monthly cells: none (skip)")
            annual_donors.setdefault(tab, {})
            for r in KPI_ROWS:
                row = rows[r - 1] if len(rows) >= r else []
                label = str(row[0]).strip() if row else ""
                cur_c = str(row[2]).strip() if len(row) > 2 else ""
                if label and cur_c.startswith("="):
                    annual_donors[tab][label] = cur_c

        # ---- Repair 6b: ANNUAL cells pasted over (report; opt-in restore) --
        for tab, r, label, was, end_row in annual_gaps:
            donor = None
            for dtab, dmap in annual_donors.items():
                if dtab != tab and label in dmap:
                    donor = (dtab, dmap[label]); break
            if donor is None:
                self.stdout.write(self.style.ERROR(
                    f"[{tab}] C{r} ({label}) is a hardcode {was!r} — no donor "
                    "tab has this metric's annual formula (skip)"
                ))
                continue
            dtab, dformula = donor
            fixed = _retarget_end_row(dformula, end_row) if end_row else dformula
            if opts.get("restore_annual"):
                self.stdout.write(self.style.WARNING(
                    f"[{tab}] C{r} ({label}): hardcode {was!r} → {fixed} "
                    f"(donor {dtab})"
                ))
                writes.append({"range": f"'{tab}'!C{r}", "values": [[fixed]]})
            else:
                self.stdout.write(self.style.NOTICE(
                    f"[{tab}] C{r} ({label}) is a HARDCODE {was!r}. "
                    f"--restore-annual would set it to {fixed} (donor {dtab}). "
                    "Left alone: col C carries intentional business numbers."
                ))

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
            for tab, ins_row in row_inserts:
                if tab not in gids:
                    raise CommandError(f"Tab {tab!r} vanished mid-run — aborting.")
                requests.append({
                    "insertDimension": {
                        "range": {
                            "sheetId": gids[tab],
                            "dimension": "ROWS",
                            "startIndex": ins_row - 1,
                            "endIndex": ins_row,
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
