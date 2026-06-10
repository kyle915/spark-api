"""
Day-before shift confirmations + morning-of unconfirmed alert.

The no-show problem: today the first signal an admin gets that a BA
isn't coming is a missed clock-in 15 minutes AFTER the activation was
supposed to start — far too late to staff a backup. This command moves
that signal to the day before, in two phases per run:

Phase A — confirmation request (~T-24h):
  For every approved AmbassadorEvent whose event starts within
  `--lead-hours` (default 26) that hasn't been asked or confirmed yet,
  push "Confirm your shift" (one tap in the app stamps confirmed_at,
  via the confirmShift mutation). Deduped via confirmation_requested_at
  — one ask per shift. BAs without an active push device are skipped
  WITHOUT stamping so a later run catches them if they register; either
  way Phase B still watches them.

Phase B — unconfirmed alert (morning-of):
  For every approved row whose event starts within `--alert-hours`
  (default 4) still missing confirmed_at — and with no Attendance row
  proving the BA is already on site — email the Ignite team ONE digest
  ("these BAs never confirmed today's shifts") so they can chase or
  grab a backup from staffing suggestions while there's still time.
  Deduped via unconfirmed_alerted_at. Rows asked less than an hour ago
  get a grace period before alerting (a late booking shouldn't page
  admins seconds after the BA was first asked).

Confirmation also flips automatically when the BA arrives / clocks in
(ambassadors.mutations._auto_confirm_on_attendance) — showing up IS
confirming.

Run hourly via `/internal/cron/shift-confirmations` (GHA cron, see
.github/workflows/shift-confirmations.yml). The 26h/4h windows are
wider than the hourly cadence on purpose so nothing slips between
runs; the stamps keep every send/alert to exactly one.

Usage:
    python manage.py send_shift_confirmations
    python manage.py send_shift_confirmations --lead-hours 26 --alert-hours 4
    python manage.py send_shift_confirmations --dry-run
"""

from __future__ import annotations

import logging
from datetime import timedelta

from django.core.management.base import BaseCommand
from django.utils import timezone

logger = logging.getLogger(__name__)

# Don't alert on a row the BA was only just asked about — give them a
# beat to answer the push before paging the Ignite team.
ALERT_GRACE = timedelta(hours=1)


