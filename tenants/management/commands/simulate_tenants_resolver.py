"""
Simulate what the clients-schema `tenants` resolver returns for a given
user (by email). Reproduces the exact filter logic so we can confirm:

    - Is the user is_staff/is_superuser? (post-#531 they get all tenants)
    - If not, which tenants does the membership filter return?
    - Are any TenantedUser rows is_active=False that would explain a miss?

Usage:
    python manage.py simulate_tenants_resolver kyle@igniteproductions.co
    python manage.py simulate_tenants_resolver ross@liquiddeath.com
"""

from __future__ import annotations

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand

from tenants.models import Tenant

User = get_user_model()


class Command(BaseCommand):
    help = "Simulate the clients-schema tenants resolver for a user."

    def add_arguments(self, parser):
        parser.add_argument("emails", nargs="+")

    def handle(self, *args, **opts):
        for email in opts["emails"]:
            self._sim(email)

    def _sim(self, email: str) -> None:
        self.stdout.write(self.style.MIGRATE_HEADING(f"\n=== {email} ==="))
        try:
            user = User.objects.get(email__iexact=email)
        except User.DoesNotExist:
            self.stdout.write(self.style.ERROR(f"  No user with email {email}"))
            return
        except User.MultipleObjectsReturned:
            self.stdout.write(self.style.ERROR(
                f"  Multiple users match {email}; refusing to simulate."
            ))
            return

        self.stdout.write(
            f"  user.id={user.id} uuid={user.uuid} "
            f"is_active={user.is_active} is_staff={user.is_staff} "
            f"is_superuser={user.is_superuser}"
        )

        # === The exact logic from tenants/schema.py:633 post-#531 ===
        if user.is_staff or user.is_superuser:
            qs = Tenant.objects.all().distinct()
            branch = "staff/super → Tenant.objects.all()"
        else:
            qs = Tenant.objects.filter(
                tenanted_users__is_active=True,
                tenanted_users__user__uuid=str(user.uuid),  # mimic strawberry.ID
            ).distinct()
            branch = (
                "non-staff → filter(tenanted_users__is_active=True, "
                f"tenanted_users__user__uuid='{user.uuid}')"
            )

        results = list(qs.order_by("id").values("id", "name"))
        self.stdout.write(f"  branch: {branch}")
        self.stdout.write(f"  returns {len(results)} tenant(s):")
        for t in results:
            self.stdout.write(f"    - id={t['id']}  name='{t['name']}'")

        # === Also simulate the "no user_uuid passed" fallback ===
        if not (user.is_staff or user.is_superuser):
            qs2 = Tenant.objects.filter(
                tenanted_users__is_active=True,
                tenanted_users__user=user,
            ).distinct()
            results2 = list(qs2.order_by("id").values("id", "name"))
            self.stdout.write(
                f"  fallback branch (no userUuid) returns {len(results2)} tenant(s):"
            )
            for t in results2:
                self.stdout.write(f"    - id={t['id']}  name='{t['name']}'")
