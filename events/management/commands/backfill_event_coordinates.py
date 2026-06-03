"""
Backfill: populate ``Event.coordinates`` (``[lat, lng]``) for events whose
coordinates are missing — null, empty (``[]``), or the ``[0, 0]`` null-island
sentinel.

Why this exists (and why it's NOT a migration):
    The "new gig nearby" distance push (and the Today / Account map pins) need
    real ``Event.coordinates``. Events materialized from a Request BEFORE the
    coordinate-copy fix landed in ``events/managers.py:from_request`` (which now
    does ``coordinates=getattr(request, "coordinates", None) or None``), plus
    any event whose request itself had no coordinates, were left with empty
    coordinates — so distance can't be computed and the proximity push is
    silent for them.

    The forward fixes (web public form + mobile profile sending coordinates,
    and from_request copying them onto new events) are handled elsewhere. This
    command repairs the EXISTING backlog of rows.

    It is NOT a migration because the target rows are tenant data and the work
    is per-row + best-effort over the network; a one-shot data migration would
    run once at deploy across all environments, whereas this can be previewed
    (dry-run), scoped per tenant, re-run idempotently against prod separately,
    and triggered manually from GitHub Actions. Mirrors the repair_* command
    trio (same flags, per-tenant transaction, idempotency, error capture,
    summary + per-tenant breakdown logging).

How it fills each event — two sources, cheapest first:
    1. COPY from the parent ``Request.coordinates`` when that's populated and
       valid (free, no network). This is the common case and runs first so we
       never hit Photon for a row we can fill for free.
    2. GEOCODE ``Event.address`` via the keyless Photon API
       (``utils.geocoding.photon_geocode``) for the rest. Best-effort: a small
       ``time.sleep`` between network calls keeps us polite to the free
       service, the call has a timeout, and a failure/no-result just SKIPS the
       row (logged, counted) rather than aborting the run.

    Saves ONLY the ``coordinates`` field (``update_fields=["coordinates"]``).

Idempotent + wrapped in a transaction per tenant:
    A row that already has valid coordinates is skipped (the candidate
    queryset excludes them, and a re-check guards the single-row helper), so a
    second run updates zero rows. Each written row gets its own savepoint so
    one failure doesn't poison the surrounding tenant transaction.

Flags (identical surface to repair_event_dates):
    --dry-run        Report what WOULD change; write nothing. DEFAULT: ON.
    --execute        Actually write coordinates (turns OFF the dry-run default).
    --tenant <x>     Scope to a single tenant (slug or numeric id). Default: all.

Usage:
    python manage.py backfill_event_coordinates                      # DRY RUN
    python manage.py backfill_event_coordinates --tenant liquid-death  # dry run, one tenant
    python manage.py backfill_event_coordinates --execute            # APPLY, all tenants
    python manage.py backfill_event_coordinates --tenant liquid-death --execute
"""

from __future__ import annotations

import logging
import time
import traceback

from django.core.management.base import BaseCommand, CommandError
from django.db import models, transaction

from events.models import Event
from tenants.models import Tenant
from utils.geocoding import has_valid_coordinates, photon_geocode

logger = logging.getLogger(__name__)

# Politeness delay between Photon network calls (the copy-from-request path
# does NOT sleep — it never touches the network).
GEOCODE_SLEEP_SECONDS = 1.0


def _format_exc(exc: BaseException) -> str:
    """Concise, log-safe one-liner for a failed row: exception type + message
    + the LAST traceback frame. Mirrors the helper in repair_event_dates."""
    type_name = type(exc).__name__
    message = " ".join(str(exc).split())
    if len(message) > 300:
        message = message[:297] + "..."
    frame = ""
    tb = exc.__traceback__
    if tb is not None:
        last = traceback.extract_tb(tb)[-1]
        filename = last.filename.split("/")[-1]
        frame = f" [{filename}:{last.lineno} in {last.name}]"
    return f"{type_name}: {message}{frame}"


def _coords_from_request(event: Event) -> list[float] | None:
    """Return valid ``[lat, lng]`` copied from the parent request, or None.

    Free (no network). ``events/managers.py:from_request`` shows the
    Request→Event relationship: an event materialized from a request carries
    ``event.request``; the request's own ``coordinates`` is what we copy.
    """
    request = getattr(event, "request", None)
    if request is None:
        return None
    coords = getattr(request, "coordinates", None)
    if has_valid_coordinates(coords):
        # Normalise to a plain list of two floats.
        return [float(coords[0]), float(coords[1])]
    return None


