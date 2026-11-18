"""Tag approved recaps' consumer comments with themes and other brands.

Reads only recaps that were never tagged or whose comments changed, newest
event first, several recaps per model call. Dry run by default: lists what
would be read and roughly how big the calls would be, calls no model and
stores nothing. ``--apply`` tags and stores.

Cost is capped per run by ``--max-calls`` (across all brands) and by
``--max-seconds``; whatever is left waits for the next run.

Cron → ``/internal/cron/tag-consumer-mentions``. Sends nothing to anyone.
"""

from __future__ import annotations

import time
from datetime import timedelta

from django.core.management.base import BaseCommand

# Stop starting calls with less than this left of the budget: one call can
# take up to the model timeout, and Cloud Run cuts a request at 300 s.
CALL_HEADROOM_SECONDS = 50


class Command(BaseCommand):
    help = "Tag consumer comments on approved recaps with themes and other brands."

    def add_arguments(self, parser):
        parser.add_argument("--tenant", type=int, default=None)
        parser.add_argument(
            "--days", type=int, default=120,
            help="Only brands with an approved recap created this recently.",
        )
        parser.add_argument(
            "--max-calls", type=int, default=40,
            help="Most model calls in this run, across all brands.",
        )
        parser.add_argument("--max-seconds", type=int, default=200)
        parser.add_argument(
            "--apply", action="store_true",
            help="Call the model and store tags. Without it nothing is called or stored.",
        )

    def handle(self, *args, **opts):
        from consumer_mentions.tagging import tag_tenant
        from events.live_board import _ops_today
        from recaps.models import CustomRecap, Recap
        from utils.ai_text import AiUnavailable

        dry_run = not opts["apply"]
        budget = max(1, opts["max_calls"])
        deadline = time.monotonic() + max(10, opts["max_seconds"]) - CALL_HEADROOM_SECONDS

        if opts["tenant"]:
            tenant_ids = [opts["tenant"]]
        else:
            since = _ops_today() - timedelta(days=opts["days"])
            ids = set(
                Recap.objects.filter(approved=True, created_at__date__gte=since)
                .values_list("event__tenant_id", flat=True)
            ) | set(
                CustomRecap.objects.filter(approved=True, created_at__date__gte=since)
                .values_list("tenant_id", flat=True)
            )
            tenant_ids = sorted(i for i in ids if i)

        calls = tagged = chars = 0
        self.stdout.write(f"{'DRY RUN: ' if dry_run else ''}{len(tenant_ids)} brands")
        for tenant_id in tenant_ids:
            if calls >= budget:
                self.stdout.write(f"reached {budget} calls; the rest waits for the next run")
                break
            if time.monotonic() > deadline:
                self.stdout.write("time limit reached; the rest waits for the next run")
                break
            try:
                result = tag_tenant(
                    tenant_id,
                    dry_run=dry_run,
                    max_calls=budget - calls,
                    deadline=deadline,
                )
            except AiUnavailable as exc:
                self.stderr.write(f"model not available: {exc}")
                break
            except Exception as exc:  # noqa: BLE001 — one brand must not stop the run
                self.stderr.write(f"tenant {tenant_id} failed: {exc}")
                continue
            calls += result.calls
            tagged += result.tagged
            chars += result.prompt_chars
            if result.recaps_with_comments:
                self.stdout.write(result.line(dry_run))
        self.stdout.write(
            f"consumer mentions: {'planned' if dry_run else 'made'} {calls} calls, "
            f"tagged {tagged} recaps, ~{chars // 4} input tokens"
        )
