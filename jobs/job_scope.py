"""Tenant-scoping shell for Jobs lifecycle / briefing / template ops.

Mirrors ``recaps/report_types.py`` ``_CampaignReportService``,
``tenants/forms.py`` ``_FormScope``, ``jobs/queries.py``
``_FavoriteAmbassadorScope`` and the round-1 ``academy.AcademyScope`` /
``announcements.AnnouncementScope``: clients are pinned to their OWN
tenant (any client-supplied ``tenant_id`` is ignored/overridden, and a
client-supplied job/template pk belonging to another tenant is rejected)
so a client can never post, staff, brief, or template another brand's
jobs, while admins (spark-admin / staff / superuser /
``@igniteproductions.co``) may target ANY tenant.

Lives in its own module so both ``jobs.queries`` and ``jobs.mutations``
share one implementation without an import cycle. All methods gate on a
caller who already passed ``StrictIsAuthenticated`` and never raise —
callers turn a None/empty scope into a safe ``[]`` / ``success=False`` /
no-op response, matching the favorites/forms/academy posture.
"""

from __future__ import annotations

import strawberry
from asgiref.sync import sync_to_async

from utils.graphql.mixins import SparkGraphQLMixin, resolve_id_to_int
from utils.graphql.permissions import (
    _is_admin_access,
    resolve_request_user_access,
)


class JobScope(SparkGraphQLMixin):
    """Resolve the concrete tenant a Jobs caller may operate on."""

    async def resolve_target_tenant_id(
        self, info: strawberry.Info, requested_tenant_id
    ) -> int | None:
        """The CONCRETE tenant id the caller may operate on, or None.

        * **client** — always their own bound tenant; ``requested_tenant_id``
          is ignored so a client can never create under another brand.
        * **admin** — the requested tenant id (global id or int), or None
          when none/garbage was passed (callers turn that into a safe
          ``[]`` / ``success=False`` rather than raising).
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

        Used by pk/uuid-addressed lookups (post/open/assign a job, set/apply
        a briefing, update/archive a template) to confirm the resource's
        tenant is in scope WITHOUT trusting a client-supplied id — mirrors
        ``_FormScope.accessible_tenant_ids`` / ``AcademyScope``.
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

    async def can_read_event_briefing(
        self, info: strawberry.Info, event_tenant_id: int | None, event_id: int | None
    ) -> bool:
        """Caller-aware gate for reading a job briefing keyed by EVENT.

        The ``jobBriefingForEvent`` query is the BA-mobile shift-offer entry
        point — a BA, a client/tenant member, OR an admin can all legitimately
        reach it — so a blunt tenant filter (like the sibling pk-addressed
        ``jobBriefing``) would lock out a BA who's been offered a shift but
        doesn't belong to the event's tenant. This resolves the caller's role
        and answers "may THIS caller read the briefing for THIS event":

        * **admin** (spark-admin / staff / superuser / ``@igniteproductions.co``)
          -> any event.
        * **tenant member** (client / spark user) -> only when the event's
          tenant is one they belong to (same membership set as
          ``accessible_tenant_ids``).
        * **BA (ambassador)** -> only when their ambassador is linked to the
          event via an ``AmbassadorEvent`` row — which covers a shift they've
          been OFFERED (``is_approved=False``) as well as one they've accepted
          / are rostered on (``is_approved=True``), so the mobile shift-offer
          flow keeps working.
        * **otherwise** -> denied.

        Gates a caller who already passed ``StrictIsAuthenticated`` and never
        raises — the resolver turns ``False`` into a ``null`` payload.
        """
        if event_id is None:
            return False

        user = await self.get_user(info)
        role_slug, is_staff, is_super, email = await resolve_request_user_access(
            user
        )

        # Admins: any event.
        if _is_admin_access(role_slug, is_staff, is_super, email):
            return True

        @sync_to_async
        def _allowed() -> bool:
            # Tenant member: the event's tenant must be one they belong to.
            if event_tenant_id is not None:
                tenant_ids = set(
                    user.tenanted_users.filter(is_active=True).values_list(
                        "tenant_id", flat=True
                    )
                )
                if event_tenant_id in tenant_ids:
                    return True

            # BA: their ambassador must be linked to the event (offered,
            # assigned, or on-roster) via AmbassadorEvent — the shift-offer
            # linkage. is_approved is intentionally NOT required so a BA who's
            # been offered but hasn't accepted still sees the briefing.
            from ambassadors.models import Ambassador, AmbassadorEvent

            ambassador_id = (
                Ambassador.objects.filter(user=user)
                .values_list("id", flat=True)
                .first()
            )
            if ambassador_id is None:
                return False
            return AmbassadorEvent.objects.filter(
                ambassador_id=ambassador_id, event_id=event_id
            ).exists()

        return await _allowed()
