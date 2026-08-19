"""Clock out leftover open standing-link punches for one BA.

Walk-in events often have no scheduled end_time, so auto_clock_out_stale_shifts
never closes them. A BA then reopens FF-* and the 90-day session restores
Saturday.

Usage:
  python manage.py clear_leftover_checkin --phone 7372680041 --dry-run
  python manage.py clear_leftover_checkin --phone 7372680041 --tenant-code FF-YMMK3Q
"""
from __future__ import annotations

import re

from django.core.management.base import BaseCommand, CommandError
from django.db.models import Q


class Command(BaseCommand):
    help = (
        "Clock out leftover open check-in punches for a BA (does not delete recaps)."
    )

    def add_arguments(self, parser):
        parser.add_argument("--phone", default="", help="BA phone digits")
        parser.add_argument("--name", default="", help="Substring of first/last name")
        parser.add_argument(
            "--tenant-code",
            default="",
            help="Standing walk-up code, e.g. FF-YMMK3Q",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Print matches, write nothing.",
        )

    def handle(self, *args, **opts):
        from ambassadors import checkin_web
        from ambassadors.models import Ambassador, Attendance

        phone = re.sub(r"\D", "", opts["phone"] or "")
        name = (opts["name"] or "").strip()
        code = (opts["tenant_code"] or "").strip()
        dry = bool(opts["dry_run"])
        if not phone and not name:
            raise CommandError("Pass --phone and/or --name.")

        tenant = None
        if code:
            kind, target = checkin_web.resolve_checkin_target(code)
            if kind != "tenant":
                raise CommandError(f"Not a standing tenant code: {code}")
            tenant = target

        q = Q()
        if phone:
            q |= Q(phone__icontains=phone)
            q |= Q(user__email__iexact=f"checkin-{phone}@walkup.spark")
            q |= Q(user__username__icontains=phone)
        if name:
            parts = name.split()
            for part in parts:
                q |= Q(user__first_name__icontains=part)
                q |= Q(user__last_name__icontains=part)

        ambassadors = list(
            Ambassador.objects.select_related("user").filter(q).distinct()[:20]
        )
        if not ambassadors:
            self.stdout.write("No matching ambassadors.")
            return

        closed = 0
        for amb in ambassadors:
            who = f"{amb.user.first_name} {amb.user.last_name}".strip() or amb.user.email
            self.stdout.write(f"BA #{amb.id} {who} phone={amb.phone!r} email={amb.user.email}")
            atts = (
                Attendance.objects.filter(ambassador=amb, source__name="clock_in")
                .select_related("event", "event__tenant")
                .order_by("-clock_time")[:40]
            )
            seen: set[int] = set()
            for att in atts:
                ev = att.event
                if ev is None or ev.id in seen:
                    continue
                seen.add(ev.id)
                if tenant is not None and ev.tenant_id != tenant.id:
                    continue
                state = checkin_web.clock_state(
                    ambassador_id=amb.id, event_id=ev.id
                )
                cal = checkin_web.event_calendar_date(ev)
                self.stdout.write(
                    f"  event #{ev.id} {cal} {ev.name!r} clock={state['state']} "
                    f"in={state['clockInAt']} out={state['clockOutAt']}"
                )
                if state["state"] != "clocked_in":
                    continue
                if dry:
                    self.stdout.write("    [dry-run] would clock out")
                    closed += 1
                    continue
                result = checkin_web.abandon_open_clock(ambassador=amb, event=ev)
                self.stdout.write(f"    cleared clockedOut={result.get('clockedOut')}")
                if result.get("clockedOut"):
                    closed += 1

        self.stdout.write(f"Closed {closed} leftover open punch(es). dry_run={dry}")
