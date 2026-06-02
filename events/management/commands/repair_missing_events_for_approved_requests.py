"""
Backfill: create the missing approved Event for every Request that is
approved/scheduled but has NO Event at all.

Why this exists (and why it's NOT a migration):
    The client self-serve create-request flow auto-approves the request
    (RequestWithDependenciesMutationService.save sets auto_approve for client
    users) but — until the accompanying code fix — the `is_client` branch of
    the create_request resolver ONLY sent notification emails; it never
    materialized an Event. (The admin "Log event" branch and approve_request
    both DO create the Event.) Result: approved, EVENTLESS Requests.

    That's fatal for recaps: the Missing Recaps query and the recap event
    picker both iterate Event rows, so an eventless-but-approved Request is
    invisible to both — no recap can ever be filed against it. Example
    incident: a Vons / Liquid Death request from 2026-05-29.

    The code fix (factoring event materialization into a shared helper called
    from the client branch too) stops NEW requests from drifting; this command
    repairs the EXISTING backlog — every client-created request since the bug
    shipped.

    EventStatus rows are TENANT DATA (one "approved"/"pending" set per tenant)
    and the drift is a data condition, not a schema change — a Django migration
    can't fix per-deployment tenant data correctly. This command can, and it
    must be run against prod separately (AFTER the code fix is deployed).

    Sibling of `repair_approved_event_status`, which fixes a DIFFERENT
    condition: a request that HAS an Event whose status is stuck on "pending".
    This command fixes requests with NO Event at all. The two are
    complementary and both idempotent.

What it does — idempotent + wrapped in a transaction:
    For every Request whose
        * RequestStatus slug ∈ {approved, scheduled}, AND
        * deleted_at IS NULL, AND
        * has NO related Event (event__isnull=True)
    create an Event via `Event.objects.from_request(..., status=<approved>)`
    using the tenant's APPROVED EventStatus (so it lands approved, NOT the
    tenant default "pending" — same resolution the live fix and
    create_event_with_request use; null-safe so a tenant missing the row falls
    through to from_request's default), then create the Pending Job(s) via
    `create_pending_jobs_for_request`.

    Requests that already have at least one Event are NEVER touched. Re-running
    on a repaired DB creates zero new events (each repaired request now has an
    Event, so it no longer matches event__isnull=True).

Flags:
    --dry-run        Report counts only; change nothing. DEFAULT: ON. The
                     command previews unless --execute is passed.
    --execute        Actually create the events (turns OFF the dry-run
                     default). Required to write.
    --tenant <x>     Scope to a single tenant (slug or numeric id).
                     Default: all tenants.

Usage:
    python manage.py repair_missing_events_for_approved_requests            # DRY RUN (default)
    python manage.py repair_missing_events_for_approved_requests --dry-run  # explicit dry run
    python manage.py repair_missing_events_for_approved_requests --tenant liquid-death            # dry run, one tenant
    python manage.py repair_missing_events_for_approved_requests --execute                        # APPLY, all tenants
    python manage.py repair_missing_events_for_approved_requests --tenant liquid-death --execute  # APPLY, one tenant
"""

from __future__ import annotations

import logging

from asgiref.sync import async_to_sync
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from events.managers import EventManager
from events.models import Event, EventStatus, Request
from recaps.models import Recap
from tenants.models import Tenant

logger = logging.getLogger(__name__)

# Request status slugs that mean the activation is going ahead, so the request
# should carry an Event. Mirrors EventManager.APPROVED_REQUEST_STATUS_SLUGS (and
# repair_approved_event_status) so the backfill and the live fix can never
# disagree about which requests should yield an approved Event.
APPROVED_REQUEST_STATUS_SLUGS = tuple(EventManager.APPROVED_REQUEST_STATUS_SLUGS)

# Target EventStatus slug the created event should land on.
APPROVED_EVENT_STATUS_SLUG = "approved"


