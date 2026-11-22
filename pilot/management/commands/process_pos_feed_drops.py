"""Process pending automated POS CSV feed drops.

Runs ``import_retailer_pos_csv`` for each pending ``PosFeedDrop``. Safe to
call frequently — empty queues exit quickly.

Usage:
    python manage.py process_pos_feed_drops
    python manage.py process_pos_feed_drops --tenant 12 --limit 20
"""

from __future__ import annotations

from django.core.management.base import BaseCommand

from pilot.ingest import process_pending_pos_feed_drops


class Command(BaseCommand):
    help = "Import pending PosFeedDrop CSV payloads into RetailerSalesImport."

    def add_arguments(self, parser):
        parser.add_argument(
            "--tenant",
            type=int,
            default=None,
            help="Restrict processing to a single tenant id.",
        )
        parser.add_argument(
            "--limit",
            type=int,
            default=50,
            help="Max pending drops to process in this run (default 50).",
        )

    def handle(self, *args, **opts):
        counts = process_pending_pos_feed_drops(
            tenant_id=opts.get("tenant"),
            limit=int(opts.get("limit") or 50),
        )
        self.stdout.write(
            self.style.SUCCESS(
                "POS feed drops — "
                f"pending={counts['pending']} processed={counts['processed']} "
                f"failed={counts['failed']} skipped={counts['skipped']}."
            )
        )
