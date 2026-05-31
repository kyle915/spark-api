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


class ReceiptCampaign(models.Model):
    """A per-tenant, always-on consumer rebate campaign (GoToAisle-style).

    A brand (tenant) runs a campaign: consumers buy the product, upload a
    receipt via the campaign's public page (`/c/<slug>`, no login), and after
    an admin validates it the consumer is paid a fixed reward via Venmo.
    Unlike the old per-event upload link, a campaign is NOT tied to any single
    sampling event — it's a standing program for the client. New
    `ConsumerReceipt` rows attach to a campaign; `event` is kept only for
    legacy per-event rows.
    """

    id = models.BigAutoField(primary_key=True)
    uuid = models.UUIDField(default=uuid7, unique=True, editable=False)

    tenant = models.ForeignKey(
        Tenant,
        on_delete=models.RESTRICT,
        null=False,
        related_name="receipt_campaigns",
    )

    name = models.CharField(max_length=255)
    # Public URL key — the campaign's no-login page lives at /c/<slug>.
    # Globally unique so the public route is unambiguous; the create mutation
    # slugifies the name and de-dupes with a numeric suffix.
    slug = models.SlugField(max_length=80, unique=True)

    # Public-facing copy rendered on the upload page.
    headline = models.CharField(max_length=255, blank=True, default="")
    description = models.TextField(blank=True, default="")
    product = models.TextField(blank=True, default="")

    # Fixed reward paid per validated receipt; pre-fills the Venmo amount.
    reward_amount = models.DecimalField(
        max_digits=10, decimal_places=2, default=0
    )
    # Memo pre-filled into the Venmo payment note (defaults to the campaign
    # name when blank).
    payout_note = models.CharField(max_length=255, blank=True, default="")

    # Only active campaigns accept public submissions + render publicly.
    is_active = models.BooleanField(default=True, db_index=True)

    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="receipt_campaigns_created",
    )
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="receipt_campaigns_updated",
    )
    created_at = models.DateTimeField(auto_now_add=True, editable=False)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ("-created_at",)
        indexes = [
            models.Index(
                fields=["tenant", "is_active", "-created_at"],
                name="rcptcmp_tenant_active_idx",
            ),
        ]

    def __str__(self) -> str:
        return (
            f"ReceiptCampaign #{self.id} {self.name!r} tenant={self.tenant_id}"
        )


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
    # Campaign this receipt was submitted against (the GoToAisle-style global
    # program). SET_NULL so deleting a campaign never orphans the proof of
    # purchase. New submissions always carry a campaign; `event` above is kept
    # nullable only for legacy per-event rows.
    campaign = models.ForeignKey(
        "receipts.ReceiptCampaign",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="receipts",
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

    # Consumer payout details. v1 pays via Venmo; `payout_handle` is the
    # consumer's Venmo username (a public identifier, NOT a credential).
    # `payout_method` is kept as a column for future providers.
    payout_method = models.CharField(max_length=16, blank=True, default="venmo")
    payout_handle = models.CharField(max_length=255, null=True, blank=True)
    # Reward locked onto the receipt at validation/payout time so a later
    # change to the campaign's reward_amount doesn't rewrite history.
    reward_amount = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        null=True,
        blank=True,
    )

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

    # Payout audit. `paid_at` is stamped when an admin confirms they sent the
    # Venmo payment. Spark does NOT move money — the admin pays in Venmo, then
    # marks it paid here. A receipt is "awaiting payout" when validated with
    # paid_at NULL, and "paid" once paid_at is set.
    paid_at = models.DateTimeField(null=True, blank=True)
    paid_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="paid_consumer_receipts",
    )

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
