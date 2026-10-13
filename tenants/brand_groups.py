"""Brand groups: Compare brands tab + per-user access grants.

Every number here is one brand's own figure, computed exactly the way that
brand's dashboard computes it (:func:`conversion_window_totals` — approved,
non-archived, 3rd-party excluded, Retail + On-Premise only). Nothing is
re-derived across brands. Shared traffic (store foot traffic, event
attendance) is never summed across brands: on a co-activation each brand's
recap carries the same shared count.
"""

from __future__ import annotations

from datetime import date

import strawberry
from asgiref.sync import sync_to_async
from django.db.models import Q
from graphql import GraphQLError

from recaps.models import CustomRecap, Recap
from recaps.tenant_overview import (
    _approved_only,
    _filter_event_window,
    _inclusive_dates_to_window,
    conversion_window_totals,
)
from tenants.models import (
    ARCHIVED_NAME_PREFIX,
    BrandGroup,
    BrandGroupAccess,
    Tenant,
    User,
)
from utils.graphql.mixins import SparkGraphQLMixin, resolve_id_to_int
from utils.graphql.permissions import (
    StrictIsAuthenticated,
    _is_admin_access,
    resolve_request_user_access,
)

COMPARE_NOTES = [
    "Each brand's numbers are that brand's own recaps — the same figures its dashboard shows.",
    "On a co-activation one shopper can sample more than one brand, so summing "
    "consumers sampled across brands counts sampling moments, not unique people.",
    "Shared traffic (store foot traffic, event attendance) is recorded once per "
    "activation and is never added up across brands.",
    "Conversion is Retail + On-Premise only: units sold ÷ consumers sampled.",
]


def _members(group: BrandGroup) -> list[Tenant]:
    return list(
        Tenant.objects.filter(brand_group_id=group.id)
        .exclude(name__istartswith=ARCHIVED_NAME_PREFIX)
        .order_by("name", "id")
    )


def _brand_row(tenant: Tenant, start: date | None, end: date | None, co_only: bool) -> dict:
    window = _inclusive_dates_to_window(start, end)
    custom = _approved_only(
        _filter_event_window(
            CustomRecap.objects.filter(tenant_id=tenant.id, archived_at__isnull=True),
            "event__",
            window,
        ),
        "",
    )
    legacy = _approved_only(
        _filter_event_window(
            Recap.objects.filter(event__tenant_id=tenant.id, archived_at__isnull=True),
            "event__",
            window,
        ),
        "",
    )
    co_keys = dict(
        custom.filter(co_activation_key__isnull=False).values_list("id", "co_activation_key")
    )
    if co_only:
        custom = custom.filter(co_activation_key__isnull=False)
        legacy = legacy.none()

    conv = conversion_window_totals(tenant.id, start, end, detail=True)
    rows = conv["recaps"]
    if co_only:
        rows = [r for r in rows if r["kind"] == "custom" and r["id"] in co_keys]
    retail_ids = {(r["kind"], r["id"]) for r in rows}
    counted = [r for r in rows if r["counted"]]
    sampled = sum(int(r["base"] or 0) for r in counted)
    units = sum(int(r["units"] or 0) for r in counted)

    total = custom.count() + legacy.count()
    return {
        "tenant_pk": tenant.id,
        "name": tenant.name,
        "recaps": total,
        "retail_recaps": len(retail_ids),
        "other_recaps": max(0, total - len(retail_ids)),
        "co_activation_recaps": len(co_keys),
        "consumers_sampled": sampled,
        "units_sold": units,
        "conversion_pct": round(units / sampled * 100, 1) if sampled else None,
        "_keys": {str(k) for k in co_keys.values()},
        "_solo": 0 if co_only else total - len(co_keys),
    }


def build_brand_group_compare(
    group: BrandGroup,
    start: date | None,
    end: date | None,
    co_activations_only: bool = False,
) -> dict:
    brands = [_brand_row(t, start, end, co_activations_only) for t in _members(group)]
    co_keys: set[str] = set()
    solo = 0
    for row in brands:
        co_keys |= row.pop("_keys")
        solo += max(0, row.pop("_solo"))
    return {
        "group_pk": group.id,
        "group_name": group.name,
        "start_date": start,
        "end_date": end,
        "co_activations_only": co_activations_only,
        "brands": brands,
        "activations": len(co_keys) + solo,
        "co_activations": len(co_keys),
        "notes": COMPARE_NOTES,
    }


def user_can_compare(user_id: int, group_id: int, is_admin: bool) -> bool:
    if is_admin:
        return True
    return BrandGroupAccess.objects.filter(brand_group_id=group_id, user_id=user_id).exists()


@strawberry.type
class BrandCompareRow:
    tenant_pk: int
    name: str
    recaps: int
    retail_recaps: int
    other_recaps: int
    co_activation_recaps: int
    consumers_sampled: int
    units_sold: int
    conversion_pct: float | None


