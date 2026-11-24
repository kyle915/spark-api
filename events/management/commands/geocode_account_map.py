"""Give Account Map requests a position when nothing else does.

    python manage.py geocode_account_map [--tenant ID] [--limit 300]
        [--sleep 0.1] [--apply]

SAFE: DRY RUN BY DEFAULT. Without ``--apply`` it only counts and lists what
it would look up, and makes no Google calls at all.

Why: the Account Map draws a request from the request's own coordinates, else
from one of its events (the event's coordinates, or the ``EventMapPoint``
that ``geocode_events`` looks up for events dated a couple of days back to two
weeks ahead). A request with a street address but no coordinates and no
placed event — an older request whose events predate the event cron, or a
future one with no event yet — is never drawn.

What it does with ``--apply``: for each such request (soft-deleted ones are
skipped), asks Google once where its address is and stores the answer in
``RequestMapPoint``. It never writes ``Request.coordinates``.

  * an answer already held for the same address — from another request, an
    event's ``EventMapPoint`` or a brand's uploaded account list — is reused
    and costs no lookup;
  * an address Google cannot place is remembered (``not_found``) and not asked
    again until its text changes;
  * ``--sleep`` seconds pass between Google calls (rate limit);
  * no key / quota / network failures store nothing and are retried next run;
    three in a row end the run early.

Prints request and tenant ids only, never addresses.
"""

from __future__ import annotations

import time
from collections import Counter

from django.core.management.base import BaseCommand

from events import geocoding
from events.map_points import address_hash, event_map_points, is_lookup_worthy, own_point

MAX_CONSECUTIVE_FAILURES = 3


