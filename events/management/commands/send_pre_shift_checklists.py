"""Per-shift "pre-shift checklist" push (~2h before start).

This was ORIGINALLY scheduled per-shift at approval time via django-rq
(events/signals.py → ambassadors.push.schedule_push_at, 2h before start).
But there is NO rqscheduler running in prod, so that job never fired — the
checklist push was silently dead. This command moves it to the
cron→endpoint pattern that already works for the activation reminder /
recap nudge: a GitHub Actions cron hits `/internal/cron/pre-shift-checklists`
every ~10 min, and the push is sent INLINE in the web process (no worker).

What it does each run: find approved AmbassadorEvent rows whose event
starts in the near-future window and that have NOT already had a checklist
pushed, send "Pre-shift checklist — {venue} at {time}" to each BA inline,
then stamp `pre_shift_checklist_sent_at` so the next run skips them.

Window: `start_time` in `(now, now + lead-minutes]` (default 120). The
dedup stamp means each shift fires exactly once — the first run that sees
it inside the window, i.e. ~2h before start (bounded by the cron cadence).
`start_time > now` so a shift that already started never gets a checklist.
A shift approved when it's already <2h out simply gets its checklist on the
next run (slightly late is better than never).

Usage:
    python manage.py send_pre_shift_checklists
    python manage.py send_pre_shift_checklists --lead-minutes 120
    python manage.py send_pre_shift_checklists --dry-run
"""

from __future__ import annotations

import logging
from datetime import timedelta

from django.core.management.base import BaseCommand
from django.utils import timezone

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = (
        "Push the 'pre-shift checklist' reminder to every BA with an approved "
        "shift starting in the next N minutes (once per shift). Run every "
        "~10 min from a cron runner."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--lead-minutes",
            type=int,
            default=120,
            help="Send the checklist for shifts starting within this many "
                 "minutes from now (default 120 — the ~2h pre-shift lead).",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Log who would get a checklist, but send nothing and stamp nothing.",
        )

    def handle(self, *args, **opts):
        from ambassadors.models import AmbassadorEvent, PushDevice
        from ambassadors.push import _send_push_to_user_sync

        lead_minutes = max(1, int(opts["lead_minutes"]))
        dry = bool(opts["dry_run"])

        now = timezone.now()
        window_end = now + timedelta(minutes=lead_minutes)

        # Approved roster rows for events starting in (now, now + lead] that
        # haven't had a checklist yet. start_time > now so a shift that
        # already started doesn't get a "coming up" checklist.
        rosters = list(
            AmbassadorEvent.objects
            .select_related("event", "ambassador", "ambassador__user", "event__timezone")
            .filter(
                is_approved=True,
                event__isnull=False,
                pre_shift_checklist_sent_at__isnull=True,
                event__start_time__gt=now,
                event__start_time__lte=window_end,
            )
        )
        if not rosters:
            self.stdout.write("No upcoming shifts in window; nothing to send.")
            return

        # Reachable BAs (active push device) by user id — skip building a
        # message for someone we can't deliver to, but DON'T stamp them: if
        # they register a device before the shift starts, a later run still
        # catches them.
        device_user_ids = set(
            PushDevice.objects.filter(is_active=True).values_list(
                "user_id", flat=True
            )
        )

        sent = 0
        stamped_ids: list[int] = []
        unreachable = 0
        for r in rosters:
            user_id = getattr(r.ambassador, "user_id", None)
            if not user_id or user_id not in device_user_ids:
                unreachable += 1
                continue

            event = r.event
            body = self._compose_body(event)

            if dry:
                self.stdout.write(
                    f"[dry-run] amb={r.ambassador_id} user={user_id} "
                    f"event={r.event_id} start={getattr(event, 'start_time', None)} "
                    f":: {body}"
                )
                sent += 1
                continue

            try:
                _send_push_to_user_sync(
                    user_id,
                    title="Pre-shift checklist",
                    body=body,
                    # Reminder-only payload (no ambassadorEventUuid) so the
                    # mobile tap handler routes to the Shifts tab rather than
                    # re-opening the accept/decline offer screen. Matches the
                    # old scheduled pre-shift-checklist payload's `kind`.
                    data={
                        "kind": "pre_shift_checklist",
                        "screen": "shifts",
                        "eventUuid": str(getattr(event, "uuid", "")),
                    },
                )
                sent += 1
                stamped_ids.append(r.id)
            except Exception:
                logger.exception(
                    "pre-shift checklist push failed amb=%s event=%s",
                    r.ambassador_id, r.event_id,
                )

        # Stamp in one bulk update so a second run in the same window can't
        # double-send. Only stamp rows we actually pushed.
        if stamped_ids and not dry:
            AmbassadorEvent.objects.filter(id__in=stamped_ids).update(
                pre_shift_checklist_sent_at=now
            )

        self.stdout.write(
            f"pre-shift checklists: sent {sent}, stamped {len(stamped_ids)}, "
            f"skipped {unreachable} unreachable, across {len(rosters)} "
            f"roster row(s) in window."
        )

    @staticmethod
    def _compose_body(event) -> str:
        """"Coming up: {venue} at {time}. Open your briefing…" — reuses the
        event name as the venue label and appends the local start time when
        we can format it."""
        venue = (getattr(event, "name", None) or "your upcoming shift")[:80]
        tail = "Open your briefing and grab your uniform + materials."
        start_time = getattr(event, "start_time", None)
        if not start_time:
            return f"Coming up: {venue}. {tail}"

        # Render in the event's timezone when one is set, else UTC. The
        # TimeZone FK stores an IANA name on `.name` (e.g. "America/Los_Angeles").
        try:
            tz = getattr(event, "timezone", None)
            tzname = getattr(tz, "name", None)
            local_start = start_time
            if tzname:
                from zoneinfo import ZoneInfo

                local_start = start_time.astimezone(ZoneInfo(tzname))
            time_str = local_start.strftime("%-I:%M %p").lstrip()
            return f"Coming up: {venue} at {time_str}. {tail}"
        except Exception:
            # Never let formatting break the checklist — fall back to venue only.
            return f"Coming up: {venue}. {tail}"
