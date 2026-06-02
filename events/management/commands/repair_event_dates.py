"""
Backfill: populate ``Event.date`` for events that have a NULL date but DO carry
a ``start_time`` (the day of the activation lives in ``start_time``, and on the
parent Request too).

Why this exists (and why it's NOT a migration):
    "Event Date" on the recap PDF + the custom-recap info panel originally read
    ONLY ``Event.date`` with no fallback. Events materialized BEFORE commit
    4b2d269 (#718, which added ``date=getattr(request, "date", None)`` in
    events/managers.py:from_request) were created with ``Event.date = NULL``
    even though ``Event.start_time`` was populated (and the date also lives in
    the recap name + on the parent Request). Those recaps therefore showed
    "N/A" / "-" for Event Date.

    The accompanying CODE fix makes the read side resilient (the ``event_date``
    GraphQL resolver and the PDF now fall back start_time → request.date →
    request.start_time), so the display is fixed on deploy WITHOUT this
    backfill. This command is DATA HYGIENE: it copies the derived date back
    into ``Event.date`` so the stored row is correct (sheets sync, raw exports,
    admin, future readers that don't go through the resolver all see a real
    date), and so the fallback chain stops doing work for these rows.

    It is NOT a migration because the target rows are tenant data and the
    derivation reads per-row values; a one-shot data migration would run once
    at deploy across all environments, whereas this can be previewed
    (dry-run), scoped per tenant, and re-run idempotently against prod
    separately. Mirrors the repair_missing_events_for_approved_requests trio
    exactly (same flags, per-tenant transaction, idempotency, error capture).

What it does — idempotent + wrapped in a transaction:
    For every Event whose
        * date IS NULL, AND
        * start_time IS NOT NULL
    (optionally scoped to one tenant) set ``event.date = event.start_time``
    (falling back to ``event.request.date`` then ``event.request.start_time``
    when start_time is somehow null — though the queryset already requires a
    non-null start_time, the fallback keeps the single-row helper correct if
    reused). Saves ONLY the ``date`` field (``update_fields=["date"]``) so
    nothing else on the row is touched.

    Re-running on a repaired DB updates zero rows: a row that now has a date no
    longer matches ``date__isnull=True``.

Flags (identical surface to repair_missing_events_for_approved_requests):
    --dry-run        Report counts only; change nothing. DEFAULT: ON. The
                     command previews unless --execute is passed.
    --execute        Actually write the dates (turns OFF the dry-run default).
    --tenant <x>     Scope to a single tenant (slug or numeric id).
                     Default: all tenants.

Usage:
    python manage.py repair_event_dates                       # DRY RUN (default)
    python manage.py repair_event_dates --dry-run             # explicit dry run
    python manage.py repair_event_dates --tenant liquid-death # dry run, one tenant
    python manage.py repair_event_dates --execute             # APPLY, all tenants
    python manage.py repair_event_dates --tenant liquid-death --execute  # APPLY, one tenant
"""

from __future__ import annotations

import logging
import traceback

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from events.models import Event
from tenants.models import Tenant

logger = logging.getLogger(__name__)


def _format_exc(exc: BaseException) -> str:
    """Concise, log-safe one-liner for a failed row update: exception type +
    message + the LAST traceback frame (file:line in func). Deliberately does
    NOT dump the full traceback or any env/secret — just enough to pinpoint
    which line threw, so a re-run surfaces the real cause in the GitHub
    Actions / cron log even without GCP access. Mirrors the helper in
    repair_missing_events_for_approved_requests.
    """
    type_name = type(exc).__name__
    message = " ".join(str(exc).split())  # collapse newlines/whitespace
    if len(message) > 300:
        message = message[:297] + "..."
    frame = ""
    tb = exc.__traceback__
    if tb is not None:
        last = traceback.extract_tb(tb)[-1]
        filename = last.filename.split("/")[-1]
        frame = f" [{filename}:{last.lineno} in {last.name}]"
    return f"{type_name}: {message}{frame}"


def _derive_date(event: Event):
    """The date to backfill onto ``event``: start_time, falling back to the
    parent request's date then its start_time. Returns a datetime or None.
    Null-safe; mirrors the read-side fallback chain (event.start_time →
    request.date → request.start_time) for the rows the queryset already
    pre-filtered to (date null, start_time set)."""
    value = getattr(event, "start_time", None)
    if value:
        return value
    request = getattr(event, "request", None)
    if request is not None:
        value = getattr(request, "date", None) or getattr(
            request, "start_time", None
        )
    return value or None


