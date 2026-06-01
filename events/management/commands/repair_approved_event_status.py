"""
Backfill: bring every Event whose parent Request is approved/scheduled up to
its tenant's APPROVED EventStatus when the Event itself is still "pending".

Why this exists (and why it's NOT a migration):
    The internal "Log event" flow and request-approval materialized an Event
    via `Event.objects.from_request(...)` WITHOUT passing a status, so the
    Event defaulted to the tenant's default EventStatus = "pending" — even
    though the parent Request was set to "approved". Result: the Master
    Tracker (reads Request.status) showed Approved while the Event detail
    page (reads Event.status) showed Pending. The code fix (passing the
    approved EventStatus from both call sites + defaulting `from_request` to
    the approved status for already-approved requests) stops NEW events from
    drifting; this command repairs the EXISTING rows.

    EventStatus rows are TENANT DATA (one "approved"/"pending" set per
    tenant), and the drift is a data condition, not a schema change — a
    Django migration can't fix per-deployment tenant data correctly. This
    command can, and it must be run against prod separately.

What it does — idempotent + wrapped in a transaction:
    For every Event whose
        * parent Request's RequestStatus slug ∈ {approved, scheduled}, AND
        * own EventStatus slug == "pending"  (i.e. NOT already approved or
          scheduled — those are left alone)
    set the Event's status to that tenant's APPROVED EventStatus
    (EventStatus with slug="approved" for the Event's tenant). Events whose
    status is already approved/scheduled, or whose parent request is not
    approved/scheduled, are NEVER touched. Re-running on a repaired DB
    produces zero changes.

    The separate Request.scheduling_status field (the "SCHEDULED" chip — a
    CharField, NOT the EventStatus FK) is unrelated and is never touched.

Flags:
    --dry-run        Report counts only; change nothing.
    --tenant <x>     Scope to a single tenant (slug or numeric id).
                     Default: all tenants.

Usage:
    python manage.py repair_approved_event_status --dry-run
    python manage.py repair_approved_event_status --tenant liquid-death --dry-run
    python manage.py repair_approved_event_status            # apply, all tenants
    python manage.py repair_approved_event_status --tenant 7  # apply, one tenant
"""

from __future__ import annotations

import logging

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.db.models import Count

from events.models import Event, EventStatus
from recaps.models import Recap
from tenants.models import Tenant

logger = logging.getLogger(__name__)

# Request status slugs that mean the activation is going ahead. Mirrors
# EventManager.APPROVED_REQUEST_STATUS_SLUGS so the backfill and the live
# fix can never disagree about which requests should yield an approved Event.
APPROVED_REQUEST_STATUS_SLUGS = ("approved", "scheduled")

# The Event's own status that signals the bug (defaulted instead of approved).
# We only flip events still sitting on this slug, so events an operator has
# already moved forward (approved/scheduled/anything else) are left alone.
PENDING_EVENT_STATUS_SLUG = "pending"

# Target status slug to set the affected events to.
APPROVED_EVENT_STATUS_SLUG = "approved"


