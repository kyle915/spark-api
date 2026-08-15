"""Add a person to a tenant — client contact, admin, or BA — and optionally
email them a sign-in link.

The `inviteUser` GraphQL mutation already does this from the admin UI. This is
the same thing reachable from a workflow, for onboarding a client's contacts
without clicking through the UI once per person.

THE EMAIL IS OPT-IN HERE, and that is the one deliberate difference from the
mutation. `inviteUser` always sends; a management command run against prod
should not be able to email a real person at a client as a side effect of
someone reading a dry run wrong. So the account and the tenant link are one
step (`--apply`) and the email is a second, explicit one (`--send-invite`).
An account with no email sent is inert but harmless — the person simply can't
sign in until someone sends the link, which is a state you can undo by doing
nothing.

Everything below matches `tenants.mutations.invite_user` on purpose — the same
role map, the same unusable-password marker, the same MagicLinkMailer envelope
and 30-minute expiry — so the two paths can't drift into sending two different
emails or creating two different kinds of user.

Idempotent. An existing user keeps their role (a role someone already set is
not ours to overwrite), gains the tenant link if missing, and is reactivated if
they were soft-deleted — `delete_user` flips `is_active=False`, and magic-link
login hard-rejects inactive users, so without that a re-invite would look like
it worked and land the person on a wall.

Usage::

    python manage.py add_tenant_user --tenant-id 18 --email a@b.com \
        --first-name Lena --last-name Lewis --role client
    ... --apply                 # create the account + tenant link, no email
    ... --apply --send-invite   # and email them the sign-in link
"""

from __future__ import annotations

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core import signing
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils.crypto import get_random_string

from tenants.models import Tenant, TenantedUser

User = get_user_model()

# Same mapping the mutation uses. Kept as a literal rather than imported so a
# reader can see what "client" actually grants without opening another file.
ROLE_MAP = {"admin": 2, "spark-admin": 2, "client": 3, "ambassador": 1}


