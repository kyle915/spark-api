"""Per-tenant gate for the admin Missing Recaps surface.

Famus Energy files via standing walk-up + sheet/request twins. Empty
schedule shells often stay request-linked with no BA while the real filed
recap lives on a walk-up Event, so /recaps/missing and the sidebar
``recapsDue`` badge scream false positives. Kyle asked that surface off
for Famus only — other tenants keep the feature.

Exit criteria (lift Famus from ``MISSING_RECAPS_DISABLED_SLUGS`` when met):
  - Nightly ``link_walkin_recaps_to_requests --apply`` + post-submit
    ``try_link_orphan_event`` keep orphan rate near zero
  - ``walkup_orphan_filed_count`` KPI on the dashboard stays low for 2 weeks
  - Manual spot-check: Missing Recaps for Famus shows only true dues
"""

from __future__ import annotations

MISSING_RECAPS_DISABLED_SLUGS = frozenset({"famus-energy"})


def missing_recaps_disabled_for_slug(slug: str | None) -> bool:
    return (slug or "").strip().lower() in MISSING_RECAPS_DISABLED_SLUGS


def missing_recaps_disabled_for_tenant(tenant) -> bool:
    if tenant is None:
        return False
    for attr in ("slug", "request_url_name"):
        if missing_recaps_disabled_for_slug(getattr(tenant, attr, None)):
            return True
    return False


def missing_recaps_disabled_for_tenant_id(tenant_id: int | None) -> bool:
    if tenant_id is None:
        return False
    from tenants.models import Tenant

    tenant = (
        Tenant.objects.filter(pk=tenant_id)
        .only("id", "slug", "request_url_name")
        .first()
    )
    return missing_recaps_disabled_for_tenant(tenant)


def walkup_orphan_filed_count(tenant_id: int | None = None, *, days: int = 45) -> int:
    """Filed recaps on request-less Events — Matching debt KPI."""
    from datetime import timedelta

    from django.utils import timezone as djtz

    from events.models import Event
    from recaps.filed import custom_filed_q, legacy_filed_q
    from recaps.models import CustomRecap, Recap

    lo = djtz.now() - timedelta(days=max(1, int(days)))
    orphan_ids = Event.objects.filter(
        request__isnull=True,
        date__gte=lo,
        co_activation_host__isnull=True,
    )
    if tenant_id is not None:
        orphan_ids = orphan_ids.filter(tenant_id=tenant_id)
    orphan_ids = orphan_ids.values_list("id", flat=True)
    custom = (
        CustomRecap.objects.filter(event_id__in=orphan_ids)
        .filter(custom_filed_q())
        .count()
    )
    legacy = (
        Recap.objects.filter(event_id__in=orphan_ids).filter(legacy_filed_q()).count()
    )
    return int(custom) + int(legacy)
