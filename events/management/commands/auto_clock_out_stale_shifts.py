"""
Auto clock-out for shifts a BA forgot to close.

The problem: a BA clocks in but never clocks out (app killed, phone died,
just forgot). The shift then reads as "still on the clock" forever — the
admin attendance view shows a shift that never ended, and any hours/pay
math that keys off the last clock-out is either missing or wildly inflated
(first clock-in → "now", days later).

This command closes those out safely, once per run:

  For every approved AmbassadorEvent whose event ENDED more than
  `--grace-minutes` ago (but within `--lookback-hours`, so we never rescan
  ancient history), if the BA's latest attendance event for that shift is a
  clock-in with no matching clock-out, insert a clock-out Attendance row
  stamped at the event's SCHEDULED end time — not "now" — so the recorded
  hours reflect the shift that was booked, not the hours-late cron run.

Idempotent by construction: once the clock-out row exists, the BA's latest
attendance event is a clock-out, so a later run won't touch them again — no
stamp field needed. Best-effort push tells the BA we closed it for them.

Only rows with a known `event.end_time` are eligible: without a scheduled
end there's no honest close time, so those are left for a human.

Run hourly via `/internal/cron/auto-clock-out` (GHA cron). The grace +
lookback windows are wider than the cadence so nothing slips between runs.

Usage:
    python manage.py auto_clock_out_stale_shifts
    python manage.py auto_clock_out_stale_shifts --grace-minutes 120 --lookback-hours 24
    python manage.py auto_clock_out_stale_shifts --dry-run
"""

from __future__ import annotations

import logging
from datetime import timedelta

from django.core.management.base import BaseCommand
from django.utils import timezone

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = (
        "Insert a scheduled-end clock-out for approved shifts whose BA "
        "clocked in but never clocked out, once the event ended more than "
        "--grace-minutes ago. Run hourly from a cron runner."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--grace-minutes",
            type=int,
            default=120,
            help="Only auto-close shifts whose end_time is at least this many "
                 "minutes in the past (default 120 — gives a BA running long "
                 "two hours to clock out themselves first).",
        )
        parser.add_argument(
            "--lookback-hours",
            type=int,
            default=24,
            help="Ignore shifts that ended more than this many hours ago "
                 "(default 24) so we never rescan old history.",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Log what would be closed, but write nothing.",
        )

    def handle(self, *args, **opts):
        from ambassadors.models import AmbassadorEvent, Attendance
        from ambassadors.mutations import _ensure_source

        grace_minutes = max(0, int(opts["grace_minutes"]))
        lookback_hours = max(1, int(opts["lookback_hours"]))
        dry = bool(opts["dry_run"])

        now = timezone.now()
        end_ceiling = now - timedelta(minutes=grace_minutes)
        end_floor = now - timedelta(hours=lookback_hours)

        # Approved rows whose event ended in the [floor, ceiling] window.
        rosters = list(
            AmbassadorEvent.objects
            .select_related("event", "event__retailer", "ambassador", "ambassador__user")
            .filter(
                is_approved=True,
                event__isnull=False,
                ambassador__isnull=False,
                event__end_time__isnull=False,
                event__end_time__lte=end_ceiling,
                event__end_time__gte=end_floor,
            )
        )
        if not rosters:
            self.stdout.write("auto clock-out: no shifts in the window.")
            return

        pairs = {(r.ambassador_id, r.event_id) for r in rosters}
        event_ids = {r.event_id for r in rosters}

        # Latest clock event per (ambassador, event). Pull only clock_in /
        # clock_out rows (ignore "arrived" / GPS pings) ordered by time so the
        # last one per pair wins.
        clock_rows = (
            Attendance.objects
            .filter(
                event_id__in=event_ids,
                source__name__in=("clock_in", "clock_out"),
            )
            .select_related("source")
            .order_by("clock_time", "id")
            .values("ambassador_id", "event_id", "source__name")
        )
        latest_kind: dict[tuple[int, int], str] = {}
        for row in clock_rows:
            key = (row["ambassador_id"], row["event_id"])
            if key in pairs:
                latest_kind[key] = row["source__name"]

        # "Still on the clock" = latest attendance event is a clock-in.
        stale = [
            r for r in rosters
            if latest_kind.get((r.ambassador_id, r.event_id)) == "clock_in"
        ]
        if not stale:
            self.stdout.write(
                f"auto clock-out: {len(rosters)} ended shift(s) in window, "
                "none left clocked-in."
            )
            return

        source = None if dry else _ensure_source("clock_out")
        closed = 0
        for r in stale:
            ev = r.event
            close_at = ev.end_time  # honest: the scheduled end, not "now"
            venue = (getattr(ev, "name", None) or "your shift")[:80]
            if dry:
                self.stdout.write(
                    f"[dry-run] close amb={r.ambassador_id} event={r.event_id} "
                    f"'{venue}' at {close_at.isoformat()}"
                )
                closed += 1
                continue
            try:
                Attendance.objects.create(
                    clock_time=close_at,
                    coordinates=None,
                    ambassador=r.ambassador,
                    job=None,
                    event=ev,
                    source=source,
                )
                closed += 1
            except Exception:
                logger.exception(
                    "auto clock-out failed amb=%s event=%s",
                    r.ambassador_id, r.event_id,
                )
                continue

            # Best-effort courtesy push. Never blocks the close.
            try:
                from ambassadors.push import _send_push_to_user_sync

                user_id = getattr(r.ambassador, "user_id", None)
                if user_id:
                    _send_push_to_user_sync(
                        user_id,
                        title="We clocked you out",
                        body=(
                            f"You didn't clock out of {venue}, so we closed it "
                            "at its scheduled end time. Tell your coordinator "
                            "if you worked later."
                        ),
                        data={"kind": "auto_clock_out", "screen": "shifts",
                              "eventUuid": str(getattr(ev, "uuid", ""))},
                    )
            except Exception:
                logger.exception(
                    "auto clock-out push failed amb=%s event=%s",
                    r.ambassador_id, r.event_id,
                )

        self.stdout.write(
            f"auto clock-out: closed {closed} of {len(stale)} stale shift(s) "
            f"(from {len(rosters)} ended in the {lookback_hours}h window)."
        )
