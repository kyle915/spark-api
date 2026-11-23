"""Store recommendations and alerts for a brand (see ``brand_insights.service``).

One row per finding. A finding is worked out in code from approved recaps
(:mod:`brand_insights.detectors`); the model may only reword its sentence,
and every number in that wording must be one of the finding's own numbers.
A brand user sees a row only once Ignite staff have published it.
"""

from __future__ import annotations

from decimal import Decimal

from django.conf import settings
from django.db import models
from uuid6 import uuid7


class BrandInsight(models.Model):
    KIND_RECOMMENDATION = "recommendation"
    KIND_ALERT = "alert"
    KINDS = (KIND_RECOMMENDATION, KIND_ALERT)

    # draft: written, waiting for staff. published: the brand sees it.
    # hidden: staff said no. stale: a later run no longer found it (only
    # ever set on a draft, so nothing a brand saw changes under them).
    STATUS_DRAFT = "draft"
    STATUS_PUBLISHED = "published"
    STATUS_HIDDEN = "hidden"
    STATUS_STALE = "stale"
    STATUSES = (STATUS_DRAFT, STATUS_PUBLISHED, STATUS_HIDDEN, STATUS_STALE)

    SOURCE_MODEL = "model"
    SOURCE_TEMPLATE = "template"

    id = models.BigAutoField(primary_key=True)
    tenant = models.ForeignKey(
        "tenants.Tenant", on_delete=models.CASCADE, related_name="brand_insights"
    )
    kind = models.CharField(max_length=16)
    # Which detector found it ("top_store", "no_product_week", ...).
    code = models.CharField(max_length=40)
    # What it is about within the brand: "store:<key>", "chain:<key>" or
    # "brand". With (tenant, kind, code, period_start) it names one finding.
    scope_key = models.CharField(max_length=200)
    # The run it belongs to: a Monday. Recommendations are rebuilt each
    # week; an alert is about the week that starts here.
    period_start = models.DateField()
    period_end = models.DateField()
    # Order within its kind; lower first.
    rank = models.PositiveSmallIntegerField(default=0)
    # Store or chain name the finding is about, for the card's heading.
    subject = models.CharField(max_length=200, blank=True, default="")
    # The sentence built in code, and the sentence shown (the model's
    # rewording when it passed the number check, else the template).
    template = models.TextField()
    body = models.TextField()
    source = models.CharField(max_length=16, default=SOURCE_TEMPLATE)
    # The numbers the sentence was built from, as written lines and as
    # values. Nothing about a person.
    facts_text = models.TextField(blank=True, default="")
    facts = models.JSONField(default=dict)
    status = models.CharField(max_length=16, default=STATUS_DRAFT, db_index=True)
    generated_at = models.DateTimeField()
    reviewed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="brand_insights_reviewed",
    )
    reviewed_at = models.DateTimeField(null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["tenant", "kind", "code", "scope_key", "period_start"],
                name="brand_insight_finding_uniq",
            )
        ]
        indexes = [
            models.Index(
                fields=["tenant", "kind", "status", "period_start"],
                name="brand_insight_lookup_idx",
            )
        ]

    def __str__(self) -> str:  # pragma: no cover - admin/debug only
        return f"{self.kind} {self.code} {self.scope_key} ({self.status})"


class DemoBudgetTarget(models.Model):
    """Monthly demo budget target for a brand workspace (optional retailer /
    market breakdown). Used by the ROI pacing bar on ``/roi-pacing``."""

    SCOPE_WORKSPACE = "workspace"
    SCOPE_RETAILER = "retailer"
    SCOPE_MARKET = "market"
    SCOPE_CHOICES = [
        (SCOPE_WORKSPACE, "Workspace"),
        (SCOPE_RETAILER, "Retailer"),
        (SCOPE_MARKET, "Market"),
    ]

    id = models.BigAutoField(primary_key=True)
    uuid = models.UUIDField(default=uuid7, unique=True, editable=False)
    tenant = models.ForeignKey(
        "tenants.Tenant",
        on_delete=models.CASCADE,
        related_name="demo_budget_targets",
    )
    # Always stored as the 1st of the calendar month.
    month = models.DateField(db_index=True)
    scope_type = models.CharField(
        max_length=16, choices=SCOPE_CHOICES, default=SCOPE_WORKSPACE
    )
    scope_key = models.CharField(max_length=200, blank=True, default="")
    scope_label = models.CharField(max_length=200, blank=True, default="")
    target_amount = models.DecimalField(
        max_digits=12, decimal_places=2, default=Decimal("0.00")
    )
    notes = models.TextField(blank=True, default="")
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="demo_budget_targets_updated",
    )
    created_at = models.DateTimeField(auto_now_add=True, editable=False)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["tenant", "month", "scope_type", "scope_key"],
                name="brand_demo_budget_target_uniq",
            )
        ]
        indexes = [
            models.Index(
                fields=["tenant", "month"],
                name="brand_demo_budget_month_idx",
            )
        ]

    def __str__(self) -> str:  # pragma: no cover - admin/debug only
        return (
            f"{self.tenant_id} {self.month} {self.scope_type}:{self.scope_key} "
            f"= {self.target_amount}"
        )

