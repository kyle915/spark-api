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
       valid (free, no network). This is the common case.
    2. GEOCODE ``Event.address`` via the keyless Photon API
       (``utils.geocoding.photon_geocode``) for the rest. Best-effort: a small
       ``time.sleep`` between network calls keeps us polite to the free
       service, the call has a timeout, and a failure/no-result just SKIPS the
       row (logged, counted) rather than aborting the run.

    Saves ONLY the ``coordinates`` field (``update_fields=["coordinates"]``).

Two-phase EXECUTE (so a single request finishes well under the Cloud Run 300s
timeout — the bug that PR #732 originally shipped geocoded the WHOLE backlog
inline with a 1s sleep per address and 504'd):
    * Phase 1 (no network, do ALL): copy ``Request.coordinates`` → event for
      every candidate whose parent request has valid coords. Free, instant.
    * Phase 2 (network, CAPPED at ``--limit``, default 40): for rows that still
      need coords, geocode the address via Photon — but only up to ``limit`` of
      them per invocation, keeping the 1s polite sleep. With limit=40 the
      request takes ~40s + overhead, safely under 300s. The report says how
      many still REMAIN so the operator re-runs until 0.

Count-only DRY-RUN:
    Dry-run NEVER touches the network. It only COUNTS: total candidates, how
    many are fillable by the free Request→Event copy, and how many would need a
    geocode. Returns instantly regardless of backlog size.

Idempotent + wrapped in a transaction per tenant:
    A row that already has valid coordinates is skipped (the candidate
    queryset excludes them, and a re-check guards the single-row helper), so a
    second run only drains whatever geocode backlog is left. Each written row
    gets its own savepoint so one failure doesn't poison the surrounding
    tenant transaction.

Flags:
    --dry-run        Report counts only (no network); write nothing. DEFAULT: ON.
    --execute        Actually write coordinates (turns OFF the dry-run default).
    --tenant <x>     Scope to a single tenant (slug or numeric id). Default: all.
    --limit <n>      Max addresses to GEOCODE per invocation (Phase 2 cap).
                     DEFAULT: 40. The free copy phase is never capped.

Usage:
    python manage.py backfill_event_coordinates                      # DRY RUN (counts)
    python manage.py backfill_event_coordinates --tenant liquid-death  # dry run, one tenant
    python manage.py backfill_event_coordinates --execute            # APPLY, all tenants
    python manage.py backfill_event_coordinates --execute --limit 100
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

# Default cap on how many addresses we GEOCODE per invocation (Phase 2). Keeps
# a single request under the Cloud Run 300s timeout: ~limit seconds of sleeps +
# the per-call timeout + overhead. The free copy phase is never capped.
DEFAULT_GEOCODE_LIMIT = 40


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
        "(null/empty/[0,0]). Phase 1 copies from the parent Request.coordinates "
        "when valid (free, no network, ALL rows); Phase 2 geocodes Event.address "
        "via the keyless Photon API for the rest, CAPPED at --limit per run "
        "(default 40) to stay under the Cloud Run timeout. Idempotent + "
        "transaction-wrapped. DRY-RUN by default (COUNTS ONLY, no network) — "
        "pass --execute to write. Supports --tenant."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run",
            action="store_true",
            default=True,
            help=(
                "Report counts only (no network, no DB writes). "
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
        parser.add_argument(
            "--limit",
            type=int,
            default=DEFAULT_GEOCODE_LIMIT,
            help=(
                "Max addresses to GEOCODE per invocation (Phase 2 cap). "
                f"Default: {DEFAULT_GEOCODE_LIMIT}. The free copy-from-request "
                "phase is never capped. Re-run until 'remaining' hits 0."
            ),
        )

    # ─── Entry point ────────────────────────────────────────────────

    def handle(self, *args, **opts):
        execute: bool = opts["execute"]
        dry_run: bool = not execute
        tenant_arg: str | None = opts["tenant"]
        limit: int = opts.get("limit") or DEFAULT_GEOCODE_LIMIT
        if limit < 0:
            raise CommandError("--limit must be >= 0")

        tenants = self._resolve_tenants(tenant_arg)

        self.stdout.write(self.style.MIGRATE_HEADING(
            "\nBackfill Event.coordinates "
            f"({'ALL tenants' if tenant_arg is None else f'tenant={tenant_arg}'})"
        ))
        if dry_run:
            self.stdout.write(self.style.NOTICE(
                "DRY RUN — counts only, NO network, NO DB writes.\n"
            ))
        else:
            self.stdout.write(self.style.NOTICE(
                f"EXECUTE — Phase 1 copies all from request; "
                f"Phase 2 geocodes at most {limit} this run.\n"
            ))

        # Phase-2 geocode budget is shared ACROSS tenants so a single
        # invocation geocodes at most `limit` addresses total (the network
        # cost is what we're capping, regardless of how it's split per tenant).
        budget = {"remaining": limit}

        grand_candidates = 0
        grand_copyable = 0          # fillable by Request→Event copy (free)
        grand_needs_geocode = 0     # candidates that require a geocode
        grand_copied = 0            # actually copied (Phase 1)
        grand_geocoded = 0          # actually geocoded (Phase 2)
        grand_failed = 0
        grand_remaining = 0         # still need a geocode after this run
        all_errors: list[dict] = []
        per_tenant: list[dict] = []

        for tenant in tenants:
            stats = self._process_tenant(
                tenant, dry_run=dry_run, budget=budget
            )
            grand_candidates += stats["candidates"]
            grand_copyable += stats["copyable"]
            grand_needs_geocode += stats["needs_geocode"]
            grand_copied += stats["copied"]
            grand_geocoded += stats["geocoded"]
            grand_failed += stats["failed"]
            grand_remaining += stats["remaining"]
            all_errors.extend(stats.get("errors", []))
            per_tenant.append({"tenant": tenant, **stats})

        # ─── Summary ────────────────────────────────────────────────
        self.stdout.write(self.style.MIGRATE_HEADING("\nSummary"))
        self.stdout.write(f"  Tenants scanned:            {len(tenants)}")
        self.stdout.write(
            f"  Candidate events (coordinates null/empty/[0,0]): "
            f"{grand_candidates}"
        )
        if dry_run:
            # COUNT-ONLY report: copyable vs needs-geocode, no writes happened.
            self.stdout.write(self.style.SUCCESS(
                f"  Would fill {grand_candidates} event(s): "
                f"by request-copy={grand_copyable} (free), "
                f"by geocode={grand_needs_geocode}"
            ))
            self.stdout.write(
                f"  Geocode batches needed at limit={limit}: "
                f"{self._batches(grand_needs_geocode, limit)}"
            )
        else:
            self.stdout.write(self.style.SUCCESS(
                f"  Updated: {grand_copied + grand_geocoded} event(s) "
                f"(copied={grand_copied}, geocoded={grand_geocoded})"
            ))
            remaining_style = (
                self.style.WARNING if grand_remaining else self.style.SUCCESS
            )
            self.stdout.write(remaining_style(
                f"  Remaining (still need geocode): {grand_remaining}"
                + (" — re-run --execute to drain the next batch"
                   if grand_remaining else " — backlog drained")
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

        # MACHINE-READABLE one-liner so the cron endpoint / operator can parse
        # the outcome out of the captured report without scraping prose.
        self.stdout.write(
            "\nRESULT "
            + (
                f"mode=dry_run candidates={grand_candidates} "
                f"copyable={grand_copyable} needs_geocode={grand_needs_geocode} "
                f"limit={limit} "
                f"batches={self._batches(grand_needs_geocode, limit)}"
                if dry_run else
                f"mode=execute candidates={grand_candidates} "
                f"copied={grand_copied} geocoded={grand_geocoded} "
                f"remaining={grand_remaining} failed={grand_failed} limit={limit}"
            )
        )

        self.stdout.write(self.style.MIGRATE_HEADING("\nPer-tenant breakdown"))
        for row in per_tenant:
            t = row["tenant"]
            if dry_run:
                self.stdout.write(
                    f"  {t.name} (id={t.id}, slug={t.slug}): "
                    f"candidates={row['candidates']} "
                    f"copyable={row['copyable']} "
                    f"needs_geocode={row['needs_geocode']}"
                )
            else:
                self.stdout.write(
                    f"  {t.name} (id={t.id}, slug={t.slug}): "
                    f"candidates={row['candidates']} "
                    f"copied={row['copied']} geocoded={row['geocoded']} "
                    f"remaining={row['remaining']}"
                    + (f" failed={row['failed']}" if row["failed"] else "")
                )

        if not dry_run and (grand_copied or grand_geocoded):
            logger.info(
                "backfill_event_coordinates: copied=%s geocoded=%s remaining=%s "
                "failed=%s tenants=%s limit=%s",
                grand_copied, grand_geocoded, grand_remaining,
                grand_failed, len(tenants), limit,
            )

    # ─── Per-tenant processing ──────────────────────────────────────

    def _process_tenant(
        self, tenant: Tenant, *, dry_run: bool, budget: dict
    ) -> dict:
        stats = {
            "candidates": 0,
            "copyable": 0,        # candidates fillable via request copy (free)
            "needs_geocode": 0,   # candidates that require a geocode
            "copied": 0,          # rows actually copied (Phase 1)
            "geocoded": 0,        # rows actually geocoded (Phase 2)
            "failed": 0,
            "remaining": 0,       # rows still needing a geocode after this run
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

        # Partition candidates WITHOUT any network call:
        #   copyable    — parent Request has valid coords (Phase 1, free)
        #   to_geocode  — everything else (Phase 2, network, capped)
        copyable: list[tuple[Event, list[float]]] = []
        to_geocode: list[Event] = []
        for ev in candidates:
            coords = _coords_from_request(ev)
            if coords is not None:
                copyable.append((ev, coords))
            else:
                to_geocode.append(ev)
        stats["copyable"] = len(copyable)
        stats["needs_geocode"] = len(to_geocode)

        header = (
            f"\n  Tenant '{tenant.name}' (id={tenant.id}, slug={tenant.slug}) "
            f"— {len(candidates)} candidate(s): "
            f"copyable={len(copyable)}, needs_geocode={len(to_geocode)}"
        )
        self.stdout.write(self.style.HTTP_INFO(header))

        if dry_run:
            # COUNT ONLY — never touch the network, never write.
            for ev, coords in copyable:
                self.stdout.write(
                    f"    ~ would copy coordinates={coords} from request onto "
                    f"event id={ev.id} (uuid={getattr(ev, 'uuid', None)})"
                )
            for ev in to_geocode:
                self.stdout.write(
                    f"    ~ would geocode event id={ev.id} "
                    f"(uuid={getattr(ev, 'uuid', None)}, address={ev.address!r})"
                )
            # Everything that needs a geocode is "remaining" in a dry-run.
            stats["remaining"] = len(to_geocode)
            return stats

        # ── EXECUTE ──────────────────────────────────────────────────
        # Phase 1: copy from request for ALL copyable rows (free, no network).
        for ev, coords in copyable:
            error = self._save_coords(ev, coords)
            if error is None:
                stats["copied"] += 1
                self.stdout.write(self.style.SUCCESS(
                    f"    ✓ copied coordinates={coords} from request onto "
                    f"event id={ev.id}"
                ))
            else:
                stats["failed"] += 1
                stats["errors"].append({"event_id": ev.id, "error": error})
                self.stdout.write(self.style.ERROR(
                    f"    ! failed event id={ev.id}: {error}"
                ))

        # Phase 2: geocode the rest, capped by the SHARED cross-tenant budget.
        for ev in to_geocode:
            if budget["remaining"] <= 0:
                # Cap hit — count this (and the rest) as still-remaining.
                stats["remaining"] += 1
                continue
            address = (getattr(ev, "address", None) or "").strip()
            if not address:
                stats["failed"] += 1
                stats["errors"].append(
                    {"event_id": ev.id, "error": "no request coords and no address"}
                )
                self.stdout.write(self.style.WARNING(
                    f"    ? skip event id={ev.id}: no request coords, no address"
                ))
                continue
            # Consume a unit of the geocode budget BEFORE the network call.
            budget["remaining"] -= 1
            # Sleep BEFORE the network call to space out requests politely.
            time.sleep(GEOCODE_SLEEP_SECONDS)
            coords = photon_geocode(address)
            if coords is None:
                stats["failed"] += 1
                stats["errors"].append(
                    {"event_id": ev.id, "error": f"geocode miss for {address!r}"}
                )
                self.stdout.write(self.style.WARNING(
                    f"    ? skip event id={ev.id}: geocode returned nothing for "
                    f"{address!r}"
                ))
                continue
            error = self._save_coords(ev, coords)
            if error is None:
                stats["geocoded"] += 1
                self.stdout.write(self.style.SUCCESS(
                    f"    ✓ geocoded coordinates={coords} onto event id={ev.id}"
                ))
            else:
                stats["failed"] += 1
                stats["errors"].append({"event_id": ev.id, "error": error})
                self.stdout.write(self.style.ERROR(
                    f"    ! failed event id={ev.id}: {error}"
                ))

        return stats

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

    @staticmethod
    def _batches(count: int, limit: int) -> int:
        """How many --execute runs of size `limit` it takes to drain `count`
        geocodes. limit=0 means "no geocoding this run" → report 0 batches."""
        if count <= 0 or limit <= 0:
            return 0
        return (count + limit - 1) // limit

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
