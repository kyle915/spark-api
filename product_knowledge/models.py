"""Text the BA product-questions box can answer from, beyond staff notes.

One row per source per brand: a page of the brand's website, a product from
its Shopify ``products.json``, or a document staff uploaded to Spark for the
brand (training-hub PDFs, check-in resource PDFs, BA briefing templates and
their attachments). The text is extracted once and re-extracted only when the
fetched bytes change (``raw_hash``). Retrieval chunks it at question time —
see ``product_knowledge.retrieval``.

Website and document text is UNTRUSTED: it is only ever put in a prompt as
fenced data. Nothing about any ambassador is stored here.
"""

from __future__ import annotations

from django.db import models


class KnowledgeSource(models.Model):
    KIND_WEBSITE_PAGE = "website_page"
    KIND_SHOPIFY_PRODUCT = "shopify_product"
    KIND_TRAINING_DOC = "training_doc"
    KIND_CHECKIN_RESOURCE = "checkin_resource"
    KIND_BRIEFING = "briefing"
    KIND_BRIEFING_ATTACHMENT = "briefing_attachment"
    KIND_CHOICES = (
        (KIND_WEBSITE_PAGE, "Website page"),
        (KIND_SHOPIFY_PRODUCT, "Website product"),
        (KIND_TRAINING_DOC, "Training hub document"),
        (KIND_CHECKIN_RESOURCE, "Check-in resource"),
        (KIND_BRIEFING, "BA briefing"),
        (KIND_BRIEFING_ATTACHMENT, "BA briefing attachment"),
    )
    WEBSITE_KINDS = (KIND_WEBSITE_PAGE, KIND_SHOPIFY_PRODUCT)

    STATUS_OK = "ok"
    STATUS_EMPTY = "empty"
    STATUS_ERROR = "error"
    STATUS_CHOICES = (
        (STATUS_OK, "Text extracted"),
        (STATUS_EMPTY, "No readable text"),
        (STATUS_ERROR, "Could not fetch or read"),
    )

    id = models.BigAutoField(primary_key=True)
    tenant = models.ForeignKey(
        "tenants.Tenant", on_delete=models.CASCADE, related_name="knowledge_sources"
    )
    kind = models.CharField(max_length=24, choices=KIND_CHOICES)
    # Stable identity within the tenant: the canonical page URL for website
    # rows, "training_resource:12" / "briefing_template:3" etc. for documents.
    # An exclusion survives refreshes because the row is found again by ref.
    ref = models.CharField(max_length=600)
    url = models.CharField(max_length=1024, blank=True, default="")
    title = models.CharField(max_length=300, blank=True, default="")
    text = models.TextField(blank=True, default="")
    # sha256 of `text` (what the model sees) and of the fetched bytes (what
    # decides whether to re-extract).
    content_hash = models.CharField(max_length=64, blank=True, default="")
    raw_hash = models.CharField(max_length=64, blank=True, default="")
    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default=STATUS_OK)
    error = models.CharField(max_length=300, blank=True, default="")
    # Staff switch: an excluded source is never fetched again and never
    # reaches a prompt.
    excluded = models.BooleanField(default=False)
    # When the text last changed / when the source was last checked.
    extracted_at = models.DateTimeField(null=True, blank=True)
    checked_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["kind", "title", "id"]
        constraints = [
            models.UniqueConstraint(
                fields=["tenant", "ref"], name="knowledge_source_tenant_ref"
            ),
        ]
        indexes = [models.Index(fields=["tenant", "excluded", "status"])]

    def __str__(self) -> str:  # pragma: no cover - debug only
        return f"{self.kind}: {self.title or self.ref}"

    @property
    def is_website(self) -> bool:
        return self.kind in self.WEBSITE_KINDS


class KnowledgeRefresh(models.Model):
    """One refresh of a tenant's website or documents — what the staff card
    shows as "last refreshed" and the error, if any."""

    SCOPE_WEBSITE = "website"
    SCOPE_DOCUMENTS = "documents"

    id = models.BigAutoField(primary_key=True)
    tenant = models.ForeignKey(
        "tenants.Tenant", on_delete=models.CASCADE, related_name="knowledge_refreshes"
    )
    scope = models.CharField(max_length=12)
    started_at = models.DateTimeField(auto_now_add=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    ok = models.BooleanField(default=False)
    # {"fetched": n, "changed": n, "unchanged": n, "skipped": n, "errors": n, ...}
    stats = models.JSONField(default=dict, blank=True)
    message = models.CharField(max_length=300, blank=True, default="")

    class Meta:
        ordering = ["-started_at"]
        indexes = [models.Index(fields=["tenant", "scope", "-started_at"])]
