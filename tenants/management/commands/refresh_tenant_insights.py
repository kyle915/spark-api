"""Precompute proactive AI insights for every active tenant.

Cron this DAILY so the dashboard's ``tenantInsights`` query always serves a
fresh server-side snapshot and never has to pay for a synchronous OpenAI call
on a user read. For each active tenant (``Tenant.active()``) it calls
:func:`recaps.tenant_insights.get_or_refresh_tenant_insights` with
``max_age_hours=0`` to FORCE a regeneration, then prints a one-line summary.

The underlying helper never raises and degrades to the last good snapshot on
failure, so one tenant's AI hiccup can't abort the whole run — the command
counts refreshed-with-items vs empty and reports both.

Usage:

    python manage.py refresh_tenant_insights
"""

from __future__ import annotations

from django.core.management.base import BaseCommand

from recaps.tenant_insights import get_or_refresh_tenant_insights
from tenants.models import Tenant


class Command(BaseCommand):
    help = "Force-refresh cached proactive AI insights for every active tenant."

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
            # max_age_hours=0 forces regeneration regardless of cache age.
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
