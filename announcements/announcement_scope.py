"""Tenant-scoping shell for Announcement admin ops (clients schema).

Mirrors ``recaps/report_types.py`` ``_CampaignReportService``,
``tenants/forms.py`` ``_FormScope`` and ``jobs/queries.py``
``_FavoriteAmbassadorScope``: clients are pinned to their OWN tenant (any
client-supplied ``tenant_id``/``uuid`` is ignored/overridden so they can
never read, broadcast to, or delete another brand's announcements), while
admins (spark-admin / staff / superuser / ``@igniteproductions.co``) may
target ANY tenant.

Lives in its own module so both ``announcements.queries`` and
``announcements.mutations`` share one implementation without an import
cycle. All methods gate on a caller who already passed
``StrictIsAuthenticated`` and never raise — callers turn a None/empty scope
into a safe ``[]`` / ``success=False`` response.
"""

from __future__ import annotations

import strawberry
from asgiref.sync import sync_to_async

from utils.graphql.mixins import SparkGraphQLMixin, resolve_id_to_int
from utils.graphql.permissions import (
    _is_admin_access,
    resolve_request_user_access,
)


class AnnouncementScope(SparkGraphQLMixin):
    """Resolve the concrete tenant an Announcement caller may operate on."""

    async def resolve_target_tenant_id(
        self, info: strawberry.Info, requested_tenant_id
    ) -> int | None:
        """The CONCRETE tenant id the caller may operate on, or None.

        * **client** — always their own bound tenant; ``requested_tenant_id``
          is ignored so a client can never reach (or broadcast to) another
          brand.
        * **admin** — the requested tenant id (global id or int), or None when
          none/garbage was passed (callers turn that into a safe ``[]`` /
          ``success=False`` rather than raising).
        """
        user = await self.get_user(info)
        role_slug, is_staff, is_super, email = await resolve_request_user_access(
            user
        )

        if not _is_admin_access(role_slug, is_staff, is_super, email):
            tenant = await self.get_user_tenant(info)
            return tenant.id if tenant else None

        if requested_tenant_id is None:
            return None
        raw = str(requested_tenant_id).strip()
        if not raw:
            return None
        try:
            return resolve_id_to_int(raw)
        except Exception:  # noqa: BLE001
            return None

    async def accessible_tenant_ids(self, info: strawberry.Info) -> set[int] | None:
        """Tenant ids the caller may touch, or None for "any" (admins).

        Used by single-announcement lookups (delete) to confirm an
        announcement's tenant is in scope WITHOUT trusting a client-supplied
        id — mirrors ``_FormScope.accessible_tenant_ids``.
        """
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
