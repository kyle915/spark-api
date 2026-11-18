"""Tables for the shared rate limiter, the audit log and usage rollups.

All writes go through the helpers in ``limiter`` / ``audit`` / ``usage`` —
they use single-statement atomic upserts so concurrent Cloud Run instances
never lose counts and never deadlock on each other.
"""

from django.conf import settings
from django.db import models


class RateLimitCounter(models.Model):
    """Fixed-window hit counter. One row per (key, window_start).

    ``key`` is e.g. ``gql:user:42:1m`` or ``login:ip:203.0.113.9:15m``.
    Rows older than a day are pruned by the daily detection cron.
    """

    key = models.CharField(max_length=200)
    window_start = models.DateTimeField()
    count = models.PositiveIntegerField(default=0)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["key", "window_start"], name="abuse_rl_key_window_uniq"
            )
        ]
        indexes = [models.Index(fields=["window_start"], name="abuse_rl_window_idx")]

    def __str__(self) -> str:  # pragma: no cover - admin nicety
        return f"{self.key} @ {self.window_start:%Y-%m-%d %H:%M} = {self.count}"


class AuditLogEntry(models.Model):
    KIND_EXPORT = "export"
    KIND_BULK = "bulk"
    KIND_DOWNLOAD_URL = "download_url"
    KIND_AMBASSADOR_READ = "ambassador_read"
    KIND_THROTTLED = "throttled"
    KIND_LOGIN = "login"
    KIND_CHOICES = [
        (KIND_EXPORT, "Export"),
        (KIND_BULK, "Bulk action"),
        (KIND_DOWNLOAD_URL, "Signed download URL"),
        (KIND_AMBASSADOR_READ, "Ambassador read"),
        (KIND_THROTTLED, "Throttled / rejected"),
        (KIND_LOGIN, "Login throttle"),
    ]

    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="+",
        db_constraint=False,
    )
    user_email = models.CharField(max_length=254, blank=True, default="")
    is_admin = models.BooleanField(default=False)
    tenant_id = models.IntegerField(null=True, blank=True, db_index=True)
    kind = models.CharField(max_length=32, choices=KIND_CHOICES, db_index=True)
    operation = models.CharField(max_length=120, db_index=True)
    params = models.JSONField(default=dict, blank=True)
    row_count = models.IntegerField(null=True, blank=True)
    success = models.BooleanField(default=True)
    detail = models.CharField(max_length=500, blank=True, default="")
    ip = models.CharField(max_length=64, blank=True, default="")
    user_agent = models.CharField(max_length=300, blank=True, default="")

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["user", "created_at"], name="abuse_audit_user_idx"),
        ]
        verbose_name = "audit log entry"
        verbose_name_plural = "audit log"

    def __str__(self) -> str:  # pragma: no cover
        return f"{self.created_at:%Y-%m-%d %H:%M} {self.user_email} {self.operation}"


class UsageBucket(models.Model):
    """Per user, per hour, per GraphQL operation name request rollup.

    One upsert per authenticated request — no row per request.
    """

    hour = models.DateTimeField()
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="+",
        db_constraint=False,
    )
    tenant_id = models.IntegerField(null=True, blank=True)
    is_admin = models.BooleanField(default=False)
    operation = models.CharField(max_length=120)
    request_count = models.PositiveIntegerField(default=0)
    error_count = models.PositiveIntegerField(default=0)
    throttled_count = models.PositiveIntegerField(default=0)
    # Requests that passed a cursor/offset (i.e. "next page" walks).
    paged_count = models.PositiveIntegerField(default=0)
    response_bytes = models.BigIntegerField(default=0)
    # Small capped samples for the detector (distinct IPs / UAs seen).
    ips = models.JSONField(default=list, blank=True)
    user_agents = models.JSONField(default=list, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["hour", "user", "operation"], name="abuse_usage_uniq"
            )
        ]
        indexes = [models.Index(fields=["hour"], name="abuse_usage_hour_idx")]


class AmbassadorReadLog(models.Model):
    """Distinct ambassadors a (non-admin) user pulled, per UTC day.

    Insert-ignore per (day, user, ambassador_key) so the count of rows per
    user-day is "how many distinct BAs did they read" — the bulk-walk signal.
    """

    day = models.DateField()
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="+",
        db_constraint=False,
    )
    tenant_id = models.IntegerField(null=True, blank=True)
    ambassador_key = models.CharField(max_length=80)
    first_operation = models.CharField(max_length=120, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["day", "user", "ambassador_key"], name="abuse_ba_read_uniq"
            )
        ]
