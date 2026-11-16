"""Build "What consumers said" ahead of time, so nobody waits on the page.

The page groups consumer comments with a model call the first time a date
range is opened (and again whenever newly approved recaps change the
comments). This command does that work in advance for the ranges the
dashboard and the report offer, for every brand with recent approved recaps.
A range whose comments have not changed since the last build costs nothing:
``get_feedback_themes`` finds the stored row and returns.

Cron (a few times a day) → ``/internal/cron/prebuild-feedback-themes``.
Sends nothing to anyone.
"""

from __future__ import annotations

import time
from datetime import date, timedelta

from django.core.management.base import BaseCommand

# Days back from today for the 7d / 30d / 90d / 6-month ranges, plus the
# year to date. The page sends inclusive dates ending today.
RANGE_DAYS = (7, 30, 90, 182)


def ranges_for(today: date) -> list[tuple[date, date]]:
    out = [(today - timedelta(days=days), today) for days in RANGE_DAYS]
    out.append((date(today.year, 1, 1), today))
    return out


class Command(BaseCommand):
    help = "Build consumer feedback themes ahead of time for recent brands."

    def add_arguments(self, parser):
        parser.add_argument("--tenant", type=int, default=None)
        parser.add_argument("--days", type=int, default=120,
                            help="Only brands with an approved recap this recently.")
        parser.add_argument("--max-seconds", type=int, default=240)
        parser.add_argument("--dry-run", action="store_true")

    def handle(self, *args, **opts):
        from events.live_board import _ops_today
        from recaps.comment_insights import get_competitor_mentions, get_product_regions
        from recaps.feedback_themes import (
            MIN_COMMENTS,
            STATUS_READY,
            gather_comments,
            get_feedback_themes,
            source_hash,
        )
        from recaps.models import CustomRecap, Recap
        from tenants.models import TenantFeedbackThemes

        today = _ops_today()
        since = today - timedelta(days=opts["days"])
        deadline = time.monotonic() + max(10, opts["max_seconds"])

        if opts["tenant"]:
            tenant_ids = [opts["tenant"]]
        else:
            ids = set(
                Recap.objects.filter(approved=True, created_at__date__gte=since)
                .values_list("event__tenant_id", flat=True)
            ) | set(
                CustomRecap.objects.filter(approved=True, created_at__date__gte=since)
                .values_list("event__tenant_id", flat=True)
            )
            tenant_ids = sorted(i for i in ids if i)

        built = fresh = thin = failed = 0
        for tenant_id in tenant_ids:
            for start, end in ranges_for(today):
                if time.monotonic() > deadline:
                    self.stdout.write("time limit reached; the rest waits for the next run")
                    self._summary(built, fresh, thin, failed)
                    return
                comments, _cut = gather_comments(tenant_id, start, end)
                if len(comments) < MIN_COMMENTS:
                    thin += 1
                    continue
                if not opts["dry_run"]:
                    # The two cards under the themes read the same comments.
                    # Each stores its own result and asks the model only when
                    # the comments have changed, so this is cheap when fresh.
                    for build in (get_competitor_mentions, get_product_regions):
                        try:
                            build(tenant_id, start, end)
                        except Exception:  # noqa: BLE001 — one card must not stop the run
                            self.stderr.write(
                                f"{build.__name__} failed for tenant {tenant_id} {start} → {end}"
                            )
                if TenantFeedbackThemes.objects.filter(
                    tenant_id=tenant_id, source_hash=source_hash(comments)
                ).exists():
                    fresh += 1
                    continue
                if opts["dry_run"]:
                    built += 1
                    self.stdout.write(f"would build tenant {tenant_id} {start} → {end}")
                    continue
                result = get_feedback_themes(tenant_id, start, end)
                if result["status"] == STATUS_READY:
                    built += 1
                    self.stdout.write(f"built tenant {tenant_id} {start} → {end}")
                else:
                    failed += 1
                    self.stdout.write(
                        f"not built tenant {tenant_id} {start} → {end}: {result['status']}"
                    )
        self._summary(built, fresh, thin, failed)

    def _summary(self, built, fresh, thin, failed):
        self.stdout.write(
            f"feedback themes: built {built}, already up to date {fresh}, "
            f"too few comments {thin}, not built {failed}"
        )
