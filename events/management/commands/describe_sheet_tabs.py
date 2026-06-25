"""Read-only: enumerate a Google Sheet's tabs (title, row/col counts) and dump
row 1 of any tab that looks like a Master Tracker or Summary.

Used to confirm exact tab names before any write — e.g. the Liquid Death sheet's
first tab is a backup, so we must target the live Master Tracker / Summary by
name. Writes nothing.

    python manage.py describe_sheet_tabs --tenant-slug ighn-liquid-death
    python manage.py describe_sheet_tabs --sheet-url "https://docs.google.com/spreadsheets/d/<id>/edit"
"""
from __future__ import annotations

from django.core.management.base import BaseCommand, CommandError

from tenants.models import Tenant
from utils.sheets_mirror import _service, extract_sheet_id


class Command(BaseCommand):
    help = "Read-only: list a Google Sheet's tabs + row 1 of tracker/summary tabs."

    def add_arguments(self, parser):
        parser.add_argument("--sheet-url", type=str, default=None)
        parser.add_argument("--tenant-slug", type=str, default=None)
        parser.add_argument(
            "--peek-tab",
            type=str,
            default=None,
            help="Dump this tab's header + sample rows + a year histogram (read-only).",
        )
        parser.add_argument("--peek-rows", type=int, default=4)
        parser.add_argument(
            "--peek-render",
            type=str,
            default="FORMATTED_VALUE",
            help="Sheets valueRenderOption: FORMATTED_VALUE (default) or FORMULA.",
        )

    def handle(self, *args, **opts):
        url = opts.get("sheet_url")
        if not url and opts.get("tenant_slug"):
            t = Tenant.objects.filter(slug=opts["tenant_slug"]).first()
            if t is None:
                t = Tenant.objects.filter(name__icontains="liquid death").first()
            if t:
                url = t.linked_sheet_url or t.recap_export_sheet_url
        if not url:
            raise CommandError("Provide --sheet-url or --tenant-slug.")
        sheet_id = extract_sheet_id(url)
        if not sheet_id:
            raise CommandError(f"Could not parse a sheet id from {url!r}.")
        svc = _service()
        if svc is None:
            raise CommandError("No Sheets credentials (ADC).")

        meta = (
            svc.spreadsheets()
            .get(
                spreadsheetId=sheet_id,
                fields="sheets.properties(title,index,sheetId,gridProperties(rowCount,columnCount))",
            )
            .execute()
        )
        sheets = meta.get("sheets", [])
        self.stdout.write(f"{len(sheets)} tab(s) in {sheet_id}:")
        for s in sheets:
            p = s.get("properties", {})
            gp = p.get("gridProperties", {})
            self.stdout.write(
                f"  [{p.get('index')}] '{p.get('title')}' "
                f"(gid={p.get('sheetId')}, {gp.get('rowCount')}x{gp.get('columnCount')})"
            )

        self.stdout.write("\nRow 1 of tracker/summary-looking tabs:")
        for s in sheets:
            title = s.get("properties", {}).get("title", "")
            low = title.lower()
            if not any(k in low for k in ("summary", "master", "tracker")):
                continue
            try:
                resp = (
                    svc.spreadsheets()
                    .values()
                    .get(spreadsheetId=sheet_id, range=f"'{title}'!1:1")
                    .execute()
                )
                row1 = (resp.get("values") or [[]])[0]
                self.stdout.write(f"  '{title}' → {row1[:18]}")
            except Exception as e:  # pragma: no cover - diagnostic
                self.stdout.write(f"  '{title}' → (read failed: {e})")

        peek = opts.get("peek_tab")
        if peek:
            self._peek(
                svc, sheet_id, peek, max(1, opts.get("peek_rows") or 4),
                (opts.get("peek_render") or "FORMATTED_VALUE").strip().upper(),
            )

    def _peek(self, svc, sheet_id: str, tab: str, n: int, render: str = "FORMATTED_VALUE"):
        """Dump a tab's header + first n data rows, and a year histogram for any
        column whose header contains 'date'. render=FORMULA shows cell formulas
        (so #REF!/QUERY references are visible). Read-only."""
        import re
        from collections import Counter

        self.stdout.write(f"\nPeek '{tab}' (header + {n} rows, render={render}):")
        try:
            resp = (
                svc.spreadsheets()
                .values()
                .get(
                    spreadsheetId=sheet_id,
                    range=f"'{tab}'!1:{n + 1}",
                    valueRenderOption=render,
                )
                .execute()
            )
        except Exception as e:
            self.stdout.write(f"  (read failed: {e})")
            return
        values = resp.get("values") or []
        if not values:
            self.stdout.write("  (empty)")
            return
        header = values[0]
        for i, h in enumerate(header):
            self.stdout.write(f"  col[{i}] {h!r}")
        for r_i, row in enumerate(values[1:], start=2):
            self.stdout.write(f"  row{r_i}: {row}")

        # Year histogram for each date-like column (read the full column once).
        date_cols = [i for i, h in enumerate(header) if "date" in str(h).lower()]
        for ci in date_cols:
            col_letter = chr(ord("A") + ci) if ci < 26 else None
            if not col_letter:
                continue
            try:
                cresp = (
                    svc.spreadsheets()
                    .values()
                    .get(spreadsheetId=sheet_id, range=f"'{tab}'!{col_letter}2:{col_letter}100000")
                    .execute()
                )
            except Exception as e:
                self.stdout.write(f"  year histogram col[{ci}] failed: {e}")
                continue
            years: Counter = Counter()
            for cr in cresp.get("values") or []:
                cell = (cr[0] if cr else "") or ""
                m = re.search(r"(20\d\d)", str(cell))
                years[m.group(1) if m else "?"] += 1
            top = dict(sorted(years.items(), key=lambda kv: str(kv[0])))
            self.stdout.write(f"  year histogram col[{ci}] {header[ci]!r}: {top}")
