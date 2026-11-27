"""Send due scheduled SavedReportView digest emails (e.g. Friday distributor exports)."""

from __future__ import annotations

from datetime import date

from django.core.management.base import BaseCommand, CommandError

from pilot.saved_views import send_due_saved_view_digests


class Command(BaseCommand):
    help = "Email scheduled SavedReportView digests due today (CSV + summary)."

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true")
        parser.add_argument("--force", action="store_true")
        parser.add_argument("--tenant", type=int, default=None)
        parser.add_argument("--view-uuid", default="")
        parser.add_argument("--day", default="", help="YYYY-MM-DD override for testing")

    def handle(self, *args, **opts):
        ref_day: date | None = None
        if (opts.get("day") or "").strip():
            try:
                ref_day = date.fromisoformat(opts["day"].strip()[:10])
            except ValueError as exc:
                raise CommandError(f"Bad --day: {opts['day']!r}") from exc

        res = send_due_saved_view_digests(
            dry_run=bool(opts["dry_run"]),
            force=bool(opts["force"]),
            tenant_id=opts.get("tenant"),
            view_uuid=(opts.get("view_uuid") or "").strip(),
            today=ref_day,
        )
        self.stdout.write(
            self.style.SUCCESS(
                f"Saved view digests done: checked={res['checked']} sent={res['sent']} "
                f"skipped={res['skipped']} failed={res['failed']}."
            )
        )