class Command(BaseCommand):
    help = (
        "Backfill Event.coordinates for events with missing coordinates "
        "(null/empty/[0,0]). Copies from the parent Request.coordinates when "
        "valid (free), else geocodes Event.address via the keyless Photon API. "
        "Idempotent + transaction-wrapped. DRY-RUN by default — pass --execute "
        "to write. Supports --tenant."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run",
            action="store_true",
            default=True,
            help=(
                "Report what WOULD change without writing to the DB. "
                "This is the DEFAULT; pass --execute to actually write."
            ),
        )
        parser.add_argument(
            "--execute",
            action="store_true",
            default=False,
            help=(
                "Actually write coordinates (disables the dry-run default). "
                "Required to write to the DB."
            ),
        )
        parser.add_argument(
            "--tenant",
            default=None,
            help=(
                "Scope to a single tenant by slug or numeric id. "
                "Default: all tenants."
            ),
        )

    # ─── Entry point ────────────────────────────────────────────────

    def handle(self, *args, **opts):
        execute: bool = opts["execute"]
        dry_run: bool = not execute
        tenant_arg: str | None = opts["tenant"]

        tenants = self._resolve_tenants(tenant_arg)

        self.stdout.write(self.style.MIGRATE_HEADING(
            "\nBackfill Event.coordinates "
            f"({'ALL tenants' if tenant_arg is None else f'tenant={tenant_arg}'})"
        ))
        if dry_run:
            self.stdout.write(self.style.NOTICE("DRY RUN — no DB writes.\n"))

        grand_candidates = 0
        grand_updated = 0
        grand_from_request = 0
        grand_geocoded = 0
        grand_failed = 0
        all_errors: list[dict] = []
        per_tenant: list[dict] = []

        for tenant in tenants:
            stats = self._process_tenant(tenant, dry_run=dry_run)
            grand_candidates += stats["candidates"]
            grand_updated += stats["updated"]
            grand_from_request += stats["from_request"]
            grand_geocoded += stats["geocoded"]
            grand_failed += stats["failed"]
            all_errors.extend(stats.get("errors", []))
            per_tenant.append({"tenant": tenant, **stats})

        # ─── Summary ────────────────────────────────────────────────
        verb = "Would update" if dry_run else "Updated"
        self.stdout.write(self.style.MIGRATE_HEADING("\nSummary"))
        self.stdout.write(f"  Tenants scanned:            {len(tenants)}")
        self.stdout.write(
            f"  Candidate events (coordinates null/empty/[0,0]): "
            f"{grand_candidates}"
        )
        self.stdout.write(self.style.SUCCESS(
            f"  {verb}: {grand_updated} event(s) "
            f"(from request={grand_from_request}, geocoded={grand_geocoded})"
        ))
        if grand_failed:
            self.stdout.write(self.style.WARNING(
                f"  Skipped/failed (no usable source): {grand_failed} event(s) "
                "— re-run to retry"
            ))
            for err in all_errors:
                self.stdout.write(self.style.ERROR(
                    f"    - event id={err['event_id']}: {err['error']}"
                ))

        self.stdout.write(self.style.MIGRATE_HEADING("\nPer-tenant breakdown"))
        for row in per_tenant:
            t = row["tenant"]
            self.stdout.write(
                f"  {t.name} (id={t.id}, slug={t.slug}): "
                f"candidates={row['candidates']} {verb.lower()}={row['updated']} "
                f"(request={row['from_request']}, geocoded={row['geocoded']})"
                + (f" failed={row['failed']}" if row["failed"] else "")
            )

        if not dry_run and grand_updated:
            logger.info(
                "backfill_event_coordinates: updated=%s (request=%s geocoded=%s) "
                "failed=%s tenants=%s",
                grand_updated, grand_from_request, grand_geocoded,
                grand_failed, len(tenants),
            )

    # ─── Per-tenant processing ──────────────────────────────────────

    def _process_tenant(self, tenant: Tenant, *, dry_run: bool) -> dict:
        stats = {
            "candidates": 0,
            "updated": 0,
            "from_request": 0,
            "geocoded": 0,
            "failed": 0,
            "errors": [],
        }

        # Target events: coordinates null OR empty OR [0,0], scoped to tenant.
        # select_related("request") so the copy-first path doesn't N+1.
        candidates_qs = (
            Event.objects.filter(tenant_id=tenant.id)
            .filter(
                models.Q(coordinates__isnull=True)
                | models.Q(coordinates=[])
                | models.Q(coordinates=[0, 0])
                | models.Q(coordinates=[0.0, 0.0])
            )
            .select_related("request")
            .order_by("id")
        )
        candidates = list(candidates_qs)
        # The ORM filter is a coarse pre-filter; re-check in Python so a row
        # with, say, a single-element array is also treated correctly.
        candidates = [
            ev for ev in candidates if not has_valid_coordinates(ev.coordinates)
        ]
        stats["candidates"] = len(candidates)

        header = (
            f"\n  Tenant '{tenant.name}' (id={tenant.id}, slug={tenant.slug}) "
            f"— {len(candidates)} candidate event(s)"
        )
        self.stdout.write(self.style.HTTP_INFO(header))

        if not candidates:
            return stats

        for ev in candidates:
            self._process_event(ev, dry_run=dry_run, stats=stats)

        return stats

    def _process_event(self, ev: Event, *, dry_run: bool, stats: dict) -> None:
        """Resolve coordinates for one event (copy-first, then geocode) and
        write them (unless dry-run). Updates ``stats`` in place."""
        coords = _coords_from_request(ev)
        source = "request"
        if coords is None:
            # No usable request coords → geocode the address via Photon.
            # Sleep BEFORE the network call to space out requests politely
            # (skipped entirely for the free copy-from-request path above).
            address = (getattr(ev, "address", None) or "").strip()
            if not address:
                stats["failed"] += 1
                stats["errors"].append(
                    {"event_id": ev.id, "error": "no request coords and no address"}
                )
                self.stdout.write(self.style.WARNING(
                    f"    ? skip event id={ev.id}: no request coords, no address"
                ))
                return
            if not dry_run:
                time.sleep(GEOCODE_SLEEP_SECONDS)
            coords = photon_geocode(address)
            source = "geocode"
            if coords is None:
                stats["failed"] += 1
                stats["errors"].append(
                    {"event_id": ev.id, "error": f"geocode miss for {address!r}"}
                )
                self.stdout.write(self.style.WARNING(
                    f"    ? skip event id={ev.id}: geocode returned nothing for "
                    f"{address!r}"
                ))
                return

        # We have coordinates. Count the source.
        if source == "request":
            stats["from_request"] += 1
        else:
            stats["geocoded"] += 1

        if dry_run:
            stats["updated"] += 1
            self.stdout.write(
                f"    ~ would set coordinates={coords} on event id={ev.id} "
                f"(uuid={getattr(ev, 'uuid', None)}, source={source})"
            )
            return

        error = self._save_coords(ev, coords)
        if error is None:
            stats["updated"] += 1
            self.stdout.write(self.style.SUCCESS(
                f"    ✓ set coordinates={coords} on event id={ev.id} "
                f"(source={source})"
            ))
        else:
            # The source counter was incremented optimistically; back it out
            # so the summary reflects only successful writes.
            if source == "request":
                stats["from_request"] -= 1
            else:
                stats["geocoded"] -= 1
            stats["failed"] += 1
            stats["errors"].append({"event_id": ev.id, "error": error})
            self.stdout.write(self.style.ERROR(
                f"    ! failed event id={ev.id}: {error}"
            ))

    # ─── Single-event write ─────────────────────────────────────────

    def _save_coords(self, event: Event, coords: list[float]) -> str | None:
        """Write ``coords`` to the event, saving ONLY the coordinates field in
        its own savepoint. Returns None on success (or idempotent skip) or a
        concise error string."""
        # Idempotent guard — re-check inside the write path.
        if has_valid_coordinates(event.coordinates):
            return None
        try:
            with transaction.atomic():
                event.coordinates = coords
                event.save(update_fields=["coordinates"])
        except Exception as exc:  # noqa: BLE001 — surface the cause in report
            logger.exception(
                "backfill_event_coordinates: failed to set coords for event_id=%s",
                event.id,
            )
            return _format_exc(exc)
        return None

    # ─── Helpers ────────────────────────────────────────────────────

    def _resolve_tenants(self, tenant_arg: str | None) -> list[Tenant]:
        """Resolve --tenant (slug or numeric id) to a list of tenants.
        Default (None) → all tenants ordered by id."""
        if tenant_arg is None:
            tenants = list(Tenant.objects.all().order_by("id"))
            if not tenants:
                raise CommandError("No tenants found.")
            return tenants

        tenant: Tenant | None = None
        if tenant_arg.isdigit():
            tenant = Tenant.objects.filter(id=int(tenant_arg)).first()
        if tenant is None:
            tenant = Tenant.objects.filter(slug=tenant_arg).first()
        if tenant is None:
            raise CommandError(
                f"No tenant matching '{tenant_arg}' (tried id then slug)."
            )
        return [tenant]
