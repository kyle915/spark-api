"""Cron staleness watchdog: alert the Ignite team when a scheduled job
goes overdue or starts erroring.

The heartbeat table (digest.CronRun, stamped by the wrapper in
digest.urls on every `/internal/cron/<name>` hit) records WHEN each job
last ran and whether it returned 2xx. This command reads that table and
compares each recurring cron against its expected cadence — the piece
that turns the passive System Health page into a proactive alarm. It's
the fix for "the RQ scheduler died silently for weeks and nobody knew":
now a job that stops firing (workflow disabled, billing lapse, deploy
broke the endpoint) surfaces as an email within a cadence window.

Only RECURRING crons are watched (EXPECTED_MAX_HOURS below). Manual
one-off ops (repair-*, backfill-*, audit-*, provisioning) are triggered
ad hoc, so "stale" is meaningless for them — they're excluded.

A cron is a PROBLEM when it has a heartbeat and either:
  * OVERDUE — last run older than its cadence + jitter grace, or
  * ERRORED — last run returned a non-2xx status.

Never-fired crons (no heartbeat row yet) are reported but do NOT alert
by default (`--alert-never-seen` opts in): right after this ships the
table is cold, and daily/weekly/monthly jobs legitimately have no row
until their first fire. Overdue/errored are the false-positive-free
signals — a job that WAS working and stopped.

Alerts are throttled per-cron via CronRun.last_alerted_at (default 12h)
so a persistently-stuck job doesn't re-email on every check. Read-only
apart from that stamp; `--dry-run` prints the plan and writes nothing.

Prod: CheckCronHealthView (/internal/cron/check-cron-health) + the
check-cron-health workflow (every 6h + manual dispatch).
"""

from __future__ import annotations

import logging

from django.core.management.base import BaseCommand
from django.utils import timezone

logger = logging.getLogger(__name__)

# endpoint-name → max hours between runs before "overdue". Grace is baked
# in above the nominal cadence to absorb GitHub Actions scheduling jitter
# (scheduled runs can lag several minutes, and daily/weekly slots drift).
# Keys MUST match the CronRun.name recorded by digest.urls (the
# _registered_views() path segment).
EXPECTED_MAX_HOURS: dict[str, float] = {
    # every ~10 min
    "activation-reminders": 1,
    "send-open-shift-alerts": 1,
    # hourly
    "recap-nudges": 3,
    "shift-confirmations": 3,
    # every 6h
    "activation-autopilot": 9,
    # daily
    "send-admin-digest": 30,
    "send-document-expiry-reminders": 30,
    "send-payment-notifications": 30,
    "send-new-gig-digest": 30,
    "send-recap-reminders": 30,
    "export-recaps-to-sheet": 30,
    "export-ld-summary": 30,
    # weekly (Monday)
    "send-client-weekly-digest": 8 * 24,
    "recap-data-health": 8 * 24,
    "send-executive-summary": 8 * 24,
    "weekly-mileage-report": 8 * 24,
    # monthly (1st)
    "send-scheduled-client-reports": 33 * 24,
}

DEFAULT_THROTTLE_HOURS = 12


