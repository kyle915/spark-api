"""Broadcast a silent push telling every spark-mobile device to run its
background OTA-update check right now, instead of waiting for the device
to be naturally opened.

Meant to be run manually right after publishing a new OTA
(``npx eas-cli update --branch production ...``), so a fix lands on field
devices immediately instead of on next natural app-open. Dry-run by
default; pass --apply to actually send.

Uses the fresh-thread pattern (never asyncio.run() on the calling thread)
because this command is also reachable via a cron endpoint under ASGI —
see push.py::_send_push_to_user_sync's docstring for why that matters.
"""

from __future__ import annotations

import asyncio
import concurrent.futures

from django.core.management.base import BaseCommand

from ambassadors.models import PushDevice
from ambassadors.push import send_silent_update_check_push


class Command(BaseCommand):
    help = "Broadcast a silent push nudging every device to check for a new OTA update."

    def add_arguments(self, parser):
        parser.add_argument(
            "--apply",
            action="store_true",
            help="Actually send. Without this flag, only reports how many devices would be targeted.",
        )

    def handle(self, *args, **options):
        apply = options["apply"]

        total = PushDevice.objects.filter(is_active=True).count()
        if not total:
            self.stdout.write("No active push devices registered — nothing to send.")
            return

        if not apply:
            self.stdout.write(
                f"DRY RUN — would broadcast a silent update-check push to {total} active device(s). "
                "Pass --apply to send."
            )
            return

        def _run() -> tuple[int, int]:
            return asyncio.run(send_silent_update_check_push())

        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
            ok_count, total_count = executor.submit(_run).result(timeout=60)

        self.stdout.write(f"Sent: {ok_count}/{total_count} devices accepted the push.")
