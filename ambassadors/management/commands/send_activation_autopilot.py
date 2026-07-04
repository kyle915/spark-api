"""Activation autopilot: get booked-but-never-signed-in BAs into the app
before their shift, and keep the Ignite team looking at the stragglers.

Feel Free week one: 8 BAs were booked, 5 had never signed in, and the
only signal was an ad-hoc script the day-of — the confirmation-push cron
literally reported "12 of 12 unreachable" because a push can't reach
someone who never opened the app. This closes that loop automatically.

Each run, for every APPROVED booking whose event starts within the next
72h where the BA has NEVER logged in:

  * BA-facing (once per booking): the first time a booking enters the
    window we re-send the "Welcome to Spark" email with a fresh temp
    password + app-store buttons (reset_ba_welcome_and_email) and stamp
    ``activation_nudge_stage=1`` so we never reset a second time (a second
    reset would invalidate the password from the first email). A BA with
    several imminent shifts gets ONE email, and all their in-window
    bookings are stamped together.

  * Admin-facing (every run): one digest to the Ignite team listing every
    still-dark BA with an imminent shift — name, email, phone, soonest
    shift, and an URGENT flag inside 24h — so a human can call the ones
    the email won't save.

Idempotent + best-effort: per-BA failures are logged and never abort the
run; ``--dry-run`` prints the plan and sends/stamps nothing. Prod:
ActivationAutopilotView (/internal/cron/activation-autopilot) + the
activation-autopilot workflow (every 6h + manual dispatch).
"""

from __future__ import annotations

import datetime
import logging

from django.core.management.base import BaseCommand
from django.utils import timezone

logger = logging.getLogger(__name__)

WINDOW_HOURS = 72
URGENT_HOURS = 24


