"""Turn a tenant's legacy 24h/3h AmbassadorJob reminder EMAILS off (or on).

Why: the "Send Event Confirmation" tab sends its own 24h-before and 3h-before
emails off ``EventConfirmation``. The older ``jobs/`` cron
(ambassador-job-reminders) sends a differently-shaped 24h/3h email off
``AmbassadorJob``. A brand on both gets two reminders per shift that don't look
like each other. Setting ``Tenant.suppress_job_reminder_emails`` says "this
tenant's shift reminders come from the confirmation tab now".

SCOPE IS EMAIL ONLY. The same cron also fires 15-min-before and
15-min-after-end PUSH notifications, which the confirmation tab has no
equivalent for. Those keep firing — this flag never touches them, because
suppressing them would delete a reminder rather than replace one.

Reversible: ``--on`` puts the legacy emails back.

Dry-run by default; pass ``--apply`` to write. Run in prod via the secret-gated
``/internal/cron/set-tenant-job-reminder-emails`` endpoint + the
set-tenant-job-reminder-emails workflow.
"""

from __future__ import annotations

from django.core.management.base import BaseCommand, CommandError

from tenants.models import Tenant


class Command(BaseCommand):
    help = (
        "Suppress (default) or restore the legacy 24h/3h AmbassadorJob reminder "
        "emails for one tenant. Dry-run by default; pass --apply to write."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--tenant-name",
            default=None,
            help="Tenant by name, case-insensitive (e.g. 'Liquid Death').",
        )
        parser.add_argument(
            "--tenant-slug",
            default=None,
            help="Tenant by slug (e.g. 'liquid-death'). Takes precedence.",
        )
        parser.add_argument(
            "--on",
            action="store_true",
            help="RESTORE the legacy emails (sets the flag back to False).",
        )
        parser.add_argument(
            "--apply",
            action="store_true",
            help="Actually write. Without this the command only reports.",
        )

    def handle(self, *args, **opts):
        slug = (opts.get("tenant_slug") or "").strip()
        name = (opts.get("tenant_name") or "").strip()
        if not slug and not name:
            raise CommandError("Pass --tenant-slug or --tenant-name.")

        qs = Tenant.objects.all()
        tenant = (
            qs.filter(slug__iexact=slug).first()
            if slug
            else qs.filter(name__iexact=name).first()
        )
        if tenant is None:
            raise CommandError(
                f"No tenant matching {slug or name!r}. "
                f"Run list_tenants to see the options."
            )

        # --on RESTORES the legacy emails, so the flag goes to False.
        suppress = not bool(opts.get("on"))
        current = bool(getattr(tenant, "suppress_job_reminder_emails", False))
        apply_ = bool(opts.get("apply"))

        self.stdout.write(
            f"tenant: [{tenant.id}] {tenant.name} (slug={tenant.slug})\n"
            f"  suppress_job_reminder_emails: {current} -> {suppress}"
        )

        if current == suppress:
            self.stdout.write(
                self.style.SUCCESS("Already in the requested state; nothing to do.")
            )
            return

        if not apply_:
            self.stdout.write(
                self.style.WARNING(
                    "Dry run — nothing written. Re-run with --apply to change it."
                )
            )
            return

        # .update() rather than .save(): a config flip shouldn't fire Tenant
        # post_save side effects (admin auto-linking, mirrors).
        Tenant.objects.filter(pk=tenant.pk).update(
            suppress_job_reminder_emails=suppress
        )
        self.stdout.write(
            self.style.SUCCESS(
                f"{'Suppressed' if suppress else 'Restored'} the legacy 24h/3h "
                f"AmbassadorJob reminder EMAILS for {tenant.name}. "
                f"The 15-min push reminders are unchanged."
            )
        )
