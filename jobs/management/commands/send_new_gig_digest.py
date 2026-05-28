"""
Daily "new gigs" digest push for ambassadors.

Walks every BA who has an active push device, matches the jobs posted in
the last N hours against that BA's job preferences (preferred states,
minimum hourly rate, favorites-only gate), and sends a single digest
push summarizing how many new gigs they can apply to.

Intended to run once a day from a cron runner (GitHub Actions hits the
`/internal/cron/send-new-gig-digest` endpoint — see digest/cron_views.py).
Idempotency note: this is a best-effort nudge, not a transactional
notification. Running it twice in a day double-notifies; the GHA schedule
fires it once.

Usage:
    python manage.py send_new_gig_digest                 # last 24h
    python manage.py send_new_gig_digest --hours 12
    python manage.py send_new_gig_digest --dry-run       # log, don't push
"""

from __future__ import annotations

import logging
from datetime import timedelta

from django.core.management.base import BaseCommand
from django.utils import timezone

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = (
        "Send each BA a digest push of new gigs (posted in the last N hours) "
        "that match their job preferences. Best-effort; run once daily."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--hours",
            type=int,
            default=24,
            help="Look-back window in hours for newly posted jobs (default 24).",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Compute matches + log who would be notified, but send nothing.",
        )

    def handle(self, *args, **opts):
        from ambassadors.models import Ambassador, PushDevice
        from ambassadors.push import _send_push_to_user_sync
        from jobs import models as jm

        hours = max(1, int(opts["hours"]))
        dry = bool(opts["dry_run"])
        since = timezone.now() - timedelta(hours=hours)

        new_jobs = list(
            jm.Job.objects
            .select_related("event", "event__state", "event__tenant")
            .filter(lifecycle_status=jm.Job.STATUS_POSTED, posted_at__gte=since)
        )
        if not new_jobs:
            self.stdout.write("No new posted jobs in window; nothing to send.")
            return

        # Only BAs with an active push device are reachable.
        device_user_ids = set(
            PushDevice.objects.filter(is_active=True).values_list(
                "user_id", flat=True
            )
        )
        if not device_user_ids:
            self.stdout.write("No active push devices; nothing to send.")
            return

        ambs = list(
            Ambassador.objects.filter(user_id__in=device_user_ids)
            .select_related("user")
        )
        amb_ids = [a.id for a in ambs]
        job_ids = [j.id for j in new_jobs]

        prefs_by_amb = {
            p.ambassador_id: p
            for p in jm.AmbassadorJobPreference.objects.filter(
                ambassador_id__in=amb_ids
            )
        }
        fav_by_amb: dict[int, set] = {}
        for amb_id, tid in jm.TenantFavoriteAmbassador.objects.filter(
            ambassador_id__in=amb_ids
        ).values_list("ambassador_id", "tenant_id"):
            fav_by_amb.setdefault(amb_id, set()).add(tid)

        applied_by_amb: dict[int, set] = {}
        for amb_id, jid in jm.JobApplication.objects.filter(
            ambassador_id__in=amb_ids, job_id__in=job_ids
        ).values_list("ambassador_id", "job_id"):
            applied_by_amb.setdefault(amb_id, set()).add(jid)

        sent = 0
        for amb in ambs:
            pref = prefs_by_amb.get(amb.id)
            if pref is not None and not pref.notify_new_gigs:
                continue
            states = set(pref.preferred_state_codes or []) if pref else set()
            min_rate = pref.min_hourly_rate if pref else None
            fav_tenants = fav_by_amb.get(amb.id, set())
            applied = applied_by_amb.get(amb.id, set())

            matches = [
                job for job in new_jobs
                if self._matches(job, states, min_rate, fav_tenants, applied)
            ]
            if not matches:
                continue

            title, body = self._compose(matches)
            if dry:
                self.stdout.write(
                    f"[dry-run] amb={amb.id} user={amb.user_id} "
                    f"matches={len(matches)} :: {body}"
                )
                sent += 1
                continue
            try:
                _send_push_to_user_sync(
                    amb.user_id,
                    title=title,
                    body=body,
                    # `screen` routes the tap (mobile reads data.screen, same
                    # convention as the recap nudge); type/count are extras.
                    data={
                        "screen": "jobs",
                        "type": "new_gig_digest",
                        "count": len(matches),
                    },
                )
                sent += 1
            except Exception:
                logger.exception(
                    "new-gig digest push failed amb=%s user=%s",
                    amb.id, amb.user_id,
                )

        self.stdout.write(
            f"new-gig digest: notified {sent} BA(s) about "
            f"{len(new_jobs)} new job(s) in the last {hours}h."
        )

    @staticmethod
    def _matches(job, states: set, min_rate, fav_tenants: set, applied: set) -> bool:
        """Does this new job match the BA's preferences + gates?"""
        if job.id in applied:
            return False
        if job.favorites_only and job.tenant_id not in fav_tenants:
            return False
        if states:
            code = getattr(getattr(job.event, "state", None), "code", None)
            if not code or code.upper() not in states:
                return False
        # Min pay: only exclude when the job has a rate AND it's below the
        # floor — never hide a gig just because pay isn't filled in yet.
        if min_rate is not None and job.hourly_rate is not None:
            if job.hourly_rate < min_rate:
                return False
        return True

    @staticmethod
    def _compose(matches: list) -> tuple[str, str]:
        n = len(matches)
        if n == 1:
            job = matches[0]
            code = getattr(getattr(job.event, "state", None), "code", None)
            where = f" in {code}" if code else ""
            return (
                "New gig available 🎉",
                f"A new gig just posted{where}. Tap to view and apply.",
            )
        return (
            "New gigs available 🎉",
            f"{n} new gigs match your preferences. Tap to view the board.",
        )
