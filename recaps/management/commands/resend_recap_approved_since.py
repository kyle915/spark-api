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

Girl Beer is never emailed: those rows are stamped ``client_notified_at``
without SMTP. Already-stamped rows are skipped so a full-window rerun
cannot re-spam.

Usage (after deploy, on the prod host):

    uv run python manage.py resend_recap_approved_since --since=2026-07-04 --dry-run
    uv run python manage.py resend_recap_approved_since --since=2026-07-04 --mark-girl-beer
    uv run python manage.py resend_recap_approved_since --since=2026-07-04 --kind=custom --after-id=406 --exclude-id=727 --html-only --limit=25
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

_SELECT_RELATED_CUSTOM = _SELECT_RELATED + ("tenant",)


def parse_since(value: str) -> datetime:
    try:
        day = datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError as exc:
        raise CommandError("--since must be YYYY-MM-DD") from exc
    return datetime.combine(day, time.min, tzinfo=ZoneInfo("America/Los_Angeles"))


def approved_recaps_since(
    since: datetime,
    *,
    recap_id: int | None = None,
    kind: str | None = None,
    after_id: int | None = None,
    through_id: int | None = None,
    exclude_ids: set[int] | None = None,
):
    """Yield (kind, recap) for approved rows whose updated_at is in-window."""
    from recaps import models

    since_utc = since.astimezone(ZoneInfo("UTC"))
    skip = exclude_ids or set()
    kinds = ("legacy", "custom") if not kind else (kind,)
    if "legacy" in kinds:
        qs = models.Recap.objects.filter(
            approved=True, updated_at__gte=since_utc
        ).select_related(*_SELECT_RELATED)
        if recap_id:
            qs = qs.filter(id=recap_id)
        if after_id:
            qs = qs.filter(id__gt=after_id)
        if through_id:
            qs = qs.filter(id__lte=through_id)
        for recap in qs.order_by("id"):
            if recap.id in skip:
                continue
            yield "legacy", recap
    if "custom" in kinds:
        qs = models.CustomRecap.objects.filter(
            approved=True, updated_at__gte=since_utc
        ).select_related(*_SELECT_RELATED_CUSTOM)
        if recap_id:
            qs = qs.filter(id=recap_id)
        if after_id:
            qs = qs.filter(id__gt=after_id)
        if through_id:
            qs = qs.filter(id__lte=through_id)
        for recap in qs.order_by("id"):
            if recap.id in skip:
                continue
            yield "custom", recap


def _tenant_label(recap) -> str:
    from recaps.mutation_parts.notify import recap_tenant

    tenant = recap_tenant(recap)
    if tenant is None:
        return "-"
    return (getattr(tenant, "slug", None) or getattr(tenant, "name", None) or "-")


