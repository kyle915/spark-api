"""List tenants with the facts that explain why one might be invisible in the UI.

"I created a client but I don't see it" has two very different causes and the
UI can't tell you which:

  * the tenant was never created (the mutation failed, or the form didn't
    submit) — nothing to find; or
  * it exists but the viewer can't see it, which is a membership / access
    question, not a data question.

Printing the row alongside its member count and creator separates those in one
look. Read-only; safe to run against prod any time.

Usage:
    python manage.py list_tenants
    python manage.py list_tenants --search dude
    python manage.py list_tenants --search "dude wipes" --verbose
"""
from __future__ import annotations

from django.core.management.base import BaseCommand
from django.db.models import Count, Q


class Command(BaseCommand):
    help = "List tenants (name, slug, created, members) — read-only diagnostic."

    def add_arguments(self, parser):
        parser.add_argument(
            "--search", type=str, default="",
            help="Case-insensitive substring match on name or slug.",
        )
        parser.add_argument(
            "--verbose", action="store_true",
            help="Also show creator, request URL name and check-in code.",
        )

    def handle(self, *args, **opts):
        from tenants.models import Tenant

        qs = Tenant.objects.all()
        term = (opts["search"] or "").strip()
        if term:
            qs = qs.filter(Q(name__icontains=term) | Q(slug__icontains=term))
        qs = qs.annotate(members=Count("tenantedusers", distinct=True)).order_by("id")

        rows = list(qs)
        total = Tenant.objects.count()
        if term:
            self.stdout.write(f"{len(rows)} of {total} tenant(s) match {term!r}\n")
        else:
            self.stdout.write(f"{total} tenant(s)\n")

        if not rows:
            self.stdout.write(self.style.WARNING(
                "No match. The tenant does not exist — this is a CREATE "
                "problem, not a visibility one."
            ))
            self.stdout.write('JSON_RESULT:{"matched": 0}')
            return

        self.stdout.write(
            f"{'id':>4}  {'name':<28} {'slug':<26} {'created':<10} members"
        )
        for t in rows:
            created = getattr(t, "created_at", None)
            self.stdout.write(
                f"{t.id:>4}  {(t.name or '')[:28]:<28} "
                f"{(t.slug or '—')[:26]:<26} "
                f"{created.date().isoformat() if created else '—':<10} "
                f"{t.members}"
            )
            if opts["verbose"]:
                who = getattr(t, "created_by", None)
                self.stdout.write(
                    f"        created_by : {getattr(who, 'email', None) or '—'}\n"
                    f"        requestUrl : {getattr(t, 'request_url_name', None) or '—'}\n"
                    f"        checkinCode: {getattr(t, 'checkin_code', None) or '—'}"
                )
            # A tenant with no members is the classic "created it, can't see
            # it" shape: the row exists but nobody is attached to it.
            if t.members == 0:
                self.stdout.write(self.style.WARNING(
                    "        ^ NO MEMBERS — exists, but no user is attached."
                ))

        self.stdout.write(f'\nJSON_RESULT:{{"matched": {len(rows)}, "total": {total}}}')
