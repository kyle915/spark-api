"""GraphQL mutations for client invoicing (clients schema).

Four admin/console actions, all tenant-scoped exactly like the receipts
mutations (a client-role user only touches their own tenant's invoices;
admins touch anything):

* ``createInvoice`` — new draft invoice + its line items; totals computed.
* ``updateInvoice`` — patch mutable fields; when ``lineItems`` is provided it
  REPLACES all lines. Totals recomputed when lines / tax_rate change.
* ``setInvoiceStatus`` — draft / sent / paid / void; stamps ``sent_at`` when
  → sent and ``paid_at`` when → paid (once each).
* ``deleteInvoice`` — soft delete (stamps ``deleted_at``).

Conventions copied from ``receipts.mutations``: ``@relay.mutation`` with a
``StrictIsAuthenticated`` gate, the whole body wrapped so a ``GraphQLError``
returns a ``success=False`` response (never a transport error), the relay
``clientMutationId`` propagated via ``build_mutation_response``, and the
saved row reloaded through the queries service queryset so the response's
nested fields read the same select_related / prefetch the list does. The
``*Input`` Floats are coerced to 2dp Decimals server-side.
"""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal

import strawberry
from asgiref.sync import sync_to_async
from graphql import GraphQLError

from django.db import transaction
from django.utils import timezone
from django.utils.dateparse import parse_date

from billing import inputs, models, types
from billing.queries import (
    InvoiceQueriesService,
    _enforce_client_tenant,
    _require_admin_or_client,
)
from utils.graphql.mixins import resolve_id_to_int
from utils.graphql.permissions import StrictIsAuthenticated
from utils.graphql.relay import ensure_relay_mutation
from utils.utils import build_mutation_response

ensure_relay_mutation()
from strawberry import relay  # noqa: E402  (relay.mutation shim ensured above)

_CENTS = Decimal("0.01")

# Valid invoice statuses for `setInvoiceStatus`.
_VALID_STATUSES = {
    models.Invoice.STATUS_DRAFT,
    models.Invoice.STATUS_SENT,
    models.Invoice.STATUS_PAID,
    models.Invoice.STATUS_VOID,
}


def _to_decimal(value, *, default: Decimal = Decimal("0")) -> Decimal:
    """Coerce a float/str into a 2dp Decimal (default on bad/empty)."""
    if value is None:
        return default
    try:
        return Decimal(str(value)).quantize(_CENTS, rounding=ROUND_HALF_UP)
    except Exception:
        return default


def _parse_date_or_none(value):
    """Parse an ISO date string; return None for empty / unparseable input."""
    if value in (None, ""):
        return None
    return parse_date(str(value)[:10])


def _build_line_item_models(
    invoice: models.Invoice,
    line_inputs: list[inputs.InvoiceLineItemInput] | None,
) -> list[models.InvoiceLineItem]:
    """Turn the line-item inputs into (unsaved) ``InvoiceLineItem`` models.

    Skips lines with a blank description. ``quantity`` defaults to 1 and
    ``unit_price`` to 0; both coerce to 2dp Decimals. ``sort_order`` follows
    the order given. ``amount`` is left at 0 here — it's set by
    :func:`recompute_invoice_totals` before save.
    """
    items: list[models.InvoiceLineItem] = []
    for index, line in enumerate(line_inputs or []):
        description = (line.description or "").strip()
        if not description:
            continue
        event_id = None
        if getattr(line, "event_id", None) not in (None, ""):
            try:
                event_id = resolve_id_to_int(line.event_id)
            except (TypeError, ValueError, GraphQLError):
                event_id = None
        items.append(
            models.InvoiceLineItem(
                invoice=invoice,
                description=description,
                quantity=_to_decimal(line.quantity, default=Decimal("1")),
                unit_price=_to_decimal(line.unit_price, default=Decimal("0")),
                event_id=event_id,
                sort_order=index,
            )
        )
    return items


