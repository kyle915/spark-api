"""Wall-clock cron: fire the four AmbassadorJob shift reminders.

Background — the reminders (24h / 3h / 15-min-before / 15-min-after-end) were
wired via django_rq `scheduler.schedule(...)` in jobs/tasks.py, but prod Cloud
Run has no Redis and no rqscheduler, so every scheduled reminder was silently
dropped (the enqueue raised and was swallowed). This command replaces that dead
path with a periodic wall-clock scan — the same pattern used for the activation
reminder and recap nudge (see digest/cron_views.py). Meant to run every ~10 min
via a GitHub Actions cron hitting /internal/cron/ambassador-job-reminders.

Idempotency + safety:
  * Dedup is the existing per-reminder timestamp columns on AmbassadorJob
    (reminder_sent_at / reminder_3h_sent_at / reminder_15m_sent_at /
    reminder_end_15m_sent_at). We only pick rows where the column is NULL, and
    the underlying sender stamps it after a successful send — so each reminder
    goes out at most once.
  * FIRST-RUN SAFETY: the three "before" reminders select only FUTURE shifts
    (start_time > now), so a first run can never blast historical shifts. The
    "after-end" reminder is bounded to shifts that ended within the last
    ``--end-lookback-minutes`` (default 120), so it also can't fire for ancient
    shifts whose column is still NULL.
  * The senders re-validate (status still approved/accepted, still unsent) and
    send INLINE via the OneSignal client — no worker needed.

Dry-run (default OFF): ``--dry-run`` reports how many rows WOULD fire per
reminder and sends/stamps nothing.
"""
from __future__ import annotations

import datetime

from django.core.management.base import BaseCommand
from django.utils import timezone

from jobs.models import AmbassadorJob
from jobs.tasks import (
    REMINDER_ALLOWED_STATUS_SLUGS,
    send_ambassador_job_24h_reminder,
    send_ambassador_job_3h_reminder,
    send_ambassador_job_15m_reminder_push,
    send_ambassador_job_end_15m_reminder_push,
)


class Command(BaseCommand):
    help = (
        "Send due AmbassadorJob shift reminders (24h/3h/15m-before/15m-after). "
        "Dry-run by default is OFF; pass --dry-run to preview."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Report counts per reminder; send nothing, stamp nothing.",
        )
        parser.add_argument(
            "--end-lookback-minutes",
            type=int,
            default=120,
            help=(
                "Only fire the after-end reminder for shifts that ended within "
                "this many minutes (first-run + gap safety). Default 120."
            ),
        )

    def handle(self, *args, **opts):
        dry_run: bool = opts["dry_run"]
        end_lookback = max(15, int(opts["end_lookback_minutes"]))
        now = timezone.now()

        def w(msg: str) -> None:
            self.stdout.write(msg)

        w(
            f"send_ambassador_job_reminders: now={now.isoformat()} "
            f"dry_run={dry_run} end_lookback_min={end_lookback}"
        )

        base = AmbassadorJob.objects.filter(
            status__slug__in=REMINDER_ALLOWED_STATUS_SLUGS
        ).select_related("status", "ambassador__user", "job__event")

        # (label, queryset, sender). Each queryset is bounded so it can never
        # select a stale/past shift for the "before" reminders, and only a
        # recently-ended shift for the after-end one.
        specs = [
            (
                "24h",
                base.filter(
                    reminder_sent_at__isnull=True,
                    job__event__start_time__gt=now,
                    job__event__start_time__lte=now + datetime.timedelta(hours=24),
                ),
                send_ambassador_job_24h_reminder,
            ),
            (
                "3h",
                base.filter(
                    reminder_3h_sent_at__isnull=True,
                    job__event__start_time__gt=now,
                    job__event__start_time__lte=now + datetime.timedelta(hours=3),
                ),
                send_ambassador_job_3h_reminder,
            ),
            (
                "15m-before",
                base.filter(
                    reminder_15m_sent_at__isnull=True,
                    job__event__start_time__gt=now,
                    job__event__start_time__lte=now + datetime.timedelta(minutes=15),
                ),
                send_ambassador_job_15m_reminder_push,
            ),
            (
                "15m-after-end",
                base.filter(
                    reminder_end_15m_sent_at__isnull=True,
                    job__event__end_time__lte=now - datetime.timedelta(minutes=15),
                    job__event__end_time__gte=now
                    - datetime.timedelta(minutes=15 + end_lookback),
                ),
                send_ambassador_job_end_15m_reminder_push,
            ),
        ]

        total_sent = 0
        for label, qs, sender in specs:
            ids = list(qs.values_list("id", flat=True))
            if dry_run:
                w(f"  [{label}] would fire: {len(ids)} (ids={ids[:20]}{'…' if len(ids) > 20 else ''})")
                continue
            sent = 0
            for aj_id in ids:
                try:
                    # Sender re-validates (unsent + status) and stamps on send;
                    # returns 1 when it actually pushed. expected_trigger_at_iso
                    # is omitted — our window select + the sender's own dedup
                    # are the correctness guards.
                    sent += int(sender(aj_id) or 0)
                except Exception:  # noqa: BLE001 — one bad row shouldn't stop the sweep
                    self.stderr.write(
                        f"  [{label}] sender raised for ambassador_job={aj_id}"
                    )
            total_sent += sent
            w(f"  [{label}] candidates={len(ids)} sent={sent}")

        w(f"done. total_sent={total_sent}")
