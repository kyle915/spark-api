"""
Timely, once-per-shift "don't forget your recap" nudge.

This was ORIGINALLY scheduled per-shift at AmbassadorEvent-creation time
via django-rq (events/signals.py → ambassadors.push.schedule_recap_nudge_at,
event end + 4h, re-checking recap state at fire time). But there is NO
rqscheduler running in prod, so it never fired. This command moves it to
the cron→endpoint pattern that already works: an hourly GitHub Actions
cron hits `/internal/cron/recap-nudges`, and the push is sent INLINE in
the web process (no worker).

Relationship to the DAILY recap reminder (recaps/.../send_recap_reminders.py
→ /internal/cron/send-recap-reminders): that is the escalating daily HAMMER
that re-nags BAs with outstanding recaps for up to a week. THIS is the
single, timely per-shift nudge that lands a few hours after the shift ends
(roadmap item #20). They complement rather than double-ping: this fires at
most ONCE per shift (guaranteed by the `recap_nudge_sent_at` stamp), so
even on a day they overlap a BA gets one timely nudge here plus the daily
sweep — by design, not a bug.

What it does each run: find approved AmbassadorEvent rows whose event ENDED
a short while ago (default 1-24h), whose event has NO recap at all (no
legacy Recap rows AND no CustomRecap rows), and that have NOT been nudged,
push "don't forget to file your recap for {venue}" inline once, then stamp
`recap_nudge_sent_at`.

Window: `end_time` in `[now - max-age-hours, now - grace-hours]` (default
[now-24h, now-1h]). The grace lower-bound keeps us from pinging someone who
just walked out; the 24h upper bound hands long-overdue shifts off to the
daily sweep.

Usage:
    python manage.py send_recap_nudges
    python manage.py send_recap_nudges --grace-hours 1 --max-age-hours 24
    python manage.py send_recap_nudges --dry-run
"""

from __future__ import annotations

import logging
from datetime import timedelta

from django.core.management.base import BaseCommand
from django.utils import timezone

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = (
        "Push a single, timely 'don't forget your recap' nudge to every BA "
        "whose approved shift ended a few hours ago with no recap filed "
        "(once per shift). Run hourly from a cron runner."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--grace-hours",
            type=int,
            default=1,
            help="Don't nudge until this many hours after the shift ends "
                 "(default 1 — gives BAs who file from the parking lot a beat).",
        )
        parser.add_argument(
            "--max-age-hours",
            type=int,
            default=24,
            help="Stop sending the timely nudge once a shift is older than "
                 "this (default 24 — older shifts are the daily sweep's job).",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Log who would be nudged, but send nothing and stamp nothing.",
        )

    def handle(self, *args, **opts):
        from ambassadors.models import AmbassadorEvent, PushDevice
        from ambassadors.push import _send_push_to_user_sync

        grace_hours = max(0, int(opts["grace_hours"]))
        max_age_hours = max(grace_hours + 1, int(opts["max_age_hours"]))
        dry = bool(opts["dry_run"])

        now = timezone.now()
        window_start = now - timedelta(hours=max_age_hours)
        nudge_before = now - timedelta(hours=grace_hours)

        # Approved roster rows for events that ended in [window_start,
        # nudge_before] and haven't been nudged yet. We key off end_time
        # specifically (not the new_end_time/date fallback the daily sweep
        # uses) because this nudge is anchored to the shift actually ending.
        rosters = list(
            AmbassadorEvent.objects
            .select_related("event", "ambassador", "ambassador__user")
            .filter(
                is_approved=True,
                event__isnull=False,
                recap_nudge_sent_at__isnull=True,
                event__end_time__gte=window_start,
                event__end_time__lte=nudge_before,
            )
        )
        if not rosters:
            self.stdout.write("No recently-ended shifts in window; nothing to send.")
            return

        # Which events already have ANY recap — legacy Recap OR CustomRecap.
        # If the event has a recap on file we don't nudge anyone on it. This
        # is event-level (the recap is filed per-event), matching the recap
        # state the old scheduled nudge re-checked at fire time.
        from recaps.models import CustomRecap, Recap

        event_ids = {r.event_id for r in rosters}
        evented_with_recap = set(
            Recap.objects.filter(event_id__in=event_ids).values_list(
                "event_id", flat=True
            )
        ) | set(
            CustomRecap.objects.filter(event_id__in=event_ids).values_list(
                "event_id", flat=True
            )
        )

        # Reachable BAs (active push device) by user id.
        device_user_ids = set(
            PushDevice.objects.filter(is_active=True).values_list(
                "user_id", flat=True
            )
        )

        sent = 0
        stamped_ids: list[int] = []
        skipped_filed = 0
        unreachable = 0
        for r in rosters:
            if r.event_id in evented_with_recap:
                skipped_filed += 1
                continue
            user_id = getattr(r.ambassador, "user_id", None)
            if not user_id or user_id not in device_user_ids:
                unreachable += 1
                continue

            event = r.event
            venue = (getattr(event, "name", None) or "your shift")[:80]
            body = f"Don't forget to file your recap for {venue}."

            if dry:
                self.stdout.write(
                    f"[dry-run] amb={r.ambassador_id} user={user_id} "
                    f"event={r.event_id} end={getattr(event, 'end_time', None)} "
                    f":: {body}"
                )
                sent += 1
                continue

            try:
                _send_push_to_user_sync(
                    user_id,
                    title="Recap due",
                    body=body,
                    data={
                        "screen": "recap",
                        "eventUuid": str(getattr(event, "uuid", "")),
                    },
                )
                sent += 1
                stamped_ids.append(r.id)
            except Exception:
                logger.exception(
                    "recap nudge push failed amb=%s event=%s",
                    r.ambassador_id, r.event_id,
                )

        # Stamp only rows we actually pushed, in one bulk update, so the next
        # hourly run skips them and we never double-nudge.
        if stamped_ids and not dry:
            AmbassadorEvent.objects.filter(id__in=stamped_ids).update(
                recap_nudge_sent_at=now
            )

        self.stdout.write(
            f"recap nudges: sent {sent}, stamped {len(stamped_ids)}, skipped "
            f"{skipped_filed} already-filed, {unreachable} unreachable, across "
            f"{len(rosters)} roster row(s) in window."
        )
