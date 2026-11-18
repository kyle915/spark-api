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
