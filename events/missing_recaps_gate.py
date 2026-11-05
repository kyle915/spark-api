"""Per-tenant gate for the admin Missing Recaps surface.

Famus Energy files via standing walk-up + sheet/request twins. Empty
schedule shells often stay request-linked with no BA while the real filed
recap lives on a walk-up Event, so /recaps/missing and the sidebar
``recapsDue`` badge scream false positives. Kyle asked that surface off
for Famus only — other tenants keep the feature.
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
