"""
Bulk-reset passwords for every user whose email ends with a given domain.

This is intentionally noisy and dry-run-by-default — bulk credential
rewrites have meaningful blast radius. To actually mutate anything you
must pass --apply, and to use a password shorter than 8 chars you must
also pass --allow-weak (3-char passwords are blocked by default).

Usage:

    # See who would be affected (no DB writes)
    python manage.py reset_passwords_for_domain \
        --domain @liquiddeath.com

    # Actually reset every @liquiddeath.com user to "LD1"
    # (will refuse without --allow-weak because LD1 fails the length check)
    python manage.py reset_passwords_for_domain \
        --domain @liquiddeath.com \
        --password LD1 \
        --allow-weak \
        --apply

    # Safer alternative: clear the password and force a reset-on-next-login
    python manage.py reset_passwords_for_domain \
        --domain @liquiddeath.com \
        --mode unusable \
        --apply
"""

from __future__ import annotations

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

User = get_user_model()

MIN_PASSWORD_LEN = 8  # informational floor; --allow-weak bypasses


class Command(BaseCommand):
    help = "Bulk-reset passwords for users matching an email domain."

    def add_arguments(self, parser):
        parser.add_argument(
            "--domain",
            required=True,
            help='Email suffix to match, e.g. "@liquiddeath.com". '
            "Match is case-insensitive and uses iendswith.",
        )
        parser.add_argument(
            "--password",
            default=None,
            help='New password to set when --mode=static. Required for static mode.',
        )
        parser.add_argument(
            "--mode",
            choices=["static", "unusable"],
            default="static",
            help="static: set the same password on every matched user. "
            "unusable: call set_unusable_password() so the next login "
            "must go through password-reset / magic link.",
        )
        parser.add_argument(
            "--apply",
            action="store_true",
            help="Actually write to the DB. Without this flag the command "
            "prints what it would do and exits.",
        )
        parser.add_argument(
            "--allow-weak",
            action="store_true",
            help=f"Permit passwords shorter than {MIN_PASSWORD_LEN} chars. "
            "Required for anything like 'LD1'.",
        )
        parser.add_argument(
            "--include-inactive",
            action="store_true",
            help="Also reset is_active=False users. Default skips them.",
        )

    def handle(self, *args, **opts):
        domain: str = opts["domain"].strip()
        if not domain.startswith("@"):
            raise CommandError(
                "--domain must start with '@', e.g. --domain @liquiddeath.com"
            )

        mode: str = opts["mode"]
        new_password: str | None = opts["password"]
        apply_changes: bool = opts["apply"]
        allow_weak: bool = opts["allow_weak"]
        include_inactive: bool = opts["include_inactive"]

        if mode == "static":
            if not new_password:
                raise CommandError(
                    "--password is required when --mode=static. "
                    "Use --mode=unusable to clear passwords instead."
                )
            if len(new_password) < MIN_PASSWORD_LEN and not allow_weak:
                raise CommandError(
                    f"Password is {len(new_password)} chars; min is "
                    f"{MIN_PASSWORD_LEN}. Pass --allow-weak to override. "
                    "(Strongly recommended: pick something longer.)"
                )

        qs = User.objects.filter(email__iendswith=domain)
        if not include_inactive:
            qs = qs.filter(is_active=True)
        qs = qs.order_by("email")

        users = list(qs)
        if not users:
            self.stdout.write(self.style.WARNING(
                f"No users match {domain} (include_inactive={include_inactive})."
            ))
            return

        self.stdout.write(
            self.style.MIGRATE_HEADING(
                f"\n{len(users)} user(s) match {domain}:"
            )
        )
        for u in users:
            flags = []
            if not u.is_active:
                flags.append("inactive")
            if u.is_staff:
                flags.append("staff")
            if u.is_superuser:
                flags.append("super")
            flag_str = f"  [{', '.join(flags)}]" if flags else ""
            self.stdout.write(f"  - {u.email}{flag_str}")

        if mode == "static":
            self.stdout.write(self.style.MIGRATE_HEADING(
                f'\nMode: static  →  password = "{new_password}" '
                f"({len(new_password)} chars)"
            ))
            if len(new_password) < MIN_PASSWORD_LEN:
                self.stdout.write(self.style.WARNING(
                    f"  WARN: password is below the {MIN_PASSWORD_LEN}-char floor. "
                    "Anyone who knows the convention can log in as these users."
                ))
        else:
            self.stdout.write(self.style.MIGRATE_HEADING(
                "\nMode: unusable  →  set_unusable_password() on every match. "
                "Users will need to use 'forgot password' or a magic link to "
                "regain access."
            ))

        if not apply_changes:
            self.stdout.write(self.style.NOTICE(
                "\nDRY RUN — no DB writes. Re-run with --apply to commit."
            ))
            return

        # Real run from here down.
        updated = 0
        with transaction.atomic():
            for u in users:
                if mode == "static":
                    u.set_password(new_password)
                else:
                    u.set_unusable_password()
                u.save(update_fields=["password"])
                updated += 1

        self.stdout.write(self.style.SUCCESS(
            f"\nUpdated {updated} user(s)."
        ))
        self.stdout.write(self.style.WARNING(
            "Sessions tied to these users are NOT invalidated by this command. "
            "If a user was already logged in, their JWT keeps working until it "
            "expires. Rotate refresh tokens separately if that matters."
        ))