@strawberry.type
class BrandGroupCompare:
    group_pk: int
    group_name: str
    start_date: date | None
    end_date: date | None
    co_activations_only: bool
    brands: list[BrandCompareRow]
    activations: int
    co_activations: int
    notes: list[str]


@strawberry.type
class BrandGroupAccessGrant:
    user_pk: int
    email: str
    name: str
    granted_at: str


class _Scope(SparkGraphQLMixin):
    async def caller(self, info: strawberry.Info) -> tuple[User, bool]:
        user = await self.get_user(info)
        role_slug, is_staff, is_super, email = await resolve_request_user_access(user)
        return user, _is_admin_access(role_slug, is_staff, is_super, email)


def _group_for_tenant(raw_tenant_id) -> BrandGroup | None:
    try:
        tenant_id = resolve_id_to_int(raw_tenant_id)
    except Exception:  # noqa: BLE001
        return None
    tenant = Tenant.objects.select_related("brand_group").filter(id=tenant_id).first()
    return tenant.brand_group if tenant else None


def _grants(group_id: int) -> list[BrandGroupAccessGrant]:
    rows = (
        BrandGroupAccess.objects.filter(brand_group_id=group_id)
        .select_related("user")
        .order_by("user__email")
    )
    return [
        BrandGroupAccessGrant(
            user_pk=row.user_id,
            email=row.user.email or "",
            name=" ".join(
                part for part in (row.user.first_name, row.user.last_name) if part
            ),
            granted_at=row.created_at.isoformat(),
        )
        for row in rows
    ]


@strawberry.type
class BrandGroupQueries:
    @strawberry.field(permission_classes=[StrictIsAuthenticated])
    async def can_compare_brands(self, info: strawberry.Info, tenant_id: strawberry.ID) -> bool:
        """Whether the caller may open this brand's Compare brands tab."""
        user, is_admin = await _Scope().caller(info)

        def _go() -> bool:
            group = _group_for_tenant(tenant_id)
            return bool(group) and user_can_compare(user.id, group.id, is_admin)

        return await sync_to_async(_go)()

    @strawberry.field(permission_classes=[StrictIsAuthenticated])
    async def brand_group_compare(
        self,
        info: strawberry.Info,
        tenant_id: strawberry.ID,
        start_date: date | None = None,
        end_date: date | None = None,
        co_activations_only: bool = False,
    ) -> BrandGroupCompare | None:
        """Side-by-side KPIs for every brand in ``tenant_id``'s group.

        Ignite staff, or users with an explicit brand-group grant. ``null``
        for everyone else.
        """
        user, is_admin = await _Scope().caller(info)

        def _go():
            group = _group_for_tenant(tenant_id)
            if group is None or not user_can_compare(user.id, group.id, is_admin):
                return None
            data = build_brand_group_compare(group, start_date, end_date, co_activations_only)
            return BrandGroupCompare(
                **{k: v for k, v in data.items() if k != "brands"},
                brands=[BrandCompareRow(**row) for row in data["brands"]],
            )

        return await sync_to_async(_go, thread_sensitive=True)()

    @strawberry.field(permission_classes=[StrictIsAuthenticated])
    async def brand_group_access(
        self, info: strawberry.Info, tenant_id: strawberry.ID
    ) -> list[BrandGroupAccessGrant]:
        """Users granted the Compare tab (Ignite staff only)."""
        _, is_admin = await _Scope().caller(info)
        if not is_admin:
            return []

        def _go():
            group = _group_for_tenant(tenant_id)
            return _grants(group.id) if group else []

        return await sync_to_async(_go)()


@strawberry.type
class BrandGroupMutations:
    @strawberry.mutation(permission_classes=[StrictIsAuthenticated])
    async def set_brand_group_access(
        self,
        info: strawberry.Info,
        tenant_id: strawberry.ID,
        email: str,
        granted: bool,
    ) -> list[BrandGroupAccessGrant]:
        """Grant or revoke one user's Compare tab access (Ignite staff only).

        The user must already belong to a brand in the group.
        """
        actor, is_admin = await _Scope().caller(info)
        if not is_admin:
            raise GraphQLError("Only Ignite staff can change brand group access.")

        def _go():
            group = _group_for_tenant(tenant_id)
            if group is None:
                raise GraphQLError("This brand isn't in a brand group.")
            address = (email or "").strip().lower()
            user = User.objects.filter(email__iexact=address).first()
            if user is None:
                raise GraphQLError("No Spark user with that email.")
            if granted:
                member = user.tenanted_users.filter(
                    Q(tenant__brand_group_id=group.id), is_active=True
                ).exists()
                if not member:
                    raise GraphQLError("That user isn't on any brand in this group.")
                BrandGroupAccess.objects.get_or_create(
                    brand_group=group, user=user, defaults={"granted_by": actor}
                )
            else:
                BrandGroupAccess.objects.filter(brand_group=group, user=user).delete()
            return _grants(group.id)

        return await sync_to_async(_go)()
