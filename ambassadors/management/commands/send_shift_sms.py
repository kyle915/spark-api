"""Text ambassadors about their shifts, from Ignite's Quo number.

Cron (every 10 minutes) → `/internal/cron/shift-sms` → this command. Sends
the pre-shift reminder two hours ahead, the missing-recap reminder 30
minutes after the shift, and one follow-up the next morning — each at most
once per ambassador per event. See ``ambassadors/shift_sms.py``.

Usage:
    python manage.py send_shift_sms
    python manage.py send_shift_sms --dry-run
"""

from __future__ import annotations

from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = "Send the shift texts that are due (pre-shift, recap due, recap next morning)."

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true")

    def handle(self, *args, **opts):
        from ambassadors.shift_sms import send_shift_sms

        dry_run = bool(opts["dry_run"])
        result = send_shift_sms(dry_run=dry_run)
        if not dry_run and not result["enabled"]:
            self.stdout.write(
                "shift sms: not sending — set SHIFT_SMS_ENABLED, QUO_API_KEY "
                "and QUO_FROM_NUMBER. Use --dry-run to see who is due."
            )
            return
        for line in result["preview"]:
            self.stdout.write(line)
        self.stdout.write(
            "shift sms: "
            + (
                f"would send {result['would_send']}"
                if dry_run
                else f"sent {result['sent']}, failed {result['failed']}"
            )
            + f", already sent {result['already_sent']}, skipped "
            f"{result['skipped_opt_out']} opted out, "
            f"{result['skipped_no_phone']} no usable phone, "
            f"{result['skipped_no_link']} no check-in link"
            + (" (dry-run)" if dry_run else "")
        )
