"""Where a client recap deck is in its life: being built, a staff draft, or
ready for the brand.

One row per :class:`recaps.models.ClientRecapDraft`. The draft keeps the
words, numbers and photos; this row keeps what the deck adds on top: its
status, the next steps, the quotes staff took out and the no-product visits
staff put back. It lives in its own app so its migration never races the
``recaps`` migrations other work is adding.

A brand sees a deck only once staff mark it ``ready``. Nothing here sends an
email or shares a link: the brand finds it under their reports.
"""

from __future__ import annotations

from django.conf import settings
from django.db import models


class ClientRecapDeck(models.Model):
    STATUS_BUILDING = "building"
    STATUS_DRAFT = "draft"
    STATUS_READY = "ready"
    STATUSES = (STATUS_BUILDING, STATUS_DRAFT, STATUS_READY)

    id = models.BigAutoField(primary_key=True)
    draft = models.OneToOneField(
        "recaps.ClientRecapDraft", on_delete=models.CASCADE, related_name="deck"
    )
    status = models.CharField(max_length=10, default=STATUS_DRAFT, db_index=True)
    next_steps = models.JSONField(blank=True, default=list)
    # Quote texts staff took out of the deck.
    hidden_quotes = models.JSONField(blank=True, default=list)
    # No-product visit keys ("C123" / "L45") staff counted back in.
    kept_visits = models.JSONField(blank=True, default=list)
    build_attempts = models.PositiveSmallIntegerField(default=0)
    build_started_at = models.DateTimeField(null=True, blank=True)
    build_note = models.CharField(max_length=300, blank=True, default="")
    ready_at = models.DateTimeField(null=True, blank=True)
    ready_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="client_recap_decks_marked_ready",
    )
    created_at = models.DateTimeField(auto_now_add=True, editable=False)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-id"]

    def __str__(self) -> str:  # pragma: no cover - admin/debug only
        return f"Client recap deck {self.draft_id} ({self.status})"
