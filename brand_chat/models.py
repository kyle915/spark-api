"""One row per brand chat turn: the question, the answer, and the log.

The row is both the conversation history (what a person sees when they
come back) and the usage log (who asked, for which brand, which tools ran,
tokens and cost). When a person clears their history the text is wiped
(``question``/``answer``/``sources`` emptied, ``cleared_at`` set) and the
log fields stay, so limits and spend still add up.
"""

from __future__ import annotations

import uuid

from django.conf import settings
from django.db import models


class BrandChatTurn(models.Model):
    STATUS_ANSWERED = "answered"
    # The model's numbers did not all come from the tool results.
    STATUS_NOT_GROUNDED = "not_grounded"
    # The model could not be reached or gave nothing usable.
    STATUS_FAILED = "failed"
    # Refused before any model call: rate limit or daily spend cap.
    STATUS_LIMITED = "limited"

    id = models.BigAutoField(primary_key=True)
    uuid = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    tenant = models.ForeignKey(
        "tenants.Tenant", on_delete=models.CASCADE, related_name="brand_chat_turns"
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="brand_chat_turns"
    )
    conversation = models.UUIDField(db_index=True)
    question = models.TextField(blank=True, default="")
    answer = models.TextField(blank=True, default="")
    status = models.CharField(max_length=16, default=STATUS_ANSWERED)
    # [{tool, label, detail, link, link_label}] shown as chips under the answer.
    sources = models.JSONField(default=list, blank=True)
    # [{label, url}] store pages named in the answer.
    links = models.JSONField(default=list, blank=True)
    # Log: [{name, args, ok, rows}] for every tool the model ran.
    tools_used = models.JSONField(default=list, blank=True)
    tool_calls = models.PositiveSmallIntegerField(default=0)
    model = models.CharField(max_length=80, blank=True, default="")
    prompt_tokens = models.PositiveIntegerField(default=0)
    completion_tokens = models.PositiveIntegerField(default=0)
    # Estimated spend in millionths of a US dollar.
    cost_micro_usd = models.PositiveBigIntegerField(default=0)
    duration_ms = models.PositiveIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    cleared_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        indexes = [
            models.Index(fields=["user", "tenant", "created_at"], name="brand_chat_user_idx"),
            models.Index(fields=["tenant", "created_at"], name="brand_chat_tenant_idx"),
        ]
        ordering = ["created_at", "id"]

    def __str__(self) -> str:  # pragma: no cover - admin/debug only
        return f"brand chat turn {self.uuid} ({self.status})"


class SparkHelpArticle(models.Model):
    """Admin-authored Spark Help / SOP knowledge entry.

    Strictly admin-writeable: brand clients may NEVER create, edit, or delete
    articles, and retrieval filters strictly by ``audience`` so ``admin``
    entries are never surfaced to non-admin brand users.
    """

    AUDIENCE_ALL = "all"
    AUDIENCE_CLIENT = "client"
    AUDIENCE_ADMIN = "admin"
    AUDIENCE_CHOICES = (
        (AUDIENCE_ALL, "Clients & Admins"),
        (AUDIENCE_CLIENT, "Brand Clients"),
        (AUDIENCE_ADMIN, "Ignite Admins Only"),
    )

    SOURCE_ADMIN_FAQ = "admin_faq"
    SOURCE_CHAT_TAUGHT = "chat_taught"
    SOURCE_GAP_RESOLVED = "gap_resolved"
    SOURCE_CHOICES = (
        (SOURCE_ADMIN_FAQ, "Admin FAQ / SOP"),
        (SOURCE_CHAT_TAUGHT, "Taught from Chat"),
        (SOURCE_GAP_RESOLVED, "Resolved Knowledge Gap"),
    )

    id = models.BigAutoField(primary_key=True)
    uuid = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    title = models.CharField(max_length=180)
    slug = models.CharField(max_length=140, blank=True, default="", db_index=True)
    category = models.CharField(max_length=60, default="general", db_index=True)
    audience = models.CharField(
        max_length=16,
        choices=AUDIENCE_CHOICES,
        default=AUDIENCE_ALL,
        db_index=True,
    )
    route_path = models.CharField(max_length=120, blank=True, default="")
    route_label = models.CharField(max_length=80, blank=True, default="")
    tags = models.JSONField(default=list, blank=True)
    body = models.TextField()
    source_kind = models.CharField(
        max_length=24, choices=SOURCE_CHOICES, default=SOURCE_ADMIN_FAQ
    )
    # Null = global Spark help; non-null = optional brand-specific admin SOP.
    tenant = models.ForeignKey(
        "tenants.Tenant",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="spark_help_articles",
    )
    # Semantic OpenAI embedding vector (text-embedding-3-small) stored as floats.
    embedding = models.JSONField(default=list, blank=True)
    embedding_model = models.CharField(max_length=64, blank=True, default="")
    embedding_hash = models.CharField(max_length=64, blank=True, default="")
    is_active = models.BooleanField(default=True, db_index=True)
    usage_count = models.PositiveIntegerField(default=0)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="spark_help_articles_created",
    )
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="spark_help_articles_updated",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        indexes = [
            models.Index(
                fields=["is_active", "audience", "updated_at"],
                name="spark_help_art_aud_idx",
            ),
        ]
        ordering = ["-updated_at", "-id"]

    def __str__(self) -> str:  # pragma: no cover - admin/debug only
        return f"help article [{self.audience}] {self.title}"


