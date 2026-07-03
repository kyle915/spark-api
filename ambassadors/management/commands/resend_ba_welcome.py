"""Re-send the "Welcome to Spark by Ignite" email to an EXISTING user.

The admin Add-a-BA flow only emails brand-new accounts — a BA whose user
already existed (created long ago, another program, etc.) gets booked but
never receives credentials or the app-download buttons. This resets them
onto the same rails as a fresh admin-created BA: new generated temp
password (requires_password_change on first sign-in), verified + active
user, active Ambassador profile, and the same welcome email.

DESTRUCTIVE to the existing password — the dry-run prints last_login and
whether a usable password exists so the operator can spot an actively
used account before overwriting it.

Dry-run by default; --apply writes + sends. Prod: ResendBaWelcomeView
(/internal/cron/resend-ba-welcome) + the resend-ba-welcome workflow.
"""

from __future__ import annotations

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError

User = get_user_model()


class Command(BaseCommand):
    help = (
        "Reset an existing user onto admin-created-BA rails (temp password, "
        "verified, active BA profile) and re-send the welcome/app email. "
        "Dry-run by default."
    )

    def add_arguments(self, parser):
        parser.add_argument("--email", required=True, help="The BA's email.")
        parser.add_argument(
            "--apply",
            action="store_true",
            help="Actually reset the password + send. Dry-run without it.",
        )

    def handle(self, *args, **opts):
        from ambassadors.models import Ambassador

        email = (opts["email"] or "").strip()

        # Diagnostic search mode: a fragment with no "@" lists every account
        # whose email OR name contains it (read-only, ignores --apply) — for
        # hunting duplicate accounts ("I *am* signed in with that email").
        if "@" not in email:
            from django.db.models import Q

            from ambassadors.models import AmbassadorEvent

            matches = list(
                User.objects.filter(
                    Q(email__icontains=email)
                    | Q(first_name__icontains=email)
                    | Q(last_name__icontains=email)
                ).order_by("id")[:20]
            )
            self.stdout.write(f"search {email!r}: {len(matches)} account(s)")
            for u in matches:
                amb = Ambassador.objects.filter(user=u).first()
                bookings = (
                    AmbassadorEvent.objects.filter(
                        ambassador=amb, is_approved=True
                    ).count()
                    if amb else 0
                )
                self.stdout.write(
                    f"  [{u.id}] {u.email!r} ({u.first_name} {u.last_name or ''}) "
                    f"last_login={u.last_login or 'NEVER'} "
                    f"ba={'yes' if amb else 'no'} bookings={bookings}"
                )
            return

        user = User.objects.filter(email__iexact=email).order_by("id").first()
        if user is None:
            raise CommandError(f"User not found: {email}")
        amb = Ambassador.objects.filter(user=user).first()

        w = self.stdout.write
        w("")
        w(f"user        : {user.id} {user.email} ({user.first_name} {user.last_name or ''})".rstrip())
        w(f"last_login  : {user.last_login or 'NEVER'}")
        w(f"is_active   : {user.is_active} · usable password: {user.has_usable_password()}")
        w(f"ba profile  : {'active' if (amb and amb.is_active) else ('inactive' if amb else 'MISSING')}")

        if not opts["apply"]:
            w(self.style.MIGRATE_LABEL(
                "DRY-RUN — nothing changed, nothing sent. Re-run with --apply "
                "(execute=true) to reset the password + send the welcome email."
            ))
            return

        from ambassadors.services import reset_ba_welcome_and_email

        msg = reset_ba_welcome_and_email(user.email)
        w(self.style.SUCCESS(msg))
