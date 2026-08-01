"""Guards on `purge_walkin_event`.

The standing check-in link lets any BA create an Event by typing an address,
so junk rows are inevitable and a removal tool is required. The risk is
obvious: a tool that deletes events by id is one fat finger away from
destroying a real activation. These tests are about the REFUSALS, not the
happy path — every guard here exists because the alternative is losing
somebody's field work.

Precedent for taking this seriously: an earlier row-move on the LD workbook
went to the wrong rows and double-counted 7,728 cans on client-facing KPIs.
The fix then, and the rule now, is a content guard on every destructive op.
"""
import uuid

import pytest
from django.core.management import call_command
from django.utils import timezone as dj_tz
from django.core.management.base import CommandError

from ambassadors.models import AmbassadorEvent, Attendance, Source
from ambassadors.tests.base import AmbassadorsGraphQLTestCase
from events.models import Event


@pytest.mark.django_db(transaction=True)
class TestPurgeWalkinEvent(AmbassadorsGraphQLTestCase):
    @pytest.fixture(autouse=True)
    def setup(self, db):
        self.system_user = self.get_system_user()
        self.roles = self.setup_default_roles()
        uid = str(uuid.uuid4())[:8]
        self.tenant = self.create_tenant(name=f"Purge Test {uid}")
        self.event = Event.objects.create(
            tenant=self.tenant, name="ZZ Junk Typo Store",
            address="999 Nowhere Way", created_by=self.system_user,
        )
        ba_user = self.create_user(
            username=f"ba-{uid}", email=f"ba-{uid}@example.com",
            role=self.roles["ambassador"],
        )
        self.ambassador = self.create_ambassador(ba_user)

    def _purge(self, expect="ZZ Junk", apply=True):
        call_command(
            "purge_walkin_event", event_uuid=str(self.event.uuid),
            expect_name=expect, apply=apply,
        )

    # -- the happy path -----------------------------------------------------

    def test_removes_a_junk_event_and_its_pending_booking(self):
        AmbassadorEvent.objects.create(
            event=self.event, ambassador=self.ambassador, is_approved=False,
            created_by=self.system_user, tenant=self.tenant,
        )
        self._purge()
        assert not Event.objects.filter(id=self.event.id).exists()
        assert not AmbassadorEvent.objects.filter(event_id=self.event.id).exists()

    def test_dry_run_deletes_nothing(self):
        self._purge(apply=False)
        assert Event.objects.filter(id=self.event.id).exists()

    def test_the_ba_stub_survives(self):
        """Walk-up BAs are soft-deactivated, never hard-deleted (RESTRICT
        FKs). Purging their junk event must not touch the person."""
        AmbassadorEvent.objects.create(
            event=self.event, ambassador=self.ambassador, is_approved=False,
            created_by=self.system_user, tenant=self.tenant,
        )
        self._purge()
        self.ambassador.refresh_from_db()
        assert self.ambassador.id is not None

    # -- the refusals (the whole point) -------------------------------------

    def test_refuses_when_the_name_does_not_match(self):
        with pytest.raises(CommandError, match="does not contain"):
            self._purge(expect="Some Other Store")
        assert Event.objects.filter(id=self.event.id).exists()

    def test_refuses_when_somebody_clocked_in(self):
        source, _ = Source.objects.get_or_create(name="clock_in")
        Attendance.objects.create(
            ambassador=self.ambassador, event=self.event, source=source,
            clock_time=dj_tz.now(),
        )
        with pytest.raises(CommandError, match="clock punch"):
            self._purge()
        assert Event.objects.filter(id=self.event.id).exists()

    def test_refuses_when_the_booking_is_approved(self):
        AmbassadorEvent.objects.create(
            event=self.event, ambassador=self.ambassador, is_approved=True,
            created_by=self.system_user, tenant=self.tenant,
        )
        with pytest.raises(CommandError, match="APPROVED"):
            self._purge()
        assert Event.objects.filter(id=self.event.id).exists()

    def test_refuses_on_an_unknown_uuid(self):
        with pytest.raises(CommandError, match="No event"):
            call_command(
                "purge_walkin_event", event_uuid=str(uuid.uuid4()),
                expect_name="anything", apply=True,
            )

    def test_refuses_a_blank_expect_name(self):
        """An empty guard is no guard — `"" in anything` is always True."""
        with pytest.raises(CommandError, match="blank"):
            call_command(
                "purge_walkin_event", event_uuid=str(self.event.uuid),
                expect_name="   ", apply=True,
            )
        assert Event.objects.filter(id=self.event.id).exists()
