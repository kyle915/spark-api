"""Invocation coverage for retext_tenant_events + resend_ba_welcome."""
import io

import pytest
from django.core import mail
from django.core.management import call_command

from ambassadors.models import Ambassador
from events.models import Event
from events.tests.base import EventsGraphQLTestCase
from jobs.models import Job, JobTitle


@pytest.mark.django_db(transaction=True)
class TestRetextTenantEvents(EventsGraphQLTestCase):
    @pytest.fixture(autouse=True)
    def setup(self):
        self.system_user = self.get_system_user()
        self.tenant = self.create_tenant(name="Feel Free")
        self.field = self.create_event_type(name="Field Sampling", tenant=self.tenant)
        status = self.create_event_status(name="Approved", tenant=self.tenant)
        self.event = Event.objects.create(
            name="Miami — Wynwood · 7/2", tenant=self.tenant,
            event_type=self.field, status=status,
            notes="2x BAs per shift (5.5h billable each).",
            created_by=self.system_user,
        )
        title = JobTitle.objects.create(
            tenant=self.tenant, name="Brand Ambassador",
            created_by=self.system_user,
        )
        self.job = Job.objects.create(
            tenant=self.tenant, event=self.event, job_title=title,
            name=self.event.name, description=self.event.notes,
            address="x", created_by=self.system_user,
        )

    def _run(self, *args):
        out = io.StringIO()
        call_command(
            "retext_tenant_events",
            "--tenant-name", "Feel Free", "--event-type", "Field Sampling",
            "--find", "5.5h billable", "--replace", "5h billable",
            *args, stdout=out,
        )
        return out.getvalue()

    def test_dry_run_counts_only(self):
        report = self._run()
        assert "Event.notes: 1 match(es)" in report
        assert "Job.description: 1 match(es)" in report
        self.event.refresh_from_db()
        assert "5.5h" in self.event.notes

    def test_apply_replaces_everywhere(self):
        self._run("--apply")
        self.event.refresh_from_db()
        self.job.refresh_from_db()
        assert "5h billable" in self.event.notes and "5.5h" not in self.event.notes
        assert "5h billable" in self.job.description and "5.5h" not in self.job.description


@pytest.mark.django_db(transaction=True)
class TestResendBaWelcome(EventsGraphQLTestCase):
    @pytest.fixture(autouse=True)
    def setup(self):
        self.system_user = self.get_system_user()
        from tenants.models import Role
        role, _ = Role.objects.get_or_create(
            slug=Role.AMBASSADOR_SLUG,
            defaults={"name": "Ambassador", "created_by": self.system_user},
        )
        from django.contrib.auth import get_user_model
        User = get_user_model()
        self.user = User.objects.create_user(
            username="felicia", email="felicia.riojas@gmail.com",
            first_name="Felicia", last_name="Riojas", role=role, is_active=True,
        )

    def _run(self, *args):
        out = io.StringIO()
        call_command(
            "resend_ba_welcome", "--email", "felicia.riojas@gmail.com",
            *args, stdout=out,
        )
        return out.getvalue()

    def test_dry_run_reports_state_only(self):
        old_password = self.user.password
        report = self._run()
        assert "last_login  : NEVER" in report
        assert "DRY-RUN" in report
        self.user.refresh_from_db()
        assert self.user.password == old_password
        assert len(mail.outbox) == 0

    def test_apply_resets_and_sends(self):
        old_password = self.user.password
        self._run("--apply")
        self.user.refresh_from_db()
        assert self.user.password != old_password
        assert self.user.requires_password_change is True
        assert Ambassador.objects.filter(user=self.user, is_active=True).exists()
        assert len(mail.outbox) == 1
        assert "Welcome to Spark" in mail.outbox[0].subject
