"""Stamp Pivot Retail Schedule row 2 Sent and silence older reminder copies.

The 2026-10-22 status write targeted column AX, past the sheet's 41-column
grid, so the email left and the status cell stayed blank. Re-checking Send
looked like a new booking. This command records Sent on the real columns
and turns day-before / few-hours reminders off on every older duplicate of
that shift. It does not send email and does not remint a check-in code.

Idempotent. Safe to run on every deploy.
"""

from django.core.management.base import BaseCommand

from events.sheet_event_confirmations import repair_pivot_retail_row2


class Command(BaseCommand):
    help = (
        "Stamp Pivot Retail Schedule row 2 Sent and silence older "
        "duplicate reminders. Does not email."
    )

    def handle(self, *args, **options):
        result = repair_pivot_retail_row2()
        self.stdout.write(
            "pivot row 2 repair "
            f"matches={result['matches']} "
            f"mailed={result['mailed']} "
            f"reminders_updated={result['reminders_updated']} "
            f"newest={result['newest_uuid'] or '-'} "
            f"ids={result['confirmation_ids']} "
            f"stamp={result['stamp']}"
        )
