"""Approving a recap is what logs a walk-up's hours.

Walk-ups land at is_approved=False and are excluded from payroll and KPIs
until someone confirms them in the Walk-ups queue. Ignite has no capacity to
work that queue in real time — they review RECAPS after the fact — so the
confirm step simply wasn't happening and hours went missing. Recap approval
now carries the same weight the Confirm button does.

The important properties: it must be idempotent, it must not disturb bookings
it doesn't own, and it must NEVER be able to fail a recap approval.
"""
import uuid

import pytest

from ambassadors.models import AmbassadorEvent
from ambassadors.tests.base import AmbassadorsGraphQLTestCase
from ambassadors.walkup import approve_booking_for_recap
from events.models import Event


@pytest.mark.django_db(transaction=True)
class TestRecapApprovalLogsHours(AmbassadorsGraphQLTestCase):
    @pytest.fixture(autouse=True)
    def setup(self, db):
        self.system_user = self.get_system_user()
        self.roles = self.setup_default_roles()
        uid = str(uuid.uuid4())[:8]
        self.tenant = self.create_tenant(name=f"Hours Test {uid}")
        self.event = Event.objects.create(
            tenant=self.tenant, name="8/1/2026 - 1 Test Way",
            address="1 Test Way", created_by=self.system_user,
        )
        ba_user = self.create_user(
            username=f"ba-{uid}", email=f"ba-{uid}@example.com",
            role=self.roles["ambassador"],
        )
        self.ambassador = self.create_ambassador(ba_user, is_active=False)

    def _booking(self, approved=False, ambassador=None, event=None):
        return AmbassadorEvent.objects.create(
            event=event or self.event,
            ambassador=ambassador or self.ambassador,
            tenant=self.tenant,
            is_approved=approved,
            created_by=self.system_user,
            source=AmbassadorEvent.SOURCE_WALKUP,
        )

    def _approve(self):
        return approve_booking_for_recap(
            ambassador_id=self.ambassador.id,
            event_id=self.event.id,
            actor=self.system_user,
        )

    # -- the behaviour Kyle asked for ---------------------------------------

    def test_pending_walkup_hours_are_logged(self):
        b = self._booking()
        out = self._approve()
        b.refresh_from_db()
        assert b.is_approved is True
        assert out["bookings_approved"] == 1

    def test_a_new_ba_is_activated(self):
        """Same side effect the Confirm button had — otherwise the BA stays
        inactive and their shifts never show."""
        self._booking()
        out = self._approve()
        self.ambassador.refresh_from_db()
        assert self.ambassador.is_active is True
        assert out["ambassador_activated"] is True

    def test_the_ba_joins_the_brand_roster(self):
        from tenants.models import TenantedUser

        self._booking()
        self._approve()
        assert TenantedUser.objects.filter(
            user=self.ambassador.user, tenant=self.tenant
        ).exists()

    # -- guardrails ---------------------------------------------------------

    def test_running_twice_changes_nothing_the_second_time(self):
        """Recaps get approved, un-approved and re-approved. This must not
        thrash or double-count."""
        self._booking()
        first = self._approve()
        second = self._approve()
        assert first["bookings_approved"] == 1
        assert second["bookings_approved"] == 0

    def test_an_already_approved_booking_is_left_alone(self):
        b = self._booking(approved=True)
        out = self._approve()
        b.refresh_from_db()
        assert b.is_approved is True
        assert out["bookings_approved"] == 0

    def test_other_peoples_bookings_on_the_same_event_are_untouched(self):
        """Several BAs share one event on the standing link — approving one
        person's recap must not approve the others' hours."""
        other_user = self.create_user(
            username=f"other-{uuid.uuid4().hex[:8]}",
            email=f"other-{uuid.uuid4().hex[:8]}@example.com",
            role=self.roles["ambassador"],
        )
        other = self.create_ambassador(other_user, is_active=False)
        mine = self._booking()
        theirs = self._booking(ambassador=other)

        self._approve()
        mine.refresh_from_db()
        theirs.refresh_from_db()
        assert mine.is_approved is True
        assert theirs.is_approved is False, "approved somebody else's hours"

    def test_missing_ids_are_a_no_op(self):
        assert approve_booking_for_recap(
            ambassador_id=None, event_id=None, actor=None
        )["bookings_approved"] == 0

    def test_a_database_failure_cannot_break_the_approval(self, monkeypatch):
        """A recap approval must never 500 because a booking couldn't be
        updated — the approval itself is the thing that matters."""
        self._booking()

        def boom(*a, **k):
            raise RuntimeError("db is having a day")

        monkeypatch.setattr(
            AmbassadorEvent.objects, "select_related", boom, raising=True
        )
        out = self._approve()  # must not raise
        assert out["bookings_approved"] == 0