class PhotoWallPin(models.Model):
    """A brand-curated in-store demo photo pinned from the Live Photo Wall
    for inclusion in shareable Buyer Pitch Decks.

    Strictly tenant-scoped: ``custom_recap_file`` must belong to an approved,
    non-archived, non-paperwork recap of ``tenant``.
    """

    id = models.BigAutoField(primary_key=True)
    uuid = models.UUIDField(default=uuid7, unique=True, editable=False)
    tenant = models.ForeignKey(
        "tenants.Tenant",
        on_delete=models.CASCADE,
        related_name="photo_wall_pins",
    )
    custom_recap_file = models.ForeignKey(
        "recaps.CustomRecapFile",
        on_delete=models.CASCADE,
        related_name="photo_wall_pins",
    )
    pinned_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="photo_wall_pins_created",
    )
    note = models.CharField(max_length=240, blank=True, default="")
    display_order = models.IntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["tenant", "custom_recap_file"],
                name="photo_wall_pin_tenant_file_uniq",
            )
        ]
        indexes = [
            models.Index(
                fields=["tenant", "display_order", "-created_at"],
                name="photo_wall_pin_tenant_idx",
            )
        ]

    def __str__(self) -> str:  # pragma: no cover - admin/debug only
        return f"PhotoWallPin tenant={self.tenant_id} file={self.custom_recap_file_id}"


class BuyerPitchDeck(models.Model):
    """A 1-click shareable, revocable Buyer Pitch Deck (``/pitch/:token``).

    Captures a buyer-safe snapshot of `/buyer-report` KPIs, sampling lift,
    optional opted-in cost-per-unit-sold, top store wins, retailer breakdown,
    and curated pinned demo photos for a single brand tenant. Public reads are
    strictly read-only and scoped solely to this deck's tenant snapshot and
    validated photo allowlist.
    """

    id = models.BigAutoField(primary_key=True)
    uuid = models.UUIDField(default=uuid7, unique=True, editable=False)
    tenant = models.ForeignKey(
        "tenants.Tenant",
        on_delete=models.CASCADE,
        related_name="buyer_pitch_decks",
    )
    token = models.CharField(max_length=64, unique=True, db_index=True)
    title = models.CharField(max_length=180)
    subtitle = models.CharField(max_length=280, blank=True, default="")
    buyer_name = models.CharField(max_length=140, blank=True, default="")
    intro_note = models.TextField(blank=True, default="")
    period_start = models.DateField()
    period_end = models.DateField()
    retailer_filter = models.CharField(max_length=120, blank=True, default="")
    market_filter = models.CharField(max_length=120, blank=True, default="")
    include_costs = models.BooleanField(default=False)
    passcode_hash = models.CharField(max_length=255, blank=True, default="")
    expires_at = models.DateTimeField(null=True, blank=True)
    revoked_at = models.DateTimeField(null=True, blank=True)
    revoked_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="buyer_pitch_decks_revoked",
    )
    pinned_photo_ids = models.JSONField(default=list, blank=True)
    snapshot_payload = models.JSONField(default=dict, blank=True)
    view_count = models.PositiveIntegerField(default=0)
    last_viewed_at = models.DateTimeField(null=True, blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="buyer_pitch_decks_created",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at", "-id"]
        indexes = [
            models.Index(
                fields=["tenant", "-created_at"],
                name="buyer_pitch_deck_tenant_idx",
            )
        ]

    @property
    def has_passcode(self) -> bool:
        return bool(self.passcode_hash)

    def __str__(self) -> str:  # pragma: no cover - admin/debug only
        return f"BuyerPitchDeck {self.uuid} ({self.tenant_id})"


class AttentionDismissal(models.Model):
    """Shared staff discard of one Today's attention notice.

    Fingerprint is ``{bucket}:{ref_id}`` — the same key the strip uses for a
    row. One active row per tenant fingerprint means every Ignite admin loses
    that notice on refresh (not a per-user local hide). Restoring stamps
    ``restored_at`` so audit stays on the row.
    """

    BUCKET_RECEIPT_CHECKS = "receipt_checks"
    BUCKET_MISSING_RECAPS = "missing_recaps"
    BUCKET_UNSTAFFED = "unstaffed"
    BUCKET_REBOOKS = "rebooks"
    BUCKET_CHOICES = [
        (BUCKET_RECEIPT_CHECKS, "Receipt checks"),
        (BUCKET_MISSING_RECAPS, "Missing recaps"),
        (BUCKET_UNSTAFFED, "Unstaffed (48h)"),
        (BUCKET_REBOOKS, "Rebook stores"),
    ]

    id = models.BigAutoField(primary_key=True)
    uuid = models.UUIDField(default=uuid7, unique=True, editable=False)
    tenant = models.ForeignKey(
        "tenants.Tenant",
        on_delete=models.CASCADE,
        related_name="attention_dismissals",
    )
    bucket = models.CharField(max_length=32, choices=BUCKET_CHOICES, db_index=True)
    ref_id = models.CharField(max_length=64)
    fingerprint = models.CharField(max_length=100)
    dismissed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="attention_dismissals_made",
    )
    dismissed_at = models.DateTimeField(auto_now_add=True, editable=False)
    restored_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="attention_dismissals_restored",
    )
    restored_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["tenant", "fingerprint"],
                condition=models.Q(restored_at__isnull=True),
                name="attention_dismissal_active_fp_uniq",
            )
        ]
        indexes = [
            models.Index(
                fields=["tenant", "bucket", "restored_at"],
                name="attention_dismissal_lookup_idx",
            )
        ]

    def __str__(self) -> str:  # pragma: no cover - admin/debug only
        return f"AttentionDismissal {self.fingerprint} ({self.tenant_id})"
