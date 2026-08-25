"""List a tenant's requests in a date window — read-only, for identifying rows.

Written for "which Torch requests came in via the public form today?", which
matters because the Torch Sheet backfill takes explicit ids and will happily
append an admin-created or bulk-imported row if you hand it one. The sheet is
only supposed to carry public spark-form submissions, so picking the ids has to
be done with eyes open.

There is NO stored flag saying "this came from the public form" — the guard in
`utils.torch_public_form_sheet` keys off the slug passed at call time, which is
gone by the time you're looking at the row afterwards. So this prints the three
signals that actually distinguish them, and lets a human decide:

  created_by    public-form submissions have no authenticated creator
  requestor_email  the form collects it; admin creates usually don't
  created_at    when it was submitted, in the tenant's own local time

Scoped by SUBMISSION time (created_at), not event date — "submitted today" is
the question being asked.

Read-only. Nothing is written.

Usage::

    python manage.py list_tenant_requests --tenant-id 17 --days 1
    python manage.py list_tenant_requests --tenant-id 17 --since 2026-08-24
"""

from __future__ import annotations

import datetime as _dt

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone


class Command(BaseCommand):
    help = "List a tenant's requests by submission date (read-only)."

    def add_arguments(self, parser):
        parser.add_argument(
            "--tenant-id", dest="tenant_id", type=int, required=True,
            help="Tenant to list. Id only — names are ambiguous.",
        )
        parser.add_argument(
            "--days", type=int, default=None,
            help="Look back this many days from now (1 = today so far).",
        )
        parser.add_argument("--since", default="", help="YYYY-MM-DD (inclusive).")
        parser.add_argument("--until", default="", help="YYYY-MM-DD (inclusive).")
        parser.add_argument(
            "--public-only", dest="public_only", action="store_true",
            help="Only rows that look like public-form submissions "
                 "(no created_by, requestor_email present).",
        )

    # ------------------------------------------------------------------

    def handle(self, *args, **opts):
        from events.models import Request
        from tenants.models import Tenant

        tenant = Tenant.objects.filter(id=opts["tenant_id"]).first()
        if tenant is None:
            raise CommandError(f"No tenant with id={opts['tenant_id']}.")

        now = timezone.now()
        if opts["days"]:
            start = now - _dt.timedelta(days=opts["days"])
            end = None
        else:
            if not opts["since"]:
                raise CommandError("Pass --days or --since.")
            try:
                start = timezone.make_aware(
                    _dt.datetime.combine(
                        _dt.date.fromisoformat(opts["since"]), _dt.time.min
                    )
                )
                end = (
                    timezone.make_aware(
                        _dt.datetime.combine(
                            _dt.date.fromisoformat(opts["until"]), _dt.time.max
                        )
                    )
                    if opts["until"]
                    else None
                )
            except ValueError as exc:
                raise CommandError(f"Dates must be YYYY-MM-DD: {exc}") from exc

        qs = (
            Request.objects.filter(tenant_id=tenant.id, created_at__gte=start)
            .select_related("request_type", "created_by")
            .order_by("created_at")
        )
        if end:
            qs = qs.filter(created_at__lte=end)

        rows = list(qs)
        self.stdout.write("=" * 78)
        self.stdout.write(
            f"TENANT : [{tenant.id}] {tenant.name}\n"
            f"WINDOW : created_at >= {start:%Y-%m-%d %H:%M} UTC"
            + (f"  ..  {end:%Y-%m-%d %H:%M} UTC" if end else "  (to now)")
        )
        self.stdout.write("=" * 78)

        shown = 0
        for r in rows:
            creator = getattr(r.created_by, "email", None)
            looks_public = not creator and bool(r.requestor_email)
            if opts["public_only"] and not looks_public:
                continue
            shown += 1
            tag = "PUBLIC-FORM?" if looks_public else "admin/bulk"
            self.stdout.write("")
            self.stdout.write(
                f"  id={r.id}  {tag}  deleted={'YES' if r.deleted_at else 'no'}\n"
                f"     uuid          : {r.uuid}\n"
                f"     submitted_at  : {r.created_at:%Y-%m-%d %H:%M} UTC\n"
                f"     type          : {getattr(r.request_type, 'name', '-')}\n"
                f"     created_by    : {creator or '(none — unauthenticated)'}\n"
                f"     requestor     : {r.requestor_email or '-'}\n"
                f"     event date    : {r.date or '-'}\n"
                f"     address       : {(r.address or '')[:58]}"
            )

        self.stdout.write("")
        self.stdout.write("=" * 78)
        self.stdout.write(
            f"{shown} request(s) shown of {len(rows)} in window.\n"
            "PUBLIC-FORM? is a heuristic (no created_by + requestor_email set), "
            "not a stored flag — confirm before feeding ids to a Sheet backfill."
        )
        self.stdout.write("IDS: " + ",".join(
            str(r.id) for r in rows
            if (not getattr(r.created_by, "email", None) and r.requestor_email)
        ))
        self.stdout.write("=" * 78)
