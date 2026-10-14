"""Generic public clock in/out links.

A ``ClockLink`` is a shareable ``/clock/<token>`` page that any worker can
open on their phone: name, email, market, and work address, then Clock in /
Clock out with one GPS fix at each moment. It is deliberately separate from
the BA check-in flow (no Ambassador accounts, no events, no recaps) so it can
be handed to a client's own staff.

The DB is the source of truth. Each session is mirrored to one row in the
link's Google Sheet, keyed by the session UUID, and the mirror never blocks a
clock action (see ``timeclock.sheets``).
"""

from __future__ import annotations

import secrets

from django.conf import settings
from django.db import models
from django.db.models import Q
from uuid6 import uuid7

from tenants.models import Tenant

DEFAULT_TIMEZONE = "America/Los_Angeles"


def new_clock_token() -> str:
    return secrets.token_urlsafe(12)


class ClockLink(models.Model):
    id = models.BigAutoField(primary_key=True)
    uuid = models.UUIDField(default=uuid7, unique=True, editable=False)
    token = models.CharField(max_length=32, unique=True, default=new_clock_token)
    label = models.CharField(max_length=120)
    # Optional: brands the page (logo + accent) and groups links per client.
    tenant = models.ForeignKey(
        Tenant,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="clock_links",
    )
    sheet_id = models.CharField(max_length=128, blank=True, default="")
    # Used to render times when the worker's phone doesn't report a zone.
    timezone = models.CharField(max_length=64, default=DEFAULT_TIMEZONE)
    is_active = models.BooleanField(default=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )
    created_at = models.DateTimeField(auto_now_add=True, editable=False)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return self.label

    @property
    def sheet_url(self) -> str:
        if not self.sheet_id:
            return ""
        return f"https://docs.google.com/spreadsheets/d/{self.sheet_id}/edit"


class ClockSession(models.Model):
    GPS_OK = "ok"
    GPS_DENIED = "denied"
    GPS_UNAVAILABLE = "unavailable"
    GPS_TIMEOUT = "timeout"
    GPS_UNSUPPORTED = "unsupported"
    GPS_STATUSES = (GPS_OK, GPS_DENIED, GPS_UNAVAILABLE, GPS_TIMEOUT, GPS_UNSUPPORTED)

    id = models.BigAutoField(primary_key=True)
    uuid = models.UUIDField(default=uuid7, unique=True, editable=False)
    link = models.ForeignKey(
        ClockLink, on_delete=models.CASCADE, related_name="sessions"
    )
    first_name = models.CharField(max_length=80)
    last_name = models.CharField(max_length=80)
    email = models.EmailField(max_length=254)
    market = models.CharField(max_length=120)
    address = models.CharField(max_length=300)
    # IANA zone from the worker's phone (falls back to the link's zone).
    timezone = models.CharField(max_length=64, default=DEFAULT_TIMEZONE)

    clock_in_at = models.DateTimeField()
    clock_in_lat = models.FloatField(null=True, blank=True)
    clock_in_lng = models.FloatField(null=True, blank=True)
    clock_in_accuracy_m = models.FloatField(null=True, blank=True)
    clock_in_gps = models.CharField(max_length=16, default=GPS_OK)

    clock_out_at = models.DateTimeField(null=True, blank=True)
    clock_out_lat = models.FloatField(null=True, blank=True)
    clock_out_lng = models.FloatField(null=True, blank=True)
    clock_out_accuracy_m = models.FloatField(null=True, blank=True)
    clock_out_gps = models.CharField(max_length=16, blank=True, default="")

    ip_address = models.CharField(max_length=64, blank=True, default="")
    user_agent = models.CharField(max_length=300, blank=True, default="")

    # Sheet mirror bookkeeping. ``sheet_row`` is a hint only — every update
    # re-checks the Session ID cell before writing, since people sort sheets.
    sheet_dirty = models.BooleanField(default=True)
    sheet_row = models.PositiveIntegerField(null=True, blank=True)
    sheet_synced_at = models.DateTimeField(null=True, blank=True)
    sheet_error = models.CharField(max_length=500, blank=True, default="")

    created_at = models.DateTimeField(auto_now_add=True, editable=False)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-clock_in_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["link", "email"],
                condition=Q(clock_out_at__isnull=True),
                name="timeclock_one_open_session_per_email",
            )
        ]
        indexes = [
            models.Index(fields=["link", "-clock_in_at"]),
            models.Index(
                fields=["sheet_dirty"],
                condition=Q(sheet_dirty=True),
                name="timeclock_sheet_dirty_idx",
            ),
        ]

    @property
    def full_name(self) -> str:
        return f"{self.first_name} {self.last_name}".strip()

    @property
    def is_open(self) -> bool:
        return self.clock_out_at is None

    @property
    def duration_minutes(self) -> int | None:
        if self.clock_out_at is None:
            return None
        seconds = (self.clock_out_at - self.clock_in_at).total_seconds()
        return max(0, int(seconds // 60))
