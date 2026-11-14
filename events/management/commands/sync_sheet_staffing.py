"""Refresh Spark's read-only mirror of who the staffing sheets have working.

Cron (every 15 minutes) → `/internal/cron/sync-sheet-staffing` → this
command. See ``events/sheet_staffing.py``. Writes to no sheet; books nobody.

Usage:
    python manage.py sync_sheet_staffing
    python manage.py sync_sheet_staffing --dry-run
"""

from __future__ import annotations

from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = "Read the staffing sheets and refresh the SheetShift mirror."

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true")

    def handle(self, *args, **opts):
        from events.sheet_staffing import sync_sheet_staffing

        result = sync_sheet_staffing(dry_run=bool(opts["dry_run"]))
        for line in result["lines"]:
            self.stdout.write(line)
        self.stdout.write(
            f"sheet staffing: {result['sources']} tabs read, {result['failed']} "
            f"unreadable, {result['rows']} staffed rows — {result['matched']} "
            f"matched, {result['no_event']} with no Spark event, "
            f"{result['ambiguous']} ambiguous"
            + (" (dry-run, nothing stored)" if result["dry_run"] else "")
        )
