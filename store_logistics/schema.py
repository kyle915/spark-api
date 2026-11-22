"""GraphQL queries and mutations for Store Logistics Notes and universal
store-key lookup."""

from __future__ import annotations

import logging

import strawberry
from asgiref.sync import sync_to_async
from strawberry import relay

from recaps.explain_schema import _scope
from store_logistics.service import (
    resolve_store_key_for_target,
    save_store_logistics_note,
)
from utils.graphql.permissions import IsClientOrSparkAdmin, StrictIsAuthenticated

logger = logging.getLogger(__name__)


@strawberry.input
class SaveStoreLogisticsNotesInput:
    tenant_id: strawberry.ID
    store_key: str
    notes: str
    client_mutation_id: strawberry.ID | None = None


@strawberry.type
class SaveStoreLogisticsNotesPayload:
    success: bool
    message: str
    store_key: str | None = None
    logistics_notes: str = ""
    logistics_notes_updated_at: str | None = None
    client_mutation_id: strawberry.ID | None = None


@strawberry.type
class StoreLogisticsQueries:
    @strawberry.field(permission_classes=[StrictIsAuthenticated])
    async def resolve_store_key(
        self,
        info: strawberry.Info,
        tenant_id: strawberry.ID,
        store_key: str | None = None,
        request_id: strawberry.ID | None = None,
        recap_id: strawberry.ID | None = None,
        store_name: str | None = None,
        address: str | None = None,
    ) -> str | None:
        """Resolve a request, recap, address, or unambiguous store name to its
        canonical ``s<pk>`` storeKey within the caller's scoped brand."""
        target, staff = await _scope(info, tenant_id)
        if target is None:
            return None
        try:
            return await sync_to_async(
                lambda: resolve_store_key_for_target(
                    target,
                    staff=staff,
                    store_key=store_key,
                    request_id=str(request_id) if request_id else None,
                    recap_id=str(recap_id) if recap_id else None,
                    store_name=store_name,
                    address=address,
                ),
                thread_sensitive=True,
            )()
        except Exception as exc:  # noqa: BLE001
            logger.exception("resolveStoreKey failed tenant=%s: %s", target, exc)
            return None


@strawberry.type
class StoreLogisticsMutations:
    @relay.mutation(permission_classes=[IsClientOrSparkAdmin])
    async def save_store_logistics_notes(
        self,
        info: strawberry.Info,
        input: SaveStoreLogisticsNotesInput,
    ) -> SaveStoreLogisticsNotesPayload:
        """Save persistent door/store logistics notes for a physical store.

        Strictly tenant-scoped: brand users can only write notes for a store
        belonging to their own brand; Ignite staff can write for the requested
        brand.
        """
        user = info.context.request.user
        target, _staff = await _scope(info, input.tenant_id)
        if target is None:
            return SaveStoreLogisticsNotesPayload(
                success=False,
                message="You do not have access to this brand.",
                client_mutation_id=input.client_mutation_id,
            )

        def _save():
            return save_store_logistics_note(
                target,
                store_key=input.store_key,
                notes=input.notes,
                user=user,
            )

        row, err = await sync_to_async(_save, thread_sensitive=True)()
        if err or row is None:
            return SaveStoreLogisticsNotesPayload(
                success=False,
                message=err or "Could not save store logistics notes.",
                client_mutation_id=input.client_mutation_id,
            )

        return SaveStoreLogisticsNotesPayload(
            success=True,
            message="Store logistics notes saved.",
            store_key=row.store_key_hint or input.store_key,
            logistics_notes=row.notes or "",
            logistics_notes_updated_at=(
                row.updated_at.isoformat() if row.updated_at else None
            ),
            client_mutation_id=input.client_mutation_id,
        )
