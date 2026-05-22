"""
Diagnose: why does user X see "No companies associated with this account"?

Dumps everything TenantGuard's resolver cares about for a given email:
the User row, its is_staff/is_superuser flags, every TenantedUser row
(active and inactive), and the tenant names those point at.

Usage:

    python manage.py dump_user_tenancy kyle@igniteproductions.co
    python manage.py dump_user_tenancy kyle@igniteproductions.co ross@liquiddeath.com
"""

from __future__ import annotations

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand

from tenants.models import TenantedUser

User = get_user_model()


class Command(BaseCommand):
    help = "Dump a user's tenancy state (User row + TenantedUser rows + tenants)."

    def add_arguments(self, parser):
        parser.add_argument("emails", nargs="+", help="One or more emails to inspect.")

    def handle(self, *args, **opts):
        for email in opts["emails"]:
            self._dump_one(email)

    def _dump_one(self, email: str) -> None:
        self.stdout.write(self.style.MIGRATE_HEADING(f"\n=== {email} ==="))
        users = list(User.objects.filter(email__iexact=email))
        if not users:
            self.stdout.write(self.style.WARNING(
                "  NO USER ROW. (email__iexact returned nothing — case-insensitive.)"
            ))
            return
        if len(users) > 1:
            self.stdout.write(self.style.WARNING(
                f"  {len(users)} user rows match this email (dupes — bad). "
                "Dumping each:"
            ))
        for u in users:
            self.stdout.write(
                f"  User id={u.id} uuid={u.uuid} email={u.email} "
                f"is_active={u.is_active} is_staff={u.is_staff} "
                f"is_superuser={u.is_superuser}"
            )
            tus = list(TenantedUser.objects.filter(user=u).select_related("tenant", "role"))
            if not tus:
                self.stdout.write(self.style.WARNING(
                    "    No TenantedUser rows. This is why TenantGuard returns "
                    "'No companies' for them. They need a TenantedUser row "
                    "(is_active=True) for at least one tenant — OR the resolver "
                    "needs to bypass that filter for them (e.g. staff/superuser)."
                ))
                continue
            for tu in tus:
                role_name = tu.role.name if tu.role else "<no role>"
                tenant_name = tu.tenant.name if tu.tenant else "<no tenant>"
                tenant_slug = (
                    tu.tenant.tenant_slug if tu.tenant and hasattr(tu.tenant, "tenant_slug")
                    else "?"
                )
                self.stdout.write(
                    f"    TU id={tu.id} tenant='{tenant_name}' "
                    f"slug={tenant_slug} role={role_name} "
                    f"is_active={tu.is_active}"
                )
