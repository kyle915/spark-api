"""A staff member's "OK — reviewed" on one hours-check flag.

The checker itself stores nothing: flags are recomputed from the clock data
every time the payroll page loads. This table only remembers that a person
looked at a flag, so it stops asking. ``fingerprint`` is the rule's short
summary of the facts it judged (e.g. ``worked=9.50;booked=6.00;ext=0``); when
a punch edit changes those facts the fingerprint no longer matches and the
flag asks again.
"""

from __future__ import annotations

from django.conf import settings
from django.db import models
from uuid6 import uuid7


class HoursFlagReview(models.Model):
    id = models.BigAutoField(primary_key=True)
    uuid = models.UUIDField(default=uuid7, unique=True, editable=False)

    tenant = models.ForeignKey(
        "tenants.Tenant", on_delete=models.CASCADE, related_name="hours_flag_reviews"
    )
    event = models.ForeignKey(
        "events.Event", on_delete=models.CASCADE, related_name="hours_flag_reviews"
    )
    ambassador = models.ForeignKey(
        "ambassadors.Ambassador",
        on_delete=models.CASCADE,
        related_name="hours_flag_reviews",
    )
    # Rule code from hours_check.rules (e.g. "over_booked").
    code = models.CharField(max_length=40)
    fingerprint = models.CharField(max_length=255, blank=True, default="")
    note = models.TextField(blank=True, default="")

    reviewed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="hours_flag_reviews",
    )
    reviewed_at = models.DateTimeField(auto_now=True)
    created_at = models.DateTimeField(auto_now_add=True, editable=False)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["event", "ambassador", "code"],
                name="uniq_hours_flag_review_per_shift_rule",
            )
        ]
        indexes = [models.Index(fields=["tenant", "event"])]

    def __str__(self) -> str:
        return f"HoursFlagReview({self.code} ev={self.event_id} ba={self.ambassador_id})"
