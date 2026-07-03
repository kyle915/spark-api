from django.db import models


class BackendErrorEvent(models.Model):
    """One row per distinct backend error signature, with occurrence counts.

    Fed by utils.error_monitor (a logging handler at ERROR level + the
    GraphQL process_errors hook), which also sends a throttled alert email
    per signature. This table is the durable record — the difference
    between "a BA screenshotted it hours later" and "we knew at the first
    occurrence" (the Feel Free recap-submit outage was invisible
    server-side until a human reported it).
    """

    id = models.BigAutoField(primary_key=True)
    # e.g. "ValueError:recaps.mutations:create_recap" — type + logger + func.
    signature = models.CharField(max_length=255, unique=True, db_index=True)
    message = models.TextField(blank=True, default="")
    # pathname:lineno of the log site (not the raise site — the traceback
    # carries that).
    location = models.CharField(max_length=255, blank=True, default="")
    traceback = models.TextField(blank=True, default="")
    count = models.PositiveIntegerField(default=1)
    first_seen = models.DateTimeField(auto_now_add=True)
    last_seen = models.DateTimeField(auto_now=True)
    # Alert throttle: at most one email per signature per hour.
    last_alerted_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-last_seen"]

    def __str__(self) -> str:  # pragma: no cover
        return f"{self.signature} ×{self.count}"
