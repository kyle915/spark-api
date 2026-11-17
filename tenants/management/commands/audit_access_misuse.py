"""Read-only misuse review for the access holes closed in #1424/#1426/#1427.

Reports, for the window before each fix, what the database can still show:
accounts created (elevated ones by email), deactivations, password-change
proxies, tenant memberships added, tenant moves, attendance written by
someone other than an admin or the BA, payroll close-out by non-admins.
See :mod:`tenants.misuse_audit`.

    python manage.py audit_access_misuse
    python manage.py audit_access_misuse --start 2026-06-01 --end 2026-11-16T18:00:00Z --json

READ-ONLY. Ambassadors and clients appear as ids; emails only for accounts
that carry an admin marker.
"""
from __future__ import annotations

import json

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from tenants.misuse_audit import DEFAULT_ROW_CAP, build_misuse_audit, parse_when

JSON_START = "MISUSEAUDIT_JSON_START"
JSON_END = "MISUSEAUDIT_JSON_END"


class Command(BaseCommand):
    help = "Read-only: what the DB shows about use of the access holes before they were closed."

    def add_arguments(self, parser):
        parser.add_argument("--start", default=None, help="YYYY-MM-DD or ISO time (default 2026-06-01)")
        parser.add_argument(
            "--end", default=None,
            help="Override every section's end (default: each fix PR's merge time)",
        )
        parser.add_argument("--cap", type=int, default=DEFAULT_ROW_CAP, help="max rows per list")
        parser.add_argument(
            "--json", action="store_true", dest="as_json",
            help=f"print the payload between {JSON_START}/{JSON_END} markers",
        )

    def handle(self, *args, **opts):
        try:
            start = parse_when(opts.get("start"))
            end = parse_when(opts.get("end"))
        except ValueError as exc:
            raise CommandError(f"bad date (expected YYYY-MM-DD or ISO): {exc}") from exc
        cap = max(1, min(int(opts.get("cap") or DEFAULT_ROW_CAP), 5000))

        payload = build_misuse_audit(start=start, end=end, cap=cap)
        payload["generated_at"] = timezone.now().isoformat()

        self.stdout.write("Access misuse review (read-only)")
        for key, value in payload["window"].items():
            self.stdout.write(f"  {key:<14} {value}")
        for key, value in payload["summary"].items():
            self.stdout.write(f"  {key:<44} {value}")

        if opts.get("as_json"):
            self.stdout.write(JSON_START)
            self.stdout.write(json.dumps(payload, default=str))
            self.stdout.write(JSON_END)
