"""GraphQL mutations for the Consumer Receipt Validation feature (clients schema).

`reviewReceipt` is the admin action: flip a pending receipt to `validated`
or `rejected`, stamping the reviewer, the review time, and an optional note.
Tenant-scoped — a client-role user can only review receipts in their own
tenant; admins (spark-admin / staff / Ignite) review anything.
"""

from __future__ import annotations

import strawberry
from asgiref.sync import sync_to_async
from graphql import GraphQLError

from django.utils import timezone

from receipts import inputs, models, types
from receipts.queries import (
    ConsumerReceiptQueriesService,
    _require_admin_or_client,
)
from utils.graphql.mixins import SparkGraphQLMixin, resolve_id_to_int
from utils.graphql.permissions import StrictIsAuthenticated
from utils.graphql.relay import ensure_relay_mutation
from utils.utils import build_mutation_response

ensure_relay_mutation()
from strawberry import relay  # noqa: E402  (relay.mutation shim ensured above)

# Statuses an admin may move a receipt INTO via review. You can validate or
# reject, but you can't review something back to "pending".
_REVIEWABLE_STATUSES = {
    models.ConsumerReceipt.STATUS_VALIDATED,
    models.ConsumerReceipt.STATUS_REJECTED,
}


@strawberry.type
class ReceiptMutations:
    @relay.mutation(permission_classes=[StrictIsAuthenticated])
    async def review_receipt(
        self,
        info: strawberry.Info,
        input: inputs.ReviewReceiptInput,
    ) -> types.ReviewReceiptResponse:
        """Validate or reject a consumer receipt; stamp the reviewer + note."""
        try:
            await _require_admin_or_client(info)

            status = (input.status or "").strip().lower()
            if status not in _REVIEWABLE_STATUSES:
                raise GraphQLError(
                    "status must be 'validated' or 'rejected'."
                )

            try:
                receipt_id = resolve_id_to_int(input.id)
            except (TypeError, ValueError, GraphQLError):
                raise GraphQLError(f"Invalid ID: {input.id}")

            service = ConsumerReceiptQueriesService()
            user = await service.get_user(info)

            try:
                receipt = await sync_to_async(
                    models.ConsumerReceipt.objects.select_related(
                        "event", "tenant", "reviewed_by"
                    ).get
                )(id=receipt_id)
            except models.ConsumerReceipt.DoesNotExist:
                raise GraphQLError("Receipt not found.")

            # Tenant gate: a client-role user may only review their own
            # tenant's receipts. Admins pass through. Surfaced as
            # "not found" so we don't leak cross-tenant existence.
            if service.get_role_slug(user) == "client":
                user_tenant = await service.get_user_tenant(info)
                if receipt.tenant_id != user_tenant.id:
                    raise GraphQLError("Receipt not found.")

            receipt.status = status
            receipt.reviewed_by = user
            receipt.reviewed_at = timezone.now()
            receipt.review_note = (input.note or "").strip()
            await sync_to_async(receipt.save)(
                update_fields=[
                    "status",
                    "reviewed_by",
                    "reviewed_at",
                    "review_note",
                    "updated_at",
                ]
            )

            # Reload through the queries service queryset so the response's
            # nested fields (publicUrl / reviewedBy / eventName) read the
            # same select_related relations the list does.
            receipt = await sync_to_async(
                ConsumerReceiptQueriesService().get_queryset().get
            )(id=receipt.id)

            return build_mutation_response(
                types.ReviewReceiptResponse,
                success=True,
                message=f"Receipt {status}.",
                input_obj=input,
                receipt=receipt,
            )
        except GraphQLError as exc:
            return build_mutation_response(
                types.ReviewReceiptResponse,
                success=False,
                message=str(exc),
                input_obj=input,
            )
