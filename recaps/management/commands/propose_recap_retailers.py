"""Suggest a retailer for events whose recaps would report as "Unknown".

Looks at events that have a filed recap, no retailer, and no suggestion yet,
and stores a pending suggestion for staff to accept while they review the
recap. Changes no event, recap or retailer. Safe to re-run: an event is looked
at once (a "nothing found" result is remembered too).

    python manage.py propose_recap_retailers --dry-run
    python manage.py propose_recap_retailers --tenant 17 --days 365 --limit 500
"""

from datetime import timedelta

from django.core.management.base import BaseCommand
from django.db.models import Exists, OuterRef, Q
from django.utils import timezone

from events.models import Event
from recaps.filed import custom_filed_q, legacy_filed_q
from recaps.models import CustomRecap, Recap
from recaps.retailer_proposals import SOURCE_LABELS, places_status, propose_for_event


def events_needing_a_proposal(*, tenant_id=None, days=120):
    no_request_retailer = Q(request__isnull=True) | (
        Q(request__retailer__isnull=True)
        & (Q(request__retailer_name__isnull=True) | Q(request__retailer_name=""))
    )
    qs = (
        Event.objects.filter(retailer__isnull=True, retailer_proposal__isnull=True)
        .filter(no_request_retailer)
        .filter(Q(request__isnull=True) | Q(request__deleted_at__isnull=True))
        .filter(
            Exists(CustomRecap.objects.filter(custom_filed_q(), event_id=OuterRef("pk")))
            | Exists(Recap.objects.filter(legacy_filed_q(), event_id=OuterRef("pk")))
        )
        .select_related("request")
    )
    if tenant_id:
        qs = qs.filter(tenant_id=tenant_id)
    if days:
        since = timezone.now() - timedelta(days=days)
        qs = qs.filter(Q(date__gte=since) | Q(date__isnull=True, created_at__gte=since))
    return qs.order_by("-date", "-id")


class Command(BaseCommand):
    help = "Suggest retailers for recapped events that have none."

    def add_arguments(self, parser):
        parser.add_argument("--tenant", type=int, default=None)
        parser.add_argument("--days", type=int, default=120)
        parser.add_argument("--limit", type=int, default=150)
        parser.add_argument("--dry-run", action="store_true")
        parser.add_argument(
            "--no-network",
            action="store_true",
            help="Only match against text Spark already holds (no Maps, no AI).",
        )

    def handle(self, *args, **opts):
        dry = opts["dry_run"]
        if not opts["no_network"]:
            self.stdout.write(places_status())
        events = list(
            events_needing_a_proposal(tenant_id=opts["tenant"], days=opts["days"])[: opts["limit"]]
        )
        found = 0
        by_source: dict[str, int] = {}
        for event in events:
            try:
                result = propose_for_event(
                    event, use_network=not opts["no_network"], dry_run=dry
                )
            except Exception as exc:  # noqa: BLE001 — one bad event must not stop the run
                self.stderr.write(f"event {event.id}: {exc}")
                continue
            if result is None:
                continue
            found += 1
            name = getattr(result, "name", None) or result.proposed_name
            by_source[result.source] = by_source.get(result.source, 0) + 1
            self.stdout.write(
                f"event {event.id} (tenant {event.tenant_id}): {name} "
                f"[{result.confidence}, from {SOURCE_LABELS.get(result.source, result.source)}]"
            )
        verb = "would suggest" if dry else "suggested"
        self.stdout.write(
            f"{len(events)} events looked at, {verb} {found}, nothing found for "
            f"{len(events) - found}. By source: {by_source or '-'}"
        )
