"""Models for Ignite staffing overflow handoffs and no-show backup cascades.

Lives in its own ``staffing`` app so migrations stay independent from
``events``, ``pilot``, and ``store_logistics``.
"""

from __future__ import annotations

from django.conf import settings
from django.db import models
from uuid6 import uuid7


class IgniteStaffingRequest(models.Model):
    """A brand workspace's request for Ignite ops to staff an unstaffed wave
    or upcoming shifts. Pure operational handoff — no client billing/checkout."""

    STATUS_PENDING = "pending"
    STATUS_ACCEPTED = "accepted"
    STATUS_ASSIGNED = "assigned"
    STATUS_COMPLETED = "completed"
    STATUS_DECLINED = "declined"
    STATUS_CANCELLED = "cancelled"
    STATUS_CHOICES = [
        (STATUS_PENDING, "Pending Review"),
        (STATUS_ACCEPTED, "Accepted by Ignite"),
        (STATUS_ASSIGNED, "BAs Assigned"),
        (STATUS_COMPLETED, "Completed"),
        (STATUS_DECLINED, "Declined"),
        (STATUS_CANCELLED, "Cancelled"),
    ]
    STAFF_MANAGEABLE_STATUSES = frozenset(
        {
            STATUS_PENDING,
            STATUS_ACCEPTED,
            STATUS_ASSIGNED,
            STATUS_COMPLETED,
            STATUS_DECLINED,
            STATUS_CANCELLED,
        }
    )

    SOURCE_WAVE_SHEET = "wave_sheet"
    SOURCE_UPCOMING_SHIFT = "upcoming_shift"
    SOURCE_STAFFING_BOARD = "staffing_board"
    SOURCE_EVENT_DETAIL = "event_detail"
    SOURCE_CHOICES = [
        (SOURCE_WAVE_SHEET, "Account Map Wave Sheet"),
        (SOURCE_UPCOMING_SHIFT, "Upcoming Shift"),
        (SOURCE_STAFFING_BOARD, "Staffing Board"),
        (SOURCE_EVENT_DETAIL, "Event Detail"),
    ]

    id = models.BigAutoField(primary_key=True)
    uuid = models.UUIDField(default=uuid7, unique=True, editable=False)

    tenant = models.ForeignKey(
        "tenants.Tenant",
        on_delete=models.CASCADE,
        related_name="ignite_staffing_requests",
    )
    requested_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="ignite_staffing_requests_created",
    )
    assigned_ops_owner = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="ignite_staffing_requests_owned",
    )

    status = models.CharField(
        max_length=24,
        choices=STATUS_CHOICES,
        default=STATUS_PENDING,
        db_index=True,
    )
    source = models.CharField(
        max_length=32,
        choices=SOURCE_CHOICES,
        default=SOURCE_WAVE_SHEET,
    )
    title = models.CharField(max_length=255, blank=True, default="")
    activation_type = models.CharField(max_length=120, blank=True, default="")
    requested_shift_count = models.PositiveIntegerField(default=1)
    requested_ba_count = models.PositiveIntegerField(default=1)
    requested_dates = models.JSONField(default=list, blank=True)
    requested_stores = models.JSONField(default=list, blank=True)
    event_ids = models.JSONField(default=list, blank=True)
    notes = models.TextField(blank=True, default="")
    ops_notes = models.TextField(blank=True, default="")

    accepted_at = models.DateTimeField(null=True, blank=True)
    assigned_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True, editable=False)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at", "-id"]
        indexes = [
            models.Index(fields=["tenant", "status", "-created_at"]),
            models.Index(fields=["status", "-created_at"]),
        ]

    def __str__(self) -> str:
        return (
            f"IgniteStaffingRequest({self.uuid} tenant={self.tenant_id} "
            f"status={self.status} shifts={self.requested_shift_count})"
        )


class NoShowCascadeAlert(models.Model):
    """Audit + live status record for a shift confirmation or missed check-in
    backup cascade."""

    REASON_UNCONFIRMED_NEAR_START = "unconfirmed_near_start"
    REASON_MISSING_CHECKIN_15M = "missing_checkin_15m"
    REASON_MANUAL = "manual"
    REASON_CHOICES = [
        (REASON_UNCONFIRMED_NEAR_START, "Unconfirmed near shift start"),
        (REASON_MISSING_CHECKIN_15M, "Missing check-in 15+ min past start"),
        (REASON_MANUAL, "Manual backup trigger"),
    ]

    STATUS_OPEN = "open"
    STATUS_DISPATCHED = "dispatched"
    STATUS_COVERED = "covered"
    STATUS_RESOLVED = "resolved"
    STATUS_DISMISSED = "dismissed"
    STATUS_CHOICES = [
        (STATUS_OPEN, "Open"),
        (STATUS_DISPATCHED, "Backup Alerts Dispatched"),
        (STATUS_COVERED, "Backup Covered"),
        (STATUS_RESOLVED, "Resolved"),
        (STATUS_DISMISSED, "Dismissed"),
    ]
    ACTIVE_STATUSES = frozenset({STATUS_OPEN, STATUS_DISPATCHED})

    id = models.BigAutoField(primary_key=True)
    uuid = models.UUIDField(default=uuid7, unique=True, editable=False)

    tenant = models.ForeignKey(
        "tenants.Tenant",
        on_delete=models.CASCADE,
        related_name="noshow_cascade_alerts",
    )
    event = models.ForeignKey(
        "events.Event",
        on_delete=models.CASCADE,
        related_name="noshow_cascade_alerts",
    )
    ambassador_event = models.ForeignKey(
        "ambassadors.AmbassadorEvent",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="noshow_cascade_alerts",
    )
    missing_ambassador = models.ForeignKey(
        "ambassadors.Ambassador",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="noshow_cascade_alerts_missed",
    )
    triggered_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="noshow_cascades_triggered",
    )

    trigger_reason = models.CharField(
        max_length=32,
        choices=REASON_CHOICES,
        default=REASON_MISSING_CHECKIN_15M,
        db_index=True,
    )
    status = models.CharField(
        max_length=24,
        choices=STATUS_CHOICES,
        default=STATUS_OPEN,
        db_index=True,
    )
    minutes_past_start = models.IntegerField(null=True, blank=True)
    notified_backup_bas = models.JSONField(default=list, blank=True)
    delivery_audit = models.JSONField(default=list, blank=True)
    ops_notified_emails = models.JSONField(default=list, blank=True)
    notes = models.TextField(blank=True, default="")

    dispatched_at = models.DateTimeField(null=True, blank=True)
    resolved_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True, editable=False)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at", "-id"]
        indexes = [
            models.Index(fields=["tenant", "status", "-created_at"]),
            models.Index(fields=["event", "trigger_reason", "status"]),
        ]

    def __str__(self) -> str:
        return (
            f"NoShowCascadeAlert({self.uuid} event={self.event_id} "
            f"reason={self.trigger_reason} status={self.status})"
        )
