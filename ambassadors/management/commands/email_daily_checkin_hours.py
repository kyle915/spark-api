"""Nightly clock-in/out summary for the field-ops crew.

Ignite reviews recaps after the fact rather than watching a live queue, so
nobody sees the day's hours until payroll. This puts the whole day — who
worked, where, in/out, how long — in one email at the end of it, while it's
still cheap to fix a missed clock-out or a BA at the wrong store.

Scope and the reasoning behind it:

  * ALL clock activity for the day, not just walk-ups. If someone worked, the
    crew wants to see it; filtering to one source would make the email quietly
    incomplete, which is worse than slightly long.
  * Grouped by brand then BA, because the first question is always "who was
    out for which client today".
  * Rows are ordered by clock-in, and anything still open or missing a punch
    is flagged rather than hidden — those are the rows that need action
    tonight.
  * Sends NOTHING on a day with no activity. A daily email that's usually
    empty is a daily email people stop opening.

"Today" is the local calendar day in ``--timezone`` (default America/Los_Angeles,
the house timezone — see the other daily crons), NOT UTC, or a 10pm PT run
would report a day that ended at 5pm.

Usage:
    python manage.py email_daily_checkin_hours                 # today, PT
    python manage.py email_daily_checkin_hours --date 2026-08-01
    python manage.py email_daily_checkin_hours --dry-run       # print, no send
"""
from __future__ import annotations

import datetime as _dt
import zoneinfo

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.utils.html import escape

DEFAULT_TZ = "America/Los_Angeles"


def _fmt_clock(dt, tz) -> str:
    if not dt:
        return "—"
    local = dt.astimezone(tz)
    return local.strftime("%-I:%M %p")


