"""GraphQL inputs for client invoicing.

The mutation inputs extend ``SparkGraphQLInput`` so they carry the relay
``clientMutationId`` (propagated back through ``build_mutation_response``).
``InvoiceLineItemInput`` is a plain nested input (no clientMutationId — it's
just a row in a list). Per the SDL contract the line-item Floats are coerced
to 2dp Decimals server-side in the mutations.
"""

import strawberry

from utils.graphql.inputs import SparkGraphQLInput


@strawberry.input
class InvoiceLineItemInput:
    """One line on an invoice. ``quantity`` defaults to 1 and ``unitPrice``
    to 0 when omitted (handled in the mutation). ``eventId`` optionally links
    the line to the sampling event it bills for."""

    description: str
    quantity: float | None = None
    unit_price: float | None = None
    event_id: strawberry.ID | None = None


@strawberry.input
class InvoiceFiltersInput:
    """Filters for the tenant-scoped ``invoices`` list.

    A plain input (no ``clientMutationId`` — it's a read filter, not a
    mutation) so the SDL is exactly ``{ tenantId  status }``. ``tenant_id``
    is honored for admins (spark-admin / staff / Ignite) but forced to the
    caller's own tenant for client-role users — same scoping posture as the
    receipts list. ``status`` narrows to a single lifecycle state
    (draft / sent / paid / void).
    """

    tenant_id: strawberry.ID | None = None
    status: str | None = None


@strawberry.input
class CreateInvoiceInput(SparkGraphQLInput):
    """Input for ``createInvoice``.

    ``tenant_id`` is honored for admins; client-role users are pinned to their
    own tenant. ``tax_rate`` is a percent. ``line_items`` are created in the
    order given (sort_order = index)."""

    tenant_id: strawberry.ID | None = None
    issue_date: str | None = None
    due_date: str | None = None
    notes: str | None = None
    tax_rate: float | None = None
    line_items: list[InvoiceLineItemInput] | None = None


@strawberry.input
class UpdateInvoiceInput(SparkGraphQLInput):
    """Input for ``updateInvoice``. Only provided fields are changed.

    When ``line_items`` is provided it REPLACES all existing line items (the
    old rows are deleted and re-created from the list); omit it to leave the
    lines untouched."""

    id: strawberry.ID
    issue_date: str | None = None
    due_date: str | None = None
    notes: str | None = None
    tax_rate: float | None = None
    line_items: list[InvoiceLineItemInput] | None = None


@strawberry.input
class SetInvoiceStatusInput(SparkGraphQLInput):
    """Input for ``setInvoiceStatus``.

    ``status`` is one of draft / sent / paid / void. Crossing into ``sent``
    stamps ``sent_at`` (once); crossing into ``paid`` stamps ``paid_at``
    (once)."""

    id: strawberry.ID
    status: str


@strawberry.input
class DeleteInvoiceInput(SparkGraphQLInput):
    """Input for ``deleteInvoice`` — soft delete (stamps ``deleted_at``)."""

    id: strawberry.ID
