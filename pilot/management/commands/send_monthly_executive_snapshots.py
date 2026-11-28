from __future__ import annotations

import json

from django.core.management.base import BaseCommand

from pilot.executive_snapshot import run_monthly_executive_snapshots


class Command(BaseCommand):
    help = "Send 1st-of-month Executive Snapshot briefs + PDFs to opted-in brand workspaces."

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run",
            action="store_true",
            default=False,
            help="Build snapshot summaries without emailing recipients.",
        )
        parser.add_argument(
            "--force",
            action="store_true",
            default=False,
            help="Send even if already sent for this month or activity is empty.",
        )
        parser.add_argument(
            "--tenant",
            type=int,
            default=None,
            help="Restrict to a single tenant ID.",
        )
        parser.add_argument(
            "--month",
            type=str,
            default="",
            help="Override reporting month as YYYY-MM (defaults to prior complete month).",
        )

    def handle(self, *args, **options):
        year = None
        month = None
        raw_month = (options.get("month") or "").strip()
        if raw_month:
            try:
                y_s, m_s = raw_month.split("-", 1)
                year = int(y_s)
                month = int(m_s)
            except ValueError as exc:
                raise ValueError("--month must be formatted as YYYY-MM") from exc

        result = run_monthly_executive_snapshots(
            dry_run=bool(options.get("dry_run")),
            force=bool(options.get("force")),
            tenant_id=options.get("tenant"),
            year=year,
            month=month,
        )
        self.stdout.write(json.dumps(result, indent=2, default=str))
