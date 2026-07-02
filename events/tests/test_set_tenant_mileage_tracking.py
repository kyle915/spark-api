"""Coverage for the bulk mileage toggle
(events/management/commands/set_tenant_mileage_tracking.py): scoping by
event type, dry-run inertness, rate parsing, and the exact prod invocation
shape (invoked via call_command — a compile check can't catch a broken
command; see test_sync_tenant_to_sheet_cmd for the war story).
"""
import io
from decimal import Decimal

import pytest
from django.core.management import CommandError, call_command

from events.models import Event
from events.tests.base import EventsGraphQLTestCase


@pytest.mark.django_db(transaction=True)
class TestSetTenantMileageTracking(EventsGraphQLTestCase):
    @pytest.fixture(autouse=True)
    def setup(self):
        self.system_user = self.get_system_user()
        self.tenant = self.create_tenant(name="Feel Free")
        self.field = self.create_event_type(name="Field Sampling", tenant=self.tenant)
        self.other = self.create_event_type(name="Retail Sampling", tenant=self.tenant)
        status = self.create_event_status(name="Approved", tenant=self.tenant)

        def mk(name, etype):
            return Event.objects.create(
                name=name, tenant=self.tenant, event_type=etype,
                status=status, created_by=self.system_user,
            )

        self.e1 = mk("Miami — Wynwood · 7/2", self.field)
        self.e2 = mk("Austin — Rainey · 7/3", self.field)
        self.e3 = mk("Kroger demo", self.other)

    def _run(self, *args):
        out = io.StringIO()
        call_command("set_tenant_mileage_tracking", *args, stdout=out)
        return out.getvalue()

    def test_dry_run_reports_but_writes_nothing(self):
        report = self._run("--tenant-name", "feel free", "--event-type", "Field Sampling")
        assert "matched     : 2" in report
        assert "DRY-RUN" in report
        assert not Event.objects.filter(track_mileage=True).exists()

    def test_apply_scopes_to_event_type_and_sets_rate(self):
        report = self._run(
            "--tenant-name", "Feel Free", "--event-type", "Field Sampling", "--apply",
        )
        assert "updated     : 2" in report
        for e in (self.e1, self.e2):
            e.refresh_from_db()
            assert e.track_mileage is True
            assert e.mileage_rate == Decimal("0.725")
        self.e3.refresh_from_db()
        assert self.e3.track_mileage is False

    def test_off_disables(self):
        self._run("--tenant-name", "Feel Free", "--apply")
        self._run("--tenant-name", "Feel Free", "--off", "--apply")
        assert not Event.objects.filter(track_mileage=True).exists()

    def test_unknown_tenant_errors(self):
        with pytest.raises(CommandError, match="Tenant not found"):
            self._run("--tenant-name", "Nope Inc")