class Command(BaseCommand):
    help = (
        "Re-send client/RMM 'recap is ready' mail for recaps approved "
        "on or after --since (America/Los_Angeles). Girl Beer is marked "
        "sent without SMTP. Does not re-send Ignite internal alerts."
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
            help="Limit to legacy Recap or CustomRecap.",
        )
        parser.add_argument(
            "--html-only",
            action="store_true",
            help=(
                "Skip PDF generate/attach. Sends the same HTML + public "
                "link as the working recap #727 sample."
            ),
        )
        parser.add_argument(
            "--after-id",
            type=int,
            default=0,
            help="Exclusive lower bound (resume after this recap id).",
        )
        parser.add_argument(
            "--through-id",
            type=int,
            default=0,
            help="Inclusive upper bound (e.g. already-sent through #406).",
        )
        parser.add_argument(
            "--exclude-id",
            type=int,
            action="append",
            default=None,
            help="Skip this recap id. Repeatable.",
        )
        parser.add_argument(
            "--limit",
            type=int,
            default=0,
            help="Max recaps to send/mark in this invocation (Cloud Run batch).",
        )
        parser.add_argument(
            "--mark-girl-beer",
            action="store_true",
            help="Stamp Girl Beer client_notified_at without SMTP. No other sends.",
        )
        parser.add_argument(
            "--mark-notified",
            action="store_true",
            help=(
                "Stamp client_notified_at on selected non-Girl-Beer recaps "
                "without SMTP (ledger for already-sent #154-#406)."
            ),
        )
        parser.add_argument(
            "--force",
            action="store_true",
            help=(
                "Clear client_notified_at and re-send even if already stamped. "
                "Requires --recap-id so a window rerun cannot mass re-spam."
            ),
        )

    def handle(self, *args, **opts):
        from recaps.mutation_parts.notify import (
            _collect_recap_approved_recipients,
            _thread_recap_approved_notify,
            is_girl_beer_recap,
            stamp_client_notified,
        )

        since = parse_since(opts["since"])
        recap_id = opts["recap_id"] or None
        kind = opts["kind"] or None
        dry_run = opts["dry_run"]
        html_only = opts["html_only"]
        after_id = opts["after_id"] or None
        through_id = opts["through_id"] or None
        exclude_ids = set(opts["exclude_id"] or [])
        limit = opts["limit"] or None
        mark_girl_beer = opts["mark_girl_beer"]
        mark_notified = opts["mark_notified"]
        force = opts["force"]

        if force and not recap_id:
            raise CommandError("--force requires --recap-id to avoid mass re-spam")

        rows = list(
            approved_recaps_since(
                since,
                recap_id=recap_id,
                kind=kind,
                after_id=after_id,
                through_id=through_id,
                exclude_ids=exclude_ids,
            )
        )

        gb_rows = []
        already_rows = []
        send_rows = []
        unique_to: set[str] = set()
        force_cleared = 0

        for recap_kind, recap in rows:
            recipients, _reply = _collect_recap_approved_recipients(recap)
            if is_girl_beer_recap(recap):
                action = (
                    "skip-notified-gb"
                    if recap.client_notified_at
                    else "mark-girl-beer"
                )
                gb_rows.append((recap_kind, recap, action, len(recipients)))
                self.stdout.write(
                    f"{recap_kind} #{recap.id} recipients={len(recipients)} "
                    f"action={action} tenant={_tenant_label(recap)}"
                )
                continue
            if recap.client_notified_at and not force:
                already_rows.append((recap_kind, recap))
                self.stdout.write(
                    f"{recap_kind} #{recap.id} recipients={len(recipients)} "
                    f"action=skip-notified tenant={_tenant_label(recap)}"
                )
                continue
            action = "force-resend" if recap.client_notified_at and force else "send"
            if recap.client_notified_at and force and not dry_run:
                type(recap).objects.filter(pk=recap.pk).update(client_notified_at=None)
                recap.client_notified_at = None
                force_cleared += 1
            for email, _first in recipients:
                unique_to.add(email.lower())
            send_rows.append((recap_kind, recap, len(recipients)))
            self.stdout.write(
                f"{recap_kind} #{recap.id} recipients={len(recipients)} "
                f"action={action} tenant={_tenant_label(recap)} "
                f"updated_at={timezone.localtime(recap.updated_at).isoformat()}"
            )

        gb_to_stamp = [
            (k, r, a, n) for k, r, a, n in gb_rows if a == "mark-girl-beer"
        ]
        send_batch = send_rows[:limit] if limit else send_rows

        sent = 0
        failed = 0
        marked_gb = 0
        marked_sent = 0
        last_id = 0

        if not dry_run:
            # Always internally-mark Girl Beer in this selection so they
            # cannot be emailed on a later pass.
            for recap_kind, recap, _action, _n in gb_to_stamp:
                if stamp_client_notified(recap, reason="girl-beer"):
                    marked_gb += 1
                last_id = recap.id
            if mark_notified:
                for recap_kind, recap, _n in send_batch:
                    if stamp_client_notified(recap, reason="already-sent"):
                        marked_sent += 1
                    last_id = recap.id
            elif not mark_girl_beer:
                for recap_kind, recap, _n in send_batch:
                    try:
                        _thread_recap_approved_notify(
                            recap.id, recap_kind, html_only=html_only
                        )
                        sent += 1
                        last_id = recap.id
                    except Exception:
                        failed += 1
                        last_id = recap.id
                        logger.exception(
                            "resend_recap_approved_since failed for %s #%s",
                            recap_kind,
                            recap.id,
                        )

        summary = (
            f"recaps={len(rows)} send_candidates={len(send_rows)} "
            f"send_batch={len(send_batch)} unique_to={len(unique_to)} "
            f"girl_beer={len(gb_rows)} girl_beer_unmarked={len(gb_to_stamp)} "
            f"already_notified={len(already_rows)} "
            f"force_cleared={force_cleared} "
            f"sent={sent} failed={failed} marked_gb={marked_gb} "
            f"marked_sent={marked_sent} last_id={last_id} "
            f"dry_run={dry_run} html_only={html_only} force={force} "
            f"mark_girl_beer={mark_girl_beer} mark_notified={mark_notified} "
            f"after_id={after_id or 0} through_id={through_id or 0} "
            f"limit={limit or 0} since={since.isoformat()}"
        )
        self.stdout.write(self.style.SUCCESS(summary))
        logger.info("resend_recap_approved_since %s", summary)
