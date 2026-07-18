"""
No-show radar — after a shift starts, catch the BA who never clocked in.

The day-before confirmation (send_shift_confirmations) catches BAs who never
said "I'm in." This is the other half: the BA confirmed (or was never asked)
but the shift has now STARTED and there's still no sign of them on site — no
clock-in, no "I'm here," no GPS ping. That's an active no-show, and every
minute the admin doesn't know is a minute they can't scramble a backup.

Once per run:

  For every approved AmbassadorEvent whose event STARTED more than
  `--threshold-minutes` ago (but within `--lookback-hours`, so we don't
  rescan yesterday) with NO Attendance row of any kind for that BA on that
  event — nudge the BA ("you're not clocked in — are you there?") and add
  the row to a single digest email to the Ignite team. Deduped via
  no_show_alerted_at so each shift pages exactly once.

Distinct from the T-15m activation reminder (which fires BEFORE start): this
is the escalation AFTER start, when silence means a real problem.

Run every ~15 min via `/internal/cron/no-show-alerts` (GHA cron). The
threshold + lookback windows are wider than the cadence so nothing slips.

Usage:
    python manage.py send_no_show_alerts
    python manage.py send_no_show_alerts --threshold-minutes 45 --lookback-hours 8
    python manage.py send_no_show_alerts --dry-run
"""

from __future__ import annotations

import logging
from datetime import timedelta