def _fmt_hours(delta) -> str:
    if delta is None:
        return "—"
    mins = int(delta.total_seconds() // 60)
    return f"{mins // 60}h {mins % 60:02d}m"


class Command(BaseCommand):
    help = (
        "Email the field-ops crew a clock-in/out summary for the day. "
        "Sends nothing when there was no activity."
    )

    def add_arguments(self, parser):
        parser.add_argument("--date", type=str, default="", help="YYYY-MM-DD (default: today)")
        parser.add_argument("--timezone", type=str, default=DEFAULT_TZ)
        parser.add_argument("--dry-run", action="store_true", help="Print, don't send.")

    def handle(self, *args, **opts):
        from ambassadors.models import Attendance

        try:
            tz = zoneinfo.ZoneInfo(opts["timezone"])
        except Exception as exc:  # noqa: BLE001
            raise CommandError(f"Bad --timezone {opts['timezone']!r}: {exc}")

        if opts["date"]:
            try:
                y, m, d = opts["date"].split("-")
                day = _dt.date(int(y), int(m), int(d))
            except Exception:
                raise CommandError("--date must be YYYY-MM-DD")
        else:
            day = _dt.datetime.now(tz).date()

        # Local midnight-to-midnight, converted to the UTC instants the DB
        # stores. Filtering on the raw UTC date would slice the day wrong for
        # every US zone.
        start = _dt.datetime.combine(day, _dt.time.min, tzinfo=tz)
        end = start + _dt.timedelta(days=1)

        punches = list(
            Attendance.objects.select_related(
                "ambassador__user", "event__tenant", "source"
            )
            .filter(clock_time__gte=start, clock_time__lt=end)
            .order_by("clock_time", "id")
        )
        if not punches:
            self.stdout.write("No clock activity — nothing to send.")
            self.stdout.write('JSON_RESULT:{"sent": false, "reason": "no_activity"}')
            return

        # Fold punches into one row per (BA, event): first in, last out.
        shifts: dict[tuple, dict] = {}
        for p in punches:
            amb = p.ambassador
            event = p.event
            key = (getattr(amb, "id", None), getattr(event, "id", None))
            row = shifts.setdefault(
                key,
                {
                    "ba": amb,
                    "event": event,
                    "brand": getattr(getattr(event, "tenant", None), "name", "") or "—",
                    "in": None,
                    "out": None,
                },
            )
            kind = (getattr(p.source, "name", "") or "").lower()
            if kind == "clock_in":
                if row["in"] is None or p.clock_time < row["in"]:
                    row["in"] = p.clock_time
            elif kind == "clock_out":
                if row["out"] is None or p.clock_time > row["out"]:
                    row["out"] = p.clock_time

        by_brand: dict[str, list] = {}
        total = _dt.timedelta()
        open_count = 0
        for row in shifts.values():
            if row["in"] and row["out"] and row["out"] > row["in"]:
                row["worked"] = row["out"] - row["in"]
                total += row["worked"]
            else:
                row["worked"] = None
                open_count += 1
            by_brand.setdefault(row["brand"], []).append(row)

        def _name(row) -> str:
            user = getattr(row["ba"], "user", None)
            if not user:
                return "Unknown BA"
            full = f"{user.first_name or ''} {user.last_name or ''}".strip()
            return full or (user.email or "Unknown BA")

        blocks = []
        for brand in sorted(by_brand):
            rows = sorted(by_brand[brand], key=lambda r: (r["in"] is None, r["in"] or start))
            cells = []
            for r in rows:
                flag = (
                    " <span style='color:#b45309'>(still open)</span>"
                    if r["in"] and not r["out"]
                    else " <span style='color:#b45309'>(no clock-in)</span>"
                    if not r["in"]
                    else ""
                )
                cells.append(
                    "<tr>"
                    f"<td style='padding:6px 10px 6px 0'>{escape(_name(r))}</td>"
                    f"<td style='padding:6px 10px 6px 0;color:#555'>"
                    f"{escape((getattr(r['event'], 'name', '') or '—')[:60])}</td>"
                    f"<td style='padding:6px 10px 6px 0;white-space:nowrap'>"
                    f"{_fmt_clock(r['in'], tz)} – {_fmt_clock(r['out'], tz)}{flag}</td>"
                    f"<td style='padding:6px 0;white-space:nowrap;font-weight:600'>"
                    f"{_fmt_hours(r['worked'])}</td>"
                    "</tr>"
                )
            blocks.append(
                f"<h3 style='margin:22px 0 6px;font-size:15px'>{escape(brand)}</h3>"
                "<table style='border-collapse:collapse;font-size:14px;width:100%'>"
                + "".join(cells)
                + "</table>"
            )

        open_note = (
            f"<p style='color:#b45309;margin:14px 0 0'>{open_count} shift(s) "
            "without a clean in/out pair — worth a look before payroll.</p>"
            if open_count
            else ""
        )
        html = (
            "<div style='font-family:system-ui,sans-serif;color:#14181a'>"
            f"<p style='font-size:15px;margin:0'><strong>{len(shifts)} shift(s)</strong> "
            f"on {day.strftime('%a, %b %-d')} — {_fmt_hours(total)} total.</p>"
            + "".join(blocks)
            + open_note
            + "</div>"
        )
        subject = f"Field hours — {day.strftime('%a, %b %-d')} ({len(shifts)} shifts)"

        to = [
            e.strip()
            for e in getattr(settings, "CHECKIN_HOURS_NOTIFY_EMAILS", [])
            if (e or "").strip()
        ]
        self.stdout.write(f"{len(shifts)} shift(s), {_fmt_hours(total)} total, {open_count} open")
        self.stdout.write(f"recipients: {to or '(none configured)'}")

        if opts["dry_run"] or not to:
            self.stdout.write(
                self.style.WARNING("DRY RUN / no recipients — not sending.\n")
            )
            self.stdout.write(
                f'JSON_RESULT:{{"sent": false, "shifts": {len(shifts)}, '
                f'"open": {open_count}}}'
            )
            return

        from utils.mailer import Envelope, Mailer

        class _HoursMailer(Mailer):
            def envelope(self) -> "Envelope":
                return Envelope(subject=subject, html=html, to_emails=to)

        _HoursMailer().send_now()
        self.stdout.write(self.style.SUCCESS(f"Sent to {len(to)} recipient(s)."))
        self.stdout.write(
            f'JSON_RESULT:{{"sent": true, "shifts": {len(shifts)}, '
            f'"open": {open_count}, "recipients": {len(to)}}}'
        )
