"""
Backfill TenantedUser rows for admins who were created without any tenant
links — typically users seeded via a script or manual SQL who then can't
log in because the CompanySelector returns zero tenants and shows the
"No companies associated with this account" wall.

Usage:
    python manage.py backfill_admin_tenant_links
    python manage.py backfill_admin_tenant_links --email nevena@igniteproductions.co
    python manage.py backfill_admin_tenant_links --dry-run
"""

from django.core.management.base import BaseCommand
from django.db import transaction

from tenants.models import TenantedUser, Tenant

ROLE_ID_ADMIN = 2


class Command(BaseCommand):
    help = (
        "Link admin users to every tenant when they're missing active "
        "TenantedUser rows. Safe to re-run."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--email",
            type=str,
            default=None,
            help="Only backfill the admin matching this email (case-insensitive).",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Print what would change without writing.",
        )

    def handle(self, *args, **opts):
        from django.contrib.auth import get_user_model

        User = get_user_model()
        email = (opts.get("email") or "").strip().lower() or None
        dry = bool(opts.get("dry_run"))

        # Include inactive admins too — a previously-removed admin should
        # be re-linkable + re-activated by this command (delete_user soft-
        # deletes, and we want a one-shot way to undo it).
        qs = User.objects.filter(role_id=ROLE_ID_ADMIN)
        if email:
            qs = qs.filter(email__iexact=email)

        all_tenants = list(Tenant.objects.all())
        if not all_tenants:
            self.stdout.write(self.style.WARNING("No tenants exist — nothing to do."))
            return

        users = list(qs)
        if not users:
            self.stdout.write(self.style.WARNING("No matching admins."))
            return

        self.stdout.write(f"Checking {len(users)} admin user(s) against {len(all_tenants)} tenant(s).")

        total_created = 0
        total_reactivated = 0
        users_reactivated = 0
        affected_users = 0

        for u in users:
            active_tenant_ids = set(
                TenantedUser.objects.filter(user=u, is_active=True).values_list(
                    "tenant_id", flat=True
                )
            )
            missing = [t for t in all_tenants if t.id not in active_tenant_ids]
            user_inactive = not u.is_active
            if not missing and not user_inactive:
                continue

            affected_users += 1
            self.stdout.write(
                f"  • {u.email or u.username} (id={u.id}): "
                f"user_active={u.is_active} active_links={len(active_tenant_ids)} "
                f"missing={len(missing)}"
            )

            if dry:
                continue

            with transaction.atomic():
                if user_inactive:
                    u.is_active = True
                    u.save(update_fields=["is_active"])
                    users_reactivated += 1
                for t in missing:
                    obj, created = TenantedUser.objects.get_or_create(
                        user=u, tenant=t, defaults={"is_active": True},
                    )
                    if created:
                        total_created += 1
                    elif not obj.is_active:
                        obj.is_active = True
                        obj.save(update_fields=["is_active"])
                        total_reactivated += 1

        msg = (
            f"Done. users_affected={affected_users} "
            f"users_reactivated={users_reactivated} "
            f"links_created={total_created} links_reactivated={total_reactivated} "
            f"{'(DRY RUN)' if dry else ''}"
        )
        self.stdout.write(self.style.SUCCESS(msg))
