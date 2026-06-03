"""
Backfill: populate ``Ambassador.coordinates`` (``[lat, lng]``) for BAs whose
coordinates are missing (empty ``[]`` / null / ``[0, 0]``) AND who have an
``address`` to geocode.

Why this exists (and why it's NOT a migration):
    The "new gig nearby" distance push needs a BA's coordinates to measure how
    far they are from an event. Most ``Ambassador.coordinates`` are empty (no
    geocoder ever populated them — see ``jobs/notifications.py``), so the
    proximity push can't include those BAs. The forward fix (the mobile profile
    sending coordinates on save) is handled elsewhere; this command geocodes
    the EXISTING backlog from each BA's stored address.

    Not a migration for the same reasons as the event backfill: per-row,
    best-effort over the network, dry-run-previewable, per-tenant-scopable,
    re-runnable, and fired manually from GitHub Actions. Mirrors the repair_*
    command surface (flags, idempotency, error capture, summary + per-tenant
    breakdown).

How it fills each BA:
    GEOCODE ``Ambassador.address`` via the keyless Photon API
    (``utils.geocoding.photon_geocode``). Best-effort: a small ``time.sleep``
    between network calls keeps us polite to the free service, the call has a
    timeout, and a miss/failure SKIPS the row (logged, counted) instead of
    aborting. Saves ONLY ``coordinates`` (``update_fields=["coordinates"]``).

    Ambassadors are NOT tenant-scoped by a ``tenant_id`` column (a BA is a
    person, linked to tenants through events/jobs). ``--tenant`` therefore
    scopes to BAs who have at least one job application within that tenant —
    the same "who works for this brand" relation the rest of the app uses —
    so an operator can roll the backfill out one brand at a time. Default
    (no --tenant) processes ALL ambassadors.

Idempotent + per-row savepoint:
    A BA who already has valid coordinates is excluded by the candidate
    queryset and re-checked in the write path, so a second run updates zero
    rows.

Flags (identical surface to repair_event_dates / backfill_event_coordinates):
    --dry-run        Report what WOULD change; write nothing. DEFAULT: ON.
    --execute        Actually write coordinates (turns OFF the dry-run default).
    --tenant <x>     Scope to BAs linked to one tenant (slug or numeric id).

Usage:
    python manage.py backfill_ambassador_coordinates                 # DRY RUN
    python manage.py backfill_ambassador_coordinates --tenant liquid-death
    python manage.py backfill_ambassador_coordinates --execute       # APPLY, all
"""

from __future__ import annotations

import logging
import time
import traceback

from django.core.management.base import BaseCommand, CommandError
from django.db import models, transaction

from ambassadors.models import Ambassador
from tenants.models import Tenant
from utils.geocoding import has_valid_coordinates, photon_geocode

logger = logging.getLogger(__name__)

GEOCODE_SLEEP_SECONDS = 1.0

# Default cap on how many addresses we GEOCODE per invocation. Keeps a single
# request under the Cloud Run 300s timeout (~limit seconds of sleeps + per-call
# timeout + overhead). Re-run --execute until 'remaining' hits 0.
DEFAULT_GEOCODE_LIMIT = 40


def _format_exc(exc: BaseException) -> str:
    """Concise, log-safe one-liner for a failed row. Mirrors repair_event_dates."""
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