class Command(BaseCommand):
    help = (
        "Look up Account Map positions (RequestMapPoint) for requests with an "
        "address and no position. Dry run unless --apply."
    )

    def add_arguments(self, parser):
        parser.add_argument("--tenant", type=int, default=None, help="Only this tenant id.")
        parser.add_argument(
            "--limit", type=int, default=300, help="Most requests to look up in one run."
        )
        parser.add_argument(
            "--sleep",
            type=float,
            default=0.1,
            help="Seconds to wait between Google lookups.",
        )
        parser.add_argument(
            "--apply", action="store_true", help="Look up and store. Without it: dry run."
        )
        parser.add_argument(
            "--verbose-ids",
            action="store_true",
            help="List each request id and what would happen to it.",
        )

    # ── finding the work ─────────────────────────────────────────────

    def _candidates(self, tenant_id):
        """Requests the map cannot draw today, plus why the rest are skipped."""
        from events.models import Event, Request, RequestMapPoint

        qs = (
            Request.objects.filter(deleted_at__isnull=True)
            .only("id", "tenant_id", "address", "coordinates")
            .order_by("id")
        )
        if tenant_id:
            qs = qs.filter(tenant_id=tenant_id)
        requests = list(qs)

        unplaced = [r for r in requests if own_point(r) is None]
        events = list(
            Event.objects.filter(request_id__in=[r.pk for r in unplaced])
            .only("id", "request_id", "coordinates", "address")
        )
        placed_by_event = {
            ev.request_id for ev in events if ev.id in event_map_points(events)
        }
        stored = {
            row.request_id: row
            for row in RequestMapPoint.objects.filter(
                request_id__in=[r.pk for r in unplaced]
            )
        }

        tally = Counter(total=len(requests))
        todo = []  # (request, digest)
        for req in requests:
            if own_point(req) is not None:
                tally["has_own_position"] += 1
                continue
            if req.pk in placed_by_event:
                tally["placed_by_event"] += 1
                continue
            digest = address_hash(req.address)
            row = stored.get(req.pk)
            if row is not None and row.address_hash == digest:
                tally["already_looked_up"] += 1
                continue
            if not is_lookup_worthy(req.address):
                tally["no_street_address"] += 1
                continue
            todo.append((req, digest))
        return todo, tally

    @staticmethod
    def _known_answers(digests) -> dict[str, tuple[str, float | None, float | None]]:
        """Answers already held for these address hashes, from any table."""
        from events.models import AccountListEntry, EventMapPoint, RequestMapPoint

        known: dict[str, tuple[str, float | None, float | None]] = {}
        if not digests:
            return known
        for model in (EventMapPoint, RequestMapPoint):
            for row in model.objects.filter(address_hash__in=digests).order_by("updated_at"):
                if row.status == model.STATUS_OK and geocoding.valid_lat_lng(row.lat, row.lng):
                    known[row.address_hash] = (model.STATUS_OK, row.lat, row.lng)
                elif row.status == model.STATUS_NOT_FOUND:
                    known.setdefault(row.address_hash, (model.STATUS_NOT_FOUND, None, None))
        for h, lat, lng in AccountListEntry.objects.filter(
            address_hash__in=digests,
            position_status__in=(
                AccountListEntry.POSITION_OK,
                AccountListEntry.POSITION_PROVIDED,
            ),
        ).values_list("address_hash", "lat", "lng"):
            if geocoding.valid_lat_lng(lat, lng):
                known[h] = ("ok", lat, lng)
        return known

    # ── running ──────────────────────────────────────────────────────

    def handle(self, *args, **opts):
        from events.models import RequestMapPoint

        apply = bool(opts.get("apply"))
        tenant_id = opts.get("tenant")
        limit = max(0, int(opts.get("limit") or 0))
        pause = max(0.0, float(opts.get("sleep") or 0.0))
        verbose = bool(opts.get("verbose_ids"))

        self.stdout.write(geocoding.geocoding_status())
        self.stdout.write(
            ("APPLY" if apply else "DRY RUN — nothing is looked up or stored")
            + (f", tenant {tenant_id}" if tenant_id else ", all tenants")
        )

        todo, tally = self._candidates(tenant_id)
        known = self._known_answers({digest for _r, digest in todo})
        reusable = [(r, d) for r, d in todo if d in known]
        to_look_up = [(r, d) for r, d in todo if d not in known]
        distinct_addresses = len({d for _r, d in to_look_up})
        by_tenant = Counter(r.tenant_id for r, _d in todo)

        self.stdout.write(
            f"Requests: {tally['total']} live. "
            f"{tally['has_own_position']} have their own position, "
            f"{tally['placed_by_event']} are placed by an event, "
            f"{tally['already_looked_up']} were already looked up, "
            f"{tally['no_street_address']} have no street address."
        )
        self.stdout.write(
            f"Without a position: {len(todo)} request(s) — "
            f"{len(reusable)} can reuse a stored answer, "
            f"{len(to_look_up)} need a lookup "
            f"({distinct_addresses} distinct address(es))."
        )
        for tid, n in sorted(by_tenant.items(), key=lambda kv: (-kv[1], kv[0])):
            self.stdout.write(f"  tenant {tid}: {n}")

        if not apply:
            if verbose:
                for req, digest in todo:
                    what = "reuse" if digest in known else "look up"
                    self.stdout.write(f"request {req.pk} tenant {req.tenant_id}: would {what}")
            self.stdout.write(
                f"Dry run: {len(reusable)} would reuse, "
                f"{min(distinct_addresses, limit)} Google lookup(s) within --limit {limit}."
            )
            return

        stored_ok = stored_not_found = reused = failed = 0
        lookups = streak = 0
        stopped = ""
        for req, digest in reusable + to_look_up:
            label = f"request {req.pk} tenant {req.tenant_id}"
            if digest in known:
                status, lat, lng = known[digest]
                self._store(req, digest, status, lat, lng)
                reused += 1
                continue
            if stopped or lookups >= limit:
                failed += 1 if stopped else 0
                continue
            if lookups and pause:
                time.sleep(pause)
            lookups += 1
            outcome, point, detail = geocoding.lookup_address(req.address)
            if outcome == geocoding.OK and point is not None:
                self._store(req, digest, RequestMapPoint.STATUS_OK, point[0], point[1])
                known[digest] = (RequestMapPoint.STATUS_OK, point[0], point[1])
                stored_ok += 1
                streak = 0
            elif outcome == geocoding.NOT_FOUND:
                self._store(req, digest, RequestMapPoint.STATUS_NOT_FOUND, None, None)
                known[digest] = (RequestMapPoint.STATUS_NOT_FOUND, None, None)
                stored_not_found += 1
                streak = 0
                self.stdout.write(f"{label}: not found ({detail})")
            else:
                failed += 1
                streak += 1
                self.stdout.write(f"{label}: lookup failed, retried next run ({detail})")
                if outcome == geocoding.NO_KEY:
                    stopped = "no Google key"
                elif streak >= MAX_CONSECUTIVE_FAILURES:
                    stopped = "Google lookups are failing"
        left = sum(1 for _r, d in to_look_up if d not in known)
        self.stdout.write(
            f"Done: {stored_ok} found, {stored_not_found} not found, {reused} reused, "
            f"{failed} failed, {lookups} Google lookup(s), {left} left for the next run"
            + (f" (stopped: {stopped})" if stopped else "")
            + "."
        )

    @staticmethod
    def _store(request, digest, status, lat, lng) -> None:
        from events.models import RequestMapPoint

        RequestMapPoint.objects.update_or_create(
            request_id=request.pk,
            defaults={
                "lat": lat,
                "lng": lng,
                "source": RequestMapPoint.SOURCE_GEOCODED,
                "address_hash": digest,
                "status": status,
            },
        )
