"""Django management command to run the no-show backup cascade watchdog.

Usage:
    python manage.py check_noshow_cascades
    python manage.py check_noshow_cascades --checkin-grace-minutes 15 --unconfirmed-lead-minutes 120
    python manage.py check_noshow_cascades --dry-run
"""

from __future__ import annotations

import json

from django.core.management.base import BaseCommand

from staffing.cascade import evaluate_noshow_cascades


class Command(BaseCommand):
    help = (
        "Detect shifts unconfirmed near start or missing check-in 15+ minutes "
        "past start, create NoShowCascadeAlert records, and dispatch backup alerts."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--checkin-grace-minutes",
            type=int,
            default=15,
            help="Trigger missing-check-in cascade once shift started this many minutes ago (default 15).",
        )
        parser.add_argument(
            "--unconfirmed-lead-minutes",
            type=int,
            default=120,
            help="Trigger unconfirmed-near-start cascade when shift starts within this many minutes (default 120).",
        )
        parser.add_argument(
            "--lookback-hours",
            type=int,
            default=6,
            help="Ignore shifts that started more than this many hours ago (default 6).",
        )
        parser.add_argument(
            "--backup-limit",
            type=int,
            default=5,
            help="Maximum number of top-ranked backup BAs to alert per shift (default 5).",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Evaluate candidates without creating alerts or sending notifications.",
        )

    def handle(self, *args, **options):
        summary = evaluate_noshow_cascades(
            checkin_grace_minutes=options["checkin_grace_minutes"],
            unconfirmed_lead_minutes=options["unconfirmed_lead_minutes"],
            lookback_hours=options["lookback_hours"],
            backup_limit=options["backup_limit"],
            dry_run=bool(options["dry_run"]),
        )
        self.stdout.write(json.dumps(summary))