class Command(BaseCommand):
    help = (
        "Backfill: create the approved Event (+ pending Job) for Requests that "
        "are approved/scheduled but have NO Event at all (the client "
        "auto-approve gap). Idempotent + transaction-wrapped. DRY-RUN by "
        "default — pass --execute to write. Supports --tenant."
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
                "Actually create the events (disables the dry-run default). "
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
        # (--execute wins over the --dry-run default so `--execute` alone
        # writes, and a bare invocation never touches the DB.)
        execute: bool = opts["execute"]
        dry_run: bool = not execute
        tenant_arg: str | None = opts["tenant"]

        tenants = self._resolve_tenants(tenant_arg)

        self.stdout.write(self.style.MIGRATE_HEADING(
            "\nRepair missing Events for approved/scheduled requests "
            f"({'ALL tenants' if tenant_arg is None else f'tenant={tenant_arg}'})"
        ))
        if dry_run:
            self.stdout.write(self.style.NOTICE("DRY RUN — no DB writes.\n"))

        grand_total_candidates = 0
        grand_total_created = 0
        grand_total_failed = 0

        for tenant in tenants:
            stats = self._process_tenant(tenant, dry_run=dry_run)
            grand_total_candidates += stats["candidates"]
            grand_total_created += stats["created"]
            grand_total_failed += stats["failed"]

        # ─── Summary ────────────────────────────────────────────────
        verb = "Would create" if dry_run else "Created"
        self.stdout.write(self.style.MIGRATE_HEADING("\nSummary"))
        self.stdout.write(
            f"  Tenants scanned:            {len(tenants)}"
        )
        self.stdout.write(
            f"  Candidate requests (approved/scheduled, no Event): "
            f"{grand_total_candidates}"
        )
        self.stdout.write(self.style.SUCCESS(
            f"  {verb}: {grand_total_created} event(s)"
        ))
        if grand_total_failed:
            self.stdout.write(self.style.WARNING(
                f"  Failed to create: {grand_total_failed} event(s) "
                "(see log) — re-run to retry"
            ))

        if not dry_run and grand_total_created:
            logger.info(
                "repair_missing_events_for_approved_requests: created=%s "
                "failed=%s tenants=%s",
                grand_total_created,
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
            "created": 0,
            "failed": 0,
        }

        # The affected requests: approved/scheduled, not soft-deleted, and with
        # NO Event at all. Scoped to this tenant. `event__isnull=True` uses the
        # reverse FK (Event.request has no related_name → reverse query name
        # `event`); matches the signal's `request.event_set` view of the world.
        candidates_qs = (
            Request.objects.filter(
                tenant_id=tenant.id,
                status__slug__in=APPROVED_REQUEST_STATUS_SLUGS,
                deleted_at__isnull=True,
                event__isnull=True,
            )
            .order_by("id")
            .distinct()
        )
        candidate_count = candidates_qs.count()
        stats["candidates"] = candidate_count

        is_liquid_death = (tenant.name or "").strip().lower() == "liquid death"

        # Always print a per-tenant header so the operator sees every tenant
        # that was scanned, even ones with zero candidates.
        header = (
            f"\n  Tenant '{tenant.name}' (id={tenant.id}, slug={tenant.slug}) "
            f"— {candidate_count} candidate request(s)"
        )
        if is_liquid_death:
            header += "  [LIQUID DEATH]"
        self.stdout.write(self.style.HTTP_INFO(header))

        if candidate_count == 0:
            return stats

        # Resolve the tenant's approved EventStatus up-front for the report. If
        # it's missing, from_request still materializes an Event (falling
        # through to the tenant default) — so we DON'T skip the tenant; we just
        # note it. Mirrors the live fix's null-safe fallback.
        approved_status = (
            EventStatus.objects.filter(
                slug=APPROVED_EVENT_STATUS_SLUG, tenant_id=tenant.id
            )
            .order_by("id")
            .first()
        )
        if approved_status is None:
            self.stdout.write(self.style.WARNING(
                f"    ! no EventStatus slug='{APPROVED_EVENT_STATUS_SLUG}' for "
                f"this tenant — created events will fall back to the tenant "
                f"default EventStatus"
            ))

        # Liquid Death edge-case reporting. Print BEFORE the writes so dry-run
        # and real-run report identically.
        if is_liquid_death:
            self._report_liquid_death_specifics(tenant, candidates_qs)

        # Snapshot the candidate ids/names now (the qs predicate stops matching
        # a request the instant it gets an Event).
        candidates = list(candidates_qs)

        if dry_run:
            for req in candidates:
                self.stdout.write(
                    f"    ~ would create approved Event for request "
                    f"id={req.id} (uuid={req.uuid}, name={req.name!r})"
                )
            stats["created"] = len(candidates)
            self.stdout.write(
                f"    ~ would create {len(candidates)} event(s)"
            )
            return stats

        with transaction.atomic():
            for req in candidates:
                created = self._create_event_for_request(req)
                if created:
                    stats["created"] += 1
                    self.stdout.write(self.style.SUCCESS(
                        f"    ✓ created approved Event for request "
                        f"id={req.id} (uuid={req.uuid})"
                    ))
                else:
                    stats["failed"] += 1
                    self.stdout.write(self.style.WARNING(
                        f"    ! failed/skipped request id={req.id} — see log"
                    ))

        return stats

    # ─── Single-request event creation ──────────────────────────────

    def _create_event_for_request(self, request: Request) -> bool:
        """Create the approved Event (+ pending Job) for one request.

        Returns True if an Event was created. Idempotent: re-checks for an
        existing Event inside the (transaction-held) call so a request that
        somehow already grew an Event is skipped rather than double-created.
        """
        # Idempotent guard — re-check inside the transaction.
        if Event.objects.filter(request_id=request.id).exists():
            return False

        # Resolve the tenant's approved EventStatus null-safely (same lookup
        # the live fix uses). from_request also defaults approved/scheduled
        # requests to the approved status, but we pass it explicitly to match
        # the resolver call sites exactly.
        event_approved_status = (
            EventStatus.objects.filter(
                slug=APPROVED_EVENT_STATUS_SLUG, tenant_id=request.tenant_id
            )
            .order_by("id")
            .first()
        )

        try:
            # Event.objects.from_request is async; this command is sync, so
            # bridge with async_to_sync. created_by mirrors the request's
            # creator so the audit trail points at who filed it.
            async_to_sync(Event.objects.from_request)(
                request=request,
                created_by=request.created_by,
                status=event_approved_status,
            )
        except Exception:
            logger.exception(
                "repair_missing_events: failed to create Event for "
                "request_id=%s",
                request.id,
            )
            return False

        # Create the Pending Job(s) now that the Event exists. Idempotent +
        # best-effort — a job failure must not undo the (successful) event
        # creation, so it's caught and logged, not raised.
        try:
            from events.signals import create_pending_jobs_for_request

            create_pending_jobs_for_request(request)
        except Exception:
            logger.exception(
                "repair_missing_events: failed to create pending job for "
                "request_id=%s",
                request.id,
            )

        return True

    # ─── Liquid Death specifics ─────────────────────────────────────

    def _report_liquid_death_specifics(self, tenant: Tenant, candidates_qs) -> None:
        """Surface Liquid Death detail: how many eventless approved/scheduled
        requests there are and how many already carry a recap (a recap filed
        against a request with no Event is the symptom that triggered this —
        though recaps attach to Events, so eventless requests normally have
        none; we report it so a surprising nonzero count is visible)."""
        candidate_ids = list(candidates_qs.values_list("id", flat=True))
        n_candidates = len(candidate_ids)

        # Recaps whose event's parent request is one of the candidates. For a
        # truly eventless request this is 0 by construction; a nonzero count
        # would mean a recap hangs off an event we didn't see (worth eyeballing).
        n_recaps_on_candidates = Recap.objects.filter(
            event__request_id__in=candidate_ids
        ).count()

        self.stdout.write(self.style.HTTP_INFO("    Liquid Death specifics:"))
        self.stdout.write(
            f"      • eventless approved/scheduled requests:    "
            f"{n_candidates}"
        )
        self.stdout.write(
            f"      • recaps already on those requests:         "
            f"{n_recaps_on_candidates}"
        )

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
