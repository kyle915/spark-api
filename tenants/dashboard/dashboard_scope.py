"""Tenant-scoping shell for client-facing dashboard reads (clients schema).

Mirrors ``recaps/report_types.py`` ``_CampaignReportService``,
``tenants/forms.py`` ``_FormScope`` and the round-1 ``academy.AcademyScope`` /
``announcements.AnnouncementScope``: a client/non-admin caller is pinned to
the tenant(s) they actually belong to (a supplied ``tenant_id`` for another
brand is rejected), while admins (spark-admin / staff / superuser /
``@igniteproductions.co``) may target ANY tenant.

Used by the dashboard PII/aggregate reads (``goalsList``, ``latestInsights``,
and the cross-tenant aggregation dashboards) to resolve the effective tenant
scope WITHOUT trusting a client-supplied id. All methods gate on a caller who
already passed ``StrictIsAuthenticated`` and never raise — callers turn a
None/empty scope into a safe ``[]`` / ``null`` response.

``resolve_scoped_tenant_id`` differs slightly from the favorites/academy
``resolve_target_tenant_id``: a non-admin may legitimately belong to more than
one tenant, and these reads take a REQUIRED/optional ``tenantId``, so instead
of always collapsing to the caller's default tenant we HONOR the requested id
when it is in the caller's accessible set (and reject it otherwise). That keeps
a legit multi-tenant member working without letting a client reach a brand they
don't belong to.
"""

from __future__ import annotations

import strawberry
from asgiref.sync import sync_to_async

from utils.graphql.mixins import SparkGraphQLMixin, resolve_id_to_int
from utils.graphql.permissions import (
    _is_admin_access,
    resolve_request_user_access,
)


class DashboardScope(SparkGraphQLMixin):
    """Resolve the tenant scope a dashboard caller may read."""

    async def accessible_tenant_ids(self, info: strawberry.Info) -> set[int] | None:
        """Tenant ids the caller may read, or None for "any" (admins)."""
        user = await self.get_user(info)
        role_slug, is_staff, is_super, email = await resolve_request_user_access(
            user
        )
        if _is_admin_access(role_slug, is_staff, is_super, email):
            return None

        @sync_to_async
        def _ids() -> set[int]:
            return set(
                user.tenanted_users.filter(is_active=True).values_list(
                    "tenant_id", flat=True
                )
            )

        return await _ids()

    async def resolve_scoped_tenant_id(
        self, info: strawberry.Info, requested_tenant_id
    ) -> int | None:
        """The CONCRETE tenant id the caller may read, or None.

        * **admin** — the requested tenant id (global id or int), or None when
          none/garbage was passed.
        * **client / non-admin** — the requested tenant id IF it's one the
          caller belongs to; otherwise their default bound tenant when nothing
          (or garbage) was requested; otherwise None (out-of-scope request ->
          caller turns it into a safe ``[]`` / ``null``).
        """
        allowed = await self.accessible_tenant_ids(info)

        requested: int | None = None
        if requested_tenant_id is not None:
            raw = str(requested_tenant_id).strip()
            if raw:
                try:
                    requested = resolve_id_to_int(raw)
                except Exception:  # noqa: BLE001
                    requested = None

        # Admin: honor whatever (valid) id was asked for; None when absent.
        if allowed is None:
            return requested

        # Non-admin: a requested id is only honored when in the caller's set.
        if requested is not None:
            return requested if requested in allowed else None

        # No id requested -> fall back to the caller's default bound tenant.
        tenant = await self.get_user_tenant(info)
        return tenant.id if tenant else None

    async def cache_scope_token(self, info: strawberry.Info) -> str:
        """A stable token describing the caller's effective tenant scope.

        Folded into the dashboard cache key so a client's tenant-narrowed
        aggregate is never served from (or written to) the admin/global
        ``:0:`` cache slot — and two non-admin callers in different tenants
        never collide. ``"all"`` for admins (global view), else a short,
        deterministic hash of the caller's accessible tenant ids.
        """
        import hashlib

        allowed = await self.accessible_tenant_ids(info)
        if allowed is None:
            return "all"
        joined = ",".join(str(t) for t in sorted(allowed))
        return "t" + hashlib.md5(joined.encode()).hexdigest()[:12]
