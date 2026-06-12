"""Mark a user's account verified + active so they can log in.

Spark logins go through gqlauth, which refuses an unverified account with
"Please verify your account." A client/RMM user created through the admin
"create client" flow can land unverified (verification email not
delivered / not clicked), leaving them stuck at the sign-in screen.

This finds the user BY EMAIL (it never creates one) and converges them to
a login-able state: `is_active=True` and gqlauth `UserStatus.verified=True`
(`archived=False`). It does NOT touch the user's role, password, or tenant
membership — so it's safe for any account type (client RMM, admin, BA) and
won't repurpose the account the way seed_review_ambassador does.

SAFE — DRY-RUN IS THE DEFAULT. Without --commit it only reports the current
state. Idempotent.

    python manage.py verify_user --email rmm@client.com --commit
"""

from __future__ import annotations

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

User = get_user_model()


class Command(BaseCommand):
    help = "Mark a user verified + active by email (dry-run by default)."

    def add_arguments(self, parser):
        parser.add_argument("--email", required=True)
        parser.add_argument(
            "--commit",
            action="store_true",
            help="Apply the change. Without this flag it's a dry-run.",
        )

    def handle(self, *args, **opts):
        email = (opts["email"] or "").strip().lower()
        commit = bool(opts["commit"])
        w = self.stdout.write

        user = User.objects.filter(email__iexact=email).order_by("id").first()
        if user is None:
            raise CommandError(
                f"No user with email {email!r}. (This command verifies an "
                "existing account; it never creates one.)"
            )

        # Read prior gqlauth status (if a row exists yet).
        prior_verified = None
        prior_archived = None
        try:
            from gqlauth.models import UserStatus

            status = UserStatus.objects.filter(user=user).first()
            if status is not None:
                prior_verified = status.verified
                prior_archived = status.archived
        except Exception as exc:  # noqa: BLE001
            w(f"  [warn] couldn't read UserStatus: {exc}")

        w("")
        w(self.style.MIGRATE_HEADING(f"verify_user: {email}"))
        w(f"  mode        : {'COMMIT' if commit else 'DRY-RUN'}")
        w(f"  user id     : {user.id}")
        w(f"  role        : {getattr(getattr(user, 'role', None), 'slug', None)}")
        w(f"  is_active   : {user.is_active}  -> True")
        w(
            f"  verified    : {prior_verified}  -> True"
            + ("  (no UserStatus row yet)" if prior_verified is None else "")
        )
        w(f"  archived    : {prior_archived}  -> False")

        if not commit:
            w("")
            w(self.style.MIGRATE_LABEL("DRY-RUN — no change. Re-run with --commit."))
            return

        with transaction.atomic():
            if not user.is_active:
                user.is_active = True
                user.save(update_fields=["is_active"])
            try:
                from gqlauth.models import UserStatus

                UserStatus.objects.update_or_create(
                    user=user,
                    defaults={"verified": True, "archived": False},
                )
            except Exception as exc:  # noqa: BLE001
                raise CommandError(f"Failed to set UserStatus: {exc}")

        w("")
        w(self.style.SUCCESS(f"Verified + active: {email} can now sign in."))
