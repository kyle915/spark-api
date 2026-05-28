"""
Aggressive daily recap reminder sweep.

The per-shift recap nudge (events/signals.py → schedule_recap_nudge_at)
fires once, a couple hours after a shift ends. Plenty of BAs still don't
file. This command is the follow-up hammer: a daily sweep that re-nudges
every BA who worked a shift in the last N days and still hasn't filed a
recap for it — escalating the message by how overdue it is.

Reachability: only BAs with an active push device are nudged. Targets
approved roster rows (AmbassadorEvent.is_approved) for events whose
effective end (new_end_time → end_time → date) is older than a short
grace window and within the look-back window.

Intended to run once a day from a cron runner (GitHub Actions hits
`/internal/cron/send-recap-reminders`).

Usage:
    python manage.py send_recap_reminders
    python manage.py send_recap_reminders --max-age-days 10 --grace-hours 3
    python manage.py send_recap_reminders --dry-run
"""

from __future__ import annotations

import logging
from datetime import timedelta

from django.core.management.base import BaseCommand
from django.db.models.functions import Coalesce
from django.utils import timezone

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = (
        "Re-nudge every BA who worked a shift in the last N days and still "
        "hasn't filed its recap. Escalates by days overdue. Run once daily."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--max-age-days",
            type=int,
            default=7,
            help="Stop nagging once a shift is older than this (default 7).",
        )
        parser.add_argument(
            "--grace-hours",
            type=int,
            default=2,
            help="Don't nag until this many hours after the shift ends "
                 "(default 2 — matches the per-shift nudge delay).",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Log who would be nudged, but send nothing.",
        )

    def handle(self, *args, **opts):
        from ambassadors.models import AmbassadorEvent, PushDevice
        from ambassadors.push import _send_push_to_user_sync
        from recaps.models import Recap

        max_age_days = max(1, int(opts["max_age_days"]))
        grace_hours = max(0, int(opts["grace_hours"]))
        dry = bool(opts["dry_run"])

        now = timezone.now()
        window_start = now - timedelta(days=max_age_days)
        nag_before = now - timedelta(hours=grace_hours)

        # Approved roster rows for events that ended in the window. Effective
        # end = first non-null of new_end_time / end_time / date.
        rosters = list(
            AmbassadorEvent.objects
            .select_related("event", "ambassador", "ambassador__user")
            .filter(is_approved=True, event__isnull=False)
            .annotate(
                eff_end=Coalesce(
                    "event__new_end_time", "event__end_time", "event__date",
                )
            )
            .filter(eff_end__gte=window_start, eff_end__lte=nag_before)
        )
        if not rosters:
            self.stdout.write("No outstanding shifts in window; nothing to send.")
            return

        event_ids = {r.event_id for r in rosters}
        amb_ids = {r.ambassador_id for r in rosters}

        # Which (event, ambassador) pairs already have a filed recap.
        filed = set(
            Recap.objects.filter(
                submited_at__isnull=False,
                event_id__in=event_ids,
                ambassador_id__in=amb_ids,
            ).values_list("event_id", "ambassador_id")
        )

        # Reachable BAs (active push device) by user id.
        device_user_ids = set(
            PushDevice.objects.filter(is_active=True).values_list(
                "user_id", flat=True
            )
        )

        sent = 0
        skipped_filed = 0
        for r in rosters:
            if (r.event_id, r.ambassador_id) in filed:
                skipped_filed += 1
                continue
            user_id = getattr(r.ambassador, "user_id", None)
            if not user_id or user_id not in device_user_ids:
                continue

            eff_end = getattr(r, "eff_end", None)
            days_over = max(0, (now - eff_end).days) if eff_end else 0
            event_name = getattr(r.event, "name", None) or "your shift"
            title, body = self._compose(event_name, days_over)

            if dry:
                self.stdout.write(
                    f"[dry-run] amb={r.ambassador_id} user={user_id} "
                    f"event={r.event_id} days_over={days_over} :: {body}"
                )
                sent += 1
                continue
            try:
                _send_push_to_user_sync(
                    user_id,
                    title=title,
                    body=body,
                    data={
                        "screen": "recap",
                        "eventUuid": str(getattr(r.event, "uuid", "")),
                    },
                )
                sent += 1
            except Exception:
                logger.exception(
                    "recap reminder push failed amb=%s event=%s",
                    r.ambassador_id, r.event_id,
                )

        self.stdout.write(
            f"recap reminders: nudged {sent}, skipped {skipped_filed} "
            f"already-filed, across {len(rosters)} roster row(s)."
        )

    @staticmethod
    def _compose(event_name: str, days_over: int) -> tuple[str, str]:
        if days_over <= 0:
            return (
                "Recap due",
                f"Don't forget to file your recap for {event_name}.",
            )
        if days_over == 1:
            return (
                "Recap overdue",
                f"Your recap for {event_name} is a day overdue — "
                f"please file it so we can bill the client.",
            )
        return (
            "Recap overdue",
            f"Your recap for {event_name} is {days_over} days overdue. "
            f"Please file it as soon as you can.",
        )
