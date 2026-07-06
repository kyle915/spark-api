"""End-to-end coverage for staff_tenant_events using the REAL committed
feel_free_staffing spec: dry-run inertness, BA creation through the admin
Add-a-BA service (welcome email with app buttons), job creation at the spec
rate, per-group booking fan-out, and idempotent re-apply."""
import io
from decimal import Decimal

import pytest
from django.core import mail
from django.core.management import call_command

from ambassadors.models import Ambassador, AmbassadorEvent
from events.models import Event
from events.tests.base import EventsGraphQLTestCase
from jobs.models import AmbassadorJob, Job


@pytest.mark.django_db(transaction=True)
class TestStaffTenantEvents(EventsGraphQLTestCase):
    @pytest.fixture(autouse=True)
    def setup(self):
        self.system_user = self.get_system_user()
        # The service resolves the global Ambassador role (prod pk=1).
        from tenants.models import Role
        from tenants.tests.base import ensure_role
        from utils.utils import ROLE_ID
        ensure_role(
            "Ambassador", slug=Role.AMBASSADOR_SLUG,
            pk=ROLE_ID.Ambassadors, created_by=self.system_user)
        self.tenant = self.create_tenant(name="Feel Free")
        self.field = self.create_event_type(name="Field Sampling", tenant=self.tenant)
        status = self.create_event_status(name="Approved", tenant=self.tenant)

        def mk(name):
            return Event.objects.create(
                name=name, tenant=self.tenant, event_type=self.field,
                status=status, address="13101 NE 16th Ave, Miami, FL 33161",
                created_by=self.system_user,
            )

        self.mia1 = mk("Miami — Wynwood · 7/2")
        self.mia2 = mk("Miami — Brickell · 7/3")
        self.aus1 = mk("Austin — Rainey St · 7/3")
        self.tpa1 = mk("Tampa / St. Pete — Curtis Hixon · 7/2")

    def _run(self, *args):
        out = io.StringIO()
        call_command(
            "staff_tenant_events",
            "--owner-email", self.system_user.email,
            *args,
            stdout=out,
        )
        return out.getvalue()

    def test_dry_run_reports_and_writes_nothing(self):
        report = self._run()
        assert "DRY-RUN" in report
        assert "WOULD create + send welcome email" in report
        assert Job.objects.count() == 0
        assert AmbassadorEvent.objects.count() == 0
        assert len(mail.outbox) == 0

    def test_apply_creates_bas_jobs_and_bookings(self):
        report = self._run("--apply")
        # 8 new BAs across the 4 staffed markets, each welcomed once.
        assert Ambassador.objects.count() == 8
        welcome = [m for m in mail.outbox if "Welcome to Spark" in m.subject]
        assert len(welcome) == 8
        assert all(a.is_active for a in Ambassador.objects.all())

        # Every event got a job at the spec rate; Tampa (no BAs) stays pending.
        assert Job.objects.count() == 4
        mia_job = Job.objects.get(event=self.mia1)
        # rate FK must be set: the GraphQL JobType declares it non-null,
        # so a rate-less Job breaks the whole admin JobsQuery.
        assert mia_job.rate is not None and mia_job.rate.amount == Decimal("30.00")
        assert mia_job.hourly_rate == Decimal("30.00")
        assert mia_job.total_hours == Decimal("5")
        assert mia_job.lifecycle_status == Job.STATUS_FILLED
        tpa_job = Job.objects.get(event=self.tpa1)
        assert tpa_job.lifecycle_status == Job.STATUS_PENDING

        # Market pairs booked on their market's events only.
        mia_bookings = AmbassadorEvent.objects.filter(event=self.mia1)
        assert mia_bookings.count() == 2
        assert all(b.is_approved for b in mia_bookings)
        assert set(
            mia_bookings.values_list("ambassador__user__email", flat=True)
        ) == {"aarchie00@gmail.com", "gabyiglesiaspr@gmail.com"}
        aus_emails = set(
            AmbassadorEvent.objects.filter(event=self.aus1)
            .values_list("ambassador__user__email", flat=True)
        )
        assert aus_emails == {"imtaradavis@gmail.com",
                              "rocio.12_virgo1996@hotmail.com"}
        assert AmbassadorEvent.objects.filter(event=self.tpa1).count() == 0
        # AmbassadorJob assignment rows exist for every booking.
        assert AmbassadorJob.objects.count() == AmbassadorEvent.objects.count()
        assert "assignments=6" in report and "bookings=6" in report

    def test_reapply_heals_rateless_jobs(self):
        # Jobs created by the pre-fix run had rate=NULL — a re-apply must
        # backfill the FK instead of skipping existing jobs.
        self._run("--apply")
        Job.objects.all().update(rate=None)
        self._run("--apply")
        assert not Job.objects.filter(rate__isnull=True).exists()

    def test_reapply_is_idempotent_and_sends_no_new_email(self):
        self._run("--apply")
        sent = len(mail.outbox)
        report = self._run("--apply")
        assert Ambassador.objects.count() == 8
        assert Job.objects.count() == 4
        assert AmbassadorJob.objects.count() == 6
        assert len(mail.outbox) == sent
        assert "ba_existing=8" in report or "ba_existing" in report