class Command(BaseCommand):
    help = (
        "Backfill Ambassador.coordinates by geocoding Ambassador.address via "
        "the keyless Photon API, for BAs with empty coordinates AND an "
        "address. Idempotent + per-row savepoints. DRY-RUN by default — pass "
        "--execute to write. --tenant scopes to BAs linked to one tenant."
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
                "Scope to BAs linked (via job applications) to a single tenant "
                "by slug or numeric id. Default: all ambassadors."
            ),
        )
        parser.add_argument(
            "--limit",
            type=int,
            default=DEFAULT_GEOCODE_LIMIT,
            help=(
                "Max addresses to GEOCODE per invocation. "
                f"Default: {DEFAULT_GEOCODE_LIMIT}. Re-run until 'remaining' is 0."
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

        tenant = self._resolve_tenant(tenant_arg)

        scope = "ALL ambassadors" if tenant is None else (
            f"tenant={tenant.slug or tenant.id}"
        )
        self.stdout.write(self.style.MIGRATE_HEADING(
            f"\nBackfill Ambassador.coordinates ({scope})"
        ))
        if dry_run:
            self.stdout.write(self.style.NOTICE(
                "DRY RUN — counts only, NO network, NO DB writes.\n"
            ))
        else:
            self.stdout.write(self.style.NOTICE(
                f"EXECUTE — geocodes at most {limit} ambassador(s) this run.\n"
            ))

        candidates = self._candidates(tenant)
        self.stdout.write(self.style.HTTP_INFO(
            f"  {len(candidates)} candidate ambassador(s) "
            "(empty coordinates + an address)"
        ))

        geocoded = 0
        failed = 0
        remaining = 0
        errors: list[dict] = []

        if dry_run:
            # COUNT ONLY — never touch the network, never write.
            for amb in candidates:
                self.stdout.write(
                    f"    ~ would geocode ambassador id={amb.id} "
                    f"(uuid={getattr(amb, 'uuid', None)}, address={amb.address!r})"
                )
            remaining = len(candidates)
        else:
            budget = limit
            for amb in candidates:
                if budget <= 0:
                    # Cap hit — count this (and the rest) as still-remaining.
                    remaining += 1
                    continue
                address = (getattr(amb, "address", None) or "").strip()
                if not address:
                    # The queryset requires an address, but stay defensive.
                    failed += 1
                    errors.append({"ambassador_id": amb.id, "error": "no address"})
                    continue
                # Consume a unit of budget BEFORE the network call.
                budget -= 1
                time.sleep(GEOCODE_SLEEP_SECONDS)
                coords = photon_geocode(address)
                if coords is None:
                    failed += 1
                    errors.append(
                        {"ambassador_id": amb.id, "error": f"geocode miss for {address!r}"}
                    )
                    self.stdout.write(self.style.WARNING(
                        f"    ? skip ambassador id={amb.id}: geocode returned "
                        f"nothing for {address!r}"
                    ))
                    continue
                error = self._save_coords(amb, coords)
                if error is None:
                    geocoded += 1
                    self.stdout.write(self.style.SUCCESS(
                        f"    ✓ set coordinates={coords} on ambassador id={amb.id}"
                    ))
                else:
                    failed += 1
                    errors.append({"ambassador_id": amb.id, "error": error})
                    self.stdout.write(self.style.ERROR(
                        f"    ! failed ambassador id={amb.id}: {error}"
                    ))

        # ─── Summary ────────────────────────────────────────────────
        self.stdout.write(self.style.MIGRATE_HEADING("\nSummary"))
        self.stdout.write(f"  Scope:                      {scope}")
        self.stdout.write(
            f"  Candidate ambassadors (empty coords + address): "
            f"{len(candidates)}"
        )
        if dry_run:
            self.stdout.write(self.style.SUCCESS(
                f"  Would geocode {len(candidates)} ambassador(s)"
            ))
            self.stdout.write(
                f"  Geocode batches needed at limit={limit}: "
                f"{self._batches(len(candidates), limit)}"
            )
        else:
            self.stdout.write(self.style.SUCCESS(
                f"  Updated: {geocoded} ambassador(s) (geocoded={geocoded})"
            ))
            remaining_style = (
                self.style.WARNING if remaining else self.style.SUCCESS
            )
            self.stdout.write(remaining_style(
                f"  Remaining (still need geocode): {remaining}"
                + (" — re-run --execute to drain the next batch"
                   if remaining else " — backlog drained")
            ))
        if failed:
            self.stdout.write(self.style.WARNING(
                f"  Skipped/failed (geocode miss / no address): {failed} "
                "— re-run to retry"
            ))
            for err in errors:
                self.stdout.write(self.style.ERROR(
                    f"    - ambassador id={err['ambassador_id']}: {err['error']}"
                ))

        # MACHINE-READABLE one-liner so the cron endpoint / operator can parse
        # the outcome without scraping prose.
        self.stdout.write(
            "\nRESULT "
            + (
                f"mode=dry_run candidates={len(candidates)} limit={limit} "
                f"batches={self._batches(len(candidates), limit)}"
                if dry_run else
                f"mode=execute candidates={len(candidates)} geocoded={geocoded} "
                f"remaining={remaining} failed={failed} limit={limit}"
            )
        )

        if not dry_run and geocoded:
            logger.info(
                "backfill_ambassador_coordinates: geocoded=%s remaining=%s "
                "failed=%s scope=%s limit=%s",
                geocoded, remaining, failed, scope, limit,
            )

    @staticmethod
    def _batches(count: int, limit: int) -> int:
        """How many --execute runs of size `limit` it takes to drain `count`
        geocodes. limit=0 → 0 batches (no geocoding this run)."""
        if count <= 0 or limit <= 0:
            return 0
        return (count + limit - 1) // limit

    # ─── Candidate selection ────────────────────────────────────────

    def _candidates(self, tenant: Tenant | None) -> list[Ambassador]:
        """Ambassadors with empty coordinates AND a non-empty address,
        optionally scoped to one tenant via their job applications."""
        qs = (
            Ambassador.objects.filter(
                models.Q(coordinates__isnull=True)
                | models.Q(coordinates=[])
                | models.Q(coordinates=[0, 0])
                | models.Q(coordinates=[0.0, 0.0])
            )
            .exclude(address__isnull=True)
            .exclude(address__exact="")
        )
        if tenant is not None:
            # BAs who have a job application within this tenant. The reverse
            # accessor from Ambassador to JobApplication is `job_applications`
            # (JobApplication.ambassador → related_name="job_applications"),
            # and JobApplication carries its own tenant FK — so filter on that
            # directly. This is the "who works for this brand" relation used
            # elsewhere in the app.
            qs = qs.filter(job_applications__tenant_id=tenant.id).distinct()

        # Coarse ORM pre-filter, then re-check in Python (a single-element or
        # non-numeric array slips past the equality filters).
        return [
            amb for amb in qs.order_by("id")
            if not has_valid_coordinates(amb.coordinates)
            and (amb.address or "").strip()
        ]

    # ─── Single-row write ───────────────────────────────────────────

    def _save_coords(self, amb: Ambassador, coords: list[float]) -> str | None:
        """Write ``coords`` to the ambassador, saving ONLY the coordinates
        field in its own savepoint. Returns None on success (or idempotent
        skip) or a concise error string."""
        if has_valid_coordinates(amb.coordinates):
            return None
        try:
            with transaction.atomic():
                amb.coordinates = coords
                amb.save(update_fields=["coordinates"])
        except Exception as exc:  # noqa: BLE001 — surface the cause in report
            logger.exception(
                "backfill_ambassador_coordinates: failed to set coords for "
                "ambassador_id=%s",
                amb.id,
            )
            return _format_exc(exc)
        return None

    # ─── Helpers ────────────────────────────────────────────────────

    def _resolve_tenant(self, tenant_arg: str | None) -> Tenant | None:
        """Resolve --tenant (slug or numeric id) to a tenant, or None for all."""
        if tenant_arg is None:
            return None
        tenant: Tenant | None = None
        if tenant_arg.isdigit():
            tenant = Tenant.objects.filter(id=int(tenant_arg)).first()
        if tenant is None:
            tenant = Tenant.objects.filter(slug=tenant_arg).first()
        if tenant is None:
            raise CommandError(
                f"No tenant matching '{tenant_arg}' (tried id then slug)."
            )
        return tenant
