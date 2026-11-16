"""Read-only per-recap audit of a tenant's LEGACY recaps.

Answers what ``campaign-to-date`` can't: each legacy recap's approval state, and
which ones sit outside a live request (soft-deleted request / no request) and so
never reach a campaign roll-up. See :mod:`recaps.legacy_recap_audit`.

    python manage.py audit_legacy_recaps --tenant-id 1
    python manage.py audit_legacy_recaps --tenant-id 1 --start 2026-01-01 --end 2026-11-16 --json

READ-ONLY. Ambassadors appear as opaque ids only.
"""
from __future__ import annotations

import json

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from recaps.legacy_recap_audit import build_legacy_recap_audit

JSON_START = "LEGACYRECAPS_JSON_START"
JSON_END = "LEGACYRECAPS_JSON_END"


class Command(BaseCommand):
    help = "Read-only: one row per legacy recap with request link, approval and KPI fields."

    def add_arguments(self, parser):
        parser.add_argument("--tenant-id", type=int, required=True)
        parser.add_argument("--start", default=None, help="YYYY-MM-DD inclusive (EVENT date)")
        parser.add_argument("--end", default=None, help="YYYY-MM-DD inclusive (EVENT date)")
        parser.add_argument(
            "--json", action="store_true", dest="as_json",
            help=f"print the payload between {JSON_START}/{JSON_END} markers",
        )

    def handle(self, *args, **opts):
        from tenants.models import Tenant

        tenant = Tenant.objects.filter(id=opts["tenant_id"]).first()
        if tenant is None:
            raise CommandError(f"No tenant with id {opts['tenant_id']}.")
        try:
            payload = build_legacy_recap_audit(tenant.id, start=opts.get("start"), end=opts.get("end"))
        except ValueError as exc:
            raise CommandError(f"bad date (expected YYYY-MM-DD): {exc}") from exc
        payload["tenant_name"] = tenant.name
        payload["generated_at"] = timezone.now().isoformat()

        self.stdout.write(f"{tenant.name} (#{tenant.id}) — legacy recap audit (read-only)")
        w = payload["window"]
        self.stdout.write(f"  window: {w['start'] or 'earliest'} → {w['end'] or 'latest'} (EVENT date)")
        self.stdout.write(f"  rows:   {payload['row_count']}")
        for key, value in payload["summary"].items():
            self.stdout.write(f"  {key:<32} {value}")

        if opts.get("as_json"):
            self.stdout.write(JSON_START)
            self.stdout.write(json.dumps(payload, default=str))
            self.stdout.write(JSON_END)
