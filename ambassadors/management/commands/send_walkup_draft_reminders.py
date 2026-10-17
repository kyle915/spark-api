"""Email BAs whose walk-up recap draft never got submitted.

Hourly cron → `/internal/cron/walkup-draft-reminders` → this command.
One email per draft (``WalkupRecapDraft.reminder_sent_at``), a few hours
after the BA clocked out or the shift day passed, linking the standing
check-in on the client host where the draft restores. Filed recaps and
empty drafts are never reminded.

Usage:
    python manage.py send_walkup_draft_reminders
    python manage.py send_walkup_draft_reminders --grace-hours 3 --dry-run
"""

from __future__ import annotations

from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = (
        "Email one reminder to each BA with an unsubmitted walk-up recap "
        "draft from a finished shift (once per draft)."
    )

    def add_arguments(self, parser):
        parser.add_argument("--grace-hours", type=int, default=3)
        parser.add_argument("--max-age-hours", type=int, default=168)
        parser.add_argument("--dry-run", action="store_true")

    def handle(self, *args, **opts):
        from ambassadors.draft_reminders import send_walkup_draft_reminders

        counts = send_walkup_draft_reminders(
            grace_hours=opts["grace_hours"],
            max_age_hours=opts["max_age_hours"],
            dry_run=bool(opts["dry_run"]),
        )
        self.stdout.write(
            "walk-up draft reminders: "
            f"sent {counts['sent']}, candidates {counts['candidates']}, "
            f"skipped {counts['skipped_filed']} filed, "
            f"{counts['skipped_empty']} empty, "
            f"{counts['skipped_open_shift']} still on shift, "
            f"{counts['skipped_no_contact']} no contact/link, "
            f"failed {counts['failed']}"
            + (" (dry-run)" if opts["dry_run"] else "")
        )
