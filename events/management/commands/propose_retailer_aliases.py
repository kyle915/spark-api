"""Look for store names that are the same store written differently.

For each tenant, groups the store names its events and requests carry and
stores what looks like the same store as PROPOSED aliases for staff to accept
or dismiss (Store names page). Changes no chart, retailer, request or event:
only accepted aliases are read by reports. Safe to re-run: a name that has a
decision is never proposed again, and the model is only asked about names it
has not seen.

    python manage.py propose_retailer_aliases --dry-run
    python manage.py propose_retailer_aliases --tenant 17 --no-ai
"""

import time

from django.core.management.base import BaseCommand
from django.db.models import Max

from events.retailer_aliases import propose_for_tenant
from tenants.models import Tenant


def tenants_to_check(tenant_id=None):
    """Tenants that have events, the ones looked at longest ago first."""
    qs = Tenant.objects.filter(events__isnull=False).distinct()
    if tenant_id:
        qs = Tenant.objects.filter(pk=tenant_id)
    return qs.annotate(_last=Max("retailer_aliases__updated_at")).order_by(
        "_last", "id"
    )


class Command(BaseCommand):
    help = "Suggest store names that are the same store (aliases for staff to accept)."

    def add_arguments(self, parser):
        parser.add_argument("--tenant", type=int, default=None)
        parser.add_argument("--limit", type=int, default=25, help="Most tenants per run.")
        parser.add_argument(
            "--max-seconds",
            type=int,
            default=200,
            help="Stop starting new tenants after this long, so a web-triggered "
            "run answers before the request times out. 0 = no limit.",
        )
        parser.add_argument("--dry-run", action="store_true")
        parser.add_argument(
            "--no-ai",
            action="store_true",
            help="Only group names that agree once case and punctuation are ignored.",
        )

    def handle(self, *args, **opts):
        dry = opts["dry_run"]
        tenants = list(tenants_to_check(opts["tenant"])[: opts["limit"]])
        budget = opts.get("max_seconds") or 0
        started = time.monotonic()
        looked = groups = 0
        for tenant in tenants:
            if budget and time.monotonic() - started > budget:
                self.stdout.write(
                    f"Stopped after {budget}s with {len(tenants) - looked} tenants "
                    "left for the next run."
                )
                break
            looked += 1
            try:
                result = propose_for_tenant(
                    tenant.id, use_ai=not opts["no_ai"], dry_run=dry
                )
            except Exception as exc:  # noqa: BLE001 — one tenant must not stop the run
                self.stderr.write(f"tenant {tenant.id}: {exc}")
                continue
            groups += result.groups
            if result.groups or result.refused:
                ai = "with AI" if result.ai_used else "rules only"
                self.stdout.write(
                    f"tenant {tenant.id}: {result.names} names, {result.groups} groups "
                    f"({ai}), {result.refused} AI groups refused"
                )
        verb = "would suggest" if dry else "suggested"
        self.stdout.write(f"{looked} tenants looked at, {verb} {groups} groups.")
