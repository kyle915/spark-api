"""GraphQL types for the Consumer Receipt Validation feature (clients schema).

`ConsumerReceiptType` is a `strawberry_django.type` whose nodes are loaded
`ConsumerReceipt` model instances — exactly like `recaps.types.Recap`. The
related objects (`event`, `reviewed_by`) are declared as *typed auto
fields* so the active `DjangoOptimizerExtension` select_relateds them when
queried (no N+1, no per-row `sync_to_async` over a list — the discipline the
recaps resolvers adopted after a 71s prod incident).

The derived scalars (`publicUrl`, `amount`, `eventName`) use the async-safe
`__dict__`-first read with a `sync_to_async` fallback — the same shape as
`RecapFile.file_url` — so a column the optimizer deferred degrades to a
single safe reload rather than raising SynchronousOnlyOperation.
"""

from __future__ import annotations

import strawberry
import strawberry_django
from asgiref.sync import sync_to_async
from strawberry.relay import Node

from events import types as event_types
from tenants import types as tenant_types
from utils.gcs import extract_blob_name_from_url, public_url

from . import models


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

    # Review audit.
    review_note: str | None
    reviewed_at: str | None

    # Typed relations — the optimizer select_relateds these when queried, so
    # `event { name }` and `reviewedBy { email }` resolve with no N+1.
    event: event_types.Event | None
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
    def amount(self) -> str | None:
        """Purchase amount as a string (Decimal serialized losslessly)."""
        value = self.__dict__.get("amount", None)
        if value is None:
            value = getattr(self, "amount", None)
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


@strawberry.type
class EventReceiptUploadLinkType:
    """The public upload link + token for an event's receipts QR."""

    event_id: strawberry.ID
    token: str
    url: str


@strawberry.type
class ReviewReceiptResponse:
    """Mutation response for `reviewReceipt`."""

    success: bool
    message: str
    client_mutation_id: strawberry.ID | None = None
    receipt: ConsumerReceiptType | None = None