class Command(BaseCommand):
    help = (
        "Day-before 'confirm you're in' push for approved shifts starting "
        "within --lead-hours (once per shift), plus a morning-of alert "
        "email to the Ignite team for still-unconfirmed shifts starting "
        "within --alert-hours. Run hourly from a cron runner."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--lead-hours",
            type=int,
            default=26,
            help="Ask for confirmation on shifts starting within this many "
                 "hours (default 26 — covers T-24h with an hourly cron, and "
                 "sweeps up late bookings on the next run).",
        )
        parser.add_argument(
            "--alert-hours",
            type=int,
            default=4,
            help="Alert the Ignite team about unconfirmed shifts starting "
                 "within this many hours (default 4).",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Log what would be sent, but send nothing and stamp nothing.",
        )

    def handle(self, *args, **opts):
        from ambassadors.models import AmbassadorEvent, Attendance, PushDevice
        from ambassadors.push import _send_push_to_user_sync

        lead_hours = max(1, int(opts["lead_hours"]))
        alert_hours = max(1, int(opts["alert_hours"]))
        dry = bool(opts["dry_run"])

        now = timezone.now()

        # ---------- Phase A: confirmation requests ----------
        window_end = now + timedelta(hours=lead_hours)
        rosters = list(
            AmbassadorEvent.objects
            .select_related(
                "event", "event__timezone", "event__retailer",
                "ambassador", "ambassador__user",
            )
            .filter(
                is_approved=True,
                event__isnull=False,
                confirmation_requested_at__isnull=True,
                confirmed_at__isnull=True,
                event__start_time__gt=now,
                event__start_time__lte=window_end,
            )
        )

        device_user_ids = set(
            PushDevice.objects.filter(is_active=True).values_list(
                "user_id", flat=True
            )
        )

        asked = 0
        stamped_ids: list[int] = []
        unreachable = 0
        for r in rosters:
            user_id = getattr(r.ambassador, "user_id", None)
            if not user_id or user_id not in device_user_ids:
                unreachable += 1
                continue

            body = self._compose_body(r.event)
            if dry:
                self.stdout.write(
                    f"[dry-run] ask amb={r.ambassador_id} user={user_id} "
                    f"event={r.event_id} "
                    f"start={getattr(r.event, 'start_time', None)} :: {body}"
                )
                asked += 1
                continue

            try:
                _send_push_to_user_sync(
                    user_id,
                    title="Confirm your shift",
                    body=body,
                    # kind=shift_confirmation is deliberately NOT a mutable
                    # preference category (ambassadors.push._push_category)
                    # — it's transactional, like "you got booked". The
                    # uuids let the mobile tap handler deep-link straight
                    # to the shift detail's confirm button.
                    data={
                        "kind": "shift_confirmation",
                        "screen": "shifts",
                        "eventUuid": str(getattr(r.event, "uuid", "")),
                        "ambassadorEventUuid": str(r.uuid),
                    },
                )
                asked += 1
                stamped_ids.append(r.id)
            except Exception:
                logger.exception(
                    "shift confirmation push failed amb=%s event=%s",
                    r.ambassador_id, r.event_id,
                )

        # One bulk stamp so a second run in the same window can't re-ask.
        if stamped_ids and not dry:
            AmbassadorEvent.objects.filter(id__in=stamped_ids).update(
                confirmation_requested_at=now
            )

        # ---------- Phase B: morning-of unconfirmed alert ----------
        alert_end = now + timedelta(hours=alert_hours)
        at_risk = list(
            AmbassadorEvent.objects
            .select_related(
                "event", "event__timezone",
                "ambassador", "ambassador__user",
            )
            .filter(
                is_approved=True,
                event__isnull=False,
                confirmed_at__isnull=True,
                unconfirmed_alerted_at__isnull=True,
                event__start_time__gt=now,
                event__start_time__lte=alert_end,
            )
            .order_by("event__start_time", "event_id")
        )

        # Grace: a row asked < 1h ago hasn't had a fair chance to answer.
        # (Includes rows Phase A stamped seconds ago in THIS run.) Rows
        # never asked at all — unreachable BAs — alert immediately: that's
        # exactly the risk the Ignite team needs to see.
        grace_cut = now - ALERT_GRACE
        at_risk = [
            r for r in at_risk
            if r.confirmation_requested_at is None
            or r.confirmation_requested_at <= grace_cut
        ]

        # Drop anyone already proven on site by an attendance ping.
        if at_risk:
            on_site = set(
                Attendance.objects.filter(
                    event_id__in={r.event_id for r in at_risk}
                ).values_list("ambassador_id", "event_id")
            )
            at_risk = [
                r for r in at_risk
                if (r.ambassador_id, r.event_id) not in on_site
            ]

        alerted = 0
        if at_risk:
            if self._send_alert(at_risk, dry=dry):
                alerted = len(at_risk)
                if not dry:
                    AmbassadorEvent.objects.filter(
                        id__in=[r.id for r in at_risk]
                    ).update(unconfirmed_alerted_at=now)

        self.stdout.write(
            f"shift confirmations: asked {asked} "
            f"({len(stamped_ids)} stamped, {unreachable} unreachable) of "
            f"{len(rosters)} in the {lead_hours}h window; alerted on "
            f"{alerted} unconfirmed row(s) in the {alert_hours}h window."
        )

    # ---------- helpers ----------

    @staticmethod
    def _compose_body(event) -> str:
        """"You're booked at {venue} — Tue, Jun 10, 11:00 AM–3:00 PM."
        in the EVENT's timezone, reusing the DST-aware label helper the
        mobile shift cards already render verbatim."""
        from ambassadors.queries import _shift_time_labels

        venue = (getattr(event, "name", None) or "your shift")[:80]
        date_label, start_label, end_label = _shift_time_labels(event)
        if date_label and start_label and end_label:
            when = f" — {date_label}, {start_label}–{end_label}"
        elif date_label and start_label:
            when = f" — {date_label}, {start_label}"
        elif date_label:
            when = f" — {date_label}"
        else:
            when = " tomorrow"
        return f"You're booked at {venue}{when}. Tap to confirm you're in."

    def _send_alert(self, rows, *, dry: bool) -> bool:
        """One digest email to the Ignite team listing every unconfirmed
        row, grouped by event. Returns True when the email actually went
        out (so the caller stamps); dry-run logs and returns False.
        Never raises — an SMTP hiccup must not kill Phase A's stamps."""
        from ambassadors.queries import _shift_time_labels

        try:
            from django.conf import settings
            from django.core.mail import EmailMessage

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
                "These booked BAs have NOT confirmed their upcoming shift:",
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
                    why = (
                        "no reply to the confirmation push"
                        if r.confirmation_requested_at
                        else "was never reached (no push device)"
                    )
                    lines.append(f"    - {label}" + (f" <{em}>" if em else "") + f" — {why}")
                lines.append("")
            lines.append(
                "Chase them down or grab a backup from Staffing suggestions "
                "on the request page before doors open."
            )

            n = len(rows)
            subject = (
                f"[Spark] {n} unconfirmed BA{'s' if n != 1 else ''} "
                "for shifts starting soon"
            )
            body = "\n".join(lines)

            if dry:
                self.stdout.write(
                    f"[dry-run] alert to {len(recipients)} recipient(s):\n"
                    f"{subject}\n{body}"
                )
                return False

            EmailMessage(
                subject=subject,
                body=body,
                from_email=settings.DEFAULT_FROM_EMAIL,
                to=recipients,
            ).send()
            return True
        except Exception:
            logger.exception("unconfirmed-shift alert email failed")
            return False