class Command(BaseCommand):
    help = (
        "Email never-signed-in BAs with a shift in the next 72h (once each) "
        "+ a digest of the stragglers to the Ignite team. Dry-run prints only."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--window-hours",
            type=int,
            default=WINDOW_HOURS,
            help=f"Look-ahead window for upcoming shifts (default {WINDOW_HOURS}).",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Print the plan; send no emails and stamp nothing.",
        )

    def handle(self, *args, **opts):
        from ambassadors.models import AmbassadorEvent

        w = self.stdout.write
        dry = opts["dry_run"]
        window_hours = opts["window_hours"]
        now = timezone.now()
        window_end = now + datetime.timedelta(hours=window_hours)
        urgent_end = now + datetime.timedelta(hours=URGENT_HOURS)

        rows = list(
            AmbassadorEvent.objects.filter(
                is_approved=True,
                event__isnull=False,
                event__start_time__gt=now,
                event__start_time__lte=window_end,
                ambassador__isnull=False,
                ambassador__user__isnull=False,
                ambassador__user__last_login__isnull=True,
            )
            .select_related("ambassador__user", "event", "tenant")
            .order_by("event__start_time")
        )

        # Group by BA (user): soonest shift, whether any booking still needs
        # the first email (stage 0), and the bookings to stamp.
        by_user: dict[int, dict] = {}
        for ae in rows:
            user = ae.ambassador.user
            g = by_user.setdefault(
                user.id,
                {
                    "user": user,
                    "ambassador": ae.ambassador,
                    "soonest": ae.event.start_time,
                    "venue": getattr(ae.event, "name", None) or "(shift)",
                    "tenant": getattr(ae.tenant, "name", "") or "",
                    "needs_email": False,
                    "events": [],
                },
            )
            g["events"].append(ae)
            if ae.event.start_time and ae.event.start_time < g["soonest"]:
                g["soonest"] = ae.event.start_time
                g["venue"] = getattr(ae.event, "name", None) or "(shift)"
            if (ae.activation_nudge_stage or 0) < 1:
                g["needs_email"] = True

        w("")
        w(f"window     : next {window_hours}h (urgent ≤ {URGENT_HOURS}h)")
        w(f"dark BAs   : {len(by_user)} never-signed-in with an upcoming shift")
        if not by_user:
            w("Nothing to do — everyone booked soon has signed in.")
            return

        emailed = 0
        for g in by_user.values():
            user = g["user"]
            urgent = bool(g["soonest"] and g["soonest"] <= urgent_end)
            tag = "URGENT" if urgent else "soon"
            w(
                f"  [{tag}] {user.get_full_name() or user.email} <{user.email}> "
                f"· {g['tenant']} · {g['soonest']:%m-%d %H:%M} · "
                f"{'email+stamp' if g['needs_email'] else 'already emailed'}"
            )
            if dry or not g["needs_email"]:
                continue
            try:
                self._send_ba_welcome(user.email)
                for ae in g["events"]:
                    if (ae.activation_nudge_stage or 0) < 1:
                        ae.activation_nudge_stage = 1
                        ae.save(update_fields=["activation_nudge_stage", "updated_at"])
                emailed += 1
            except Exception:  # noqa: BLE001 — one BA must not abort the run
                logger.exception(
                    "activation autopilot: welcome email failed for %s", user.email
                )

        if dry:
            w("DRY-RUN — no emails sent, nothing stamped.")
            return

        w(f"BA welcome emails sent: {emailed}")
        self._send_admin_digest(by_user, urgent_end)
        w(self.style.SUCCESS("Admin digest sent."))

    # -- helpers -----------------------------------------------------------

    def _send_ba_welcome(self, email: str) -> None:
        """Fresh temp password + app-store buttons via the shared service."""
        from ambassadors.services import reset_ba_welcome_and_email

        reset_ba_welcome_and_email(email)

    def _send_admin_digest(self, by_user: dict, urgent_end) -> None:
        import html as _html

        from django.conf import settings

        from tenants.support import _resolve_ignite_recipients
        from utils.mailer import Envelope, Mailer

        recipients = _resolve_ignite_recipients()
        if not recipients:
            self.stdout.write("  [digest] No Ignite recipients — skipped.")
            return

        ordered = sorted(by_user.values(), key=lambda g: g["soonest"])
        n_urgent = sum(1 for g in ordered if g["soonest"] <= urgent_end)

        def _row(g):
            user = g["user"]
            urgent = g["soonest"] <= urgent_end
            phone = getattr(g["ambassador"], "phone", None) or "—"
            return (
                "<tr>"
                f"<td>{'🔴' if urgent else '🟡'}</td>"
                f"<td>{_html.escape(user.get_full_name() or '—')}</td>"
                f"<td>{_html.escape(user.email)}</td>"
                f"<td>{_html.escape(str(phone))}</td>"
                f"<td>{_html.escape(g['tenant'])}</td>"
                f"<td>{g['soonest']:%a %m-%d %I:%M %p} UTC</td>"
                "</tr>"
            )

        table = "".join(_row(g) for g in ordered)
        body = f"""
        <h2 style="margin:0 0 8px">{len(ordered)} booked BA(s) haven't signed in yet</h2>
        <p style="margin:0 0 12px;color:#555">
          These ambassadors have an approved shift in the next {WINDOW_HOURS}h and have
          <b>never signed into Spark</b>. Each was auto-sent a fresh welcome email with
          a temp password + app links. {n_urgent} have a shift within {URGENT_HOURS}h
          (🔴) — worth a direct call.
        </p>
        <table cellpadding="6" style="border-collapse:collapse" border="1">
          <tr><th></th><th>Name</th><th>Email</th><th>Phone</th><th>Client</th><th>Soonest shift</th></tr>
          {table}
        </table>
        <p style="color:#888;margin-top:12px">Activation autopilot · once a BA signs in
          they drop off this list automatically.</p>
        """

        subject = (
            f"[Spark] {len(ordered)} BA(s) not signed in "
            f"({n_urgent} within {URGENT_HOURS}h)"
        )

        class _DigestMailer(Mailer):
            def envelope(self) -> Envelope:
                return Envelope(subject=subject, html=body, to_emails=recipients)

        _ = settings  # (kept for parity with other crons' imports)
        _DigestMailer().send_now()
