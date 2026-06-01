"""Precompute proactive insight snapshots for every active tenant.

The dashboard's ``tenantInsights`` query now computes its FIXED, deterministic
buckets LIVE (no AI call, no token cost), so this command is no longer required
to keep reads fast. It is retained as an optional cron that writes a snapshot
row per active tenant — a cheap historical record / cache of the deterministic
buckets — by calling
:func:`recaps.tenant_insights.get_or_refresh_tenant_insights` for each active
tenant (``Tenant.active()``), then prints a one-line summary.

The underlying helper never raises and degrades to ``[]`` on failure, so one
tenant's data hiccup can't abort the whole run — the command counts
refreshed-with-items vs empty and reports both.

Usage:

    python manage.py refresh_tenant_insights
"""

from __future__ import annotations

from django.core.management.base import BaseCommand

from recaps.tenant_insights import get_or_refresh_tenant_insights
from tenants.models import Tenant


class Command(BaseCommand):
    help = "Snapshot the deterministic proactive insight buckets for every active tenant."

    def handle(self, *args, **options):
        # Skip archived clients (the "[ARCHIVED]" rename convention) when the
        # classmethod exists; otherwise fall back to every tenant.
        active = getattr(Tenant, "active", None)
        tenants = active() if callable(active) else Tenant.objects.all()

        total = 0
        with_items = 0
        empty = 0
        for tenant in tenants.iterator():
            total += 1
            # max_age_hours is accepted for compatibility but no longer gates
            # anything — the buckets are deterministic, so each call recomputes
            # and snapshots them fresh.
            items, _generated_at = get_or_refresh_tenant_insights(
                tenant.id, max_age_hours=0
            )
            if items:
                with_items += 1
            else:
                empty += 1

        self.stdout.write(
            self.style.SUCCESS(
                f"Refreshed insights for {total} active tenant(s): "
                f"{with_items} with insights, {empty} empty."
            )
        )
