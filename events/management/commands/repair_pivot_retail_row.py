"""Stamp mailed Pivot rows whose Confirmation Status is still blank.

The AX status write was past the 41-column Retail Schedule grid, so the
email left and the status cell stayed blank. This command scans that tab
and, for each blank row whose booked email already left, records Sent and
turns reminders off on older duplicates of that shift. A blank row that
was never mailed is left alone. It does not send email and does not
remint a check-in code.

Idempotent. Safe to run on every deploy.
"""

from django.core.management.base import BaseCommand

from events.sheet_event_confirmations import repair_pivot_blank_status_rows


class Command(BaseCommand):
    help = (
        "Stamp mailed Pivot Retail Schedule rows Sent when status is blank, "
        "and silence older duplicate reminders. Does not email."
    )

    def handle(self, *args, **options):
        result = repair_pivot_blank_status_rows()
        self.stdout.write(
            "pivot blank-status repair "
            f"scanned={result['scanned']} repaired={len(result['repaired'])}"
        )
        for item in result["repaired"]:
            self.stdout.write(
                f"row={item['row_number']} "
                f"store={item['store_name']} "
                f"matches={item['matches']} "
                f"mailed={item['mailed']} "
                f"reminders_updated={item['reminders_updated']} "
                f"newest={item['newest_uuid'] or '-'} "
                f"ids={item['confirmation_ids']} "
                f"stamp={item['stamp']}"
            )
