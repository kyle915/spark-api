"""GraphQL types for the Consumer Receipt Validation feature (clients schema).

`ConsumerReceiptType` is a `strawberry_django.type` whose nodes are loaded
`ConsumerReceipt` model instances — exactly like `recaps.types.Recap`. The
related objects (`event`, `campaign`, `reviewed_by`) are declared as *typed
auto fields* so the active `DjangoOptimizerExtension` select_relateds them
when queried (no N+1, no per-row `sync_to_async` over a list — the discipline
the recaps resolvers adopted after a 71s prod incident).

The derived scalars (`publicUrl`, `amount`, `rewardAmount`, `eventName`,
`campaignName`, `payoutLink`) use the async-safe `__dict__`-first read with a
`sync_to_async` fallback — the same shape as `RecapFile.file_url` — so a
column the optimizer deferred degrades to a single safe reload rather than
raising SynchronousOnlyOperation.

`payoutLink` is a PayPal.Me web link pre-filled with the consumer's handle and
the reward amount. Spark never moves money — the link just opens PayPal for
the admin to send (and then mark paid).
"""

from __future__ import annotations

from decimal import Decimal

import strawberry
import strawberry_django
from asgiref.sync import sync_to_async
from strawberry.relay import Node

from events import types as event_types
from tenants import types as tenant_types
from utils.gcs import extract_blob_name_from_url, public_url

from . import models


def _anno_int(instance, key: str) -> int:
    """Read an aggregate annotation off a model instance (0 when absent)."""
    value = instance.__dict__.get(key, None)
    return int(value) if value is not None else 0


def _anno_decimal(instance, key: str) -> Decimal:
    """Read a Decimal aggregate annotation off an instance (0 when absent)."""
    value = instance.__dict__.get(key, None)
    return value if value is not None else Decimal("0")


@strawberry_django.type(models.ReceiptCampaign)
class ReceiptCampaignType(Node):
    """A per-tenant consumer rebate campaign (GoToAisle-style)."""

    uuid: str
    name: str
    slug: str
    headline: str | None
    description: str | None
    product: str | None
    payout_note: str | None
    is_active: bool
    created_at: str
    updated_at: str

    @strawberry.field
    def reward_amount(self) -> str:
        """Fixed reward per validated receipt, as a string (e.g. "5.00")."""
        value = self.__dict__.get("reward_amount", None)
        if value is None:
            value = getattr(self, "reward_amount", None)
        return str(value) if value is not None else "0"

    # Aggregate counts for the dashboard cards. Populated by the
    # `receiptCampaigns` resolver's `.annotate()`; default to 0 when the
    # campaign is loaded as a relation (e.g. ConsumerReceipt.campaign) without
    # them.
    @strawberry.field
    def receipts_count(self) -> int:
        return _anno_int(self, "receipts_count")

    @strawberry.field
    def pending_count(self) -> int:
        return _anno_int(self, "pending_count")

    @strawberry.field
    def validated_count(self) -> int:
        return _anno_int(self, "validated_count")

    @strawberry.field
    def paid_count(self) -> int:
        return _anno_int(self, "paid_count")

    @strawberry.field
    def budget_cap(self) -> str | None:
        """Total payout ceiling as a string, or null when uncapped."""
        value = self.__dict__.get("budget_cap", None)
        if value is None:
            value = getattr(self, "budget_cap", None)
        return f"{value:.2f}" if value is not None else None

    @strawberry.field
    def total_paid(self) -> str:
        """Sum of rewards already paid out (annotated by receiptCampaigns)."""
        return f"{_anno_decimal(self, 'total_paid'):.2f}"

    @strawberry.field
    def total_committed(self) -> str:
        """Sum of validated-but-unpaid rewards (annotated)."""
        return f"{_anno_decimal(self, 'total_committed'):.2f}"

    @strawberry.field
    def budget_remaining(self) -> str | None:
        """Cap minus paid, as a string; null when uncapped."""
        cap = self.__dict__.get("budget_cap", None)
        if cap is None:
            cap = getattr(self, "budget_cap", None)
        if cap is None:
            return None
        return f"{(cap - _anno_decimal(self, 'total_paid')):.2f}"


