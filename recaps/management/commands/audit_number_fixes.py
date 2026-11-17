"""Read-only: what the recap number fixes change, per brand.

See :mod:`recaps.number_fixes_audit`. Three sections per tenant: approved-only
(campaign reports + client Home tiles), give-away wording no longer counted as
sold, and answers the one number reader reads differently.

    python manage.py audit_number_fixes                 # every tenant
    python manage.py audit_number_fixes --tenant-id 17  # one tenant
    python manage.py audit_number_fixes --no-reports    # skip the report rebuild
    python manage.py audit_number_fixes --json

READ-ONLY. Field names, counts and short numeric answers only.
"""
from __future__ import annotations

import json

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from recaps.number_fixes_audit import build_number_fixes_audit

JSON_START = "NUMBERFIXES_JSON_START"
JSON_END = "NUMBERFIXES_JSON_END"


class Command(BaseCommand):
    help = "Read-only: per-brand impact of the approved-only / give-away / number-parsing fixes."

    def add_arguments(self, parser):
        parser.add_argument("--tenant-id", type=int, default=None)
        parser.add_argument(
            "--no-reports", action="store_true",
            help="skip rebuilding every campaign report (the slow part)",
        )
        parser.add_argument(
            "--json", action="store_true", dest="as_json",
            help=f"print the payload between {JSON_START}/{JSON_END} markers",
        )

    def handle(self, *args, **opts):
        from tenants.models import Tenant

        qs = Tenant.objects.order_by("id")
        if opts.get("tenant_id") is not None:
            qs = qs.filter(id=opts["tenant_id"])
            if not qs.exists():
                raise CommandError(f"No tenant with id {opts['tenant_id']}.")

        tenants = []
        for tenant in qs:
            row = build_number_fixes_audit(
                tenant.id, include_reports=not opts.get("no_reports")
            )
            row["tenant_name"] = tenant.name
            tenants.append(row)
            self._print(row)

        if opts.get("as_json"):
            self.stdout.write(JSON_START)
            self.stdout.write(json.dumps(
                {"generated_at": timezone.now().isoformat(), "tenants": tenants},
                default=str,
            ))
            self.stdout.write(JSON_END)

    def _print(self, row):
        sold = row["sold_wording"]
        parsing = row["number_parsing"]
        approval = row.get("approved_only")
        moved = (
            sold["recaps_that_change"]
            or parsing["answers_read_differently"]
            or (approval and (
                approval["campaign_reports_all_requests"]["requests_that_change"]
                or approval["client_home_last_30_days"]["recaps_no_longer_counted"]
            ))
        )
        self.stdout.write(f"\n{row['tenant_name']} (#{row['tenant_id']})" + ("" if moved else " — no change"))
        if not moved:
            return
        if approval:
            rep = approval["campaign_reports_all_requests"]
            b, a = rep["before"], rep["after"]
            self.stdout.write(
                f"  approved-only: {rep['requests_that_change']} campaign report(s) change; "
                f"consumers {b.get('consumers_reached', 0):,} -> {a.get('consumers_reached', 0):,}, "
                f"sold {b.get('products_sold', 0):,} -> {a.get('products_sold', 0):,}, "
                f"recaps {b.get('recaps', 0):,} -> {a.get('recaps', 0):,}"
            )
            home = approval["client_home_last_30_days"]
            self.stdout.write(
                f"  client Home (30d): {home['recaps_no_longer_counted']} recap(s), "
                f"{home['consumers_no_longer_counted']:,} consumers no longer counted"
            )
        if sold["recaps_that_change"]:
            u = sold["units_sold_client_visible"]
            self.stdout.write(
                f"  give-away != sold: {sold['recaps_that_change']} recap(s); "
                f"units sold (approved) {u['before']:,} -> {u['after']:,}"
            )
            for f in sold["fields_no_longer_sold"][:5]:
                self.stdout.write(f"    - {f['field']!r}: {f['recaps']} recaps, {f['units']:,} units")
        if parsing["answers_read_differently"]:
            self.stdout.write(
                f"  number reader: {parsing['answers_read_differently']} of "
                f"{parsing['answers_checked']} KPI answers read differently; net "
                f"{parsing['net_change_vs_strip_reader']:+,} vs list/dashboard reader, "
                f"{parsing['net_change_vs_first_digits_reader']:+,} vs report reader"
            )
            for ex in parsing["examples"][:4]:
                self.stdout.write(
                    f"    {ex['value']!r}: was {ex['old_strip']} / {ex['old_first_digits']}, now {ex['new']}"
                )
