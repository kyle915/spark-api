"""Read-only: list recent Resend emails to one recipient with delivery status.

Pages through Resend's email list (newest first, 100 per page) and prints the
id, created time, subject, and last delivery event for every email whose
``to`` includes the address. Confirms a send was accepted and delivered (or
bounced / suppressed) without Cloud Logging access. Never sends anything.

Usage:
    python manage.py resend_recent_sends --recipient someone@example.com --pages 3
"""

from __future__ import annotations

from django.core.management.base import BaseCommand, CommandError

from utils.mailer import resend


class Command(BaseCommand):
    help = "Read-only: recent Resend emails to one recipient, with last delivery event."

    def add_arguments(self, parser):
        parser.add_argument("--recipient", required=True)
        parser.add_argument("--pages", type=int, default=3, help="Pages of 100 to scan (max 20).")

    def handle(self, *args, **opts):
        recipient = opts["recipient"].strip().lower()
        if "@" not in recipient:
            raise CommandError("--recipient must be an email address")
        pages = max(1, min(int(opts["pages"]), 20))

        w = self.stdout.write
        scanned = 0
        matches = 0
        cursor = None
        for _ in range(pages):
            params = {"limit": 100}
            if cursor:
                params["after"] = cursor
            page = resend.Emails.list(params)
            data = page.get("data") or []
            for email in data:
                scanned += 1
                to = [str(t).strip().lower() for t in (email.get("to") or [])]
                if recipient not in to:
                    continue
                matches += 1
                w(
                    f"  {email.get('created_at')}  id={email.get('id')}  "
                    f"last_event={email.get('last_event')}  to={len(to)}  "
                    f"subject={email.get('subject')!r}"
                )
            if not page.get("has_more") or not data:
                break
            cursor = data[-1].get("id")
        w(f"Scanned {scanned} recent emails; {matches} to {recipient}.")
