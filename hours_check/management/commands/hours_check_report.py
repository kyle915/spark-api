"""Read-only: how many shifts each hours-check rule flags for a period.

    python manage.py hours_check_report --start 2026-11-01 --end 2026-11-14
    python manage.py hours_check_report --start ... --end ... --tenant 17

Without --tenant it runs every client that had an approved booking in the
period. Writes nothing; prints counts per rule, so thresholds can be tuned
before staff see the flags.
"""

from __future__ import annotations

from collections import Counter
from datetime import date

from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = "Count hours-check flags per rule for a pay period (read-only)."

    def add_arguments(self, parser):
        parser.add_argument("--start", required=True)
        parser.add_argument("--end", required=True)
        parser.add_argument("--tenant", type=int, default=None)

    def handle(self, *args, **opts):
        from ambassadors.models import AmbassadorEvent
        from hours_check.service import run_check

        start = date.fromisoformat(opts["start"])
        end = date.fromisoformat(opts["end"])
        if opts["tenant"]:
            tenant_ids = [opts["tenant"]]
        else:
            tenant_ids = sorted(
                set(
                    AmbassadorEvent.objects.filter(
                        is_approved=True,
                        event__date__date__gte=start,
                        event__date__date__lte=end,
                    ).values_list("event__tenant_id", flat=True)
                )
            )
        total = flagged = 0
        per_rule: Counter = Counter()
        for tid in tenant_ids:
            rep = run_check(tid, start, end)
            total += rep.total_shifts
            flagged += rep.flagged_shifts
            for s in rep.shifts:
                for cf in s.flags:
                    per_rule[cf.flag.code] += 1
            self.stdout.write(
                f"tenant {tid}: {rep.flagged_shifts} of {rep.total_shifts} shifts flagged"
            )
        self.stdout.write(f"ALL: {flagged} of {total} shifts flagged")
        for code, n in per_rule.most_common():
            self.stdout.write(f"  {code:<24} {n}")
