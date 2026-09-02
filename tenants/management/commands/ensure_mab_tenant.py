"""Create Mark Anthony Brands tenant if missing (name + slug).

DRY-RUN by default. Under --apply, creates ``Mark Anthony Brands`` with slug
``mark-anthony-brands`` when neither already exists.

Usage::

    python manage.py ensure_mab_tenant
    python manage.py ensure_mab_tenant --apply
"""

from __future__ import annotations

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.db.models import Q

from tenants.models import Tenant

User = get_user_model()

TENANT_NAME = "Mark Anthony Brands"
TENANT_SLUG = "mark-anthony-brands"


class Command(BaseCommand):
    help = (
        "Ensure Mark Anthony Brands tenant exists "
        f"({TENANT_NAME!r} / {TENANT_SLUG!r}). Dry-run default."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--apply",
            action="store_true",
            help="Actually create the tenant when missing.",
        )
        parser.add_argument(
            "--owner-email",
            dest="owner_email",
            default="",
            help="Optional creator email; defaults to first superuser / user.",
        )

    def handle(self, *args, **opts):
        apply = bool(opts["apply"])
        owner = None
        email = (opts.get("owner_email") or "").strip()
        if email:
            try:
                owner = User.objects.get(email__iexact=email)
            except User.DoesNotExist:
                raise CommandError(f"No user with email {email!r}.")
        else:
            owner = (
                User.objects.filter(is_superuser=True).order_by("id").first()
                or User.objects.order_by("id").first()
            )
        if owner is None:
            raise CommandError("No user available to own the tenant.")

        existing = list(
            Tenant.objects.filter(
                Q(slug__iexact=TENANT_SLUG) | Q(name__iexact=TENANT_NAME)
            )
            .distinct()
            .order_by("id")
        )
        if existing:
            for t in existing:
                self.stdout.write(
                    f"= already present [{t.id}] name={t.name!r} slug={t.slug!r}"
                )
            return

        if not apply:
            self.stdout.write(
                self.style.WARNING(
                    f"DRY-RUN — would create tenant {TENANT_NAME!r} "
                    f"slug={TENANT_SLUG!r} owned by {owner.email}."
                )
            )
            return

        tenant = Tenant.objects.create(
            name=TENANT_NAME,
            slug=TENANT_SLUG,
            created_by=owner,
        )
        self.stdout.write(
            self.style.SUCCESS(
                f"+ created [{tenant.id}] {tenant.name!r} slug={tenant.slug!r}"
            )
        )