class Command(BaseCommand):
    help = (
        "Add a user to a tenant (client/admin/ambassador). Dry-run unless "
        "--apply; the invite email is separately opt-in via --send-invite."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--tenant-id",
            dest="tenant_id",
            type=int,
            default=None,
            help="Tenant to attach to. Required for client/ambassador.",
        )
        parser.add_argument("--email", required=True, help="Their email address.")
        parser.add_argument("--first-name", dest="first_name", default="")
        parser.add_argument("--last-name", dest="last_name", default="")
        parser.add_argument(
            "--role",
            default="client",
            choices=sorted(ROLE_MAP),
            help="client (default) sees their own tenant; admin sees all.",
        )
        parser.add_argument(
            "--send-invite",
            dest="send_invite",
            action="store_true",
            help="Email them a magic sign-in link. Requires --apply.",
        )
        parser.add_argument(
            "--apply",
            action="store_true",
            help="Actually write (omit for a dry run that changes nothing).",
        )

    # ------------------------------------------------------------------

    def handle(self, *args, **opts):
        apply = bool(opts["apply"])
        send_invite = bool(opts["send_invite"])
        email = (opts["email"] or "").strip().lower()
        role = opts["role"].strip().lower()
        role_id = ROLE_MAP[role]

        if not email or "@" not in email:
            raise CommandError(f"{opts['email']!r} is not an email address.")
        if send_invite and not apply:
            raise CommandError(
                "--send-invite needs --apply. Refusing to email a real person "
                "from a run that writes nothing."
            )

        tenant = None
        if opts["tenant_id"]:
            tenant = Tenant.objects.filter(id=opts["tenant_id"]).first()
            if tenant is None:
                raise CommandError(f"No tenant with id={opts['tenant_id']}.")
        elif role != "admin":
            raise CommandError(
                f"--tenant-id is required for role {role!r} — without it the "
                "user is created with no tenant link and lands on "
                '"No companies associated" at sign-in.'
            )

        existing = User.objects.filter(email__iexact=email).first()

        self.stdout.write("=" * 72)
        self.stdout.write(
            f"EMAIL  : {email}\n"
            f"NAME   : {opts['first_name']} {opts['last_name']}".rstrip() + "\n"
            f"ROLE   : {role} (role_id={role_id})\n"
            f"TENANT : "
            + (f"[{tenant.id}] {tenant.name!r}" if tenant else "(all — admin)")
            + "\n"
            f"EXISTS : "
            + (
                f"yes — id={existing.id}, active={existing.is_active}, "
                f"role_id={existing.role_id} (role NOT overwritten)"
                if existing
                else "no — will be created"
            )
            + "\n"
            f"INVITE : {'YES — email will be sent' if send_invite else 'no email'}\n"
            f"MODE   : {'APPLY (writing)' if apply else 'DRY-RUN (no writes)'}"
        )
        self.stdout.write("=" * 72)

        if tenant is not None and existing is not None:
            linked = TenantedUser.objects.filter(
                user=existing, tenant=tenant
            ).first()
            self.stdout.write(
                "\n  Tenant link: "
                + (
                    f"present (active={linked.is_active})"
                    if linked
                    else "MISSING — would be created"
                )
            )

        if not apply:
            self.stdout.write(
                "\nDRY-RUN — nothing written, no email sent. Re-run with "
                "--apply (and --send-invite to email the sign-in link)."
            )
            return

        user, created = self._create_or_get(
            email, opts["first_name"], opts["last_name"], role_id, tenant
        )
        self.stdout.write(
            f"\n  {'+ created' if created else '= existing'} user id={user.id} "
            f"{user.email}"
        )
        if tenant is not None:
            self.stdout.write(f"  = linked to [{tenant.id}] {tenant.name!r}")

        if not send_invite:
            self.stdout.write("")
            self.stdout.write("=" * 72)
            self.stdout.write(
                self.style.SUCCESS(
                    f"{email} now has {role} access to "
                    + (tenant.name if tenant else "all tenants")
                    + "."
                )
            )
            self.stdout.write(
                "No email sent. They cannot sign in until one is — re-run with "
                "--send-invite, or use the Invite button in the admin UI."
            )
            self.stdout.write("=" * 72)
            return

        self._send_invite(user)

    # ------------------------------------------------------------------

    def _create_or_get(self, email, first_name, last_name, role_id, tenant):
        """Create the user (or reuse an existing one) and ensure the link."""
        with transaction.atomic():
            user = User.objects.filter(email__iexact=email).first()
            created = False
            if user is None:
                # Unusable-password marker, matching the mutation and the seed
                # script: the person is forced through magic-link / reset to
                # set their own credentials rather than being handed one.
                user = User.objects.create(
                    username=email,
                    email=email,
                    first_name=first_name or "",
                    last_name=last_name or "",
                    password="!" + get_random_string(40),
                    is_active=True,
                    is_staff=False,
                    is_superuser=False,
                    role_id=role_id,
                )
                created = True
            elif not user.is_active:
                # delete_user soft-deletes by flipping this; magic-link login
                # hard-rejects inactive users, so a re-invite without this
                # looks successful and dead-ends.
                user.is_active = True
                user.save(update_fields=["is_active"])
                self.stdout.write("  ~ reactivated a previously removed account")

            if role_id == ROLE_MAP["admin"]:
                tenants_qs = Tenant.objects.all()
            elif tenant is not None:
                tenants_qs = Tenant.objects.filter(id=tenant.id)
            else:
                tenants_qs = Tenant.objects.none()
            for t in tenants_qs:
                link, was_created = TenantedUser.objects.get_or_create(
                    user=user, tenant=t, defaults={"is_active": True}
                )
                if not was_created and not link.is_active:
                    link.is_active = True
                    link.save(update_fields=["is_active"])
            return user, created

    def _send_invite(self, user) -> None:
        """Same token, link and envelope as `invite_user`."""
        from tenants.envelopes import MagicLinkMailer

        token = signing.dumps(
            {"u": user.id, "e": user.email}, salt="spark.magic-link.v1"
        )
        base = getattr(
            settings, "ADMIN_FRONTEND_URL", "https://admin.igniteproductions.co"
        ).rstrip("/")
        link = f"{base}/magic/{token}"

        mobile_link = None
        try:
            from tenants.mutations import _build_magic_link_mobile

            mobile_link = _build_magic_link_mobile(token)
        except Exception:  # noqa: BLE001 — web link alone is still usable
            pass

        # A client contact reads email on a laptop and wants the admin web, so
        # the app link is never the primary CTA for them; only a BA gets that.
        is_ambassador = getattr(user, "role_id", None) == ROLE_MAP["ambassador"]

        mailer = MagicLinkMailer(
            user=user,
            link=link,
            mobile_link=mobile_link,
            app_primary=is_ambassador,
            expires_minutes=30,
        )
        # Surfaced, not swallowed: the mutation logs and returns success on a
        # send failure because a UI button shouldn't 500. Here the whole point
        # of the run is the email, so a failure has to be visible in the log.
        mailer.send_now()

        self.stdout.write("")
        self.stdout.write("=" * 72)
        self.stdout.write(
            self.style.SUCCESS(f"Sign-in link emailed to {user.email}.")
        )
        self.stdout.write(
            "The link expires in 30 minutes; they can request a fresh one from "
            "the login page, so an unopened invite isn't a dead end."
        )
        self.stdout.write("=" * 72)