class SparkHelpGap(models.Model):
    """Unanswered or unhelpful question logged for Ignite admins to resolve.

    When a client or admin asks a Spark Help question that has no confident
    knowledge hit — or clicks "Not helpful" — it lands here (deduplicated by
    ``normalized_key`` + ``caller_role``) so admins can answer once and teach
    Spark forever. Strictly admin-readable and admin-resolvable.
    """

    STATUS_OPEN = "open"
    STATUS_RESOLVED = "resolved"
    STATUS_DISMISSED = "dismissed"
    STATUS_CHOICES = (
        (STATUS_OPEN, "Open"),
        (STATUS_RESOLVED, "Resolved"),
        (STATUS_DISMISSED, "Dismissed"),
    )

    ROLE_CLIENT = "client"
    ROLE_ADMIN = "admin"

    SURFACE_ASK_SPARK = "ask_spark"
    SURFACE_CHAT_DRAFT = "chat_draft"
    SURFACE_FEEDBACK = "feedback"

    REASON_NO_MATCH = "no_match"
    REASON_LOW_CONFIDENCE = "low_confidence"
    REASON_NOT_HELPFUL = "not_helpful"
    REASON_NOT_GROUNDED = "not_grounded"

    id = models.BigAutoField(primary_key=True)
    uuid = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    question = models.TextField()
    normalized_key = models.CharField(max_length=200, db_index=True)
    route_path = models.CharField(max_length=120, blank=True, default="")
    caller_role = models.CharField(max_length=16, default=ROLE_CLIENT, db_index=True)
    source_surface = models.CharField(max_length=24, default=SURFACE_ASK_SPARK)
    reason = models.CharField(max_length=32, default=REASON_NO_MATCH)
    tenant = models.ForeignKey(
        "tenants.Tenant",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="spark_help_gaps",
    )
    asked_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="spark_help_gaps_asked",
    )
    asked_count = models.PositiveIntegerField(default=1)
    last_asked_at = models.DateTimeField(auto_now=True, db_index=True)
    status = models.CharField(
        max_length=16, choices=STATUS_CHOICES, default=STATUS_OPEN, db_index=True
    )
    resolved_article = models.ForeignKey(
        SparkHelpArticle,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="resolved_gaps",
    )
    resolved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="spark_help_gaps_resolved",
    )
    resolved_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        indexes = [
            models.Index(
                fields=["status", "asked_count", "last_asked_at"],
                name="spark_help_gap_queue_idx",
            ),
            models.Index(
                fields=["normalized_key", "caller_role", "status"],
                name="spark_help_gap_dedup_idx",
            ),
        ]
        ordering = ["-asked_count", "-last_asked_at", "-id"]

    def __str__(self) -> str:  # pragma: no cover - admin/debug only
        return f"help gap ({self.status}, {self.caller_role}) {self.question[:50]}"
