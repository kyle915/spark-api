"""One-off data fix for Neutonic's geographic-performance map.

Two corrections, each GUARDED so it can only touch the exact rows we
diagnosed (safe to re-run; idempotent):

  1. The real Austin Costco recap's event has no State FK, so the geo map
     (which buckets by event.state.code) silently dropped it. Set that
     event's state to TX. Guard: Neutonic recap whose event address
     contains "Austin, TX" and whose event has NO state yet.

  2. A leftover "TEST event Costco" recap in Jacksonville, FL is polluting
     the map with 650 consumers. Delete that CustomRecap and its child rows.
     Guard: Neutonic recap whose event name contains "TEST" and whose event
     state is FL.

Dry-run by default; pass --apply to write. Bounded to the Neutonic tenant.
"""
from __future__ import annotations

from django.core.management.base import BaseCommand
from django.db import transaction


class Command(BaseCommand):
    help = (
        "Fix Neutonic geo data: set the Austin event's state to TX and "
        "delete the leftover FL 'TEST event Costco' recap. Dry-run unless --apply."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--apply",
            action="store_true",
            help="Actually write. Without this, only report what WOULD change.",
        )

    def handle(self, *args, **opts):
        from events.models import State
        from recaps.models import (
            CustomFieldValue,
            CustomRecap,
            CustomRecapFile,
            CustomRecapProductSample,
        )
        from tenants.models import Tenant

        apply = bool(opts["apply"])
        w = self.stdout.write

        tenant = Tenant.objects.filter(slug="neutonic").first()
        if tenant is None:
            w("no tenant with slug 'neutonic' — nothing to do.")
            return

        w(f"fix_neutonic_geo: tenant='{tenant.name}' apply={apply}")

        # --- 1. Austin Costco: set the event's state to TX ------------------
        tx = State.objects.filter(code__iexact="TX").first()
        state_fixed = 0
        austin = CustomRecap.objects.select_related("event", "event__state").filter(
            tenant=tenant,
            event__address__icontains="Austin, TX",
            event__state__isnull=True,
        )
        for r in austin:
            ev = r.event
            w(
                f"  [state] recap #{r.id} event #{ev.id} '{ev.name}' "
                f"addr={ev.address!r} state=None -> TX"
            )
            if apply and tx is not None:
                ev.state = tx
                ev.save(update_fields=["state"])
                state_fixed += 1
        if tx is None:
            w("  [state] WARNING: no State row with code 'TX' found — skipped.")

        # --- 2. Delete the leftover FL 'TEST event Costco' recap ------------
        deleted = 0
        test = CustomRecap.objects.select_related("event", "event__state").filter(
            tenant=tenant,
            event__name__icontains="TEST",
            event__state__code__iexact="FL",
        )
        for r in test:
            ev = r.event
            cfv = CustomFieldValue.objects.filter(custom_recap=r).count()
            w(
                f"  [delete] recap #{r.id} event '{ev.name}' state=FL "
                f"addr={ev.address!r} ({cfv} field values)"
            )
            if apply:
                with transaction.atomic():
                    CustomFieldValue.objects.filter(custom_recap=r).delete()
                    CustomRecapProductSample.objects.filter(custom_recap=r).delete()
                    CustomRecapFile.objects.filter(custom_recap=r).delete()
                    r.delete()
                deleted += 1

        w(
            f"done. apply={apply} state_fixed={state_fixed} "
            f"recaps_deleted={deleted} tx_state_found={tx is not None}"
        )
