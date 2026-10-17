"""Write the admin weekly narration. Does not email anyone.

Dry-run is the default: it prints the fact packet (bucket keys + filed
recap count) and stores nothing. ``--apply`` calls the model and upserts
``TenantWeeklyNarration``. There is no mailer in this command.

    python manage.py generate_weekly_narration
    python manage.py generate_weekly_narration --apply
    python manage.py generate_weekly_narration --apply --tenant-id 12
"""

from __future__ import annotations

import json
from datetime import date

from django.core.management.base import BaseCommand, CommandError

from recaps.weekly_narration import previous_complete_week, upsert_narration
from tenants.models import Tenant


class Command(BaseCommand):
    help = "Store a weekly narration from insight buckets. Dry-run by default. Never emails."

    def add_arguments(self, parser):
        parser.add_argument(
            "--apply",
            action="store_true",
            help="Call the model and store the narration. Omit to dry-run.",
        )
        parser.add_argument("--tenant-id", type=int, default=None)
        parser.add_argument(
            "--week-start",
            type=str,
            default="",
            help="Monday YYYY-MM-DD. Default is the previous complete week.",
        )

    def handle(self, *args, **options):
        apply = bool(options["apply"])
        if options["week_start"]:
            try:
                start = date.fromisoformat(options["week_start"])
            except ValueError as exc:
                raise CommandError("--week-start must be YYYY-MM-DD") from exc
            if start.weekday() != 0:
                raise CommandError("--week-start must be a Monday")
            end = start.fromordinal(start.toordinal() + 6)
        else:
            start, end = previous_complete_week()

        if options["tenant_id"]:
            tenants = Tenant.objects.filter(id=options["tenant_id"])
        else:
            tenants = Tenant.active()

        written = 0
        for tenant in tenants.iterator():
            summary = upsert_narration(tenant.id, start, end, apply=apply)
            summary["tenant_name"] = tenant.name
            self.stdout.write(json.dumps(summary))
            if summary.get("written"):
                written += 1
        self.stdout.write(
            f"mode={'apply' if apply else 'dry_run'} tenants_written={written} emails_sent=0"
        )