class Command(BaseCommand):
    help = (
        "Alert the Ignite team about scheduled crons that have gone overdue "
        "or are erroring (per digest.CronRun heartbeats). Dry-run prints only."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Print the plan; send no email and stamp nothing.",
        )
        parser.add_argument(
            "--throttle-hours",
            type=int,
            default=DEFAULT_THROTTLE_HOURS,
            help=(
                "Don't re-alert about the same cron within this many hours "
                f"(default {DEFAULT_THROTTLE_HOURS})."
            ),
        )
        parser.add_argument(
            "--alert-never-seen",
            action="store_true",
            help=(
                "Also alert on watched crons that have no heartbeat at all "
                "(off by default to avoid cold-start false alarms)."
            ),
        )

    def handle(self, *args, **opts):
        from digest.models import CronRun

        w = self.stdout.write
        dry = opts["dry_run"]
        throttle_hours = opts["throttle_hours"]
        alert_never_seen = opts["alert_never_seen"]
        now = timezone.now()

        by_name = {c.name: c for c in CronRun.objects.all()}

        problems: list[dict] = []   # overdue / errored / never-seen
        healthy = 0
        for name, max_hours in sorted(EXPECTED_MAX_HOURS.items()):
            row = by_name.get(name)
            if row is None or row.last_run_at is None:
                problems.append(
                    {
                        "name": name,
                        "kind": "never-seen",
                        "max_hours": max_hours,
                        "row": row,
                        "hours_since": None,
                    }
                )
                continue
            hours_since = (now - row.last_run_at).total_seconds() / 3600
            if not row.last_ok:
                problems.append(
                    {
                        "name": name,
                        "kind": "errored",
                        "max_hours": max_hours,
                        "row": row,
                        "hours_since": hours_since,
                    }
                )
            elif hours_since > max_hours:
                problems.append(
                    {
                        "name": name,
                        "kind": "overdue",
                        "max_hours": max_hours,
                        "row": row,
                        "hours_since": hours_since,
                    }
                )
            else:
                healthy += 1

        w("")
        w(f"watched crons : {len(EXPECTED_MAX_HOURS)}")
        w(f"healthy       : {healthy}")
        w(f"problems      : {len(problems)}")

        # Decide which problems are alertable (respect throttle + never-seen
        # opt-in). never-seen rows have no CronRun to throttle on, so they
        # alert every run when opted in — acceptable, they're rare + urgent.
        alertable: list[dict] = []
        for p in problems:
            kind = p["kind"]
            if kind == "never-seen" and not alert_never_seen:
                w(f"  [never-seen] {p['name']} — no heartbeat yet (not alerting)")
                continue
            row = p["row"]
            throttled = False
            if row is not None and row.last_alerted_at is not None:
                since_alert = (now - row.last_alerted_at).total_seconds() / 3600
                throttled = since_alert < throttle_hours
            hs = p["hours_since"]
            hs_str = f"{hs:.1f}h ago" if hs is not None else "never"
            tag = "throttled" if throttled else "ALERT"
            w(f"  [{kind}] {p['name']} — last {hs_str} "
              f"(cadence ≤{p['max_hours']}h) [{tag}]")
            if not throttled:
                alertable.append(p)

        if not alertable:
            w(self.style.SUCCESS("No new cron-health alerts."))
            return

        if dry:
            w(f"DRY-RUN — would alert on {len(alertable)} cron(s); "
              "no email sent, nothing stamped.")
            return

        sent = self._send_alert(alertable, now)
        if sent:
            for p in alertable:
                row = p["row"]
                if row is not None:
                    row.last_alerted_at = now
                    row.save(update_fields=["last_alerted_at"])
            w(self.style.SUCCESS(f"Alert emailed for {len(alertable)} cron(s)."))
        else:
            w("No Ignite recipients — alert not sent (nothing stamped).")

    # -- helpers -----------------------------------------------------------

    def _send_alert(self, alertable: list[dict], now) -> bool:
        """Email one digest of the problem crons to the Ignite team.
        Returns True if an email was dispatched."""
        import html as _html

        from tenants.support import _resolve_ignite_recipients
        from utils.mailer import Envelope, Mailer

        recipients = _resolve_ignite_recipients()
        if not recipients:
            return False

        def _row(p: dict) -> str:
            row = p["row"]
            hs = p["hours_since"]
            last_run = (
                f"{row.last_run_at:%a %m-%d %I:%M %p} UTC"
                if row is not None and row.last_run_at
                else "never"
            )
            hs_str = f"{hs:.1f}h ago" if hs is not None else "—"
            status = row.last_status if row is not None else "—"
            icon = {"errored": "🔴", "overdue": "🟠", "never-seen": "⚪"}.get(
                p["kind"], "⚠️"
            )
            return (
                "<tr>"
                f"<td>{icon}</td>"
                f"<td><b>{_html.escape(p['name'])}</b></td>"
                f"<td>{_html.escape(p['kind'])}</td>"
                f"<td>{_html.escape(last_run)}</td>"
                f"<td>{_html.escape(hs_str)}</td>"
                f"<td>≤{p['max_hours']}h</td>"
                f"<td>{_html.escape(str(status))}</td>"
                "</tr>"
            )

        table = "".join(_row(p) for p in alertable)
        n = len(alertable)
        body = f"""
        <h2 style="margin:0 0 8px">{n} scheduled job(s) need attention</h2>
        <p style="margin:0 0 12px;color:#555">
          These automations have gone <b>overdue</b> (older than their cadence),
          are <b>erroring</b> (last run returned a non-2xx status), or have never
          reported a heartbeat. A job that silently stops firing is exactly the
          failure that went unnoticed for weeks before — this is the alarm for it.
        </p>
        <table cellpadding="6" style="border-collapse:collapse" border="1">
          <tr>
            <th></th><th>Automation</th><th>Issue</th><th>Last run</th>
            <th>Age</th><th>Cadence</th><th>Last status</th>
          </tr>
          {table}
        </table>
        <p style="color:#888;margin-top:12px">
          Cron staleness watchdog · see the full heartbeat list on System Health
          → Automations. Re-alerts are throttled per job.
        </p>
        """
        subject = f"[Spark] {n} scheduled job(s) overdue or erroring"

        class _CronAlertMailer(Mailer):
            def envelope(self) -> Envelope:
                return Envelope(subject=subject, html=body, to_emails=recipients)

        _CronAlertMailer().send_now()
        return True
