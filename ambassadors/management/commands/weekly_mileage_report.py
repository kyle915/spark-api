"""Weekly mileage reimbursement report — emailed CSV, Monday mornings.

Mileage tracking writes completed MileageSession rows (total_miles ×
snapshotted rate_per_mile = reimbursement_amount), but until now nothing
rolled them up for payroll. This aggregates the prior Monday–Sunday week
per tenant → per BA → per session, emails an HTML summary with a CSV
attachment, and stays silent when the week had no completed sessions.

Recipients: settings.MILEAGE_REPORT_EMAILS. Window override for reruns:
--week-ending YYYY-MM-DD (a Sunday). Dry-run prints the summary without
sending. Prod: WeeklyMileageReportView (/internal/cron/weekly-mileage-report)
+ the weekly-mileage-report workflow (Mondays 13:00 UTC + manual dispatch).
"""

from __future__ import annotations

import csv
import datetime
import io

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from ambassadors.models import MileageSession


class Command(BaseCommand):
    help = (
        "Email the prior Mon-Sun week's mileage reimbursement CSV per "
        "BA/tenant. Dry-run prints without sending."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--week-ending",
            default=None,
            help="Sunday (YYYY-MM-DD) closing the report week. Default: the "
            "most recent Sunday before today.",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Print the summary; don't send the email.",
        )

    def handle(self, *args, **opts):
        today = timezone.localdate()
        if opts["week_ending"]:
            try:
                week_end = datetime.date.fromisoformat(opts["week_ending"])
            except ValueError:
                raise CommandError(f"Bad --week-ending: {opts['week_ending']!r}")
            if week_end.weekday() != 6:
                raise CommandError("--week-ending must be a Sunday.")
        else:
            # Most recent Sunday strictly before today (so Monday runs
            # report on the week that just closed).
            week_end = today - datetime.timedelta(days=(today.weekday() + 1))
        week_start = week_end - datetime.timedelta(days=6)

        start_dt = timezone.make_aware(
            datetime.datetime.combine(week_start, datetime.time.min)
        )
        end_dt = timezone.make_aware(
            datetime.datetime.combine(week_end, datetime.time.max)
        )

        sessions = (
            MileageSession.objects.filter(
                status=MileageSession.STATUS_COMPLETED,
                ended_at__gte=start_dt,
                ended_at__lte=end_dt,
            )
            .select_related("tenant", "event", "ambassador__user")
            .order_by("tenant__name", "ambassador__user__last_name", "ended_at")
        )

        rows = []
        for s in sessions:
            user = getattr(s.ambassador, "user", None)
            rows.append({
                "tenant": getattr(s.tenant, "name", "") or "",
                "ambassador": (
                    f"{getattr(user, 'first_name', '')} "
                    f"{getattr(user, 'last_name', '') or ''}"
                ).strip() or getattr(user, "email", "?"),
                "email": getattr(user, "email", ""),
                "event": getattr(s.event, "name", "") or "",
                "date": s.ended_at.date().isoformat() if s.ended_at else "",
                "miles": float(s.total_miles or 0),
                "rate": float(s.rate_per_mile or 0),
                "amount": float(s.reimbursement_amount or 0),
            })

        w = self.stdout.write
        w("")
        w(f"week       : {week_start} → {week_end} (Mon-Sun)")
        w(f"sessions   : {len(rows)}")
        if not rows:
            w("Nothing to report — no email sent.")
            return

        # Per-BA rollup for the summary table.
        totals: dict[tuple[str, str], dict] = {}
        for r in rows:
            key = (r["tenant"], r["ambassador"])
            t = totals.setdefault(
                key, {"trips": 0, "miles": 0.0, "amount": 0.0, "email": r["email"]}
            )
            t["trips"] += 1
            t["miles"] += r["miles"]
            t["amount"] += r["amount"]

        grand = sum(t["amount"] for t in totals.values())
        for (tenant, ba), t in sorted(totals.items()):
            w(
                f"  {tenant} · {ba}: {t['trips']} trip(s), "
                f"{t['miles']:.1f} mi, ${t['amount']:.2f}"
            )
        w(f"TOTAL owed : ${grand:.2f}")

        if opts["dry_run"]:
            w("DRY-RUN — email not sent.")
            return

        self._send(week_start, week_end, rows, totals, grand)
        w(self.style.SUCCESS("Report emailed."))

    def _send(self, week_start, week_end, rows, totals, grand):
        import html as _html

        from django.conf import settings

        from utils.mailer import Envelope, Mailer

        recipients = list(getattr(settings, "MILEAGE_REPORT_EMAILS", []) or [])
        if not recipients:
            self.stdout.write(self.style.WARNING(
                "MILEAGE_REPORT_EMAILS is empty — nothing sent."
            ))
            return

        buf = io.StringIO()
        writer = csv.DictWriter(buf, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)
        csv_bytes = buf.getvalue().encode()

        table = "".join(
            f"<tr><td>{_html.escape(tenant)}</td><td>{_html.escape(ba)}</td>"
            f"<td align=right>{t['trips']}</td>"
            f"<td align=right>{t['miles']:.1f}</td>"
            f"<td align=right>${t['amount']:.2f}</td></tr>"
            for (tenant, ba), t in sorted(totals.items())
        )
        body = f"""
        <h2 style="margin:0 0 8px">Mileage reimbursements · {week_start} – {week_end}</h2>
        <table cellpadding="6" style="border-collapse:collapse" border="1">
          <tr><th>Client</th><th>Ambassador</th><th>Trips</th><th>Miles</th><th>Owed</th></tr>
          {table}
          <tr><td colspan="4" align="right"><b>Total</b></td>
              <td align="right"><b>${grand:.2f}</b></td></tr>
        </table>
        <p style="color:#888">Per-trip detail attached as CSV. Rates are the
        per-event snapshots taken when each trip was stopped.</p>
        """

        class _ReportMailer(Mailer):
            def envelope(self) -> Envelope:
                return Envelope(
                    subject=(
                        f"Weekly mileage: ${grand:.2f} owed "
                        f"({week_start} – {week_end})"
                    ),
                    html=body,
                    to_emails=recipients,
                    attachments=[{
                        "filename": f"mileage_{week_start}_{week_end}.csv",
                        "content": list(csv_bytes),
                        "content_type": "text/csv",
                    }],
                )

        _ReportMailer().send_now()
