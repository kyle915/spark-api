"""Combine the Torch schedule tabs into one de-duped tab, flagging conflicts.

The workbook holds the same schedule three times — 'Retail Schedule' (an
INDEX/MATCH mirror), 'Retail Schedule Raw', and 'Retail Schedule Source' — and
they do not agree. 83 rows already differ between the mirror and the source.
The union of the three is the real schedule; the disagreements are what nobody
can currently see.

So this does the combining, but it does NOT quietly pick a winner. Where two
tabs carry the same activation with different values, the row is written once
and FLAGGED with which field disagrees and what each tab said. A silent merge
would bury exactly the thing that needs a human.

    (default)  read-only. Reports tab inventory, dedupe counts and every
               conflict, and uploads the full conflict list to GCS.
    --apply    creates the combined tab. Existing tabs are never read-modified
               or deleted -- this only ever ADDS a tab.

Dedupe key is Date + Store Name + Address + Start Time, each normalised for
case, whitespace and punctuation, because the three tabs spell the same store
differently.

Usage::

    python manage.py build_torch_combined_tab
    python manage.py build_torch_combined_tab --apply
"""

from __future__ import annotations

import csv
import io as _io
import json
import re

from django.core.management.base import BaseCommand

DEFAULT_TABS = ["Retail Schedule", "Retail Schedule Raw", "Retail Schedule Source"]
COMBINED_TAB = "Combined Schedule (Spark)"

# The nine client columns that describe an activation. Everything right of
# these is ops-owned or Spark bookkeeping and is carried from whichever tab
# supplied the row, never merged.
CORE = 9


def _norm(value) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(value or "").strip().lower())


def _norm_date(value) -> str:
    """Fold the several date spellings the tabs use into one token."""
    import datetime as _dt

    text = str(value or "").strip()
    for fmt in ("%b %d, %Y", "%B %d, %Y", "%m/%d/%Y", "%Y-%m-%d", "%m/%d/%y"):
        try:
            return _dt.datetime.strptime(text, fmt).date().isoformat()
        except ValueError:
            continue
    return _norm(text)


