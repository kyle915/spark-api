"""Coverage for Tenant.suppress_job_reminder_emails.

A brand using the "Send Event Confirmation" tab sends its own 24h/3h emails off
EventConfirmation. The older `jobs/` cron sends a differently-shaped 24h/3h
email off AmbassadorJob, so a brand on both would give a BA two reminders per
shift that don't look like each other. This flag turns the legacy pair off.

The two things that matter, and the second is the one worth guarding hardest:

1. The 24h/3h EMAILS stop for a suppressed tenant, and keep working for
   everyone else.
2. The 15-min-before and 15-min-after-end PUSH reminders on that same cron
   KEEP FIRING. They have no equivalent in the confirmation tab, so catching
   them in this flag would silently delete a reminder rather than replace it —
   and nothing in the product would tell you.
"""

from __future__ import annotations

from io import StringIO
from unittest.mock import patch

import pytest
from django.core.management import call_command

MAIL_PATH = "utils.mailer.Mailer.send"


@pytest.mark.django_db
class TestSuppressJobReminderEmails:
    def test_flag_defaults_off_so_other_brands_are_untouched(self):
        from tenants.models import Tenant

        assert (
            Tenant._meta.get_field("suppress_job_reminder_emails").default is False
        )

    def test_suppressed_tenant_sends_no_reminder_email(self):
        """The guard lives in the shared sender, so it holds no matter who calls."""
        from jobs.tasks import _send_ambassador_job_exact_reminder

        class _Tenant:
            id = 1
            suppress_job_reminder_emails = True

        class _Status:
            slug = "approved"

        class _Job:
            id = 7
            tenant_id = 1
            tenant = _Tenant()
            status = _Status()
            reminder_sent_at = None

        with patch(
            "jobs.tasks._get_ambassador_job_for_reminder", return_value=_Job()
        ), patch(
            "jobs.tasks._event_trigger_at_hours_before_utc"
        ) as trigger, patch(MAIL_PATH) as send:
            from django.utils import timezone as djtz

            trigger.return_value = djtz.now()
            result = _send_ambassador_job_exact_reminder(
                7,
                expected_trigger_at_iso=None,
                hours_before=24,
                reminder_field="reminder_sent_at",
                mailer_class=object,
                reminder_label="24h",
            )

        assert result == 0
        assert send.call_count == 0

    def test_command_excludes_suppressed_tenants_from_the_email_specs_only(self):
        """The dry-run report must match what would really be sent, and the two
        PUSH specs must NOT be filtered."""
        import inspect

        from jobs.management.commands import send_ambassador_job_reminders

        src = inspect.getsource(send_ambassador_job_reminders)
        # The email-eligible queryset exists and excludes the flag.
        assert "emails = base.exclude(tenant__suppress_job_reminder_emails=True)" in src
        # 24h + 3h read from it; the two pushes still read the unfiltered base.
        assert src.count("emails.filter(") == 2
        assert src.count("base.filter(") == 2

    def test_setter_command_is_dry_run_by_default_and_reversible(self):
        from tenants.models import Tenant
        from tenants.tests.base import ensure_role
        from django.contrib.auth import get_user_model

        User = get_user_model()
        role = ensure_role("System")
        user = User.objects.create_user(
            username="sys-suppress", email="sys-suppress@spark.local", role=role
        )
        if not role.created_by:
            role.created_by = user
            role.save()
        tenant = Tenant.objects.create(
            name="Liquid Death", slug="liquid-death", created_by=user
        )

        # Dry run writes nothing.
        out = StringIO()
        call_command(
            "set_tenant_job_reminder_emails",
            "--tenant-slug", "liquid-death",
            stdout=out,
        )
        tenant.refresh_from_db()
        assert tenant.suppress_job_reminder_emails is False
        assert "Dry run" in out.getvalue()

        # --apply writes.
        call_command(
            "set_tenant_job_reminder_emails",
            "--tenant-slug", "liquid-death", "--apply",
            stdout=StringIO(),
        )
        tenant.refresh_from_db()
        assert tenant.suppress_job_reminder_emails is True

        # --on --apply puts the legacy emails back.
        call_command(
            "set_tenant_job_reminder_emails",
            "--tenant-slug", "liquid-death", "--on", "--apply",
            stdout=StringIO(),
        )
        tenant.refresh_from_db()
        assert tenant.suppress_job_reminder_emails is False

    def test_setter_command_rejects_an_unknown_tenant(self):
        from django.core.management.base import CommandError

        with pytest.raises(CommandError, match="No tenant matching"):
            call_command(
                "set_tenant_job_reminder_emails",
                "--tenant-slug", "not-a-real-brand",
                stdout=StringIO(),
            )
