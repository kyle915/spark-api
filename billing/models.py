"""Models for client invoicing.

An ``Invoice`` bills a *tenant* (the brand / client Ignite serves) for work
done — a sampling program, an activation, a block of field-marketing hours.
Each invoice carries an ordered list of ``InvoiceLineItem`` rows and three
STORED money columns (subtotal / tax_amount / total) recomputed from those
lines whenever they (or ``tax_rate``) change.

This lives in its own ``billing`` app, deliberately separate from
``receipts`` (a *consumer* rebate, the other money flow) so the two never get
confused: a receipt pays a shopper out; an invoice bills the client. The
field / FK / timestamp conventions mirror ``receipts.models`` (BigAutoField
pk, uuid7, ``tenant`` FK on RESTRICT, ``created_by`` / ``updated_by`` audit
columns, a ``deleted_at`` soft-delete marker) so it reads like the rest of
the codebase.
"""

from decimal import ROUND_HALF_UP, Decimal

from uuid6 import uuid7

from django.conf import settings
from django.db import models

from tenants.models import Tenant

# Money rounds to cents, half-up — the convention an accountant expects.
_CENTS = Decimal("0.01")


def _quantize_money(value) -> Decimal:
    """Coerce a value into a 2dp Decimal (0.00 on anything unparseable)."""
    if value is None:
        return Decimal("0.00")
    try:
        return Decimal(str(value)).quantize(_CENTS, rounding=ROUND_HALF_UP)
    except Exception:
        return Decimal("0.00")


class Invoice(models.Model):
    """A bill issued to a tenant (client) for completed work.

    The human-facing ``number`` (``INV-00042``) is derived from the pk and
    stamped on the first save (see :meth:`save`). ``status`` walks
    draft → sent → paid (or → void); ``sent_at`` / ``paid_at`` are stamped by
    the ``setInvoiceStatus`` mutation when it crosses those transitions. The
    three money columns are STORED (not derived at read time) and recomputed
    by :func:`recompute_invoice_totals` whenever line items or ``tax_rate``
    change.
    """

    # Status lifecycle. `draft` on creation; an admin moves it to `sent`
    # (stamps sent_at), `paid` (stamps paid_at), or `void`. db_index on the
    # column keeps the tenant-scoped status filter fast.
    STATUS_DRAFT = "draft"
    STATUS_SENT = "sent"
    STATUS_PAID = "paid"
    STATUS_VOID = "void"

    STATUS_CHOICES = [
        (STATUS_DRAFT, "Draft"),
        (STATUS_SENT, "Sent"),
        (STATUS_PAID, "Paid"),
        (STATUS_VOID, "Void"),
    ]

    id = models.BigAutoField(primary_key=True)
    uuid = models.UUIDField(default=uuid7, unique=True, editable=False)

    tenant = models.ForeignKey(
        Tenant,
        on_delete=models.RESTRICT,
        null=False,
        related_name="invoices",
    )

    # Human-facing invoice number, `INV-{id:05d}`. Generated AFTER the first
    # save (the pk isn't known until then); unique so two invoices never share
    # a number. Blank only in the instant between INSERT and the follow-up
    # save() inside :meth:`save`.
    number = models.CharField(max_length=32, unique=True, blank=True)

    status = models.CharField(
        max_length=16,
        choices=STATUS_CHOICES,
        default=STATUS_DRAFT,
        db_index=True,
    )

    issue_date = models.DateField(null=True, blank=True)
    due_date = models.DateField(null=True, blank=True)

    currency = models.CharField(max_length=8, default="USD")
    notes = models.TextField(blank=True, default="")

    # Tax rate as a PERCENT (e.g. 8.25 = 8.25%), applied to the subtotal.
    tax_rate = models.DecimalField(max_digits=5, decimal_places=2, default=0)

    # STORED money columns, recomputed from the line items (and tax_rate) by
    # `recompute_invoice_totals` whenever those change. Kept on the row so
    # list / PDF reads don't re-aggregate the children every time.
    subtotal = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    tax_amount = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    total = models.DecimalField(max_digits=12, decimal_places=2, default=0)

    # Stamped when the invoice crosses into `sent` / `paid` (see the
    # setInvoiceStatus mutation). Null until then.
    sent_at = models.DateTimeField(null=True, blank=True)
    paid_at = models.DateTimeField(null=True, blank=True)

    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="invoices_created",
    )
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="invoices_updated",
    )
    created_at = models.DateTimeField(auto_now_add=True, editable=False)
    updated_at = models.DateTimeField(auto_now=True)

    # Soft-delete marker. A deleted invoice is excluded from the list + the
    # public link but its row survives so the number is never reused and the
    # billing history stays intact. Mirrors `receipts` / `recaps`.
    deleted_at = models.DateTimeField(null=True, blank=True, db_index=True)

    class Meta:
        ordering = ("-created_at",)
        indexes = [
            # The admin list reads "this tenant's invoices, optionally
            # filtered by status, newest first" — match that access pattern.
            models.Index(
                fields=["tenant", "status", "-created_at"],
                name="inv_tenant_status_created_idx",
            ),
        ]

    def __str__(self) -> str:
        return f"Invoice {self.number or f'#{self.id}'} tenant={self.tenant_id}"

    def save(self, *args, **kwargs):
        """Persist the row, generating ``number`` from the pk on first save.

        The number `INV-{id:05d}` needs the pk, which only exists after the
        INSERT, so a brand-new invoice is saved once to mint the id and then
        re-saved (number only) to stamp the derived number. An update with a
        narrow ``update_fields`` is left untouched so the totals/status saves
        stay cheap.
        """
        creating = self._state.adding and not self.number
        super().save(*args, **kwargs)
        if creating and not self.number:
            self.number = f"INV-{self.id:05d}"
            # Only write the one column we just derived; don't disturb the
            # row we just inserted (and don't recurse through this branch).
            super().save(update_fields=["number"])


