from uuid6 import uuid7

from django.db import models
from django.conf import settings

from ambassadors.models import Ambassador


class AmbassadorAvailability(models.Model):
    """One availability window for a BA.

    Two shapes are supported by the same table:
      * Recurring weekly slot   -> is_recurring=True,  weekday set, date NULL
      * One-off date override   -> is_recurring=False, date set,    weekday NULL

    The mobile weekly editor (#193) writes recurring weekday rows only;
    `setAvailability` replaces the BA's full recurring set atomically.
    The nullable `date` + `is_recurring` flag are here from day one so a
    one-off-date editor can be added later with no migration.

    Times are naive local wall-clock TimeFields (no tz) — same modeling
    as Event.start_time/end_time, which the digest matcher compares
    against. The daily new-gig digest matcher
    (jobs/management/commands/send_new_gig_digest.py) will later call
    `covers()` to gate matches by availability.
    """

    WEEKDAYS = [
        (0, "Monday"),
        (1, "Tuesday"),
        (2, "Wednesday"),
        (3, "Thursday"),
        (4, "Friday"),
        (5, "Saturday"),
        (6, "Sunday"),
    ]

    id = models.BigAutoField(primary_key=True)
    uuid = models.UUIDField(default=uuid7, unique=True, editable=False)

    ambassador = models.ForeignKey(
        Ambassador,
        on_delete=models.CASCADE,
        null=False,
        related_name="availability",
    )

    # Recurring weekly slot: 0=Mon .. 6=Sun. NULL for a one-off date row.
    weekday = models.IntegerField(choices=WEEKDAYS, null=True, blank=True)
    # One-off override for a specific calendar date. NULL for recurring.
    date = models.DateField(null=True, blank=True)
    is_recurring = models.BooleanField(default=True)

    start_time = models.TimeField()
    end_time = models.TimeField()

    note = models.CharField(max_length=255, null=True, blank=True)

    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        related_name="availability_created_by",
    )
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        related_name="availability_updated_by",
    )

    created_at = models.DateTimeField(auto_now_add=True, editable=False)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["weekday", "start_time"]
        indexes = [
            models.Index(fields=["ambassador", "is_recurring"]),
            models.Index(fields=["ambassador", "weekday"]),
            models.Index(fields=["ambassador", "date"]),
        ]

    def __str__(self) -> str:
        when = (
            self.get_weekday_display()
            if self.weekday is not None
            else (self.date.isoformat() if self.date else "?")
        )
        return f"avail ba={self.ambassador_id} {when} {self.start_time}-{self.end_time}"

    def covers(self, on_date, start, end) -> bool:
        """True if this window covers [start, end] on `on_date`.

        Used by the future digest/scheduling matcher. A recurring row
        matches when its weekday == on_date.weekday(); a one-off row
        matches when its date == on_date. The slot must fully contain
        the requested [start, end] window.
        """
        if self.is_recurring:
            if self.weekday is None or self.weekday != on_date.weekday():
                return False
        else:
            if self.date is None or self.date != on_date:
                return False
        return self.start_time <= start and self.end_time >= end
