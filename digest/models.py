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


class CronRun(models.Model):
    """Heartbeat for each internal cron endpoint — one row per cron name.

    Every hit of `/internal/cron/<name>` stamps this (via the wrapper in
    digest.urls), so the System Health page can show whether each
    automation actually fired and when — the thing that was invisible when
    the RQ scheduler silently died for weeks. `last_ok` is the HTTP result
    (2xx), `stale` (a property) is computed by readers against expected
    cadence.
    """

    id = models.BigAutoField(primary_key=True)
    name = models.CharField(max_length=100, unique=True, db_index=True)
    last_run_at = models.DateTimeField(null=True, blank=True)
    last_status = models.PositiveSmallIntegerField(default=0)
    last_ok = models.BooleanField(default=False)
    last_detail = models.TextField(blank=True, default="")
    run_count = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ["name"]

    def __str__(self) -> str:  # pragma: no cover
        return f"{self.name} @ {self.last_run_at} ok={self.last_ok}"


def record_cron_run(name: str, *, status: int, detail: str = "") -> None:
    """Best-effort heartbeat stamp for one cron hit. Never raises."""
    try:
        from django.db.models import F
        from django.utils import timezone

        row, created = CronRun.objects.get_or_create(
            name=name[:100],
            defaults={
                "last_run_at": timezone.now(),
                "last_status": status,
                "last_ok": 200 <= status < 400,
                "last_detail": (detail or "")[:2000],
                "run_count": 1,
            },
        )
        if not created:
            CronRun.objects.filter(pk=row.pk).update(
                last_run_at=timezone.now(),
                last_status=status,
                last_ok=200 <= status < 400,
                last_detail=(detail or "")[:2000],
                run_count=F("run_count") + 1,
            )
    except Exception:  # noqa: BLE001 — heartbeat must never break a cron
        pass
