"""Precompute "What people are saying" sentiment snapshots for every tenant.

The dashboard's ``tenantSentiment`` query is AI-backed (an OpenAI call), so it
reads from a cached :class:`tenants.models.TenantSentimentSnapshot` rather than
summarizing live. This command is the daily cron that keeps that cache warm:
for each ACTIVE tenant (``Tenant.active()``) it calls
:func:`recaps.tenant_sentiment.get_or_refresh_tenant_sentiment`, which serves a
fresh snapshot or regenerates + persists one (and falls back to the last good
snapshot on failure). Running it ~daily keeps steady-state spend at roughly one
OpenAI call per tenant per day while dashboard reads stay instant.

The underlying helper NEVER raises and degrades to ``(None, None)`` (or the
last-good snapshot) on failure, so one tenant's data hiccup or a transient AI
outage can't abort the whole run — the command counts refreshed-with-payload vs
empty and reports both.

Use ``--max-age-hours`` to control the freshness gate (default 24: only tenants
whose newest snapshot is older than this are regenerated). All-time snapshots
(``year=None``) are refreshed; per-year snapshots are produced on demand by the
resolver.

Usage:

    python manage.py refresh_tenant_sentiment
    python manage.py refresh_tenant_sentiment --max-age-hours 12
"""

from __future__ import annotations

from django.core.management.base import BaseCommand

from recaps.tenant_sentiment import get_or_refresh_tenant_sentiment
from tenants.models import Tenant


class Command(BaseCommand):
    help = (
        "Refresh the cached consumer-sentiment snapshot for every active "
        "tenant (daily cron for the dashboard's tenantSentiment query)."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--max-age-hours",
            type=int,
            default=24,
            help=(
                "Only regenerate a tenant's snapshot when its newest one is "
                "older than this many hours (default: 24)."
            ),
        )

    def handle(self, *args, **options):
        max_age_hours = options["max_age_hours"]

        # Skip archived clients (the "[ARCHIVED]" rename convention) when the
        # classmethod exists; otherwise fall back to every tenant.
        active = getattr(Tenant, "active", None)
        tenants = active() if callable(active) else Tenant.objects.all()

        total = 0
        with_payload = 0
        empty = 0
        for tenant in tenants.iterator():
            total += 1
            # All-time snapshot (year=None) is the dashboard default. The
            # helper never raises and falls back to the last good snapshot.
            payload, _generated_at = get_or_refresh_tenant_sentiment(
                tenant.id, year=None, max_age_hours=max_age_hours
            )
            if payload:
                with_payload += 1
            else:
                empty += 1

        self.stdout.write(
            self.style.SUCCESS(
                f"Refreshed sentiment for {total} active tenant(s): "
                f"{with_payload} with a summary, {empty} empty."
            )
        )