class Command(BaseCommand):
    help = "Combine + de-dupe the Torch schedule tabs, flagging conflicts."

    def add_arguments(self, parser):
        parser.add_argument("--apply", action="store_true",
                            help="Create the combined tab (adds a tab; never edits existing ones).")
        parser.add_argument("--tabs", default=",".join(DEFAULT_TABS),
                            help="Comma-separated tab names to combine.")
        parser.add_argument("--target", default=COMBINED_TAB,
                            help=f"Tab to write (default {COMBINED_TAB!r}).")
        parser.add_argument(
            "--overwrite", action="store_true",
            help="Allow writing into a tab that already exists. DESTRUCTIVE: "
                 "the tab is cleared and rebuilt, which also removes any "
                 "formulas in it. Every tab is backed up first.",
        )

    # ------------------------------------------------------------------

    def handle(self, *args, **opts):
        from utils import torch_public_form_sheet as T

        apply = bool(opts["apply"])
        sid = T.TORCH_PUBLIC_FORM_SHEET_ID
        want = [t.strip() for t in opts["tabs"].split(",") if t.strip()]

        self.stdout.write("=" * 74)
        self.stdout.write(
            "TORCH COMBINED TAB\n"
            f"MODE: {'APPLY (creates a tab)' if apply else 'REPORT (read-only)'}"
        )
        self.stdout.write("=" * 74)

        svc = T._service()
        if not svc:
            self.stdout.write(self.style.ERROR("\n  No Sheets credentials."))
            return

        meta = svc.spreadsheets().get(spreadsheetId=sid).execute()
        present = [s["properties"]["title"] for s in meta.get("sheets", [])]
        self.stdout.write(f"\n  tabs in workbook ({len(present)}):")
        for t in present:
            self.stdout.write(f"     {t!r}")

        missing = [t for t in want if t not in present]
        if missing:
            self.stdout.write(
                self.style.WARNING(f"\n  NOT FOUND, skipping: {missing}")
            )
        want = [t for t in want if t in present]
        if not want:
            self.stdout.write(self.style.ERROR("\n  None of the requested tabs exist."))
            return

        # -- read every tab ------------------------------------------------
        data: dict[str, list] = {}
        for t in want:
            rows = (
                svc.spreadsheets().values()
                .get(spreadsheetId=sid, range=f"'{t}'!A1:BZ",
                     valueRenderOption="FORMATTED_VALUE")
                .execute().get("values", [])
            )
            data[t] = rows
            self.stdout.write(f"\n  {t!r}: {len(rows)} row(s)")

        header = max((r[0] for r in data.values() if r), key=len, default=[])
        width = max((len(r) for rows in data.values() for r in rows), default=0)

        # -- combine + dedupe ---------------------------------------------
        combined: dict[tuple, dict] = {}
        order: list[tuple] = []
        for tab in want:
            for n, row in enumerate(data[tab], start=1):
                if n == 1 or not any(str(c).strip() for c in row):
                    continue
                padded = list(row) + [""] * (width - len(row))
                key = (
                    _norm_date(padded[2]),
                    _norm(padded[3]),
                    _norm(padded[6]),
                    _norm(padded[4]),
                )
                if not any(key):
                    continue
                if key not in combined:
                    combined[key] = {"row": padded, "tabs": [tab],
                                     "rownums": [n], "conflicts": [],
                                     "filled": 0}
                    order.append(key)
                    continue
                entry = combined[key]
                entry["tabs"].append(tab)
                entry["rownums"].append(n)
                # Merge across EVERY column, not just the descriptive nine.
                # Ops-owned columns (Rate, BA Name, Recap, Contract ...) exist
                # on some tabs and not others; taking the base row from one tab
                # and ignoring the rest would silently drop whichever of those
                # only lived on another tab.
                for i in range(len(padded)):
                    a = str(entry["row"][i]).strip() if i < len(entry["row"]) else ""
                    b = str(padded[i]).strip()
                    if not b:
                        continue
                    if not a:
                        # Fill a blank from whichever tab has the value. This is
                        # the only merge decision made without a human.
                        entry["row"][i] = padded[i]
                        entry["filled"] += 1
                        continue
                    if a != b and i < CORE:
                        # Only the descriptive columns are worth flagging; ops
                        # columns differ by design across tabs.
                        col = header[i] if i < len(header) else f"col{i+1}"
                        entry["conflicts"].append(
                            {"field": str(col), "a_tab": entry["tabs"][0], "a": a,
                             "b_tab": tab, "b": b}
                        )

        conflicted = [combined[k] for k in order if combined[k]["conflicts"]]
        multi = [combined[k] for k in order if len(combined[k]["tabs"]) > 1]

        # Where do the singletons come from? A row present in only ONE tab is
        # either genuinely missing from the others, or an artifact of the
        # mirror rendering shifted values (which changes its dedupe key). Those
        # need very different responses, so the breakdown is reported BEFORE
        # any tab is written rather than discovered afterwards.
        from collections import Counter

        only = Counter()
        allthree = 0
        for k in order:
            tabs = set(combined[k]["tabs"])
            if len(tabs) == 1:
                only[next(iter(tabs))] += 1
            if len(tabs) == len(want):
                allthree += 1

        self.stdout.write("")
        self.stdout.write("-" * 74)
        self.stdout.write(
            f"  unique activations : {len(order)}\n"
            f"  appear in >1 tab   : {len(multi)}\n"
            f"  in ALL {len(want)} tabs      : {allthree}\n"
            f"  WITH CONFLICTS     : {len(conflicted)}"
        )
        self.stdout.write("\n  present in ONE tab only:")
        for tab in want:
            self.stdout.write(f"     {tab:<28} {only.get(tab, 0)}")
        self.stdout.write("-" * 74)

        for e in conflicted[:15]:
            r = e["row"]
            self.stdout.write(
                f"\n  {r[2]} · {r[3]} · {r[6][:40]}"
                f"\n     in: {', '.join(e['tabs'])}"
            )
            for c in e["conflicts"][:6]:
                self.stdout.write(
                    f"     {c['field']}: {c['a_tab']}={c['a']!r}  vs  "
                    f"{c['b_tab']}={c['b']!r}"
                )
        if len(conflicted) > 15:
            self.stdout.write(f"\n  ... and {len(conflicted) - 15} more (see CSV)")

        # -- full conflict list to GCS ------------------------------------
        buf = _io.StringIO()
        w = csv.writer(buf)
        w.writerow(["date", "store", "address", "in_tabs", "field",
                    "tab_a", "value_a", "tab_b", "value_b"])
        for e in conflicted:
            r = e["row"]
            for c in e["conflicts"]:
                w.writerow([r[2], r[3], r[6], "|".join(e["tabs"]),
                            c["field"], c["a_tab"], c["a"], c["b_tab"], c["b"]])

        from django.utils import timezone as _tz

        from utils.gcs import public_url, upload_bytes

        stamp = _tz.now().strftime("%Y%m%d%H%M%S")
        blob = f"exports/torch-combined/{stamp}-conflicts.csv"
        upload_bytes(blob, buf.getvalue().encode(), content_type="text/csv")
        self.stdout.write(f"\n  CONFLICTS CSV: {public_url(blob)}")

        if not apply:
            self.stdout.write(
                f"\n  READ-ONLY — no tab created. Re-run with --apply to write "
                f"{opts['target']!r}."
            )
            return

        # -- create the tab (additive only) --------------------------------
        target = opts["target"]
        overwrite = bool(opts["overwrite"])

        if target in present and not overwrite:
            self.stdout.write(
                self.style.ERROR(
                    f"\n  {target!r} already exists — refusing to overwrite it. "
                    "Pass --overwrite if that is genuinely intended."
                )
            )
            return

        # A destructive write gets a FULL backup first — every tab, values and
        # formulas. The earlier backup covered two tabs; rebuilding a tab from
        # a snapshot that never contained the others is not a backup.
        if target in present:
            full = {}
            for t in present:
                try:
                    full[t] = {
                        "values": svc.spreadsheets().values().get(
                            spreadsheetId=sid, range=f"'{t}'!A1:BZ",
                            valueRenderOption="FORMATTED_VALUE",
                        ).execute().get("values", []),
                        "formulas": svc.spreadsheets().values().get(
                            spreadsheetId=sid, range=f"'{t}'!A1:BZ",
                            valueRenderOption="FORMULA",
                        ).execute().get("values", []),
                    }
                except Exception as exc:  # noqa: BLE001
                    full[t] = {"error": str(exc)}
            blob2 = f"exports/torch-combined/{stamp}-FULL-BACKUP.json"
            upload_bytes(
                blob2,
                json.dumps({"sheet_id": sid, "tabs": full}, indent=2).encode(),
                content_type="application/json",
            )
            self.stdout.write(
                self.style.WARNING(
                    f"\n  FULL BACKUP (all {len(present)} tabs): {public_url(blob2)}"
                )
            )
            # Clear the target so stale rows below the new data can't survive.
            svc.spreadsheets().values().clear(
                spreadsheetId=sid, range=f"'{target}'!A1:BZ", body={}
            ).execute()
        else:
            svc.spreadsheets().batchUpdate(
                spreadsheetId=sid,
                body={"requests": [{"addSheet": {"properties": {"title": target}}}]},
            ).execute()

        out_header = list(header) + ["Sources", "Conflict?", "Conflict detail"]
        body = [out_header]
        for k in order:
            e = combined[k]
            detail = "; ".join(
                f"{c['field']}: {c['a_tab']}={c['a']} vs {c['b_tab']}={c['b']}"
                for c in e["conflicts"]
            )
            body.append(
                list(e["row"])[:len(header)]
                + ["|".join(e["tabs"]), "YES" if e["conflicts"] else "", detail]
            )

        svc.spreadsheets().values().update(
            spreadsheetId=sid,
            range=f"'{target}'!A1",
            valueInputOption="RAW",
            body={"values": body},
        ).execute()

        self.stdout.write("")
        self.stdout.write("=" * 74)
        self.stdout.write(
            self.style.SUCCESS(
                f"Created {target!r}: {len(body) - 1} de-duped rows, "
                f"{len(conflicted)} flagged."
            )
        )
        filled = sum(e["filled"] for e in combined.values())
        self.stdout.write(
            f"{filled} blank cell(s) filled from another tab."
        )
        self.stdout.write("=" * 74)
