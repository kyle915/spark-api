"""Sales assist: Spark numbers turned into DRAFT notes and emails in Close.

Nothing in this app sends an email or a text. It writes, at most, a note and
an email with status ``draft`` onto a Close lead, and a rep reviews and sends
it by hand. See ``docs/sales-assist.md``.

* :class:`CloseLink` — a staff-reviewed link from one Spark tenant (an
  existing client) to its Close lead. Nothing is ever linked automatically:
  the fuzzy name match only suggests.
* :class:`SalesProfile` — how a tenant may be described to a PROSPECT when
  its results are used as an anonymized example ("a national energy-drink
  brand"). Never the tenant's name.
* :class:`SalesAssistDraft` — the audit log, one row per draft considered
  (dry run, created, skipped or failed), and the idempotency record: at most
  one CREATED row per ``idem_key``.
* :class:`SalesAssistRun` — one row per cron run with its counts and the
  estimated AI cost.
* :class:`SalesAssistCursor` — small key/value store (the new-lead poll
  cursor).
"""

from __future__ import annotations

from django.conf import settings
from django.db import models
from django.db.models import Q


class CloseLink(models.Model):
    tenant = models.OneToOneField(
        "tenants.Tenant", on_delete=models.CASCADE, related_name="close_link"
    )
    close_lead_id = models.CharField(max_length=80)
    # Optional: the opportunity the renewal belongs to. Close notes and emails
    # live on the lead; the opportunity is named in the note for context.
    close_opportunity_id = models.CharField(max_length=80, blank=True, default="")
    # Optional: when the client's agreement renews. A renewal draft is made
    # ahead of this date (SALES_ASSIST_RENEWAL_LEAD_DAYS) as well as quarterly.
    renewal_date = models.DateField(null=True, blank=True)
    # Optional: who the draft email is addressed to. Blank = the first
    # contact on the Close lead with an email address.
    contact_email = models.EmailField(blank=True, default="")
    active = models.BooleanField(default=True)
    linked_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="+",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self) -> str:
        return f"{self.tenant_id} -> {self.close_lead_id}"


class SalesProfile(models.Model):
    SOURCE_AI = "ai"
    SOURCE_STAFF = "staff"

    tenant = models.OneToOneField(
        "tenants.Tenant", on_delete=models.CASCADE, related_name="sales_profile"
    )
    # Plain category words, used to find comparable results ("energy drink").
    category = models.CharField(max_length=120, blank=True, default="")
    # How the brand is described to a prospect: generic, never its name
    # ("a national energy-drink brand").
    descriptor = models.CharField(max_length=160, blank=True, default="")
    # Never use this tenant's numbers with a prospect, even anonymized.
    exclude_from_examples = models.BooleanField(default=False)
    source = models.CharField(max_length=12, default=SOURCE_AI)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self) -> str:
        return f"{self.tenant_id}: {self.descriptor or self.category}"


class SalesAssistRun(models.Model):
    action = models.CharField(max_length=32)
    applied = models.BooleanField(default=False)
    summary = models.JSONField(default=dict)
    ai_calls = models.PositiveIntegerField(default=0)
    est_cost_usd = models.DecimalField(max_digits=8, decimal_places=4, default=0)
    started_at = models.DateTimeField(auto_now_add=True)
    finished_at = models.DateTimeField(null=True, blank=True)


class SalesAssistDraft(models.Model):
    KIND_RENEWAL = "renewal"
    KIND_PROSPECT = "prospect"

    STATUS_DRY_RUN = "dry_run"
    STATUS_CREATED = "created"
    STATUS_SKIPPED = "skipped"
    STATUS_FAILED = "failed"

    run = models.ForeignKey(
        SalesAssistRun, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="drafts",
    )
    kind = models.CharField(max_length=16)
    idem_key = models.CharField(max_length=200)
    status = models.CharField(max_length=16)
    reason = models.CharField(max_length=300, blank=True, default="")
    tenant = models.ForeignKey(
        "tenants.Tenant", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="+",
    )
    close_lead_id = models.CharField(max_length=80, blank=True, default="")
    lead_name = models.CharField(max_length=255, blank=True, default="")
    period_start = models.DateField(null=True, blank=True)
    period_end = models.DateField(null=True, blank=True)
    # Everything the text was allowed to say (numbers checked against this).
    facts = models.JSONField(default=dict)
    note_text = models.TextField(blank=True, default="")
    email_to = models.CharField(max_length=255, blank=True, default="")
    email_subject = models.CharField(max_length=255, blank=True, default="")
    email_body = models.TextField(blank=True, default="")
    close_note_id = models.CharField(max_length=80, blank=True, default="")
    close_email_id = models.CharField(max_length=80, blank=True, default="")
    ai_calls = models.PositiveSmallIntegerField(default=0)
    est_cost_usd = models.DecimalField(max_digits=8, decimal_places=4, default=0)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            # Idempotency: one CREATED draft per key (a tenant's quarter, or a
            # lead). Dry runs, skips and failures can repeat freely.
            models.UniqueConstraint(
                fields=["idem_key"],
                condition=Q(status="created"),
                name="uniq_sales_assist_created_per_key",
            )
        ]
        indexes = [models.Index(fields=["kind", "-created_at"])]


class SalesAssistCursor(models.Model):
    name = models.CharField(max_length=64, unique=True)
    value = models.CharField(max_length=255, blank=True, default="")
    updated_at = models.DateTimeField(auto_now=True)