class InvoiceLineItem(models.Model):
    """One billable line on an invoice (description × quantity × unit price).

    ``amount`` is stored (= quantity × unit_price) and recomputed alongside
    the parent's subtotal/tax/total by :func:`recompute_invoice_totals`. An
    optional ``event`` FK links the line to the sampling event it bills for
    (SET_NULL so deleting an event never destroys the billing row).
    """

    id = models.BigAutoField(primary_key=True)

    invoice = models.ForeignKey(
        Invoice,
        on_delete=models.CASCADE,
        related_name="line_items",
    )

    description = models.TextField()
    quantity = models.DecimalField(max_digits=10, decimal_places=2, default=1)
    unit_price = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    # Stored line total (= quantity * unit_price), set by recompute helper.
    amount = models.DecimalField(max_digits=12, decimal_places=2, default=0)

    event = models.ForeignKey(
        "events.Event",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="invoice_line_items",
    )

    sort_order = models.IntegerField(default=0)

    class Meta:
        ordering = ("sort_order", "id")

    def __str__(self) -> str:
        return f"InvoiceLineItem #{self.id} invoice={self.invoice_id}"


def recompute_invoice_totals(invoice: Invoice, line_items) -> None:
    """Recompute stored line + invoice money columns IN MEMORY.

    Given an ``invoice`` and an iterable of its ``line_items``, this:
      * sets each line's ``amount = quantity * unit_price`` (2dp),
      * sets ``invoice.subtotal = sum(amounts)``,
      * sets ``invoice.tax_amount = subtotal * tax_rate / 100`` (2dp),
      * sets ``invoice.total = subtotal + tax_amount``.

    It does NOT save — the caller persists the line items and the invoice
    (so a single mutation can recompute then write everything in one
    transaction). Call it whenever the line items or ``tax_rate`` change.
    """
    subtotal = Decimal("0.00")
    for item in line_items:
        amount = _quantize_money(item.quantity) * _quantize_money(item.unit_price)
        item.amount = amount.quantize(_CENTS, rounding=ROUND_HALF_UP)
        subtotal += item.amount

    subtotal = subtotal.quantize(_CENTS, rounding=ROUND_HALF_UP)
    rate = _quantize_money(invoice.tax_rate)
    tax_amount = (subtotal * rate / Decimal("100")).quantize(
        _CENTS, rounding=ROUND_HALF_UP
    )

    invoice.subtotal = subtotal
    invoice.tax_amount = tax_amount
    invoice.total = (subtotal + tax_amount).quantize(
        _CENTS, rounding=ROUND_HALF_UP
    )
