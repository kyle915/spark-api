"""
Find users by partial match on name or email. Dump enough state to
diagnose a "this client can't sign in" report cold.

Usage:
    python manage.py find_user lauren
    python manage.py find_user lauren --tenant "Liquid Death"
"""

from __future__ import annotations

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand
from django.db.models import Q

from tenants.models import TenantedUser

User = get_user_model()


class Command(BaseCommand):
    help = "Find users by partial name/email match and dump their tenancy + auth state."

    def add_arguments(self, parser):
        parser.add_argument(
            "needles",
            nargs="+",
            help="Strings to match against first_name / last_name / email / username.",
        )
        parser.add_argument(
            "--tenant",
            default=None,
            help="If set, only include users with a TenantedUser row pointing at this tenant name.",
        )

    def handle(self, *args, **opts):
        for needle in opts["needles"]:
            self._find(needle, opts.get("tenant"))

    def _find(self, needle: str, tenant_name: str | None) -> None:
        self.stdout.write(self.style.MIGRATE_HEADING(f"\n=== '{needle}' ==="))
        q = (
            Q(first_name__icontains=needle)
            | Q(last_name__icontains=needle)
            | Q(email__icontains=needle)
            | Q(username__icontains=needle)
        )
        qs = User.objects.filter(q).order_by("email")
        if tenant_name:
            qs = qs.filter(tenanted_users__tenant__name__icontains=tenant_name).distinct()

        users = list(qs)
        if not users:
            self.stdout.write(self.style.WARNING(f"  No users match '{needle}'."))
            return

        self.stdout.write(f"  {len(users)} match(es):")
        for u in users:
            usable = u.has_usable_password()
            last_login = (
                u.last_login.isoformat(timespec="seconds") if u.last_login else "never"
            )
            self.stdout.write(
                f"\n  --- {u.email} ---\n"
                f"    id={u.id}  uuid={u.uuid}\n"
                f"    name='{u.first_name} {u.last_name}'  username={u.username}\n"
                f"    is_active={u.is_active}  is_staff={u.is_staff}  is_super={u.is_superuser}\n"
                f"    has_usable_password={usable}\n"
                f"    last_login={last_login}\n"
                f"    date_joined={u.date_joined.isoformat(timespec='seconds')}"
            )
            tus = list(
                TenantedUser.objects.filter(user=u).select_related("tenant")
            )
            if not tus:
                self.stdout.write("    TenantedUser rows: (none)")
            else:
                self.stdout.write("    TenantedUser rows:")
                for tu in tus:
                    t = tu.tenant
                    slug = getattr(t, "tenant_slug", None) or "?"
                    self.stdout.write(
                        f"      - tu_id={tu.id}  tenant='{t.name}'  "
                        f"slug={slug}  is_active={tu.is_active}"
                    )