@strawberry_django.type(models.ConsumerReceipt)
class ConsumerReceiptType(Node):
    """A shopper-submitted purchase receipt in the admin review queue."""

    uuid: str
    status: str
    submitted_at: str
    created_at: str
    updated_at: str

    event_id: strawberry.ID | None

    # Optional self-reported fields.
    consumer_name: str | None
    consumer_email: str | None
    consumer_phone: str | None
    store_name: str | None
    purchase_date: str | None
    product: str | None

    # Consumer payout details (v1: PayPal).
    payout_method: str | None
    payout_handle: str | None
    paid_at: str | None

    # Fraud / duplicate flag + OCR auto-read. Declared nullable to match the
    # SDL contract even though the underlying columns are non-null-with-default.
    is_flagged: bool
    flag_reason: str | None
    ocr_store: str | None
    ocr_date: str | None
    ocr_text: str | None
    ocr_ran_at: str | None

    # Review audit.
    review_note: str | None
    reviewed_at: str | None

    # Typed relations — the optimizer select_relateds these when queried, so
    # `event { name }`, `campaign { name }`, and `reviewedBy { email }` resolve
    # with no N+1.
    event: event_types.Event | None
    campaign: ReceiptCampaignType | None
    reviewed_by: tenant_types.SparkUserType | None

    @strawberry.field
    async def event_name(self) -> str | None:
        """Name of the event this receipt was submitted against.

        Convenience scalar so the queue can render the event label without
        selecting the whole `event` node. async-safe: read the loaded
        relation from __dict__; if the optimizer didn't preload it, fall
        back to a single guarded reload.
        """
        if not getattr(self, "event_id", None):
            return None
        event = self.__dict__.get("event")
        if event is None:
            def _reload():
                try:
                    return self.event
                except Exception:
                    return None
            event = await sync_to_async(_reload, thread_sensitive=True)()
        return getattr(event, "name", None) if event is not None else None

    @strawberry.field
    async def campaign_name(self) -> str | None:
        """Name of the campaign this receipt was submitted against."""
        if not getattr(self, "campaign_id", None):
            return None
        campaign = self.__dict__.get("campaign")
        if campaign is None:
            def _reload():
                try:
                    return self.campaign
                except Exception:
                    return None
            campaign = await sync_to_async(_reload, thread_sensitive=True)()
        return getattr(campaign, "name", None) if campaign is not None else None

    @strawberry.field
    def amount(self) -> str | None:
        """Purchase amount as a string (Decimal serialized losslessly)."""
        value = self.__dict__.get("amount", None)
        if value is None:
            value = getattr(self, "amount", None)
        return str(value) if value is not None else None

    @strawberry.field
    def reward_amount(self) -> str | None:
        """Reward locked onto this receipt at validation/payout (string)."""
        value = self.__dict__.get("reward_amount", None)
        if value is None:
            value = getattr(self, "reward_amount", None)
        return str(value) if value is not None else None

    @strawberry.field
    def ocr_amount(self) -> str | None:
        """OCR-extracted purchase amount as a string."""
        value = self.__dict__.get("ocr_amount", None)
        if value is None:
            value = getattr(self, "ocr_amount", None)
        return str(value) if value is not None else None

    @strawberry.field(name="publicUrl")
    def public_url_field(self) -> str | None:
        """Public (unsigned) URL for the stored receipt image.

        `image` holds the GCS blob path; resolve it to a public URL the
        same way recap files / tenant logos do (signing fails on Cloud Run
        service accounts, so we serve the unsigned public-bucket URL).
        """
        blob = self.__dict__.get("image", None)
        if blob is None:
            blob = getattr(self, "image", None)
        if not blob:
            return None
        return public_url(extract_blob_name_from_url(blob))

    @strawberry.field
    async def payout_link(self) -> str | None:
        """A PayPal.Me link pre-filled to pay this consumer.

        Spark never moves money — this just opens PayPal.Me with the
        recipient handle and reward amount pre-filled, so the admin can send
        the payment and then mark the receipt paid. Null when the consumer
        left no payout handle. PayPal.Me links don't support a memo
        parameter, so (unlike the old Venmo link) no note is appended.
        """
        handle = str(
            self.__dict__.get("payout_handle")
            or getattr(self, "payout_handle", "")
            or ""
        ).strip().lstrip("@")
        if not handle:
            return None

        amt = self.__dict__.get("reward_amount", None)
        if amt is None:
            amt = getattr(self, "reward_amount", None)

        if amt is None and getattr(self, "campaign_id", None):
            campaign = self.__dict__.get("campaign")
            if campaign is None:
                def _reload():
                    try:
                        return self.campaign
                    except Exception:
                        return None
                campaign = await sync_to_async(_reload, thread_sensitive=True)()
            if campaign is not None:
                amt = getattr(campaign, "reward_amount", None)

        if amt is None:
            return f"https://paypal.me/{handle}"
        try:
            amt_str = f"{amt:.2f}"
        except (TypeError, ValueError):
            amt_str = str(amt)
        return f"https://paypal.me/{handle}/{amt_str}"


@strawberry.type
class EventReceiptUploadLinkType:
    """The public upload link + token for an event's receipts QR.

    Legacy per-event surface — superseded by per-campaign public pages
    (`/c/<slug>`). Kept so existing minted links keep resolving.
    """

    event_id: strawberry.ID
    token: str
    url: str


@strawberry.type
class ReviewReceiptResponse:
    """Mutation response for `reviewReceipt` and `markReceiptPaid`."""

    success: bool
    message: str
    client_mutation_id: strawberry.ID | None = None
    receipt: ConsumerReceiptType | None = None


@strawberry.type
class ReceiptCampaignResponse:
    """Mutation response for `createReceiptCampaign` / `updateReceiptCampaign`."""

    success: bool
    message: str
    client_mutation_id: strawberry.ID | None = None
    campaign: ReceiptCampaignType | None = None
