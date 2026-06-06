"""Alert eligible BAs when a shift is dropped (an OpenShift opens up).

The fill half of shift-swap closes the loop only if eligible BAs *learn*
a shift opened — this fans out a push to them. It runs as a wall-clock
cron (hit via `/internal/cron/send-open-shift-alerts`), NOT inline in the
drop request: there's no RQ worker in prod (see #270), and fanning out to
every eligible BA inline would stall the drop mutation. Each OpenShift is
alerted exactly once — `notified_at` is stamped after the sweep, so the
cron cadence + a future-only filter mean a dropped shift pings its pool
once and never re-blasts.

Eligibility mirrors the `myOpenShifts` board: future, unclaimed, brand the
BA has worked with, not the dropper, not already on the event, reachable
(active push device). When the event has coordinates, a proximity gate
(same radius as the new-gig-nearby push) keeps it relevant; otherwise the
whole worked-with-brand pool is alerted, capped per shift. Respects the
BA's "gigs" push preference (kind="open_shift" → gigs category).

Usage::
    python manage.py send_open_shift_alerts
    python manage.py send_open_shift_alerts --radius-miles 40 --max-per-shift 150
    python manage.py send_open_shift_alerts --dry-run
"""

from __future__ import annotations

import logging

from django.core.management.base import BaseCommand
from django.utils import timezone

logger = logging.getLogger(__name__)

DEFAULT_RADIUS_MILES = 30.0
DEFAULT_MAX_PER_SHIFT = 200


class Command(BaseCommand):
    help = "Push eligible BAs when a shift is dropped (open-shift alerts)."

    def add_arguments(self, parser):
        parser.add_argument("--radius-miles", type=float, default=DEFAULT_RADIUS_MILES)
        parser.add_argument("--max-per-shift", type=int, default=DEFAULT_MAX_PER_SHIFT)
        parser.add_argument("--dry-run", action="store_true")

    def handle(self, *args, **opts):
        from ambassadors.models import (
            Ambassador,
            AmbassadorEvent,
            OpenShift,
            PushDevice,
        )
        from ambassadors.push import _send_push_to_user_sync
        from ambassadors.reliability import (
            NEUTRAL_SORT_SCORE,
            reliability_for_users,
        )

        try:
            from ambassadors.staffing import _haversine_miles
        except Exception:  # pragma: no cover - defensive import
            _haversine_miles = None  # type: ignore

        radius = float(opts["radius_miles"])
        max_per_shift = int(opts["max_per_shift"])
        dry_run = bool(opts["dry_run"])
        now = timezone.now()

        open_shifts = list(
            OpenShift.objects.select_related("event", "event__state").filter(
                claimed_at__isnull=True,
                notified_at__isnull=True,
                event__start_time__gt=now,
            )
        )
        if not open_shifts:
            self.stdout.write("open-shift alerts: nothing to alert.")
            return

        device_user_ids = set(
            PushDevice.objects.filter(is_active=True).values_list(
                "user_id", flat=True
            )
        )

        total_sent = 0
        processed = 0
        for os_row in open_shifts:
            ev = os_row.event
            venue = (getattr(ev, "name", None) or "a shift").strip() or "a shift"
            ev_coords = getattr(ev, "coordinates", None)

            worked_amb_ids = set(
                AmbassadorEvent.objects.filter(
                    event__tenant_id=ev.tenant_id
                ).values_list("ambassador_id", flat=True)
            )
            already_amb_ids = set(
                AmbassadorEvent.objects.filter(event=ev).values_list(
                    "ambassador_id", flat=True
                )
            )
            candidates = list(
                Ambassador.objects.filter(
                    id__in=worked_amb_ids, user_id__in=device_user_ids
                )
                .exclude(id__in=already_amb_ids)
                .select_related("user")
            )

            # Rank most-reliable-first so that when the fan-out is capped
            # (max_per_shift) the BAs most likely to actually show up are the
            # ones pinged. One batched set of grouped COUNTs for the whole pool
            # (no N+1); ties break on completed-shift count. New BAs sort at a
            # neutral rank — above known droppers, below proven-reliable.
            reliability = reliability_for_users([a.user_id for a in candidates])
            candidates.sort(
                key=lambda a: (
                    reliability[a.user_id].sort_score
                    if a.user_id in reliability
                    else NEUTRAL_SORT_SCORE,
                    reliability[a.user_id].completed
                    if a.user_id in reliability
                    else 0,
                ),
                reverse=True,
            )

            sent_this = 0
            capped = False
            for amb in candidates:
                if amb.user_id == os_row.released_by_id:
                    continue
                if sent_this >= max_per_shift:
                    capped = True
                    break

                amb_coords = getattr(amb, "coordinates", None)
                distance = None
                if _haversine_miles and ev_coords and amb_coords:
                    try:
                        distance = _haversine_miles(ev_coords, amb_coords)
                    except Exception:
                        distance = None
                if distance is not None:
                    if distance > radius:
                        continue
                    body = (
                        f"A shift opened up ~{int(round(distance))} mi away — "
                        f"{venue}. Tap to grab it."
                    )
                else:
                    body = (
                        f"A shift opened up — {venue}. Tap to grab it before "
                        f"it's gone."
                    )

                if not dry_run:
                    try:
                        _send_push_to_user_sync(
                            amb.user_id,
                            title="Open shift available",
                            body=body,
                            data={
                                "kind": "open_shift",
                                "screen": "openshifts",
                                "openShiftUuid": str(os_row.uuid),
                                "eventUuid": str(getattr(ev, "uuid", "")),
                            },
                        )
                    except Exception:
                        logger.exception(
                            "open-shift alert push failed amb=%s open_shift=%s",
                            amb.id, os_row.id,
                        )
                        continue
                sent_this += 1

            if capped:
                logger.info(
                    "open-shift alert capped at %s for open_shift=%s (pool larger)",
                    max_per_shift, os_row.id,
                )
            if not dry_run:
                os_row.notified_at = now
                os_row.save(update_fields=["notified_at"])
            total_sent += sent_this
            processed += 1

        verb = "would alert" if dry_run else "alerted"
        self.stdout.write(
            f"open-shift alerts: {verb} {total_sent} BA(s) across "
            f"{processed} open shift(s)."
        )
