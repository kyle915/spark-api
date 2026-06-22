"""Per-recap "consumers sampled" breakdown for one tenant — a read-only
diagnostic to explain the dashboard's "Consumers reached" total.

The Program-KPIs / hero "consumers" figure is the SUM of each recap's
consumers-sampled value across the window (legacy ``ConsumerEngagements
.total_consumer`` + custom "consumers sampled" field via the shared
``recaps.types._consumers_sampled_from_fields``). When that total looks too
high, this command shows EXACTLY which recaps contribute, so we can tell
"legit (lots of varied events)" from "stray/duplicate rows".

It also flags the one real over-count risk: an event that has BOTH a legacy
``Recap`` AND a custom ``CustomRecap`` — those get summed twice.

READ-ONLY. No writes. Window defaults to the current calendar year (matching
the dashboard's "This year"); ``--all-time`` sums everything; ``--year N``
picks a year.

Run via the ``/internal/cron/audit-tenant-consumers`` endpoint (or the
``Audit tenant consumers`` GitHub Action) so it executes against prod.
"""

from __future__ import annotations

from django.core.management.base import BaseCommand, CommandError

from events.models import Event
from recaps.models import ConsumerEngagements, CustomFieldValue, CustomRecap, Recap
from recaps.tenant_overview import _filter_event_window, _year_bounds
from recaps.types import _consumers_sampled_from_fields
from tenants.models import Tenant


def _resolve_tenant(ident: str) -> Tenant:
    """Find a tenant by numeric id, exact request-url-name, or name (icontains)."""
    if ident.isdigit():
        t = Tenant.objects.filter(id=int(ident)).first()
        if t:
            return t
    t = Tenant.objects.filter(request_url_name__iexact=ident).first()
    if t:
        return t
    matches = list(Tenant.objects.filter(name__icontains=ident)[:5])
    if len(matches) == 1:
        return matches[0]
    if not matches:
        raise CommandError(f"No tenant matches {ident!r}.")
    names = ", ".join(f"{m.id}:{m.name}" for m in matches)
    raise CommandError(f"{ident!r} is ambiguous — matches: {names}. Use the id.")


class Command(BaseCommand):
    help = "Read-only per-recap consumers-sampled breakdown for one tenant."

    def add_arguments(self, parser):
        parser.add_argument("--tenant", required=True, help="id, request-url-name, or name")
        parser.add_argument("--year", type=int, default=None, help="calendar year (default: current)")
        parser.add_argument("--all-time", action="store_true", help="ignore the year window")

    def handle(self, *args, **opts):
        tenant = _resolve_tenant(opts["tenant"])
        if opts["all_time"]:
            window = None
            label = "all-time"
        else:
            from django.utils import timezone
            year = opts["year"] or timezone.now().year
            window = _year_bounds(year)
            label = str(year)

        self.stdout.write(f"Consumers audit — {tenant.name} (id {tenant.id}) · {label}")
        self.stdout.write("=" * 64)

        # --- CUSTOM recaps: consumers-sampled per recap via the shared matcher.
        cfv = _filter_event_window(
            CustomFieldValue.objects.filter(custom_recap__tenant_id=tenant.id),
            "custom_recap__event__",
            window,
        ).values_list(
            "custom_recap_id", "custom_recap__name", "custom_recap__event_id",
            "custom_recap__total_engagements", "custom_field__name", "value",
        )
        by_recap: dict = {}
        for rid, rname, eid, eng, fname, val in cfv.iterator():
            row = by_recap.setdefault(
                rid, {"name": rname, "event_id": eid, "eng": eng, "pairs": []}
            )
            row["pairs"].append((fname, val))

        custom_total = 0
        custom_event_ids: set = set()
        self.stdout.write("\nCUSTOM recaps  [consumers-sampled | total_engagements]")
        for rid, row in sorted(by_recap.items()):
            sampled = _consumers_sampled_from_fields(row["pairs"]) or 0
            custom_total += int(sampled)
            if row["event_id"]:
                custom_event_ids.add(row["event_id"])
            self.stdout.write(
                f"  recap {rid} · {(row['name'] or '')[:38]:38} "
                f"sampled={int(sampled):>6}  eng={row['eng'] or 0:>6}"
            )
        self.stdout.write(f"  -> custom recaps: {len(by_recap)}  consumers-sampled SUM = {custom_total}")

        # --- LEGACY recaps: ConsumerEngagements.total_consumer per recap.
        legacy_recaps = _filter_event_window(
            Recap.objects.filter(event__tenant_id=tenant.id), "event__", window
        )
        legacy_ids = list(legacy_recaps.values_list("id", "name", "event_id"))
        legacy_total = 0
        legacy_event_ids: set = set()
        self.stdout.write("\nLEGACY recaps  [total_consumer]")
        for rid, rname, eid in legacy_ids:
            tc = (
                ConsumerEngagements.objects.filter(recap_id=rid)
                .values_list("total_consumer", flat=True)
            )
            s = sum(int(x or 0) for x in tc)
            legacy_total += s
            if eid:
                legacy_event_ids.add(eid)
            self.stdout.write(f"  recap {rid} · {(rname or '')[:38]:38} consumers={s:>6}")
        self.stdout.write(f"  -> legacy recaps: {len(legacy_ids)}  total_consumer SUM = {legacy_total}")

        # --- Double-count check: events with BOTH a legacy and a custom recap.
        both = custom_event_ids & legacy_event_ids
        self.stdout.write("\n" + "=" * 64)
        self.stdout.write(f"GRAND TOTAL consumers (custom {custom_total} + legacy {legacy_total}) = {custom_total + legacy_total}")
        self.stdout.write(f"distinct events with a recap: {len(custom_event_ids | legacy_event_ids)}")
        if both:
            ev = ", ".join(str(e) for e in sorted(both))
            self.stdout.write(
                self.style.WARNING(
                    f"⚠ {len(both)} event(s) have BOTH a legacy AND custom recap — "
                    f"their consumers are counted TWICE: events {ev}"
                )
            )
        else:
            self.stdout.write("no event has both a legacy and custom recap (no double-count).")
