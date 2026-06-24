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
