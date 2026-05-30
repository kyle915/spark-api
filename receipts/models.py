"""Models for the Consumer Receipt Validation feature.

A `ConsumerReceipt` is proof-of-purchase a *shopper* (not a brand
ambassador) uploads via a public, no-login link tied to a sampling event.
Admins then manually review each one (validate / reject + note) in a
tenant-scoped queue. There is no OCR in v1 — every field besides the image
is optional and self-reported by the consumer.

This lives in its own `receipts` app — deliberately NOT in `recaps` — so it
never gets confused with `recaps.RecapFile`, which is the brand
ambassador's recap-attachment file (a different actor, a different flow).
The model mirrors the field/FK/timestamp conventions used across
`events` / `recaps` (BigAutoField pk, uuid7, `tenant` FK, `created_by`-style
audit columns) so it reads like the rest of the codebase.
"""

from uuid6 import uuid7

from django.conf import settings
from django.db import models

from tenants.models import Tenant


class ConsumerReceipt(models.Model):
    """A shopper-submitted purchase receipt awaiting admin review."""

    # Status lifecycle. `pending` on creation (public submit); an admin
    # flips it to `validated` or `rejected` via the `reviewReceipt`
    # mutation. db_index on the column (see field below) keeps the
    # tenant-scoped "pending queue" filter fast.
    STATUS_PENDING = "pending"
    STATUS_VALIDATED = "validated"
    STATUS_REJECTED = "rejected"

    STATUS_CHOICES = [
        (STATUS_PENDING, "Pending"),
        (STATUS_VALIDATED, "Validated"),
        (STATUS_REJECTED, "Rejected"),
    ]

    id = models.BigAutoField(primary_key=True)
    uuid = models.UUIDField(default=uuid7, unique=True, editable=False)

    tenant = models.ForeignKey(
        Tenant,
        on_delete=models.RESTRICT,
        null=False,
        related_name="consumer_receipts",
    )
    # Nullable: a tenant may mint a generic (non-event) upload link later,
    # and we never want a deleted/edited event to orphan a submitted
    # receipt. SET_NULL keeps the proof-of-purchase row even if the event
    # goes away.
    event = models.ForeignKey(
        "events.Event",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="consumer_receipts",
    )

    # GCS blob path (NOT a signed URL). Stored server-side by the public
    # upload view via utils.gcs.upload_bytes; surfaced to admins through
    # utils.gcs.public_url in the GraphQL type. Mirrors how recap files
    # store the blob path and resolve a public URL at read time.
    image = models.CharField(max_length=1024, null=False)

    submitted_at = models.DateTimeField(auto_now_add=True, db_index=True)

    # Optional, self-reported consumer + purchase details. All nullable —
    # v1 has no OCR, so the consumer types whatever they want (or nothing).
    consumer_name = models.CharField(max_length=255, null=True, blank=True)
    consumer_email = models.CharField(max_length=254, null=True, blank=True)
    consumer_phone = models.CharField(max_length=50, null=True, blank=True)
    store_name = models.CharField(max_length=255, null=True, blank=True)
    purchase_date = models.DateField(null=True, blank=True)
    amount = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        null=True,
        blank=True,
    )
    product = models.TextField(null=True, blank=True)

    status = models.CharField(
        max_length=16,
        choices=STATUS_CHOICES,
        default=STATUS_PENDING,
        db_index=True,
    )

    # Review audit. reviewed_by is the admin User who acted; reviewed_at is
    # stamped at review time; review_note is the admin's free-text reason
    # (especially useful on a rejection).
    reviewed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="reviewed_consumer_receipts",
    )
    review_note = models.TextField(blank=True, default="")
    reviewed_at = models.DateTimeField(null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True, editable=False)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ("-submitted_at",)
        indexes = [
            # The admin queue reads "this tenant's receipts, optionally
            # filtered by status, newest first". Composite index matches
            # that access pattern.
            models.Index(
                fields=["tenant", "status", "-submitted_at"],
                name="rcpt_tenant_status_sub_idx",
            ),
        ]

    def __str__(self) -> str:
        return f"ConsumerReceipt #{self.id} [{self.status}] tenant={self.tenant_id}"
