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
