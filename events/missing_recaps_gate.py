"""Per-tenant gate for the admin Missing Recaps surface.

Famus was gated while walk-up↔schedule twin matching left false DUE shells.
Matching (post-submit + nightly link_walkin + fuzzy folds) is live; the four
historical orphans below have **no safe same-day DUE twin** and are parked as
accepted debt so they do not inflate the orphan KPI or block the surface.

Lifted: ``MISSING_RECAPS_DISABLED_SLUGS`` is empty. Re-add a slug only if
false Missing Recaps return for that tenant.
"""

from __future__ import annotations

# Empty = Missing Recaps on for every tenant (including Famus).
MISSING_RECAPS_DISABLED_SLUGS: frozenset[str] = frozenset()

# Filed walk-up Events with no safe Tracker twin (diagnose 2026-11).
# Excluded from walkup_orphan_filed_count so KPI reflects actionable debt.
ACCEPTED_WALKUP_ORPHAN_EVENT_IDS: frozenset[int] = frozenset(
    {
        6948,  # Smileys E Broadway Campbellsville — no-same-day-due
        6988,  # West 5th Owensboro — no-same-day-due
        7021,  # Old State Rte 74 Batavia OH — same-day dues are other stores
        7039,  # Old Mill Rd zip 40117 — zip/street ambiguity
    }
)


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
    """Filed recaps on request-less Events — Matching debt KPI.

    Parks ``ACCEPTED_WALKUP_ORPHAN_EVENT_IDS`` so historical no-twin orphans
    do not keep the KPI red after the Famus gate lift.
    """
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
    ).exclude(id__in=ACCEPTED_WALKUP_ORPHAN_EVENT_IDS)
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
