"""Weekly mileage report: prior Mon-Sun window, per-BA rollup, CSV email,
silent when empty."""
import datetime
import io
from decimal import Decimal

import pytest
from django.core import mail
from django.core.management import call_command
from django.utils import timezone

from ambassadors.models import Ambassador, MileageSession
from events.models import Event
from events.tests.base import EventsGraphQLTestCase


@pytest.mark.django_db(transaction=True)
class TestWeeklyMileageReport(EventsGraphQLTestCase):
    @pytest.fixture(autouse=True)
    def setup(self, settings):
        settings.MILEAGE_REPORT_EMAILS = ["kyle@igniteproductions.co"]
        self.system_user = self.get_system_user()
        self.tenant = self.create_tenant(name="Feel Free")
        etype = self.create_event_type(name="Field Sampling", tenant=self.tenant)
        status = self.create_event_status(name="Approved", tenant=self.tenant)
        self.event = Event.objects.create(
            name="Miami — Wynwood · 7/2", tenant=self.tenant,
            event_type=etype, status=status, created_by=self.system_user,
        )
        from django.contrib.auth import get_user_model
        User = get_user_model()
        ba_user = User.objects.create_user(
            username="alicia", email="aarchie00@gmail.com",
            first_name="Alicia", last_name="Archie", is_active=True,
            role=self._role(),
        )
        self.amb = Ambassador.objects.create(
            user=ba_user, is_active=True, created_by=ba_user, updated_by=ba_user,
        )
        # Two completed trips inside the target week (ending Sun 2026-06-28),
        # one outside it.
        self.week_end = datetime.date(2026, 6, 28)
        inside = timezone.make_aware(datetime.datetime(2026, 6, 26, 20, 0))
        outside = timezone.make_aware(datetime.datetime(2026, 6, 20, 20, 0))
        for ended_at, miles in ((inside, "12.4"), (inside, "8.1"), (outside, "99")):
            s = MileageSession.objects.create(
                tenant=self.tenant, ambassador=self.amb, event=self.event,
                status=MileageSession.STATUS_COMPLETED,
                total_miles=Decimal(miles),
                rate_per_mile=Decimal("0.725"),
                reimbursement_amount=Decimal(miles) * Decimal("0.725"),
            )
            # ended_at is auto-managed nowhere; set explicitly.
            MileageSession.objects.filter(pk=s.pk).update(ended_at=ended_at)

    def _role(self):
        from tenants.models import Role
        from tenants.tests.base import ensure_role
        from utils.utils import ROLE_ID
        return ensure_role(
            "Ambassador", slug=Role.AMBASSADOR_SLUG,
            pk=ROLE_ID.Ambassadors, created_by=self.system_user)

    def _run(self, *args):
        out = io.StringIO()
        call_command(
            "weekly_mileage_report",
            "--week-ending", self.week_end.isoformat(),
            *args, stdout=out,
        )
        return out.getvalue()

    def test_report_rolls_up_week_and_emails_csv(self):
        report = self._run()
        assert "sessions   : 2" in report
        assert "TOTAL owed : $14.86" in report  # (12.4 + 8.1) * 0.725
        assert len(mail.outbox) == 1
        msg = mail.outbox[0]
        assert "$14.86" in msg.subject
        assert msg.attachments, "CSV attachment missing"

    def test_dry_run_sends_nothing(self):
        report = self._run("--dry-run")
        assert "DRY-RUN" in report
        assert len(mail.outbox) == 0

    def test_empty_week_sends_nothing(self):
        MileageSession.objects.all().delete()
        report = self._run()
        assert "Nothing to report" in report
        assert len(mail.outbox) == 0
