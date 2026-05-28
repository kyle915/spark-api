"""
Wingspan is an API-integration app (no payments are stored locally —
they're fetched live from Wingspan). The only thing we persist is a tiny
dedup ledger so the daily "payment sent" push never double-notifies a BA
about the same disbursement.
"""

from __future__ import annotations

from django.conf import settings
from django.db import models


class NotifiedWingspanPayment(models.Model):
    """One row per Wingspan payment we've already pushed a "you've been
    paid" notification for. The daily poll skips any payment id already
    here, so re-runs (and the daily cadence) are idempotent."""

    payment_id = models.CharField(max_length=255, unique=True)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )
    amount = models.DecimalField(
        max_digits=10, decimal_places=2, null=True, blank=True,
    )
    notified_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("-notified_at",)

    def __str__(self) -> str:
        return f"NotifiedWingspanPayment({self.payment_id})"
