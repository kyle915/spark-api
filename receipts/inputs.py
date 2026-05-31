"""GraphQL inputs for the Consumer Receipt Validation feature."""

import strawberry

from utils.graphql.inputs import SparkGraphQLInput


@strawberry.input
class ConsumerReceiptFiltersInput(SparkGraphQLInput):
    """Filters for the tenant-scoped `receipts` admin queue.

    `tenant_id` is honored for admins (spark-admin / staff / Ignite) but
    forced to the caller's own tenant for client-role users — same scoping
    posture as the recaps list (see `receipts/queries.py`). `campaign_id`
    scopes to a single campaign's submissions; `paid` splits validated
    receipts into "awaiting payout" (False) vs "paid" (True).
    """

    tenant_id: strawberry.ID | None = None
    event_id: strawberry.ID | None = None
    campaign_id: strawberry.ID | None = None
    # One of ConsumerReceipt.STATUS_* ("pending" / "validated" / "rejected").
    status: str | None = None
    # True → only paid (paid_at set); False → only unpaid; None → either.
    paid: bool | None = None


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


@strawberry.input
class ReceiptCampaignFiltersInput(SparkGraphQLInput):
    """Filters for the tenant-scoped `receiptCampaigns` query."""

    tenant_id: strawberry.ID | None = None
    is_active: bool | None = None


@strawberry.input
class CreateReceiptCampaignInput(SparkGraphQLInput):
    """Input for `createReceiptCampaign`.

    `tenant_id` is honored for admins; client-role users are pinned to their
    own tenant. `slug` is optional — when blank the name is slugified and
    de-duped. `reward_amount` is the fixed payout per validated receipt.
    """

    name: str
    tenant_id: strawberry.ID | None = None
    headline: str | None = None
    description: str | None = None
    product: str | None = None
    reward_amount: float | None = None
    # Optional total payout ceiling. Omit/null = no cap.
    budget_cap: float | None = None
    payout_note: str | None = None
    is_active: bool | None = None
    slug: str | None = None


@strawberry.input
class UpdateReceiptCampaignInput(SparkGraphQLInput):
    """Input for `updateReceiptCampaign`. Only provided fields are changed."""

    id: strawberry.ID
    name: str | None = None
    headline: str | None = None
    description: str | None = None
    product: str | None = None
    reward_amount: float | None = None
    # Optional total payout ceiling. None = no change; <= 0 clears the cap.
    budget_cap: float | None = None
    payout_note: str | None = None
    is_active: bool | None = None
    slug: str | None = None


@strawberry.input
class MarkReceiptPaidInput(SparkGraphQLInput):
    """Input for `markReceiptPaid`.

    Stamps the payout audit on a *validated* receipt. `amount` optionally
    overrides the reward (else the receipt's snapshot, else the campaign
    reward); `payout_handle` optionally corrects the consumer's Venmo handle.
    """

    id: strawberry.ID
    amount: float | None = None
    payout_handle: str | None = None


@strawberry.input
class DeleteReceiptCampaignInput(SparkGraphQLInput):
    """Input for `deleteReceiptCampaign`. Empty campaign → hard delete;
    a campaign with receipts → soft-archive (data preserved)."""

    id: strawberry.ID


@strawberry.input
class RunReceiptOcrInput(SparkGraphQLInput):
    """Input for `runReceiptOcr` — admin-triggered OCR of one receipt."""

    id: strawberry.ID
