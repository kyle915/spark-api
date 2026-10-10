"""Public BA job board: one shareable link per brand, open shifts per gig.

A gig is an Event that came from a field marketing plan. BAs pick gigs on
the public page and book themselves; each booking is a normal approved
AmbassadorEvent + AmbassadorJob, so the shift shows up everywhere else in
Spark. ``JobBoardBooking`` keeps the contact details the BA typed and links
the records it created, so an admin cancel can undo exactly those.
"""

from __future__ import annotations

import secrets

from django.conf import settings
from django.db import models
from django.db.models import Q
from uuid6 import uuid7

from tenants.models import Tenant


def new_board_token() -> str:
    return secrets.token_urlsafe(12)


class JobBoard(models.Model):
    """The brand's shareable board. The token is the link; it never changes."""

    id = models.BigAutoField(primary_key=True)
    tenant = models.OneToOneField(
        Tenant, on_delete=models.CASCADE, related_name="job_board"
    )
    token = models.CharField(max_length=32, unique=True, default=new_board_token)
    created_at = models.DateTimeField(auto_now_add=True, editable=False)
    updated_at = models.DateTimeField(auto_now=True)


class JobBoardGig(models.Model):
    """Admin overrides for one gig. No row means the plan's defaults."""

    id = models.BigAutoField(primary_key=True)
    tenant = models.ForeignKey(
        Tenant, on_delete=models.CASCADE, related_name="job_board_gigs"
    )
    event = models.OneToOneField(
        "events.Event", on_delete=models.CASCADE, related_name="job_board_gig"
    )
    # Total BA shifts on the gig. Everyone approved on the event counts
    # against it, whether they booked here or an admin assigned them.
    open_shifts = models.PositiveSmallIntegerField()
    listed = models.BooleanField(default=True)
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )
    created_at = models.DateTimeField(auto_now_add=True, editable=False)
    updated_at = models.DateTimeField(auto_now=True)


class JobBoardBooking(models.Model):
    id = models.BigAutoField(primary_key=True)
    uuid = models.UUIDField(default=uuid7, unique=True, editable=False)
    tenant = models.ForeignKey(
        Tenant, on_delete=models.CASCADE, related_name="job_board_bookings"
    )
    event = models.ForeignKey(
        "events.Event", on_delete=models.CASCADE, related_name="job_board_bookings"
    )
    ambassador = models.ForeignKey(
        "ambassadors.Ambassador",
        on_delete=models.SET_NULL,
        null=True,
        related_name="job_board_bookings",
    )
    ambassador_event = models.ForeignKey(
        "ambassadors.AmbassadorEvent",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )
    ambassador_job = models.ForeignKey(
        "jobs.AmbassadorJob",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )
    confirmation = models.ForeignKey(
        "events.EventConfirmation",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )
    name = models.CharField(max_length=120)
    # Ten US digits, no punctuation.
    phone = models.CharField(max_length=20)
    email = models.EmailField(max_length=254)
    note = models.TextField(blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True, editable=False)
    cancelled_at = models.DateTimeField(null=True, blank=True)
    cancelled_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )

    class Meta:
        ordering = ("created_at", "id")
        indexes = [
            models.Index(fields=["phone"], name="jobboard_booking_phone_idx"),
            models.Index(fields=["email"], name="jobboard_booking_email_idx"),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["event", "phone"],
                condition=Q(cancelled_at__isnull=True),
                name="jobboard_one_live_booking_per_phone",
            ),
            models.UniqueConstraint(
                fields=["event", "email"],
                condition=Q(cancelled_at__isnull=True),
                name="jobboard_one_live_booking_per_email",
            ),
        ]
