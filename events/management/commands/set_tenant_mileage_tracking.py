"""Bulk-toggle GPS mileage tracking on a tenant's events.

The per-gig toggle (Event.track_mileage + mileage_rate) is designed for the
admin EventMileagePanel, one event at a time — useless when a whole imported
season (e.g. Feel Free's 249 field samplings) should reimburse mileage. This
flips the flag in one scoped UPDATE.

Scope: --tenant-name (iexact, same resolution as import_event_schedule)
narrowed by --event-type (name, iexact) and/or --since-date. Uses a queryset
.update() on purpose: no per-row save() → no post_save side effects (calendar
sync, mirrors) fired 249 times for a config flip.

Dry-run by default — prints how many events match and how many are already
on. Pass --apply to write. Run in prod via the secret-gated
SetTenantMileageTrackingView (/internal/cron/set-tenant-mileage-tracking) +
the set-tenant-mileage-tracking workflow.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from events.models import Event
from tenants.models import Tenant


class Command(BaseCommand):
    help = (
        "Bulk-enable (or disable) mileage tracking + $/mile rate on a "
        "tenant's events. Dry-run by default; pass --apply to write."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--tenant-name",
            required=True,
            help="Tenant by name (case-insensitive), e.g. 'Feel Free'.",
        )
        parser.add_argument(
            "--event-type",
            default=None,
            help="Only events of this EventType name (case-insensitive), "
            "e.g. 'Field Sampling'. Default: every event on the tenant.",
        )
        parser.add_argument(
            "--since-date",
            default=None,
            help="Only events dated YYYY-MM-DD or later.",
        )
        parser.add_argument(
            "--rate",
            default="0.725",
            help="$/mile reimbursement rate (3 decimals; default 0.725). "
            "Pass 'none' to track miles only, no dollar amount.",
        )
        parser.add_argument(
            "--off",
            action="store_true",
            help="Disable tracking on the matched events instead.",
        )
        parser.add_argument(
            "--apply",
            action="store_true",
            help="Actually write. Without this flag nothing changes.",
        )

    def handle(self, *args, **opts):
        tenant = (
            Tenant.objects.filter(name__iexact=opts["tenant_name"].strip())
            .order_by("id")
            .first()
        )
        if not tenant:
            raise CommandError(f"Tenant not found: {opts['tenant_name']!r}")

        enable = not opts["off"]
        rate: Decimal | None = None
        if enable and (opts["rate"] or "").strip().lower() != "none":
            try:
                rate = Decimal(opts["rate"].strip())
            except InvalidOperation:
                raise CommandError(f"Bad --rate: {opts['rate']!r}")

        qs = Event.objects.filter(tenant=tenant)
        if opts["event_type"]:
            qs = qs.filter(event_type__name__iexact=opts["event_type"].strip())
        if opts["since_date"]:
            qs = qs.filter(date__gte=opts["since_date"])

        total = qs.count()
        already = qs.filter(track_mileage=enable).count()
        w = self.stdout.write
        w("")
        w(f"tenant      : {tenant.id} ({tenant.name})")
        w(f"scope       : event_type={opts['event_type'] or '(any)'} "
          f"since={opts['since_date'] or '(any)'}")
        w(f"action      : {'ENABLE' if enable else 'DISABLE'} tracking"
          + (f" @ ${rate}/mile" if rate is not None else " (miles only)" if enable else ""))
        w(f"matched     : {total}")
        w(f"already set : {already}")

        if not opts["apply"]:
            w(self.style.MIGRATE_LABEL(
                "DRY-RUN — nothing written. Re-run with --apply (execute=true)."
            ))
            return

        fields = {"track_mileage": enable, "updated_at": timezone.now()}
        if enable:
            fields["mileage_rate"] = rate
        changed = qs.update(**fields)
        w(self.style.SUCCESS(f"updated     : {changed}"))