from django.core.management.base import BaseCommand
from django.utils import timezone

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = (
        "Nudge the BA + alert the Ignite team about approved shifts that "
        "started more than --threshold-minutes ago with no clock-in / "
        "arrival of any kind. Run every ~15 min from a cron runner."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--threshold-minutes",
            type=int,
            default=45,
            help="Flag a shift as a no-show once it started this many minutes "
                 "ago with no attendance (default 45).",
        )
        parser.add_argument(
            "--lookback-hours",
            type=int,
            default=8,
            help="Ignore shifts that started more than this many hours ago "
                 "(default 8) so we never rescan old history.",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Log what would be alerted, but send nothing and stamp nothing.",
        )

    def handle(self, *args, **opts):
        from ambassadors.models import AmbassadorEvent, Attendance, PushDevice

        threshold_minutes = max(1, int(opts["threshold_minutes"]))
        lookback_hours = max(1, int(opts["lookback_hours"]))
        dry = bool(opts["dry_run"])

        now = timezone.now()
        start_ceiling = now - timedelta(minutes=threshold_minutes)
        start_floor = now - timedelta(hours=lookback_hours)

        candidates = list(
            AmbassadorEvent.objects
            .select_related("event", "event__timezone", "ambassador", "ambassador__user")
            .filter(
                is_approved=True,
                event__isnull=False,
                ambassador__isnull=False,
                no_show_alerted_at__isnull=True,
                event__start_time__lte=start_ceiling,
                event__start_time__gte=start_floor,
            )
            .order_by("event__start_time", "event_id")
        )
        if not candidates:
            self.stdout.write("no-show radar: no started shifts in the window.")
            return

        # Drop anyone with ANY attendance for that shift — a clock-in,
        # an "I'm here," even a GPS ping means they're not a no-show.
        on_site = set(
            Attendance.objects.filter(
                event_id__in={r.event_id for r in candidates}
            ).values_list("ambassador_id", "event_id")
        )
        no_shows = [
            r for r in candidates
            if (r.ambassador_id, r.event_id) not in on_site
        ]
        if not no_shows:
            self.stdout.write(
                f"no-show radar: {len(candidates)} started shift(s) in window, "
                "all accounted for."
            )
            return

        # --- Nudge each BA that has a push device (best-effort). ---
        device_user_ids = set(
            PushDevice.objects.filter(is_active=True).values_list(
                "user_id", flat=True
            )
        )
        nudged = 0
        if not dry:
            from ambassadors.push import _send_push_to_user_sync

            for r in no_shows:
                user_id = getattr(r.ambassador, "user_id", None)
                if not user_id or user_id not in device_user_ids:
                    continue
                venue = (getattr(r.event, "name", None) or "your shift")[:80]
                try:
                    _send_push_to_user_sync(
                        user_id,
                        title="Are you at your shift?",
                        body=(
                            f"{venue} has started and you're not clocked in. "
                            "Open Spark to clock in now."
                        ),
                        data={"kind": "no_show_nudge", "screen": "shifts",
                              "eventUuid": str(getattr(r.event, "uuid", "")),
                              "ambassadorEventUuid": str(r.uuid)},
                    )
                    nudged += 1
                except Exception:
                    logger.exception(
                        "no-show BA nudge failed amb=%s event=%s",
                        r.ambassador_id, r.event_id,
                    )

        # --- One digest email to the Ignite team. ---
        alerted = 0
        if self._send_alert(no_shows, dry=dry):
            alerted = len(no_shows)
            if not dry:
                AmbassadorEvent.objects.filter(
                    id__in=[r.id for r in no_shows]
                ).update(no_show_alerted_at=now)

        self.stdout.write(
            f"no-show radar: {len(no_shows)} no-show(s) of {len(candidates)} "
            f"started shift(s); nudged {nudged} BA(s), "
            f"{'would alert' if dry else 'alerted'} on {alerted or len(no_shows)}."
        )

    # ---------- helpers ----------

    def _send_alert(self, rows, *, dry: bool) -> bool:
        """One digest email to the Ignite team listing every no-show, grouped
        by event. Returns True when the email actually went out (so the caller
        stamps); dry-run logs and returns False. Never raises."""
        from ambassadors.queries import _shift_time_labels

        try:
            from tenants.support import _resolve_ignite_recipients

            recipients = _resolve_ignite_recipients()
            if not recipients:
                self.stdout.write(
                    "  [alert] No Ignite recipients resolved — alert skipped."
                )
                return False

            by_event: dict[int, list] = {}
            for r in rows:
                by_event.setdefault(r.event_id, []).append(r)

            lines = [
                "These booked BAs have NOT clocked in and their shift has "
                "already started:",
                "",
            ]
            for group in by_event.values():
                ev = group[0].event
                date_label, start_label, end_label = _shift_time_labels(ev)
                when = ", ".join(x for x in (date_label, start_label) if x)
                if when and end_label:
                    when = f"{when}–{end_label}"
                name = (getattr(ev, "name", None) or "(unnamed event)")
                lines.append(f"• {name}" + (f" — {when}" if when else ""))
                for r in group:
                    user = getattr(r.ambassador, "user", None)
                    nm = ((user.get_full_name() if user else "") or "").strip()
                    em = (user.email if user else "") or ""
                    label = nm or em or f"BA #{r.ambassador_id}"
                    lines.append(
                        f"    - {label}" + (f" <{em}>" if em else "")
                        + " — no clock-in yet"
                    )
                lines.append("")
            lines.append(
                "Call them, or grab a backup from Staffing suggestions on the "
                "request page before the window closes."
            )

            n = len(rows)
            subject = (
                f"[Spark] {n} BA{'s' if n != 1 else ''} not clocked in — "
                "shift already started"
            )
            body = "\n".join(lines)

            if dry:
                self.stdout.write(
                    f"[dry-run] alert to {len(recipients)} recipient(s):\n"
                    f"{subject}\n{body}"
                )
                return False

            # House Resend mailer — NOT django.core.mail (no SMTP on Cloud Run).
            import html as _html

            from utils.mailer import Envelope, Mailer

            body_html = (
                '<pre style="font-family:inherit;white-space:pre-wrap;'
                f'margin:0">{_html.escape(body)}</pre>'
            )

            class _NoShowAlertMailer(Mailer):
                def envelope(self) -> Envelope:
                    return Envelope(
                        subject=subject,
                        html=body_html,
                        to_emails=recipients,
                    )

            _NoShowAlertMailer().send_now()
            return True
        except Exception:
            logger.exception("no-show alert email failed")
            return False
