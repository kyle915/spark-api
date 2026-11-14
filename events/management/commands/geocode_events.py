"""Look up a map position for events that have an address but no coordinates.

    python manage.py geocode_events [--tenant ID] [--days-back 2]
        [--days-ahead 14] [--limit 200] [--dry-run]

Why: the staff "Today" map can only draw an event that has a position. Most
events carry a street address and no coordinates, so a morning with nobody
clocked in yet is an empty map.

What it does: for events dated in the window, with a real street address and
no usable position, asks Google once where the address is and stores the
answer in ``EventMapPoint``. It does NOT write ``Event.coordinates`` — that
column drives clock-in distance checks, nearby-gig pushes and recap roll-ups,
which must not start acting on a looked-up guess.

Safe to run as often as you like:
  * an event that already has coordinates, or a stored point for its current
    address, is left alone;
  * an address Google cannot place is remembered ("not_found") and not asked
    again until the address text changes;
  * two events at the same address cost one lookup;
  * a failed call (no key, quota, network) stores nothing and is retried on
    the next run; three failures in a row end the run early.

Events on deleted requests are skipped. Prints event and tenant ids only.
"""

from __future__ import annotations

from datetime import timedelta

from django.core.management.base import BaseCommand

from events import geocoding
from events.map_points import address_hash, is_lookup_worthy, own_point

# Stop asking after this many failed calls in a row — the service is down or
# the key is refused, and the rest of the run would fail the same way.
MAX_CONSECUTIVE_FAILURES = 3


class Command(BaseCommand):
    help = (
        "Look up map positions (EventMapPoint) for events with an address and "
        "no coordinates. Never writes Event.coordinates."
    )

    def add_arguments(self, parser):
        parser.add_argument("--tenant", type=int, default=None,
                            help="Only this tenant id.")
        parser.add_argument("--days-back", type=int, default=2)
        parser.add_argument("--days-ahead", type=int, default=14)
        parser.add_argument("--limit", type=int, default=200,
                            help="Most events to look up in one run.")
        parser.add_argument("--dry-run", action="store_true",
                            help="List what would be looked up; store nothing.")

    def handle(self, *args, **opts):
        from events.live_board import _ops_today
        from events.models import Event, EventMapPoint

        dry_run = bool(opts.get("dry_run"))
        limit = max(0, int(opts.get("limit") or 0))
        today = _ops_today()
        start = today - timedelta(days=max(0, int(opts.get("days_back") or 0)))
        end = today + timedelta(days=max(0, int(opts.get("days_ahead") or 0)))

        self.stdout.write(geocoding.geocoding_status())
        self.stdout.write(
            f"Events dated {start.isoformat()} to {end.isoformat()}"
            + (f", tenant {opts['tenant']}" if opts.get("tenant") else ", all tenants")
            + (" — DRY RUN, nothing is stored" if dry_run else "")
        )

        qs = (
            Event.objects.filter(date__date__gte=start, date__date__lte=end)
            .exclude(request__deleted_at__isnull=False)
            .exclude(address__isnull=True)
            .exclude(address="")
            .only("id", "tenant_id", "address", "coordinates", "date")
            .order_by("date", "id")
        )
        if opts.get("tenant"):
            qs = qs.filter(tenant_id=opts["tenant"])
        events = list(qs)

        stored = {
            row.event_id: row
            for row in EventMapPoint.objects.filter(
                event_id__in=[ev.id for ev in events]
            )
        }

        has_position = 0
        no_street_address = 0
        todo: list[tuple] = []  # (event, hash)
        for ev in events:
            if own_point(ev) is not None:
                has_position += 1
                continue
            digest = address_hash(ev.address)
            row = stored.get(ev.id)
            if row is not None and row.address_hash == digest:
                has_position += 1  # found, or known not findable
                continue
            if not is_lookup_worthy(ev.address):
                no_street_address += 1
                self.stdout.write(
                    f"event {ev.id} tenant {ev.tenant_id}: skipped (no street address)"
                )
                continue
            todo.append((ev, digest))

        over_limit = max(0, len(todo) - limit)
        todo = todo[:limit]

        # An answer already held for the same address (another event at the
        # same store) is reused rather than paid for twice.
        known: dict[str, tuple[str, float | None, float | None]] = {}
        if todo:
            for row in EventMapPoint.objects.filter(
                address_hash__in={digest for _ev, digest in todo}
            ).order_by("updated_at"):
                known[row.address_hash] = (row.status, row.lat, row.lng)

        found = not_found = reused = failed = would = 0
        streak = 0
        stopped = ""
        for ev, digest in todo:
            label = f"event {ev.id} tenant {ev.tenant_id}"
            if digest in known:
                status, lat, lng = known[digest]
                if dry_run:
                    would += 1
                    self.stdout.write(f"{label}: would reuse a stored answer ({status})")
                    continue
                self._store(ev, digest, status, lat, lng)
                reused += 1
                self.stdout.write(f"{label}: reused a stored answer ({status})")
                continue
            if dry_run:
                would += 1
                self.stdout.write(f"{label}: would look up")
                continue
            if stopped:
                failed += 1
                self.stdout.write(f"{label}: not looked up ({stopped})")
                continue

            outcome, point, detail = geocoding.lookup_address(ev.address)
            if outcome == geocoding.OK and point is not None:
                self._store(ev, digest, EventMapPoint.STATUS_OK, point[0], point[1])
                known[digest] = (EventMapPoint.STATUS_OK, point[0], point[1])
                found += 1
                streak = 0
                self.stdout.write(f"{label}: found")
            elif outcome == geocoding.NOT_FOUND:
                self._store(ev, digest, EventMapPoint.STATUS_NOT_FOUND, None, None)
                known[digest] = (EventMapPoint.STATUS_NOT_FOUND, None, None)
                not_found += 1
                streak = 0
                self.stdout.write(f"{label}: not found ({detail})")
            else:
                failed += 1
                streak += 1
                self.stdout.write(f"{label}: lookup failed, will retry next run ({detail})")
                if outcome == geocoding.NO_KEY:
                    stopped = "no Google key"
                elif streak >= MAX_CONSECUTIVE_FAILURES:
                    stopped = "Google lookups are failing"

        if dry_run:
            summary = (
                f"Dry run: {would} event(s) would be looked up, "
                f"{has_position} already settled, "
                f"{no_street_address} with no street address"
            )
        else:
            summary = (
                f"Done: {found} found, {not_found} not found, {reused} reused, "
                f"{failed} failed (retried next run), "
                f"{has_position} already settled, "
                f"{no_street_address} with no street address"
            )
        if over_limit:
            summary += f", {over_limit} left for the next run (limit {limit})"
        self.stdout.write(summary + ".")

    @staticmethod
    def _store(event, digest, status, lat, lng) -> None:
        from events.models import EventMapPoint

        EventMapPoint.objects.update_or_create(
            event_id=event.id,
            defaults={
                "lat": lat,
                "lng": lng,
                "source": EventMapPoint.SOURCE_GEOCODED,
                "address_hash": digest,
                "status": status,
            },
        )
