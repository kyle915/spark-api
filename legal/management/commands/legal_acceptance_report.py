"""List admin/client users and whether they accepted the current Terms.

    python manage.py legal_acceptance_report            # who HASN'T accepted
    python manage.py legal_acceptance_report --all      # everyone
    python manage.py legal_acceptance_report --csv out.csv

Read-only. "Pending" = never accepted, or accepted an older version.
"""

import csv
import sys

from django.core.management.base import BaseCommand

from legal import versions
from legal.service import acceptance_report


class Command(BaseCommand):
    help = "List active admin/client users who have not accepted the current legal documents."

    def add_arguments(self, parser):
        parser.add_argument("--all", action="store_true", help="Include users who already accepted.")
        parser.add_argument("--csv", metavar="PATH", help="Write rows to a CSV file ('-' for stdout).")
        parser.add_argument("--search", default="", help="Filter by email / name substring.")

    def handle(self, *args, **opts):
        rows = acceptance_report(pending_only=not opts["all"], search=opts["search"])
        everyone = acceptance_report(search=opts["search"])
        accepted = sum(1 for r in everyone if r.status == "accepted")

        if opts["csv"]:
            fh = sys.stdout if opts["csv"] == "-" else open(opts["csv"], "w", newline="")
            try:
                w = csv.writer(fh)
                w.writerow(["email", "name", "role", "status", "accepted_version", "accepted_at", "ip_address"])
                for r in rows:
                    w.writerow([r.email, r.name, r.role, r.status, r.accepted_version or "",
                                r.accepted_at.isoformat() if r.accepted_at else "", r.ip_address])
            finally:
                if fh is not sys.stdout:
                    fh.close()
        else:
            for r in rows:
                when = r.accepted_at.strftime("%Y-%m-%d %H:%M UTC") if r.accepted_at else "-"
                self.stdout.write(
                    f"{r.status:9} {r.email:40} {r.role:12} {r.accepted_version or '-':12} {when}"
                )

        self.stdout.write(
            self.style.SUCCESS(
                f"Current version {versions.current_version()}: {accepted} of {len(everyone)} "
                f"admin/client users accepted; {len(everyone) - accepted} pending."
            )
        )
