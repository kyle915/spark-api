"""Move Spark-written Torch rows into date order on the retail-schedule Sheet.

New rows now insert in position, but rows written before that change sit at the
bottom of the workbook, below December, where nobody reading a chronological
schedule will find them.

SAFETY: only rows carrying a Spark Request UUID are ever touched. The client's
~2,100 own rows have no UUID and are never moved, rewritten or deleted — this
cannot reorder their data, only Spark's own additions to it.

A row is moved by deleting it and re-inserting at the right index, so the row's
values travel intact. Each move is one request, and the sheet is re-read
between moves because every delete/insert shifts the rows below it.

DRY-RUN by default. --apply writes.

Usage::

    python manage.py reflow_torch_sheet
    python manage.py reflow_torch_sheet --apply
"""

from __future__ import annotations

from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = "Move Spark-written Torch sheet rows into date order (dry-run default)."

    def add_arguments(self, parser):
        parser.add_argument(
            "--apply", action="store_true",
            help="Actually move rows (omit for a dry run that changes nothing).",
        )

    # ------------------------------------------------------------------

    def handle(self, *args, **opts):
        from utils import torch_public_form_sheet as T

        apply = bool(opts["apply"])
        sid = T.TORCH_PUBLIC_FORM_SHEET_ID
        gid = T.TORCH_PUBLIC_FORM_GID

        self.stdout.write("=" * 72)
        self.stdout.write(
            "REFLOW TORCH SHEET — Spark-written rows only\n"
            f"MODE: {'APPLY (moving rows)' if apply else 'DRY-RUN (no writes)'}"
        )
        self.stdout.write("=" * 72)

        svc = T._service()
        if not svc:
            self.stdout.write(self.style.ERROR("\n  No Sheets credentials."))
            return
        tab = T._tab_for_gid(svc, sid, gid)
        header = T._ensure_extra_headers(svc, sid, tab)

        try:
            ui = header.index(T.UUID_HEADER)
            di = header.index("Date")
        except ValueError:
            self.stdout.write(
                self.style.ERROR(f"\n  Missing {T.UUID_HEADER!r} or 'Date' column.")
            )
            return

        moved = 0
        for _ in range(200):  # bounded; one move per pass
            grid = (
                svc.spreadsheets()
                .values()
                .get(spreadsheetId=sid, range=T._qualify(tab, "A2:BZ"))
                .execute()
                .get("values", [])
            )

            dated = []
            for n, row in enumerate(grid, start=2):
                cell = row[di] if len(row) > di else ""
                d = T._parse_sheet_date(cell)
                if d is not None:
                    dated.append((n, d))

            target = None
            for n, d in dated:
                row = grid[n - 2]
                uuid = (row[ui] if len(row) > ui else "").strip()
                if not uuid:
                    continue  # client-owned row — never touched
                # Where would this row go if it were being inserted now?
                others = [(rn, dd) for rn, dd in dated if rn != n]
                want = T._insert_index_for_date(others, d)
                if want is None:
                    want = (others[-1][0] + 1) if others else n
                if want > n:
                    want -= 1  # removing this row shifts everything below up
                if want != n:
                    target = (n, want, d, uuid, row)
                    break

            if target is None:
                break

            n, want, d, uuid, row = target
            self.stdout.write(f"\n  row {n} ({d})  ->  row {want}   uuid={uuid[:8]}…")
            if not apply:
                self.stdout.write(
                    "  DRY-RUN — stopping after the first move; later positions "
                    "depend on this one actually happening."
                )
                return
            T._delete_row(svc, sid, gid, n)
            T._insert_row_at(svc, sid, tab, gid, want, row)
            moved += 1

        self.stdout.write("")
        self.stdout.write("=" * 72)
        if apply:
            self.stdout.write(self.style.SUCCESS(f"Moved {moved} row(s)."))
        else:
            self.stdout.write("DRY-RUN — nothing moved.")
        self.stdout.write("Client-owned rows (no Spark UUID) were never touched.")
        self.stdout.write("=" * 72)