class Command(BaseCommand):
    help = (
        "Backfill Event.date from start_time (fallback request.date / "
        "request.start_time) for events created before the date-copy fix "
        "(#718) — date IS NULL but start_time IS set. Idempotent + "
        "transaction-wrapped. DRY-RUN by default — pass --execute to write. "
        "Supports --tenant."
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
                "Actually write the dates (disables the dry-run default). "
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
        # DRY-RUN is the default; --execute is the explicit opt-in to write.
        execute: bool = opts["execute"]
        dry_run: bool = not execute
        tenant_arg: str | None = opts["tenant"]

        tenants = self._resolve_tenants(tenant_arg)

        self.stdout.write(self.style.MIGRATE_HEADING(
            "\nRepair Event.date from start_time "
            f"({'ALL tenants' if tenant_arg is None else f'tenant={tenant_arg}'})"
        ))
        if dry_run:
            self.stdout.write(self.style.NOTICE("DRY RUN — no DB writes.\n"))

        grand_total_candidates = 0
        grand_total_updated = 0
        grand_total_failed = 0
        all_errors: list[dict] = []
        per_tenant: list[dict] = []

        for tenant in tenants:
            stats = self._process_tenant(tenant, dry_run=dry_run)
            grand_total_candidates += stats["candidates"]
            grand_total_updated += stats["updated"]
            grand_total_failed += stats["failed"]
            all_errors.extend(stats.get("errors", []))
            per_tenant.append(
                {
                    "tenant": tenant,
                    "candidates": stats["candidates"],
                    "updated": stats["updated"],
                    "failed": stats["failed"],
                }
            )

        # ─── Summary ────────────────────────────────────────────────
        verb = "Would update" if dry_run else "Updated"
        self.stdout.write(self.style.MIGRATE_HEADING("\nSummary"))
        self.stdout.write(
            f"  Tenants scanned:            {len(tenants)}"
        )
        self.stdout.write(
            f"  Candidate events (date null, start_time set): "
            f"{grand_total_candidates}"
        )
        self.stdout.write(self.style.SUCCESS(
            f"  {verb}: {grand_total_updated} event(s)"
        ))
        if grand_total_failed:
            self.stdout.write(self.style.WARNING(
                f"  Failed to update: {grand_total_failed} event(s) "
                "— re-run to retry"
            ))
            for err in all_errors:
                self.stdout.write(self.style.ERROR(
                    f"    - event id={err['event_id']}: {err['error']}"
                ))

        # Per-tenant breakdown (always printed, mirrors the missing-events
        # report so an operator sees every tenant that was touched).
        self.stdout.write(self.style.MIGRATE_HEADING("\nPer-tenant breakdown"))
        for row in per_tenant:
            t = row["tenant"]
            self.stdout.write(
                f"  {t.name} (id={t.id}, slug={t.slug}): "
                f"candidates={row['candidates']} {verb.lower()}={row['updated']}"
                + (f" failed={row['failed']}" if row["failed"] else "")
            )

        if not dry_run and grand_total_updated:
            logger.info(
                "repair_event_dates: updated=%s failed=%s tenants=%s",
                grand_total_updated,
                grand_total_failed,
                len(tenants),
            )

    # ─── Per-tenant processing ──────────────────────────────────────

    def _process_tenant(self, tenant: Tenant, *, dry_run: bool) -> dict:
        """Repair one tenant. Returns a stats dict.

        A tenant's run is wrapped in a single transaction (skipped on
        dry-run) so a mid-run failure leaves no partial backfill.
        """
        stats = {
            "candidates": 0,
            "updated": 0,
            "failed": 0,
            "errors": [],
        }

        # Target events: date null, start_time set, scoped to this tenant.
        # select_related("request") so the (rare) fallback to request.date /
        # request.start_time doesn't N+1.
        candidates_qs = (
            Event.objects.filter(
                tenant_id=tenant.id,
                date__isnull=True,
                start_time__isnull=False,
            )
            .select_related("request")
            .order_by("id")
        )
        candidate_count = candidates_qs.count()
        stats["candidates"] = candidate_count

        header = (
            f"\n  Tenant '{tenant.name}' (id={tenant.id}, slug={tenant.slug}) "
            f"— {candidate_count} candidate event(s)"
        )
        self.stdout.write(self.style.HTTP_INFO(header))

        if candidate_count == 0:
            return stats

        # Snapshot now (the qs predicate stops matching a row the instant it
        # gets a date).
        candidates = list(candidates_qs)

        if dry_run:
            for ev in candidates:
                derived = _derive_date(ev)
                self.stdout.write(
                    f"    ~ would set date={derived!s} on event "
                    f"id={ev.id} (uuid={getattr(ev, 'uuid', None)}, "
                    f"name={getattr(ev, 'name', None)!r})"
                )
            stats["updated"] = len(candidates)
            self.stdout.write(
                f"    ~ would update {len(candidates)} event(s)"
            )
            return stats

        # Each row gets its OWN savepoint so one failure doesn't poison the
        # surrounding transaction. The outer atomic() still gives an
        # all-or-nothing tenant run on an unexpected crash.
        with transaction.atomic():
            for ev in candidates:
                error = self._set_date_for_event(ev)
                if error is None:
                    stats["updated"] += 1
                    self.stdout.write(self.style.SUCCESS(
                        f"    ✓ set date on event id={ev.id} "
                        f"(uuid={getattr(ev, 'uuid', None)})"
                    ))
                else:
                    stats["failed"] += 1
                    stats["errors"].append({"event_id": ev.id, "error": error})
                    self.stdout.write(self.style.ERROR(
                        f"    ! failed event id={ev.id}: {error}"
                    ))

        return stats

    # ─── Single-event update ────────────────────────────────────────

    def _set_date_for_event(self, event: Event) -> str | None:
        """Set ``event.date`` from the derived value and save ONLY that field.

        Returns ``None`` on success (or idempotent skip when the row already
        has a date / no derivable date), or a concise error string on failure.
        Runs in a nested savepoint so a failure rolls back just this row.
        """
        # Idempotent guard — re-check inside the transaction.
        if getattr(event, "date", None) is not None:
            return None

        derived = _derive_date(event)
        if derived is None:
            # Nothing to copy (shouldn't happen given the queryset, but stay
            # defensive — never write a null over a null).
            return None

        try:
            with transaction.atomic():
                event.date = derived
                event.save(update_fields=["date"])
        except Exception as exc:  # noqa: BLE001 — surface the cause in report
            logger.exception(
                "repair_event_dates: failed to set date for event_id=%s",
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
