"""Scoped find/replace across a tenant's event text fields.

For fixing a fact that was baked into imported text after the fact — e.g.
Feel Free's shift billing changed from 5.5h to 5h AFTER the import wrote
"(5.5h billable each)" into every event's notes (and job descriptions
copied it). Touches Event.notes, the parent Request.notes, and
Job.description for the matched events only.

Guarded: tenant + event-type scope, literal substring match (no regex),
dry-run by default with per-model match counts, bulk_update writes (no
signal fan-out).
"""

from __future__ import annotations

from django.core.management.base import BaseCommand, CommandError

from events.models import Event
from tenants.models import Tenant


class Command(BaseCommand):
    help = (
        "Literal find/replace in Event.notes + Request.notes + "
        "Job.description for one tenant/event-type. Dry-run by default."
    )

    def add_arguments(self, parser):
        parser.add_argument("--tenant-name", required=True)
        parser.add_argument("--event-type", required=True)
        parser.add_argument("--find", required=True, help="Literal substring.")
        parser.add_argument("--replace", required=True)
        parser.add_argument(
            "--apply", action="store_true",
            help="Actually write. Dry-run without it.",
        )

    def handle(self, *args, **opts):
        from events.models import Request
        from jobs.models import Job

        tenant = (
            Tenant.objects.filter(name__iexact=opts["tenant_name"].strip())
            .order_by("id")
            .first()
        )
        if not tenant:
            raise CommandError(f"Tenant not found: {opts['tenant_name']!r}")
        find, replace = opts["find"], opts["replace"]
        if not find:
            raise CommandError("--find must be non-empty")

        events = Event.objects.filter(
            tenant=tenant, event_type__name__iexact=opts["event_type"].strip()
        )
        event_ids = list(events.values_list("id", flat=True))
        request_ids = list(
            events.exclude(request_id=None).values_list("request_id", flat=True)
        )

        targets = [
            ("Event.notes", Event.objects.filter(id__in=event_ids,
                                                 notes__contains=find), "notes"),
            ("Request.notes", Request.objects.filter(id__in=request_ids,
                                                     notes__contains=find), "notes"),
            ("Job.description", Job.objects.filter(event_id__in=event_ids,
                                                   description__contains=find),
             "description"),
        ]

        w = self.stdout.write
        w("")
        w(f"tenant : {tenant.id} ({tenant.name}) · scope {len(event_ids)} event(s)")
        w(f"find   : {find!r}  →  replace: {replace!r}")
        total = 0
        for label, qs, field in targets:
            rows = list(qs)
            w(f"  {label}: {len(rows)} match(es)")
            total += len(rows)
            if opts["apply"] and rows:
                for r in rows:
                    setattr(r, field, getattr(r, field).replace(find, replace))
                type(rows[0]).objects.bulk_update(rows, [field], batch_size=500)

        if opts["apply"]:
            w(self.style.SUCCESS(f"updated {total} row(s)."))
        else:
            w(self.style.MIGRATE_LABEL(
                "DRY-RUN — nothing written. Re-run with --apply (execute=true)."
            ))
