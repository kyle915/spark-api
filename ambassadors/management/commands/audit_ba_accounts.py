"""Audit BA account health: relay duplicates, per-person account sets,
tenant-less ambassadors, and a tenant's recap/clock-in vitals.

Built for the Feel Free week-one fallout. Answers, from live data:

- Which Apple Hide-My-Email relay accounts exist, and which are EMPTY
  duplicates (no bookings / recaps / mileage) that only exist because SSO
  used to auto-create on unmatched emails. ``--deactivate-empty-relay-dups
  --apply`` turns those off, so sign-in stops matching them (the SSO
  matcher skips inactive accounts) and the BA is nudged back to their
  invited email.
- ``--names "Alicia Archie,Rocio"`` — every account fragment-matching each
  person (the Rocio/Alicia duplicate diagnosis, fleet-wide).
- ``--tenant-slug feel-free`` — recap templates, recap counts, clock-in
  counts by date: "are recaps/clock-ins actually landing?"
- Tenant-less ambassador census (the "442 BAs without a tenant" question):
  how many are relay dups vs marketplace self-signups vs invited-but-idle.

Read-only by default; every section is individually try/caught so one bad
relation can't blank the whole report.
"""

from __future__ import annotations

from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = "Audit BA accounts: relay dups, per-name account sets, tenant-less census."

    def add_arguments(self, parser):
        parser.add_argument("--names", default="", help="Comma-separated name/email fragments.")
        parser.add_argument("--tenant-slug", default="", help="Tenant vitals (templates/recaps/clock-ins).")
        parser.add_argument(
            "--deactivate-empty-relay-dups",
            action="store_true",
            help="Deactivate relay accounts with no bookings/recaps/mileage.",
        )
        parser.add_argument(
            "--backfill-memberships",
            action="store_true",
            help="Create the missing TenantedUser for every booked BA (the "
            "historical version of the #893 assignment signal).",
        )
        parser.add_argument("--apply", action="store_true", help="Actually write (default dry-run).")

    def handle(self, *args, **opts):
        w = self.stdout.write

        def section(title, fn):
            w("")
            w(f"===== {title}")
            try:
                fn()
            except Exception as exc:  # noqa: BLE001 — report, keep going
                w(self.style.ERROR(f"  section failed: {exc!r}"))

        section("RELAY ACCOUNTS (@privaterelay.appleid.com)", self._relay)
        if opts["names"]:
            section("NAME SEARCH", lambda: self._names(opts["names"]))
        if opts["tenant_slug"]:
            section(f"TENANT VITALS: {opts['tenant_slug']}", lambda: self._tenant(opts["tenant_slug"]))
        section("TENANT-LESS AMBASSADOR CENSUS", self._tenantless)
        section("BACKEND ERRORS (last 24h)", self._errors)
        if opts["deactivate_empty_relay_dups"]:
            section("DEACTIVATE EMPTY RELAY DUPS", lambda: self._deactivate(opts["apply"]))
        if opts["backfill_memberships"]:
            section("BACKFILL TENANT MEMBERSHIPS", lambda: self._backfill(opts["apply"]))

    # -- helpers -----------------------------------------------------------

    def _facts(self, user):
        """Booking/recap/mileage counts for one user's ambassador (if any)."""
        from ambassadors.models import Ambassador, AmbassadorEvent, MileageSession
        from recaps.models import CustomRecap, Recap

        amb = Ambassador.objects.filter(user=user).first()
        if amb is None:
            return None, 0, 0, 0
        bookings = AmbassadorEvent.objects.filter(ambassador=amb).count()
        recaps = (
            Recap.objects.filter(ambassador=amb).count()
            + CustomRecap.objects.filter(ambassador=amb).count()
        )
        miles = MileageSession.objects.filter(ambassador=amb).count()
        return amb, bookings, recaps, miles

    def _user_line(self, user):
        from tenants.models import TenantedUser

        amb, bookings, recaps, miles = self._facts(user)
        tenants = list(
            TenantedUser.objects.filter(user=user, is_active=True)
            .select_related("tenant")
            .values_list("tenant__name", flat=True)
        )
        login = user.last_login.strftime("%m-%d %H:%M") if user.last_login else "never"
        profile = "-"
        if amb is not None:
            bits = []
            if amb.phone:
                bits.append("phone")
            if amb.address:
                bits.append("addr")
            if amb.bio or amb.about_me:
                bits.append("bio")
            profile = "+".join(bits) or "empty"
        return (
            f"  u{user.id} {user.email} | {user.first_name} {user.last_name} | "
            f"active={user.is_active} login={login} | amb={'yes' if amb else 'NO'} "
            f"profile={profile} | bookings={bookings} recaps={recaps} miles={miles} | "
            f"tenants={tenants or '[]'}"
        )

    def _relay_qs(self):
        from django.contrib.auth import get_user_model

        return get_user_model().objects.filter(
            email__iendswith="privaterelay.appleid.com"
        )

    def _empty_relay(self):
        out = []
        for u in self._relay_qs():
            _, bookings, recaps, miles = self._facts(u)
            if bookings == 0 and recaps == 0 and miles == 0 and not u.is_staff:
                out.append(u)
        return out

    # -- sections ----------------------------------------------------------

    def _relay(self):
        w = self.stdout.write
        users = list(self._relay_qs())
        w(f"  total: {len(users)}")
        for u in users:
            w(self._user_line(u))
        empty = self._empty_relay()
        w(f"  EMPTY dups (no bookings/recaps/mileage, deactivatable): "
          f"{[u.id for u in empty if u.is_active]}")

    def _names(self, raw):
        from django.contrib.auth import get_user_model
        from django.db.models import Q

        User = get_user_model()
        w = self.stdout.write
        for frag in [f.strip() for f in raw.split(",") if f.strip()]:
            w(f"  -- '{frag}'")
            parts = frag.split()
            q = Q()
            for p in parts:
                q &= (
                    Q(first_name__icontains=p)
                    | Q(last_name__icontains=p)
                    | Q(email__icontains=p)
                )
            for u in User.objects.filter(q)[:10]:
                w(self._user_line(u))

    def _tenant(self, slug):
        import datetime

        from django.utils import timezone

        from ambassadors.models import Attendance
        from recaps.models import CustomRecap, CustomRecapTemplate, Recap
        from tenants.models import Tenant

        w = self.stdout.write
        tenant = Tenant.objects.filter(slug=slug).first()
        if tenant is None:
            w(f"  no tenant with slug {slug!r}")
            return
        for t in CustomRecapTemplate.objects.filter(tenant=tenant):
            w(f"  template id={t.id} '{t.name}' event_type={t.event_type_id}")
        customs = CustomRecap.objects.filter(event__tenant=tenant)
        legacy = Recap.objects.filter(event__tenant=tenant)
        w(f"  custom recaps: {customs.count()} | legacy recaps: {legacy.count()}")
        for r in customs.order_by("-id")[:5]:
            amb = r.ambassador
            who = f"{amb.user.first_name} {amb.user.last_name}" if amb and amb.user else "?"
            w(f"    custom #{r.id} event={getattr(r.event, 'name', '?')} by {who}")
        since = timezone.now() - datetime.timedelta(days=7)
        atts = (
            Attendance.objects.filter(
                event__tenant=tenant, source__name="clock_in", clock_time__gte=since
            )
            .select_related("ambassador__user")
            .order_by("clock_time")
        )
        w(f"  clock-ins last 7d: {atts.count()}")
        for a in atts:
            who = (
                f"{a.ambassador.user.first_name} {a.ambassador.user.last_name}"
                if a.ambassador and a.ambassador.user
                else "?"
            )
            w(f"    {a.clock_time:%m-%d %H:%M} {who} @ {getattr(a.event, 'name', '?')}")

    def _tenantless(self):
        from ambassadors.models import Ambassador, AmbassadorEvent

        w = self.stdout.write
        from tenants.models import TenantedUser

        member_user_ids = set(
            TenantedUser.objects.filter(is_active=True).values_list("user_id", flat=True)
        )
        ambs = Ambassador.objects.filter(user__isnull=False).select_related("user")
        orphans = [a for a in ambs if a.user_id not in member_user_ids]
        w(f"  ambassadors with no active tenant membership: {len(orphans)}")
        relay = [a for a in orphans if a.user.email.endswith("privaterelay.appleid.com")]
        signed_in = [a for a in orphans if a.user.last_login]
        booked = [
            a for a in orphans
            if AmbassadorEvent.objects.filter(ambassador=a).exists()
        ]
        w(f"    relay emails: {len(relay)}")
        w(f"    ever signed in: {len(signed_in)}")
        w(f"    with bookings (should be 0 after #893): {len(booked)}")
        w("    → the rest are marketplace/self-signups or invited-but-idle: "
          f"{len(orphans) - len(relay)}")

    def _errors(self):
        import datetime

        from django.utils import timezone

        from digest.models import BackendErrorEvent

        w = self.stdout.write
        since = timezone.now() - datetime.timedelta(hours=24)
        rows = BackendErrorEvent.objects.filter(last_seen__gte=since).order_by("-last_seen")[:10]
        if not rows:
            w("  none recorded")
        for r in rows:
            w(f"  ×{r.count} {r.signature} | last {r.last_seen:%m-%d %H:%M} | {r.message[:110]}")

    def _deactivate(self, apply: bool):
        w = self.stdout.write
        targets = [u for u in self._empty_relay() if u.is_active]
        for u in targets:
            w(self._user_line(u))
        if not targets:
            w("  nothing to deactivate")
            return
        if not apply:
            w(f"  DRY-RUN — would deactivate {len(targets)} account(s). Re-run with --apply.")
            return
        for u in targets:
            u.is_active = False
            u.save(update_fields=["is_active"])
        w(self.style.SUCCESS(f"  deactivated {len(targets)} empty relay duplicate(s)."))

    def _backfill(self, apply: bool):
        """Give every booked BA the tenant membership their bookings imply.

        PR #893 ensures this on NEW assignments; this closes the historical
        gap (422 booked-but-rosterless BAs found 2026-07-03). Never touches
        existing rows — including deliberately-deactivated memberships,
        which get_or_create leaves inactive.
        """
        from collections import Counter

        from ambassadors.models import AmbassadorEvent
        from tenants.models import TenantedUser

        w = self.stdout.write
        pairs = set(
            AmbassadorEvent.objects.filter(
                ambassador__user__isnull=False, event__tenant__isnull=False
            ).values_list("ambassador__user_id", "event__tenant_id")
        )
        existing = set(TenantedUser.objects.values_list("user_id", "tenant_id"))
        missing = sorted(pairs - existing)
        w(f"  booked (user, tenant) pairs: {len(pairs)} | already members: "
          f"{len(pairs & existing)} | MISSING: {len(missing)}")
        per_tenant = Counter(t for _, t in missing)
        for tenant_id, n in per_tenant.most_common():
            w(f"    tenant {tenant_id}: +{n}")
        if not missing:
            return
        if not apply:
            w(f"  DRY-RUN — would create {len(missing)} membership(s). "
              "Re-run with apply=true.")
            return
        created = 0
        for user_id, tenant_id in missing:
            _, was_created = TenantedUser.objects.get_or_create(
                user_id=user_id, tenant_id=tenant_id,
                defaults={"is_active": True},
            )
            created += int(was_created)
        w(self.style.SUCCESS(f"  created {created} membership(s)."))