@strawberry.type
class BillingMutations:
    @relay.mutation(permission_classes=[StrictIsAuthenticated])
    async def create_invoice(
        self,
        info: strawberry.Info,
        input: inputs.CreateInvoiceInput,
    ) -> types.InvoiceResponse:
        """Create a draft invoice for a tenant, with its line items + totals."""
        try:
            await _require_admin_or_client(info)

            service = InvoiceQueriesService()
            user = await service.get_user(info)

            tenant_id_raw = (
                resolve_id_to_int(input.tenant_id)
                if input.tenant_id not in (None, "")
                else None
            )
            tenant_id = await _enforce_client_tenant(service, info, tenant_id_raw)
            if not tenant_id:
                raise GraphQLError("A tenant is required to create an invoice.")

            tax_rate = _to_decimal(input.tax_rate, default=Decimal("0"))
            issue_date = _parse_date_or_none(input.issue_date)
            due_date = _parse_date_or_none(input.due_date)
            notes = (input.notes or "").strip()
            line_inputs = input.line_items

            def _create() -> models.Invoice:
                with transaction.atomic():
                    invoice = models.Invoice.objects.create(
                        tenant_id=tenant_id,
                        status=models.Invoice.STATUS_DRAFT,
                        issue_date=issue_date,
                        due_date=due_date,
                        notes=notes,
                        tax_rate=tax_rate,
                        created_by=user,
                        updated_by=user,
                    )
                    items = _build_line_item_models(invoice, line_inputs)
                    models.recompute_invoice_totals(invoice, items)
                    if items:
                        models.InvoiceLineItem.objects.bulk_create(items)
                    invoice.save(
                        update_fields=[
                            "subtotal",
                            "tax_amount",
                            "total",
                            "updated_at",
                        ]
                    )
                    return invoice

            invoice = await sync_to_async(_create, thread_sensitive=True)()

            # Reload through the queries service queryset so the response's
            # nested fields (clientName / lineItems / totals) read the same
            # select_related / prefetch the list does.
            invoice = await sync_to_async(
                InvoiceQueriesService().get_queryset().get
            )(id=invoice.id)

            return build_mutation_response(
                types.InvoiceResponse,
                success=True,
                message=f"Invoice {invoice.number} created.",
                input_obj=input,
                invoice=invoice,
            )
        except GraphQLError as exc:
            return build_mutation_response(
                types.InvoiceResponse,
                success=False,
                message=str(exc),
                input_obj=input,
            )

    @relay.mutation(permission_classes=[StrictIsAuthenticated])
    async def update_invoice(
        self,
        info: strawberry.Info,
        input: inputs.UpdateInvoiceInput,
    ) -> types.InvoiceResponse:
        """Patch a draft/sent invoice; replace line items when provided."""
        try:
            await _require_admin_or_client(info)
            try:
                invoice_id = resolve_id_to_int(input.id)
            except (TypeError, ValueError, GraphQLError):
                raise GraphQLError(f"Invalid ID: {input.id}")

            service = InvoiceQueriesService()
            user = await service.get_user(info)

            try:
                invoice = await sync_to_async(
                    models.Invoice.objects.select_related("tenant").get
                )(id=invoice_id, deleted_at__isnull=True)
            except models.Invoice.DoesNotExist:
                raise GraphQLError("Invoice not found.")

            # Tenant gate: a client-role user may only edit their own tenant's
            # invoices. Surfaced as "not found" so we don't leak cross-tenant
            # existence.
            if service.get_role_slug(user) == "client":
                user_tenant = await service.get_user_tenant(info)
                if invoice.tenant_id != user_tenant.id:
                    raise GraphQLError("Invoice not found.")

            replace_lines = input.line_items is not None
            line_inputs = input.line_items
            # Snapshot the scalar patches outside the sync block.
            set_issue = input.issue_date is not None
            set_due = input.due_date is not None
            set_notes = input.notes is not None
            set_tax = input.tax_rate is not None
            issue_date = _parse_date_or_none(input.issue_date)
            due_date = _parse_date_or_none(input.due_date)
            notes = (input.notes or "").strip()
            tax_rate = _to_decimal(input.tax_rate, default=Decimal("0"))

            def _update() -> models.Invoice:
                with transaction.atomic():
                    update_fields = ["updated_by", "updated_at"]
                    invoice.updated_by = user
                    if set_issue:
                        invoice.issue_date = issue_date
                        update_fields.append("issue_date")
                    if set_due:
                        invoice.due_date = due_date
                        update_fields.append("due_date")
                    if set_notes:
                        invoice.notes = notes
                        update_fields.append("notes")
                    if set_tax:
                        invoice.tax_rate = tax_rate
                        update_fields.append("tax_rate")

                    # When lineItems is provided, REPLACE all of them. Build
                    # the new rows, recompute (which sets each line `amount`
                    # AND the invoice totals), then persist the rows.
                    if replace_lines:
                        invoice.line_items.all().delete()
                        items = _build_line_item_models(invoice, line_inputs)
                        models.recompute_invoice_totals(invoice, items)
                        if items:
                            models.InvoiceLineItem.objects.bulk_create(items)
                        update_fields += ["subtotal", "tax_amount", "total"]
                    elif set_tax:
                        # Tax changed but lines didn't — recompute totals from
                        # the existing (already-amounted) lines.
                        items = list(invoice.line_items.all())
                        models.recompute_invoice_totals(invoice, items)
                        update_fields += ["subtotal", "tax_amount", "total"]

                    invoice.save(update_fields=update_fields)
                    return invoice

            invoice = await sync_to_async(_update, thread_sensitive=True)()

            invoice = await sync_to_async(
                InvoiceQueriesService().get_queryset().get
            )(id=invoice.id)

            return build_mutation_response(
                types.InvoiceResponse,
                success=True,
                message="Invoice updated.",
                input_obj=input,
                invoice=invoice,
            )
        except GraphQLError as exc:
            return build_mutation_response(
                types.InvoiceResponse,
                success=False,
                message=str(exc),
                input_obj=input,
            )

    @relay.mutation(permission_classes=[StrictIsAuthenticated])
    async def set_invoice_status(
        self,
        info: strawberry.Info,
        input: inputs.SetInvoiceStatusInput,
    ) -> types.InvoiceResponse:
        """Move an invoice through its lifecycle; stamp sent_at / paid_at."""
        try:
            await _require_admin_or_client(info)

            status = (input.status or "").strip().lower()
            if status not in _VALID_STATUSES:
                raise GraphQLError(
                    "status must be one of draft / sent / paid / void."
                )

            try:
                invoice_id = resolve_id_to_int(input.id)
            except (TypeError, ValueError, GraphQLError):
                raise GraphQLError(f"Invalid ID: {input.id}")

            service = InvoiceQueriesService()
            user = await service.get_user(info)

            try:
                invoice = await sync_to_async(
                    models.Invoice.objects.select_related("tenant").get
                )(id=invoice_id, deleted_at__isnull=True)
            except models.Invoice.DoesNotExist:
                raise GraphQLError("Invoice not found.")

            if service.get_role_slug(user) == "client":
                user_tenant = await service.get_user_tenant(info)
                if invoice.tenant_id != user_tenant.id:
                    raise GraphQLError("Invoice not found.")

            update_fields = ["status", "updated_by", "updated_at"]
            invoice.status = status
            invoice.updated_by = user
            # Stamp the transition timestamps once. Moving INTO sent stamps
            # sent_at if not already set; moving INTO paid stamps paid_at.
            if status == models.Invoice.STATUS_SENT and invoice.sent_at is None:
                invoice.sent_at = timezone.now()
                update_fields.append("sent_at")
            if status == models.Invoice.STATUS_PAID:
                if invoice.sent_at is None:
                    # A paid invoice was necessarily sent; backfill sent_at so
                    # the timeline is coherent.
                    invoice.sent_at = timezone.now()
                    update_fields.append("sent_at")
                if invoice.paid_at is None:
                    invoice.paid_at = timezone.now()
                    update_fields.append("paid_at")

            await sync_to_async(invoice.save)(update_fields=update_fields)

            invoice = await sync_to_async(
                InvoiceQueriesService().get_queryset().get
            )(id=invoice.id)

            return build_mutation_response(
                types.InvoiceResponse,
                success=True,
                message=f"Invoice marked {status}.",
                input_obj=input,
                invoice=invoice,
            )
        except GraphQLError as exc:
            return build_mutation_response(
                types.InvoiceResponse,
                success=False,
                message=str(exc),
                input_obj=input,
            )

    @relay.mutation(permission_classes=[StrictIsAuthenticated])
    async def delete_invoice(
        self,
        info: strawberry.Info,
        input: inputs.DeleteInvoiceInput,
    ) -> types.InvoiceResponse:
        """Soft-delete an invoice (stamps ``deleted_at``); row is preserved."""
        try:
            await _require_admin_or_client(info)
            try:
                invoice_id = resolve_id_to_int(input.id)
            except (TypeError, ValueError, GraphQLError):
                raise GraphQLError(f"Invalid ID: {input.id}")

            service = InvoiceQueriesService()
            user = await service.get_user(info)

            try:
                invoice = await sync_to_async(
                    models.Invoice.objects.select_related("tenant").get
                )(id=invoice_id, deleted_at__isnull=True)
            except models.Invoice.DoesNotExist:
                raise GraphQLError("Invoice not found.")

            if service.get_role_slug(user) == "client":
                user_tenant = await service.get_user_tenant(info)
                if invoice.tenant_id != user_tenant.id:
                    raise GraphQLError("Invoice not found.")

            invoice.deleted_at = timezone.now()
            invoice.updated_by = user
            await sync_to_async(invoice.save)(
                update_fields=["deleted_at", "updated_by", "updated_at"]
            )

            return build_mutation_response(
                types.InvoiceResponse,
                success=True,
                message="Invoice deleted.",
                input_obj=input,
            )
        except GraphQLError as exc:
            return build_mutation_response(
                types.InvoiceResponse,
                success=False,
                message=str(exc),
                input_obj=input,
            )