class Command(BaseCommand):
    help = (
        "Backfill: set Events to their tenant's approved EventStatus when the "
        "parent Request is approved/scheduled but the Event is still pending. "
        "Idempotent + transaction-wrapped. Supports --dry-run and --tenant."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Report what WOULD change without writing to the DB.",
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
        dry_run: bool = opts["dry_run"]
        tenant_arg: str | None = opts["tenant"]

        tenants = self._resolve_tenants(tenant_arg)

        self.stdout.write(self.style.MIGRATE_HEADING(
            "\nRepair approved Event status "
            f"({'ALL tenants' if tenant_arg is None else f'tenant={tenant_arg}'})"
        ))
        if dry_run:
            self.stdout.write(self.style.NOTICE("DRY RUN — no DB writes.\n"))

        grand_total_candidates = 0
        grand_total_changed = 0
        grand_total_skipped_no_status = 0

        for tenant in tenants:
            stats = self._process_tenant(tenant, dry_run=dry_run)
            grand_total_candidates += stats["candidates"]
            grand_total_changed += stats["changed"]
            grand_total_skipped_no_status += stats["skipped_no_status"]

        # ─── Summary ────────────────────────────────────────────────
        verb = "Would update" if dry_run else "Updated"
        self.stdout.write(self.style.MIGRATE_HEADING("\nSummary"))
        self.stdout.write(
            f"  Tenants scanned:            {len(tenants)}"
        )
        self.stdout.write(
            f"  Candidate events (pending + approved/scheduled request): "
            f"{grand_total_candidates}"
        )
        self.stdout.write(self.style.SUCCESS(
            f"  {verb}: {grand_total_changed} event(s) → approved"
        ))
        if grand_total_skipped_no_status:
            self.stdout.write(self.style.WARNING(
                f"  Skipped (tenant has no '{APPROVED_EVENT_STATUS_SLUG}' "
                f"EventStatus): {grand_total_skipped_no_status} event(s)"
            ))

        if not dry_run and grand_total_changed:
            logger.info(
                "repair_approved_event_status: changed=%s skipped_no_status=%s "
                "tenants=%s",
                grand_total_changed,
                grand_total_skipped_no_status,
                len(tenants),
            )

    # ─── Per-tenant processing ──────────────────────────────────────

    def _process_tenant(self, tenant: Tenant, *, dry_run: bool) -> dict:
        """Repair one tenant. Returns a stats dict.

        A tenant's run is wrapped in a single transaction (skipped on
        dry-run) so a mid-run failure leaves no partial flip.
        """
        stats = {
            "candidates": 0,
            "changed": 0,
            "skipped_no_status": 0,
        }

        # The affected events: own status is pending AND parent request is
        # approved/scheduled. Scoped to this tenant via the Event's own
        # tenant FK (Event.tenant_id == Recap-less, authoritative).
        candidates_qs = (
            Event.objects.filter(
                tenant_id=tenant.id,
                status__slug=PENDING_EVENT_STATUS_SLUG,
                request__status__slug__in=APPROVED_REQUEST_STATUS_SLUGS,
            )
        )
        candidate_count = candidates_qs.count()
        stats["candidates"] = candidate_count

        is_liquid_death = (tenant.name or "").strip().lower() == "liquid death"

        # Always print a per-tenant header so the operator sees every tenant
        # that was scanned, even ones with zero candidates.
        header = (
            f"\n  Tenant '{tenant.name}' (id={tenant.id}, slug={tenant.slug}) "
            f"— {candidate_count} candidate event(s)"
        )
        if is_liquid_death:
            header += "  [LIQUID DEATH]"
        self.stdout.write(self.style.HTTP_INFO(header))

        if candidate_count == 0:
            return stats

        # Resolve the tenant's approved EventStatus. If it's missing we can't
        # flip anything for this tenant — report and skip (don't hard-fail the
        # whole run, mirroring the live fix's null-safe fallback).
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
                f"this tenant — skipping its {candidate_count} candidate(s)"
            ))
            stats["skipped_no_status"] = candidate_count
            return stats

        # Liquid Death edge-case reporting (recap-visibility): how many of the
        # affected Requests carry >1 Event, and where recaps sit on those.
        # Print BEFORE the flip so dry-run and real-run report identically.
        if is_liquid_death:
            self._report_liquid_death_specifics(tenant, candidates_qs)

        # Apply (or simulate) the flip.
        candidate_ids = list(candidates_qs.values_list("id", flat=True))
        if dry_run:
            stats["changed"] = len(candidate_ids)
            self.stdout.write(
                f"    ~ would set {len(candidate_ids)} event(s) → "
                f"'{approved_status.name}' (id={approved_status.id})"
            )
            return stats

        with transaction.atomic():
            # Re-filter by the same predicate inside the transaction so we
            # never flip an event that changed underneath us; bulk update is
            # idempotent (re-running matches zero rows once repaired).
            updated = (
                Event.objects.filter(
                    id__in=candidate_ids,
                    status__slug=PENDING_EVENT_STATUS_SLUG,
                    request__status__slug__in=APPROVED_REQUEST_STATUS_SLUGS,
                ).update(status=approved_status)
            )
        stats["changed"] = updated
        self.stdout.write(self.style.SUCCESS(
            f"    ✓ set {updated} event(s) → '{approved_status.name}' "
            f"(id={approved_status.id})"
        ))
        return stats

    # ─── Liquid Death specifics ─────────────────────────────────────

    def _report_liquid_death_specifics(self, tenant: Tenant, candidates_qs) -> None:
        """Surface the related recap-visibility edge case for Liquid Death:
        how many affected Requests have >1 Event, and where recaps sit on
        those multi-event requests. (When a Request spawned more than one
        Event — e.g. a duplicate materialization — a recap is attached to a
        SPECIFIC Event, so flipping the other Event's status can change which
        approved Event the recap shows under.)"""
        affected_request_ids = list(
            candidates_qs.exclude(request_id=None)
            .values_list("request_id", flat=True)
            .distinct()
        )
        n_affected_requests = len(affected_request_ids)

        # Of the affected requests, which carry MORE THAN ONE Event total
        # (counting every event on the request, not just the pending ones)?
        multi_event_request_ids = [
            row["request_id"]
            for row in (
                Event.objects.filter(request_id__in=affected_request_ids)
                .values("request_id")
                .annotate(n=Count("id"))
                .filter(n__gt=1)
            )
        ]
        n_multi_event_requests = len(multi_event_request_ids)

        # Recaps sitting on events that belong to those multi-event requests.
        recaps_on_multi = Recap.objects.filter(
            event__request_id__in=multi_event_request_ids
        )
        n_recaps_on_multi = recaps_on_multi.count()
        # How many of those recaps hang off an event that is itself a
        # candidate (pending) vs. some other event on the same request — this
        # is the visibility nuance worth eyeballing.
        n_recaps_on_pending_candidate = recaps_on_multi.filter(
            event__status__slug=PENDING_EVENT_STATUS_SLUG,
            event__request__status__slug__in=APPROVED_REQUEST_STATUS_SLUGS,
        ).count()

        self.stdout.write(self.style.HTTP_INFO("    Liquid Death specifics:"))
        self.stdout.write(
            f"      • events that would change:                 "
            f"{candidates_qs.count()}"
        )
        self.stdout.write(
            f"      • distinct affected requests:               "
            f"{n_affected_requests}"
        )
        self.stdout.write(
            f"      • affected requests with >1 Event:          "
            f"{n_multi_event_requests}"
        )
        self.stdout.write(
            f"      • recaps on those multi-event requests:     "
            f"{n_recaps_on_multi}"
        )
        self.stdout.write(
            f"        (of which sit on a pending candidate event: "
            f"{n_recaps_on_pending_candidate})"
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
