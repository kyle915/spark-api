"""Provision (or re-provision) the Apple/Play review BA login.

App-store reviewers need a working BA account. This finds the user by
email — creating it if absent — sets the given password, marks it
active + verified (no forced password change), gives it the ambassador
role + an ACTIVE Ambassador profile, so a reviewer can log into the
mobile app and navigate a real BA experience.

Idempotent: safe to re-run; it converges the account to the desired
state and prints what changed (including the PRIOR role, since this may
repurpose an existing account — see PLAY-GPS / review notes).

    python manage.py seed_review_ambassador --email x@y.com --password 'pw'

Password is NEVER hard-coded or logged here; it's passed in at call
time (e.g. a one-off GH workflow_dispatch input).
"""

from __future__ import annotations

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

User = get_user_model()


class Command(BaseCommand):
    help = "Create/repair a BA login for app-store review (idempotent)."

    def add_arguments(self, parser):
        parser.add_argument("--email", required=True)
        parser.add_argument("--password", required=True)
        parser.add_argument(
            "--first-name", default="Kyle",
            help="Used only when the user is created fresh.",
        )

    def handle(self, *args, **opts):
        from ambassadors.models import Ambassador
        from tenants.models import Role

        email = opts["email"].strip().lower()
        password = opts["password"]
        if not password or len(password) < 6:
            raise CommandError("Password too short.")

        try:
            ambassador_role = Role.objects.get(slug=Role.AMBASSADOR_SLUG)
        except Role.DoesNotExist:
            raise CommandError("Ambassador role not found — seed roles first.")

        with transaction.atomic():
            user = User.objects.filter(email__iexact=email).first()
            created = False
            prior_role = None
            if user is None:
                user = User.objects.create(
                    username=email,
                    email=email,
                    first_name=opts["first_name"],
                    last_name="",
                    role=ambassador_role,
                    is_active=True,
                )
                created = True
            else:
                prior_role = getattr(
                    getattr(user, "role", None), "slug", None
                )

            user.set_password(password)
            user.is_active = True
            # Don't force a reviewer through a password-change wall.
            if hasattr(user, "requires_password_change"):
                user.requires_password_change = False
            # Make it a BA so the mobile BA queries resolve.
            user.role = ambassador_role
            user.save()

            # Email-verification gate off.
            try:
                from gqlauth.models import UserStatus

                UserStatus.objects.update_or_create(
                    user=user,
                    defaults={"verified": True, "archived": False},
                )
            except Exception as exc:  # noqa: BLE001 — non-fatal
                self.stdout.write(f"  [warn] UserStatus skipped: {exc}")

            amb, amb_created = Ambassador.objects.get_or_create(
                user=user,
                defaults={
                    "created_by": user,
                    "updated_by": user,
                    "is_active": True,
                },
            )
            if not amb_created and not amb.is_active:
                amb.is_active = True
                amb.save(update_fields=["is_active"])

        self.stdout.write(
            self.style.SUCCESS(
                f"Review BA ready: {email} "
                f"(user {'created' if created else 'updated'}"
                + (f", prior role '{prior_role}'" if prior_role else "")
                + f", ambassador profile {'created' if amb_created else 'active'})."
            )
        )
