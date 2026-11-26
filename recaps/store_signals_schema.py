"""GraphQL query exposing extracted Field Store Signals for Account Map."""

from __future__ import annotations

import logging
import strawberry
from asgiref.sync import sync_to_async
from graphql import GraphQLError

from events.staffing_board import _accessible_tenants
from recaps.store_signals import list_account_map_store_signals
from utils.graphql.mixins import resolve_id_to_int
from utils.graphql.permissions import IsClientOrSparkAdmin

logger = logging.getLogger(__name__)


@strawberry.type
class StoreSignalItem:
    recap_uuid: str
    store_key: str
    store_name: str
    address: str
    city: str
    state_code: str
    event_date: str
    stockout_risk: str
    reorder_opportunity: str
    manager_contact: str
    manager_note: str
    competitor_brand: str
    competitor_promo: str
    rmm_email: str
    distributor_name: str


@strawberry.type
class StoreSignalQueries:
    @strawberry.field(permission_classes=[IsClientOrSparkAdmin])
    async def account_map_store_signals(
        self,
        info: strawberry.Info,
        tenant_id: strawberry.ID | None = None,
    ) -> list[StoreSignalItem]:
        """Recent store-level field intelligence (OOS risks, reorder leads, buyer notes, competitor promos)."""
        user = info.context.request.user
        is_admin, allowed_ids = await _accessible_tenants(user)

        resolved_tenant_id: int | None = None
        if tenant_id:
            try:
                resolved_tenant_id = resolve_id_to_int(tenant_id)
            except Exception as exc:  # noqa: BLE001
                raise GraphQLError("Invalid tenant id.") from exc

        if is_admin:
            if not resolved_tenant_id:
                return []
        else:
            allowed_set = set(allowed_ids or [])
            if not allowed_set:
                return []
            if resolved_tenant_id is None or resolved_tenant_id not in allowed_set:
                resolved_tenant_id = sorted(allowed_set)[0]

        try:
            rows = await sync_to_async(list_account_map_store_signals)(
                resolved_tenant_id,
                include_unapproved=bool(is_admin),
            )
        except Exception:  # noqa: BLE001
            logger.exception("account_map_store_signals failed tenant=%s", resolved_tenant_id)
            return []

        return [StoreSignalItem(**row) for row in rows]
