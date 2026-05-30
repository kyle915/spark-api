"""GraphQL inputs for the Consumer Receipt Validation feature."""

import strawberry

from utils.graphql.inputs import SparkGraphQLInput


@strawberry.input
class ConsumerReceiptFiltersInput(SparkGraphQLInput):
    """Filters for the tenant-scoped `receipts` admin queue.

    `tenant_id` is honored for admins (spark-admin / staff / Ignite) but
    forced to the caller's own tenant for client-role users — same scoping
    posture as the recaps list (see `receipts/queries.py`).
    """

    tenant_id: strawberry.ID | None = None
    event_id: strawberry.ID | None = None
    # One of ConsumerReceipt.STATUS_* ("pending" / "validated" / "rejected").
    status: str | None = None


@strawberry.input
class ReviewReceiptInput(SparkGraphQLInput):
    """Input for the `reviewReceipt` mutation.

    `id` is the receipt's numeric id (or a relay global id — resolved via
    `resolve_id_to_int`). `status` must be "validated" or "rejected"
    (you can't review something back to "pending"). `note` is optional
    free text stored as the review note.
    """

    id: strawberry.ID
    status: str
    note: str | None = None
