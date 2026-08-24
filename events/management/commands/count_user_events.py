"""Count the events and requests attributed to one person, split by event type.

"How many retail samplings are mapped to <person>?" has no single answer,
because a person maps to work through FOUR different columns and they do not
agree:

  Event.rmm_asigned       the RMM on the executed event
  Request.rmm_asigned     the RMM on the request that produced it
  Request.requestor_email free text — who submitted it (may not be a user)
  Request.created_by      the account that keyed it in

Reporting one of these and calling it "the" number is how you end up confidently
wrong. So this prints all four, broken out by event type, and lets the reader
pick the definition they meant.

Soft-deleted requests are counted SEPARATELY rather than silently included or
dropped: `deleteRequest` only sets `Request.deleted_at`, and the dashboard KPIs
exclude those, so a total that quietly includes them won't reconcile against
what the client sees.

Read-only. Nothing is written.

Usage::

    python manage.py count_user_events --name "Lauren Giaccio"
    python manage.py count_user_events --name "Lauren Giaccio" --tenant liquid-death
    python manage.py count_user_events --user-id 2 --event-type "Retail Sampling"
"""

from __future__ import annotations

from collections import Counter

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.db.models import Q

User = get_user_model()


class Command(BaseCommand):
    help = (
        "Count events/requests attributed to a person, by event type, across "
        "every attribution column (read-only)."
    )

    def add_arguments(self, parser):
        parser.add_argument("--name", default="", help="Name/email fragment.")
        parser.add_argument(
            "--user-id", dest="user_id", type=int, default=None,
            help="Exact user id. Preferred when the name is ambiguous.",
        )
        parser.add_argument(
            "--tenant", default="", help="Tenant slug or name substring to scope to."
        )
        parser.add_argument(
            "--event-type", dest="event_type", default="",
            help="Only report this event type (substring). Default: all.",
        )
        parser.add_argument(
            "--list", action="store_true",
            help="Also list the matching events (date, type, address).",
        )

    # ------------------------------------------------------------------

    def handle(self, *args, **opts):
        from events.models import Event, Request
        from tenants.models import Tenant

        users = self._resolve_users(opts["user_id"], opts["name"])

        tenant = None
        if opts["tenant"]:
            t = opts["tenant"].strip()
            tenant = Tenant.objects.filter(
                Q(slug__iexact=t) | Q(slug__icontains=t) | Q(name__icontains=t)
            ).order_by("id").first()
            if tenant is None:
                raise CommandError(f"No tenant matches {opts['tenant']!r}.")

        etype_needle = opts["event_type"].strip().lower()

        self.stdout.write("=" * 72)
        self.stdout.write(
            "EVENT ATTRIBUTION\n"
            f"TENANT : {f'[{tenant.id}] {tenant.name}' if tenant else '(all)'}\n"
            f"FILTER : {opts['event_type'] or '(all event types)'}"
        )
        self.stdout.write("=" * 72)

        for user in users:
            self.stdout.write("")
            self.stdout.write("-" * 72)
            self.stdout.write(
                f"u{user.id}  {user.email}  |  {user.first_name} {user.last_name}"
            )
            self.stdout.write("-" * 72)

            # 1) Events where they are the assigned RMM.
            ev = Event.objects.filter(rmm_asigned_id=user.id)
            if tenant:
                ev = ev.filter(tenant_id=tenant.id)
            self._report(
                "Events with rmm_asigned = this user",
                self._by_type(ev, "event_type__name"),
                etype_needle,
            )

            # 2) Requests where they are the assigned RMM. Split live vs
            #    soft-deleted, because the dashboard only counts the live ones.
            rq = Request.objects.filter(rmm_asigned_id=user.id)
            if tenant:
                rq = rq.filter(tenant_id=tenant.id)
            self._report(
                "Requests with rmm_asigned = this user (live)",
                self._by_type(rq.filter(deleted_at__isnull=True), "request_type__name"),
                etype_needle,
            )
            self._report(
                "Requests with rmm_asigned = this user (SOFT-DELETED)",
                self._by_type(rq.filter(deleted_at__isnull=False), "request_type__name"),
                etype_needle,
            )

            # 3) Requests they submitted, matched on the free-text email.
            if user.email:
                rqe = Request.objects.filter(
                    requestor_email__iexact=user.email, deleted_at__isnull=True
                )
                if tenant:
                    rqe = rqe.filter(tenant_id=tenant.id)
                self._report(
                    "Requests with requestor_email = this user (live)",
                    self._by_type(rqe, "request_type__name"),
                    etype_needle,
                )

            # 4) Requests keyed in under their account.
            rqc = Request.objects.filter(
                created_by_id=user.id, deleted_at__isnull=True
            )
            if tenant:
                rqc = rqc.filter(tenant_id=tenant.id)
            self._report(
                "Requests created_by = this user (live)",
                self._by_type(rqc, "request_type__name"),
                etype_needle,
            )

            if opts["list"]:
                self._list_events(ev, etype_needle)

        self.stdout.write("")
        self.stdout.write("=" * 72)
        self.stdout.write("Read-only — nothing was modified.")
        self.stdout.write("=" * 72)

    # ------------------------------------------------------------------

    def _resolve_users(self, user_id, name):
        if user_id:
            u = User.objects.filter(id=user_id).first()
            if u is None:
                raise CommandError(f"No user with id={user_id}.")
            return [u]
        needle = (name or "").strip()
        if not needle:
            raise CommandError("Pass --name or --user-id.")
        q = Q()
        for part in needle.split():
            q &= (
                Q(first_name__icontains=part)
                | Q(last_name__icontains=part)
                | Q(email__icontains=part)
            )
        users = list(User.objects.filter(q).order_by("id")[:10])
        if not users:
            raise CommandError(f"No user matches {needle!r}.")
        return users

    def _by_type(self, qs, field: str) -> Counter:
        c = Counter()
        for name in qs.values_list(field, flat=True):
            c[name or "(no type)"] += 1
        return c

    def _report(self, label: str, counts: Counter, needle: str) -> None:
        if needle:
            counts = Counter(
                {k: v for k, v in counts.items() if needle in (k or "").lower()}
            )
        total = sum(counts.values())
        self.stdout.write(f"\n  {label}: {total}")
        if not total:
            self.stdout.write("      (none)")
            return
        for name, n in counts.most_common():
            self.stdout.write(f"      {n:>5}  {name}")

    def _list_events(self, qs, needle: str) -> None:
        rows = qs.select_related("event_type").order_by("date")
        if needle:
            rows = rows.filter(event_type__name__icontains=needle)
        rows = list(rows[:200])
        self.stdout.write(f"\n  Event list ({len(rows)} shown):")
        for e in rows:
            self.stdout.write(
                f"      {str(getattr(e, 'date', '') or '?'):<12} "
                f"{(getattr(e.event_type, 'name', '') or '-')[:22]:<22} "
                f"{(e.address or '')[:46]}"
            )
