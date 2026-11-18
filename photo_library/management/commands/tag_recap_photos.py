"""Tag recap photos for the photo library.

Each photo is tagged once (what it shows, the brand's products in it, crowd
size, setting, quality) and again only when its file changes. Receipts are
set aside without a model call. Dry run by default: counts what would be
done, downloads nothing, calls no model and stores nothing. ``--apply``
tags and stores.

Cost is capped per run by ``--max-calls`` and ``--max-seconds``; whatever is
left waits for the next run.

Cron → ``/internal/cron/tag-recap-photos``. Sends nothing to anyone.
"""

from __future__ import annotations

import json

from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = "Tag recap photos for the searchable photo library (dry run unless --apply)."

    def add_arguments(self, parser):
        parser.add_argument("--tenant", type=int, default=None)
        parser.add_argument("--max-calls", type=int, default=100)
        parser.add_argument("--max-seconds", type=int, default=240)
        parser.add_argument("--max-files", type=int, default=400)
        parser.add_argument(
            "--apply", action="store_true",
            help="Call the model and store tags. Without it nothing is called or stored.",
        )

    def handle(self, *args, **opts):
        from photo_library.tagging import tag_pending_photos

        report = tag_pending_photos(
            dry_run=not opts["apply"],
            tenant_id=opts["tenant"],
            max_calls=opts["max_calls"],
            max_seconds=opts["max_seconds"],
            max_files=opts["max_files"],
        )
        prefix = "DRY RUN: " if report["dry_run"] else ""
        calls = report["would_call"] if report["dry_run"] else report["model_calls"]
        self.stdout.write(
            f"{prefix}photo library: {report['pending']} photos waiting, looked at "
            f"{report['looked_at']}, {'would call' if report['dry_run'] else 'called'} the model "
            f"{calls} times, copied {report['copied']}, set aside {report['excluded']} receipts "
            f"or screenshots, {report['unreadable']} unreadable, {report['errors']} errors"
            + (f", stopped: {report['stopped']}" if report["stopped"] else "")
        )
        self.stdout.write(json.dumps(report))
