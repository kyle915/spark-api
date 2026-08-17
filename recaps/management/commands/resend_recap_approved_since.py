"""
Re-send client/RMM "Your activation recap is ready" mail.

Window: ``approved=True`` and ``updated_at >= --since`` in
America/Los_Angeles. There is no ``approved_at`` column; approve()
always ``save()``s, so ``updated_at`` is the approval stamp.

Recipients match ``_notify_recap_approved_to_rmm_or_clients``: event RMM
+ tenant client-role users + Tenant.recap_recipient_emails + requestor.
One email per recipient per recap, same templates
(``recap_approved_notification`` / ``custom_recap_approved_notification``).
Public link is ``https://client.igniteproductions.co/r/:token``.

Does **not** re-send Ignite internal "recap submitted" or suspect-numbers
alerts.

Added to recover sends that crashed in the Cloud Tasks daemon-thread
fallback (``SynchronousOnlyOperation`` in ``utils.cloud_tasks:_safe``,
from 2026-08-15). Those sends never reached ``mailer.send()``, so this
command sends once for the window (no delivery ledger to skip).

Usage (after deploy, on the prod host):

    uv run python manage.py resend_recap_approved_since --since=2026-08-15 --dry-run
    uv run python manage.py resend_recap_approved_since --since=2026-08-15
"""

from __future__ import annotations

import logging
from datetime import datetime, time
from zoneinfo import ZoneInfo

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

logger = logging.getLogger(__name__)

_SELECT_RELATED = (
    "event",
    "event__tenant",
    "event__rmm_asigned",
    "event__timezone",
    "event__request",
    "event__request__created_by",
    "job",
    "retailer",
    "timezone",
    "ambassador",
    "ambassador__user",
)


def parse_since(value: str) -> datetime:
    try:
        day = datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError as exc:
        raise CommandError("--since must be YYYY-MM-DD") from exc
    return datetime.combine(day, time.min, tzinfo=ZoneInfo("America/Los_Angeles"))


def approved_recaps_since(since: datetime, *, recap_id: int | None = None, kind: str | None = None):
    """Yield (kind, recap) for approved rows whose updated_at is in-window."""
    from recaps import models

    since_utc = since.astimezone(ZoneInfo("UTC"))
    kinds = ("legacy", "custom") if not kind else (kind,)
    if "legacy" in kinds:
        qs = models.Recap.objects.filter(
            approved=True, updated_at__gte=since_utc
        ).select_related(*_SELECT_RELATED)
        if recap_id:
            qs = qs.filter(id=recap_id)
        for recap in qs.order_by("id"):
            yield "legacy", recap
    if "custom" in kinds:
        qs = models.CustomRecap.objects.filter(
            approved=True, updated_at__gte=since_utc
        ).select_related(*_SELECT_RELATED)
        if recap_id:
            qs = qs.filter(id=recap_id)
        for recap in qs.order_by("id"):
            yield "custom", recap


class Command(BaseCommand):
    help = (
        "Re-send client/RMM 'recap is ready' mail for recaps approved "
        "on or after --since (America/Los_Angeles). Does not re-send "
        "Ignite internal review/suspect-numbers alerts."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--since",
            required=True,
            help="Inclusive start date YYYY-MM-DD in America/Los_Angeles.",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Count recaps and unique To addresses; send nothing.",
        )
        parser.add_argument(
            "--recap-id",
            type=int,
            default=0,
            help="Optional single recap/custom-recap id.",
        )
        parser.add_argument(
            "--kind",
            choices=("legacy", "custom"),
            default="",
            help="Limit to legacy Recap or CustomRecap when using --recap-id.",
        )
        parser.add_argument(
            "--html-only",
            action="store_true",
            help=(
                "Skip PDF generate/attach. Sends the same HTML + public "
                "link as the working recap #727 sample."
            ),
        )

    def handle(self, *args, **opts):
        from recaps.mutation_parts.notify import (
            _collect_recap_approved_recipients,
            _thread_recap_approved_notify,
        )

        since = parse_since(opts["since"])
        recap_id = opts["recap_id"] or None
        kind = opts["kind"] or None
        dry_run = opts["dry_run"]
        html_only = opts["html_only"]

        rows = list(approved_recaps_since(since, recap_id=recap_id, kind=kind))
        unique_to: set[str] = set()
        sent = 0
        failed = 0

        for recap_kind, recap in rows:
            recipients, _reply = _collect_recap_approved_recipients(recap)
            for email, _first in recipients:
                unique_to.add(email.lower())
            self.stdout.write(
                f"{recap_kind} #{recap.id} recipients={len(recipients)} "
                f"updated_at={timezone.localtime(recap.updated_at).isoformat()}"
            )
            if dry_run:
                continue
            try:
                _thread_recap_approved_notify(
                    recap.id, recap_kind, html_only=html_only
                )
                sent += 1
            except Exception:
                failed += 1
                logger.exception(
                    "resend_recap_approved_since failed for %s #%s",
                    recap_kind,
                    recap.id,
                )

        summary = (
            f"recaps={len(rows)} unique_to={len(unique_to)} "
            f"sent={sent} failed={failed} dry_run={dry_run} "
            f"html_only={html_only} since={since.isoformat()}"
        )
        self.stdout.write(self.style.SUCCESS(summary))
        logger.info("resend_recap_approved_since %s", summary)
