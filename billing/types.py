"""GraphQL types for client invoicing (clients schema).

``InvoiceType`` is a ``strawberry_django.type`` whose nodes are loaded
``Invoice`` model instances — exactly like ``receipts.types.ConsumerReceiptType``.
The line items are exposed as a resolved list of a plain ``InvoiceLineItemType``;
the parent ``tenant`` is read for ``clientName``.

Conventions copied from the receipts types:
  * All money / decimal columns are serialized as STRINGS (lossless Decimal),
    via the async-safe ``__dict__``-first read with a ``sync_to_async``-free
    ``getattr`` fallback — the same shape as ``ConsumerReceiptType.amount`` —
    so a column the optimizer deferred degrades to a safe reload rather than
    raising SynchronousOnlyOperation.
  * Date / datetime columns surface as ISO strings (or null).
  * ``shareToken`` is a ``TimestampSigner`` token over the invoice id (salt
    ``billing.invoice.v1``) the web client turns into the public link / PDF.
"""

from __future__ import annotations

from decimal import Decimal

import strawberry
import strawberry_django
from asgiref.sync import sync_to_async
from strawberry.relay import Node

from billing import models
from billing.tokens import make_invoice_token


def _money_str(instance, key: str) -> str:
    """Read a Decimal money column off an instance, as a string (e.g. "12.00").

    ``__dict__``-first so a value the optimizer loaded is read synchronously;
    falls back to ``getattr`` for a deferred column. Always 2dp.
    """
    value = instance.__dict__.get(key, None)
    if value is None:
        value = getattr(instance, key, None)
    if value is None:
        return "0.00"
    try:
        return f"{Decimal(str(value)):.2f}"
    except Exception:
        return str(value)


@strawberry.type(name="InvoiceLineItem")
class InvoiceLineItemType:
    """One billable line on an invoice. Money fields are strings.

    Exposed in the schema as ``InvoiceLineItem`` (the Python class keeps the
    ``Type`` suffix to read clearly alongside ``InvoiceType``)."""

    id: strawberry.ID
    description: str
    quantity: str
    unit_price: str
    amount: str
    event_id: strawberry.ID | None
    sort_order: int


def _line_item_to_type(item: models.InvoiceLineItem) -> InvoiceLineItemType:
    """Map a model line item onto its GraphQL type (money → strings)."""
    return InvoiceLineItemType(
        id=strawberry.ID(str(item.id)),
        description=item.description or "",
        quantity=_money_str(item, "quantity"),
        unit_price=_money_str(item, "unit_price"),
        amount=_money_str(item, "amount"),
        event_id=(
            strawberry.ID(str(item.event_id))
            if getattr(item, "event_id", None)
            else None
        ),
        sort_order=int(getattr(item, "sort_order", 0) or 0),
    )


@strawberry_django.type(models.Invoice, name="Invoice")
class InvoiceType(Node):
    """An invoice billing a tenant (client) for work done.

    Exposed in the schema as ``Invoice`` (the Python class keeps the ``Type``
    suffix to read clearly in the resolvers)."""

    uuid: str
    number: str
    status: str
    currency: str
    notes: str | None
    issue_date: str | None
    due_date: str | None
    sent_at: str | None
    paid_at: str | None
    created_at: str
    updated_at: str

    @strawberry.field
    async def client_name(self) -> str:
        """The billed client's name (``tenant.name``).

        async-safe: read the optimizer-loaded ``tenant`` relation from
        ``__dict__``; if it wasn't preloaded, fall back to a single guarded
        reload rather than raising under async.
        """
        tenant = self.__dict__.get("tenant")
        if tenant is None:
            def _reload():
                try:
                    return self.tenant
                except Exception:
                    return None
            tenant = await sync_to_async(_reload, thread_sensitive=True)()
        return getattr(tenant, "name", "") or "" if tenant is not None else ""

    @strawberry.field
    def subtotal(self) -> str:
        return _money_str(self, "subtotal")

    @strawberry.field
    def tax_rate(self) -> str:
        """Tax rate as a percent, serialized as a string (e.g. "8.25")."""
        return _money_str(self, "tax_rate")

    @strawberry.field
    def tax_amount(self) -> str:
        return _money_str(self, "tax_amount")

    @strawberry.field
    def total(self) -> str:
        return _money_str(self, "total")

    @strawberry.field
    async def line_items(self) -> list[InvoiceLineItemType]:
        """The invoice's line items, ordered (sort_order, id).

        async-safe: read the prefetched relation from ``__dict__`` when the
        list resolver preloaded it (``prefetch_related("line_items")``);
        otherwise fall back to a single guarded query.
        """
        cache = getattr(self, "_prefetched_objects_cache", None)
        if cache is not None and "line_items" in cache:
            items = list(cache["line_items"])
        else:
            def _load():
                return list(self.line_items.all())
            items = await sync_to_async(_load, thread_sensitive=True)()
        return [_line_item_to_type(item) for item in items]

    @strawberry.field
    def share_token(self) -> str:
        """Signed share token over the invoice id (public link + PDF auth)."""
        return make_invoice_token(self.id)


@strawberry.type
class InvoiceResponse:
    """Mutation response for create / update / setStatus / delete invoice."""

    success: bool
    message: str
    client_mutation_id: strawberry.ID | None = None
    invoice: InvoiceType | None = None
